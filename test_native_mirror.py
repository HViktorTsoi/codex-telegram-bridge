import json
from pathlib import Path
import tempfile
import unittest

from bridge import RpcError
from native_mirror import NativeMirror
from topics_bridge import TargetBridge


class NativeMirrorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name)
        (self.path / 'access.json').write_text(json.dumps({'dmPolicy': 'allowlist', 'allowFrom': ['123']}))
        self.config = {'owner_id': '123', 'thread_id': 'existing-native', 'socket_path': '/unused',
                       'telegram_state_dir': str(self.path), 'native_mirror': True, 'notification_mode': 'all'}
        self.target = {'key': 'paper', 'name': '论文', 'thread_id': 'existing-native', 'message_thread_id': 42}
        self.bridge = TargetBridge(self.config, self.path, self.target, lambda: '')
        self.now = 1000
        self.bridge.mirror = NativeMirror(self.bridge.store, clock=lambda: self.now)
        self.items, self.calls = {}, []
        test = self

        class Native:
            async def items(self, turn_id):
                test.calls.append(turn_id)
                for item in test.items[turn_id]:
                    if isinstance(item, Exception):
                        raise item
                    yield {'item': item}

            async def call(self, method, params):
                raise AssertionError('Mirroring must never start, steer or interrupt a native turn')

        self.bridge.codex = Native()

    async def asyncTearDown(self):
        self.bridge.store.db.close()
        self.tmp.cleanup()

    @staticmethod
    def turn(tid='new', status='inProgress', started=1001):
        return {'id': tid, 'status': status, 'startedAt': started}

    @staticmethod
    def user(text='桌面端输入', client_id=None, iid='user'):
        return {'id': iid, 'type': 'userMessage', 'clientId': client_id,
                'content': [{'type': 'text', 'text': text}]}

    @staticmethod
    def agent(text='**模型回复**', iid='answer', phase='final_answer'):
        return {'id': iid, 'type': 'agentMessage', 'text': text, 'phase': phase}

    def output(self):
        return self.bridge.store.db.execute('SELECT * FROM outbox ORDER BY rowid').fetchall()

    async def enable(self):
        await self.bridge.update_native_mirror([self.turn('old', 'completed', 900)])
        self.now = 1001

    async def test_initial_boundary_does_not_publish_completed_or_active_history(self):
        self.items['active'] = [self.user(), self.agent()]
        await self.bridge.update_native_mirror([self.turn('old', 'completed', 900), self.turn('active', started=999)])
        self.bridge.record_item('active', self.agent())
        self.assertEqual(self.calls, ['active'])
        self.assertEqual(self.output(), [])
        self.bridge.record_item('active', self.user('启用后的新输入', iid='later-user'))
        self.bridge.record_item('active', self.agent('启用后的回复', iid='later-answer'))
        self.items['active'].extend([self.user('启用后的新输入', iid='history-later-user'),
                                     self.agent('启用后的回复', iid='history-later-answer')])
        await self.bridge.update_native_mirror([self.turn('active', started=999)])
        self.assertEqual(len(self.output()), 2)

    async def test_completion_of_a_partial_item_present_at_enable_is_new_content(self):
        self.items['active'] = [self.user(), self.agent('尚未完整')]
        await self.bridge.update_native_mirror([self.turn('active', started=999)])
        self.bridge.record_item('active', self.agent('完整的新回复'))
        self.items['active'][-1] = self.agent('完整的新回复')
        await self.bridge.update_native_mirror([self.turn('active', started=999)])
        self.assertEqual(len(self.output()), 1)
        self.assertEqual(self.output()[0]['text'], '完整的新回复')

    async def test_new_desktop_input_then_completed_reply_keep_topic_and_deduplicate(self):
        await self.enable()
        self.items['new'] = [self.user(), self.agent('尚未结束的输出')]
        await self.bridge.update_native_mirror([self.turn()])
        self.assertEqual(len(self.output()), 1)
        self.assertIn('桌面端输入', self.output()[0]['text'])
        self.assertTrue(self.output()[0]['rich_text'].startswith('>'))
        self.bridge.record_item('new', self.agent())
        self.items['new'] = [self.user(), self.agent()]
        await self.bridge.update_native_mirror([self.turn(status='completed')])
        await self.bridge.update_native_mirror([self.turn(status='completed')])
        output = self.output()
        self.assertEqual(len(output), 2)
        self.assertTrue(all(row['message_thread_id'] == 42 for row in output))
        self.assertEqual(output[-1]['rich_text'], '**模型回复**')
        self.assertEqual(self.bridge.store.db.execute('SELECT COUNT(*) FROM inbox').fetchone()[0], 0)

    async def test_reconnect_recovers_a_future_completed_turn_without_reexecution(self):
        await self.enable()
        self.bridge.mirror = NativeMirror(self.bridge.store, clock=lambda: self.now)
        self.items['new'] = [self.user(), self.agent()]
        await self.bridge.update_native_mirror([self.turn(status='completed')])
        self.assertEqual(len(self.output()), 2)
        count = len(self.calls)
        self.bridge.mirror = NativeMirror(self.bridge.store, clock=lambda: self.now)
        await self.bridge.update_native_mirror([self.turn(status='completed')])
        self.assertEqual(len(self.calls), count)
        self.assertEqual(len(self.output()), 2)

    async def test_live_event_and_legacy_history_with_different_ids_are_copied_once(self):
        await self.enable()
        self.items['new'] = [self.user(iid='history-user-1684')]
        await self.bridge.update_native_mirror([self.turn()])
        self.bridge.record_item('new', self.user(iid='event-user-1'))
        self.bridge.record_item('new', self.agent(iid='msg-native-id'))
        self.items['new'].append(self.agent(iid='history-answer-1685'))
        await self.bridge.update_native_mirror([self.turn()])
        self.bridge.record_item('new', self.agent(iid='msg-native-id'))
        await self.bridge.update_native_mirror([self.turn(status='completed')])
        self.assertEqual(len(self.output()), 2)
        self.assertTrue(all('native-content-' in row['key'] for row in self.output()))

    async def test_upgrade_establishes_new_boundary_for_old_event_id_state(self):
        self.bridge.store.set(NativeMirror.KEY, json.dumps({'enabled': True, 'route': ['existing-native', 42], 'since': 900}))
        self.items['new'] = [self.user(iid='history-user'), self.agent(iid='history-agent')]
        await self.bridge.update_native_mirror([self.turn()])
        self.bridge.record_item('new', self.agent(iid='event-agent'))
        await self.bridge.update_native_mirror([self.turn(status='completed')])
        self.assertEqual(self.output(), [])
        self.assertEqual(self.bridge.mirror.state()['protocol'], 3)

    async def test_history_itself_can_renumber_items_after_turn_persistence(self):
        await self.enable()
        self.items['new'] = [self.user(iid='item-1')]
        await self.bridge.update_native_mirror([self.turn()])
        self.items['new'] = [self.user(iid='item-1686'), self.agent(iid='item-1687')]
        await self.bridge.update_native_mirror([self.turn(status='completed')])
        self.assertEqual(len(self.output()), 2)

    async def test_agent_ids_and_optional_phase_can_change_without_duplicate_output(self):
        await self.enable()
        self.items['new'] = [self.user(), self.agent(iid='live', phase='commentary')]
        await self.bridge.update_native_mirror([self.turn()])
        self.bridge.record_item('new', self.agent(iid='live', phase='commentary'))
        await self.bridge.update_native_mirror([self.turn()])
        self.items['new'] = [self.user(iid='different-user'), self.agent(iid='persisted', phase=None)]
        await self.bridge.update_native_mirror([self.turn(status='completed')])
        self.assertEqual(len(self.output()), 2)

    async def test_distinct_identical_messages_keep_their_history_occurrences(self):
        await self.enable()
        self.items['new'] = [self.user('继续', iid='u1'), self.agent('收到', iid='a1')]
        await self.bridge.update_native_mirror([self.turn()])
        self.bridge.record_item('new', self.agent('收到', iid='event-1'))
        await self.bridge.update_native_mirror([self.turn()])
        self.items['new'].extend([self.user('继续', iid='u2'), self.agent('收到', iid='a2')])
        self.bridge.record_item('new', self.user('继续', iid='event-2'))
        self.bridge.record_item('new', self.agent('收到', iid='event-3'))
        await self.bridge.update_native_mirror([self.turn(status='completed')])
        self.assertEqual(len(self.output()), 4)
        count = len(self.calls)
        self.bridge.mirror = NativeMirror(self.bridge.store, clock=lambda: self.now)
        await self.bridge.update_native_mirror([self.turn(status='completed')])
        self.assertEqual(len(self.calls), count)
        self.assertEqual(len(self.output()), 4)

    async def test_partial_history_failure_can_resume_without_duplicate_user_echo(self):
        await self.enable()
        self.items['new'] = [self.user(), RpcError('Disconnected')]
        with self.assertRaises(RpcError):
            await self.bridge.update_native_mirror([self.turn(status='completed')])
        self.assertEqual(len(self.output()), 1)
        self.items['new'] = [self.user(), self.agent()]
        await self.bridge.update_native_mirror([self.turn(status='completed')])
        self.assertEqual(len(self.output()), 2)

    async def test_telegram_input_and_reply_are_not_echoed_by_native_mirror(self):
        await self.enable()
        self.bridge.store.accept({'update_id': 8}, {'chat': {'id': 123}, 'message_thread_id': 42, 'text': 'TG 输入'})
        self.bridge.store.mark(8, 'running', 'new')
        self.items['new'] = [self.user(client_id='telegram-8'), self.agent()]
        await self.bridge.update_native_mirror([self.turn(status='completed')])
        self.assertEqual(self.output(), [])
        self.bridge.record_item('new', self.agent())
        self.assertEqual(len(self.output()), 1)
        self.assertEqual(self.output()[0]['text'], '**模型回复**')

    async def test_desktop_steer_of_a_telegram_turn_is_visible_without_duplicate_reply(self):
        await self.enable()
        self.bridge.store.accept({'update_id': 8}, {'chat': {'id': 123}, 'message_thread_id': 42, 'text': 'TG 输入'})
        self.bridge.store.mark(8, 'running', 'new')
        self.items['new'] = [self.user(client_id='telegram-8'), self.user('桌面补充', iid='desktop'), self.agent()]
        await self.bridge.update_native_mirror([self.turn(status='completed')])
        self.assertEqual(len(self.output()), 1)
        self.assertIn('桌面补充', self.output()[0]['text'])
        self.bridge.record_item('new', self.agent())
        self.assertEqual(len(self.output()), 2)

    async def test_another_telegram_instance_on_same_machine_is_not_treated_as_desktop(self):
        await self.enable()
        self.items['new'] = [self.user(client_id='telegram-999'), self.agent()]
        await self.bridge.update_native_mirror([self.turn(status='completed')])
        self.bridge.record_item('new', self.agent())
        self.assertEqual(self.output(), [])

    async def test_telegram_input_is_not_echoed_when_persisted_metadata_loses_client_id(self):
        await self.enable()
        self.items['new'] = [self.user(client_id='telegram-999', iid='active')]
        await self.bridge.update_native_mirror([self.turn()])
        self.items['new'] = [self.user(iid='persisted'), self.agent()]
        await self.bridge.update_native_mirror([self.turn(status='completed')])
        self.assertEqual(self.output(), [])

    async def test_reasoning_tools_and_internal_agent_messages_are_excluded(self):
        await self.enable()
        self.items['new'] = [self.user(), {'id': 'r', 'type': 'reasoning', 'text': 'private'},
                             {'id': 'tool', 'type': 'commandExecution', 'output': 'private'},
                             self.agent('private', iid='analysis', phase='analysis'),
                             self.agent('[external_agent_tool callback]', iid='internal')]
        await self.bridge.update_native_mirror([self.turn(status='completed')])
        self.assertEqual(len(self.output()), 1)
        self.assertNotIn('private', self.output()[0]['text'])

    async def test_images_are_described_without_uploading_local_files(self):
        await self.enable()
        self.items['new'] = [{'id': 'picture', 'type': 'userMessage', 'content': [
            {'type': 'localImage', 'path': '/sensitive/photo.jpg'}]}]
        await self.bridge.update_native_mirror([self.turn(status='completed')])
        self.assertIn('图片附件', self.output()[0]['text'])
        self.assertNotIn('/sensitive', self.output()[0]['text'])

    async def test_repaired_topic_establishes_boundary_and_never_moves_old_queued_output(self):
        await self.enable()
        self.items['new'] = [self.user()]
        await self.bridge.update_native_mirror([self.turn()])
        self.bridge.target['message_thread_id'] = 99
        self.items['new'].append(self.agent())
        await self.bridge.update_native_mirror([self.turn()])
        self.bridge.record_item('new', self.agent())
        self.assertEqual(len(self.output()), 1)
        self.assertEqual(self.output()[0]['message_thread_id'], 42)
        self.now += 1
        self.items['after'] = [self.user('新话题输入')]
        await self.bridge.update_native_mirror([self.turn('after', 'completed', self.now)])
        self.assertEqual(self.output()[-1]['message_thread_id'], 99)

    async def test_disabling_or_revoking_owner_stops_sync_and_reenable_does_not_replay_gap(self):
        await self.enable()
        self.items['new'] = [self.user()]
        self.bridge.target['native_mirror'] = False
        await self.bridge.update_native_mirror([self.turn()])
        self.assertFalse(self.bridge.mirror.state()['enabled'])
        self.bridge.target['native_mirror'] = True
        await self.bridge.update_native_mirror([self.turn()])
        self.assertEqual(self.calls, ['new'])
        (self.path / 'access.json').write_text(json.dumps({'dmPolicy': 'allowlist', 'allowFrom': []}))
        await self.bridge.update_native_mirror([self.turn('another')])
        self.assertFalse(self.bridge.mirror.state()['enabled'])
        self.assertEqual(self.output(), [])

    async def test_missing_topic_and_default_disabled_do_not_read_or_generate(self):
        self.bridge.target['topic_missing'] = True
        await self.bridge.update_native_mirror([self.turn()])
        self.bridge.target.pop('topic_missing')
        self.bridge.config.pop('native_mirror')
        await self.bridge.update_native_mirror([self.turn()])
        self.assertEqual(self.calls, [])
        self.assertEqual(self.bridge.mirror.state(), {})

    async def test_old_or_unknown_start_time_cannot_backfill_unseen_history(self):
        await self.enable()
        await self.bridge.update_native_mirror([self.turn('old-unseen', 'completed', 100),
                                               self.turn('no-timestamp', 'completed', None)])
        self.assertEqual(self.calls, [])
        self.assertEqual(self.output(), [])


if __name__ == '__main__':
    unittest.main()
