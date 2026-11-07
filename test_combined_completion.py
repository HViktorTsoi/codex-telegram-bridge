"""Completion output is packed before delivery, without losing source history."""
import unittest

from bridge import TelegramError
from completion_delivery import CompletionDelivery
from message_format import utf16_size
import test_completion_integration as harness

PNG = harness.PNG
UNCONFIRMED_TEXT = '⚠️ 部分正文发送结果未获确认，未自动重发；完整记录保留在 Codex，可要求重发。'
SUBMESSAGE_SEPARATOR = '\n\n────────────\n\n'


class CombinedCompletionTests(unittest.IsolatedAsyncioTestCase):
    # Reuse the real bridge harness without inheriting its legacy behavior tests.
    asyncSetUp = harness.CompletionIntegrationTests.asyncSetUp
    asyncTearDown = harness.CompletionIntegrationTests.asyncTearDown
    turn = harness.CompletionIntegrationTests.turn
    entry = harness.CompletionIntegrationTests.entry
    emit = harness.CompletionIntegrationTests.emit
    cycle = harness.CompletionIntegrationTests.cycle
    begin_telegram = harness.CompletionIntegrationTests.begin_telegram
    drain = harness.CompletionIntegrationTests.drain
    formal_calls = harness.CompletionIntegrationTests.formal_calls

    async def complete(self, texts, status='completed'):
        self.items['current'] = [self.entry(text, f'item-{i}', 'final_answer' if i == len(texts) - 1 else 'commentary')
                                 for i, text in enumerate(texts)]
        self.turns = [self.turn(status=status)]
        self.bridge.finish_turn(self.turns[0])
        await self.cycle()

    def restart_delivery(self):
        self.bridge.delivery = CompletionDelivery(self.bridge, clock=lambda: self.now)
        self.bridge.delivery.recover()

    async def send_row(self, row):
        await self.bridge.send_one(row)

    async def test_twelve_public_items_become_one_message_with_completion_in_body(self):
        await self.begin_telegram()
        texts = [f'第 {i} 项工作内容' for i in range(12)]
        await self.complete(texts)
        rows = await self.drain()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][1], SUBMESSAGE_SEPARATOR.join(texts) + '\n\n✅ 本轮任务已完成。')
        self.assertFalse(rows[0][2])
        self.assertEqual(len(self.formal_calls()), 1)
        self.assertEqual(self.formal_calls()[0][1]['message_thread_id'], 42)
        # A packed delivery must retain all canonical source rows for recovery.
        originals = self.bridge.store.db.execute("SELECT text FROM outbox WHERE key LIKE 'turn:%' ORDER BY rowid").fetchall()
        self.assertEqual([r[0] for r in originals], texts)
        self.restart_delivery()
        self.assertIsNone(self.bridge.delivery.next_row())

    async def test_long_unicode_source_is_split_few_times_without_text_loss(self):
        await self.begin_telegram()
        text = ('保留完整内容🙂' * 1250) + '最后一个字'
        await self.complete([text])
        rows = await self.drain()
        self.assertLessEqual(len(rows), 5)
        self.assertTrue(all(utf16_size(row[1]) <= 3800 for row in rows))
        self.assertEqual(''.join(row[1] for row in rows), text + '\n\n✅ 本轮任务已完成。')
        self.assertEqual(sum(not row[2] for row in rows), 1)
        self.assertFalse(rows[-1][2])
        self.assertTrue(all(row[2] for row in rows[:-1]))

    async def test_rich_code_fences_are_rebalanced_after_source_chunks_are_joined(self):
        await self.begin_telegram()
        text = '```python\n' + ('print("完整代码")\n' * 430) + '```\n代码之后'
        await self.complete([text])
        rows = await self.drain()
        self.assertEqual(''.join(row[1] for row in rows), text + '\n\n✅ 本轮任务已完成。')
        rich = [params['rich_message']['markdown'] for method, params in self.formal_calls()
                if method == 'sendRichMessage']
        self.assertEqual(len(rich), len(rows))
        self.assertTrue(all(part.count('```') % 2 == 0 for part in rich))
        self.assertEqual(sum(not row[2] for row in rows), 1)

    async def test_picture_is_sent_separately_before_the_single_notifying_text(self):
        await self.begin_telegram()
        image = self.path / 'result.png'
        image.write_bytes(PNG)
        await self.complete(['处理过程', f'结果已生成。\n\n![结果图片]({image})'])
        rows = await self.drain()
        self.assertEqual(len(rows), 2)
        self.assertTrue(rows[0][3])
        self.assertTrue(rows[0][2])
        self.assertFalse(rows[1][3])
        self.assertFalse(rows[1][2])
        self.assertIn('处理过程', rows[1][1])
        self.assertIn('结果已生成', rows[1][1])
        self.assertTrue(rows[1][1].endswith('✅ 本轮任务已完成。'))
        calls = self.formal_calls()
        self.assertEqual(calls[0][0], 'sendPhoto')
        self.assertTrue(calls[0][1]['disable_notification'])
        self.assertFalse(calls[-1][1]['disable_notification'])
        self.assertTrue(all(params['message_thread_id'] == 42 for _, params in calls))

    async def test_terminal_event_holds_all_content_until_full_scan_and_controls_bypass(self):
        await self.begin_telegram()
        for i in range(8):
            progress = self.entry(f'未完成过程 {i}', str(i))
            self.items['current'].append(progress)
            self.emit(progress)
        await self.cycle()
        self.bridge.store.enqueue_at('input:77:reply', '123', '当前模型设置', 42)
        rows = await self.drain()
        self.assertEqual([r[1] for r in rows], ['当前模型设置'])
        terminal = self.turn(status='completed')
        self.bridge.finish_turn(terminal)
        self.assertIsNone(self.bridge.delivery.next_row())
        self.turns = [terminal]
        await self.cycle()
        packed = await self.drain()
        self.assertEqual(len(packed), 1)
        self.assertTrue(all(f'未完成过程 {i}' in packed[0][1] for i in range(8)))

    async def test_restart_before_send_preserves_one_stable_packed_delivery(self):
        await self.begin_telegram()
        await self.complete(['第一段', '第二段', '第三段'])
        row = self.bridge.delivery.next_row()
        expected = (row['key'], row['text'])
        self.restart_delivery()
        restarted = self.bridge.delivery.next_row()
        self.assertEqual((restarted['key'], restarted['text']), expected)
        await self.send_row(restarted)
        self.restart_delivery()
        self.assertIsNone(self.bridge.delivery.next_row())
        self.assertEqual(len(self.formal_calls()), 1)

    async def test_lost_final_response_is_not_replayed_as_a_burst_after_restart(self):
        await self.begin_telegram()
        await self.complete([f'内容 {i}' for i in range(9)])
        row = self.bridge.delivery.next_row()
        self.bridge.delivery.begin(row)
        self.assertTrue(self.bridge.delivery.failed(row, ConnectionError('lost reply')))
        self.restart_delivery()
        self.assertIsNone(self.bridge.delivery.next_row())
        self.assertEqual(self.bridge.store.db.execute('SELECT notice_state FROM completion_turns').fetchone()[0], 'uncertain')
        source = self.bridge.store.db.execute("SELECT COUNT(*) FROM outbox WHERE key LIKE 'turn:%'").fetchone()[0]
        self.assertEqual(source, 9)

    async def test_rate_limited_packed_final_allows_commands_and_retries_same_body(self):
        await self.begin_telegram()
        await self.complete(['完整的第一段', '完整的第二段'])
        row = self.bridge.delivery.next_row()
        self.bridge.delivery.begin(row)
        self.assertTrue(self.bridge.delivery.failed(row, TelegramError('sendRichMessage', 429, 'limited', retry_after=20)))
        self.bridge.store.enqueue_at('input:91:reply', '123', '系统指令回复', 42)
        command = self.bridge.delivery.next_row()
        self.assertEqual(command['text'], '系统指令回复')
        await self.send_row(command)
        self.assertIsNone(self.bridge.delivery.next_row())
        self.now += 20
        retry = self.bridge.delivery.next_row()
        self.assertEqual((retry['key'], retry['text']), (row['key'], row['text']))
        await self.send_row(retry)
        self.assertIsNone(self.bridge.delivery.next_row())

    async def test_late_unsent_content_is_merged_before_completion_notification(self):
        await self.begin_telegram()
        await self.complete(['已扫描的结果'])
        self.assertIsNotNone(self.bridge.delivery.next_row())
        self.bridge.store.enqueue_at('turn:current:late', '123', '后到的结果', 42)
        rows = await self.drain()
        self.assertEqual(len(rows), 1)
        self.assertIn('已扫描的结果', rows[0][1])
        self.assertIn('后到的结果', rows[0][1])
        self.assertEqual(rows[0][1].count('✅ 本轮任务已完成。'), 1)
        self.assertFalse(rows[0][2])

    async def test_failed_picture_warning_is_merged_into_final_text(self):
        await self.begin_telegram()
        image = self.path / 'failed.png'
        image.write_bytes(PNG)
        await self.complete(['过程保留', f'图片见下：\n\n![结果]({image})'])
        row = self.bridge.delivery.next_row()
        self.assertTrue(row['media_json'])
        self.bridge.store.images.failed(row, uncertain=True)
        rows = await self.drain()
        self.assertEqual(len(rows), 1)
        self.assertIn('图片发送结果未获确认', rows[0][1])
        self.assertIn('过程保留', rows[0][1])
        self.assertFalse(rows[0][2])

    async def test_restart_after_first_long_chunk_sends_only_remaining_suffix(self):
        await self.begin_telegram()
        text = '分块时保留所有内容🙂' * 1000
        await self.complete([text])
        first = self.bridge.delivery.next_row()
        first_text = first['text']
        self.assertTrue(self.bridge.delivery.silent(first))
        await self.send_row(first)
        self.restart_delivery()
        remaining = await self.drain()
        self.assertEqual(first_text + ''.join(row[1] for row in remaining), text + '\n\n✅ 本轮任务已完成。')
        self.assertEqual(sum(not row[2] for row in remaining), 1)
        self.restart_delivery()
        self.assertIsNone(self.bridge.delivery.next_row())

    async def test_late_content_after_completed_delivery_is_quiet_without_second_notice(self):
        await self.begin_telegram()
        await self.complete(['最初的完整结果'])
        self.assertEqual(len(await self.drain()), 1)
        self.bridge.store.enqueue_at('turn:current:late-after-send', '123', '迟到的补充内容', 42)
        tail = await self.drain()
        self.assertEqual(len(tail), 1)
        self.assertEqual(tail[0][1], '迟到的补充内容')
        self.assertTrue(tail[0][2])
        self.restart_delivery()
        self.assertIsNone(self.bridge.delivery.next_row())

    async def test_rate_limited_quiet_chunk_holds_following_chunks_but_not_controls(self):
        await self.begin_telegram()
        text = '限流时仍保留顺序🙂' * 1000
        await self.complete([text])
        first = self.bridge.delivery.next_row()
        self.assertTrue(self.bridge.delivery.silent(first))
        self.bridge.delivery.begin(first)
        self.assertTrue(self.bridge.delivery.failed(first, TelegramError('sendRichMessage', 429, 'limited', retry_after=20)))
        self.bridge.store.enqueue_at('input:quiet-retry:reply', '123', '优先响应的系统查询', 42)
        command = self.bridge.delivery.next_row()
        self.assertEqual(command['text'], '优先响应的系统查询')
        await self.send_row(command)
        self.assertIsNone(self.bridge.delivery.next_row(), 'later chunks and final notification must wait for the first chunk')
        self.now += 20
        retry = self.bridge.delivery.next_row()
        self.assertEqual((retry['key'], retry['text']), (first['key'], first['text']))
        await self.send_row(retry)
        rest = await self.drain()
        self.assertEqual(first['text'] + ''.join(row[1] for row in rest), text + '\n\n✅ 本轮任务已完成。')
        self.assertEqual(sum(not row[2] for row in rest), 1)

    async def test_crash_during_quiet_chunk_does_not_replay_that_chunk_or_sources(self):
        await self.begin_telegram()
        text = '崩溃恢复时不得重复刷屏🙂' * 1000
        await self.complete([text])
        first = self.bridge.delivery.next_row()
        self.assertTrue(self.bridge.delivery.silent(first))
        self.bridge.delivery.begin(first)
        self.restart_delivery()
        remaining = await self.drain()
        self.assertTrue(remaining)
        self.assertTrue(all(row[0] != first['key'] for row in remaining))
        merged = first['text'] + ''.join(row[1] for row in remaining)
        self.assertEqual(merged.count(UNCONFIRMED_TEXT), 1)
        self.assertIn(UNCONFIRMED_TEXT, remaining[-1][1])
        self.assertEqual(merged.replace(SUBMESSAGE_SEPARATOR + UNCONFIRMED_TEXT, ''), text + '\n\n✅ 本轮任务已完成。')
        self.assertEqual(sum(not row[2] for row in remaining), 1)
        self.restart_delivery()
        self.assertIsNone(self.bridge.delivery.next_row())
        sources = self.bridge.store.db.execute("SELECT text FROM outbox WHERE key LIKE 'turn:%' ORDER BY rowid").fetchall()
        self.assertEqual(sum(row[0] == UNCONFIRMED_TEXT for row in sources), 1)
        self.assertEqual(''.join(row[0] for row in sources if row[0] != UNCONFIRMED_TEXT), text)

    async def test_policy_reenable_preserves_prepared_body_without_historical_alert(self):
        await self.begin_telegram()
        await self.complete(['切换前仍未发送的过程', '切换前仍未发送的最终结果'])
        prepared = self.bridge.delivery.next_row()
        self.assertIn('切换前仍未发送的最终结果', prepared['text'])
        self.bridge.target['notification_mode'] = 'all'
        self.bridge.delivery = CompletionDelivery(self.bridge, clock=lambda: self.now)
        self.now += 20
        self.bridge.target['notification_mode'] = 'completion'
        self.restart_delivery()
        rows = await self.drain()
        self.assertTrue(rows, 'policy changes must not discard packed but unsent source content')
        text = ''.join(row[1] for row in rows)
        self.assertIn('切换前仍未发送的过程', text)
        self.assertIn('切换前仍未发送的最终结果', text)
        self.assertTrue(all(row[2] for row in rows), 'old completion must not become a new normal notification')
        self.restart_delivery()
        self.assertIsNone(self.bridge.delivery.next_row())

    async def test_failed_and_interrupted_statuses_merge_into_the_notifying_body(self):
        for status, label in (('failed', '❌ 本轮任务执行失败'), ('interrupted', '⏹ 本轮任务已停止')):
            with self.subTest(status=status):
                tid = 'failed-case' if status == 'failed' else 'interrupted-case'
                turn = self.turn(tid=tid, status='inProgress')
                self.bridge.delivery.watch(turn)
                self.bridge.store.enqueue_at(f'turn:{tid}:first', '123', '结束前保留的工作内容', 42)
                terminal = self.turn(tid=tid, status=status)
                self.bridge.delivery.terminal(terminal)
                self.bridge.delivery.seal(terminal)
                rows = await self.drain()
                self.assertEqual(len(rows), 1)
                self.assertIn('结束前保留的工作内容', rows[0][1])
                self.assertIn(label, rows[0][1])
                self.assertFalse(rows[0][2])
                self.assertIsNone(self.bridge.delivery.next_row())

    async def test_reenable_preserves_rate_limited_final_body_and_cooldown(self):
        await self.begin_telegram()
        await self.complete(['模式切换前第一段', '模式切换前第二段'])
        row = self.bridge.delivery.next_row()
        self.bridge.delivery.begin(row)
        self.assertTrue(self.bridge.delivery.failed(row, TelegramError('sendRichMessage', 429, 'limited', retry_after=20)))
        self.bridge.target['notification_mode'] = 'all'
        self.bridge.delivery = CompletionDelivery(self.bridge, clock=lambda: self.now)
        self.now += 1
        self.bridge.target['notification_mode'] = 'completion'
        self.restart_delivery()
        self.assertIsNone(self.bridge.delivery.next_row(), 'policy changes must preserve the server cooldown')
        self.now += 20
        rows = await self.drain()
        self.assertTrue(rows)
        text = ''.join(row[1] for row in rows)
        self.assertEqual(text.count('模式切换前第一段'), 1)
        self.assertEqual(text.count('模式切换前第二段'), 1)
        self.assertTrue(all(row[2] for row in rows), 'old rate-limited body must be retained without a historical alert')
        self.restart_delivery()
        self.assertIsNone(self.bridge.delivery.next_row())

    async def test_unknown_quiet_chunk_delivery_merges_warning_into_one_final_notice(self):
        await self.begin_telegram()
        text = '未知发送结果应明确提示🙂' * 1000
        await self.complete([text])
        first = self.bridge.delivery.next_row()
        self.assertTrue(self.bridge.delivery.silent(first))
        self.bridge.delivery.begin(first)
        self.assertTrue(self.bridge.delivery.failed(first, ConnectionError('response lost')))
        rows = await self.drain()
        self.assertTrue(rows)
        self.assertTrue(all(row[0] != first['key'] for row in rows))
        merged = first['text'] + ''.join(row[1] for row in rows)
        self.assertEqual(merged.count(UNCONFIRMED_TEXT), 1)
        self.assertIn(UNCONFIRMED_TEXT, rows[-1][1])
        self.assertEqual(merged.replace(SUBMESSAGE_SEPARATOR + UNCONFIRMED_TEXT, ''), text + '\n\n✅ 本轮任务已完成。')
        self.assertEqual(sum(not row[2] for row in rows), 1)
        self.restart_delivery()
        self.assertIsNone(self.bridge.delivery.next_row())

    async def test_late_content_rebuilds_rate_limited_final_without_bypassing_cooldown(self):
        await self.begin_telegram()
        await self.complete(['首次完整正文'])
        final = self.bridge.delivery.next_row()
        self.bridge.delivery.begin(final)
        self.assertTrue(self.bridge.delivery.failed(final, TelegramError('sendRichMessage', 429, 'limited', retry_after=20)))
        self.bridge.store.enqueue_at('turn:current:late-after429', '123', '限流期间补齐的正文', 42)
        self.assertIsNone(self.bridge.delivery.next_row(), 'coalescing late output must not bypass retry_after')
        self.now += 19
        self.assertIsNone(self.bridge.delivery.next_row())
        self.now += 1
        rows = await self.drain()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], final['key'])
        self.assertEqual(rows[0][1], '首次完整正文' + SUBMESSAGE_SEPARATOR + '限流期间补齐的正文\n\n✅ 本轮任务已完成。')
        self.assertFalse(rows[0][2])
        self.restart_delivery()
        self.assertIsNone(self.bridge.delivery.next_row())

    async def test_late_item_after_delivered_prefix_keeps_old_tail_once_and_item_separator(self):
        await self.begin_telegram()
        text = '已部分发送的原始条目🙂' * 1000
        await self.complete([text])
        first = self.bridge.delivery.next_row()
        self.assertTrue(self.bridge.delivery.silent(first))
        await self.send_row(first)
        self.bridge.store.enqueue_at('turn:current:after-prefix', '123', '前段发送之后补充的新条目', 42)
        remaining = await self.drain()
        self.assertTrue(all(row[0] != first['key'] for row in remaining))
        self.assertEqual(first['text'] + ''.join(row[1] for row in remaining),
                         text + SUBMESSAGE_SEPARATOR + '前段发送之后补充的新条目\n\n✅ 本轮任务已完成。')
        self.assertEqual(sum(not row[2] for row in remaining), 1)
        self.restart_delivery()
        self.assertIsNone(self.bridge.delivery.next_row())

    async def test_changed_topic_does_not_receive_frozen_packed_results(self):
        await self.begin_telegram()
        await self.complete(['原话题结果第一段', '原话题结果第二段'])
        self.assertIsNotNone(self.bridge.delivery.next_row())
        self.bridge.target['message_thread_id'] = 99
        self.assertIsNone(self.bridge.delivery.next_row())
        self.restart_delivery()
        self.assertIsNone(self.bridge.delivery.next_row())
        self.assertEqual(self.formal_calls(), [])


if __name__ == '__main__':
    unittest.main()
