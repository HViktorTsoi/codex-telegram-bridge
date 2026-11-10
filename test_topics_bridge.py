import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from topics_bridge import ExactCodex, RpcError, TopicRouter
from bridge import Codex, now_ms


class NativeCompatibilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_resume_must_return_exact_existing_thread(self):
        with patch.object(Codex, 'connect', new=AsyncMock(return_value={'thread': {'id': 'different'}})):
            with self.assertRaises(RpcError):
                await ExactCodex('/unused', 'wanted').connect()

    async def test_legacy_history_fallback_preserves_item_timestamps_and_client_ids(self):
        client = ExactCodex('/unused', 'existing')
        calls = []

        async def call(method, params):
            calls.append((method, params))
            if method == 'thread/items/list':
                raise RpcError('thread/items/list is not supported yet')
            if not params.get('cursor'):
                return {'data': [{'id': 'unrelated', 'items': []}], 'nextCursor': 'older'}
            return {'data': [{'id': 'wanted', 'items': [
                {'type': 'userMessage', 'clientId': 'original-input', 'completedAtMs': 100},
                {'type': 'agentMessage', 'id': 'reply', 'text': 'Recovered', 'completedAtMs': 200}]}]}

        client.call = call
        entries = [e async for e in client.items('wanted')]
        self.assertEqual(entries[0]['item']['clientId'], 'original-input')
        self.assertEqual(entries[1]['completedAtMs'], 200)
        self.assertTrue(all(e['turnId'] == 'wanted' for e in entries))
        self.assertTrue(all(p['threadId'] == 'existing' for _, p in calls))
        self.assertEqual([m for m, _ in calls], ['thread/items/list', 'thread/turns/list', 'thread/turns/list'])
        calls.clear()
        [e async for e in client.items('wanted')]
        self.assertTrue(all(m == 'thread/turns/list' for m, _ in calls))

    async def test_other_history_errors_are_not_hidden_by_fallback(self):
        client = ExactCodex('/unused', 'existing')
        client.call = AsyncMock(side_effect=RpcError('Permission denied'))
        with self.assertRaisesRegex(RpcError, 'Permission denied'):
            [e async for e in client.items('wanted')]
        self.assertEqual(client.call.await_count, 1)


class TopicRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name)
        (self.path / 'access.json').write_text(json.dumps({'dmPolicy': 'allowlist', 'allowFrom': ['123']}))
        self.config = {
            'notification_mode': 'all',
            'owner_id': '123', 'telegram_state_dir': str(self.path),
            'socket_path': str(self.path / 'test.sock'), 'thread_id': 'native-meeting',
            'root_target': 'meeting', 'initial_telegram_offset': 100,
            'targets': [
                {'key': 'meeting', 'name': '会议记录', 'thread_id': 'native-meeting', 'message_thread_id': 11},
                {'key': 'instagram', 'name': 'Instagram 短片', 'thread_id': 'native-instagram', 'message_thread_id': 22},
            ],
        }
        self.router = TopicRouter(self.config, self.path)

    async def asyncTearDown(self):
        self.close_router()
        self.tmp.cleanup()

    def close_router(self):
        if self.router is not None:
            for bridge in self.router.bridges.values():
                bridge.store.db.close()
            self.router.store.db.close()
            self.router = None

    @staticmethod
    def message(update_id, topic_id=None, **extra):
        message = {'from': {'id': 123}, 'chat': {'id': 123, 'type': 'private'},
                   'message_id': update_id + 1000, 'text': 'hello'}
        if topic_id is not None:
            message['message_thread_id'] = topic_id
        message.update(extra)
        return {'update_id': update_id, 'message': message}

    def accept(self, update):
        self.router.ingest(update)
        while self.router.forward_one():
            pass

    def test_owner_topic_routes_and_unconfirmed_main_message_is_held(self):
        for update in [self.message(100), self.message(101, 11), self.message(102, 22)]:
            self.accept(update)
        meeting = self.router.bridges['meeting'].store
        insta = self.router.bridges['instagram'].store
        self.assertEqual([(r['id'], r['message_thread_id']) for r in meeting.db.execute('SELECT * FROM inbox ORDER BY id')],
                         [(101, 11)])
        self.assertEqual([(r['id'], r['message_thread_id']) for r in insta.db.execute('SELECT * FROM inbox')], [(102, 22)])
        self.assertEqual(self.router.store.offset(), 103)
        self.assertEqual(self.router.picker.guard.pending_count(), 1)
        self.assertEqual(self.router.root_target['thread_id'], 'native-meeting')

    def test_unknown_topic_fails_closed_and_notifies_same_topic(self):
        self.accept(self.message(100, 999))
        self.assertTrue(all(b.store.pending() is None for b in self.router.bridges.values()))
        notice = self.router.store.db.execute('SELECT * FROM notices').fetchone()
        self.assertEqual(notice['topic_id'], 999)
        self.assertIn('尚未绑定', notice['text'])

    async def test_session_command_keeps_native_target_and_replies_in_origin_topic(self):
        self.accept(self.message(100, 22, text='/status'))
        child = self.router.bridges['instagram']
        child.codex = type('Native', (), {'settings': AsyncMock(return_value={
            'thread': {'id': 'native-instagram', 'name': 'Instagram 短片'}, 'model': 'test-model'})})()
        await child.dispatch_one(child.store.pending())
        child.codex.settings.assert_awaited_once()
        self.assertEqual(child.store.db.execute('SELECT message_thread_id FROM inbox').fetchone()[0], 22)
        output = child.store.db.execute('SELECT * FROM outbox').fetchone()
        self.assertEqual(output['message_thread_id'], 22)
        self.assertIn('native-instagram', output['text'])
        self.assertIn('test-model', output['text'])
        self.assertIsNone(self.router.bridges['meeting'].store.pending())

    async def test_global_help_from_unknown_topic_is_answered_in_main_without_generation(self):
        self.accept(self.message(100, 999, text='/help'))
        child = self.router.bridges['meeting']
        child.codex = type('NoNativeCalls', (), {'call': AsyncMock(side_effect=AssertionError('No generation'))})()
        await child.dispatch_one(child.store.pending())
        child.codex.call.assert_not_awaited()
        output = child.store.db.execute('SELECT * FROM outbox').fetchone()
        self.assertIsNone(output['message_thread_id'])
        self.assertIn('/sessions', output['text'])

    def test_unknown_topic_session_command_fails_in_origin_without_retargeting(self):
        for i, command in enumerate(['/model test', '/effort high', '/stop', '/status', '/format rich'], 100):
            self.accept(self.message(i, 999, text=command))
        self.assertTrue(all(b.store.pending() is None for b in self.router.bridges.values()))
        notices = self.router.store.db.execute('SELECT * FROM notices').fetchall()
        self.assertEqual(len(notices), 5)
        self.assertTrue(all(r['topic_id'] == 999 and '尚未绑定' in r['text'] for r in notices))

    def test_command_like_attachment_caption_retains_normal_reply_topic(self):
        self.accept(self.message(100, 22, text='', caption='/status', document={'file_id': 'a'}))
        child = self.router.bridges['instagram']
        child.store.enqueue('input:100:reply', '123', '附件处理失败')
        self.assertEqual(child.store.db.execute('SELECT message_thread_id FROM outbox').fetchone()[0], 22)

    def test_unauthorized_senders_groups_and_topic_service_messages_are_not_forwarded(self):
        for update in [self.message(100, 11, **{'from': {'id': 999}}),
                       self.message(101, 11, chat={'id': -456, 'type': 'supergroup'}),
                       self.message(102, 11, forum_topic_created={'name': 'test'}),
                       self.message(103, 11, text='', pinned_message={'message_id': 50, 'text': '工作状态'})]:
            self.accept(update)
        self.assertEqual(self.router.store.db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0], 0)
        self.assertEqual(self.router.store.offset(), 104)

    def test_revoked_owner_is_denied_before_queue_handoff(self):
        self.router.ingest(self.message(100, 11))
        (self.path / 'access.json').write_text(json.dumps({'dmPolicy': 'allowlist', 'allowFrom': []}))
        self.assertTrue(self.router.forward_one())
        self.assertIsNone(self.router.bridges['meeting'].store.pending())
        self.assertEqual(self.router.store.db.execute('SELECT state FROM jobs').fetchone()[0], 'denied')

    def test_acceptance_snapshot_survives_restart_and_duplicate_handoff(self):
        update = self.message(100, 22)
        self.router.ingest(update)
        child = self.router.bridges['instagram']
        # Simulate crash after child commit but before router acknowledgement.
        child.store.accept({'update_id': 100}, update['message'])
        child.store.mark(100, 'running', 'already-accepted-turn')
        self.close_router()
        self.router = TopicRouter(self.config, self.path)
        self.router.forward_one()
        self.router.ingest(update)
        self.assertFalse(self.router.forward_one())
        row = self.router.bridges['instagram'].store.db.execute('SELECT * FROM inbox').fetchone()
        self.assertEqual((row['state'], row['turn_id'], row['message_thread_id']), ('running', 'already-accepted-turn', 22))

    async def test_reply_context_uses_original_input_topic_without_retargeting_parent_topic(self):
        self.accept(self.message(100, 22, text='修改这里', reply_to_message={
            'message_id': 80, 'message_thread_id': 11, 'text': '另一个话题的被引用内容'}))
        child = self.router.bridges['instagram']
        child.codex = type('Native', (), {'call': AsyncMock(return_value={'turn': {'id': 'quoted-turn'}})})()
        await child.dispatch_one(child.store.pending())
        method, params = child.codex.call.call_args.args
        self.assertEqual((method, params['threadId']), ('turn/start', 'native-instagram'))
        self.assertTrue(params['input'][0]['text'].startswith('修改这里\n\n［引用内容］'))
        self.assertIn('> 另一个话题的被引用内容', params['input'][0]['text'])
        context = json.loads(child.store.db.execute('SELECT reply_json FROM inbox').fetchone()[0])
        self.assertEqual((context['reply_to_message_id'], context['reply_to_thread_id']), (80, 11))
        self.assertIsNone(self.router.bridges['meeting'].store.pending())

    def test_changed_native_binding_blocks_accepted_work(self):
        self.router.ingest(self.message(100, 22))
        self.router.bridges['instagram'].thread_id = 'other-thread'
        self.router.forward_one()
        self.assertIsNone(self.router.bridges['instagram'].store.pending())
        self.assertEqual(self.router.store.db.execute('SELECT state FROM jobs').fetchone()[0], 'blocked')

    def test_album_cannot_cross_target_or_topic(self):
        self.accept(self.message(100, 11, media_group_id='album', photo=[{'file_id': 'one'}]))
        self.accept(self.message(101, 22, media_group_id='album', photo=[{'file_id': 'two'}]))
        self.assertIsNone(self.router.bridges['instagram'].store.pending())
        self.assertIn('相册目标', self.router.store.db.execute('SELECT text FROM notices').fetchone()[0])

    async def test_real_dispatch_uses_each_existing_thread_and_retains_media_topic(self):
        requests = []

        class StubCodex:
            async def call(self, method, params):
                requests.append((method, params))
                return {'turn': {'id': 'turn-' + params['threadId']}}

        for i, (key, topic) in enumerate([('meeting', 11), ('instagram', 22)], 100):
            self.accept(self.message(i, topic))
            child = self.router.bridges[key]
            child.codex = StubCodex()
            await child.dispatch_one(child.store.pending())
        self.assertEqual([p['threadId'] for _, p in requests], ['native-meeting', 'native-instagram'])
        self.assertTrue(all(method == 'turn/start' for method, _ in requests))
        self.assertTrue(all(set(p) == {'threadId', 'input', 'clientUserMessageId'} for _, p in requests))

    async def test_interleaved_outputs_keep_topics_after_configuration_edit(self):
        sends = []

        class StubTelegram:
            async def call(self, method, **params):
                sends.append((method, params))
                return {'message_id': 800 + len(sends)}

        tasks = []
        try:
            for i, (key, topic) in enumerate([('instagram', 22), ('meeting', 11)], 100):
                self.accept(self.message(i, topic))
                child = self.router.bridges[key]
                child.store.mark(i, 'running', 'turn-' + key)
                child.record_item('turn-' + key, {'type': 'agentMessage', 'id': 'a', 'text': key}, now_ms() + 1)
                child.target['message_thread_id'] = 999  # Output reads the queue snapshot, never current config.
                child.telegram = StubTelegram()
                tasks.append(asyncio.create_task(child.sender_loop()))
            for _ in range(30):
                if len(sends) == 2:
                    break
                await asyncio.sleep(0.01)
            self.assertEqual({p['message_thread_id'] for _, p in sends}, {11, 22})
            self.assertIn('instagram', next(p.get('text') or p['rich_message']['markdown']
                                           for _, p in sends if p['message_thread_id'] == 22))
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def test_recovery_replays_only_original_topic_without_reexecution(self):
        self.accept(self.message(100, 22))
        child = self.router.bridges['instagram']
        since = child.store.pending()['received_ms']
        child.store.mark(100, 'dispatching')

        class RecoveredCodex:
            async def turns(self, limit=30):
                return [{'id': 'old-turn', 'status': 'completed'}]

            async def items(self, turn_id):
                yield {'item': {'type': 'userMessage', 'clientId': 'telegram-100'}}
                yield {'completedAtMs': since + 10, 'item': {'type': 'agentMessage', 'id': 'a', 'text': 'Recovered'}}

        child.codex = RecoveredCodex()
        await child.reconcile(replay=True)
        await child.reconcile(replay=True)
        self.assertEqual(child.store.db.execute('SELECT state FROM inbox').fetchone()[0], 'done')
        rows = child.store.db.execute('SELECT * FROM outbox').fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['message_thread_id'], 22)
        self.assertEqual(self.router.bridges['meeting'].store.counts()['unsent_messages'], 0)

    async def test_status_and_stop_are_scoped_to_selected_topic(self):
        calls = []
        values = {'model': 'existing-model', 'reasoningEffort': 'high'}

        class StubCodex:
            async def settings(self):
                return {'thread': {'id': 'native-instagram', 'name': 'Instagram', 'status': {'type': 'active'}},
                        **values}

            async def models(self):
                return [{'id': model, 'model': model, 'defaultReasoningEffort': 'high',
                         'supportedReasoningEfforts': [{'reasoningEffort': 'low'}, {'reasoningEffort': 'high'}]}
                        for model in ('existing-model', 'alternate-model')]

            async def turns(self, limit=1):
                return [{'id': 'in-progress', 'status': 'inProgress'}]

            async def call(self, method, params):
                calls.append((method, params))
                if method == 'thread/settings/update':
                    if 'model' in params:
                        values['model'] = params['model']
                    if 'effort' in params:
                        values['reasoningEffort'] = params['effort']
                return {}

        child = self.router.bridges['instagram']
        child.codex = StubCodex()
        commands = ['/status', '/stop', '/model', '/effort', '/format', '/model unavailable',
                    '/effort invalid', '/format invalid', '/model alternate-model', '/effort low',
                    '/model@test_bot', '/unknown']
        for i, text in enumerate(commands, 100):
            self.accept(self.message(i, 22, text=text))
            await child.dispatch_one(child.store.pending())
        replies = '\n'.join(r['text'] for r in child.store.db.execute('SELECT * FROM outbox'))
        self.assertIn('native-instagram', replies)
        self.assertEqual(calls, [
            ('turn/interrupt', {'threadId': 'native-instagram', 'turnId': 'in-progress'}),
            ('thread/settings/update', {'threadId': 'native-instagram', 'model': 'alternate-model', 'effort': 'high'}),
            ('thread/settings/update', {'threadId': 'native-instagram', 'effort': 'low'})])
        self.assertEqual(values, {'model': 'alternate-model', 'reasoningEffort': 'low'})
        self.assertIn('模型不可用', replies)
        self.assertIn('不支持 effort', replies)
        self.assertIn('用法：/format', replies)
        self.assertTrue(all(r[0] == 22 for r in child.store.db.execute('SELECT message_thread_id FROM outbox')))
        self.assertEqual(self.router.bridges['meeting'].store.counts()['unsent_messages'], 0)
        self.accept(self.message(200, None, text='/format invalid'))
        root = self.router.bridges['meeting']
        await root.dispatch_one(root.store.pending())
        self.assertIsNone(root.store.db.execute('SELECT message_thread_id FROM outbox').fetchone()[0])


if __name__ == '__main__':
    unittest.main()
