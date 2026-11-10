"""Protocol regressions for original native question request/response IDs."""
import asyncio
import copy
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import aiohttp

from bridge import Bridge, Codex, RpcError


class Socket:
    def __init__(self):
        self.queue, self.sent, self.closed = asyncio.Queue(), [], False
        self.failure = None
        self.write_gate = None

    def __aiter__(self):
        return self

    async def __anext__(self):
        data = await self.queue.get()
        if data is None:
            raise StopAsyncIteration
        return SimpleNamespace(type=aiohttp.WSMsgType.TEXT, data=json.dumps(data))

    async def send_json(self, data):
        self.sent.append(copy.deepcopy(data))
        if self.write_gate:
            await self.write_gate.wait()
        if self.failure:
            raise self.failure

    async def close(self):
        self.closed = True
        await self.queue.put(None)


class QuestionRpcTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = Codex('/unused', 'native')
        self.socket = self.client.ws = Socket()
        self.reader = self.client.reader = asyncio.create_task(self.client._read())
        self.params = {'threadId': 'native', 'turnId': 'turn', 'itemId': 'item', 'questions': [
            {'id': 'direction', 'question': 'Which direction?', 'options': [{'label': 'Left'}, {'label': 'Right'}]}]}
        self.answers = {'direction': {'answers': ['Left']}}

    async def asyncTearDown(self):
        await self.socket.close()
        await self.reader

    async def request(self, request_id='original-request'):
        event = {'id': request_id, 'method': 'item/tool/requestUserInput', 'params': copy.deepcopy(self.params)}
        await self.socket.queue.put(event)
        return await asyncio.wait_for(self.client.events.get(), 1)

    async def test_request_keeps_original_id_params_and_connection_identity(self):
        for request_id in ('server-42', 42):
            event = await self.request(request_id)
            self.assertEqual(event['id'], request_id)
            self.assertIs(type(event['id']), type(request_id))
            self.assertEqual(self.client.pending_user_inputs[request_id], self.params)
            self.assertEqual(event['_user_input_epoch'], self.client.user_input_epoch)

    async def test_resolved_clears_before_event_consumption_and_retains_notification(self):
        await self.request(42)
        await self.socket.queue.put({'method': 'serverRequest/resolved', 'params': {
            'threadId': 'native', 'requestId': 42}})
        event = await asyncio.wait_for(self.client.events.get(), 1)
        self.assertEqual(event['method'], 'serverRequest/resolved')
        self.assertEqual(event['_user_input_epoch'], self.client.user_input_epoch)
        self.assertNotIn(42, self.client.pending_user_inputs)
        with self.assertRaises(RpcError):
            await self.client.respond_user_input(42, self.answers)
        self.assertEqual(self.socket.sent, [])

    async def test_other_thread_requests_and_resolutions_do_not_cross_route(self):
        await self.request(42)
        foreign = copy.deepcopy(self.params)
        foreign['threadId'] = 'other-native'
        await self.socket.queue.put({'id': 99, 'method': 'item/tool/requestUserInput', 'params': foreign})
        await self.socket.queue.put({'method': 'serverRequest/resolved', 'params': {
            'threadId': 'other-native', 'requestId': 42}})
        # A marker proves all earlier messages have been read.
        await self.socket.queue.put({'method': 'item/completed', 'params': {'threadId': 'native', 'item': {}}})
        self.assertEqual((await asyncio.wait_for(self.client.events.get(), 1))['method'], 'item/completed')
        self.assertEqual(set(self.client.pending_user_inputs), {42})
        self.assertTrue(self.client.events.empty())

    async def test_only_native_question_requests_are_answerable(self):
        for method in ('item/commandExecution/requestApproval', 'item/permissions/requestApproval'):
            await self.socket.queue.put({'id': method, 'method': method, 'params': self.params})
            event = await asyncio.wait_for(self.client.events.get(), 1)
            self.assertEqual(event['method'], method)
            with self.assertRaises(RpcError):
                await self.client.respond_user_input(method, self.answers)
        self.assertEqual(self.client.pending_user_inputs, {})
        self.assertEqual(self.socket.sent, [])

    async def test_response_is_original_rpc_result_and_not_new_turn(self):
        for request_id in ('original-request', 57):
            await self.request(request_id)
            await self.client.respond_user_input(request_id, self.answers)
            self.assertEqual(self.socket.sent[-1], {'id': request_id, 'result': {'answers': self.answers}})
            self.assertIs(type(self.socket.sent[-1]['id']), type(request_id))
            self.assertNotIn(request_id, self.client.pending_user_inputs)
            with self.assertRaises(RpcError):
                await self.client.respond_user_input(request_id, self.answers)
        self.assertEqual(len(self.socket.sent), 2)

    async def test_claim_precedes_socket_write_so_concurrent_clicks_send_once(self):
        await self.request()
        self.socket.write_gate = asyncio.Event()
        first = asyncio.create_task(self.client.respond_user_input('original-request', self.answers))
        await asyncio.sleep(0)
        with self.assertRaises(RpcError):
            await self.client.respond_user_input('original-request', self.answers)
        self.socket.write_gate.set()
        await first
        self.assertEqual(len(self.socket.sent), 1)

    async def test_ambiguous_write_is_not_retried_or_reopened_by_same_connection_replay(self):
        await self.request()
        self.socket.failure = ConnectionError('write interrupted')
        with self.assertRaises(ConnectionError):
            await self.client.respond_user_input('original-request', self.answers)
        await self.socket.queue.put({'id': 'original-request', 'method': 'item/tool/requestUserInput', 'params': self.params})
        await self.socket.queue.put({'method': 'item/completed', 'params': {'threadId': 'native', 'item': {}}})
        self.assertEqual((await asyncio.wait_for(self.client.events.get(), 1))['method'], 'item/completed')
        with self.assertRaises(RpcError):
            await self.client.respond_user_input('original-request', self.answers)
        self.assertEqual(len(self.socket.sent), 1)
        self.assertEqual(self.client.pending_user_inputs, {})

    async def test_disconnect_discards_pending_requests(self):
        await self.request()
        await self.socket.close()
        await self.reader
        self.assertEqual(self.client.pending_user_inputs, {})
        with self.assertRaises(ConnectionError):
            await self.client.respond_user_input('original-request', self.answers)
        self.assertEqual(self.socket.sent, [])

    async def test_reconnect_changes_identity_and_waits_for_live_replay(self):
        await self.request()
        previous_epoch = self.client.user_input_epoch
        await self.socket.close()
        await self.reader
        socket = Socket()
        session = SimpleNamespace(ws_connect=AsyncMock(return_value=socket), close=AsyncMock())
        with patch('bridge.aiohttp.ClientSession', return_value=session), \
                patch.object(self.client, 'call', new=AsyncMock(return_value={'thread': {'id': 'native'}})):
            await self.client.connect()
        self.socket, self.reader = socket, self.client.reader
        self.assertNotEqual(previous_epoch, self.client.user_input_epoch)
        self.assertEqual(self.client.pending_user_inputs, {})
        with self.assertRaises(RpcError):
            await self.client.respond_user_input('original-request', self.answers)
        await self.request('replayed-request')
        await self.client.respond_user_input('replayed-request', self.answers)
        self.assertEqual(socket.sent[-1]['id'], 'replayed-request')

    async def test_invalid_answers_and_secret_questions_stay_in_original_client(self):
        for answers in ({}, {'wrong': {'answers': ['Left']}}, {'direction': {'answers': 'Left'}},
                        {'direction': {'answers': []}}, {'direction': {'answers': [42]}}):
            with self.subTest(answers=answers):
                await self.request()
                with self.assertRaises(RpcError):
                    await self.client.respond_user_input('original-request', answers)
        self.params['questions'][0]['isSecret'] = True
        await self.request('secret')
        with self.assertRaises(RpcError):
            await self.client.respond_user_input('secret', self.answers)
        self.assertEqual(self.socket.sent, [])


