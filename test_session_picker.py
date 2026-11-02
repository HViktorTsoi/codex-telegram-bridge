import asyncio
import copy
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import AsyncMock

from bridge import TelegramError
from session_picker import BindingError, topic_missing_error
from topics_bridge import TopicRouter


class PickerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name)
        self.config = {'owner_id': '123', 'telegram_state_dir': str(self.path),
                       'socket_path': str(self.path / 'native.sock'), 'thread_id': 'meeting-native',
                       'expected_bot_username': 'test_bot', 'mode': 'topics', 'host_id': 'zbox',
                       'root_target': 'meeting', 'targets': [
                           {'key': 'meeting', 'name': '会议记录', 'thread_id': 'meeting-native', 'message_thread_id': 11},
                           {'key': 'instagram', 'name': 'Instagram', 'thread_id': 'instagram-native', 'message_thread_id': 22}]}
        (self.path / 'config.json').write_text(json.dumps(self.config))
        (self.path / 'access.json').write_text(json.dumps({'dmPolicy': 'allowlist', 'allowFrom': ['123']}))
        self.router = TopicRouter(copy.deepcopy(self.config), self.path)
        self.router.bot_id = 444
        self.router.verify_existing = AsyncMock(side_effect=lambda tid: {'id': tid, 'name': '新会话', 'cwd': '/work'})
        self.sequence = 100
        self.native_calls = []
        tests = self

        class Catalog:
            async def call(self, method, params):
                tests.native_calls.append((method, params))
                if method == 'thread/list':
                    if params.get('cursor'):
                        return {'data': [{'id': 'new-page-2', 'name': '第二页会话', 'cwd': '/work/新项目'},
                                         {'id': 'missing-cwd', 'name': '目录未提供'}]}
                    return {'data': [{'id': 'new-native', 'name': '新会话', 'cwd': '/work/新项目'},
                                     {'id': 'meeting-native', 'name': '会议记录', 'cwd': '/work/meeting-native'}], 'nextCursor': 'second-page'}
                if method == 'thread/read':
                    return {'thread': {'id': params['threadId'], 'name': '新会话', 'cwd': '/work/' + params['threadId']}}
                raise AssertionError('Picker must never generate/fork a conversation: ' + method)

        class Telegram:
            def __init__(self):
                self.calls, self.messages, self.created = [], {}, []
                self.next_id = 200
                self.create_error = None

            async def call(self, method, **params):
                self.calls.append((method, params))
                if method == 'answerCallbackQuery':
                    return True
                if method == 'createForumTopic':
                    if self.create_error:
                        raise self.create_error
                    self.created.append(params)
                    return {'message_thread_id': 900 + len(self.created), 'name': params['name']}
                if method == 'sendMessage':
                    self.next_id += 1
                    msg = {**params, 'message_id': self.next_id, 'from': {'id': 444, 'is_bot': True},
                           'chat': {'id': 123, 'type': 'private'}, 'date': 1}
                    self.messages[self.next_id] = msg
                    return msg
                if method == 'editMessageText':
                    msg = {**self.messages[params['message_id']], **params}
                    self.messages[params['message_id']] = msg
                    return msg
                raise AssertionError(method)

        self.telegram = Telegram()
        self.router.telegram = self.telegram
        root = self.router.bridges['meeting']
        root.ready.set()
        root.codex = Catalog()

    async def asyncTearDown(self):
        for bridge in self.router.bridges.values():
            bridge.store.db.close()
        self.router.store.db.close()
        self.tmp.cleanup()

    async def menu(self, topic_id=11):
        self.sequence += 1
        msg = {'message_id': self.sequence, 'from': {'id': 123},
               'chat': {'id': 123, 'type': 'private'}, 'text': '/sessions'}
        if topic_id:
            msg['message_thread_id'] = topic_id
        self.router.ingest({'update_id': self.sequence, 'message': msg})
        await self.router.picker.handle_one()
        return self.telegram.messages[self.telegram.next_id]

    def callback(self, message, label, sender=123):
        button = next(b for row in message['reply_markup']['inline_keyboard'] for b in row if label in b['text'])
        self.sequence += 1
        return {'update_id': self.sequence, 'callback_query': {'id': str(self.sequence), 'from': {'id': sender},
                'message': copy.deepcopy(message), 'data': button['callback_data']}}

    async def click(self, message, label, **kwargs):
        update = self.callback(message, label, **kwargs)
        self.router.ingest(update)
        await self.router.picker.handle_one()
        return self.telegram.messages[message['message_id']]

    async def confirmation(self):
        menu = await self.menu()
        catalog = await self.click(menu, '添加会话')
        return await self.click(catalog, '新会话')

    async def test_menu_and_pagination_are_control_events_without_native_turns(self):
        menu = await self.menu()
        self.assertNotIn('message_thread_id', menu)
        self.assertIn('1. 会议记录', menu['text'])
        self.assertIn('目录：/work/meeting-native', menu['text'])
        self.assertIn('目录：/work/instagram-native', menu['text'])
        self.assertIsNone(self.router.bridges['meeting'].store.pending())
        catalog = await self.click(menu, '添加会话')
        self.assertIn('新会话', str(catalog['reply_markup']))
        self.assertEqual(catalog['text'].count('/work/新项目'), 1)
        self.assertIn('目录 1/3', catalog['text'])
        self.assertIn('本目录 2 个会话', catalog['text'])
        self.assertIn('第二页会话', str(catalog['reply_markup']))
        self.assertNotIn('会议记录', str(catalog['reply_markup']))
        self.assertNotIn('📁', str(catalog['reply_markup']))
        second = await self.click(catalog, '下个目录')
        self.assertIn('/work/meeting-native', second['text'])
        self.assertIn('会议记录', str(second['reply_markup']))
        missing = await self.click(second, '下个目录')
        self.assertIn('📁 未提供', missing['text'])
        second = await self.click(missing, '上个目录')
        first = await self.click(second, '上个目录')
        self.assertIn('新会话', str(first['reply_markup']))
        catalog_calls = [(m, p) for m, p in self.native_calls if m == 'thread/list']
        self.assertEqual(len(catalog_calls), 2)
        self.assertEqual(catalog_calls[1][1]['cursor'], 'second-page')
        self.assertTrue(all(m in ('thread/list', 'thread/read') for m, _ in self.native_calls))
        self.assertTrue(all(len(b['callback_data'].encode()) <= 64 for row in first['reply_markup']['inline_keyboard'] for b in row))
        self.assertEqual(self.telegram.created, [])

    async def test_directory_subpages_keep_selection_and_return_position(self):
        menu = await self.menu()
        threads = [{'id': f'shared-{i}', 'name': f'项目{i}', 'cwd': '/work/shared'} for i in range(1, 14)]
        threads.append({'id': 'other-directory', 'name': '其他目录', 'cwd': '/work/other'})
        async def metadata(method, params):
            if method == 'thread/list':
                return {'data': threads}
            if method == 'thread/read':
                return {'thread': next(t for t in threads if t['id'] == params['threadId'])}
            raise AssertionError(method)
        self.router.catalog_call = AsyncMock(side_effect=metadata)
        catalog = await self.click(menu, '添加会话')
        self.assertEqual(catalog['text'].count('/work/shared'), 1)
        self.assertIn('会话第 1/2 页', catalog['text'])
        self.assertNotIn('项目13', str(catalog['reply_markup']))
        second = await self.click(catalog, '下一页')
        self.assertIn('会话第 2/2 页', second['text'])
        confirmation = await self.click(second, '项目13')
        self.assertIn('shared-13', confirmation['text'])
        returned = await self.click(confirmation, '返回会话列表')
        self.assertIn('会话第 2/2 页', returned['text'])
        self.assertIn('项目13', str(returned['reply_markup']))
        other = await self.click(returned, '下个目录')
        self.assertIn('/work/other', other['text'])
        self.assertNotIn('项目13', str(other['reply_markup']))
        self.assertEqual(sum(call.args[0] == 'thread/list' for call in self.router.catalog_call.call_args_list), 1)

    async def test_directory_snapshot_and_buttons_survive_restart_without_refetch(self):
        menu = await self.menu()
        catalog = await self.click(menu, '添加会话')
        original = self.router
        reloaded = TopicRouter(copy.deepcopy(self.config), self.path)
        reloaded.bot_id, reloaded.telegram = 444, self.telegram
        reloaded.catalog_call = AsyncMock(side_effect=AssertionError('Paging must reuse the saved directory snapshot'))
        self.router = reloaded
        try:
            other = await self.click(catalog, '下个目录')
            self.assertIn('/work/meeting-native', other['text'])
            self.assertIn('会议记录', str(other['reply_markup']))
            reloaded.catalog_call.assert_not_awaited()
        finally:
            for bridge in reloaded.bridges.values():
                bridge.store.db.close()
            reloaded.store.db.close()
            self.router = original

    async def test_unknown_topic_sessions_menu_is_sent_to_main_chat(self):
        menu = await self.menu(topic_id=999)
        self.assertNotIn('message_thread_id', menu)
        catalog = await self.click(menu, '添加会话')
        self.assertEqual(catalog['message_id'], menu['message_id'])
        self.assertNotIn('message_thread_id', catalog)
        self.assertEqual(self.telegram.created, [])

    async def test_legacy_topic_callback_sends_new_main_panel(self):
        menu = await self.menu()
        legacy = self.telegram.messages[menu['message_id']]
        legacy['message_thread_id'] = 11
        self.router.store.db.execute('UPDATE ui_buttons SET topic_id=11')
        self.router.store.db.commit()
        self.router.ingest(self.callback(legacy, '添加会话'))
        await self.router.picker.handle_one()
        migrated = self.telegram.messages[self.telegram.next_id]
        self.assertNotEqual(migrated['message_id'], menu['message_id'])
        self.assertNotIn('message_thread_id', migrated)
        self.assertFalse(any(method == 'editMessageText' for method, _ in self.telegram.calls))
        await self.click(migrated, '返回')
        self.assertEqual(self.telegram.created, [])

    async def test_confirmation_binds_exact_id_atomically_and_activates_without_restart(self):
        menu = await self.menu()
        catalog = await self.click(menu, '添加会话')
        confirmation = await self.click(catalog, '新会话')
        self.assertIn('new-native', confirmation['text'])
        self.assertEqual(self.telegram.created, [])
        started = []
        self.router.running = True
        self.router.start_target = lambda bridge: started.append(bridge.thread_id)
        result = await self.click(confirmation, '确认绑定')
        self.assertIn('已绑定', result['text'])
        self.assertEqual(len(self.telegram.created), 1)
        self.assertEqual(started, ['new-native'])
        persisted = json.loads((self.path / 'config.json').read_text())
        self.assertEqual(persisted['targets'][:2], self.config['targets'])
        target = persisted['targets'][2]
        self.assertEqual((target['thread_id'], target['message_thread_id']), ('new-native', 901))
        self.assertEqual(self.router.by_topic[901]['thread_id'], 'new-native')
        self.router.verify_existing.assert_awaited_once_with('new-native')
        self.assertEqual(self.router.store.db.execute('SELECT status FROM binding_ops').fetchone()[0], 'active')
        self.assertEqual(persisted['thread_id'], 'meeting-native')

    async def test_double_confirmation_and_existing_selection_never_create_duplicate_topics(self):
        confirmation = await self.confirmation()
        original = self.callback(confirmation, '确认绑定')
        self.router.ingest(original)
        await self.router.picker.handle_one()
        replay = copy.deepcopy(original)
        self.sequence += 1
        replay['update_id'] = self.sequence
        self.router.ingest(replay)
        await self.router.picker.handle_one()
        self.assertEqual(len(self.telegram.created), 1)
        menu = await self.menu()
        catalog = await self.click(menu, '添加会话')
        catalog = await self.click(catalog, '下个目录')
        result = await self.click(catalog, '已绑定 会议记录')
        self.assertIn('已经绑定', result['text'])
        self.assertEqual(len(self.telegram.created), 1)

    async def test_stale_forged_and_wrong_message_callbacks_cannot_bind(self):
        confirmation = await self.confirmation()
        for mutation in ('sender', 'message', 'topic', 'token'):
            with self.subTest(mutation=mutation):
                update = self.callback(confirmation, '确认绑定')
                callback = update['callback_query']
                if mutation == 'sender':
                    callback['from']['id'] = 999
                elif mutation == 'message':
                    callback['message']['message_id'] += 100
                elif mutation == 'topic':
                    callback['message']['message_thread_id'] = 22
                else:
                    callback['data'] = 'ui:made-up-token'
                self.router.ingest(update)
                await self.router.picker.handle_one()
        token = self.callback(confirmation, '确认绑定')['callback_query']['data'][3:]
        with self.router.store.db:
            self.router.store.db.execute('UPDATE ui_buttons SET expires_at=? WHERE token=?', (time.time() - 1, token))
        await self.click(confirmation, '确认绑定')
        self.assertEqual(self.telegram.created, [])
        self.router.verify_existing.assert_not_awaited()

    async def test_revocation_after_receipt_denies_binding(self):
        confirmation = await self.confirmation()
        self.router.ingest(self.callback(confirmation, '确认绑定'))
        (self.path / 'access.json').write_text(json.dumps({'dmPolicy': 'allowlist', 'allowFrom': []}))
        await self.router.picker.handle_one()
        self.assertEqual(self.telegram.created, [])

    async def test_topic_creation_timeout_is_not_blindly_retried(self):
        self.telegram.create_error = ConnectionError('response lost')
        with self.assertRaises(BindingError):
            await self.router.picker.bind('new-native')
        self.telegram.create_error = None
        with self.assertRaisesRegex(BindingError, '尚未确认'):
            await self.router.picker.bind('new-native')
        self.assertEqual(sum(method == 'createForumTopic' for method, _ in self.telegram.calls), 1)

    async def test_crash_after_topic_creation_recovers_config_without_another_topic(self):
        target = {'key': 'recovered', 'name': '恢复会话', 'thread_id': 'recovered-native', 'message_thread_id': 901}
        self.router.picker.operation('recovered-native', target, 'topic_created')
        await self.router.picker.recover()
        self.assertEqual(self.telegram.created, [])
        self.assertEqual(self.router.by_topic[901]['thread_id'], 'recovered-native')
        persisted = json.loads((self.path / 'config.json').read_text())
        self.assertEqual(persisted['targets'][-1], target)
        await self.router.picker.recover()
        self.assertEqual(len(self.router.targets), 3)

    async def test_configuration_conflict_keeps_created_topic_record_for_recovery(self):
        changed = copy.deepcopy(self.config)
        changed['socket_path'] = '/different.sock'
        (self.path / 'config.json').write_text(json.dumps(changed))
        confirmation = await self.confirmation()
        await self.click(confirmation, '确认绑定')
        self.assertEqual(len(self.telegram.created), 1)
        self.assertEqual(self.router.store.db.execute('SELECT status FROM binding_ops').fetchone()[0], 'topic_created')
        self.assertEqual(len(self.router.targets), 2)
        self.assertEqual(json.loads((self.path / 'config.json').read_text())['socket_path'], '/different.sock')

    async def test_native_validation_failure_never_creates_or_forks(self):
        self.router.verify_existing = AsyncMock(side_effect=ValueError('missing conversation'))
        confirmation = await self.confirmation()
        await self.click(confirmation, '确认绑定')
        self.assertEqual(self.telegram.created, [])
        self.assertTrue(all(m in ('thread/list', 'thread/read') for m, _ in self.native_calls))

    async def test_bound_conversation_can_recreate_deleted_topic_with_same_queue_and_native_id(self):
        original_bridge = self.router.bridges['meeting']
        original_store = original_bridge.store
        original_store.accept({'update_id': 1}, {'chat': {'id': 123}, 'message_thread_id': 11, 'text': 'old request'})
        menu = await self.menu()
        catalog = await self.click(menu, '添加会话')
        catalog = await self.click(catalog, '下个目录')
        bound = await self.click(catalog, '已绑定 会议记录')
        confirm = await self.click(bound, '重新创建话题')
        self.assertEqual(self.telegram.created, [])
        self.assertIn('meeting-native', confirm['text'])
        await self.click(confirm, '确认重新创建')
        self.assertEqual(len(self.telegram.created), 1)
        target = self.router.targets['meeting']
        self.assertEqual((target['thread_id'], target['message_thread_id']), ('meeting-native', 901))
        self.assertIs(self.router.bridges['meeting'], original_bridge)
        self.assertIs(original_bridge.store, original_store)
        self.assertEqual(original_store.pending()['message_thread_id'], 11)
        self.assertNotIn(11, self.router.by_topic)
        self.assertEqual(self.router.root_target['message_thread_id'], 901)
        self.assertEqual(json.loads((self.path / 'config.json').read_text())['thread_id'], 'meeting-native')
        # A late failure for the old topic cannot invalidate the replacement.
        self.router.mark_topic_missing('meeting', 11)
        self.assertFalse(self.router.targets['meeting'].get('topic_missing'))
        await self.router.picker.repair('meeting-native', 11)
        self.assertEqual(len(self.telegram.created), 1)

    async def test_missing_topic_flag_survives_restart_and_allows_rebinding(self):
        self.router.mark_topic_missing('meeting', 11)
        self.assertTrue(self.router.targets['meeting']['topic_missing'])
        self.assertNotIn(11, self.router.by_topic)
        persisted = json.loads((self.path / 'config.json').read_text())
        reloaded = TopicRouter(persisted, self.path)
        try:
            self.assertNotIn(11, reloaded.by_topic)
            self.assertTrue(reloaded.targets['meeting']['topic_missing'])
        finally:
            for b in reloaded.bridges.values():b.store.db.close()
            reloaded.store.db.close()
        target = await self.router.picker.bind('meeting-native')
        self.assertEqual(target['message_thread_id'], 901)
        self.assertFalse(target.get('topic_missing'))
        self.assertEqual(len(self.router.targets), 2)

    async def test_failed_delivery_marks_only_explicit_missing_topic_and_preserves_reply(self):
        child = self.router.bridges['meeting']
        child.store.accept({'update_id': 1}, {'chat': {'id': 123}, 'message_thread_id': 11, 'text': 'request'})
        child.store.enqueue('input:1:reply', '123', 'Preserved original reply')

        class MissingTelegram:
            async def call(self, method, **params):
                raise TelegramError(method, 400, 'Bad Request: message thread not found')

        child.telegram = MissingTelegram()
        task = asyncio.create_task(child.sender_loop())
        try:
            for _ in range(30):
                if self.router.targets['meeting'].get('topic_missing'):break
                await asyncio.sleep(.01)
            self.assertTrue(self.router.targets['meeting'].get('topic_missing'))
            row = child.store.db.execute('SELECT message_id,message_thread_id,text FROM outbox').fetchone()
            self.assertEqual(tuple(row), (-1, 11, 'Preserved original reply'))
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    def test_closed_topics_permissions_and_rate_limits_are_not_treated_as_deletion(self):
        self.assertTrue(topic_missing_error(TelegramError('sendMessage', 400, 'message thread not found')))
        for code, text in [(400, 'TOPIC_CLOSED'), (403, 'Forbidden'), (429, 'Too Many Requests'),
                           (500, 'Internal Server Error'), (400, 'chat not found'), (400, 'message is too long')]:
            self.assertFalse(topic_missing_error(TelegramError('sendMessage', code, text)))

    async def test_repair_after_topic_creation_recovers_existing_binding_without_duplicate(self):
        target = {**self.config['targets'][0], 'message_thread_id': 901, 'previous_message_thread_id': 11}
        self.router.picker.operation('meeting-native', target, 'topic_created')
        await self.router.picker.recover()
        self.assertEqual(self.telegram.created, [])
        self.assertEqual(len(self.router.targets), 2)
        self.assertEqual(self.router.targets['meeting']['message_thread_id'], 901)
        self.assertNotIn(11, self.router.by_topic)
        await self.router.picker.recover()
        self.assertEqual(len(self.router.targets), 2)

    async def test_repair_timeout_is_not_retried_and_original_binding_is_retained(self):
        self.telegram.create_error = ConnectionError('response lost')
        with self.assertRaises(BindingError):
            await self.router.picker.repair('meeting-native', 11)
        self.telegram.create_error = None
        with self.assertRaisesRegex(BindingError, '待核对'):
            await self.router.picker.repair('meeting-native', 11)
        self.assertEqual(sum(m == 'createForumTopic' for m, _ in self.telegram.calls), 1)
        self.assertEqual(self.router.targets['meeting']['message_thread_id'], 11)

    async def test_repair_requires_matching_old_topic_and_explicit_validation(self):
        changed = {**self.config['targets'][0], 'message_thread_id': 901}
        with self.assertRaises(ValueError):
            await self.router.install_target(changed)
        with self.assertRaises(ValueError):
            await self.router.install_target(changed, replace_topic=True, expected_topic_id=999)
        self.router.verify_existing = AsyncMock(side_effect=ValueError('native thread missing'))
        with self.assertRaises(ValueError):
            await self.router.picker.repair('meeting-native', 11)
        self.assertEqual(self.telegram.created, [])


if __name__ == '__main__':
    unittest.main()
