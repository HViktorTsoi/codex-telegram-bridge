"""Stop buttons may interrupt only their exact frozen task, without model input."""
import asyncio
import copy
import json
from pathlib import Path
import tempfile
import time
import unittest

from completion_preview import STOP_REQUEST_KEY
from status_controls import StatusControls
from topics_bridge import TopicRouter


class StatusControlTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name)
        self.config = {
            'owner_id': '123', 'telegram_state_dir': str(self.path),
            'socket_path': str(self.path / 'native.sock'), 'thread_id': 'native-a',
            'expected_bot_username': 'test_bot', 'mode': 'topics', 'host_id': 'zbox',
            'notification_mode': 'completion', 'preview_style': 'status-bar',
            'root_target': 'a', 'targets': [
                {'key': 'a', 'name': '会话 A', 'thread_id': 'native-a', 'message_thread_id': 42},
                {'key': 'b', 'name': '会话 B', 'thread_id': 'native-b', 'message_thread_id': 84},
            ],
        }
        (self.path / 'config.json').write_text(json.dumps(self.config))
        self.allow_owner()
        self.router = TopicRouter(copy.deepcopy(self.config), self.path)
        self.router.bot_id, self.router.username = 444, 'test_bot'
        self.calls, self.messages, self.native_calls = [], {}, []
        self.sequence = 10000
        test = self

        class Telegram:
            async def call(self, method, **params):
                test.calls.append((method, copy.deepcopy(params)))
                if method == 'answerCallbackQuery':
                    return True
                if method in ('sendMessage', 'sendRichMessage'):
                    test.sequence += 1
                    result = {
                        **copy.deepcopy(params), 'message_id': test.sequence,
                        'date': 1, 'from': {'id': 444, 'is_bot': True},
                        'chat': {'id': int(params['chat_id']), 'type': 'private'},
                        'message_thread_id': params.get('message_thread_id'),
                        'is_topic_message': True,
                    }
                    test.messages[result['message_id']] = result
                    return copy.deepcopy(result)
                if method in ('editMessageText', 'editMessageReplyMarkup'):
                    result = test.messages[params['message_id']]
                    result.update(copy.deepcopy(params))
                    return copy.deepcopy(result)
                if method == 'deleteMessage':
                    test.messages.pop(params['message_id'], None)
                    return True
                raise AssertionError('Unexpected Telegram API: ' + method)

        class Native:
            def __init__(self, name):
                self.name, self.latest = name, name + '-work'
                self.status = 'inProgress'
                self.query_error, self.interrupt_error = None, None
                self.query_gate, self.after_query = None, None
                self.query_started = asyncio.Event()

            async def turns(self, limit=10):
                test.native_calls.append((self.name, 'turns', {'limit': limit}))
                self.query_started.set()
                if self.query_gate:
                    await self.query_gate.wait()
                if self.query_error:
                    raise self.query_error
                result = [{'id': self.latest, 'status': self.status}]
                if self.after_query:
                    self.after_query()
                return result

            async def call(self, method, params):
                test.native_calls.append((self.name, method, copy.deepcopy(params)))
                if method != 'turn/interrupt':
                    raise AssertionError('A stop control must not start/fork model input: ' + method)
                if self.interrupt_error:
                    raise self.interrupt_error
                return {}

        self.telegram = Telegram()
        self.router.telegram = self.telegram
        self.natives = {}
        for name, bridge in self.router.bridges.items():
            bridge.telegram, bridge.username = self.telegram, 'test_bot'
            bridge.ready.set()
            self.natives[name] = bridge.codex = Native(name)
        self.assertIsInstance(self.router.status_controls, StatusControls)

    async def asyncTearDown(self):
        for bridge in self.router.bridges.values():
            bridge.store.db.close()
        self.router.store.db.close()
        self.tmp.cleanup()

    def allow_owner(self, allowed=True):
        (self.path / 'access.json').write_text(json.dumps({
            'dmPolicy': 'allowlist', 'allowFrom': ['123'] if allowed else [],
        }))

    async def bar(self, name='a'):
        bridge, native = self.router.bridges[name], self.natives[name]
        turn = {'id': native.latest, 'status': 'inProgress', 'startedAt': time.time() - 1}
        bridge.store.enqueue_at('turn:' + turn['id'] + ':public', '123', '正在处理 ' + name,
                                bridge.target['message_thread_id'])
        await bridge.delivery.update(self.telegram, turns=[turn])
        identity = dict(bridge.delivery._group(turn['id']))
        state = bridge.delivery.body_preview.load(identity)
        self.assertGreater(state['message_id'], 0)
        message = copy.deepcopy(self.messages[state['message_id']])
        button = next(b for row in message['reply_markup']['inline_keyboard'] for b in row
                      if b.get('callback_data', '').startswith('stop:'))
        self.assertEqual(button['callback_data'], 'stop:' + bridge.delivery.body_preview._key(identity).split(':')[-1])
        return message

    def callback(self, message, data=None):
        self.sequence += 1
        if data is None:
            data = next(b['callback_data'] for row in message['reply_markup']['inline_keyboard'] for b in row
                        if b.get('callback_data', '').startswith('stop:'))
        return {'update_id': self.sequence, 'callback_query': {
            'id': str(self.sequence), 'from': {'id': 123},
            'message': copy.deepcopy(message), 'data': data,
        }}

    async def click(self, message, update=None):
        update = self.callback(message) if update is None else update
        self.router.ingest(update)
        await self.router.status_controls.handle_one()
        return update

    def interruptions(self):
        return [entry for entry in self.native_calls if entry[1] == 'turn/interrupt']

    def results(self, name='a'):
        return self.router.bridges[name].store.db.execute(
            "SELECT * FROM outbox WHERE key LIKE 'stop-result:%' ORDER BY rowid").fetchall()

    def preview_record(self, message, name='a'):
        callback = self.callback(message)['callback_query']['data']
        key = 'completion_preview_v1:' + callback[len('stop:'):]
        store = self.router.bridges[name].store
        return store, key, json.loads(store.get(key))

    def ledger(self, message, name='a'):
        data = self.callback(message)['callback_query']['data']
        return json.loads(self.router.bridges[name].store.get(STOP_REQUEST_KEY + data[len('stop:'):], '{}'))

    async def test_stop_uses_exact_turn_and_original_topic_without_waiting_for_dispatch_lock(self):
        message = await self.bar()
        bridge = self.router.bridges['a']
        async with bridge.dispatch_lock:
            await asyncio.wait_for(self.click(message), timeout=1)
        self.assertEqual(self.interruptions(), [('a', 'turn/interrupt', {
            'threadId': 'native-a', 'turnId': 'a-work',
        })])
        self.assertIn(('a', 'turns', {'limit': 1}), self.native_calls)
        self.assertEqual(self.ledger(message)['state'], 'requested')
        rows = self.results()
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]['chat_id'], rows[0]['message_thread_id']), ('123', 42))
        self.assertIn('停止', rows[0]['text'])
        self.assertTrue(any(method == 'answerCallbackQuery' for method, _ in self.calls))
        self.assertTrue(bridge.delivery.next_row()['key'].startswith('stop-result:'))
        self.assertEqual(self.router.store.db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0], 0)
        self.assertEqual(bridge.store.db.execute('SELECT COUNT(*) FROM inbox').fetchone()[0], 0)

    async def test_two_threads_stop_independently_while_one_native_query_is_waiting(self):
        first, second = await self.bar('a'), await self.bar('b')
        self.natives['a'].query_gate = asyncio.Event()
        self.router.ingest(self.callback(first))
        task_a = asyncio.create_task(self.router.status_controls.handle_one())
        await asyncio.wait_for(self.natives['a'].query_started.wait(), 1)
        try:
            self.router.ingest(self.callback(second))
            await asyncio.wait_for(self.router.status_controls.handle_one(), 1)
            self.assertEqual(self.interruptions(), [('b', 'turn/interrupt', {
                'threadId': 'native-b', 'turnId': 'b-work',
            })])
        finally:
            self.natives['a'].query_gate.set()
            await task_a
        self.assertEqual({(n, p['threadId'], p['turnId']) for n, _, p in self.interruptions()},
                         {('a', 'native-a', 'a-work'), ('b', 'native-b', 'b-work')})
        self.assertTrue(all(r['message_thread_id'] == 84 for r in self.results('b')))

    async def test_control_loop_keeps_other_threads_responsive_during_a_slow_native_read(self):
        first, second = await self.bar('a'), await self.bar('b')
        self.natives['a'].query_gate = asyncio.Event()
        self.router.ingest(self.callback(first))
        worker = asyncio.create_task(self.router.status_controls.loop())
        try:
            await asyncio.wait_for(self.natives['a'].query_started.wait(), 1)
            self.router.ingest(self.callback(second))

            async def wait_for_second():
                while not any(name == 'b' for name, _, _ in self.interruptions()):
                    await asyncio.sleep(0.01)

            await asyncio.wait_for(wait_for_second(), 1)
            self.assertFalse(any(name == 'a' for name, _, _ in self.interruptions()))
        finally:
            self.natives['a'].query_gate.set()
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)

    async def test_duplicate_update_and_fresh_click_only_request_one_interrupt(self):
        message = await self.bar()
        update = await self.click(message)
        await self.click(message, update)
        await self.click(message)
        self.assertEqual(len(self.interruptions()), 1)
        self.assertEqual(self.ledger(message)['state'], 'requested')

    async def test_old_turn_button_never_interrupts_a_new_latest_turn(self):
        message = await self.bar()
        self.natives['a'].latest = 'newer-work'
        await self.click(message)
        self.assertEqual(self.interruptions(), [])
        self.assertNotIn('requested', self.ledger(message).get('state', ''))

    async def test_completion_between_native_read_and_interrupt_still_targets_the_old_id(self):
        message = await self.bar()
        self.natives['a'].after_query = lambda: setattr(self.natives['a'], 'latest', 'newer-work')
        await self.click(message)
        self.assertEqual(self.interruptions(), [('a', 'turn/interrupt', {
            'threadId': 'native-a', 'turnId': 'a-work',
        })])

    async def test_heartbeat_during_native_read_preserves_the_frozen_stop_identity(self):
        message = await self.bar()
        store, key, original = self.preview_record(message)
        frozen = {field: original['desired_identity'][field]
                  for field in ('native_id', 'chat_id', 'topic_id', 'turn_id')}
        refreshed = []

        def heartbeat():
            state = json.loads(store.get(key))
            identity = state['desired_identity']
            identity.update(next_preview=identity.get('next_preview', 0) + 15,
                            notify=0 if identity.get('notify') else 1)
            store.set(key, json.dumps(state))
            refreshed.append({field: identity[field] for field in frozen})

        self.natives['a'].after_query = heartbeat
        await self.click(message)
        self.assertEqual(refreshed, [frozen])
        self.assertEqual(self.interruptions(), [('a', 'turn/interrupt', {
            'threadId': 'native-a', 'turnId': 'a-work',
        })])
        self.assertEqual(self.ledger(message)['state'], 'requested')

    async def test_forged_callback_sender_bot_message_topic_and_hash_cannot_interrupt(self):
        message = await self.bar()
        samples = []
        for field, value in (
            ('sender', 777), ('bot', 555), ('message_id', message['message_id'] + 100),
            ('topic', 84), ('chat_id', 999), ('chat_type', 'group'), ('date', 0),
        ):
            update = self.callback(message)
            callback, origin = update['callback_query'], update['callback_query']['message']
            if field == 'sender':
                callback['from']['id'] = value
            elif field == 'bot':
                origin['from']['id'] = value
            elif field == 'topic':
                origin['message_thread_id'] = value
            elif field == 'chat_id':
                origin['chat']['id'] = value
            elif field == 'chat_type':
                origin['chat']['type'] = value
            else:
                origin[field] = value
            samples.append(update)
        samples.append(self.callback(message, 'stop:' + '0' * 24))
        for update in samples:
            with self.subTest(update=update['update_id']):
                await self.click(message, update)
                self.assertEqual(self.interruptions(), [])
        self.assertEqual(self.native_calls, [])

    async def test_finished_transferred_closed_or_changed_frozen_preview_cannot_interrupt(self):
        message = await self.bar()
        store, key, original = self.preview_record(message)
        states = [dict(original, finished=True), dict(original, closed=True),
                  dict(original, transferred_to='completion_preview_v1:' + 'f' * 24)]
        changed_route = copy.deepcopy(original)
        changed_route['route'][0] = 'another-native'
        states.append(changed_route)
        changed_identity = copy.deepcopy(original)
        changed_identity['desired_identity']['turn_id'] = 'another-turn'
        states.append(changed_identity)
        for index, state in enumerate(states):
            with self.subTest(index=index):
                store.set(key, json.dumps(state))
                await self.click(message)
                self.assertEqual(self.interruptions(), [])
        self.assertEqual(self.native_calls, [])

    async def test_repaired_topic_cannot_reuse_an_old_button_on_old_or_new_topic(self):
        message = await self.bar()
        bridge = self.router.bridges['a']
        updated = {**bridge.target, 'message_thread_id': 99}
        self.router.targets['a'] = bridge.target = updated
        self.router.by_topic.pop(42)
        self.router.by_topic[99] = updated
        await self.click(message)
        moved = copy.deepcopy(message)
        moved['message_thread_id'] = 99
        await self.click(moved)
        self.assertEqual(self.native_calls, [])

    async def test_pending_receipt_hash_and_terminal_group_cannot_interrupt(self):
        message = await self.bar()
        bridge = self.router.bridges['a']
        identity = {'native_id': bridge.thread_id, 'chat_id': bridge.owner, 'topic_id': 42,
                    'source_id': 77, 'turn_id': None}
        key = bridge.delivery.body_preview._key(identity)
        bridge.store.set(key, json.dumps({'route': bridge.delivery.route(), 'desired_identity': identity,
                                         'message_id': message['message_id'], 'delivery': 'sent'}))
        await self.click(message, self.callback(message, 'stop:' + key.split(':')[-1]))
        with bridge.store.db:
            bridge.store.db.execute("UPDATE completion_turns SET status='completed',stage='ready'")
        await self.click(message)
        self.assertEqual(self.native_calls, [])

    async def test_access_revoked_after_ingestion_or_native_read_prevents_interrupt(self):
        message = await self.bar()
        self.router.ingest(self.callback(message))
        self.allow_owner(False)
        await self.router.status_controls.handle_one()
        self.assertEqual(self.native_calls, [])
        self.allow_owner()
        self.natives['a'].after_query = lambda: self.allow_owner(False)
        await self.click(message)
        self.assertEqual(self.interruptions(), [])

    async def test_query_failure_can_be_retried_without_ambiguous_interrupt_ledger(self):
        message = await self.bar()
        self.natives['a'].query_error = ConnectionError('native unavailable before any interrupt')
        await self.click(message)
        self.assertEqual(self.interruptions(), [])
        self.assertNotIn(self.ledger(message).get('state'), ('sending', 'requested', 'uncertain'))
        self.natives['a'].query_error = None
        await self.click(message)
        self.assertEqual(len(self.interruptions()), 1)

    async def test_restart_with_inflight_interrupt_intent_never_replays_the_rpc(self):
        message = await self.bar()
        bridge = self.router.bridges['a']
        data = self.callback(message)['callback_query']['data']
        ledger_key = STOP_REQUEST_KEY + data[len('stop:'):]
        bridge.store.set(ledger_key, json.dumps({
            'state': 'sending', 'route': bridge.delivery.route(), 'turn_id': 'a-work',
            'message_id': message['message_id'],
        }))
        self.router.status_controls = StatusControls(self.router)
        self.router.status_controls.recover()
        await self.click(message)
        self.assertEqual(self.interruptions(), [])
        self.assertEqual(self.ledger(message)['state'], 'uncertain')

    async def test_unknown_interrupt_ack_and_restart_do_not_repeat_the_request(self):
        message = await self.bar()
        self.natives['a'].interrupt_error = ConnectionError('interrupt reply lost')
        await self.click(message)
        self.assertEqual(len(self.interruptions()), 1)
        self.assertEqual(self.ledger(message)['state'], 'uncertain')
        self.router.status_controls = StatusControls(self.router)
        self.router.status_controls.recover()
        await self.router.status_controls.handle_one()
        self.natives['a'].interrupt_error = None
        await self.click(message)
        self.assertEqual(len(self.interruptions()), 1)
        self.assertEqual(self.ledger(message)['state'], 'uncertain')


if __name__ == '__main__':
    unittest.main()
