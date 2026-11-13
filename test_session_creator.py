import asyncio
import copy
from pathlib import Path
import time
import unittest
from unittest.mock import AsyncMock

import test_session_picker as fixtures
from bridge import TelegramError
from topics_bridge import TopicRouter


class CreatorTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.PickerTests.asyncSetUp
    asyncTearDown = fixtures.PickerTests.asyncTearDown
    menu = fixtures.PickerTests.menu
    callback = fixtures.PickerTests.callback
    click = fixtures.PickerTests.click

    async def wizard(self):
        old = self.router.catalog_call
        self.created_threads = {}
        self.create_calls = []

        async def native(method, params):
            if method == 'thread/start':
                self.create_calls.append((method, params))
                tid = 'created-' + str(len(self.created_threads) + 1)
                thread = {'id': tid, 'cwd': params['cwd'], 'name': None, 'ephemeral': False}
                self.created_threads[tid] = thread
                return {'thread': thread, 'cwd': params['cwd']}
            if method == 'thread/name/set':
                self.create_calls.append((method, params))
                self.created_threads[params['threadId']]['name'] = params['name']
                return {}
            if method == 'thread/read' and params['threadId'] in self.created_threads:
                return {'thread': self.created_threads[params['threadId']]}
            return await old(method, params)

        self.router.catalog_call = AsyncMock(side_effect=native)
        self.router.verify_existing = AsyncMock(side_effect=lambda tid: self.created_threads[tid])
        menu = await self.menu()
        browser = await self.click(menu, '新建会话')
        self.panel_id = browser['message_id']
        return browser

    def panel(self):
        row = self.router.store.db.execute('SELECT panel_id FROM ui_new_drafts ORDER BY updated_at DESC LIMIT 1').fetchone()
        return self.telegram.messages[row['panel_id']]

    async def answer(self, prompt, text, sender=123, topic=None, media=False):
        self.sequence += 1
        message = {'message_id': self.sequence, 'from': {'id': sender},
                   'chat': {'id': 123, 'type': 'private'}, 'text': text, 'reply_to_message': copy.deepcopy(prompt)}
        if topic:
            message['message_thread_id'] = topic
        if media:
            message['document'] = {'file_id': 'document', 'file_name': 'test.txt'}
        update = {'update_id': self.sequence, 'message': message}
        self.router.ingest(update)
        await self.router.picker.handle_one()
        return update

    async def choose_path(self, browser, path):
        await self.click(browser, '输入路径')
        prompt = self.telegram.messages[self.telegram.next_id]
        await self.answer(prompt, str(path))
        return self.panel()

    async def review(self, name='测试新会话'):
        browser = await self.wizard()
        browser = await self.choose_path(browser, self.path)
        await self.click(browser, '使用当前目录')
        prompt = self.telegram.messages[self.telegram.next_id]
        await self.answer(prompt, name)
        return self.panel()

    async def test_text_name_reply_sends_review_below_input_and_retires_old_panel(self):
        browser = await self.wizard()
        await self.click(browser, '使用当前目录')
        prompt = copy.deepcopy(self.telegram.messages[self.telegram.next_id])
        old = copy.deepcopy(self.panel())
        old_text = old['text']
        update = await self.answer(prompt, 'IOT开发')
        review = copy.deepcopy(self.panel())
        self.assertNotEqual(review['message_id'], old['message_id'])
        self.assertGreater(review['message_id'], update['message']['message_id'])
        self.assertIn('确认新建', review['text'])
        self.assertIn('IOT开发', review['text'])
        self.assertEqual(self.telegram.messages[old['message_id']]['text'], old_text)
        self.assertEqual(self.telegram.messages[old['message_id']]['reply_markup']['inline_keyboard'], [])
        states = self.router.store.db.execute('SELECT state FROM ui_buttons WHERE message_id=?',
                                              (old['message_id'],)).fetchall()
        self.assertTrue(states)
        self.assertTrue(all(row['state'] == 'expired' for row in states))
        self.assertEqual(self.create_calls, [])
        self.assertEqual(self.telegram.created, [])

    async def test_modified_name_uses_new_panel_and_old_confirmation_cannot_create(self):
        old = copy.deepcopy(await self.review('旧名称'))
        await self.click(old, '修改名称')
        prompt = copy.deepcopy(self.telegram.messages[self.telegram.next_id])
        await self.answer(prompt, '新名称')
        latest = copy.deepcopy(self.panel())
        self.assertNotEqual(latest['message_id'], old['message_id'])
        self.assertIn('新名称', latest['text'])
        await self.click(old, '确认新建并连接')
        self.assertEqual(self.create_calls, [])
        self.assertEqual(self.telegram.created, [])
        self.assertEqual(self.router.store.db.execute('SELECT name FROM ui_new_drafts').fetchone()[0], '新名称')
        await self.click(latest, '确认新建并连接')
        self.assertEqual(sum(method == 'thread/start' for method, _ in self.create_calls), 1)

    async def test_home_resume_moves_current_panel_and_rejects_older_active_confirmation(self):
        old = copy.deepcopy(await self.review('恢复同一草稿'))
        menu = await self.menu()
        current = copy.deepcopy(await self.click(menu, '继续处理：恢复同一草稿'))
        self.assertNotEqual(current['message_id'], old['message_id'])
        self.assertEqual(self.panel()['message_id'], current['message_id'])
        self.assertTrue(self.router.store.db.execute("SELECT 1 FROM ui_buttons WHERE message_id=? "
                                                    "AND action='new:confirm' AND state='active'",
                                                    (old['message_id'],)).fetchone())
        await self.click(old, '确认新建并连接')
        self.assertEqual(self.create_calls, [])
        self.assertEqual(self.telegram.created, [])
        await self.click(current, '确认新建并连接')
        self.assertEqual(sum(method == 'thread/start' for method, _ in self.create_calls), 1)
        self.assertEqual(len(self.telegram.created), 1)

    async def test_path_and_folder_text_replies_each_create_a_new_browser_panel(self):
        browser = await self.wizard()
        old_id = browser['message_id']
        browser = await self.choose_path(browser, self.path)
        self.assertNotEqual(browser['message_id'], old_id)
        await self.click(browser, '新建文件夹')
        previous = self.panel()['message_id']
        prompt = copy.deepcopy(self.telegram.messages[self.telegram.next_id])
        await self.answer(prompt, '新的目录')
        self.assertNotEqual(self.panel()['message_id'], previous)
        self.assertIn(str(self.path / '新的目录'), self.panel()['text'])
        self.assertEqual(self.create_calls, [])

    async def test_old_markup_cleanup_failure_does_not_fail_the_new_review(self):
        browser = await self.wizard()
        await self.click(browser, '使用当前目录')
        prompt = copy.deepcopy(self.telegram.messages[self.telegram.next_id])
        previous = self.panel()['message_id']
        original = self.telegram.call

        async def fail_cleanup(method, **params):
            if method == 'editMessageReplyMarkup':
                raise ConnectionError('old card cleanup unavailable')
            return await original(method, **params)

        self.telegram.call = fail_cleanup
        await self.answer(prompt, '清理失败也保留')
        self.assertNotEqual(self.panel()['message_id'], previous)
        self.assertIn('清理失败也保留', self.panel()['text'])
        self.assertEqual(self.router.store.db.execute('SELECT state FROM ui_jobs ORDER BY id DESC LIMIT 1').fetchone()[0], 'done')
        self.assertEqual(self.create_calls, [])

    async def test_unknown_or_rate_limited_review_send_does_not_repeat_input_or_create(self):
        for failure in (ConnectionError('review acknowledgement lost'),
                        TelegramError('sendMessage', 429, 'Too Many Requests', retry_after=30)):
            with self.subTest(failure=type(failure).__name__):
                browser = await self.wizard()
                await self.click(browser, '使用当前目录')
                prompt = copy.deepcopy(self.telegram.messages[self.telegram.next_id])
                previous = copy.deepcopy(self.panel())
                original = self.telegram.call
                attempts = []

                async def fail_review(method, **params):
                    if method == 'sendMessage' and params.get('text', '').startswith('确认新建 Codex'):
                        attempts.append(params)
                        if isinstance(failure, ConnectionError):
                            await original(method, **params)  # Delivered, but ACK lost.
                        raise failure
                    return await original(method, **params)

                self.telegram.call = fail_review
                update = await self.answer(prompt, '待继续-' + type(failure).__name__)
                await self.router.picker.recover()
                self.router.ingest(update)
                await self.router.picker.handle_one()
                self.assertEqual(len(attempts), 1)
                self.assertEqual(self.create_calls, [])
                self.assertEqual(self.telegram.created, [])
                await self.click(previous, '取消')
                self.assertEqual(self.router.store.db.execute('SELECT state FROM ui_new_drafts ORDER BY updated_at DESC LIMIT 1').fetchone()[0], 'review')
                self.telegram.call = original
                menu = await self.menu()
                restored = await self.click(menu, '继续处理：待继续-' + type(failure).__name__)
                self.assertIn('确认新建', restored['text'])
                self.assertIn('待继续-' + type(failure).__name__, restored['text'])
                self.assertEqual(len(attempts), 1)
                self.assertEqual(self.create_calls, [])
                self.assertEqual(self.telegram.created, [])

    async def test_expired_review_is_not_listed_as_a_resumable_draft(self):
        await self.review('已过期确认')
        self.router.store.db.execute('UPDATE ui_new_drafts SET expires_at=?', (time.time() - 1,))
        self.assertEqual(self.router.picker.creator.unfinished('123'), [])

    async def test_create_folder_name_and_exact_new_binding_only_after_confirmation(self):
        browser = await self.wizard()
        (self.path / '已有项目').mkdir()
        browser = await self.choose_path(browser, self.path)
        browser = await self.click(browser, '已有项目')
        self.assertIn(str(self.path / '已有项目'), browser['text'])
        browser = await self.click(browser, '上级目录')
        await self.click(browser, '新建文件夹')
        await self.answer(self.telegram.messages[self.telegram.next_id], '中文 新项目')
        self.assertTrue((self.path / '中文 新项目').is_dir())
        browser = self.panel()
        self.assertIn(str(self.path / '中文 新项目'), browser['text'])
        await self.click(browser, '使用当前目录')
        prompt = self.telegram.messages[self.telegram.next_id]
        self.assertTrue(prompt['reply_markup']['force_reply'])
        await self.answer(prompt, '新的论文会话')
        review = self.panel()
        self.assertEqual(self.create_calls, [])
        self.assertEqual(self.telegram.created, [])
        self.assertIn('确认新建', review['text'])
        self.assertIn('新的论文会话', review['text'])
        update = self.callback(review, '确认新建并连接')
        self.router.ingest(update)
        await self.router.picker.handle_one()
        self.assertEqual(self.create_calls, [
            ('thread/start', {'cwd': str((self.path / '中文 新项目').resolve())}),
            ('thread/name/set', {'threadId': 'created-1', 'name': '新的论文会话'})])
        target = next(t for t in self.router.targets.values() if t['thread_id'] == 'created-1')
        self.assertEqual(target['name'], '新的论文会话')
        self.assertEqual(target['message_thread_id'], 901)
        self.assertEqual(self.router.root_target['thread_id'], 'meeting-native')
        self.assertIsNone(self.router.bridges[target['key']].store.pending())
        # A second durable callback for the old confirmation cannot create another native session.
        update['update_id'] += 1
        self.router.ingest(update)
        await self.router.picker.handle_one()
        self.assertEqual(len(self.created_threads), 1)
        self.assertEqual(len(self.telegram.created), 1)
        self.assertNotIn('message_thread_id', self.panel())

    async def test_prompt_reply_overrides_at_dispatch_but_unrelated_messages_follow_normal_route(self):
        review = await self.review('@paper hello')
        self.assertIn('@paper hello', review['text'])
        self.assertEqual(self.router.store.db.execute('SELECT count(*) FROM cc_requests').fetchone()[0], 0)
        self.assertEqual(self.router.store.db.execute('SELECT count(*) FROM jobs').fetchone()[0], 0)
        self.sequence += 1
        self.router.ingest({'update_id': self.sequence, 'message': {
            'message_id': self.sequence, 'from': {'id': 123}, 'chat': {'id': 123, 'type': 'private'},
            'message_thread_id': 22, 'text': '普通消息'}})
        self.assertTrue(self.router.forward_one())
        self.assertEqual(self.router.bridges['instagram'].store.pending()['text'], '普通消息')
        self.assertEqual(self.create_calls, [])

    async def test_folder_validation_retry_and_cancellation_leave_no_native_thread(self):
        browser = await self.wizard()
        browser = await self.choose_path(browser, self.path)
        (self.path / 'existing').mkdir()
        await self.click(browser, '新建文件夹')
        for bad in ['../outside', '.', 'a/b', 'a\\b', 'existing']:
            await self.answer(self.telegram.messages[self.telegram.next_id], bad)
            self.assertIn('新建文件夹', self.telegram.messages[self.telegram.next_id]['text'])
        await self.answer(self.telegram.messages[self.telegram.next_id], '有效 目录')
        self.assertTrue((self.path / '有效 目录').is_dir())
        await self.click(self.panel(), '取消')
        self.assertIn('已取消', self.panel()['text'])
        self.assertEqual(self.create_calls, [])
        self.assertEqual(self.telegram.created, [])

    async def test_path_resolves_relative_symlink_hidden_paging_and_invalid_path(self):
        browser = await self.wizard()
        (self.path / '.hidden').mkdir()
        for i in range(12):
            (self.path / f'project-{i:02}').mkdir()
        browser = await self.choose_path(browser, self.path)
        self.assertNotIn('.hidden', str(browser['reply_markup']))
        second = await self.click(browser, '下一页')
        self.assertIn('project-11', str(second['reply_markup']))
        browser = await self.click(second, '显示点号目录')
        self.assertIn('.hidden', str(browser['reply_markup']))
        await self.click(browser, '输入路径')
        await self.answer(self.telegram.messages[self.telegram.next_id], 'not-present')
        prompt = self.telegram.messages[self.telegram.next_id]
        self.assertIn('目录不存在', prompt['text'])
        await self.answer(prompt, '~this-user-does-not-exist-telegram-test')
        prompt = self.telegram.messages[self.telegram.next_id]
        self.assertIn('目录不存在', prompt['text'])
        (self.path / 'alias').symlink_to(self.path / 'project-00', target_is_directory=True)
        await self.answer(prompt, 'alias')
        self.assertIn(str(self.path / 'project-00'), self.panel()['text'])

    async def test_stale_wrong_topic_foreign_and_media_prompt_replies_never_reach_model(self):
        browser = await self.wizard()
        await self.click(browser, '使用当前目录')
        prompt = self.telegram.messages[self.telegram.next_id]
        await self.answer(prompt, 'foreign', sender=999)
        self.assertEqual(self.router.store.db.execute('SELECT count(*) FROM ui_new_drafts WHERE name IS NOT NULL').fetchone()[0], 0)
        await self.answer(prompt, 'wrong topic', topic=22)
        await self.answer(prompt, 'attachment', media=True)
        replacement = self.telegram.messages[self.telegram.next_id]
        self.assertIn('请用文字', replacement['text'])
        await self.answer(prompt, 'stale')
        self.assertEqual(self.router.store.db.execute('SELECT count(*) FROM jobs').fetchone()[0], 0)
        await self.answer(replacement, '/cancel')
        self.assertIn('已取消', self.panel()['text'])
        self.assertEqual(self.create_calls, [])

    async def test_start_timeout_does_not_retry_even_after_restart(self):
        review = await self.review()
        original = self.router.catalog_call

        async def timeout(method, params):
            if method == 'thread/start':
                self.create_calls.append((method, params))
                raise asyncio.TimeoutError()
            return await original(method, params)

        self.router.catalog_call = AsyncMock(side_effect=timeout)
        await self.click(review, '确认新建并连接')
        self.router.picker.creator.recover()
        menu = await self.menu()
        await self.click(menu, '继续处理')
        self.assertIn('不会自动再次创建', self.telegram.messages[menu['message_id']]['text'])
        self.assertEqual(sum(m == 'thread/start' for m, _ in self.create_calls), 1)
        self.assertEqual(self.telegram.created, [])

    async def test_naming_failure_recovery_uses_same_native_id(self):
        review = await self.review()
        original = self.router.catalog_call
        failure = True

        async def flaky(method, params):
            nonlocal failure
            if method == 'thread/name/set' and failure:
                failure = False
                raise ConnectionError('lost name response')
            return await original(method, params)

        self.router.catalog_call = AsyncMock(side_effect=flaky)
        await self.click(review, '确认新建并连接')
        self.assertEqual(self.router.store.db.execute('SELECT state FROM ui_new_drafts').fetchone()[0], 'created')
        menu = await self.menu()
        await self.click(menu, '继续处理')
        self.assertEqual(sum(m == 'thread/start' for m, _ in self.create_calls), 1)
        self.assertEqual(len(self.telegram.created), 1)
        self.assertTrue(any(t['thread_id'] == 'created-1' for t in self.router.targets.values()))

    async def test_topic_timeout_recovery_keeps_native_id_and_does_not_duplicate_topic(self):
        review = await self.review()
        self.telegram.create_error = asyncio.TimeoutError()
        await self.click(review, '确认新建并连接')
        self.telegram.create_error = None
        menu = await self.menu()
        await self.click(menu, '继续处理')
        self.assertEqual(sum(m == 'thread/start' for m, _ in self.create_calls), 1)
        self.assertEqual(sum(m == 'createForumTopic' for m, _ in self.telegram.calls), 1)
        self.assertEqual(self.router.store.db.execute('SELECT state FROM ui_new_drafts').fetchone()[0], 'named')

    async def test_prompt_and_draft_survive_restart_and_directory_disappearance_prevents_creation(self):
        browser = await self.wizard()
        folder = self.path / 'temporary-project'
        folder.mkdir()
        browser = await self.choose_path(browser, folder)
        await self.click(browser, '使用当前目录')
        prompt = self.telegram.messages[self.telegram.next_id]
        original = self.router
        reloaded = TopicRouter(copy.deepcopy(self.config), self.path)
        reloaded.bot_id, reloaded.telegram = 444, self.telegram
        self.router = reloaded
        try:
            await reloaded.picker.recover()
            await self.answer(prompt, '重启后命名')
            self.assertIn('重启后命名', self.panel()['text'])
            folder.rmdir()
            reloaded.catalog_call = AsyncMock(side_effect=AssertionError('No RPC for missing directory'))
            await self.click(self.panel(), '确认新建并连接')
            reloaded.catalog_call.assert_not_awaited()
            self.assertEqual(self.telegram.created, [])
        finally:
            for b in reloaded.bridges.values():
                b.store.db.close()
            reloaded.store.db.close()
            self.router = original

    async def test_invalid_start_response_never_renames_an_existing_bound_session(self):
        review = await self.review()
        self.router.catalog_call = AsyncMock(return_value={'thread': {'id': 'meeting-native', 'cwd': str(self.path)}})
        await self.click(review, '确认新建并连接')
        self.assertEqual(self.router.catalog_call.await_count, 1)
        self.assertEqual(self.telegram.created, [])
        self.assertEqual(self.router.targets['meeting']['name'], '会议记录')
        self.assertEqual(self.router.store.db.execute('SELECT state FROM ui_new_drafts').fetchone()[0], 'blocked')

    async def test_crash_during_start_recovery_is_uncertain_without_another_start(self):
        await self.review()
        db = self.router.store.db
        with db:
            db.execute("UPDATE ui_new_drafts SET state='creating'")
        await self.router.picker.recover()
        menu = await self.menu()
        await self.click(menu, '继续处理')
        self.assertIn('需要核对', self.telegram.messages[menu['message_id']]['text'])
        self.assertEqual(self.create_calls, [])

    async def test_expired_prompt_cannot_be_dispatched_and_home_path_supported(self):
        browser = await self.wizard()
        browser = await self.choose_path(browser, '~/')
        self.assertIn(str(Path.home().resolve()), browser['text'])
        await self.click(browser, '使用当前目录')
        prompt = self.telegram.messages[self.telegram.next_id]
        with self.router.store.db:
            self.router.store.db.execute('UPDATE ui_new_prompts SET expires_at=?', (time.time() - 1,))
        await self.answer(prompt, '@meeting this must not dispatch')
        self.assertEqual(self.router.store.db.execute('SELECT count(*) FROM jobs').fetchone()[0], 0)
        self.assertEqual(self.create_calls, [])


if __name__ == '__main__':
    unittest.main()
