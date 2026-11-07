import base64
import copy
import json
from pathlib import Path
import tempfile
import time
import unittest

from bridge import RpcError, TelegramError
from completion_delivery import CompletionDelivery
from message_format import send_output
from topics_bridge import TargetBridge


PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aL1sAAAAASUVORK5CYII=')


class CompletionIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name)
        (self.path / 'access.json').write_text(json.dumps({'dmPolicy': 'allowlist', 'allowFrom': ['123']}))
        self.config = {'owner_id': '123', 'thread_id': 'existing-native', 'socket_path': '/unused',
                       'telegram_state_dir': str(self.path), 'notification_mode': 'completion',
                       'activity_indicator': True, 'native_mirror': False}
        self.target = {'key': 'paper', 'name': '论文', 'thread_id': 'existing-native', 'message_thread_id': 42}
        self.bridge = TargetBridge(self.config, self.path, self.target, lambda: '')
        self.now = time.time() + 5
        self.bridge.delivery.clock = lambda: self.now
        self.bridge.mirror.clock = lambda: self.now
        self.bridge.ready.set()
        self.turns, self.items, self.native_calls = [], {}, []
        self.telegram_calls, self.sequence = [], 100
        self.rich_rejection = False
        self.draft_error = None
        self.native_status = {'type': 'active', 'activeFlags': []}
        test = self

        class Native:
            async def turns(self, limit=30):
                test.native_calls.append(('thread/turns/list', limit))
                return copy.deepcopy(test.turns)

            async def items(self, turn_id):
                test.native_calls.append(('thread/items/list', turn_id))
                for entry in test.items.get(turn_id, []):
                    if isinstance(entry, Exception):
                        raise entry
                    yield copy.deepcopy(entry)

            async def call(self, method, params):
                test.native_calls.append((method, params))
                if method != 'thread/read':
                    raise AssertionError('Completion display must never start or alter a model turn: ' + method)
                return {'thread': {'id': 'existing-native', 'status': copy.deepcopy(test.native_status)}}

        class Telegram:
            async def call(self, method, **params):
                test.telegram_calls.append((method, copy.deepcopy(params)))
                if method == 'sendMessageDraft':
                    if test.draft_error:
                        raise test.draft_error
                    return True
                if method == 'sendRichMessage' and test.rich_rejection:
                    raise TelegramError(method, 400, "can't parse rich message")
                test.sequence += 1
                return {'message_id': test.sequence, 'message_thread_id': params.get('message_thread_id')}

            async def upload(self, method, field, path, **params):
                test.telegram_calls.append((method, {**copy.deepcopy(params), 'field': field, 'path': str(path)}))
                test.sequence += 1
                return {'message_id': test.sequence, 'message_thread_id': params.get('message_thread_id')}

        self.bridge.codex = Native()
        self.bridge.telegram = Telegram()

    async def asyncTearDown(self):
        self.bridge.store.db.close()
        self.tmp.cleanup()

    def turn(self, tid='current', status='inProgress', started=None):
        return {'id': tid, 'status': status, 'startedAt': self.now - 1 if started is None else started,
                'completedAt': self.now if status != 'inProgress' else None}

    def entry(self, text, iid='answer', phase='commentary'):
        return {'item': {'id': iid, 'type': 'agentMessage', 'text': text, 'phase': phase},
                'completedAtMs': int(self.now * 1000)}

    def emit(self, entry, tid='current'):
        self.bridge.record_item(tid, entry['item'], entry.get('completedAtMs'))

    async def cycle(self):
        turns = await self.bridge.reconcile()
        await self.bridge.update_native_mirror(turns)
        await self.bridge.update_activity(turns)

    async def begin_telegram(self, tid='current'):
        message = {'message_id': 1, 'from': {'id': 123}, 'chat': {'id': 123, 'type': 'private'},
                   'message_thread_id': 42, 'text': '处理这个任务'}
        self.bridge.store.accept({'update_id': 1}, message)
        self.bridge.store.mark(1, 'running', tid)
        with self.bridge.store.db:
            self.bridge.store.db.execute('UPDATE inbox SET received_ms=? WHERE id=1', (int(self.now * 1000) - 100,))
        self.turns = [self.turn(tid)]
        self.items[tid] = []
        await self.cycle()

    async def drain(self):
        rows = []
        for _ in range(50):
            row = self.bridge.delivery.next_row()
            if row is None:
                return rows
            silent = self.bridge.delivery.silent(row)
            rows.append((row['key'], row['text'], silent, row['media_json']))
            self.bridge.delivery.begin(row)
            self.bridge.store.images.begin(row)
            sent = await send_output(self.bridge.telegram, chat_id=row['chat_id'], text=row['text'],
                                     rich_text=row['rich_text'], topic_id=row['message_thread_id'],
                                     media_json=row['media_json'], disable_notification=silent)
            self.bridge.store.images.sent(row, sent['message_id'])
            self.bridge.delivery.sent(row, sent['message_id'])
        self.fail('Completion delivery did not become idle')

    def drafts(self):
        return [params for method, params in self.telegram_calls if method == 'sendMessageDraft']

    def formal_calls(self):
        return [(method, params) for method, params in self.telegram_calls
                if method in ('sendMessage', 'sendRichMessage', 'sendPhoto', 'sendDocument')]

    async def test_old_working_status_card_is_retired_once_without_new_status_messages(self):
        self.bridge.activity.save({'open': True, 'message_id': 77, 'route': ['123', 42],
                                   'turn_id': 'current', 'delivery': 'sent'})
        await self.begin_telegram()
        self.now += 16
        await self.cycle()
        deletes = [params for method, params in self.telegram_calls if method == 'deleteMessage']
        self.assertEqual(deletes, [{'chat_id': '123', 'message_id': 77}])
        self.assertFalse(self.bridge.activity.load()['open'])
        self.assertGreaterEqual(len(self.drafts()), 2)
        self.assertEqual(self.formal_calls(), [])

    async def test_native_id_renumbering_deduplicates_history_but_keeps_distinct_equal_messages(self):
        await self.begin_telegram()
        entry = self.entry('相同内容', 'live-id')
        self.emit(entry)
        self.items['current'] = [entry]
        await self.cycle()
        self.items['current'] = [self.entry('相同内容', 'persisted-id-1', None),
                                 self.entry('相同内容', 'persisted-id-2', None)]
        self.turns = [self.turn(status='completed')]
        self.bridge.finish_turn(self.turns[0])
        await self.cycle()
        rows = await self.drain()
        self.assertEqual([text for key, text, _, _ in rows if key.startswith('turn:')], ['相同内容', '相同内容'])
        self.assertEqual(sum(not silent for _, _, silent, _ in rows), 1)

    async def test_live_event_waits_for_history_and_successful_scan_is_not_repeated_without_new_items(self):
        await self.begin_telegram()
        item = self.entry('历史稍后才返回的进度', 'late-history')
        self.emit(item)
        await self.cycle()
        self.items['current'] = [item]
        await self.cycle()
        before = sum(method == 'thread/items/list' for method, _ in self.native_calls)
        await self.cycle()
        after = sum(method == 'thread/items/list' for method, _ in self.native_calls)
        self.assertEqual(after, before)
        self.now += 16
        await self.cycle()
        self.assertIn('历史稍后才返回的进度', self.drafts()[-1]['text'])
        self.assertEqual(self.formal_calls(), [])

    async def test_reenabling_does_not_import_an_old_turn_completed_during_all_mode(self):
        await self.begin_telegram()
        self.bridge.target['notification_mode'] = 'all'
        self.bridge.delivery = CompletionDelivery(self.bridge, clock=lambda: self.now)
        self.now += 10
        self.turns = [self.turn(status='completed', started=self.now - 11)]
        self.items['current'] = [self.entry('关闭期间已展示的历史回复', 'old-visible')]
        self.bridge.finish_turn(self.turns[0])
        self.now += 10
        self.bridge.target['notification_mode'] = 'completion'
        self.bridge.delivery = CompletionDelivery(self.bridge, clock=lambda: self.now)
        await self.cycle()
        self.assertIsNone(self.bridge.delivery.next_row())
        self.assertEqual(self.bridge.store.db.execute('SELECT COUNT(*) FROM outbox').fetchone()[0], 0)

    async def test_activation_during_active_task_does_not_recopy_previously_delivered_legacy_text(self):
        await self.begin_telegram()
        old = self.entry('切换前已显示的进度', 'old-id')
        self.bridge.store.enqueue_at('turn:current:old-id', '123', old['item']['text'], 42)
        with self.bridge.store.db:
            self.bridge.store.db.execute('UPDATE outbox SET message_id=777')
        self.now += 10
        self.bridge.target['notification_mode'] = 'all'
        CompletionDelivery(self.bridge, clock=lambda: self.now)
        self.now += 10
        self.bridge.target['notification_mode'] = 'completion'
        self.bridge.delivery = CompletionDelivery(self.bridge, clock=lambda: self.now)
        self.bridge.delivery.watch(self.turn())
        self.items['current'] = [old, self.entry('切换后产生的最终结果', 'new-final', 'final_answer')]
        self.turns = [self.turn(status='completed')]
        self.bridge.finish_turn(self.turns[0])
        await self.cycle()
        rows = await self.drain()
        self.assertNotIn('切换前已显示的进度', ''.join(text for _, text, _, _ in rows))
        self.assertIn('切换后产生的最终结果', ''.join(text for _, text, _, _ in rows))

    async def test_legacy_history_without_timestamps_uses_prior_delivery_occurrences(self):
        await self.begin_telegram()
        self.bridge.store.enqueue_at('turn:current:old-id', '123', '之前已显示', 42)
        with self.bridge.store.db:
            self.bridge.store.db.execute('UPDATE outbox SET message_id=888')
        old = self.entry('之前已显示', 'renumbered-old')
        old.pop('completedAtMs')
        final = self.entry('新增结果没有时间戳', 'new-final', 'final_answer')
        final.pop('completedAtMs')
        self.items['current'] = [old, final]
        self.turns = [self.turn(status='completed')]
        self.bridge.finish_turn(self.turns[0])
        await self.cycle()
        rows = await self.drain()
        self.assertNotIn('之前已显示', ''.join(text for _, text, _, _ in rows))
        self.assertIn('新增结果没有时间戳', ''.join(text for _, text, _, _ in rows))

    async def test_final_answer_phase_is_held_until_real_terminal_history_scan(self):
        await self.begin_telegram()
        early = self.entry('中间进度仍在工作', 'progress')
        final_phase = self.entry('这段已有 final_answer 标记，但任务尚未结束', 'early-final', 'final_answer')
        self.items['current'] = [early, final_phase]
        self.emit(early)
        self.emit(final_phase)
        self.now += 16
        await self.cycle()
        self.assertIsNone(self.bridge.delivery.next_row())
        self.assertEqual(self.formal_calls(), [])
        self.assertIn('中间进度仍在工作', self.drafts()[-1]['text'])
        self.assertIn('任务尚未结束', self.drafts()[-1]['text'])

        late = self.entry('终态历史中才出现的最后一段', 'late-final', 'final_answer')
        self.items['current'].append(late)
        self.turns = [self.turn(status='completed')]
        self.bridge.finish_turn(self.turns[0])
        self.assertIsNone(self.bridge.delivery.next_row(), 'terminal event alone must not release unseen history')
        await self.cycle()
        rows = await self.drain()
        self.assertEqual(sum(not silent for _, _, silent, _ in rows), 1)
        self.assertTrue(all(silent for key, _, silent, _ in rows if key.startswith('turn:')))
        self.assertIn('终态历史中才出现的最后一段', ''.join(text for _, text, _, _ in rows))
        self.assertFalse(rows[-1][2], 'the only normal notification follows all result rows')
        self.assertTrue(all(params.get('message_thread_id') == 42 for _, params in self.formal_calls()))

    async def test_status_and_cumulative_text_share_one_draft_without_new_status_bubbles(self):
        await self.begin_telegram()
        first = self.entry('第一段过程', 'first')
        self.items['current'] = [first]
        self.emit(first)
        self.now += 16
        await self.cycle()
        first_preview = self.drafts()[-1]
        second = self.entry('第二段过程', 'second')
        self.items['current'].append(second)
        self.emit(second)
        self.now += 16
        await self.cycle()
        second_preview = self.drafts()[-1]
        self.assertEqual(first_preview['draft_id'], second_preview['draft_id'])
        self.assertEqual(second_preview['message_thread_id'], 42)
        self.assertIn('第一段过程', second_preview['text'])
        self.assertIn('第二段过程', second_preview['text'])
        self.assertIn('正在工作', second_preview['text'])
        self.assertEqual(self.formal_calls(), [])
        self.assertFalse(any(method in ('editMessageText', 'deleteMessage') for method, _ in self.telegram_calls))

    async def test_failed_terminal_history_scan_does_not_release_partial_results(self):
        await self.begin_telegram()
        first = self.entry('已知第一段', 'first')
        late = self.entry('后来补齐的结果', 'late', 'final_answer')
        self.emit(first)
        self.items['current'] = [first, RpcError('history temporarily unavailable')]
        self.turns = [self.turn(status='completed')]
        self.bridge.finish_turn(self.turns[0])
        with self.assertRaises(RpcError):
            await self.cycle()
        self.assertIsNone(self.bridge.delivery.next_row())
        self.items['current'] = [first, late]
        await self.cycle()
        rows = await self.drain()
        self.assertEqual(sum(not silent for _, _, silent, _ in rows), 1)
        self.assertEqual(sum('已知第一段' in text for _, text, _, _ in rows), 1)
        self.assertIn('后来补齐的结果', ''.join(text for _, text, _, _ in rows))

    async def test_held_turn_does_not_block_an_explicit_system_command_reply(self):
        await self.begin_telegram()
        progress = self.entry('尚未完成的任务', 'progress')
        self.items['current'] = [progress]
        self.emit(progress)
        await self.cycle()
        self.bridge.store.enqueue_at('input:99:reply', '123', '当前模型：example', 42)
        rows = await self.drain()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][1], '当前模型：example')
        self.assertIsNone(self.bridge.delivery.next_row())
        self.assertEqual(self.bridge.store.db.execute("SELECT message_id FROM outbox WHERE key LIKE 'turn:%'").fetchone()[0], None)

    async def test_unsupported_draft_keeps_outputs_buffered_without_formal_progress_fallback(self):
        self.draft_error = TelegramError('sendMessageDraft', 404, 'method unavailable')
        await self.begin_telegram()
        progress = self.entry('草稿不可用时仍保留的完整过程', 'progress')
        self.items['current'] = [progress]
        self.emit(progress)
        self.now += 16
        await self.cycle()
        self.assertEqual(self.formal_calls(), [])
        self.assertIsNone(self.bridge.delivery.next_row())
        self.turns = [self.turn(status='completed')]
        await self.cycle()
        rows = await self.drain()
        self.assertIn('草稿不可用时仍保留的完整过程', ''.join(text for _, text, _, _ in rows))
        self.assertEqual(sum(not silent for _, _, silent, _ in rows), 1)

    async def test_waiting_and_unknown_status_never_release_or_announce_completion(self):
        await self.begin_telegram()
        progress = self.entry('等待你在客户端批准', 'progress')
        self.items['current'] = [progress]
        self.emit(progress)
        self.native_status = {'type': 'active', 'activeFlags': ['waitingOnApproval']}
        self.now += 16
        await self.cycle()
        self.assertIn('需要你处理', self.drafts()[-1]['text'])
        self.assertIsNone(self.bridge.delivery.next_row())
        self.turns = [self.turn(status='unknown')]
        await self.cycle()
        self.assertIsNone(self.bridge.delivery.next_row())
        self.assertEqual(self.formal_calls(), [])
        self.turns = [self.turn(status='completed')]
        await self.cycle()
        self.assertEqual(sum(not silent for _, _, silent, _ in await self.drain()), 1)

    async def test_completion_mode_uses_draft_even_with_legacy_activity_indicator_disabled(self):
        self.bridge.target['activity_indicator'] = False
        await self.begin_telegram()
        progress = self.entry('正文预览不能依赖旧状态卡开关', 'progress')
        self.items['current'] = [progress]
        self.emit(progress)
        self.now += 16
        await self.cycle()
        self.assertIn('正文预览不能依赖旧状态卡开关', self.drafts()[-1]['text'])
        self.assertEqual(self.formal_calls(), [])
        self.assertFalse(any(method == 'editMessageText' for method, _ in self.telegram_calls))

    async def test_images_and_plain_format_fallback_remain_quiet_before_completion_notice(self):
        await self.begin_telegram()
        image = self.path / '结果.png'
        image.write_bytes(PNG)
        item = self.entry('**完整结果**\n\n![图片](<' + str(image) + '>)', 'image-result', 'final_answer')
        self.emit(item)
        self.items['current'] = [item]
        self.assertIsNone(self.bridge.delivery.next_row())
        self.turns = [self.turn(status='completed')]
        self.rich_rejection = True
        await self.cycle()
        rows = await self.drain()
        self.assertTrue(any(media for _, _, _, media in rows))
        calls = self.formal_calls()
        self.assertTrue(any(method == 'sendPhoto' for method, _ in calls))
        self.assertTrue(any(method == 'sendRichMessage' for method, _ in calls))
        self.assertTrue(any(method == 'sendMessage' and params.get('disable_notification') for method, params in calls))
        self.assertTrue(all(params.get('disable_notification') for method, params in calls[:-1]))
        self.assertFalse(calls[-1][1].get('disable_notification', False))
        self.assertEqual(sum(not silent for _, _, silent, _ in rows), 1)

    async def test_restart_after_notification_delivery_never_replays_results_or_completion(self):
        await self.begin_telegram()
        item = self.entry('结果只发送一次', 'result', 'final_answer')
        self.emit(item)
        self.items['current'] = [item]
        self.turns = [self.turn(status='completed')]
        await self.cycle()
        rows = await self.drain()
        self.assertEqual(sum(not silent for _, _, silent, _ in rows), 1)
        previous = self.bridge
        self.bridge = TargetBridge(copy.deepcopy(self.config), self.path, copy.deepcopy(self.target), lambda: '')
        self.bridge.codex, self.bridge.telegram = previous.codex, previous.telegram
        self.bridge.ready.set()
        self.bridge.delivery.clock = lambda: self.now
        previous.store.db.close()
        self.bridge.delivery.recover()
        await self.cycle()
        self.assertEqual(await self.drain(), [])

    async def test_desktop_mirror_uses_the_same_hold_and_completion_policy_without_old_alerts(self):
        self.bridge.target['native_mirror'] = True
        self.turns = [self.turn('old', 'completed', started=self.now - 100)]
        await self.cycle()
        self.assertIsNone(self.bridge.delivery.next_row())
        self.now += 2
        self.turns = [self.turn('desktop')]
        user = {'item': {'id': 'desktop-user', 'type': 'userMessage', 'clientId': None,
                         'content': [{'type': 'text', 'text': '电脑端开始的任务'}]},
                'completedAtMs': int(self.now * 1000)}
        progress = self.entry('电脑端的过程消息', 'desktop-progress')
        self.items['desktop'] = [user]
        await self.cycle()
        self.emit(progress, 'desktop')
        self.items['desktop'].append(progress)
        self.now += 16
        await self.cycle()
        self.assertIsNone(self.bridge.delivery.next_row())
        self.assertIn('电脑端的过程消息', self.drafts()[-1]['text'])
        self.assertEqual(self.formal_calls(), [])
        final = self.entry('电脑端最终结果', 'desktop-result', 'final_answer')
        self.items['desktop'].append(final)
        self.turns = [self.turn('desktop', 'completed')]
        await self.cycle()
        rows = await self.drain()
        self.assertEqual(sum(not silent for _, _, silent, _ in rows), 1)
        self.assertIn('电脑端最终结果', ''.join(text for _, text, _, _ in rows))
        self.assertEqual(self.bridge.store.db.execute('SELECT COUNT(*) FROM inbox').fetchone()[0], 0)
        self.assertFalse(any(method in ('turn/start', 'turn/interrupt', 'thread/settings/update')
                             for method, _ in self.native_calls))
