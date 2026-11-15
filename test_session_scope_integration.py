"""Ownership changes must invalidate persisted menus, queues and workers."""
import asyncio
import json
import time
import unittest
from unittest.mock import AsyncMock

import test_session_picker as picker_fixtures
import test_session_creator as creator_fixtures
from control_center import export_bundle, put_notice, write_json
from session_scope import SessionScope, SessionScopeError
from topics_bridge import TopicRouter


class ScopeIntegrationTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = picker_fixtures.PickerTests.asyncSetUp
    asyncTearDown = picker_fixtures.PickerTests.asyncTearDown
    menu = picker_fixtures.PickerTests.menu
    callback = picker_fixtures.PickerTests.callback
    click = picker_fixtures.PickerTests.click

    def enable_scope(self, scope='general'):
        self.registry = self.path / 'scope.json'
        self.assignments = {t['thread_id']: scope for t in self.router.targets.values()}
        self.write_scope()
        self.router.config.update(session_scope_file=str(self.registry), session_scope=scope)
        (self.path / 'config.json').write_text(json.dumps(self.router.config))
        self.router.session_scope = SessionScope(self.router.config)
        for bridge in self.router.bridges.values():
            bridge.config.update(session_scope_file=str(self.registry), session_scope=scope)
            bridge.session_scope = SessionScope(bridge.config)

    def write_scope(self):
        write_json(self.registry, {'version': 1, 'scopes': ['general', 'research', 'development'],
                                  'default_scope': 'general', 'assignments': self.assignments})

    def moved(self, thread_id):
        self.assignments[thread_id] = 'research'
        self.write_scope()

    async def test_old_catalog_selection_and_binding_recovery_cannot_restore_moved_threads(self):
        self.enable_scope()
        catalog_id, _ = await self.router.picker.new_catalog()
        target = {'key': 'old', 'name': '迁出会话', 'thread_id': 'new-native', 'message_thread_id': 99}
        self.router.picker.operation('new-native', target, 'topic_created')
        self.moved('new-native')
        self.moved('new-page-2')
        menu = await self.menu()
        update = {'message': menu}
        await self.router.picker.catalog(update, {'catalog_id': catalog_id})
        text = str(self.telegram.messages[self.telegram.next_id])
        self.assertNotIn('/work/新项目', text)
        self.assertNotIn('new-native', self.router.picker._directory_values())
        calls = len(self.native_calls)
        for action in (self.router.picker.selected(update, 'new-native'),
                       self.router.picker.bind('new-native')):
            with self.assertRaises(SessionScopeError):
                await action
        self.assertEqual(len(self.native_calls), calls)
        self.router.install_target = AsyncMock()
        await self.router.picker.recover()
        self.router.install_target.assert_not_awaited()
        self.assertEqual(self.telegram.created, [])

    async def test_moved_bound_thread_is_hidden_and_old_dispatch_queue_is_rejected(self):
        self.enable_scope()
        entries = [{'host': 'zbox', 'thread_id': t['thread_id'], 'name': t['name'],
                    'latest_reply': 'cached-' + t['thread_id']} for t in self.router.targets.values()]
        write_json(self.path / 'control/local.json', {'host': 'zbox', 'generated': time.time(), 'entries': entries})
        entry = self.router.control.resolve('instagram')
        origin = {'chat': {'id': 123, 'type': 'private'}}
        await self.router.control.dispatch(origin, 500, entry, 'queued task')
        await self.router.control.synchronize()
        self.moved('instagram-native')
        visible, snapshots = self.router.control.view()
        self.assertEqual([e['thread_id'] for e in visible], ['meeting-native'])
        self.assertNotIn('instagram-native', str(snapshots))
        with self.assertRaises(SessionScopeError):
            await self.router.control.dispatch(origin, 501, entry, 'stale task')
        self.assertTrue(self.router.control.deliver_one())
        row = self.router.store.db.execute('SELECT state FROM cc_deliveries').fetchone()
        self.assertEqual(row['state'], 'failed')
        self.assertIsNone(self.router.bridges['instagram'].store.pending())
        self.assertNotIn('instagram-native', str(export_bundle(self.path)))
        with self.assertRaises(SessionScopeError):
            await self.router.install_target(dict(self.router.targets['instagram']))
        with self.assertRaises(SessionScopeError):
            TopicRouter(self.router.config, self.path)

    async def test_old_directory_button_with_all_threads_moved_shows_a_safe_empty_panel(self):
        self.enable_scope()
        groups = [{'cwd': '/migrating-directory', 'threads': [{'id': 'new-native', 'name': 'moved session'}]}]
        with self.router.store.db:
            self.router.store.db.execute('INSERT INTO ui_catalogs VALUES(?,?,?)',
                ('old-directory', json.dumps(groups), time.time() + 900))
        menu = await self.menu()
        await self.router.picker.directory_index({'message': menu}, 'old-directory', groups)
        directory_menu = self.telegram.messages[self.telegram.next_id]
        self.moved('new-native')
        empty = await self.click(directory_menu, '1. 📁')
        self.assertIn('未找到可绑定会话', empty['text'])
        self.assertNotIn('/migrating-directory', str(empty))
        job = self.router.store.db.execute('SELECT state FROM ui_jobs ORDER BY id DESC LIMIT 1').fetchone()
        self.assertEqual(job['state'], 'done')
        await self.router.picker.directory_index({'message': menu}, 'old-directory', groups, page=20)
        self.assertIn('未找到可绑定会话', self.telegram.messages[self.telegram.next_id]['text'])

    async def test_moved_worker_does_not_dispatch_or_send_queued_outputs(self):
        self.enable_scope()
        bridge = self.router.bridges['instagram']
        bridge.telegram = self.telegram
        bridge.codex.call = AsyncMock()
        bridge.store.accept({'update_id': 700}, {'from': {'id': 123},
            'chat': {'id': 123, 'type': 'private'}, 'message_thread_id': 22, 'text': 'private task'})
        bridge.store.enqueue('queued', '123', 'private output')
        self.moved(bridge.thread_id)
        self.assertFalse(bridge.owner_allowed())
        await bridge.dispatch_one(bridge.store.pending())
        bridge.codex.call.assert_not_awaited()
        self.assertEqual(bridge.store.db.execute('SELECT state FROM inbox').fetchone()['state'], 'denied')
        count = len(self.telegram.calls)
        await bridge.send_one(bridge.store.db.execute('SELECT * FROM outbox LIMIT 1').fetchone())
        self.assertEqual(len(self.telegram.calls), count)

    async def test_metadata_read_that_moves_scope_cannot_render_a_picker_confirmation(self):
        self.enable_scope()

        async def read_then_move(method, params):
            self.assertEqual(method, 'thread/read')
            self.moved(params['threadId'])
            return {'thread': {'id': params['threadId'], 'name': 'private metadata', 'cwd': '/private'}}

        self.router.bridges['meeting'].codex.call = AsyncMock(side_effect=read_then_move)
        update = {'message': {'chat': {'id': 123, 'type': 'private'}, 'message_id': 999}}
        with self.assertRaises(SessionScopeError):
            await self.router.picker.selected(update, 'new-native')
        self.assertEqual(self.telegram.calls, [])

    async def test_missing_registry_blocks_cached_views_and_notices_do_not_leak_after_move(self):
        self.enable_scope()
        with self.router.store.db:
            put_notice(self.router.store.db, 'old-relay', '123', None, 'private cached reply',
                       label='zbox/Instagram', thread_id='instagram-native')
        self.moved('instagram-native')
        task = asyncio.create_task(self.router.notices_loop())
        try:
            await asyncio.sleep(0.02)
        finally:
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertFalse(any(p.get('text') == 'private cached reply' for _, p in self.telegram.calls))
        self.assertEqual(self.router.store.db.execute('SELECT message_id FROM notices').fetchone()[0], -1)
        self.registry.unlink()
        self.assertFalse(self.router.owner_allowed())
        self.assertEqual(self.router.control.view()[0], [])
        with self.assertRaises(SessionScopeError):
            await self.router.picker.new_catalog()


class ScopedCreatorTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = picker_fixtures.PickerTests.asyncSetUp
    asyncTearDown = picker_fixtures.PickerTests.asyncTearDown
    menu = picker_fixtures.PickerTests.menu
    callback = picker_fixtures.PickerTests.callback
    click = picker_fixtures.PickerTests.click
    enable_scope = ScopeIntegrationTests.enable_scope
    write_scope = ScopeIntegrationTests.write_scope
    wizard = creator_fixtures.CreatorTests.wizard
    panel = creator_fixtures.CreatorTests.panel
    answer = creator_fixtures.CreatorTests.answer
    choose_path = creator_fixtures.CreatorTests.choose_path
    review = creator_fixtures.CreatorTests.review

    async def test_new_research_session_is_claimed_before_naming_and_binding(self):
        self.enable_scope('research')
        review = await self.review('新的研究会话')
        native = self.router.catalog_call

        async def checked(method, params):
            if method == 'thread/name/set':
                self.router.session_scope.require(params['threadId'])
            return await native(method, params)

        self.router.catalog_call = AsyncMock(side_effect=checked)
        await self.click(review, '确认新建并连接')
        assignments = json.loads(self.registry.read_text())['assignments']
        self.assertEqual(assignments['created-1'], 'research')
        self.assertTrue(any(t['thread_id'] == 'created-1' for t in self.router.targets.values()))
        self.assertEqual(sum(m == 'thread/start' for m, _ in self.create_calls), 1)
