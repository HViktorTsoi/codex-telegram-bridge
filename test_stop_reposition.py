"""A relocated preview cannot leave an old actionable Stop button behind."""
import asyncio
import copy
import time
import unittest

import test_status_controls as fixtures


class StopRepositionTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.StatusControlTests.asyncSetUp
    asyncTearDown = fixtures.StatusControlTests.asyncTearDown
    allow_owner = fixtures.StatusControlTests.allow_owner
    bar = fixtures.StatusControlTests.bar
    callback = fixtures.StatusControlTests.callback
    click = fixtures.StatusControlTests.click
    interruptions = fixtures.StatusControlTests.interruptions
    ledger = fixtures.StatusControlTests.ledger

    async def relocate(self, name='a', marker='new-user-reply'):
        bridge, native = self.router.bridges[name], self.natives[name]
        bridge.delivery.note_interaction(marker)
        turn = {'id': native.latest, 'status': 'inProgress', 'startedAt': time.time() - 1}
        await bridge.delivery.update(self.telegram, turns=[turn])
        identity = dict(bridge.delivery._group(turn['id']))
        state = bridge.delivery.body_preview.load(identity)
        return copy.deepcopy(self.messages[state['message_id']])

    async def test_old_stop_is_invalid_after_reposition_and_new_stop_targets_the_same_turn(self):
        old = await self.bar()
        new = await self.relocate()
        self.assertNotEqual(new['message_id'], old['message_id'])
        self.assertEqual(self.callback(old)['callback_query']['data'],
                         self.callback(new)['callback_query']['data'])
        self.assertTrue(new['disable_notification'])
        await self.click(old)
        self.assertEqual(self.interruptions(), [])
        await self.click(new)
        self.assertEqual(self.interruptions(), [('a', 'turn/interrupt', {
            'threadId': 'native-a', 'turnId': 'a-work',
        })])
        self.assertEqual(self.ledger(new)['state'], 'requested')
        self.assertFalse(any(name == 'b' for name, _, _ in self.native_calls))

    async def test_reposition_during_old_stop_validation_cannot_interrupt_via_the_old_message(self):
        old = await self.bar()
        self.natives['a'].query_gate = asyncio.Event()
        self.router.ingest(self.callback(old))
        worker = asyncio.create_task(self.router.status_controls.handle_one())
        try:
            await asyncio.wait_for(self.natives['a'].query_started.wait(), 1)
            new = await self.relocate(marker='reply-during-stop-check')
            self.assertNotEqual(new['message_id'], old['message_id'])
        finally:
            self.natives['a'].query_gate.set()
            await worker
        self.assertEqual(self.interruptions(), [])
        await self.click(new)
        self.assertEqual(self.interruptions(), [('a', 'turn/interrupt', {
            'threadId': 'native-a', 'turnId': 'a-work',
        })])


if __name__ == '__main__':
    unittest.main()
