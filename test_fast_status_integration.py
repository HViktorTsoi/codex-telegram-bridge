import asyncio
import copy
import json
import unittest
from unittest.mock import AsyncMock, patch

from completion_delivery import CompletionDelivery
import test_completion_integration as fixtures
import test_session_picker as picker_fixtures
from topics_bridge import TopicRouter


class FastStatusIntegrationTests(unittest.IsolatedAsyncioTestCase):
    turn = fixtures.CompletionIntegrationTests.turn
    entry = fixtures.CompletionIntegrationTests.entry
    emit = fixtures.CompletionIntegrationTests.emit
    cycle = fixtures.CompletionIntegrationTests.cycle
    begin_telegram = fixtures.CompletionIntegrationTests.begin_telegram

    async def asyncSetUp(self):
        await fixtures.CompletionIntegrationTests.asyncSetUp(self)
        self.bridge.config['preview_style'] = 'block'
        self.bridge.delivery = CompletionDelivery(self.bridge, clock=lambda: self.now)
        self.workers = []
        self.preview_seen = asyncio.Event()
        self.preview_started = asyncio.Event()
        self.preview_release = None
        tests = self

        class Telegram:
            async def call(self, method, **params):
                tests.telegram_calls.append((method, copy.deepcopy(params)))
                if method in ('sendMessageDraft', 'sendRichMessageDraft'):
                    tests.preview_started.set()
                    if tests.preview_release is not None:
                        await tests.preview_release.wait()
                    tests.preview_seen.set()
                    return True
                tests.sequence += 1
                return {'message_id': tests.sequence}

        self.bridge.telegram = Telegram()

    async def asyncTearDown(self):
        for task in self.workers:
            task.cancel()
        results = await asyncio.gather(*self.workers, return_exceptions=True)
        try:
            for result in results:
                if isinstance(result, BaseException) and not isinstance(result, asyncio.CancelledError):
                    raise result
        finally:
            await fixtures.CompletionIntegrationTests.asyncTearDown(self)

    def accept(self, update_id=1, text='执行这个任务', **extra):
        message = {'message_id': update_id, 'from': {'id': 123},
                   'chat': {'id': 123, 'type': 'private'}, 'message_thread_id': 42,
                   'text': text, **extra}
        self.bridge.store.accept({'update_id': update_id}, message)
        # The borrowed fixture uses an injected clock. Keep later accepted
        # input later than begin_telegram's deliberately seeded receipt time.
        with self.bridge.store.db:
            self.bridge.store.db.execute('UPDATE inbox SET received_ms=? WHERE id=?',
                                         (int(self.now * 1000) + update_id, update_id))
        return message

    def start_preview(self):
        self.bridge.receipt_event.set()
        task = asyncio.create_task(self.bridge.preview_loop())
        self.workers.append(task)
        return task

    async def wait_for(self, condition, timeout=2):
        async def waiting():
            while not condition():
                await asyncio.sleep(0.005)
        await asyncio.wait_for(waiting(), timeout)

    def previews(self):
        return [(method, params) for method, params in self.telegram_calls
                if method in ('sendMessageDraft', 'sendRichMessageDraft')]

    def last_preview(self):
        method, params = self.previews()[-1]
        if method == 'sendRichMessageDraft':
            return params['rich_message']['blocks'][0]['text']
        return params['text']

    def assert_no_formal_task_messages(self):
        self.assertFalse(any(method in ('sendMessage', 'sendRichMessage', 'sendPhoto', 'sendDocument')
                             for method, _ in self.telegram_calls))

    async def dispatch(self, row):
        async with self.bridge.dispatch_lock:
            await self.bridge.dispatch_one(row)

    async def test_receipt_appears_while_native_is_not_ready_and_dispatch_lock_is_busy(self):
        self.bridge.ready.clear()
        await self.bridge.dispatch_lock.acquire()
        try:
            self.start_preview()
            self.accept()
            await asyncio.wait_for(self.preview_seen.wait(), 2)
            self.assertIn('已收到', self.last_preview())
            self.assertIn('连接', self.last_preview())
            self.assertEqual(self.native_calls, [])
            self.assertEqual(self.bridge.store.db.execute('SELECT COUNT(*) FROM completion_turns').fetchone()[0], 0)
            self.assert_no_formal_task_messages()
        finally:
            self.bridge.dispatch_lock.release()

    async def test_slow_attachment_preparation_does_not_delay_first_receipt(self):
        preparing, release = asyncio.Event(), asyncio.Event()

        async def prepare(*args):
            preparing.set()
            await release.wait()
            return [{'type': 'text', 'text': '附件已经准备好'}]

        self.bridge.codex.call = AsyncMock(return_value={'turn': {'id': 'actual-turn'}})
        self.start_preview()
        self.accept(text='', document={'file_id': 'owned-fixture'}, caption='处理这份附件')
        with patch('bridge.inputs_for', new=AsyncMock(side_effect=prepare)):
            task = asyncio.create_task(self.dispatch(self.bridge.store.pending()))
            self.workers.append(task)
            await asyncio.wait_for(preparing.wait(), 2)
            await asyncio.wait_for(self.preview_seen.wait(), 2)
            self.assertTrue(self.bridge.dispatch_lock.locked())
            self.assertFalse(task.done())
            self.bridge.codex.call.assert_not_awaited()
            self.assertIn('已收到', self.last_preview())
            self.assertEqual(self.bridge.store.db.execute('SELECT COUNT(*) FROM completion_turns').fetchone()[0], 0)
            self.assert_no_formal_task_messages()
            release.set()
            await asyncio.wait_for(task, 2)
        self.assertEqual(self.bridge.store.db.execute('SELECT turn_id FROM inbox').fetchone()[0], 'actual-turn')
        self.assertEqual(self.bridge.codex.call.await_count, 1)

    async def test_slow_turn_start_does_not_delay_first_receipt_or_create_a_fake_turn(self):
        starting, release = asyncio.Event(), asyncio.Event()

        async def native(method, params):
            self.assertEqual(method, 'turn/start')
            self.assertEqual(params['threadId'], 'existing-native')
            starting.set()
            await release.wait()
            return {'turn': {'id': 'real-native-turn'}}

        self.bridge.codex.call = AsyncMock(side_effect=native)
        self.start_preview()
        self.accept()
        with patch('bridge.inputs_for', new=AsyncMock(return_value=[{'type': 'text', 'text': '执行这个任务'}])):
            task = asyncio.create_task(self.dispatch(self.bridge.store.pending()))
            self.workers.append(task)
            await asyncio.wait_for(starting.wait(), 2)
            await asyncio.wait_for(self.preview_seen.wait(), 2)
            self.assertFalse(task.done())
            self.assertEqual(self.bridge.store.db.execute('SELECT state FROM inbox').fetchone()[0], 'dispatching')
            self.assertEqual(self.bridge.store.db.execute('SELECT COUNT(*) FROM completion_turns').fetchone()[0], 0)
            receipt_id = self.previews()[0][1]['draft_id']
            self.assertIn('已收到', self.last_preview())
            self.assert_no_formal_task_messages()
            release.set()
            await asyncio.wait_for(task, 2)
        groups = self.bridge.store.db.execute('SELECT turn_id,draft_id FROM completion_turns').fetchall()
        self.assertEqual([row['turn_id'] for row in groups], ['real-native-turn'])
        self.assertEqual(groups[0]['draft_id'], receipt_id)

    async def test_slow_telegram_preview_cannot_block_the_native_turn_start(self):
        self.preview_release = asyncio.Event()
        self.bridge.codex.call = AsyncMock(return_value={'turn': {'id': 'real-native-turn'}})
        self.start_preview()
        self.accept()
        await asyncio.wait_for(self.preview_started.wait(), 2)
        with patch('bridge.inputs_for', new=AsyncMock(return_value=[{'type': 'text', 'text': '执行这个任务'}])):
            await asyncio.wait_for(self.dispatch(self.bridge.store.pending()), 0.5)
        self.assertEqual(self.bridge.store.db.execute('SELECT state FROM inbox').fetchone()[0], 'running')
        self.assertFalse(self.preview_seen.is_set())
        self.bridge.codex.call.assert_awaited_once()
        self.preview_release.set()
        await asyncio.wait_for(self.preview_seen.wait(), 2)

    async def test_duplicate_accept_does_not_wake_or_repeat_an_initial_preview(self):
        self.start_preview()
        self.accept()
        await asyncio.wait_for(self.preview_seen.wait(), 2)
        await asyncio.sleep(0.05)
        self.bridge.receipt_event.clear()
        count = len(self.previews())
        self.accept()
        self.assertFalse(self.bridge.receipt_event.is_set())
        await asyncio.sleep(0.15)
        self.assertEqual(len(self.previews()), count)
        self.assertEqual(self.bridge.store.db.execute('SELECT COUNT(*) FROM inbox').fetchone()[0], 1)

    async def test_commands_and_empty_inputs_do_not_create_task_receipts(self):
        self.start_preview()
        for update_id, text in enumerate(('/status', '/stop', '/not-a-command', ''), 1):
            self.accept(update_id, text)
        await asyncio.sleep(0.2)
        self.assertEqual(self.previews(), [])
        self.assertEqual(self.native_calls, [])
        self.assertEqual(self.bridge.store.db.execute('SELECT COUNT(*) FROM completion_turns').fetchone()[0], 0)

    async def test_command_like_media_caption_is_a_task_and_gets_an_early_receipt(self):
        self.start_preview()
        self.accept(text='', caption='/stop 只是附件说明', photo=[{'file_id': 'fixture'}])
        await asyncio.wait_for(self.preview_seen.wait(), 2)
        self.assertIn('已收到', self.last_preview())
        self.assertEqual(self.native_calls, [])
        self.assert_no_formal_task_messages()

    async def test_new_steering_message_preserves_the_active_tasks_cumulative_body(self):
        await self.begin_telegram()
        item = self.entry('已有任务的累计进度，不能被新消息清空。', 'progress')
        self.items['current'] = [item]
        self.emit(item)
        await self.cycle()
        old_id = self.previews()[-1][1]['draft_id']
        self.start_preview()
        self.accept(2, '这是补充要求')
        await self.wait_for(lambda: any('已收到' in (params.get('text') or params.get('rich_message', {})
                                       .get('blocks', [{}])[0].get('text', '')) for _, params in self.previews()))
        self.assertIn('已有任务的累计进度，不能被新消息清空。', self.last_preview())
        self.assertEqual(self.previews()[-1][1]['draft_id'], old_id)
        self.assertEqual([row[0] for row in self.bridge.store.db.execute('SELECT turn_id FROM completion_turns')], ['current'])
        self.assert_no_formal_task_messages()

    async def test_completion_notice_waits_for_an_inflight_receipt_preview(self):
        self.preview_release = asyncio.Event()
        self.start_preview()
        self.accept()
        await asyncio.wait_for(self.preview_started.wait(), 2)
        self.bridge.store.mark(1, 'running', 'fast-real-turn')
        self.bridge.delivery.watch(self.turn('fast-real-turn'))
        completed = self.turn('fast-real-turn', 'completed')
        self.bridge.finish_turn(completed)
        self.bridge.delivery.seal(completed)
        sender = asyncio.create_task(self.bridge.sender_loop())
        self.workers.append(sender)
        await asyncio.sleep(0.1)
        self.assert_no_formal_task_messages()
        self.assertFalse(self.preview_seen.is_set())
        self.preview_release.set()
        await self.wait_for(lambda: any(method in ('sendMessage', 'sendRichMessage')
                                        for method, _ in self.telegram_calls))
        methods = [method for method, _ in self.telegram_calls]
        self.assertLess(methods.index('sendRichMessageDraft'), methods.index('sendMessage'))
        self.assertEqual(sum(method in ('sendMessage', 'sendRichMessage') for method in methods), 1)
        self.assertEqual(self.bridge.store.db.execute('SELECT notice_state FROM completion_turns').fetchone()[0], 'sent')

    async def test_input_failure_reply_waits_for_an_inflight_receipt_preview(self):
        self.preview_release = asyncio.Event()
        self.start_preview()
        self.accept()
        await asyncio.wait_for(self.preview_started.wait(), 2)
        self.bridge.store.mark(1, 'failed')
        self.bridge.store.enqueue('input:1:attachment_error', '123', '附件未准备成功，请重新发送。')
        sender = asyncio.create_task(self.bridge.sender_loop())
        self.workers.append(sender)
        await asyncio.sleep(0.1)
        self.assert_no_formal_task_messages()
        self.preview_release.set()
        await self.wait_for(lambda: any(method == 'sendMessage' for method, _ in self.telegram_calls))
        formal = [(method, params) for method, params in self.telegram_calls
                  if method in ('sendMessage', 'sendRichMessage')]
        self.assertEqual(len(formal), 1)
        self.assertIn('附件未准备成功', formal[0][1]['text'])
        self.assertEqual(self.bridge.store.db.execute('SELECT COUNT(*) FROM completion_turns').fetchone()[0], 0)

    async def test_old_ready_turn_cannot_replace_a_new_pending_inputs_receipt(self):
        await self.begin_telegram()
        completed = self.turn(status='completed')
        self.bridge.finish_turn(completed)
        self.bridge.delivery.seal(completed)
        self.preview_seen.clear()
        self.start_preview()
        self.accept(2, '新任务仍在准备中')
        await asyncio.wait_for(self.preview_seen.wait(), 2)
        receipt_id = self.previews()[-1][1]['draft_id']
        self.assertIn('已收到', self.last_preview())
        self.now += 16
        await self.bridge.update_activity([completed])
        self.assertIn('已收到', self.last_preview())
        self.assertNotIn('已完成', self.last_preview())
        self.assertEqual(self.previews()[-1][1]['draft_id'], receipt_id)
        self.assertEqual(self.bridge.store.db.execute('SELECT state FROM inbox WHERE id=2').fetchone()[0], 'pending')
        self.assert_no_formal_task_messages()


