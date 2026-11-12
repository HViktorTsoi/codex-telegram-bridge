"""A live turn must keep its preview when persisted history briefly says stopped."""
import copy
import json
from pathlib import Path
import tempfile
import time
import unittest

from message_format import send_output
from topics_bridge import TargetBridge


class TurnPreviewLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name)
        (self.path / 'access.json').write_text(json.dumps({'dmPolicy': 'allowlist', 'allowFrom': ['123']}))
        config = {'owner_id': '123', 'thread_id': 'native', 'socket_path': '/unused',
                  'telegram_state_dir': str(self.path), 'notification_mode': 'completion',
                  'preview_style': 'status-bar', 'preview_body_style': 'footer', 'native_mirror': True}
        target = {'key': 'expenses', 'name': '报销管理', 'thread_id': 'native', 'message_thread_id': 42}
        self.bridge = TargetBridge(config, self.path, target, lambda: '')
        self.now = time.time() + 5
        self.bridge.delivery.clock = self.bridge.mirror.clock = lambda: self.now
        self.bridge.ready.set()
        self.history, self.entries, self.native_calls, self.sent = [], [], [], []
        self.sequence = 100
        test = self

        async def call(method, params):
            test.native_calls.append((method, copy.deepcopy(params)))
            if method == 'thread/turns/list':
                return {'data': copy.deepcopy(test.history)}
            if method == 'thread/items/list':
                return {'data': copy.deepcopy(test.entries)}
            if method == 'thread/read':
                return {'thread': {'id': 'native', 'status': {'type': 'active', 'activeFlags': []}}}
            raise AssertionError('Preview lifecycle must not dispatch a model task: ' + method)

        # Keep the real Codex.turns(), its live-state merge and item pagination.
        self.bridge.codex.call = call

        class Telegram:
            async def call(self, method, **params):
                test.sent.append((method, copy.deepcopy(params)))
                test.sequence += 1
                return {'message_id': params.get('message_id', test.sequence),
                        'message_thread_id': params.get('message_thread_id', 42)}

        self.bridge.telegram = Telegram()
        message = {'message_id': 1, 'from': {'id': 123}, 'chat': {'id': 123, 'type': 'private'},
                   'message_thread_id': 42, 'text': '核对报销材料'}
        self.bridge.store.accept({'update_id': 1}, message)
        self.bridge.store.mark(1, 'running', 'current')
        self.live = {'id': 'current', 'status': 'inProgress', 'startedAt': self.now - 1}
        self.bridge.codex.turn_state.started(self.live)
        self.history = [dict(self.live, status='interrupted', completedAt=None)]

    async def asyncTearDown(self):
        self.bridge.store.db.close()
        self.tmp.cleanup()

    async def cycle(self):
        turns = await self.bridge.reconcile()
        await self.bridge.update_native_mirror(turns)
        await self.bridge.update_activity(turns)

    def item(self, text, phase='commentary'):
        entry = {'item': {'id': str(len(self.entries)), 'type': 'agentMessage',
                          'text': text, 'phase': phase}, 'completedAtMs': int(self.now * 1000)}
        self.entries.append(entry)
        self.bridge.record_item('current', entry['item'], entry['completedAtMs'])

    def group(self):
        return self.bridge.delivery._group('current')

    async def drain(self):
        delivered = []
        for _ in range(20):
            row = self.bridge.delivery.next_row()
            if row is None:
                return delivered
            silent = self.bridge.delivery.silent(row)
            delivered.append((row['text'], silent))
            self.bridge.delivery.begin(row)
            self.bridge.store.images.begin(row)
            result = await send_output(self.bridge.telegram, chat_id=row['chat_id'],
                text=row['text'], rich_text=row['rich_text'], topic_id=row['message_thread_id'],
                media_json=row['media_json'], disable_notification=silent)
            self.bridge.store.images.sent(row, result['message_id'])
            self.bridge.delivery.sent(row, result['message_id'])
            await self.bridge.delivery.complete_status(self.bridge.telegram, row)
        self.fail('Completion queue did not drain')

    async def test_unflushed_interrupted_snapshot_keeps_footer_buttons_and_one_final_notification(self):
        await self.cycle()
        self.item('第一段过程')
        self.now += 16
        await self.cycle()
        self.item('第二段过程')
        self.now += 16
        await self.cycle()
        self.assertEqual((self.group()['stage'], self.group()['status']), ('active', 'inProgress'))
        self.assertEqual(self.bridge.store.counts()['running_inputs'], 1)
        self.assertEqual(await self.drain(), [])
        preview = self.bridge.delivery.body_preview.load(dict(self.group()))
        self.assertFalse(preview.get('finished'))
        buttons = [button for row in preview['sent_markup']['inline_keyboard'] for button in row]
        self.assertTrue(any(button.get('callback_data', '').startswith('stop:') for button in buttons))
        rich = [params['rich_message']['html'] for _, params in self.sent
                if 'html' in params.get('rich_message', {})]
        self.assertIn('第一段过程', rich[-1])
        self.assertIn('第二段过程', rich[-1])
        self.assertIn('<footer>', rich[-1])
        self.assertFalse(any(method == 'deleteMessage' for method, _ in self.sent))

        self.item('这是最终答复', 'final_answer')
        ended = dict(self.live, status='completed', completedAt=self.now)
        self.bridge.codex.turn_state.finished(ended)
        self.bridge.finish_turn(ended)
        self.history = [ended]
        await self.cycle()
        result = await self.drain()
        self.assertEqual(len(result), 2)
        self.assertIn('第一段过程', result[0][0])
        self.assertIn('第二段过程', result[0][0])
        self.assertEqual(result[1], ('这是最终答复', False))
        self.assertEqual(sum(not silent for _, silent in result), 1)
        self.assertEqual(self.bridge.store.counts()['running_inputs'], 0)
        await self.cycle()
        self.assertEqual(await self.drain(), [])

    async def test_real_stop_event_beats_stale_active_history_and_retires_stop_button(self):
        self.history = [self.live]
        self.item('停止前的过程')
        await self.cycle()
        ended = dict(self.live, status='interrupted')
        self.bridge.codex.turn_state.finished(ended)
        self.bridge.finish_turn(ended)
        self.now += 16
        await self.cycle()
        self.assertEqual((self.group()['stage'], self.group()['status']), ('ready', 'interrupted'))
        self.assertEqual(self.bridge.store.counts()['running_inputs'], 0)
        preview = self.bridge.delivery.body_preview.load(dict(self.group()))
        buttons = [button for row in preview['sent_markup']['inline_keyboard'] for button in row]
        self.assertFalse(any(button.get('callback_data', '').startswith('stop:') for button in buttons))
        result = await self.drain()
        self.assertEqual(sum(not silent for _, silent in result), 1)
        self.assertIn('停止前的过程', ''.join(text for text, _ in result))
        self.assertIn('本轮任务已停止', result[-1][0])
