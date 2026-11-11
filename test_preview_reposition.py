"""An accepted user interaction relocates one cumulative preview, once."""
import copy
import json
import unittest

from completion_delivery import CompletionDelivery
import test_completion_delivery as fixtures
import test_receipt_preview as receipts


class PreviewRepositionTests(unittest.IsolatedAsyncioTestCase):
    asyncTearDown = fixtures.CompletionDeliveryTests.asyncTearDown
    output = fixtures.CompletionDeliveryTests.output
    turn = fixtures.CompletionDeliveryTests.turn
    accept = receipts.ReceiptPreviewTests.accept

    async def asyncSetUp(self):
        await fixtures.CompletionDeliveryTests.asyncSetUp(self)
        self.bridge.config['preview_style'] = 'status-bar'
        self.delivery = CompletionDelivery(self.bridge, clock=lambda: self.now)
        self.sequence, self.messages, self.failures, self.before_ack = 100, {}, {}, None
        test = self

        class Telegram:
            async def call(self, method, **params):
                test.calls.append((method, copy.deepcopy(params)))
                pending = test.failures.get(method, [])
                if pending:
                    raise pending.pop(0)
                if method in ('sendMessage', 'sendRichMessage'):
                    test.sequence += 1
                    result = {'message_id': test.sequence, 'message_thread_id': params['message_thread_id'],
                              'chat': {'id': params['chat_id']}, **copy.deepcopy(params)}
                    test.messages[test.sequence] = result
                    if test.before_ack:
                        await test.before_ack()
                    return copy.deepcopy(result)
                if method in ('editMessageText', 'editMessageReplyMarkup'):
                    result = test.messages[params['message_id']]
                    result.update(copy.deepcopy(params))
                    return copy.deepcopy(result)
                if method == 'deleteMessage':
                    test.messages.pop(params['message_id'], None)
                    return True
                raise AssertionError(method)

        self.telegram = Telegram()

    def creates(self):
        return [(method, params) for method, params in self.calls if method in ('sendMessage', 'sendRichMessage')]

    def state(self):
        return self.delivery.body_preview.load(dict(self.delivery._group('work')))

    async def start(self):
        self.output('第一段完整公开过程')
        await self.delivery.update(self.telegram, turns=[self.turn()])
        return self.state()['message_id']

    def restart(self):
        self.delivery = CompletionDelivery(self.bridge, clock=lambda: self.now)

    async def test_new_interaction_recreates_quietly_with_all_body_then_edits_the_new_id(self):
        old = await self.start()
        self.output('第二段公开过程', 'turn:work:second')
        self.assertTrue(self.delivery.note_interaction('telegram:2', chat_id=123, topic_id=42))
        await self.delivery.update(self.telegram, turns=[self.turn()])
        new = self.state()['message_id']
        self.assertGreater(new, old)
        self.assertEqual(len(self.creates()), 2)
        _, params = self.creates()[-1]
        self.assertTrue(params['disable_notification'])
        self.assertEqual((params['chat_id'], params['message_thread_id']), (123, 42))
        self.assertIn('第一段完整公开过程', json.dumps(params, ensure_ascii=False))
        self.assertIn('第二段公开过程', json.dumps(params, ensure_ascii=False))
        self.assertNotIn(old, self.messages)
        self.assertEqual(self.calls[-1], ('deleteMessage', {'chat_id': 123, 'message_id': old}))
        self.assertEqual(self.state()['interaction_sequence'], self.delivery.interaction_sequence())
        self.now += 15
        self.output('第三段公开过程', 'turn:work:third')
        await self.delivery.update(self.telegram, turns=[self.turn()])
        self.assertEqual(len(self.creates()), 2)
        self.assertEqual(self.calls[-1][1]['message_id'], new)
        self.assertIn('⏹ 停止当前任务', json.dumps(self.state()['sent_markup'], ensure_ascii=False))

    async def test_initial_creation_and_coalesced_events_do_not_make_extra_previews(self):
        self.delivery.note_interaction('telegram:initial')
        first = await self.start()
        self.assertEqual(len(self.creates()), 1)
        self.delivery.note_interaction('telegram:second')
        self.delivery.note_interaction('choice:group:0')
        await self.delivery.update(self.telegram, turns=[self.turn()])
        self.assertEqual(len(self.creates()), 2)
        self.assertNotEqual(first, self.state()['message_id'])
        self.assertFalse(self.delivery.note_interaction('choice:group:0'))
        self.restart()
        self.now += 15
        await self.delivery.update(self.telegram, turns=[self.turn()])
        self.assertEqual(len(self.creates()), 2)

    async def test_send_snapshot_does_not_consume_an_interaction_arriving_before_ack(self):
        await self.start()
        self.delivery.note_interaction('telegram:second')
        original_sequence = self.delivery.interaction_sequence()

        async def newer_input():
            self.before_ack = None
            self.delivery.note_interaction('telegram:during-send')

        self.before_ack = newer_input
        await self.delivery.update(self.telegram, turns=[self.turn()])
        self.assertEqual(self.state()['interaction_sequence'], original_sequence)
        self.assertGreater(self.delivery.interaction_sequence(), original_sequence)
        await self.delivery.update(self.telegram, turns=[self.turn()])
        self.assertEqual(len(self.creates()), 3)
        self.assertEqual(self.state()['interaction_sequence'], self.delivery.interaction_sequence())

    async def test_known_rate_limit_keeps_old_preview_until_one_retry_ack(self):
        old = await self.start()
        self.failures['sendRichMessage'] = [fixtures.ApiError(429, retry_after=40)]
        self.delivery.note_interaction('telegram:second')
        await self.delivery.update(self.telegram, turns=[self.turn()])
        self.assertEqual(self.state()['message_id'], old)
        self.assertIn(old, self.messages)
        self.assertFalse(any(method == 'deleteMessage' for method, _ in self.calls))
        self.now += 20
        self.delivery.note_interaction('telegram:during-cooldown')
        await self.delivery.update(self.telegram, turns=[self.turn()])
        self.assertEqual(len(self.creates()), 2)
        self.now += 20
        await self.delivery.update(self.telegram, turns=[self.turn()])
        self.assertEqual(len(self.creates()), 3)
        self.assertNotEqual(self.state()['message_id'], old)
        self.assertNotIn(old, self.messages)

    async def test_unknown_replacement_result_cannot_be_recreated_after_restart_or_new_input(self):
        old = await self.start()
        self.failures['sendRichMessage'] = [ConnectionError('unknown send result')]
        self.delivery.note_interaction('telegram:second')
        await self.delivery.update(self.telegram, turns=[self.turn()])
        self.assertEqual(self.state()['replacement']['state'], 'uncertain')
        self.assertEqual(self.state()['message_id'], old)
        self.restart()
        self.now += 30
        self.delivery.note_interaction('telegram:third')
        self.output('仍在保存的新内容', 'turn:work:second')
        await self.delivery.update(self.telegram, turns=[self.turn()])
        self.assertEqual(len(self.creates()), 2)
        self.assertEqual(self.calls[-1][1]['message_id'], old)
        self.assertIn('仍在保存的新内容', json.dumps(self.calls[-1], ensure_ascii=False))
        self.assertFalse(any(method == 'deleteMessage' for method, _ in self.calls))

    async def test_crash_creation_intent_is_not_retried(self):
        old = await self.start()
        self.delivery.note_interaction('telegram:second')
        state = self.state()
        state['replacement'] = {'state': 'creating', 'generation': self.delivery.interaction_sequence(),
                                'old_message_id': old, 'retry_at': 0}
        self.delivery.body_preview._save(state, dict(self.delivery._group('work')))
        self.restart()
        await self.delivery.update(self.telegram, turns=[self.turn()])
        self.assertEqual(len(self.creates()), 1)
        self.assertEqual(self.state()['replacement']['state'], 'uncertain')

    async def test_retired_cleanup_retries_without_recreating_and_never_deletes_canonical_output(self):
        old = await self.start()
        self.failures['deleteMessage'] = [fixtures.ApiError(429, retry_after=40)]
        self.delivery.note_interaction('telegram:second')
        await self.delivery.update(self.telegram, turns=[self.turn()])
        new = self.state()['message_id']
        self.assertEqual(self.state()['retired'][0]['state'], 'queued')
        self.restart()
        self.now += 40
        await self.delivery.body_preview.cleanup(self.telegram)
        self.assertEqual(self.state()['retired'][0]['state'], 'deleted')
        self.assertIn(new, self.messages)
        self.assertNotIn(old, self.messages)
        self.assertEqual(len(self.creates()), 2)

        self.store.enqueue_at('canonical:protection', '123', '正式正文', 42)
        with self.store.db:
            self.store.db.execute("UPDATE outbox SET message_id=999 WHERE key LIKE 'canonical:protection:%'")
        state = self.state()
        state['retired'].append({'message_id': 999, 'state': 'queued', 'retry_at': 0})
        self.delivery.body_preview._save(state, dict(self.delivery._group('work')))
        await self.delivery.body_preview.cleanup(self.telegram)
        self.assertFalse(any(method == 'deleteMessage' and params['message_id'] == 999 for method, params in self.calls))
        self.assertEqual(self.state()['retired'][-1]['state'], 'failed')

    async def test_foreign_routes_disabled_modes_and_finished_previews_do_not_move(self):
        await self.start()
        for args in ({'chat_id': 999}, {'topic_id': 99}):
            self.assertFalse(self.delivery.note_interaction('foreign', **args))
        self.assertEqual(self.delivery.interaction_sequence(), 0)
        self.bridge.config['notification_mode'] = 'all'
        self.restart()
        self.assertFalse(self.delivery.note_interaction('disabled'))
        self.bridge.config['notification_mode'] = 'completion'
        self.restart()
        identity = dict(self.delivery._group('work'))
        self.delivery.body_preview.finish(identity)
        self.delivery.note_interaction('after-final')
        await self.delivery.update(self.telegram, turns=[self.turn()])
        self.assertEqual(len(self.creates()), 1)

    async def test_source_to_turn_adoption_preserves_known_replacement_and_watermark(self):
        self.accept('首条任务', source_id=1)
        self.delivery.note_interaction('telegram:1')
        await self.delivery.receipt(self.telegram)
        first_id = self.sequence
        self.now += 1
        self.accept('中途补充', source_id=2)
        self.delivery.note_interaction('telegram:2')
        await self.delivery.receipt(self.telegram)
        self.assertEqual(len(self.creates()), 2)
        self.store.mark(2, 'running', 'work')
        self.output()
        await self.delivery.update(self.telegram, turns=[self.turn()])
        self.assertEqual(len(self.creates()), 2)
        self.assertEqual(self.state()['message_id'], self.sequence)
        self.assertEqual(self.state()['interaction_sequence'], self.delivery.interaction_sequence())
        self.assertTrue(self.state()['retired'])
        self.assertNotIn(first_id, self.messages)


if __name__ == '__main__':
    unittest.main()
