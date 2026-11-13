"""Account-event observation and usage-card routing without native generation."""
import asyncio
import copy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from bridge import Codex
from test_question_rpc import Socket
from topics_bridge import TopicRouter


class AccountEventRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = Codex('/unused', 'native')
        self.socket = self.client.ws = Socket()
        self.reader = self.client.reader = asyncio.create_task(self.client._read())
        # _read captures the connection epoch before a simulated reconnect.
        await asyncio.sleep(0)

    async def asyncTearDown(self):
        await self.socket.close()
        await self.reader

    async def marker(self):
        event = {'method': 'item/completed', 'params': {
            'threadId': 'native', 'turnId': 'marker-turn', 'item': {'id': 'marker-item'}}}
        await self.socket.queue.put(event)
        received = await asyncio.wait_for(self.client.events.get(), 1)
        self.assertEqual(received, event)
        self.assertTrue(self.client.events.empty())

    async def test_only_whitelisted_notifications_reach_hook_before_thread_filter(self):
        hook = self.client.on_account_event = Mock()
        events = [
            {'method': 'account/rateLimits/updated', 'params': {'rateLimits': {'primary': {'usedPercent': 17}}}},
            {'method': 'account/updated', 'params': {'threadId': 'another-native', 'authMode': 'chatgpt'}},
        ]
        for event in events:
            await self.socket.queue.put(copy.deepcopy(event))
        await self.marker()
        self.assertEqual([call.args[0] for call in hook.call_args_list],
                         [{**event, '_account_epoch': self.client.user_input_epoch} for event in events])
        self.assertEqual(self.socket.sent, [])

    async def test_nonroot_client_without_hook_discards_account_events_even_with_thread_id(self):
        self.assertIsNone(self.client.on_account_event)
        for method in ('account/rateLimits/updated', 'account/updated'):
            await self.socket.queue.put({'method': method, 'params': {'threadId': 'native'}})
        await self.marker()
        self.assertEqual(self.client.pending_user_inputs, {})
        self.assertEqual(self.socket.sent, [])

    async def test_old_connection_epoch_does_not_update_account_observer(self):
        hook = self.client.on_account_event = Mock()
        old_epoch = self.client.user_input_epoch
        self.client.user_input_epoch = 'new-connection-epoch'
        self.assertNotEqual(old_epoch, self.client.user_input_epoch)
        for method in ('account/rateLimits/updated', 'account/updated'):
            await self.socket.queue.put({'method': method, 'params': {}})
        await self.marker()
        hook.assert_not_called()

    async def test_observer_exception_does_not_drop_later_native_turn_event(self):
        hook = self.client.on_account_event = Mock(side_effect=RuntimeError('observer failed'))
        with patch('bridge.LOG.warning') as warning:
            for method in ('account/rateLimits/updated', 'account/updated'):
                await self.socket.queue.put({'method': method, 'params': {}})
            await self.socket.queue.put({'method': 'turn/started', 'params': {
                'threadId': 'native', 'turn': {'id': 'later-turn', 'status': 'inProgress'}}})
            turn_event = await asyncio.wait_for(self.client.events.get(), 1)
            self.assertEqual(turn_event['method'], 'turn/started')
            self.assertEqual(turn_event['_turn_state_epoch'], self.client.turn_state.epoch)
            self.assertIn('later-turn', self.client.turn_state.active)
            await self.marker()
        self.assertEqual(hook.call_count, 2)
        self.assertEqual(warning.call_count, 2)
        self.assertFalse(self.reader.done())

    async def test_account_requests_and_other_global_notifications_cannot_reach_hook_or_native_queue(self):
        hook = self.client.on_account_event = Mock()
        events = [
            {'id': 'account-request', 'method': 'account/updated', 'params': {}},
            {'id': 'limits-request', 'method': 'account/rateLimits/updated', 'params': {'threadId': 'native'}},
            {'id': 'login-request', 'method': 'account/login', 'params': {}},
            {'id': 'approval', 'method': 'item/commandExecution/requestApproval', 'params': {}},
            {'method': 'account/login/completed', 'params': {}},
            {'method': 'account/unknown', 'params': {}},
        ]
        for event in events:
            await self.socket.queue.put(event)
        await self.marker()
        hook.assert_not_called()
        self.assertEqual(self.client.pending_user_inputs, {})
        self.assertEqual(self.socket.sent, [])


class UsageRouterIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name)
        (self.path / 'access.json').write_text(json.dumps({'dmPolicy': 'allowlist', 'allowFrom': ['123']}))
        self.config = {
            'owner_id': '123', 'host_id': 'test-host', 'telegram_state_dir': str(self.path),
            'expected_bot_username': 'test_bot', 'socket_path': str(self.path / 'unused.sock'),
            'thread_id': 'native-root', 'root_target': 'root', 'initial_telegram_offset': 100,
            'targets': [
                {'key': 'root', 'name': 'Manager', 'thread_id': 'native-root', 'message_thread_id': 11},
                {'key': 'other', 'name': 'Other', 'thread_id': 'native-other', 'message_thread_id': 22},
            ],
        }
        self.router = TopicRouter(self.config, self.path)
        self.router.bot_id, self.router.username = 456, 'test_bot'
        self.router.telegram = SimpleNamespace(call=AsyncMock(side_effect=AssertionError('No Telegram calls during ingest')))
        for bridge in self.router.bridges.values():
            bridge.codex.call = AsyncMock(side_effect=AssertionError('No native calls during ingest'))

    async def asyncTearDown(self):
        for bridge in self.router.bridges.values():
            bridge.store.db.close()
        self.router.store.db.close()
        self.tmp.cleanup()

    @staticmethod
    def message(update_id, text='/usage', topic=None, **extra):
        message = {'message_id': update_id + 1000, 'date': 1, 'text': text,
                   'from': {'id': 123}, 'chat': {'id': 123, 'type': 'private'}}
        if topic is not None:
            message['message_thread_id'] = topic
        message.update(extra)
        return {'update_id': update_id, 'message': message}

    def rows(self, table):
        return self.router.store.db.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0]

    def assert_no_native_or_all_dispatch(self):
        self.assertEqual(self.rows('jobs'), 0)
        self.assertEqual(self.rows('cc_jobs'), 0)
        self.assertEqual(self.rows('cc_requests'), 0)
        self.assertEqual(self.router.picker.guard.pending_count(), 0)
        for bridge in self.router.bridges.values():
            self.assertIsNone(bridge.store.pending())
            bridge.codex.call.assert_not_awaited()
        self.router.telegram.call.assert_not_awaited()

    def test_only_root_native_connection_observes_account_events(self):
        root, other = (self.router.bridges[key].codex for key in ('root', 'other'))
        self.assertEqual(root.on_account_event, self.router.usage.on_account_event)
        self.assertIsNone(other.on_account_event)
        self.assertFalse(self.router.usage.enabled)
        root.on_account_event({'method': 'account/updated', 'params': {},
                               '_account_epoch': root.user_input_epoch})
        self.assertEqual(len(self.router.usage.events), 0)

    def test_usage_command_from_main_or_topic_enters_only_usage_lane(self):
        for update in (self.message(100), self.message(101, topic=22),
                       self.message(102, text='/usage@test_bot', topic=11)):
            self.router.ingest(update)
        self.assertEqual(self.rows('usage_jobs'), 3)
        self.assertEqual(self.router.store.offset(), 103)
        self.assert_no_native_or_all_dispatch()

    def test_usage_refresh_callback_enters_only_usage_lane(self):
        update = {'update_id': 100, 'callback_query': {
            'id': 'callback', 'from': {'id': 123}, 'data': 'uc:refresh', 'message': {
                'message_id': 42, 'date': 1, 'from': {'id': 456},
                'chat': {'id': 123, 'type': 'private'}, 'text': 'Cached usage'}}}
        self.router.ingest(update)
        self.assertEqual(self.rows('usage_jobs'), 1)
        payload = json.loads(self.router.store.db.execute('SELECT payload FROM usage_jobs').fetchone()[0])
        self.assertEqual(payload, update)
        self.assert_no_native_or_all_dispatch()

    def test_unauthorized_usage_command_is_not_queued_for_any_session(self):
        self.router.ingest(self.message(100, **{'from': {'id': 999}}))
        self.assertEqual(self.rows('usage_jobs'), 0)
        self.assertEqual(self.router.store.offset(), 101)
        self.assert_no_native_or_all_dispatch()

    def test_usage_like_attachment_keeps_normal_target_and_payload(self):
        updates = [
            self.message(100, text='', topic=22, caption='/usage', document={'file_id': 'test-document'}),
            self.message(101, text='/usage', topic=22, photo=[{'file_id': 'test-photo'}]),
        ]
        for update in updates:
            self.router.ingest(update)
        self.assertEqual(self.rows('usage_jobs'), 0)
        self.assertEqual(self.rows('jobs'), 2)
        while self.router.forward_one():
            pass
        other = self.router.bridges['other']
        rows = other.store.db.execute('SELECT text,message_thread_id,message_json FROM inbox ORDER BY id').fetchall()
        self.assertEqual(len(rows), 2)
        self.assertEqual([row['message_thread_id'] for row in rows], [22, 22])
        self.assertEqual(json.loads(rows[0]['message_json']),
                         {'caption': '/usage', 'document': {'file_id': 'test-document'}})
        self.assertEqual(json.loads(rows[1]['message_json']), {'photo': [{'file_id': 'test-photo'}]})
        self.assertEqual(rows[1]['text'], '/usage')
        self.assertIsNone(self.router.bridges['root'].store.pending())
        for bridge in self.router.bridges.values():
            bridge.codex.call.assert_not_awaited()

    def test_forwarded_usage_text_keeps_normal_topic_routing(self):
        update = self.message(100, topic=22, forward_origin={'type': 'hidden_user', 'sender_user_name': 'Example'})
        self.router.ingest(update)
        self.assertEqual(self.rows('usage_jobs'), 0)
        self.assertTrue(self.router.forward_one())
        row = self.router.bridges['other'].store.pending()
        self.assertEqual((row['text'], row['message_thread_id']), ('/usage', 22))
        self.assertIsNone(self.router.bridges['root'].store.pending())

    def test_real_question_prompt_reply_bypasses_usage_command_parser(self):
        with self.router.store.db:
            self.router.store.db.execute('INSERT INTO choice_prompts '
                '(chat_id,message_id,topic_id,bot_id,question_id,position) VALUES(?,?,?,?,?,?)',
                ('123', 77, 22, 456, 'native-question', 0))
        parent = {'message_id': 77, 'from': {'id': 456}, 'text': '回答 Codex 问题：'}
        with patch.object(self.router.usage, 'ingest', wraps=self.router.usage.ingest) as ingest:
            self.router.ingest(self.message(100, text='/usage', topic=22, reply_to_message=parent))
            self.router.ingest(self.message(101, text='我的正常答案', topic=22, reply_to_message=parent))
        ingest.assert_not_called()
        self.assertEqual(self.rows('usage_jobs'), 0)
        self.assertEqual(self.rows('choice_jobs'), 1)
        queued = json.loads(self.router.store.db.execute('SELECT payload FROM choice_jobs').fetchone()[0])
        self.assertEqual(queued['message']['text'], '我的正常答案')
        self.assertEqual(queued['message']['reply_to_message'], parent)


if __name__ == '__main__':
    unittest.main()
