"""Concurrent topic status messages and optional pins must stay isolated."""
import asyncio
import copy
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from completion_delivery import CompletionDelivery
from topics_bridge import RoutedStore
from test_completion_delivery import ApiError


class ParallelStatusTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.now = 1000
        self.bridges, self.deliveries = {}, {}
        self.calls, self.messages = [], {}
        self.sequence, self.after_call = 100, None
        self.failures = {}
        for name, topic in (('a', 42), ('b', 84)):
            store = RoutedStore(Path(self.tmp.name) / (name + '.sqlite'), 'native-' + name)
            bridge = SimpleNamespace(
                store=store, thread_id='native-' + name, owner='123', owner_allowed=lambda: True,
                target={'key': name, 'thread_id': 'native-' + name, 'message_thread_id': topic},
                config={'notification_mode': 'completion', 'preview_style': 'status-card'})
            self.bridges[name] = bridge
            self.restart(name)
        test = self

        class Telegram:
            async def call(self, method, **params):
                test.calls.append((method, copy.deepcopy(params)))
                errors = test.failures.get(method, [])
                if errors:
                    raise errors.pop(0)
                if test.after_call:
                    await test.after_call(method, params)
                if method in ('sendMessageDraft', 'sendRichMessageDraft', 'pinChatMessage'):
                    return True
                if method == 'sendMessage':
                    test.sequence += 1
                    mid = test.sequence
                    test.messages[mid] = {'message_id': mid, 'message_thread_id': params['message_thread_id']}
                    return copy.deepcopy(test.messages[mid])
                if method == 'editMessageText':
                    return copy.deepcopy(test.messages[params['message_id']])
                raise AssertionError('Status UI must not call any model or unknown method: ' + method)

        self.telegram = Telegram()

    async def asyncTearDown(self):
        for bridge in self.bridges.values():
            bridge.store.db.close()
        self.tmp.cleanup()

    def restart(self, name):
        self.deliveries[name] = CompletionDelivery(self.bridges[name], clock=lambda: self.now)

    def enable_pin(self, name):
        self.bridges[name].config['status_card_pin'] = True
        self.restart(name)

    def turn(self, name, status='inProgress', tid=None):
        return {'id': tid or name + '-work', 'status': status, 'startedAt': 1001}

    def output(self, name, text=None, tid=None):
        bridge = self.bridges[name]
        bridge.store.enqueue_at('turn:' + (tid or name + '-work') + ':body', '123',
                                text or name.upper() + ' 独立正文', bridge.target['message_thread_id'])

    async def update(self, name, status='inProgress', tid=None):
        await self.deliveries[name].update(self.telegram, turns=[self.turn(name, status, tid)])

    def state(self, name):
        return self.deliveries[name].status_card.load()

    def methods(self, method):
        return [params for called, params in self.calls if called == method]

    async def test_two_simultaneous_topics_have_distinct_cards_and_body_drafts(self):
        self.output('a')
        self.output('b')
        await asyncio.gather(self.update('a'), self.update('b'))
        a, b = self.state('a')['message_id'], self.state('b')['message_id']
        self.assertNotEqual(a, b)
        self.assertEqual({p['message_thread_id'] for p in self.methods('sendMessage')}, {42, 84})
        first_drafts = {p['message_thread_id']: p['draft_id'] for p in self.methods('sendRichMessageDraft')}
        self.now += 15
        await asyncio.gather(self.update('a'), self.update('b'))
        self.assertEqual({p['message_id'] for p in self.methods('editMessageText')}, {a, b})
        self.assertEqual(len(self.methods('sendMessage')), 2)
        for draft in self.methods('sendRichMessageDraft'):
            name = 'a' if draft['message_thread_id'] == 42 else 'b'
            self.assertEqual(draft['draft_id'], first_drafts[draft['message_thread_id']])
            self.assertEqual(draft['rich_message']['blocks'], [{'type': 'thinking', 'text': name.upper() + ' 独立正文'}])
        self.assertFalse(self.methods('pinChatMessage'), 'Pinning is opt-in')

    async def test_a_blocked_status_edit_does_not_block_the_other_topics_heartbeat(self):
        self.output('a')
        self.output('b')
        await asyncio.gather(self.update('a'), self.update('b'))
        a, b = self.state('a')['message_id'], self.state('b')['message_id']
        entered, release = asyncio.Event(), asyncio.Event()

        async def block_a(method, params):
            if method == 'editMessageText' and params['message_id'] == a:
                entered.set()
                await release.wait()

        self.after_call = block_a
        self.now += 15
        task = asyncio.create_task(self.update('a'))
        try:
            await asyncio.wait_for(entered.wait(), 0.5)
            await asyncio.wait_for(self.update('b'), 0.5)
            self.assertFalse(task.done())
            self.assertEqual(self.state('b')['updated_at'], self.now)
            self.assertIn(b, [p['message_id'] for p in self.methods('editMessageText')])
            self.assertEqual(self.methods('sendRichMessageDraft')[-1]['message_thread_id'], 84)
        finally:
            release.set()
            await task

    async def test_completing_one_topic_never_finishes_the_other_card(self):
        self.output('a')
        self.output('b')
        await asyncio.gather(self.update('a'), self.update('b'))
        b_before = copy.deepcopy(self.state('b'))
        delivery = self.deliveries['a']
        delivery.seal(self.turn('a', 'completed'))
        final = delivery.next_row()
        delivery.begin(final)
        self.bridges['a'].store.images.sent(final, 500)
        delivery.sent(final, 500)
        await delivery.complete_status(self.telegram, final)
        self.assertTrue(self.state('a')['terminal'])
        self.assertIn('已完成', self.state('a')['sent_text'])
        self.assertEqual(self.state('b'), b_before)
        self.assertFalse(self.state('b')['terminal'])

    async def test_each_topics_own_card_is_pinned_quietly_before_its_body_draft(self):
        for name in ('a', 'b'):
            self.enable_pin(name)
            self.output(name)
        await asyncio.gather(self.update('a'), self.update('b'))
        pinned = self.methods('pinChatMessage')
        self.assertEqual(len(pinned), 2)
        self.assertEqual({p['message_id'] for p in pinned}, {self.state('a')['message_id'], self.state('b')['message_id']})
        self.assertTrue(all(p['disable_notification'] is True and str(p['chat_id']) == '123' for p in pinned))
        for name, topic in (('a', 42), ('b', 84)):
            mid = self.state(name)['message_id']
            pin_index = next(i for i, (method, p) in enumerate(self.calls) if method == 'pinChatMessage' and p['message_id'] == mid)
            draft_index = next(i for i, (method, p) in enumerate(self.calls) if method == 'sendRichMessageDraft' and p['message_thread_id'] == topic)
            self.assertEqual(self.messages[mid]['message_thread_id'], topic)
            self.assertLess(pin_index, draft_index)

    async def test_refresh_restart_and_next_turn_do_not_pin_or_create_again(self):
        self.enable_pin('a')
        self.output('a')
        await self.update('a')
        mid = self.state('a')['message_id']
        self.now += 15
        await self.update('a')
        self.restart('a')
        self.now += 15
        await self.update('a')
        delivery = self.deliveries['a']
        delivery.seal(self.turn('a', 'completed'))
        final = delivery.next_row()
        delivery.begin(final)
        self.bridges['a'].store.images.sent(final, 500)
        delivery.sent(final, 500)
        await delivery.complete_status(self.telegram, final)
        self.output('a', '下一轮正文', tid='a-next')
        self.now += 15
        await self.update('a', tid='a-next')
        self.assertEqual(len(self.methods('sendMessage')), 1)
        self.assertEqual(len(self.methods('pinChatMessage')), 1)
        self.assertEqual(self.state('a')['message_id'], mid)
        self.assertEqual(self.methods('sendRichMessageDraft')[-1]['rich_message']['blocks'][0]['text'], '下一轮正文')

    async def test_target_override_disables_only_its_own_pin(self):
        for name in ('a', 'b'):
            self.enable_pin(name)
            self.output(name)
        self.bridges['a'].target['status_card_pin'] = False
        await asyncio.gather(self.update('a'), self.update('b'))
        self.assertEqual([p['message_id'] for p in self.methods('pinChatMessage')], [self.state('b')['message_id']])
        self.assertEqual(len(self.methods('sendRichMessageDraft')), 2)

    async def test_pin_rate_limit_does_not_delay_card_edits_or_body_drafts(self):
        self.enable_pin('a')
        self.output('a')
        self.failures['pinChatMessage'] = [ApiError(429, retry_after=30)]
        await self.update('a')
        self.assertEqual(len(self.methods('pinChatMessage')), 1)
        self.assertEqual(len(self.methods('sendRichMessageDraft')), 1)
        self.assertEqual(self.state('a').get('retry_at', 0), 0)
        self.now += 15
        await self.update('a')
        self.assertEqual(len(self.methods('pinChatMessage')), 1)
        self.assertTrue(self.methods('editMessageText'))
        self.assertEqual(len(self.methods('sendRichMessageDraft')), 2)
        self.now += 15
        await self.update('a')
        self.assertEqual(len(self.methods('pinChatMessage')), 2)
        self.assertEqual(len(self.methods('sendMessage')), 1)
        self.assertEqual(len(self.methods('sendRichMessageDraft')), 3)

    async def test_unknown_pin_acknowledgement_is_not_replayed_after_restart(self):
        self.enable_pin('a')
        self.output('a')
        self.failures['pinChatMessage'] = [ConnectionError('unknown pin acknowledgement')]
        await self.update('a')
        self.assertEqual(len(self.methods('sendRichMessageDraft')), 1)
        self.restart('a')
        self.now += 15
        await self.update('a')
        self.assertEqual(len(self.methods('pinChatMessage')), 1)
        self.assertEqual(len(self.methods('sendMessage')), 1)
        self.assertEqual(len(self.methods('sendRichMessageDraft')), 2)

    async def test_crash_during_pin_is_uncertain_without_automatic_repin(self):
        self.enable_pin('a')
        self.output('a')
        await self.update('a')
        state = self.state('a')
        state.update(pin_state='sending', pin_message_id=state['message_id'])
        self.deliveries['a'].status_card.save(state)
        self.restart('a')
        self.now += 15
        await self.update('a')
        self.assertEqual(len(self.methods('pinChatMessage')), 1)
        self.assertEqual(self.state('a')['pin_state'], 'uncertain')
        self.assertEqual(len(self.methods('sendRichMessageDraft')), 2)

    async def test_failed_pin_does_not_block_either_topics_body(self):
        for name in ('a', 'b'):
            self.enable_pin(name)
            self.output(name)
        self.failures['pinChatMessage'] = [ApiError(403)]
        await self.update('a')
        await self.update('b')
        self.now += 15
        await asyncio.gather(self.update('a'), self.update('b'))
        self.assertEqual(len(self.methods('pinChatMessage')), 2)
        self.assertEqual({p['message_thread_id'] for p in self.methods('sendRichMessageDraft')}, {42, 84})
        self.assertEqual(len(self.methods('sendRichMessageDraft')), 4)
        self.assertEqual(len(self.methods('sendMessage')), 2)

    async def test_owner_revocation_after_card_creation_prevents_pin_and_body(self):
        self.enable_pin('a')
        self.output('a')

        async def revoke(method, params):
            if method == 'sendMessage':
                self.bridges['a'].owner_allowed = lambda: False

        self.after_call = revoke
        await self.update('a')
        self.assertEqual(len(self.methods('sendMessage')), 1)
        self.assertEqual(self.methods('pinChatMessage'), [])
        self.assertEqual(self.methods('sendRichMessageDraft'), [])

    async def test_repaired_topic_never_pins_the_old_status_message(self):
        self.output('a')
        await self.update('a')
        old_card = self.state('a')['message_id']
        self.bridges['a'].target['message_thread_id'] = 99
        self.enable_pin('a')
        self.now += 15
        self.output('a', '修复后新话题正文', tid='a-repaired')
        await self.update('a', tid='a-repaired')
        pins = self.methods('pinChatMessage')
        self.assertEqual(len(pins), 1)
        self.assertNotEqual(pins[0]['message_id'], old_card)
        self.assertEqual(self.messages[pins[0]['message_id']]['message_thread_id'], 99)
        self.assertEqual(self.methods('sendRichMessageDraft')[-1]['message_thread_id'], 99)


if __name__ == '__main__':
    unittest.main()
