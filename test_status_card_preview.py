"""A persistent status message and ephemeral body must remain independent."""
import copy
import json
import unittest

from completion_delivery import CompletionDelivery
import test_completion_delivery as fixtures
import test_receipt_preview as receipt_fixtures


class StatusCardPreviewTests(unittest.IsolatedAsyncioTestCase):
    asyncTearDown = fixtures.CompletionDeliveryTests.asyncTearDown
    turn = fixtures.CompletionDeliveryTests.turn
    output = fixtures.CompletionDeliveryTests.output
    mark_sent = fixtures.CompletionDeliveryTests.mark_sent
    accept = receipt_fixtures.ReceiptPreviewTests.accept

    async def asyncSetUp(self):
        await fixtures.CompletionDeliveryTests.asyncSetUp(self)
        self.bridge.config['preview_style'] = 'status-card'
        self.delivery = CompletionDelivery(self.bridge, clock=lambda: self.now)
        self.sequence = 100
        self.failures = {}
        self.after_call = None
        test = self

        class Telegram:
            async def call(self, method, **params):
                test.calls.append((method, copy.deepcopy(params)))
                failures = test.failures.get(method, [])
                if failures:
                    raise failures.pop(0)
                if test.after_call is not None:
                    await test.after_call(method, params)
                if method in ('sendMessageDraft', 'sendRichMessageDraft'):
                    return True
                test.sequence += 1
                return {'message_id': params.get('message_id', test.sequence),
                        'message_thread_id': params.get('message_thread_id')}

        self.telegram = Telegram()

    def creates(self):
        return [(method, params) for method, params in self.calls
                if method in ('sendMessage', 'sendRichMessage')]

    def edits(self):
        return [params for method, params in self.calls if method == 'editMessageText']

    def drafts(self):
        return [params for method, params in self.calls if method == 'sendRichMessageDraft']

    @staticmethod
    def card_text(params):
        return params.get('text') or json.dumps(params.get('rich_message', {}), ensure_ascii=False)

    def restart(self):
        self.delivery = CompletionDelivery(self.bridge, clock=lambda: self.now)

    async def test_receipt_creates_only_a_quiet_status_message_without_a_fake_turn(self):
        self.accept('这段是用户任务，不能进入状态栏')
        self.assertTrue(await self.delivery.receipt(self.telegram))
        self.assertEqual(len(self.creates()), 1)
        _, params = self.creates()[0]
        self.assertTrue(params['disable_notification'])
        self.assertEqual(params['message_thread_id'], 42)
        self.assertIn('已收到', self.card_text(params))
        self.assertNotIn('这段是用户任务', self.card_text(params))
        self.assertEqual(self.drafts(), [])
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM completion_turns').fetchone()[0], 0)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM outbox').fetchone()[0], 0)

    async def test_failed_input_before_native_turn_updates_the_existing_receipt_card(self):
        self.accept('尚未启动原生任务的输入', 1)
        await self.delivery.receipt(self.telegram)
        card_id = self.delivery.status_card.load()['message_id']
        self.store.mark(1, 'failed')
        self.now += 15
        await self.delivery.receipt(self.telegram)
        self.assertEqual(len(self.creates()), 1)
        self.assertEqual(self.drafts(), [])
        self.assertEqual(self.edits()[-1]['message_id'], card_id)
        self.assertIn('未能提交', self.card_text(self.edits()[-1]))
        self.assertNotIn('正在准备', self.card_text(self.edits()[-1]))
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM completion_turns').fetchone()[0], 0)

    async def test_rejected_supplement_does_not_mark_an_existing_native_turn_failed(self):
        self.accept('正在执行的原输入', 1)
        self.store.mark(1, 'running', 'work')
        self.output('原任务仍在执行的正文')
        await self.delivery.update(self.telegram, turns=[self.turn()])
        self.now += 1
        self.accept('被拒绝的补充输入', 2)
        await self.delivery.receipt(self.telegram)
        self.store.mark(2, 'failed')
        self.now += 15
        await self.delivery.receipt(self.telegram)
        await self.delivery.update(self.telegram, turns=[self.turn()])
        self.assertEqual(len(self.creates()), 1)
        self.assertNotIn('失败', self.card_text(self.edits()[-1]))
        self.assertIn('正在工作', self.card_text(self.edits()[-1]))
        self.assertEqual(self.drafts()[-1]['rich_message']['blocks'][0]['text'], '原任务仍在执行的正文')

    async def test_status_message_precedes_a_separate_body_only_draft(self):
        body = '正文 **原样保留**，含 <tag> & 特殊字符。'
        self.output(body)
        await self.delivery.update(self.telegram, turns=[self.turn()])
        self.assertEqual(len(self.creates()), 1)
        self.assertEqual(len(self.drafts()), 1)
        self.assertIn('正在工作', self.card_text(self.creates()[0][1]))
        self.assertNotIn(body, self.card_text(self.creates()[0][1]))
        self.assertEqual(self.drafts()[0]['rich_message']['blocks'], [{'type': 'thinking', 'text': body}])
        self.assertIn(self.calls[0][0], ('sendMessage', 'sendRichMessage'))
        self.assertEqual(self.calls[-1][0], 'sendRichMessageDraft')
        self.assertIsNone(self.delivery.next_row())

    async def test_fifteen_second_heartbeat_edits_the_same_card_and_preserves_cumulative_body(self):
        self.output('第一段')
        await self.delivery.update(self.telegram, turns=[self.turn()])
        first_draft = self.drafts()[0]['draft_id']
        card_id = self.delivery.status_card.load()['message_id']
        self.now += 14
        await self.delivery.update(self.telegram, turns=[self.turn()])
        self.assertEqual(len(self.calls), 2)
        self.output('第二段', 'turn:work:second')
        self.now += 1
        await self.delivery.update(self.telegram, turns=[self.turn()])
        self.assertEqual(len(self.creates()), 1)
        self.assertEqual([params['message_id'] for params in self.edits()], [card_id])
        self.assertEqual(self.drafts()[-1]['draft_id'], first_draft)
        self.assertEqual(self.drafts()[-1]['rich_message']['blocks'][0]['text'], '第一段\n\n第二段')
        self.assertEqual(self.calls[-2][0], 'editMessageText')
        self.assertEqual(self.calls[-1][0], 'sendRichMessageDraft')

    async def test_restart_reuses_status_message_and_body_draft_identity(self):
        self.output('重启前已经存在的正文')
        await self.delivery.update(self.telegram, turns=[self.turn()])
        card_id = self.delivery.status_card.load()['message_id']
        draft_id = self.drafts()[-1]['draft_id']
        self.restart()
        self.now += 15
        await self.delivery.update(self.telegram, turns=[self.turn()])
        self.assertEqual(len(self.creates()), 1)
        self.assertEqual(self.edits()[-1]['message_id'], card_id)
        self.assertEqual(self.drafts()[-1]['draft_id'], draft_id)

    async def test_next_turn_reuses_the_existing_status_message(self):
        self.output('第一轮正文')
        await self.delivery.update(self.telegram, turns=[self.turn()])
        card_id = self.delivery.status_card.load()['message_id']
        terminal = self.turn('completed')
        self.delivery.seal(terminal)
        final = self.delivery.next_row()
        self.delivery.begin(final)
        self.mark_sent(final)
        await self.delivery.complete_status(self.telegram, final)
        self.now += 15
        self.output('第二轮正文', 'turn:second:body')
        await self.delivery.update(self.telegram, turns=[self.turn(tid='second', started=self.now)])
        self.assertEqual(len(self.creates()), 1)
        self.assertTrue(self.edits())
        self.assertTrue(all(params['message_id'] == card_id for params in self.edits()))
        self.assertEqual(self.drafts()[-1]['rich_message']['blocks'][0]['text'], '第二轮正文')

    async def test_unknown_creation_response_does_not_create_a_second_status_message(self):
        # Both supported status send methods fail if selected; body drafts work.
        self.failures = {method: [ConnectionError('lost create response')]
                         for method in ('sendMessage', 'sendRichMessage')}
        self.output('状态创建不明确时仍可预览正文')
        await self.delivery.update(self.telegram, turns=[self.turn()])
        self.assertEqual(len(self.creates()), 1)
        self.assertEqual(len(self.drafts()), 1)
        self.restart()
        self.now += 15
        await self.delivery.update(self.telegram, turns=[self.turn()])
        self.assertEqual(len(self.creates()), 1)
        self.assertEqual(len(self.drafts()), 2)

    async def test_crash_during_status_creation_never_blindly_replays_it(self):
        self.output()
        await self.delivery.update(self.telegram, turns=[self.turn()])
        state = self.delivery.status_card.load()
        state.pop('message_id', None)
        state['delivery'] = 'creating'
        self.delivery.status_card.save(state)
        self.restart()
        self.now += 15
        await self.delivery.update(self.telegram, turns=[self.turn()])
        self.assertEqual(len(self.creates()), 1)
        self.assertEqual(len(self.drafts()), 2)

    async def test_status_creation_rate_limit_retries_after_cooldown_without_losing_body(self):
        self.failures = {method: [fixtures.ApiError(429, retry_after=20)]
                         for method in ('sendMessage', 'sendRichMessage')}
        self.output('限流时仍保留的正文')
        await self.delivery.update(self.telegram, turns=[self.turn()])
        self.assertEqual(len(self.creates()), 1)
        self.assertEqual(len(self.drafts()), 1)
        self.now += 15
        await self.delivery.update(self.telegram, turns=[self.turn()])
        self.assertEqual(len(self.creates()), 1)
        self.now += 15
        await self.delivery.update(self.telegram, turns=[self.turn()])
        self.assertEqual(len(self.creates()), 2)
        self.assertIsNotNone(self.delivery.status_card.load().get('message_id'))
        self.assertEqual(self.drafts()[-1]['rich_message']['blocks'][0]['text'], '限流时仍保留的正文')

    async def test_delayed_first_status_creation_immediately_redraws_the_body_it_clears(self):
        self.failures['sendMessage'] = [fixtures.ApiError(429, retry_after=20)]
        self.output('独立状态卡创建后必须立即恢复的正文')
        await self.delivery.update(self.telegram, turns=[self.turn()])
        self.assertEqual(len(self.creates()), 1)
        self.assertEqual(len(self.drafts()), 1)
        self.now += 15
        await self.delivery.update(self.telegram, turns=[self.turn()])
        self.assertEqual(len(self.creates()), 1)
        self.assertEqual(len(self.drafts()), 2)
        self.now += 5
        before_retry = len(self.calls)
        await self.delivery.update(self.telegram, turns=[self.turn()])
        retry_calls = self.calls[before_retry:]
        self.assertEqual([method for method, _ in retry_calls if method != 'editMessageText'],
                         ['sendMessage', 'sendRichMessageDraft'])
        self.assertEqual(retry_calls[-1][0], 'sendRichMessageDraft')
        self.assertEqual(len(self.creates()), 2)
        self.assertEqual(len(self.drafts()), 3)
        self.assertEqual(self.drafts()[-1]['rich_message']['blocks'][0]['text'],
                         '独立状态卡创建后必须立即恢复的正文')
        self.assertIsNotNone(self.delivery.status_card.load().get('message_id'))

    async def test_unsupported_body_drafts_do_not_disable_independent_status_updates(self):
        self.failures['sendRichMessageDraft'] = [fixtures.ApiError(404)]
        self.output('正文接口暂不可用')
        await self.delivery.update(self.telegram, turns=[self.turn()])
        self.assertTrue(self.delivery.state().get('draft_unsupported'))
        self.now += 15
        await self.delivery.update(self.telegram, turns=[self.turn()])
        self.assertEqual(len(self.creates()), 1)
        self.assertEqual(len(self.edits()), 1)
        self.assertEqual(len(self.drafts()), 1)
        self.assertIsNone(self.delivery.next_row())

    async def test_body_draft_cooldown_does_not_delay_status_heartbeat(self):
        self.failures['sendRichMessageDraft'] = [fixtures.ApiError(429, retry_after=60)]
        self.output('正文草稿有较长冷却')
        await self.delivery.update(self.telegram, turns=[self.turn()])
        self.now += 15
        await self.delivery.update(self.telegram, turns=[self.turn()])
        self.assertEqual(len(self.edits()), 1)
        self.assertEqual(len(self.drafts()), 1)

    async def test_permanent_status_edit_failure_does_not_retry_forever_or_block_body(self):
        self.output('状态卡失败不应阻止正文预览')
        await self.delivery.update(self.telegram, turns=[self.turn()])
        self.failures['editMessageText'] = [fixtures.ApiError(403)]
        self.now += 15
        await self.delivery.update(self.telegram, turns=[self.turn()])
        self.assertEqual(len(self.edits()), 1)
        self.now += 30
        await self.delivery.update(self.telegram, turns=[self.turn()])
        self.assertEqual(len(self.edits()), 1)
        self.assertEqual(len(self.creates()), 1)
        self.assertEqual(len(self.drafts()), 3)
        self.assertEqual(self.drafts()[-1]['rich_message']['blocks'][0]['text'], '状态卡失败不应阻止正文预览')

    async def test_terminal_status_is_edited_after_final_body_acknowledgement(self):
        self.output('已有的完整正文')
        await self.delivery.update(self.telegram, turns=[self.turn()])
        card_id = self.delivery.status_card.load()['message_id']
        self.delivery.seal(self.turn('completed'))
        final = self.delivery.next_row()
        self.now += 15
        await self.delivery.update(self.telegram, turns=[self.turn('completed')])
        self.assertIn('正在发送', self.card_text(self.edits()[-1]))
        before_ack = len(self.calls)
        await self.delivery.complete_status(self.telegram, final)
        self.assertEqual(len(self.calls), before_ack)
        self.delivery.begin(final)
        self.mark_sent(final)
        await self.delivery.complete_status(self.telegram, final)
        self.assertEqual(self.edits()[-1]['message_id'], card_id)
        self.assertIn('已完成', self.card_text(self.edits()[-1]))
        self.assertNotIn('正在发送', self.card_text(self.edits()[-1]))
        self.assertEqual(len(self.creates()), 1)

    async def test_failed_and_stopped_turns_edit_the_same_card_with_distinct_labels(self):
        for tid, status, label in (('failed-turn', 'failed', '执行失败'),
                                   ('stopped-turn', 'interrupted', '已停止')):
            with self.subTest(status=status):
                self.now += 15
                active = self.turn(tid=tid, started=self.now)
                self.output('终态前保留的正文', f'turn:{tid}:body')
                await self.delivery.update(self.telegram, turns=[active])
                self.delivery.seal(self.turn(status, tid=tid, started=self.now))
                final = self.delivery.next_row()
                self.delivery.begin(final)
                self.mark_sent(final)
                await self.delivery.complete_status(self.telegram, final)
                self.assertIn(label, self.card_text(self.edits()[-1]))
                self.assertNotIn('正在发送', self.card_text(self.edits()[-1]))
        self.assertEqual(len(self.creates()), 1)
        card_id = self.delivery.status_card.load()['message_id']
        self.assertTrue(all(params['message_id'] == card_id for params in self.edits()))

    async def test_rate_limited_terminal_card_edit_retries_without_replaying_final_output(self):
        self.output('已经发送完成的正文')
        await self.delivery.update(self.telegram, turns=[self.turn()])
        self.delivery.seal(self.turn('completed'))
        final = self.delivery.next_row()
        self.delivery.begin(final)
        self.mark_sent(final)
        self.failures['editMessageText'] = [fixtures.ApiError(429, retry_after=20)]
        await self.delivery.complete_status(self.telegram, final)
        self.assertEqual(len(self.edits()), 1)
        self.assertIsNone(self.delivery.next_row())
        self.now += 15
        await self.delivery.update(self.telegram, turns=[self.turn('completed')])
        self.assertEqual(len(self.edits()), 1)
        self.now += 5
        await self.delivery.update(self.telegram, turns=[self.turn('completed')])
        self.assertEqual(len(self.edits()), 2)
        self.assertIn('已完成', self.card_text(self.edits()[-1]))
        self.assertEqual(len(self.creates()), 1)
        self.assertIsNone(self.delivery.next_row())

    async def test_old_completion_cannot_overwrite_a_new_pending_receipt(self):
        self.accept('任务A', 1)
        self.store.mark(1, 'running', 'work')
        self.output('任务A正文')
        await self.delivery.update(self.telegram, turns=[self.turn()])
        self.delivery.seal(self.turn('completed'))
        final = self.delivery.next_row()
        self.store.mark(1, 'done')
        self.now += 1
        self.accept('任务B', 2)
        await self.delivery.receipt(self.telegram)
        card_before = copy.deepcopy(self.delivery.status_card.load())
        calls_before = len(self.calls)
        self.delivery.begin(final)
        self.mark_sent(final)
        await self.delivery.complete_status(self.telegram, final)
        self.assertEqual(len(self.calls), calls_before)
        self.assertEqual(self.delivery.status_card.load()['message_id'], card_before['message_id'])
        self.assertIn('已收到', self.card_text(self.edits()[-1]))

    async def test_owner_revocation_during_creation_prevents_the_body_draft(self):
        async def revoke_after_creation(method, params):
            if method in ('sendMessage', 'sendRichMessage'):
                self.bridge.owner_allowed = lambda: False
        self.after_call = revoke_after_creation
        self.output('撤权后不能发送的正文')
        await self.delivery.update(self.telegram, turns=[self.turn()])
        self.assertEqual(len(self.creates()), 1)
        self.assertEqual(self.drafts(), [])
        self.now += 15
        await self.delivery.update(self.telegram, turns=[self.turn()])
        self.assertEqual(len(self.calls), 1)

    async def test_repaired_topic_never_edits_the_old_status_card_or_leaks_old_body(self):
        self.output('原话题的正文')
        await self.delivery.update(self.telegram, turns=[self.turn()])
        old_card = self.delivery.status_card.load()['message_id']
        self.bridge.target['message_thread_id'] = 99
        self.now += 15
        self.restart()
        self.output('新话题正文', 'turn:new:body', topic=99)
        await self.delivery.update(self.telegram, turns=[self.turn(tid='new', started=self.now)])
        self.assertEqual(len(self.creates()), 2)
        self.assertEqual(self.creates()[-1][1]['message_thread_id'], 99)
        self.assertTrue(all(params['message_id'] != old_card for params in self.edits()))
        self.assertEqual(self.drafts()[-1]['message_thread_id'], 99)
        self.assertEqual(self.drafts()[-1]['rich_message']['blocks'][0]['text'], '新话题正文')


if __name__ == '__main__':
    unittest.main()