class ServerEventHookTests(unittest.IsolatedAsyncioTestCase):
    async def test_handled_events_bypass_default_request_and_item_paths(self):
        bridge = object.__new__(Bridge)
        bridge.codex = SimpleNamespace(events=asyncio.Queue())
        bridge.dispatch_lock = asyncio.Lock()
        bridge.record_item, bridge.finish_turn = Mock(), Mock()
        bridge.store = SimpleNamespace(running=Mock(return_value=True), enqueue=Mock())
        bridge.owner = '123'
        observed, complete = [], asyncio.Event()

        async def handle(event):
            observed.append(event['method'])
            if len(observed) == 3:
                complete.set()
            return event['method'] != 'item/completed'

        bridge.handle_server_event = handle
        for event in [
            {'id': 'question', 'method': 'item/tool/requestUserInput', 'params': {}},
            {'method': 'serverRequest/resolved', 'params': {}},
            {'method': 'item/completed', 'params': {'turnId': 'turn', 'item': {'id': 'item'}}},
        ]:
            bridge.codex.events.put_nowait(event)
        task = asyncio.create_task(bridge.event_loop())
        try:
            await asyncio.wait_for(complete.wait(), 1)
        finally:
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(observed, ['item/tool/requestUserInput', 'serverRequest/resolved', 'item/completed'])
        bridge.store.enqueue.assert_not_called()
        bridge.record_item.assert_called_once_with('turn', {'id': 'item'}, None)

    async def test_default_hook_preserves_existing_approval_handling(self):
        bridge = object.__new__(Bridge)
        self.assertFalse(await bridge.handle_server_event({'method': 'item/permissions/requestApproval'}))


if __name__ == '__main__':
    unittest.main()
