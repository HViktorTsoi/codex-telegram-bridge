import copy
import json
import time
import unittest
from unittest.mock import AsyncMock, patch

from bridge import TelegramError
import test_session_picker as fixtures
from topics_bridge import TopicRouter


class RenamerTests(unittest.IsolatedAsyncioTestCase):
    asyncTearDown = fixtures.PickerTests.asyncTearDown
    menu = fixtures.PickerTests.menu
    callback = fixtures.PickerTests.callback
    click = fixtures.PickerTests.click

    async def asyncSetUp(self):
        await fixtures.PickerTests.asyncSetUp(self)
        self.native_threads = {t['thread_id']: {'id': t['thread_id'], 'name': t['name'],
                                               'cwd': str(self.path), 'ephemeral': False}
                               for t in self.router.targets.values()}
        self.topic_names = {t['message_thread_id']: t['name'] for t in self.router.targets.values()}
        self.writes = []
        self.native_error = self.topic_error = None
        self.native_apply_before_error = self.topic_apply_before_error = False
        self.latest_panel_id = None
        self.router.catalog_call = AsyncMock(side_effect=self.native)
        original = self.telegram.call

        async def telegram(method, **params):
            if method == 'editForumTopic':
                self.telegram.calls.append((method, params))
                if self.topic_error:
                    error, self.topic_error = self.topic_error, None
                    if self.topic_apply_before_error:
                        self.topic_names[params['message_thread_id']] = params['name']
                    raise error
                self.topic_names[params['message_thread_id']] = params['name']
                return True
            result = await original(method, **params)
            if (method in ('sendMessage', 'editMessageText')
                    and 'inline_keyboard' in params.get('reply_markup', {})):
                self.latest_panel_id = result['message_id']
            return result

        self.telegram.call = telegram

    async def native(self, method, params):
        if method == 'thread/read':
            return {'thread': copy.deepcopy(self.native_threads[params['threadId']])}
        if method == 'thread/list':
            return {'data': list(copy.deepcopy(self.native_threads).values())}
        if method == 'thread/turns/list':
            return {'data': []}
        if method == 'thread/name/set':
            self.writes.append((method, copy.deepcopy(params)))
            if self.native_error:
                error, self.native_error = self.native_error, None
                if self.native_apply_before_error:
                    self.native_threads[params['threadId']]['name'] = params['name']
                raise error
            self.native_threads[params['threadId']]['name'] = params['name']
            return {}
        raise AssertionError('Renaming must not generate, fork, resume or interrupt: ' + method)

    def panel(self):
        return self.telegram.messages[self.latest_panel_id]

    def prompt(self):
        return max((m for m in self.telegram.messages.values()
                    if m.get('reply_markup', {}).get('force_reply')), key=lambda m: m['message_id'])

    async def drain(self):
        while await self.router.picker.handle_one():
            pass

    async def send(self, text, topic=11, **extra):
        self.sequence += 1
        message = {'message_id': self.sequence, 'from': {'id': 123},
                   'chat': {'id': 123, 'type': 'private'}, 'text': text, **extra}
        if topic:
            message['message_thread_id'] = topic
        update = {'update_id': self.sequence, 'message': message}
        self.router.ingest(update)
        await self.drain()
        return update

    async def answer(self, prompt, text, sender=123, topic=None, **extra):
        return await self.send(text, topic, **{'from': {'id': sender},
                                              'reply_to_message': copy.deepcopy(prompt)}, **extra)

    async def start(self, thread_id='meeting-native'):
        target = next(t for t in self.router.targets.values() if t['thread_id'] == thread_id)
        menu = await self.menu()
        bound = await self.click(menu, target['name'])
        await self.click(bound, '修改名称')
        return self.prompt()

    async def review(self, name='新会话名称', thread_id='meeting-native'):
        prompt = await self.start(thread_id)
        await self.answer(prompt, name)
        return self.panel()

    def assert_titles(self, name, key='meeting', tid='meeting-native', topic=11):
        self.assertEqual(self.native_threads[tid]['name'], name)
        self.assertEqual(self.topic_names[topic], name)
        self.assertEqual(self.router.targets[key]['name'], name)
        self.assertEqual(self.router.bridges[key].target['name'], name)
        self.assertEqual(self.router.by_topic[topic]['name'], name)
        self.assertEqual(self.router.targets[key]['thread_id'], tid)
        self.assertEqual(self.router.targets[key]['message_thread_id'], topic)
        config = json.loads((self.path / 'config.json').read_text())
        target = next(t for t in config['targets'] if t['key'] == key)
        self.assertEqual(target['name'], name)
        self.assertEqual(len(config['targets']), 2)

    async def resume(self):
        menu = await self.menu()
        await self.click(menu, '继续同步名称')

    async def test_bound_button_requires_final_confirmation_and_preserves_root_identity(self):
        prompt = await self.start()
        self.assertTrue(prompt['text'].startswith('会话改名 · 输入新名称'))
        self.assertTrue(prompt['reply_markup']['force_reply'])
        await self.answer(prompt, '中文 新名字')
        review = self.panel()
        self.assertIn('中文 新名字', review['text'])
        self.assertIn('会议记录', review['text'])
        self.assertEqual(self.writes, [])
        self.assertFalse(any(method == 'editForumTopic' for method, _ in self.telegram.calls))
        await self.click(review, '确认改名')
        self.assert_titles('中文 新名字')
        self.assertEqual(self.router.root_target['name'], '中文 新名字')
        self.assertEqual(self.router.config['root_target'], 'meeting')
        self.assertEqual(self.router.config['thread_id'], 'meeting-native')
        self.assertEqual(self.writes, [('thread/name/set', {'threadId': 'meeting-native', 'name': '中文 新名字'})])
        topic_calls = [p for method, p in self.telegram.calls if method == 'editForumTopic']
        self.assertEqual(len(topic_calls), 1)
        self.assertEqual(str(topic_calls[0]['chat_id']), '123')
        self.assertEqual(topic_calls[0]['message_thread_id'], 11)
        self.assertEqual(topic_calls[0]['name'], '中文 新名字')
        self.assertTrue(all(b.store.pending() is None for b in self.router.bridges.values()))

    async def test_nonroot_rename_does_not_replace_session_or_touch_running_input(self):
        child = self.router.bridges['instagram']
        child.store.accept({'update_id': 50}, {'message_id': 50, 'chat': {'id': 123},
                                             'message_thread_id': 22, 'text': '正在进行的任务'})
        child.store.mark(50, 'running', 'original-active-turn')
        bridge_identity = id(child)
        review = await self.review('短片新名字', 'instagram-native')
        await self.click(review, '确认改名')
        self.assert_titles('短片新名字', key='instagram', tid='instagram-native', topic=22)
        self.assertEqual(id(self.router.bridges['instagram']), bridge_identity)
        row = child.store.db.execute('SELECT state,turn_id,text FROM inbox WHERE id=50').fetchone()
        self.assertEqual(tuple(row), ('running', 'original-active-turn', '正在进行的任务'))
        self.assertEqual(self.router.root_target['name'], '会议记录')

    async def test_duplicate_confirmation_changes_no_extra_names(self):
        review = await self.review()
        update = self.callback(review, '确认改名')
        self.router.ingest(update)
        await self.drain()
        count = len(self.writes)
        update['update_id'] += 1
        self.router.ingest(update)
        await self.drain()
        self.assertEqual(len(self.writes), count)
        self.assert_titles('新会话名称')

    async def test_alias_and_slash_shaped_names_are_literal_prompt_input(self):
        for name in ['@paper 名字', '/stop']:
            review = await self.review(name)
            self.assertIn(name, review['text'])
            self.assertEqual(self.router.store.db.execute('SELECT COUNT(*) FROM cc_requests').fetchone()[0], 0)
            self.assertEqual(self.router.store.db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0], 0)
            self.assertEqual(self.router.picker.guard.pending_count(), 0)
            await self.click(review, '确认改名')
            self.assert_titles(name)

    async def test_cancellation_and_stale_prompt_reply_do_not_route_to_model(self):
        prompt = await self.start()
        await self.answer(prompt, '/cancel')
        await self.answer(prompt, '@paper 过期回复不应执行')
        self.assertEqual(self.writes, [])
        self.assertEqual(self.router.store.db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0], 0)
        self.assertEqual(self.router.store.db.execute('SELECT COUNT(*) FROM cc_requests').fetchone()[0], 0)
        self.assertEqual(self.router.picker.guard.pending_count(), 0)
        self.assert_titles('会议记录')

    async def test_foreign_wrong_topic_and_media_replies_cannot_rename(self):
        prompt = await self.start()
        await self.answer(prompt, '他人名称', sender=999)
        await self.answer(prompt, '错误话题', topic=22)
        await self.answer(prompt, '', photo=[{'file_id': 'photo'}], caption='不得作为名称')
        self.assertEqual(self.writes, [])
        self.assert_titles('会议记录')
        self.assertTrue(all(b.store.pending() is None for b in self.router.bridges.values()))
        self.assertEqual(self.router.picker.guard.pending_count(), 0)

    async def test_duplicate_blank_and_overlong_names_do_not_mutate_either_side(self):
        for name in ['Instagram', '   ', 'x' * 129]:
            prompt = await self.start()
            await self.answer(prompt, name)
            self.assertEqual(self.writes, [])
            self.assert_titles('会议记录')

    async def test_ordinary_topic_chat_and_all_guard_continue_while_name_prompt_is_open(self):
        await self.start()
        await self.send('普通话题消息', topic=22)
        self.assertTrue(self.router.forward_one())
        self.assertEqual(self.router.bridges['instagram'].store.pending()['text'], '普通话题消息')
        await self.send('普通 All 内容', topic=None)
        self.assertEqual(self.router.picker.guard.pending_count(), 1)
        self.assertEqual(self.writes, [])

    async def test_direct_topic_command_chooses_its_bound_session_and_still_reviews(self):
        await self.send('/rename 命令改名', topic=22)
        review = self.panel()
        self.assertIn('命令改名', review['text'])
        self.assertIn('instagram-native', review['text'])
        self.assertEqual(self.writes, [])
        await self.click(review, '确认改名')
        self.assert_titles('命令改名', key='instagram', tid='instagram-native', topic=22)

    async def test_all_command_has_target_chooser_and_no_default(self):
        await self.send('/rename', topic=None)
        chooser = self.panel()
        buttons = str(chooser.get('reply_markup'))
        self.assertIn('会议记录', buttons)
        self.assertIn('Instagram', buttons)
        self.assertEqual(self.writes, [])
        self.assertFalse(any(m.get('reply_markup', {}).get('force_reply') for m in self.telegram.messages.values()))
        await self.click(chooser, 'Instagram')
        await self.answer(self.prompt(), 'All 选中的名字')
        await self.click(self.panel(), '确认改名')
        self.assert_titles('All 选中的名字', key='instagram', tid='instagram-native', topic=22)

    async def test_native_response_lost_resumes_same_name_without_new_session(self):
        review = await self.review('重试新名称')
        self.native_error = ConnectionError('native rename response lost')
        self.native_apply_before_error = True
        await self.click(review, '确认改名')
        self.assertEqual(self.native_threads['meeting-native']['name'], '重试新名称')
        self.assertEqual(self.topic_names[11], '会议记录')
        await self.resume()
        self.assert_titles('重试新名称')
        self.assertTrue(all(method == 'thread/name/set' for method, _ in self.writes))
        self.assertTrue(all(params['name'] == '重试新名称' for _, params in self.writes))

    async def test_topic_response_lost_retries_topic_only_and_accepts_not_modified(self):
        review = await self.review('话题新名称')
        self.topic_error = ConnectionError('topic response lost')
        self.topic_apply_before_error = True
        await self.click(review, '确认改名')
        self.assertEqual(len(self.writes), 1)
        self.assertEqual(self.native_threads['meeting-native']['name'], '话题新名称')
        self.assertEqual(self.topic_names[11], '话题新名称')
        self.topic_error = TelegramError('editForumTopic', 400, 'TOPIC_NOT_MODIFIED')
        await self.resume()
        self.assert_titles('话题新名称')
        self.assertEqual(len(self.writes), 1)

    async def test_reopening_pending_session_exposes_same_operation_without_new_draft(self):
        review = await self.review('继续同一项改名')
        self.topic_error = ConnectionError('topic temporarily unavailable')
        await self.click(review, '确认改名')
        self.assertEqual(self.router.store.db.execute('SELECT COUNT(*) FROM ui_renames').fetchone()[0], 1)
        update = await self.send('/rename', topic=11)
        job = self.router.store.db.execute('SELECT state FROM ui_jobs WHERE id=?', (update['update_id'],)).fetchone()
        self.assertEqual(job['state'], 'done')
        self.assertEqual(self.router.store.db.execute('SELECT COUNT(*) FROM ui_renames').fetchone()[0], 1)
        self.assertEqual(len(self.writes), 1)
        await self.click(self.panel(), '继续同步名称')
        self.assert_titles('继续同一项改名')
        self.assertEqual(len(self.writes), 1)

    async def test_config_commit_failure_resumes_without_repeating_remote_renames(self):
        review = await self.review('配置新名称')
        original = self.router.rename_target

        def fail_once(*args, **kwargs):
            raise OSError('simulated config write failure')

        with patch.object(self.router, 'rename_target', side_effect=fail_once):
            await self.click(review, '确认改名')
        self.assertEqual(self.native_threads['meeting-native']['name'], '配置新名称')
        self.assertEqual(self.topic_names[11], '配置新名称')
        topic_calls = sum(m == 'editForumTopic' for m, _ in self.telegram.calls)
        self.router.rename_target = original
        await self.resume()
        self.assert_titles('配置新名称')
        self.assertEqual(len(self.writes), 1)
        self.assertEqual(sum(m == 'editForumTopic' for m, _ in self.telegram.calls), topic_calls)

    async def test_binding_change_before_confirm_blocks_both_renames(self):
        review = await self.review()
        self.router.targets['meeting']['message_thread_id'] = 999
        await self.click(review, '确认改名')
        self.assertEqual(self.writes, [])
        self.assertFalse(any(method == 'editForumTopic' for method, _ in self.telegram.calls))

    async def test_binding_change_after_native_rename_never_renames_replacement_topic(self):
        review = await self.review('只完成原生改名')

        async def changed(method, params):
            result = await self.native(method, params)
            if method == 'thread/name/set':
                self.router.targets['meeting']['message_thread_id'] = 999
            return result

        self.router.catalog_call = AsyncMock(side_effect=changed)
        await self.click(review, '确认改名')
        self.assertEqual(self.native_threads['meeting-native']['name'], '只完成原生改名')
        self.assertEqual(self.topic_names[11], '会议记录')
        self.assertFalse(any(method == 'editForumTopic' for method, _ in self.telegram.calls))

    async def test_access_revoked_after_native_rename_stops_topic_and_config_sync(self):
        review = await self.review('权限变更测试')

        async def revoked(method, params):
            result = await self.native(method, params)
            if method == 'thread/name/set':
                (self.path / 'access.json').write_text(json.dumps({'dmPolicy': 'allowlist', 'allowFrom': []}))
            return result

        self.router.catalog_call = AsyncMock(side_effect=revoked)
        await self.click(review, '确认改名')
        self.assertEqual(self.native_threads['meeting-native']['name'], '权限变更测试')
        self.assertEqual(self.topic_names[11], '会议记录')
        self.assertEqual(self.router.targets['meeting']['name'], '会议记录')
        self.assertFalse(any(method == 'editForumTopic' for method, _ in self.telegram.calls))

    async def test_access_revoked_during_final_read_stops_local_config_commit(self):
        review = await self.review('最终读回权限测试')

        async def revoked(method, params):
            result = await self.native(method, params)
            stage = self.router.store.db.execute('SELECT stage FROM ui_renames ORDER BY updated_at DESC LIMIT 1').fetchone()[0]
            if method == 'thread/read' and stage == 'config_pending':
                (self.path / 'access.json').write_text(json.dumps({'dmPolicy': 'allowlist', 'allowFrom': []}))
            return result

        self.router.catalog_call = AsyncMock(side_effect=revoked)
        await self.click(review, '确认改名')
        self.assertEqual(self.native_threads['meeting-native']['name'], '最终读回权限测试')
        self.assertEqual(self.topic_names[11], '最终读回权限测试')
        self.assertEqual(self.router.targets['meeting']['name'], '会议记录')

    async def test_cache_failure_after_config_commit_can_resume_without_remote_writes(self):
        review = await self.review('缓存恢复测试')
        with patch('control_center.write_json', side_effect=OSError('cache update unavailable')):
            await self.click(review, '确认改名')
        self.assert_titles('缓存恢复测试')
        count = len(self.writes)
        topic_count = sum(method == 'editForumTopic' for method, _ in self.telegram.calls)
        self.assertEqual(self.router.store.db.execute('SELECT stage FROM ui_renames').fetchone()[0], 'config_pending')
        await self.resume()
        self.assert_titles('缓存恢复测试')
        self.assertEqual(len(self.writes), count)
        self.assertEqual(sum(method == 'editForumTopic' for method, _ in self.telegram.calls), topic_count)

    async def test_external_native_name_change_blocks_stale_review(self):
        review = await self.review()
        self.native_threads['meeting-native']['name'] = '客户端已改名'
        await self.click(review, '确认改名')
        self.assertEqual(self.writes, [])
        self.assertEqual(self.native_threads['meeting-native']['name'], '客户端已改名')
        self.assertEqual(self.topic_names[11], '会议记录')
        self.assertEqual(self.router.targets['meeting']['name'], '会议记录')

    async def test_older_review_cannot_undo_a_later_completed_rename(self):
        older = await self.review('旧的待确认名字')
        newer = await self.review('后来确认的名字')
        await self.click(newer, '确认改名')
        count = len(self.writes)
        await self.click(older, '确认改名')
        self.assertEqual(len(self.writes), count)
        self.assert_titles('后来确认的名字')

    async def test_revoke_access_before_confirmation_never_mutates_names(self):
        review = await self.review()
        (self.path / 'access.json').write_text(json.dumps({'dmPolicy': 'allowlist', 'allowFrom': []}))
        await self.click(review, '确认改名')
        self.assertEqual(self.writes, [])
        self.assert_titles('会议记录')

    async def test_pending_rename_survives_restart_and_requires_explicit_resume(self):
        review = await self.review('重启后恢复')
        self.topic_error = ConnectionError('lost topic request')
        await self.click(review, '确认改名')
        self.assertEqual(len(self.writes), 1)
        original = self.router
        config = json.loads((self.path / 'config.json').read_text())
        restarted = TopicRouter(copy.deepcopy(config), self.path)
        restarted.bot_id, restarted.telegram = 444, self.telegram
        restarted.catalog_call = AsyncMock(side_effect=self.native)
        self.router = restarted
        try:
            calls_before = len(self.writes)
            await restarted.picker.recover()
            self.assertEqual(len(self.writes), calls_before)
            await self.resume()
            self.assert_titles('重启后恢复')
            self.assertEqual(len(self.writes), 1)
        finally:
            for b in restarted.bridges.values():
                b.store.db.close()
            restarted.store.db.close()
            self.router = original

    async def test_expired_prompt_and_duplicate_reply_do_not_become_regular_all_messages(self):
        prompt = await self.start()
        with self.router.store.db:
            self.router.store.db.execute('UPDATE ui_rename_prompts SET expires_at=?', (time.time() - 1,))
        await self.answer(prompt, '@paper 不得误派')
        self.assertEqual(self.writes, [])
        self.assertEqual(self.router.picker.guard.pending_count(), 0)
        self.assertEqual(self.router.store.db.execute('SELECT COUNT(*) FROM cc_requests').fetchone()[0], 0)
        prompt = await self.start()
        await self.answer(prompt, '有效新名')
        await self.answer(prompt, '重复提交不可覆盖')
        self.assertEqual(self.writes, [])
        self.assertIn('有效新名', self.panel()['text'])


if __name__ == '__main__':
    unittest.main()
