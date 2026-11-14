"""Pending cancellation remains available while a native connection is down."""
import asyncio
import copy
import json
import time
import unittest

import test_status_controls as fixtures


class PendingStopTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.StatusControlTests.asyncSetUp
    asyncTearDown = fixtures.StatusControlTests.asyncTearDown
    allow_owner = fixtures.StatusControlTests.allow_owner
    bar = fixtures.StatusControlTests.bar
    callback = fixtures.StatusControlTests.callback
    interruptions = fixtures.StatusControlTests.interruptions

    def accept(self, uid, text='do work', name='a', stamp=None, **payload):
        bridge = self.router.bridges[name]
        message = {'message_id': uid, 'date': 1, 'from': {'id': 123},
                   'chat': {'id': 123, 'type': 'private'}, 'message_thread_id': bridge.target['message_thread_id'],
                   'text': text, **payload}
        bridge.store.accept({'update_id': uid}, message)
        if stamp is not None:
            with bridge.store.db:
                bridge.store.db.execute('UPDATE inbox SET received_ms=? WHERE id=?', (stamp, uid))
        return message

    def command(self, uid=100, name='a', **changes):
        topic = self.router.bridges[name].target['message_thread_id']
        message = {'message_id': uid, 'date': 1, 'from': {'id': 123}, 'text': '/stop',
                   'chat': {'id': 123, 'type': 'private'}, 'message_thread_id': topic, **changes}
        return {'update_id': uid, 'message': message}

    def state(self, uid, name='a'):
        row = self.router.bridges[name].store.db.execute('SELECT state FROM inbox WHERE id=?', (uid,)).fetchone()
        return row[0] if row else None

    def notices(self, name='a'):
        return self.router.bridges[name].store.db.execute(
            "SELECT * FROM outbox WHERE key LIKE 'stop-control-result:%' ORDER BY rowid").fetchall()

    async def process(self, update):
        self.router.ingest(update)
        await self.router.status_controls.handle_one()

    async def pending_bar(self, uid=10):
        self.accept(uid)
        bridge = self.router.bridges['a']
        bridge.ready.clear()
        await bridge.delivery.receipt(self.telegram, connected=False)
        message = copy.deepcopy(self.messages[max(self.messages)])
        data = next(b['callback_data'] for row in message['reply_markup']['inline_keyboard']
                    for b in row if b.get('callback_data', '').startswith('cancel:'))
        return message, data

    async def test_offline_stop_cancels_only_frozen_pending_tasks_without_waiting_for_lock(self):
        bridge = self.router.bridges['a']
        bridge.ready.clear()
        self.accept(10)
        self.accept(11, text='', photo=[{'file_id': 'photo', 'width': 10, 'height': 10}])
        self.accept(12, name='b')
        self.accept(13, text='/status')
        update = self.command()
        self.router.ingest(update)
        self.accept(200, text='new after stop')
        async with bridge.dispatch_lock:
            await asyncio.wait_for(self.router.status_controls.handle_one(), 1)
        self.assertEqual([self.state(uid) for uid in (10, 11, 13, 200)], ['cancelled', 'cancelled', 'pending', 'pending'])
        self.assertEqual(self.state(12, 'b'), 'pending')
        self.assertEqual(self.native_calls, [])
        self.assertIn('2 条', self.notices()[0]['text'])
        self.assertIn('未连接', self.notices()[0]['text'])
        self.assertEqual(self.notices()[0]['message_thread_id'], 42)
        self.assertIsNone(self.state(100))

    async def test_unforwarded_router_tasks_are_cancelled_in_the_priority_lane(self):
        bridge = self.router.bridges['a']
        bridge.ready.clear()
        message = self.accept(10)
        with bridge.store.db:
            bridge.store.db.execute('DELETE FROM inbox WHERE id=10')
        self.router.store.accept({'update_id': 10}, message, bridge.target)
        await self.process(self.command())
        self.assertEqual(self.router.store.db.execute('SELECT state FROM jobs WHERE id=10').fetchone()[0], 'cancelled')
        self.assertIsNone(self.router.store.pending())
        self.assertIsNone(self.state(10))
        self.assertEqual(self.native_calls, [])

    async def test_pending_bar_cancel_is_local_and_retires_the_pending_button(self):
        message, data = await self.pending_bar()
        await self.process(self.callback(message, data))
        self.assertEqual(self.state(10), 'cancelled')
        shown = self.messages[message['message_id']]
        labels = [b['text'] for row in shown['reply_markup']['inline_keyboard'] for b in row]
        self.assertTrue(any('已取消待处理' in label for label in labels))
        self.assertFalse(any(b.get('callback_data', '').startswith('cancel:')
                             for row in shown['reply_markup']['inline_keyboard'] for b in row))
        self.assertEqual(self.native_calls, [])
        self.assertEqual(len(self.notices()), 1)

    async def test_old_source_cancel_cannot_cancel_later_input_or_an_active_turn(self):
        message, data = await self.pending_bar()
        self.accept(20, text='later')
        await self.process(self.callback(message, data))
        self.assertEqual(self.state(10), 'pending')
        self.assertEqual(self.state(20), 'pending')
        self.assertEqual(self.native_calls, [])
        self.assertTrue(any('失效' in p.get('text', '') for m, p in self.calls if m == 'answerCallbackQuery'))

    async def test_source_cutoff_uses_original_receipt_before_delayed_forward(self):
        message, data = await self.pending_bar()
        bridge = self.router.bridges['a']
        source = dict(bridge.store.db.execute('SELECT * FROM inbox WHERE id=10').fetchone())
        original = {'message_id': 10, 'date': 1, 'from': {'id': 123},
                    'chat': {'id': 123, 'type': 'private'}, 'message_thread_id': 42, 'text': 'original'}
        later = dict(original, message_id=20, text='later original input')
        self.router.store.accept({'update_id': 10}, original, bridge.target)
        self.router.store.accept({'update_id': 20}, later, bridge.target)
        with self.router.store.db:
            self.router.store.db.execute("UPDATE jobs SET state='done',received_ms=? WHERE id=10", (source['received_ms'] - 2000,))
            self.router.store.db.execute('UPDATE jobs SET received_ms=? WHERE id=20', (source['received_ms'] - 1000,))
        await self.process(self.callback(message, data))
        self.assertEqual(self.state(10), 'cancelled')
        self.assertEqual(self.router.store.db.execute('SELECT state FROM jobs WHERE id=20').fetchone()[0], 'pending')
        self.assertEqual(self.native_calls, [])

    async def test_source_that_started_dispatch_is_not_cancelled_by_old_button(self):
        for state in ('dispatching', 'running', 'uncertain'):
            message, data = await self.pending_bar(uid=self.sequence + 1)
            uid = self.router.bridges['a'].delivery._latest_receipt_source()['id']
            self.router.bridges['a'].store.mark(uid, state)
            await self.process(self.callback(message, data))
            self.assertEqual(self.state(uid), state)
        self.assertEqual(self.native_calls, [])

    async def test_cancel_rechecks_owner_bot_topic_message_and_binding(self):
        message, data = await self.pending_bar()
        samples = []
        for name, value in [('from', {'id': 999}), ('message_thread_id', 84), ('message_id', 99999)]:
            update = self.callback(message, data)
            if name == 'from':
                update['callback_query']['from'] = value
            else:
                update['callback_query']['message'][name] = value
            samples.append(update)
        for update in samples:
            await self.process(update)
            self.assertEqual(self.state(10), 'pending')
        self.allow_owner(False)
        await self.process(self.callback(message, data))
        self.assertEqual(self.state(10), 'pending')
        self.assertEqual(self.native_calls, [])

    async def test_active_command_interrupts_exact_task_and_is_deduplicated_with_button(self):
        message = await self.bar()
        async with self.router.bridges['a'].dispatch_lock:
            await asyncio.wait_for(self.process(self.command()), 1)
        self.assertEqual(self.interruptions(), [('a', 'turn/interrupt', {'threadId': 'native-a', 'turnId': 'a-work'})])
        self.assertIn('已请求停止', self.notices()[0]['text'])
        await self.process(self.callback(message))
        self.assertEqual(len(self.interruptions()), 1)

    async def test_unknown_interrupt_is_not_replayed_by_a_new_command_or_recovery(self):
        await self.bar()
        self.natives['a'].interrupt_error = ConnectionError('unknown')
        await self.process(self.command(100))
        self.router.status_controls.recover()
        await self.process(self.command(101))
        self.assertEqual(len(self.interruptions()), 1)
        self.assertIn('待核对', self.notices()[-1]['text'])

    async def test_queued_stop_does_not_interrupt_a_later_task(self):
        await self.bar()
        update = self.command()
        self.router.ingest(update)
        self.natives['a'].latest = 'new-task'
        await self.router.status_controls.handle_one()
        self.assertEqual(self.interruptions(), [])
        self.assertIn('之后开始', self.notices()[0]['text'])

    async def test_rebinding_after_native_read_prevents_interrupt(self):
        await self.bar()
        self.natives['a'].after_query = lambda: self.router.targets['a'].update(thread_id='changed')
        await self.process(self.command())
        self.assertEqual(self.interruptions(), [])

    async def test_recover_migrates_legacy_stop_with_original_cutoff_and_not_newer_input(self):
        bridge = self.router.bridges['a']
        bridge.ready.clear()
        self.accept(10, stamp=1000)
        message = self.command(20)['message']
        self.router.store.accept({'update_id': 20}, message, bridge.target)
        with self.router.store.db:
            self.router.store.db.execute('UPDATE jobs SET received_ms=2000 WHERE id=20')
        self.router.forward_one()
        self.accept(30, stamp=3000)
        self.router.status_controls.recover()
        self.assertEqual(self.state(20), 'done')
        await self.router.status_controls.handle_one()
        self.assertEqual(self.state(10), 'cancelled')
        self.assertEqual(self.state(30), 'pending')
        self.router.status_controls.recover()
        self.assertFalse(await self.router.status_controls.handle_one())
        self.assertEqual(self.native_calls, [])

    async def test_cancel_rechecks_pending_state_after_callback_acknowledgement(self):
        message, data = await self.pending_bar()
        bridge = self.router.bridges['a']
        original_answer = self.router.status_controls._answer
        async def answer(callback, text, alert=False):
            await original_answer(callback, text, alert)
            bridge.store.mark(10, 'dispatching')
        self.router.status_controls._answer = answer
        await self.process(self.callback(message, data))
        self.assertEqual(self.state(10), 'dispatching')
        self.assertEqual(self.native_calls, [])
        self.assertEqual(self.notices(), [])

    async def test_slow_native_read_in_one_topic_does_not_block_offline_stop_in_another(self):
        await self.bar('a')
        self.natives['a'].query_gate = asyncio.Event()
        self.router.bridges['b'].ready.clear()
        self.accept(10, name='b')
        self.router.ingest(self.command(100, name='a'))
        self.router.ingest(self.command(101, name='b'))
        worker = asyncio.create_task(self.router.status_controls.loop())
        try:
            await asyncio.wait_for(self.natives['a'].query_started.wait(), 1)
            for _ in range(50):
                if self.state(10, 'b') == 'cancelled':
                    break
                await asyncio.sleep(.01)
            self.assertEqual(self.state(10, 'b'), 'cancelled')
            self.assertTrue(self.notices('b'))
            self.assertEqual(self.interruptions(), [])
        finally:
            self.natives['a'].query_gate.set()
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)

    async def test_all_stop_keeps_confirmation_in_all_and_does_not_default_to_other_topic(self):
        self.router.bridges['a'].ready.clear()
        self.accept(10)
        self.accept(11, name='b')
        await self.process(self.command(message_thread_id=None))
        self.assertEqual(self.state(10), 'cancelled')
        self.assertEqual(self.state(11, 'b'), 'pending')
        self.assertIsNone(self.notices()[0]['message_thread_id'])

    async def test_literal_stop_in_force_reply_and_media_are_not_reserved_controls(self):
        parent = {'message_id': 77, 'from': {'id': 444}, 'text': '新建 Codex 会话 · 会话名称\n请输入名称'}
        update = self.command(100, reply_to_message=parent)
        self.router.ingest(update)
        self.assertEqual(self.router.store.db.execute('SELECT COUNT(*) FROM status_stop_jobs').fetchone()[0], 0)
        self.assertEqual(self.router.store.db.execute('SELECT COUNT(*) FROM ui_jobs').fetchone()[0], 1)
        self.assertFalse(self.router.status_controls.ingest(self.command(101, document={'file_id': 'file'})))
        self.assertFalse(self.router.status_controls.ingest(self.command(102, text='/stop@another_bot')))


if __name__ == '__main__':
    unittest.main()
