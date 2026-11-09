"""Global session menus stay responsive while native/UI work is pending."""
import asyncio
import copy
import json
import unittest
from unittest.mock import AsyncMock

from bridge import RpcError
import test_session_picker as fixture
from topics_bridge import TopicRouter


class SessionsResponsivenessTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixture.PickerTests.asyncSetUp
    asyncTearDown = fixture.PickerTests.asyncTearDown
    menu = fixture.PickerTests.menu
    callback = fixture.PickerTests.callback
    click = fixture.PickerTests.click

    def incoming(self, text='/sessions', topic=None, **extra):
        self.sequence += 1
        message = {'message_id': self.sequence, 'from': {'id': 123},
                   'chat': {'id': 123, 'type': 'private'}, 'text': text}
        if topic is not None:
            message['message_thread_id'] = topic
        message.update(extra)
        return {'update_id': self.sequence, 'message': message}

    def job(self, update):
        return self.router.store.db.execute('SELECT * FROM ui_jobs WHERE id=?', (update['update_id'],)).fetchone()

    async def home_once(self):
        return await asyncio.wait_for(self.router.picker.handle_one(lane='home'), 1)

    async def test_cached_menu_opens_in_all_while_native_and_dispatch_locks_are_busy(self):
        entered, release = asyncio.Event(), asyncio.Event()
        other = self.router.bridges['instagram']
        other.ready.set()
        other.store.accept({'update_id': 900}, {'message_id': 900, 'chat': {'id': 123},
                                                'message_thread_id': 22, 'text': '正在运行的另一个任务'})
        other.store.mark(900, 'running', 'active-instagram-turn')

        async def busy_native(method, params):
            self.assertEqual(method, 'thread/turns/list')
            entered.set()
            await release.wait()
            return {'data': []}

        self.router.catalog_call = AsyncMock(side_effect=busy_native)
        native_work = asyncio.create_task(self.router.catalog_call('thread/turns/list', {'threadId': 'instagram-native'}))
        await asyncio.wait_for(entered.wait(), 1)
        root, other = self.router.bridges['meeting'], self.router.bridges['instagram']
        await root.dispatch_lock.acquire()
        await other.dispatch_lock.acquire()
        try:
            update = self.incoming()
            self.router.ingest(update)
            self.assertTrue(await self.home_once())
            panel = self.telegram.messages[self.telegram.next_id]
            self.assertNotIn('message_thread_id', panel)
            self.assertIn('目录：/work/meeting-native', panel['text'])
            self.assertIn('目录：/work/instagram-native', panel['text'])
            self.assertIn('添加会话', str(panel['reply_markup']))
            self.assertEqual(self.job(update)['state'], 'done')
            self.assertFalse(release.is_set())
            self.assertFalse(native_work.done())
            self.assertEqual(self.router.catalog_call.await_count, 1)
            self.assertTrue(all(b.store.pending() is None for b in self.router.bridges.values()))
            self.assertEqual(other.store.db.execute('SELECT state FROM inbox WHERE id=900').fetchone()[0], 'running')
        finally:
            root.dispatch_lock.release()
            other.dispatch_lock.release()
            release.set()
            await native_work

    async def test_missing_directory_metadata_does_not_trigger_a_native_read_before_menu(self):
        with self.router.store.db:
            self.router.store.db.execute('DELETE FROM ui_thread_directories')
        self.router.catalog_call = AsyncMock(side_effect=AssertionError('Menu must not read native metadata'))
        update = self.incoming()
        self.router.ingest(update)
        self.assertTrue(await self.home_once())
        panel = self.telegram.messages[self.telegram.next_id]
        self.assertIn('会议记录', panel['text'])
        self.assertIn('Instagram', panel['text'])
        self.assertEqual(panel['text'].count('目录暂不可用'), 2)
        self.router.catalog_call.assert_not_awaited()

    async def test_home_worker_bypasses_an_older_catalog_job_still_processing(self):
        menu = await self.menu(topic_id=None)
        old = self.callback(menu, '添加会话')
        self.router.ingest(old)
        entered, release, sent = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def slow_catalog(method, params):
            self.assertEqual(method, 'thread/list')
            entered.set()
            await release.wait()
            return {'data': []}

        self.router.catalog_call = AsyncMock(side_effect=slow_catalog)
        original_call = self.telegram.call

        async def record_main_panel(method, **params):
            result = await original_call(method, **params)
            if method == 'sendMessage' and '已绑定的原 Codex 会话' in params.get('text', ''):
                sent.set()
            return result

        self.telegram.call = record_main_panel
        actions = asyncio.create_task(self.router.picker.loop())
        homes = asyncio.create_task(self.router.picker.home_loop())
        try:
            await asyncio.wait_for(entered.wait(), 1)
            self.assertEqual(self.job(old)['state'], 'processing')
            new = self.incoming('/sessions@test_bot')
            self.router.ingest(new)
            await asyncio.wait_for(sent.wait(), 1)
            self.assertEqual(self.job(new)['state'], 'done')
            self.assertEqual(self.job(old)['state'], 'processing')
            self.assertFalse(release.is_set())
            self.assertEqual(self.router.catalog_call.await_count, 1)
            panel = self.telegram.messages[self.telegram.next_id]
            self.assertNotIn('message_thread_id', panel)
        finally:
            actions.cancel()
            homes.cancel()
            await asyncio.gather(actions, homes, return_exceptions=True)

    async def test_actions_lane_leaves_direct_sessions_for_home_lane(self):
        update = self.incoming()
        self.router.ingest(update)
        self.assertFalse(await self.router.picker.handle_one(lane='actions'))
        self.assertEqual(self.job(update)['state'], 'pending')
        self.assertEqual(self.telegram.calls, [])
        self.assertTrue(await self.home_once())
        self.assertEqual(self.job(update)['state'], 'done')

    async def test_duplicate_updates_and_competing_home_claims_send_only_one_menu(self):
        update = self.incoming()
        self.router.ingest(update)
        self.router.ingest(copy.deepcopy(update))
        result = await asyncio.gather(self.home_once(), self.home_once())
        self.assertEqual(sorted(result), [False, True])
        self.assertEqual(sum(method == 'sendMessage' for method, _ in self.telegram.calls), 1)
        self.assertEqual(self.job(update)['state'], 'done')

    async def test_unknown_topic_sessions_routes_to_main_without_selecting_any_native_target(self):
        self.router.catalog_call = AsyncMock(side_effect=AssertionError('No native calls for menus'))
        update = self.incoming('/sessions', topic=999)
        self.router.ingest(update)
        self.assertTrue(await self.home_once())
        self.assertNotIn('message_thread_id', self.telegram.messages[self.telegram.next_id])
        self.assertTrue(all(b.store.pending() is None for b in self.router.bridges.values()))
        self.router.catalog_call.assert_not_awaited()

    async def test_revoked_owner_is_denied_before_fast_menu_send(self):
        update = self.incoming()
        self.router.ingest(update)
        (self.path / 'access.json').write_text(json.dumps({'dmPolicy': 'allowlist', 'allowFrom': []}))
        self.assertTrue(await self.home_once())
        self.assertEqual(self.job(update)['state'], 'denied')
        self.assertEqual(self.telegram.calls, [])

    async def test_media_and_creator_or_rename_prompt_replies_do_not_enter_fast_home_lane(self):
        for title in ('新建 Codex 会话 · 输入名称', '会话改名 · 输入新名称'):
            with self.subTest(prompt=title):
                update = self.incoming('/sessions', reply_to_message={
                    'message_id': 701, 'from': {'id': 444}, 'text': title})
                self.router.ingest(update)
                self.assertFalse(await self.home_once())
                self.assertEqual(self.job(update)['state'], 'pending')
        media = self.incoming('/sessions', photo=[{'file_id': 'fixture'}])
        self.router.ingest(media)
        self.assertEqual(self.job(media)['state'], 'pending')
        self.assertEqual(self.router.picker.guard.pending_count(), 1)
        self.assertFalse(await self.home_once())
        self.assertEqual(self.telegram.calls, [])

    async def test_foreign_machine_snapshot_paths_are_not_displayed_in_local_menu(self):
        with self.router.store.db:
            self.router.store.db.execute('DELETE FROM ui_thread_directories')
        control = self.path / 'control'
        control.mkdir(exist_ok=True)
        self.router.catalog_call = AsyncMock(side_effect=AssertionError('No live reads for menu'))
        for snapshot_host in ('other-machine', 'zbox'):
            with self.subTest(snapshot_host=snapshot_host):
                (control / 'local.json').write_text(json.dumps({'host': snapshot_host, 'entries': [
                    {'host': 'other-machine', 'thread_id': 'meeting-native', 'cwd': '/foreign/private-project'}]}))
                update = self.incoming()
                self.router.ingest(update)
                self.assertTrue(await self.home_once())
                panel = self.telegram.messages[self.telegram.next_id]
                self.assertNotIn('/foreign/private-project', panel['text'])
                self.assertIn('目录暂不可用', panel['text'])
        self.router.catalog_call.assert_not_awaited()

    async def test_cached_directory_survives_router_restart_and_native_disconnect(self):
        self.router.picker.cache_directory({'id': 'meeting-native', 'cwd': '/缓存/保存的目录'})
        previous = self.router
        self.router = TopicRouter(copy.deepcopy(self.config), self.path)
        self.router.telegram, self.router.bot_id = self.telegram, 444
        self.router.catalog_call = AsyncMock(side_effect=ConnectionError('Native unavailable'))
        for bridge in previous.bridges.values():
            bridge.store.db.close()
        previous.store.db.close()
        update = self.incoming()
        self.router.ingest(update)
        self.assertTrue(await self.home_once())
        self.assertIn('/缓存/保存的目录', self.telegram.messages[self.telegram.next_id]['text'])
        self.router.catalog_call.assert_not_awaited()

    async def test_display_cache_never_replaces_fresh_native_binding_validation(self):
        self.router.picker.cache_directory({'id': 'new-native', 'cwd': '/缓存/旧目录'})
        self.router.verify_existing = AsyncMock(side_effect=RpcError('Cannot verify current native identity'))
        with self.assertRaises(RpcError):
            await self.router.picker.bind('new-native')
        self.router.verify_existing.assert_awaited_once_with('new-native')
        self.assertEqual(self.telegram.created, [])
        self.assertEqual(set(self.router.targets), {'meeting', 'instagram'})


if __name__ == '__main__':
    unittest.main()
