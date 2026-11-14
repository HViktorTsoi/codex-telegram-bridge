"""Known Telegram topic refusals preserve sessions and require a manual retry."""
import asyncio
import copy
import json
import time
import unittest

from bridge import TelegramError
from session_picker import (BindingError, TopicCreationUnavailable,
                            TOPIC_CREATION_COOLDOWN_KEY)
import test_session_creator as fixtures
from topics_bridge import TopicRouter


class TopicCreationUnavailableTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.CreatorTests.asyncSetUp
    asyncTearDown = fixtures.CreatorTests.asyncTearDown
    menu = fixtures.CreatorTests.menu
    callback = fixtures.CreatorTests.callback
    click = fixtures.CreatorTests.click
    wizard = fixtures.CreatorTests.wizard
    panel = fixtures.CreatorTests.panel
    answer = fixtures.CreatorTests.answer
    choose_path = fixtures.CreatorTests.choose_path
    review = fixtures.CreatorTests.review

    @staticmethod
    def refusal():
        return TelegramError('createForumTopic', 400, 'Bad Request: BOT_TOPIC_WRONG_HOME')

    def creation_count(self):
        return sum(method == 'createForumTopic' for method, _ in self.telegram.calls)

    def native_start_count(self):
        return sum(method == 'thread/start' for method, _ in self.create_calls)

    def cooldown(self):
        return self.router.store.db.execute('SELECT key,value FROM kv WHERE key LIKE ?',
                                            (TOPIC_CREATION_COOLDOWN_KEY + '%',)).fetchone()

    def expire_cooldown(self):
        row = self.cooldown()
        saved = json.loads(row['value'])
        saved['until'] = time.time() - 1
        with self.router.store.db:
            self.router.store.db.execute('UPDATE kv SET value=? WHERE key=?', (json.dumps(saved), row['key']))

    async def fail_new(self, name='保留的新会话'):
        review = await self.review(name)
        self.telegram.create_error = self.refusal()
        await self.click(review, '确认新建并连接')
        return self.panel()

    async def test_named_session_is_preserved_with_retry_panel_and_safe_refusal_code(self):
        panel = await self.fail_new('Skyrim 项目')
        draft = self.router.store.db.execute('SELECT * FROM ui_new_drafts').fetchone()
        self.assertEqual((draft['state'], draft['thread_id'], draft['name']), ('named', 'created-1', 'Skyrim 项目'))
        self.assertIn('会话已创建并保留', panel['text'])
        self.assertIn('Telegram 暂时无法', panel['text'])
        self.assertIn('Skyrim 项目', panel['text'])
        self.assertIn(str(self.path), panel['text'])
        self.assertIn('created-1', panel['text'])
        self.assertNotIn('话题功能', panel['text'])
        self.assertNotIn('Threaded', panel['text'])
        buttons = [b for row in panel['reply_markup']['inline_keyboard'] for b in row]
        retry = next(b for b in buttons if b['text'] == '重试连接')
        stored = self.router.store.db.execute('SELECT action,data FROM ui_buttons WHERE token=?',
                                               (retry['callback_data'][3:],)).fetchone()
        self.assertEqual(stored['action'], 'new:resume')
        self.assertEqual(json.loads(stored['data'])['id'], draft['id'])
        operation = self.router.store.db.execute('SELECT * FROM binding_ops WHERE thread_id=?', ('created-1',)).fetchone()
        self.assertEqual((operation['status'], operation['error']), ('failed', 'BOT_TOPIC_WRONG_HOME'))
        self.assertNotIn('message_thread_id', json.loads(operation['target_json']))
        saved = json.loads(self.cooldown()['value'])
        self.assertEqual(saved['route'], [444, '123'])
        self.assertEqual(saved['code'], 'BOT_TOPIC_WRONG_HOME')
        self.assertGreater(saved['until'], time.time() + 295)
        self.assertLessEqual(saved['until'], time.time() + 300)
        self.assertEqual((self.native_start_count(), self.creation_count()), (1, 1))
        self.assertEqual(len(self.router.targets), 2)

    async def test_repeated_manual_clicks_during_cooldown_make_no_more_topic_or_native_requests(self):
        await self.fail_new()
        self.telegram.create_error = None  # The local gate must prevent even a successful request.
        for _ in range(3):
            await self.click(self.panel(), '重试连接')
            self.assertIn('暂时无法', self.panel()['text'])
        self.assertEqual((self.native_start_count(), self.creation_count()), (1, 1))
        self.assertEqual(self.router.store.db.execute('SELECT state FROM ui_new_drafts').fetchone()[0], 'named')
        self.assertEqual(self.telegram.created, [])

    async def test_expired_cooldown_manual_retry_binds_the_same_native_session_once(self):
        await self.fail_new()
        self.expire_cooldown()
        self.telegram.create_error = None
        await self.click(self.panel(), '重试连接')
        target = next(t for t in self.router.targets.values() if t['thread_id'] == 'created-1')
        self.assertEqual(target['message_thread_id'], 901)
        self.assertEqual(self.native_start_count(), 1)
        self.assertEqual(self.creation_count(), 2)
        self.assertEqual(len(self.telegram.created), 1)
        self.assertEqual(self.router.store.db.execute('SELECT state FROM ui_new_drafts').fetchone()[0], 'done')

    async def test_cooldown_and_named_retry_survive_a_router_restart(self):
        await self.fail_new()
        original = self.router
        reloaded = TopicRouter(copy.deepcopy(self.config), self.path)
        reloaded.bot_id, reloaded.telegram = 444, self.telegram
        reloaded.catalog_call, reloaded.verify_existing = original.catalog_call, original.verify_existing
        self.router = reloaded
        try:
            await reloaded.picker.recover()
            self.telegram.create_error = None
            await self.click(self.panel(), '重试连接')
            self.assertIn('暂时无法', self.panel()['text'])
            self.assertEqual((self.native_start_count(), self.creation_count()), (1, 1))
            self.assertEqual(reloaded.store.db.execute('SELECT state FROM ui_new_drafts').fetchone()[0], 'named')
        finally:
            for bridge in reloaded.bridges.values():
                bridge.store.db.close()
            reloaded.store.db.close()
            self.router = original

    async def test_known_cooldown_prevents_a_second_empty_session_before_thread_start(self):
        await self.fail_new('原会话保留')
        review = await self.review('稍后再创建')
        self.assertEqual(self.create_calls, [])
        await self.click(review, '确认新建并连接')
        self.assertEqual(self.create_calls, [])
        self.assertEqual(self.creation_count(), 1)
        drafts = self.router.store.db.execute('SELECT * FROM ui_new_drafts ORDER BY updated_at').fetchall()
        self.assertEqual(len(drafts), 2)
        pending = next(d for d in drafts if d['thread_id'] is None)
        self.assertEqual((pending['state'], pending['name']), ('review', '稍后再创建'))
        self.assertIn('本次尚未新建', self.panel()['text'])
        self.assertIn('名称和目录已保留', self.panel()['text'])
        self.assertIn('确认新建并连接', str(self.panel()['reply_markup']))

    async def test_timeout_and_server_error_stay_uncertain_without_a_retry_cooldown(self):
        for index, error in enumerate((asyncio.TimeoutError(), TelegramError('createForumTopic', 503, 'Unavailable'))):
            tid = 'unconfirmed-' + str(index)
            self.telegram.create_error = error
            with self.assertRaises(BindingError) as caught:
                await self.router.picker.bind(tid)
            self.assertNotIsInstance(caught.exception, TopicCreationUnavailable)
            row = self.router.store.db.execute('SELECT status FROM binding_ops WHERE thread_id=?', (tid,)).fetchone()
            self.assertEqual(row['status'], 'uncertain')
            self.telegram.create_error = None
            with self.assertRaisesRegex(BindingError, '尚未确认'):
                await self.router.picker.bind(tid)
        self.assertEqual(self.creation_count(), 2)
        self.assertIsNone(self.cooldown())

    async def test_other_400_keeps_existing_manual_retry_and_does_not_gate_normal_commands(self):
        self.telegram.create_error = TelegramError('createForumTopic', 400, 'Bad Request: invalid name')
        with self.assertRaises(BindingError) as caught:
            await self.router.picker.bind('ordinary-native')
        self.assertNotIsInstance(caught.exception, TopicCreationUnavailable)
        row = self.router.store.db.execute('SELECT * FROM binding_ops WHERE thread_id=?', ('ordinary-native',)).fetchone()
        self.assertEqual((row['status'], row['error']), ('failed', 'Telegram 400'))
        self.assertIsNone(self.cooldown())
        self.telegram.create_error = None
        await self.router.picker.bind('ordinary-native')
        self.assertEqual(self.creation_count(), 2)
        await self.menu()
        self.sequence += 1
        self.router.ingest({'update_id': self.sequence, 'message': {
            'message_id': self.sequence, 'from': {'id': 123}, 'chat': {'id': 123, 'type': 'private'},
            'message_thread_id': 22, 'text': '/stop'}})
        self.assertFalse(self.router.forward_one())
        self.assertIsNone(self.router.bridges['instagram'].store.pending())
        self.assertTrue(await self.router.status_controls.handle_one())
        job = self.router.store.db.execute('SELECT state FROM status_stop_jobs WHERE id=?', (self.sequence,)).fetchone()
        self.assertEqual(job['state'], 'done')
        output = self.router.bridges['instagram'].store.db.execute(
            "SELECT * FROM outbox WHERE key LIKE 'stop-control-result:%'").fetchone()
        self.assertEqual(output['message_thread_id'], 22)
        self.assertIn('未连接', output['text'])

    async def test_cooldown_is_frozen_to_runtime_bot_and_owner_chat(self):
        self.telegram.create_error = self.refusal()
        with self.assertRaises(TopicCreationUnavailable):
            await self.router.picker.bind('blocked-native')
        with self.assertRaises(TopicCreationUnavailable):
            self.router.picker.check_topic_creation_available()
        self.router.bot_id = 555
        self.router.picker.check_topic_creation_available()
        self.router.bot_id, self.router.owner = 444, '999'
        self.router.picker.check_topic_creation_available()
        self.router.owner = '123'
        with self.assertRaises(TopicCreationUnavailable):
            self.router.picker.check_topic_creation_available()

    async def test_existing_topic_repair_reports_refusal_and_keeps_original_binding(self):
        self.telegram.create_error = self.refusal()
        with self.assertRaises(TopicCreationUnavailable) as caught:
            await self.router.picker.repair('meeting-native', 11)
        self.assertIn('暂时无法', str(caught.exception))
        self.assertNotIn('话题功能', str(caught.exception))
        self.assertEqual(self.router.targets['meeting']['message_thread_id'], 11)
        self.assertEqual(len(self.router.targets), 2)
        with self.assertRaises(TopicCreationUnavailable):
            await self.router.picker.repair('meeting-native', 11)
        self.assertEqual(self.creation_count(), 1)


if __name__ == '__main__':
    unittest.main()
