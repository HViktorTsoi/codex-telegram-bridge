"""Priority cancellation cannot revive cached or still-preparing user input."""
import asyncio
import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import aiohttp

from attachments import AttachmentError
from bridge import Bridge, InputPreparationError, now_ms
from topics_bridge import TargetBridge


class ObservedLock:
    """Signal completion of the input loop's first post-wait critical section."""
    def __init__(self, lock):
        self.lock = lock
        self.exited = asyncio.Event()

    async def __aenter__(self):
        await self.lock.acquire()
        return self

    async def __aexit__(self, *error):
        self.lock.release()
        self.exited.set()


class InputCancellationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name)
        (self.path / 'access.json').write_text(json.dumps({
            'dmPolicy': 'allowlist', 'allowFrom': ['123']}))
        config = {'owner_id': '123', 'thread_id': 'original-native',
                  'telegram_state_dir': str(self.path), 'socket_path': str(self.path / 'unused.sock')}
        self.bridge = Bridge(config, self.path)
        self.bridge.codex = SimpleNamespace(
            call=AsyncMock(return_value={'turn': {'id': 'real-turn'}}), ws=None)
        self.bridge.telegram = SimpleNamespace(
            call=AsyncMock(side_effect=AssertionError('No Telegram network calls in input tests')))
        self.tasks = []

    async def asyncTearDown(self):
        for task in self.tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.bridge.store.db.close()
        self.tmp.cleanup()

    def accept(self, update_id=1, *, album=None, attachment=False):
        message = {'message_id': update_id, 'from': {'id': 123},
                   'chat': {'id': 123, 'type': 'private'}, 'text': '处理这个输入'}
        if attachment:
            message.update(document={'file_id': 'fixture-' + str(update_id), 'file_name': 'input.pdf'})
        if hasattr(self.bridge, 'target'):
            message['message_thread_id'] = self.bridge.target['message_thread_id']
        if album is not None:
            message['media_group_id'] = album
        self.bridge.store.accept({'update_id': update_id}, message)
        if album is not None:
            with self.bridge.store.db:
                self.bridge.store.db.execute('UPDATE inbox SET received_ms=? WHERE id=?',
                                            (now_ms() - 5000, update_id))
        return self.bridge.store.db.execute('SELECT * FROM inbox WHERE id=?', (update_id,)).fetchone()

    def states(self):
        return [(row['id'], row['state'], row['client_id'], row['turn_id'])
                for row in self.bridge.store.db.execute('SELECT * FROM inbox ORDER BY id')]

    def task(self, coroutine):
        task = asyncio.create_task(coroutine)
        self.tasks.append(task)
        return task

    async def dispatch(self, row):
        async with self.bridge.dispatch_lock:
            await self.bridge.dispatch_one(row)

    def use_topic_bridge(self):
        """Keep the real routed error-queue path, with no native or sender tasks."""
        old = self.bridge
        config = {**old.config, 'notification_mode': 'all', 'native_mirror': False}
        old.store.db.close()
        self.bridge = TargetBridge(config, self.path,
            {'key': 'topic', 'name': 'Current topic', 'thread_id': 'original-native',
             'message_thread_id': 42}, lambda: '')
        self.bridge.codex, self.bridge.telegram = old.codex, old.telegram
        self.bridge.codex.ws = SimpleNamespace(closed=False, close=AsyncMock())

    async def cancelled_during_prepare(self, *, attachment_stage):
        row = self.accept(attachment=True)
        entered, release = asyncio.Event(), asyncio.Event()

        async def blocked(*args):
            entered.set()
            await release.wait()
            return [{'type': 'text', 'text': '已完成准备'}]

        inputs = AsyncMock(side_effect=blocked) if attachment_stage else AsyncMock(
            return_value=[{'type': 'text', 'text': '附件已保存'}])
        preparation = AsyncMock(side_effect=blocked) if not attachment_stage else AsyncMock(
            side_effect=lambda rows, values: values)
        with patch('bridge.inputs_for', new=inputs), patch.object(
                self.bridge, 'prepare_inputs', new=preparation):
            worker = self.task(self.dispatch(row))
            await asyncio.wait_for(entered.wait(), 1)
            self.assertTrue(self.bridge.dispatch_lock.locked())
            self.bridge.store.mark(row['id'], 'cancelled')
            release.set()
            await asyncio.wait_for(worker, 1)
        self.bridge.codex.call.assert_not_awaited()
        self.bridge.telegram.call.assert_not_awaited()
        self.assertEqual(self.states(), [(1, 'cancelled', 'telegram-1', None)])
        self.assertEqual(self.bridge.store.counts()['unsent_messages'], 0)

    async def test_cancel_during_attachment_download_does_not_start_native_turn(self):
        await self.cancelled_during_prepare(attachment_stage=True)

    async def test_cancel_during_prepare_inputs_does_not_start_native_turn(self):
        await self.cancelled_during_prepare(attachment_stage=False)

    async def test_cancelled_input_is_not_overwritten_by_late_preparation_rejection(self):
        row = self.accept()
        entered, release = asyncio.Event(), asyncio.Event()

        async def preparation(*args):
            entered.set()
            await release.wait()
            raise InputPreparationError('技能元信息读取失败')

        with patch('bridge.inputs_for', new=AsyncMock(return_value=[{'type': 'text', 'text': 'task'}])), \
                patch.object(self.bridge, 'prepare_inputs', side_effect=preparation):
            worker = self.task(self.dispatch(row))
            await asyncio.wait_for(entered.wait(), 1)
            self.bridge.store.mark(row['id'], 'cancelled')
            release.set()
            await asyncio.wait_for(worker, 1)
        self.assertEqual(self.states(), [(1, 'cancelled', 'telegram-1', None)])
        self.bridge.codex.call.assert_not_awaited()
        self.assertEqual(self.bridge.store.counts()['unsent_messages'], 0)

    async def locked_input_loop(self, *, cancel):
        self.accept()
        cached = asyncio.Event()
        original_pending = self.bridge.store.pending

        def pending():
            row = original_pending()
            if row is not None:
                cached.set()
            return row

        underlying = asyncio.Lock()
        await underlying.acquire()
        observed = self.bridge.dispatch_lock = ObservedLock(underlying)
        self.bridge.ready.set()
        dispatch = AsyncMock(side_effect=AssertionError('Cached row must not be dispatched'))
        with patch.object(self.bridge.store, 'pending', side_effect=pending), patch.object(
                self.bridge, 'dispatch_one', new=dispatch):
            worker = self.task(self.bridge.input_loop())
            try:
                await asyncio.wait_for(cached.wait(), 1)
                self.assertTrue(underlying.locked())
                if cancel:
                    self.bridge.store.mark(1, 'cancelled')
                else:
                    self.bridge.ready.clear()
                underlying.release()
                await asyncio.wait_for(observed.exited.wait(), 1)
                dispatch.assert_not_awaited()
                self.bridge.codex.call.assert_not_awaited()
                self.assertFalse(worker.done())
            finally:
                worker.cancel()
                await asyncio.gather(worker, return_exceptions=True)
                if underlying.locked():
                    underlying.release()
        expected = 'cancelled' if cancel else 'pending'
        self.assertEqual(self.states(), [(1, expected, 'telegram-1', None)])

    async def test_cached_input_cancelled_while_waiting_for_dispatch_lock_is_not_dispatched(self):
        await self.locked_input_loop(cancel=True)

    async def test_cached_input_is_not_dispatched_if_connection_became_unready_while_waiting_for_lock(self):
        await self.locked_input_loop(cancel=False)

    def test_partial_album_cancellation_rolls_back_claim_for_every_other_member(self):
        for update_id in (10, 11, 12):
            self.accept(update_id, album='shared-album', attachment=True)
        stale_batch = self.bridge.store.batch(self.bridge.store.pending())
        self.assertEqual([row['id'] for row in stale_batch], [10, 11, 12])
        # A second connection commits the cancellation after batch selection.
        # Failed CAS must not roll that cancellation back or partly claim peers.
        with sqlite3.connect(self.path / 'bridge.sqlite') as stop_writer:
            stop_writer.execute("UPDATE inbox SET state='cancelled' WHERE id=11")
        self.assertFalse(self.bridge.store.claim_batch(stale_batch))
        expected = [(10, 'pending', 'telegram-10', None),
                    (11, 'cancelled', 'telegram-11', None),
                    (12, 'pending', 'telegram-12', None)]
        self.assertEqual(self.states(), expected)
        self.assertFalse(self.bridge.store.db.in_transaction)
        with sqlite3.connect('file:' + str(self.path / 'bridge.sqlite') + '?mode=ro', uri=True) as observer:
            self.assertEqual(observer.execute('SELECT id,state,client_id,turn_id FROM inbox ORDER BY id').fetchall(), expected)
        self.bridge.codex.call.assert_not_awaited()

    def test_normal_album_claim_is_durable_and_unifies_client_id_once(self):
        for update_id in (20, 21, 22):
            self.accept(update_id, album='normal-album', attachment=True)
        batch = self.bridge.store.batch(self.bridge.store.pending())
        self.assertTrue(self.bridge.store.claim_batch(batch))
        expected = [(update_id, 'dispatching', 'telegram-20', None) for update_id in (20, 21, 22)]
        self.assertEqual(self.states(), expected)
        self.assertFalse(self.bridge.store.claim_batch(batch))
        self.assertEqual(self.states(), expected)
        self.assertFalse(self.bridge.store.claim_batch([]))

    async def test_partial_album_cancellation_during_preparation_never_sends_remaining_parts(self):
        for update_id in (30, 31):
            self.accept(update_id, album='preparing-album', attachment=True)
        row = self.bridge.store.pending()
        entered, release = asyncio.Event(), asyncio.Event()

        async def blocked(rows, *args):
            self.assertEqual([item['id'] for item in rows], [30, 31])
            entered.set()
            await release.wait()
            return [{'type': 'text', 'text': '整组相册已准备'}]

        with patch('bridge.inputs_for', new=AsyncMock(side_effect=blocked)):
            worker = self.task(self.dispatch(row))
            await asyncio.wait_for(entered.wait(), 1)
            self.bridge.store.mark(31, 'cancelled')
            release.set()
            await asyncio.wait_for(worker, 1)
        self.assertEqual(self.states(), [(30, 'pending', 'telegram-30', None),
                                        (31, 'cancelled', 'telegram-31', None)])
        self.bridge.codex.call.assert_not_awaited()

    async def test_normal_uncancelled_queued_input_starts_exactly_one_original_native_turn(self):
        self.accept()
        self.accept()  # Telegram update replay must remain one durable input.
        idle = asyncio.Event()
        original_pending = self.bridge.store.pending

        def pending():
            row = original_pending()
            if row is None:
                idle.set()
            return row

        async def native(method, params):
            self.assertEqual(method, 'turn/start')
            self.assertEqual(params['threadId'], 'original-native')
            self.assertEqual(params['clientUserMessageId'], 'telegram-1')
            self.assertEqual(self.states(), [(1, 'dispatching', 'telegram-1', None)])
            return {'turn': {'id': 'real-turn'}}

        self.bridge.codex.call.side_effect = native
        self.bridge.ready.set()
        with patch('bridge.inputs_for', new=AsyncMock(return_value=[{'type': 'text', 'text': '处理这个输入'}])), \
                patch.object(self.bridge.store, 'pending', side_effect=pending):
            worker = self.task(self.bridge.input_loop())
            try:
                await asyncio.wait_for(idle.wait(), 1)
                self.bridge.codex.call.assert_awaited_once()
                self.assertEqual(self.states(), [(1, 'running', 'telegram-1', 'real-turn')])
            finally:
                worker.cancel()
                await asyncio.gather(worker, return_exceptions=True)
        self.bridge.telegram.call.assert_not_awaited()

    async def test_attachment_transport_errors_keep_native_connection_and_allow_the_next_text(self):
        self.use_topic_bridge()
        errors = [ConnectionError('https://api.telegram.org/file/botFAKE_SECRET/input.pdf'),
                  asyncio.TimeoutError(), aiohttp.ClientConnectionError('lost file transport'),
                  OSError('private file transport details')]
        for index, error in enumerate(errors):
            with self.subTest(error=type(error).__name__):
                file_id, text_id = index * 2 + 1, index * 2 + 2
                self.accept(file_id, attachment=True)
                self.accept(text_id)
                idle, prepared = asyncio.Event(), []
                original_pending = self.bridge.store.pending

                def pending():
                    row = original_pending()
                    if row is None:
                        idle.set()
                    return row

                async def inputs(rows, *args):
                    prepared.append(rows[0]['id'])
                    if rows[0]['id'] == file_id:
                        raise error
                    return [{'type': 'text', 'text': '后续普通文字'}]

                self.bridge.ready.set()
                with patch('bridge.inputs_for', new=AsyncMock(side_effect=inputs)), patch.object(
                        self.bridge.store, 'pending', side_effect=pending):
                    worker = self.task(self.bridge.input_loop())
                    try:
                        await asyncio.wait_for(idle.wait(), 1)
                        self.assertFalse(worker.done())
                        self.assertEqual(prepared, [file_id, text_id])
                        self.assertEqual(self.states()[-2:], [
                            (file_id, 'failed', 'telegram-' + str(file_id), None),
                            (text_id, 'running', 'telegram-' + str(text_id), 'real-turn')])
                        self.bridge.codex.ws.close.assert_not_awaited()
                        self.assertEqual(self.bridge.codex.call.await_count, index + 1)
                        method, params = self.bridge.codex.call.call_args.args
                        self.assertEqual(method, 'turn/start')
                        self.assertEqual(params['clientUserMessageId'], 'telegram-' + str(text_id))
                    finally:
                        worker.cancel()
                        await asyncio.gather(worker, return_exceptions=True)
                notices = self.bridge.store.db.execute('SELECT text,message_thread_id FROM outbox').fetchall()
                self.assertEqual(len(notices), index + 1)
                for notice in notices:
                    self.assertEqual(notice['message_thread_id'], 42)
                    self.assertIn('附件下载暂时失败', notice['text'])
                    self.assertNotIn('FAKE_SECRET', notice['text'])
                    self.assertNotIn('api.telegram.org', notice['text'])
                    self.assertNotIn('private file transport details', notice['text'])
        self.bridge.telegram.call.assert_not_awaited()

    async def test_permanent_attachment_failure_marks_only_input_failed_and_queues_original_topic_hint(self):
        self.use_topic_bridge()
        row = self.accept(attachment=True)
        problem = '附件超过20MB下载上限，请压缩或拆分后发送。'
        with patch('bridge.inputs_for', new=AsyncMock(side_effect=AttachmentError(problem))):
            await self.dispatch(row)
        self.assertEqual(self.states(), [(1, 'failed', 'telegram-1', None)])
        notices = self.bridge.store.db.execute('SELECT chat_id,text,message_thread_id FROM outbox').fetchall()
        self.assertEqual([(notice['chat_id'], notice['text'], notice['message_thread_id']) for notice in notices],
                         [('123', problem, 42)])
        self.bridge.codex.call.assert_not_awaited()
        self.bridge.codex.ws.close.assert_not_awaited()
        self.bridge.telegram.call.assert_not_awaited()

    async def test_cancelled_download_failure_preserves_cancelled_state_and_does_not_queue_an_error(self):
        self.use_topic_bridge()
        for update_id, error in enumerate((ConnectionError('late network failure'),
                                           AttachmentError('late permanent failure')), 1):
            with self.subTest(error=type(error).__name__):
                row = self.accept(update_id, attachment=True)
                entered, release = asyncio.Event(), asyncio.Event()

                async def late_failure(*args):
                    entered.set()
                    await release.wait()
                    raise error

                with patch('bridge.inputs_for', new=AsyncMock(side_effect=late_failure)):
                    worker = self.task(self.dispatch(row))
                    await asyncio.wait_for(entered.wait(), 1)
                    self.bridge.store.mark(update_id, 'cancelled')
                    release.set()
                    await asyncio.wait_for(worker, 1)
                self.assertEqual(self.states()[-1],
                                 (update_id, 'cancelled', 'telegram-' + str(update_id), None))
                self.assertEqual(self.bridge.store.counts()['unsent_messages'], 0)
        self.bridge.codex.call.assert_not_awaited()
        self.bridge.codex.ws.close.assert_not_awaited()
        self.bridge.telegram.call.assert_not_awaited()

    async def test_native_prepare_connection_error_keeps_existing_reconnect_behavior(self):
        self.use_topic_bridge()
        self.accept(attachment=True)
        closed = asyncio.Event()

        async def close_native():
            closed.set()
            self.bridge.ready.clear()

        self.bridge.codex.ws.close.side_effect = close_native
        self.bridge.ready.set()
        with patch('bridge.inputs_for', new=AsyncMock(return_value=[{'type': 'text', 'text': '文件已保存'}])), \
                patch.object(self.bridge, 'prepare_inputs', new=AsyncMock(
                    side_effect=ConnectionError('native session disconnected'))):
            worker = self.task(self.bridge.input_loop())
            try:
                await asyncio.wait_for(closed.wait(), 1)
                self.bridge.codex.ws.close.assert_awaited_once()
                self.bridge.codex.call.assert_not_awaited()
                self.assertEqual(self.states(), [(1, 'pending', 'telegram-1', None)])
                self.assertEqual(self.bridge.store.counts()['unsent_messages'], 0)
            finally:
                worker.cancel()
                await asyncio.gather(worker, return_exceptions=True)


if __name__ == '__main__':
    unittest.main()
