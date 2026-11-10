"""Status controls belong only to the displayed active native task."""
import json
import unittest

from completion_preview import STOP_REQUEST_KEY
import test_persistent_status as harness


class StopButtonMarkupTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = harness.PersistentStatusTests.asyncSetUp
    asyncTearDown = harness.PersistentStatusTests.asyncTearDown
    restart = harness.PersistentStatusTests.restart
    delivery = harness.PersistentStatusTests.delivery
    turn = harness.PersistentStatusTests.turn
    accept = harness.PersistentStatusTests.accept
    pending_identity = harness.PersistentStatusTests.pending_identity
    identity = harness.PersistentStatusTests.identity
    output = harness.PersistentStatusTests.output
    update = harness.PersistentStatusTests.update
    calls_for = harness.PersistentStatusTests.calls_for
    sends = harness.PersistentStatusTests.sends
    drain = harness.PersistentStatusTests.drain

    @staticmethod
    def controls(message):
        return [button for row in message['reply_markup']['inline_keyboard']
                for button in row if 'callback_data' in button]

    def preview(self):
        return self.delivery().body_preview

    def message(self):
        return self.messages[self.preview().load(self.identity())['message_id']]

    async def test_active_task_exposes_one_stop_beside_the_second_status_row(self):
        self.output()
        await self.update()
        rows = self.message()['reply_markup']['inline_keyboard']
        self.assertEqual([len(row) for row in rows], [1, 2])
        self.assertTrue(all(row[0].get('disabled') == {} for row in rows))
        button = self.controls(self.message())[0]
        self.assertEqual(button['text'], '⏹ 停止当前任务')
        self.assertNotIn('disabled', button)
        expected = 'stop:' + self.preview()._key(self.identity())[-24:]
        self.assertEqual(button['callback_data'], expected)
        self.assertEqual(len(button['callback_data'].encode()), 29)

    async def test_receipt_adopts_real_turn_before_exposing_a_control_on_the_same_message(self):
        self.accept()
        await self.delivery().receipt(self.telegram)
        pending = self.pending_identity()
        mid = self.preview().load(pending)['message_id']
        self.assertEqual(self.controls(self.messages[mid]), [])
        self.assertIsNone(self.preview().stop_callback(pending))
        pending_key = self.preview()._key(pending)
        self.bridges['a'].store.mark(7, 'running', 'a-work')
        self.output()
        self.now += 1
        await self.update()
        self.assertEqual(self.preview().load(self.identity())['message_id'], mid)
        button = self.controls(self.messages[mid])[0]
        self.assertEqual(button['callback_data'], self.preview().stop_callback(self.identity()))
        self.assertNotEqual(button['callback_data'], 'stop:' + pending_key[-24:])
        self.assertEqual(len(self.sends()), 1)

    async def test_body_and_heartbeat_edits_preserve_the_exact_task_callback(self):
        self.output()
        await self.update()
        mid = self.message()['message_id']
        callback = self.controls(self.message())[0]['callback_data']
        self.output('下一段过程', iid='next')
        self.now += 15
        await self.update()
        self.assertEqual(self.message()['message_id'], mid)
        self.assertEqual(self.controls(self.message())[0]['callback_data'], callback)
        self.now += 15
        self.restart()
        await self.update()
        self.assertEqual(self.message()['message_id'], mid)
        self.assertEqual(self.controls(self.message())[0]['callback_data'], callback)
        self.assertEqual(len(self.sends()), 1)

    async def test_terminal_preview_and_final_reply_do_not_offer_stop(self):
        self.output()
        await self.update()
        self.assertEqual(len(self.controls(self.message())), 1)
        self.now += 15
        await self.update(status='completed')
        self.assertEqual(self.controls(self.message()), [])
        self.assertIsNone(self.preview().stop_callback(self.identity()))
        self.delivery().seal(self.turn(status='completed'))
        await self.drain()
        final = next(iter(self.messages.values()))
        self.assertEqual(self.controls(final), [])
        self.assertEqual([len(row) for row in final['reply_markup']['inline_keyboard']], [1, 1])

    async def test_durable_stop_attempt_hides_control_without_changing_body_or_message(self):
        self.output()
        await self.update()
        mid = self.message()['message_id']
        body = harness.PersistentStatusTests.body_text(self.message())
        digest = self.preview()._key(self.identity())[-24:]
        store = self.bridges['a'].store
        for state in ('sending', 'requested', 'uncertain'):
            with self.subTest(state=state):
                store.set(STOP_REQUEST_KEY + digest, json.dumps({'state': state}))
                self.restart()
                self.assertIsNone(self.preview().stop_callback(self.identity()))
                self.now += 15
                await self.update()
                self.assertEqual(self.message()['message_id'], mid)
                self.assertEqual(self.controls(self.message()), [])
                self.assertEqual(harness.PersistentStatusTests.body_text(self.message()), body)
        self.assertEqual(len(self.sends()), 1)

    async def test_old_or_mismatched_route_never_exposes_a_stop_control(self):
        self.output()
        await self.update()
        old_identity = self.identity()
        self.assertIsNotNone(self.preview().stop_callback(old_identity))
        for wrong in (dict(old_identity, native_id='other-native'),
                      dict(old_identity, chat_id='999'), dict(old_identity, topic_id=84)):
            self.assertIsNone(self.preview().stop_callback(wrong))
        self.now += 15
        self.output('新任务', tid='a-next')
        await self.update(tid='a-next', started=self.now)
        self.assertIsNone(self.preview().stop_callback(old_identity))
        self.assertIsNotNone(self.preview().stop_callback(self.identity('a-next')))


if __name__ == '__main__':
    unittest.main()
