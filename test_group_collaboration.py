import asyncio
import copy
import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from bridge import TelegramError
from group_collaboration import GroupCollaboration, input_id
from topics_bridge import RoutedStore


class GroupCollaborationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = sqlite3.connect(':memory:')
        self.db.row_factory = sqlite3.Row
        self.db.execute('CREATE TABLE kv(key TEXT PRIMARY KEY,value TEXT)')
        self.db.execute("INSERT INTO kv VALUES('offset','0')")
        self.now, self.update_id, self.mid, self.calls = 1000, 100, 1000, []
        self.router = SimpleNamespace(config={'expected_bot_username': 'test_bot'}, owner='123',
            bot_id=444, username='test_bot', owner_allowed=lambda: True,
            store=SimpleNamespace(db=self.db, offset=lambda: int(self.db.execute(
                "SELECT value FROM kv WHERE key='offset'").fetchone()[0])), targets={}, bridges={})
        participants = []
        for key, role, topic in [('cad', 'CAD', 11), ('manager', 'Telegram Manager', 22)]:
            target = {'key': key, 'thread_id': key + '-native', 'message_thread_id': topic}
            store = RoutedStore(Path(self.tmp.name) / (key + '.sqlite'), target['thread_id'])
            bridge = SimpleNamespace(target=target, thread_id=target['thread_id'], store=store,
                codex=SimpleNamespace(turns=AsyncMock(return_value=[{'id': 'old', 'status': 'completed'}]),
                                      user_input_epoch='epoch', turn_state=SimpleNamespace(active={})),
                ready=asyncio.Event(), dispatch_lock=asyncio.Lock())
            bridge.ready.set()
            self.router.targets[key], self.router.bridges[key] = target, bridge
            participants.append({'key': key, 'role': role, 'thread_id': target['thread_id'], 'topic_id': topic})
        self.settings = {'enabled': True, 'participants': participants}
        self.db.execute('INSERT INTO kv VALUES(?,?)', (GroupCollaboration.SETTINGS, json.dumps(self.settings)))
        self.db.commit()

        async def send(method, **params):
            self.calls.append((method, copy.deepcopy(params)))
            self.mid += 1
            return {'message_id': self.mid, 'chat': {'id': int(params['chat_id'])}}

        self.router.telegram = SimpleNamespace(call=AsyncMock(side_effect=send))
        self.ui = GroupCollaboration(self.router, clock=lambda: self.now)

    async def asyncTearDown(self):
        for bridge in self.router.bridges.values():
            bridge.store.db.close()
        self.db.close()
        self.tmp.cleanup()

    def update(self, text='/collab_test@test_bot', sender=123, chat=-100123, topic=7, **extra):
        self.update_id += 1
        return {'update_id': self.update_id, 'message': {'message_id': self.update_id, 'text': text,
            'from': {'id': sender}, 'chat': {'id': chat, 'type': 'supergroup' if chat < 0 else 'private'},
            **({'message_thread_id': topic} if topic else {}), **extra}}

    async def command(self, text='/collab_test@test_bot', **extra):
        update = self.update(text, **extra)
        self.assertTrue(self.ui.ingest(update))
        await self.ui.handle_one()
        return update

    def run_row(self):
        return self.db.execute('SELECT * FROM gc_runs ORDER BY created DESC LIMIT 1').fetchone()

    def inbox(self, role):
        return self.router.bridges[role].store.db.execute('SELECT * FROM inbox ORDER BY id').fetchall()

    async def started(self):
        await self.command()
        await self.ui.tick()  # Establish group delivery before dispatching a model.
        await self.ui.tick()
        return self.run_row()

    def answer(self, stage, *, text=None, turn='test-turn', phase='final_answer', replay=False):
        run = self.run_row()
        bridge = self.router.bridges['cad' if stage == 'cad' else 'manager']
        with bridge.store.db:
            bridge.store.db.execute("UPDATE inbox SET state='done',turn_id=? WHERE id=?",
                                    (turn, input_id(run['id'], stage)))
        item = {'type': 'agentMessage', 'phase': phase,
                'text': self.ui.expected(run, stage) if text is None else text}
        self.ui.on_item(bridge, turn, item, replay=replay)

    async def test_exact_two_existing_sessions_round_trip_and_no_history_leak(self):
        run = await self.started()
        self.assertEqual(run['state'], 'cad_queued')
        self.assertEqual(len(self.inbox('cad')), 1)
        self.assertEqual(len(self.inbox('manager')), 0)
        guard = json.loads(self.inbox('cad')[0]['main_guard_json'])
        self.assertEqual(guard, {'target_key': 'cad', 'thread_id': 'cad-native', 'topic_id': 11})
        self.answer('cad', text='Unrelated private work and progress')
        await self.ui.tick()
        self.assertEqual(self.run_row()['state'], 'cad_queued')
        self.answer('cad')
        await self.ui.tick()
        await self.ui.tick()
        self.assertEqual(self.run_row()['state'], 'manager_queued')
        self.assertEqual(len(self.inbox('manager')), 1)
        self.assertIn(self.ui.expected(run, 'cad'), self.inbox('manager')[0]['text'])
        self.answer('manager')
        await self.ui.tick()
        for _ in range(5):
            await self.ui.tick()
        self.assertEqual(self.run_row()['state'], 'done')
        self.assertEqual((len(self.inbox('cad')), len(self.inbox('manager'))), (1, 1))
        self.assertEqual(len(self.calls), 3)
        self.assertNotIn('Unrelated', json.dumps(self.calls))
        self.assertTrue(all(p['chat_id'] == '-100123' and p['message_thread_id'] == 7 for _, p in self.calls))

    async def test_owner_bot_command_forward_and_attachment_isolation(self):
        for update in [self.update(sender=999), self.update(**{'from': {'id': 123, 'is_bot': True}})]:
            self.assertTrue(self.ui.ingest(update))
        for update in [self.update('/collab_test@other_bot'), self.update('/collab_test extra'),
                       self.update('ordinary group conversation'), self.update(forward_origin={}),
                       self.update(document={'file_id': 'file', 'file_name': 'x.txt'})]:
            self.assertFalse(self.ui.ingest(update))
        self.assertFalse(await self.ui.handle_one())
        self.assertIsNone(self.run_row())
        self.assertEqual(self.inbox('cad'), [])

    async def test_disabled_and_private_commands_only_explain_setup(self):
        self.settings['enabled'] = False
        self.db.execute('UPDATE kv SET value=? WHERE key=?', (json.dumps(self.settings), self.ui.SETTINGS))
        await self.command()
        await self.ui.tick()
        await self.command(chat=123, topic=None)
        await self.ui.tick()
        self.assertIsNone(self.run_row())
        self.assertEqual(self.inbox('cad'), [])
        self.assertEqual(len(self.calls), 2)

    async def test_manager_waits_for_current_user_work_and_status_remains_available(self):
        await self.started()
        manager = self.router.bridges['manager']
        manager.store.accept({'update_id': 888}, {'chat': {'id': 123}, 'text': 'real user work', 'message_thread_id': 22})
        manager.store.db.execute("UPDATE inbox SET state='running',turn_id='current-user-turn' WHERE id=888")
        manager.store.db.commit()
        self.answer('cad')
        await self.ui.tick()
        await self.ui.tick()
        self.assertEqual(self.run_row()['state'], 'manager_wait')
        self.assertEqual(len(self.inbox('manager')), 1)
        manager.codex.turns.assert_not_awaited()
        await self.command('/collab_status')
        await self.ui.tick()
        self.assertIn('等待 Telegram Manager', self.calls[-1][1]['text'])
        manager.store.db.execute("UPDATE inbox SET state='done' WHERE id=888")
        manager.store.db.commit()
        self.now += 5
        await self.ui.tick()
        self.assertEqual(len(self.inbox('manager')), 2)
        self.assertEqual(self.run_row()['state'], 'manager_queued')

    async def test_duplicate_start_restart_and_replay_cannot_repeat_inputs(self):
        update = await self.command()
        self.ui.ingest(copy.deepcopy(update))
        await self.ui.handle_one()
        await self.ui.tick()
        await self.ui.tick()
        self.ui = GroupCollaboration(self.router, clock=lambda: self.now)
        self.ui.recover()
        self.now += 5
        await self.ui.tick()
        self.assertEqual(len(self.inbox('cad')), 1)
        self.answer('cad', replay=True)
        self.answer('cad', replay=True)
        await self.ui.tick()
        await self.ui.tick()
        self.assertEqual(len(self.inbox('manager')), 1)
        await self.command()
        await self.ui.tick()
        self.assertEqual(self.db.execute('SELECT count(*) FROM gc_runs').fetchone()[0], 1)

    async def test_nonce_turn_client_and_final_phase_must_all_match(self):
        await self.started()
        run = self.run_row()
        cad = self.router.bridges['cad']
        self.answer('cad', phase='commentary')
        self.assertIsNone(self.run_row()['cad_reply'])
        self.ui.on_item(cad, 'wrong-turn', {'type': 'agentMessage', 'text': self.ui.expected(run, 'cad')})
        self.assertIsNone(self.run_row()['cad_reply'])
        cad.store.db.execute("UPDATE inbox SET client_id='other-client' WHERE id=?", (input_id(run['id'], 'cad'),))
        cad.store.db.commit()
        self.ui.on_item(cad, 'test-turn', {'type': 'agentMessage', 'text': self.ui.expected(run, 'cad')})
        self.assertIsNone(self.run_row()['cad_reply'])
        self.assertEqual(len(self.inbox('manager')), 0)

    async def test_stop_cancels_only_pending_test_not_running_user_task(self):
        await self.started()
        await self.command('/collab_stop')
        await self.ui.tick()
        self.assertEqual(self.run_row()['state'], 'stopped')
        self.assertEqual(self.inbox('cad')[0]['state'], 'cancelled')
        self.assertEqual(self.inbox('manager'), [])
        self.assertFalse(any(m.startswith('turn/') for m, _ in self.calls))

    async def test_timeout_and_rebinding_prevent_downstream_dispatch(self):
        await self.started()
        self.now += 601
        await self.ui.tick()
        self.assertEqual(self.run_row()['state'], 'timeout')
        self.assertEqual(self.inbox('cad')[0]['state'], 'cancelled')
        self.assertEqual(self.inbox('manager'), [])

    async def test_group_start_unknown_is_not_repeated_or_dispatched_after_restart(self):
        self.router.telegram.call.side_effect = ConnectionError('group send result unknown')
        await self.command()
        await self.ui.tick()
        self.ui = GroupCollaboration(self.router, clock=lambda: self.now)
        self.ui.recover()
        await self.ui.tick()
        self.assertEqual(self.router.telegram.call.await_count, 1)
        self.assertEqual(self.inbox('cad'), [])
        self.assertEqual(self.run_row()['state'], 'failed')

    async def test_group_rate_limit_retries_after_delay_before_dispatch(self):
        original = self.router.telegram.call.side_effect
        self.router.telegram.call.side_effect = TelegramError('sendMessage', 429, 'rate limited', retry_after=20)
        await self.command()
        await self.ui.tick()
        self.now += 19
        await self.ui.tick()
        self.assertEqual(self.inbox('cad'), [])
        self.assertEqual(self.router.telegram.call.await_count, 1)
        self.now += 1
        self.router.telegram.call.side_effect = original
        await self.ui.tick()
        await self.ui.tick()
        self.assertEqual(len(self.inbox('cad')), 1)

    async def test_new_user_input_arriving_during_idle_check_wins(self):
        await self.command()
        await self.ui.tick()
        cad = self.router.bridges['cad']

        async def read(limit=1):
            cad.store.accept({'update_id': 900}, {'chat': {'id': 123}, 'text': 'new user task', 'message_thread_id': 11})
            return [{'id': 'old', 'status': 'completed'}]

        cad.codex.turns.side_effect = read
        await self.ui.tick()
        self.assertEqual(self.run_row()['state'], 'cad_wait')
        self.assertEqual(len(self.inbox('cad')), 1)
        self.assertEqual(self.inbox('cad')[0]['id'], 900)


if __name__ == '__main__':
    unittest.main()
