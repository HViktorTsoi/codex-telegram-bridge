import json
import unittest

from completion_delivery import CompletionDelivery
from message_format import utf16_size
import test_completion_delivery as fixtures


class BlockPreviewTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.CompletionDeliveryTests.asyncSetUp
    asyncTearDown = fixtures.CompletionDeliveryTests.asyncTearDown
    turn = fixtures.CompletionDeliveryTests.turn
    output = fixtures.CompletionDeliveryTests.output
    all_outbox = fixtures.CompletionDeliveryTests.all_outbox

    def select(self, global_style='block', target_style=None):
        self.bridge.config['preview_style'] = global_style
        self.bridge.target.pop('preview_style', None)
        if target_style is not None:
            self.bridge.target['preview_style'] = target_style
        self.delivery = CompletionDelivery(self.bridge, clock=lambda: self.now)

    def block_text(self, index=-1):
        method, params = self.calls[index]
        self.assertEqual(method, 'sendRichMessageDraft')
        self.assertNotIn('text', params)
        self.assertEqual(set(params['rich_message']), {'blocks'})
        blocks = params['rich_message']['blocks']
        self.assertIn(len(blocks), (1, 2))
        self.assertTrue(all(block['type'] == 'thinking' for block in blocks))
        return '\n\n'.join(block['text'] for block in blocks)

    async def test_block_preview_separates_status_from_cumulative_literal_body(self):
        self.select()
        first_body = '第一段 **公开进度**，保留 <tag> & 原样文字。路径 /private/data.png 只是说明。'
        self.output(first_body)
        await self.delivery.update(self.telegram, turns=[self.turn()])
        first_text = self.block_text()
        first_id = self.calls[-1][1]['draft_id']
        self.assertIn('正在工作', first_text)
        self.assertTrue(first_text.endswith(first_body))
        blocks = self.calls[-1][1]['rich_message']['blocks']
        self.assertEqual(len(blocks), 2)
        self.assertEqual(len(blocks[0]['text'].splitlines()), 2)
        self.assertNotIn(first_body, blocks[0]['text'])
        self.assertEqual(blocks[1]['text'], first_body)

        self.output('第二段新增进度。', 'turn:work:second')
        self.now += 15
        await self.delivery.update(self.telegram, turns=[self.turn()])
        text = self.block_text()
        self.assertIn(first_body + '\n\n第二段新增进度。', text)
        blocks = self.calls[-1][1]['rich_message']['blocks']
        self.assertEqual(len(blocks), 2)
        self.assertEqual(blocks[1]['text'], first_body + '\n\n第二段新增进度。')
        self.assertEqual(self.calls[-1][1]['draft_id'], first_id)
        self.assertEqual(self.calls[-1][1]['chat_id'], 123)
        self.assertEqual(self.calls[-1][1]['message_thread_id'], 42)
        self.assertTrue(all(method == 'sendRichMessageDraft' for method, _ in self.calls))
        self.assertIsNone(self.delivery.next_row())
        self.assertEqual(len(self.all_outbox()), 2)
        self.assertTrue(all(row['message_id'] is None and row['media_json'] is None for row in self.all_outbox()))

    async def test_block_status_refreshes_at_fifteen_seconds_without_new_normal_messages(self):
        self.select()
        self.output()
        await self.delivery.update(self.telegram, turns=[self.turn(started=self.now)])
        first_id = self.calls[0][1]['draft_id']
        self.now += 14
        await self.delivery.update(self.telegram, turns=[self.turn()])
        self.assertEqual(len(self.calls), 1)
        self.now += 1
        await self.delivery.update(self.telegram, turns=[self.turn()])
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.calls[-1][1]['draft_id'], first_id)
        self.assertIn('15秒', self.block_text())
        self.assertTrue(all(method == 'sendRichMessageDraft' for method, _ in self.calls))

    async def test_waiting_and_disconnection_update_the_same_public_progress_block(self):
        self.select()
        self.output('需要保留的过程正文')
        await self.delivery.update(self.telegram, turns=[self.turn()],
                                   status={'activeFlags': ['waitingOnUserInput']})
        draft_id = self.calls[-1][1]['draft_id']
        self.assertIn('需要你处理', self.block_text())
        self.now += 15
        await self.delivery.update(self.telegram, connected=False)
        self.assertIn('连接中断', self.block_text())
        self.assertIn('需要保留的过程正文', self.block_text())
        self.assertEqual(self.calls[-1][1]['draft_id'], draft_id)
        self.assertIsNone(self.delivery.next_row())

    async def test_block_limit_keeps_the_full_queue_and_marks_omitted_older_text(self):
        self.select()
        body = '原始第一段\n' + '😀累计过程' * 1800 + '\n原始最后一段'
        self.output(body)
        await self.delivery.update(self.telegram, turns=[self.turn()])
        text = self.block_text()
        self.assertLessEqual(utf16_size(text), 3800)
        self.assertIn('预览仅显示最近部分', text)
        self.assertTrue(text.endswith('原始最后一段'))
        self.assertEqual(''.join(row['text'] for row in self.all_outbox()), body)
        self.assertTrue(all(row['message_id'] is None for row in self.all_outbox()))

    async def test_block_draft_identity_and_mode_window_survive_restart(self):
        self.select()
        self.output()
        await self.delivery.update(self.telegram, turns=[self.turn()])
        first_id = self.calls[-1][1]['draft_id']
        since = self.delivery.state()['since']
        self.now += 15
        self.delivery = CompletionDelivery(self.bridge, clock=lambda: self.now)
        await self.delivery.update(self.telegram, turns=[self.turn()])
        self.assertEqual(self.calls[-1][1]['draft_id'], first_id)
        self.assertEqual(self.delivery.state()['since'], since)
        self.assertTrue(self.block_text().endswith('阶段一'))

    async def test_preview_style_defaults_to_plain_and_target_override_wins(self):
        await self.delivery.update(self.telegram, turns=[self.turn()])
        first_id = self.calls[-1][1]['draft_id']
        self.assertEqual(self.calls[-1][0], 'sendMessageDraft')

        self.now += 15
        self.select(global_style='block', target_style='plain')
        await self.delivery.update(self.telegram, turns=[self.turn()])
        self.assertEqual(self.calls[-1][0], 'sendMessageDraft')
        self.assertEqual(self.calls[-1][1]['draft_id'], first_id)

        self.now += 15
        self.select(global_style='plain', target_style='block')
        await self.delivery.update(self.telegram, turns=[self.turn()])
        self.assertEqual(self.calls[-1][1]['draft_id'], first_id)
        self.block_text()

    async def test_unsupported_block_preview_never_sends_an_ordinary_fallback(self):
        self.select()
        self.output('接口不支持时仍保存完整过程')
        for code in (400, 404):
            with self.subTest(code=code):
                self.error = fixtures.ApiError(code)
                state = self.delivery.state()
                state.pop('draft_unsupported', None)
                self.store.set(CompletionDelivery.KEY, json.dumps(state))
                self.now += 30
                before = len(self.calls)
                await self.delivery.update(self.telegram, turns=[self.turn()])
                self.now += 30
                await self.delivery.update(self.telegram, turns=[self.turn()])
                self.assertEqual(len(self.calls), before + 1)
                self.assertEqual(self.calls[-1][0], 'sendRichMessageDraft')
                self.assertTrue(self.delivery.state()['draft_unsupported'])
                self.assertIsNone(self.delivery.next_row())
        self.assertEqual(''.join(row['text'] for row in self.all_outbox()), '接口不支持时仍保存完整过程')

    async def test_changing_preview_style_clears_only_the_stale_unsupported_marker(self):
        self.output('切换显示方式仍保留这轮过程')
        await self.delivery.update(self.telegram, turns=[self.turn()])
        since = self.delivery.state()['since']
        draft_id = self.calls[-1][1]['draft_id']
        self.error = fixtures.ApiError(404)
        self.now += 15
        await self.delivery.update(self.telegram, turns=[self.turn()])
        self.assertTrue(self.delivery.state()['draft_unsupported'])

        self.error = None
        self.now += 15
        self.select('block')
        self.assertEqual(self.delivery.state()['since'], since)
        self.assertFalse(self.delivery.state().get('draft_unsupported', False))
        await self.delivery.update(self.telegram, turns=[self.turn()])
        self.assertEqual(self.calls[-1][1]['draft_id'], draft_id)
        self.assertIn('切换显示方式仍保留这轮过程', self.block_text())
        self.assertIsNone(self.delivery.next_row())

    async def test_same_style_restart_keeps_unsupported_block_marker(self):
        self.select()
        self.error = fixtures.ApiError(404)
        await self.delivery.update(self.telegram, turns=[self.turn()])
        self.assertTrue(self.delivery.state()['draft_unsupported'])
        self.now += 30
        self.delivery = CompletionDelivery(self.bridge, clock=lambda: self.now)
        await self.delivery.update(self.telegram, turns=[self.turn()])
        self.assertEqual(len(self.calls), 1)
        self.assertTrue(self.delivery.state()['draft_unsupported'])

    async def test_unknown_preview_style_fails_configuration_validation(self):
        self.bridge.config['preview_style'] = 'unsupported-style'
        with self.assertRaises(ValueError):
            CompletionDelivery(self.bridge, clock=lambda: self.now)