class ConfirmedMainReceiptTests(unittest.IsolatedAsyncioTestCase):
    callback = picker_fixtures.PickerTests.callback

    async def asyncSetUp(self):
        await picker_fixtures.PickerTests.asyncSetUp(self)
        for bridge in self.router.bridges.values():
            bridge.store.db.close()
        self.router.store.db.close()
        self.config.update(notification_mode='completion', preview_style='block')
        (self.path / 'config.json').write_text(json.dumps(self.config))
        self.router = TopicRouter(copy.deepcopy(self.config), self.path)
        self.router.bot_id, self.router.telegram = 444, self.telegram
        self.draft_calls, self.workers = [], []
        original = self.telegram.call

        async def call(method, **params):
            if method in ('sendMessageDraft', 'sendRichMessageDraft'):
                self.draft_calls.append((method, copy.deepcopy(params)))
                return True
            return await original(method, **params)

        self.telegram.call = call
        for bridge in self.router.bridges.values():
            bridge.telegram = self.telegram
            self.workers.append(asyncio.create_task(bridge.preview_loop()))

    async def asyncTearDown(self):
        for task in self.workers:
            task.cancel()
        results = await asyncio.gather(*self.workers, return_exceptions=True)
        try:
            for result in results:
                if isinstance(result, BaseException) and not isinstance(result, asyncio.CancelledError):
                    raise result
        finally:
            await picker_fixtures.PickerTests.asyncTearDown(self)

    async def test_main_message_receipt_waits_for_target_confirmation_then_appears_in_that_topic(self):
        self.sequence += 1
        self.router.ingest({'update_id': self.sequence, 'message': {
            'message_id': self.sequence, 'from': {'id': 123}, 'chat': {'id': 123, 'type': 'private'},
            'text': 'All 消息确认后才显示接收状态'}})
        await self.router.picker.handle_one()
        panel = self.telegram.messages[self.telegram.next_id]
        await asyncio.sleep(0.15)
        self.assertEqual(self.draft_calls, [])
        self.router.ingest(self.callback(panel, 'Instagram'))
        await self.router.picker.handle_one()
        review = self.telegram.messages[panel['message_id']]
        await asyncio.sleep(0.15)
        self.assertEqual(self.draft_calls, [])
        self.router.ingest(self.callback(review, '确认发送'))
        await self.router.picker.handle_one()

        async def waiting():
            while not self.draft_calls:
                await asyncio.sleep(0.005)
        await asyncio.wait_for(waiting(), 2)
        self.assertTrue(all(params['message_thread_id'] == 22 for _, params in self.draft_calls))
        self.assertEqual(self.router.bridges['instagram'].store.db.execute('SELECT COUNT(*) FROM inbox').fetchone()[0], 1)
        self.assertTrue(all(bridge.store.db.execute('SELECT COUNT(*) FROM completion_turns').fetchone()[0] == 0
                            for bridge in self.router.bridges.values()))
