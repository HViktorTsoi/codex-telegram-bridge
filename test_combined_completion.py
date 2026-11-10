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

    async def test_public_history_is_one_quiet_message_and_final_answer_is_separate(self):
        await self.begin_telegram()
        texts = [f'第 {i} 项工作内容' for i in range(12)]
        await self.complete(texts)
        rows = await self.drain()
        self.assertEqual([row[1] for row in rows], [SUBMESSAGE_SEPARATOR.join(texts[:-1]), texts[-1]])
        self.assertEqual([row[2] for row in rows], [True, False])
        self.assertEqual(len(self.formal_calls()), 2)
        self.assertTrue(all(params['message_thread_id'] == 42 for _, params in self.formal_calls()))
        self.assertNotIn('✅ 本轮任务已完成', ''.join(row[1] for row in rows))
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
        self.assertEqual(''.join(row[1] for row in rows), text)
        self.assertEqual(sum(not row[2] for row in rows), 1)
        self.assertFalse(rows[-1][2])
        self.assertTrue(all(row[2] for row in rows[:-1]))

    async def test_rich_code_fences_are_rebalanced_after_source_chunks_are_joined(self):
        await self.begin_telegram()
        text = '```python\n' + ('print("完整代码")\n' * 430) + '```\n代码之后'
        await self.complete([text])
        rows = await self.drain()
        self.assertEqual(''.join(row[1] for row in rows), text)
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
        self.assertEqual(len(rows), 3)
        self.assertTrue(rows[0][3])
        self.assertTrue(rows[0][2])
        self.assertEqual(rows[1][1], '处理过程')
        self.assertTrue(rows[1][2])
        self.assertFalse(rows[2][3])
        self.assertFalse(rows[2][2])
        self.assertIn('结果已生成', rows[2][1])
        self.assertNotIn('处理过程', rows[2][1])
        self.assertNotIn('✅ 本轮任务已完成', rows[2][1])
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
        self.assertEqual([row[1] for row in packed],
                         [SUBMESSAGE_SEPARATOR.join(f'未完成过程 {i}' for i in range(7)), '未完成过程 7'])
        self.assertEqual([row[2] for row in packed], [True, False])

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
        remaining = await self.drain()
        self.assertEqual([row[1] for row in remaining], ['第三段'])
        self.assertFalse(remaining[0][2])
        self.assertEqual(len(self.formal_calls()), 2)
        self.restart_delivery()
        self.assertIsNone(self.bridge.delivery.next_row())

    async def test_lost_final_response_is_not_replayed_as_a_burst_after_restart(self):
        await self.begin_telegram()
        await self.complete([f'内容 {i}' for i in range(9)])
        history = self.bridge.delivery.next_row()
        self.assertTrue(self.bridge.delivery.silent(history))
        await self.send_row(history)
        row = self.bridge.delivery.next_row()
        self.assertEqual(row['text'], '内容 8')
        self.assertFalse(self.bridge.delivery.silent(row))
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
        history = self.bridge.delivery.next_row()
        self.assertEqual(history['text'], '完整的第一段')
        self.assertTrue(self.bridge.delivery.silent(history))
        await self.send_row(history)
        row = self.bridge.delivery.next_row()
        self.assertEqual(row['text'], '完整的第二段')
        self.assertFalse(self.bridge.delivery.silent(row))
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
        self.assertEqual([row[1] for row in rows], ['后到的结果', '已扫描的结果'])
        self.assertEqual([row[2] for row in rows], [True, False])
        self.assertNotIn('✅ 本轮任务已完成', ''.join(row[1] for row in rows))

    async def test_failed_picture_warning_is_history_before_the_clean_final_answer(self):
        await self.begin_telegram()
        image = self.path / 'failed.png'
        image.write_bytes(PNG)
        await self.complete(['过程保留', f'图片见下：\n\n![结果]({image})'])
        row = self.bridge.delivery.next_row()
        self.assertTrue(row['media_json'])
        self.bridge.store.images.failed(row, uncertain=True)
        rows = await self.drain()
        self.assertEqual(len(rows), 2)
        self.assertIn('图片发送结果未获确认', rows[0][1])
        self.assertIn('过程保留', rows[0][1])
        self.assertTrue(rows[0][2])
        self.assertEqual(rows[1][1], '图片见下：')
        self.assertFalse(rows[1][2])

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
        self.assertEqual(first_text + ''.join(row[1] for row in remaining), text)
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
        self.assertEqual(first['text'] + ''.join(row[1] for row in rest), text)
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
        self.assertNotIn(UNCONFIRMED_TEXT, remaining[-1][1])
        self.assertEqual(first['text'] + ''.join(row[1] for row in remaining if UNCONFIRMED_TEXT not in row[1]), text)
        self.assertTrue(all(row[2] for row in remaining if UNCONFIRMED_TEXT in row[1]))
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
        self.assertEqual(prepared['text'], '切换前仍未发送的过程')
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

    async def test_unknown_quiet_chunk_warning_is_quiet_and_does_not_pollute_final(self):
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
        self.assertNotIn(UNCONFIRMED_TEXT, rows[-1][1])
        self.assertEqual(first['text'] + ''.join(row[1] for row in rows if UNCONFIRMED_TEXT not in row[1]), text)
        self.assertTrue(all(row[2] for row in rows if UNCONFIRMED_TEXT in row[1]))
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
        self.assertEqual([row[1] for row in rows], ['限流期间补齐的正文', '首次完整正文'])
        self.assertEqual(rows[-1][0], final['key'])
        self.assertEqual([row[2] for row in rows], [True, False])
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
        late = [row for row in remaining if '前段发送之后补充的新条目' in row[1]]
        self.assertEqual(len(late), 1)
        self.assertEqual(late[0][1], '前段发送之后补充的新条目')
        self.assertTrue(late[0][2])
        self.assertEqual(first['text'] + ''.join(row[1] for row in remaining if row not in late), text)
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

    async def test_final_only_has_no_empty_history_or_success_footer(self):
        await self.begin_telegram()
        await self.complete(['唯一的最终回复。'])
        rows = await self.drain()
        self.assertEqual([row[1] for row in rows], ['唯一的最终回复。'])
        self.assertEqual([row[2] for row in rows], [False])
        self.restart_delivery()
        self.assertIsNone(self.bridge.delivery.next_row())

    async def test_assistant_authored_status_words_are_preserved_verbatim(self):
        await self.begin_telegram()
        answer = '原文需要保留以下句子：\n\n✅ 本轮任务已完成。'
        await self.complete(['前面的公开过程', answer])
        history = self.bridge.delivery.next_row()
        self.assertEqual(history['text'], '前面的公开过程')
        await self.send_row(history)
        final = self.bridge.delivery.next_row()
        self.assertEqual(final['text'], answer)
        self.bridge.delivery.begin(final)
        self.assertTrue(self.bridge.delivery.failed(final, TelegramError('sendRichMessage', 429, 'limited', retry_after=15)))
        self.bridge.store.enqueue_at('turn:current:late-status-test', '123', '限流后补齐的公开过程', 42)
        self.restart_delivery()
        self.now += 15
        rows = await self.drain()
        self.assertEqual([row[1] for row in rows], ['限流后补齐的公开过程', answer])
        self.assertEqual([row[2] for row in rows], [True, False])

    async def test_history_and_long_final_keep_whole_logical_item_boundaries(self):
        await self.begin_telegram()
        final = '最终回复全文🙂' * 1300
        await self.complete(['公开进度一', '公开进度二', final])
        rows = await self.drain()
        self.assertEqual(rows[0][1], '公开进度一' + SUBMESSAGE_SEPARATOR + '公开进度二')
        self.assertEqual(''.join(row[1] for row in rows[1:]), final)
        self.assertGreater(len(rows), 2)
        self.assertTrue(all(utf16_size(row[1]) <= 3800 for row in rows))
        self.assertTrue(all(row[2] for row in rows[:-1]))
        self.assertFalse(rows[-1][2])
        self.assertNotIn(SUBMESSAGE_SEPARATOR, ''.join(row[1] for row in rows[1:]))

    async def test_last_final_answer_wins_over_an_earlier_final_and_later_commentary(self):
        await self.begin_telegram()
        self.items['current'] = [self.entry('早期过程', 'progress'),
                                 self.entry('先前的最终标记', 'first-final', 'final_answer'),
                                 self.entry('真正的最终回复', 'last-final', 'final_answer'),
                                 self.entry('随后补到的公开进度', 'late-commentary')]
        self.turns = [self.turn(status='completed')]
        self.bridge.finish_turn(self.turns[0])
        await self.cycle()
        rows = await self.drain()
        self.assertEqual([row[1] for row in rows],
                         [SUBMESSAGE_SEPARATOR.join(['早期过程', '先前的最终标记', '随后补到的公开进度']),
                          '真正的最终回复'])
        self.assertEqual([row[2] for row in rows], [True, False])

    async def test_phase_less_history_chooses_last_assistant_not_private_items(self):
        await self.begin_telegram()
        self.items['current'] = [self.entry('旧协议过程', 'progress', None),
                                 self.entry('旧协议最终回复', 'final', None),
                                 {'item': {'id': 'private', 'type': 'reasoning', 'text': 'PRIVATE REASONING'}},
                                 {'item': {'id': 'tool', 'type': 'commandExecution', 'text': 'PRIVATE TOOL OUTPUT'}}]
        self.turns = [self.turn(status='completed')]
        self.bridge.finish_turn(self.turns[0])
        await self.cycle()
        rows = await self.drain()
        self.assertEqual([row[1] for row in rows], ['旧协议过程', '旧协议最终回复'])
        self.assertEqual([row[2] for row in rows], [True, False])
        self.assertNotIn('PRIVATE', ''.join(row[1] for row in rows))

    async def test_success_without_text_uses_one_nonempty_notice_without_success_footer(self):
        await self.begin_telegram()
        await self.complete([])
        rows = await self.drain()
        self.assertEqual([row[1] for row in rows], ['本轮没有文本回复。'])
        self.assertEqual([row[2] for row in rows], [False])

    async def test_image_only_success_sends_attachment_then_nonempty_notice(self):
        await self.begin_telegram()
        image = self.path / 'image-only.png'
        image.write_bytes(PNG)
        await self.complete([f'![图片](<{image}>)'])
        rows = await self.drain()
        self.assertEqual(len(rows), 2)
        self.assertTrue(rows[0][3])
        self.assertTrue(rows[0][2])
        self.assertEqual(rows[1][1], '本轮没有文本回复。')
        self.assertFalse(rows[1][2])
        self.assertEqual([method for method, _ in self.formal_calls()], ['sendPhoto', 'sendRichMessage'])

    async def test_legacy_final_without_timestamp_wins_over_new_canonical_commentary(self):
        await self.begin_telegram()
        self.bridge.store.enqueue_at('turn:current:legacy-live-final', '123', '已排队的真实最终回复', 42)
        final = self.entry('已排队的真实最终回复', 'renumbered-final', 'final_answer')
        final.pop('completedAtMs')
        self.items['current'] = [self.entry('终态扫描新找到的公开过程', 'new-progress'), final]
        self.turns = [self.turn(status='completed')]
        self.bridge.finish_turn(self.turns[0])
        await self.cycle()
        rows = await self.drain()
        self.assertEqual([row[1] for row in rows], ['终态扫描新找到的公开过程', '已排队的真实最终回复'])
        self.assertEqual([row[2] for row in rows], [True, False])
        originals = self.bridge.store.db.execute("SELECT text FROM outbox WHERE key LIKE 'turn:%' ORDER BY rowid").fetchall()
        self.assertEqual(sum(row[0] == '已排队的真实最终回复' for row in originals), 1)

    async def legacy_v21_plan(self, cooldown=0):
        """Install the actual pre-split ledger shape, with canonical rows handed off."""
        await self.begin_telegram()
        await self.complete(['旧版尚未发送的过程', '旧版尚未发送的最终回复'])
        delivery, db = self.bridge.delivery, self.bridge.store.db
        group = delivery._group('current')
        final_key = f"completion-notice:{delivery.batches.digest(group)}:topic:42:0"
        text = '旧版尚未发送的过程' + SUBMESSAGE_SEPARATOR + '旧版尚未发送的最终回复\n\n✅ 本轮任务已完成。'
        sources = db.execute("SELECT * FROM outbox WHERE key LIKE 'turn:%' ORDER BY rowid").fetchall()
        with db:
            db.execute('DELETE FROM completion_final_sources')
            for source in sources:
                db.execute('INSERT OR IGNORE INTO completion_rows VALUES(?,?,?,?)',
                           (source['key'], 'current', '123', 42))
                db.execute('INSERT INTO completion_packet_sources VALUES(?,?,?,?,?)',
                           (source['key'], 'current', '123', 42, 1))
                db.execute('UPDATE outbox SET message_id=-2 WHERE key=?', (source['key'],))
            db.execute('INSERT INTO outbox(key,chat_id,text,rich_text,message_thread_id,retry_at,attempts) '
                       'VALUES(?,?,?,?,?,?,?)',
                       (final_key, '123', text, text, 42, self.now + cooldown if cooldown else 0, int(bool(cooldown))))
            db.execute("UPDATE completion_turns SET notice_state='queued',notice_key=? WHERE turn_id='current'",
                       (final_key,))
        self.restart_delivery()
        return final_key

    async def test_legacy_v21_queued_plan_preserves_each_source_once_across_restart(self):
        await self.legacy_v21_plan()
        rows = await self.drain()
        text = ''.join(row[1] for row in rows)
        self.assertEqual(text.count('旧版尚未发送的过程'), 1)
        self.assertEqual(text.count('旧版尚未发送的最终回复'), 1)
        self.assertEqual(sum(not row[2] for row in rows), 1)
        self.restart_delivery()
        self.assertIsNone(self.bridge.delivery.next_row())

    async def test_legacy_v21_rate_limited_plan_keeps_cooldown_without_source_replay(self):
        final_key = await self.legacy_v21_plan(cooldown=20)
        self.assertIsNone(self.bridge.delivery.next_row())
        self.bridge.store.enqueue_at('input:upgrade-control:reply', '123', '升级后仍响应系统查询', 42)
        command = self.bridge.delivery.next_row()
        self.assertEqual(command['text'], '升级后仍响应系统查询')
        await self.send_row(command)
        self.now += 19
        self.assertIsNone(self.bridge.delivery.next_row())
        self.now += 1
        rows = await self.drain()
        self.assertEqual(rows[-1][0], final_key)
        text = ''.join(row[1] for row in rows)
        self.assertEqual(text.count('旧版尚未发送的过程'), 1)
        self.assertEqual(text.count('旧版尚未发送的最终回复'), 1)
        self.assertEqual(sum(not row[2] for row in rows), 1)
        self.restart_delivery()
        self.assertIsNone(self.bridge.delivery.next_row())


if __name__ == '__main__':
    unittest.main()
