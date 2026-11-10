import copy
import json
import time
import unittest
from unittest.mock import AsyncMock, patch

import test_session_picker as fixtures
from topics_bridge import TargetBridge, TopicRouter


class MainGuardTests(unittest.IsolatedAsyncioTestCase):
    asyncTearDown = fixtures.PickerTests.asyncTearDown
    menu = fixtures.PickerTests.menu
    callback = fixtures.PickerTests.callback

    async def asyncSetUp(self):
        await fixtures.PickerTests.asyncSetUp(self)
        self.now = time.time()
        self.router.picker.guard.clock = lambda: self.now
        control = self.path / 'control'
        control.mkdir(exist_ok=True)
        entries = [{'host': 'zbox', 'thread_id': t['thread_id'], 'name': t['name'],
                    'target_key': t['key'], 'topic_id': t['message_thread_id'],
                    'aliases': [t['key'], t['name']], 'status': {'type': 'idle'},
                    'questions': [], 'updated': self.now} for t in self.config['targets']]
        (control / 'local.json').write_text(json.dumps({'host': 'zbox', 'generated': self.now, 'entries': entries}))

    def message(self, text='普通 All 消息', topic=None, sender=123, **extra):
        self.sequence += 1
        message = {'message_id': self.sequence, 'from': {'id': sender},
                   'chat': {'id': 123, 'type': 'private'}, 'text': text, 'date': 1}
        if topic:
            message['message_thread_id'] = topic
        message.update(extra)
        return {'update_id': self.sequence, 'message': message}

    async def send(self, text='普通 All 消息', **extra):
        update = self.message(text, **extra)
        self.router.ingest(update)
        await self.pump()
        return update, self.telegram.messages.get(self.telegram.next_id)

    async def pump(self):
        for _ in range(50):
            if not await self.router.picker.handle_one():
                return
        self.fail('Management job loop failed to become idle')

    async def click(self, message, label, **kwargs):
        self.router.ingest(self.callback(message, label, **kwargs))
        await self.pump()
        return self.telegram.messages[message['message_id']]

    def inbox(self, key='instagram'):
        return self.router.bridges[key].store.db.execute('SELECT * FROM inbox ORDER BY id').fetchall()

    def assert_held(self):
        self.assertEqual(self.router.store.db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0], 0)
        self.assertTrue(all(not self.inbox(key) for key in self.router.bridges))

    async def confirm(self, panel, name='Instagram'):
        # Selecting a destination is the send action; no review page follows.
        return await self.click(panel, name)

    async def legacy_review(self, panel, name='Instagram'):
        """Persist a pre-upgrade review and its real callback binding."""
        selection = self.callback(panel, name)
        token = selection['callback_query']['data'][3:]
        data = json.loads(self.router.store.db.execute(
            'SELECT data FROM ui_buttons WHERE token=?', (token,)).fetchone()[0])
        data.pop('send_on_select', None)  # Old review callbacks predate one-click delivery.
        guard = self.router.picker.guard
        guard._save(data['id'], state='review', selection_json=json.dumps(data['target']),
                    selected_revision=data['revision'], expires_at=self.now + guard.TTL)
        return await guard._panel(selection, '旧版待确认消息', [
            [('确认发送', 'main:confirm', data)],
            [('重新选择', 'main:choose', {'id': data['id']}),
             ('取消', 'main:cancel', {'id': data['id']})]], guard._draft(data['id']))

    async def test_plain_main_message_sends_on_selection_without_a_confirmation_page_or_native_calls(self):
        update, panel = await self.send('请检查这个项目')
        self.assert_held()
        self.assertIn('All', panel['text'])
        self.assertNotIn('message_thread_id', panel)
        self.assertEqual(self.router.picker.guard.pending_count(), 1)
        result = await self.click(panel, 'Instagram')
        self.assertIn('Instagram', result['text'])
        self.assertIn('已加入', result['text'])
        self.assertNotIn('确认发送', str(result['reply_markup']))
        row = self.inbox()[0]
        self.assertEqual((row['id'], row['text'], row['message_thread_id']),
                         (update['update_id'], '请检查这个项目', 22))
        self.assertTrue(json.loads(row['main_guard_json']))
        self.assertEqual(self.inbox('meeting'), [])
        self.assertEqual(self.router.picker.guard.pending_count(), 0)
        self.assertTrue(all(method in ('thread/read', 'thread/list') for method, _ in self.native_calls))
        self.assertEqual(self.telegram.created, [])

    async def test_default_root_is_never_implicitly_selected_and_can_be_chosen_explicitly(self):
        _, panel = await self.send('发给 Manager 的内容')
        self.assertEqual(self.inbox('meeting'), [])
        await self.confirm(panel, '会议记录')
        self.assertEqual(self.inbox('meeting')[0]['text'], '发给 Manager 的内容')
        self.assertEqual(self.inbox(), [])

    async def test_duplicate_message_and_target_click_enqueue_exact_original_input_once(self):
        update, panel = await self.send('只发送一次')
        self.router.ingest(copy.deepcopy(update))
        await self.router.picker.handle_one()
        self.assertEqual(self.router.picker.guard.pending_count(), 1)
        click = self.callback(panel, 'Instagram')
        self.router.ingest(click)
        await self.router.picker.handle_one()
        self.sequence += 1
        click['update_id'] = self.sequence
        self.router.ingest(click)
        await self.router.picker.handle_one()
        self.router.ingest(copy.deepcopy(update))
        await self.router.picker.handle_one()
        self.assertEqual(len(self.inbox()), 1)
        self.assertEqual(self.inbox()[0]['id'], update['update_id'])
        self.assertEqual(self.inbox('meeting'), [])

    async def test_cancellation_and_stale_target_click_do_not_send_or_select_a_default(self):
        _, panel = await self.send('发错了，不要执行')
        stale = self.callback(panel, 'Instagram')
        await self.click(panel, '取消')
        self.router.ingest(stale)
        await self.router.picker.handle_one()
        self.assert_held()
        self.assertEqual(self.router.picker.guard.pending_count(), 0)

    async def test_two_independent_main_messages_do_not_share_the_last_selected_target(self):
        first, first_panel = await self.send('第一条')
        second, second_panel = await self.send('第二条')
        self.assertEqual(self.router.picker.guard.pending_count(), 2)
        await self.click(first_panel, 'Instagram')
        await self.click(second_panel, '会议记录')
        self.assertEqual([(r['id'], r['text']) for r in self.inbox()], [(first['update_id'], '第一条')])
        self.assertEqual([(r['id'], r['text']) for r in self.inbox('meeting')], [(second['update_id'], '第二条')])
        third, _ = await self.send('第三条仍需重新选择')
        self.assertEqual(self.router.picker.guard.pending_count(), 1)
        self.assertEqual(len(self.inbox()), 1)
        self.assertEqual(len(self.inbox('meeting')), 1)

    async def test_photo_document_and_command_like_captions_are_held_as_payload(self):
        examples = [({'photo': [{'file_id': 'photo-1', 'width': 100, 'height': 100}], 'caption': '/stop'}, 'photo'),
                    ({'document': {'file_id': 'file-1', 'file_name': '材料.pdf'}, 'caption': '@instagram 不执行别名'}, 'document')]
        for index, (extra, kind) in enumerate(examples):
            _, panel = await self.send('', **extra)
            self.assertEqual(self.router.store.db.execute('SELECT COUNT(*) FROM cc_jobs').fetchone()[0], 0)
            self.assertEqual(sum(len(self.inbox(key)) for key in self.router.bridges), index)
            await self.click(panel, 'Instagram')
            self.assertEqual(sum(len(self.inbox(key)) for key in self.router.bridges), index + 1)
            row = self.inbox()[-1]
            self.assertEqual(row['text'], extra['caption'])
            self.assertEqual(json.loads(row['message_json'])[kind], extra[kind])
            self.assertEqual(row['message_thread_id'], 22)
        self.assertEqual(self.inbox('meeting'), [])

    async def test_quoted_main_message_preserves_snapshot_and_routes_to_chosen_topic(self):
        update, panel = await self.send('修改这里', reply_to_message={
            'message_id': 80, 'message_thread_id': 22, 'text': '另一个话题的原始引用内容'},
            quote={'text': '原始引用', 'position': 6})
        await self.confirm(panel, '会议记录')
        row = self.inbox('meeting')[0]
        context = json.loads(row['reply_json'])
        self.assertEqual(row['text'], '修改这里')
        self.assertEqual(row['message_thread_id'], 11)
        self.assertEqual(context['reply_to_message_id'], 80)
        self.assertEqual(context['reply_to_thread_id'], 22)
        self.assertEqual(context['quote'], '原始引用')
        self.assertEqual(self.inbox(), [])

    async def test_owner_and_callback_context_checks_cannot_be_bypassed(self):
        await self.send('外人不应产生草稿', sender=999)
        self.assertEqual(self.router.picker.guard.pending_count(), 0)
        _, panel = await self.send('本人待发送')
        unauthorized = self.callback(panel, 'Instagram', sender=999)
        self.router.ingest(unauthorized)
        await self.router.picker.handle_one()
        self.assert_held()
        forged = self.callback(panel, 'Instagram')
        forged['callback_query']['message']['message_thread_id'] = 22
        self.router.ingest(forged)
        await self.router.picker.handle_one()
        self.assert_held()
        (self.path / 'access.json').write_text(json.dumps({'dmPolicy': 'allowlist', 'allowFrom': []}))
        await self.click(panel, 'Instagram')
        self.assert_held()

    async def test_changed_binding_since_selector_render_is_blocked_before_handoff(self):
        _, panel = await self.send('不要投给变更后的会话')
        self.router.targets['instagram']['thread_id'] = 'different-native'
        await self.click(panel, 'Instagram')
        self.assert_held()

    async def test_repaired_topic_after_selection_requires_selecting_again(self):
        _, panel = await self.send('话题改变后不能自动跟随')
        self.router.targets['instagram']['message_thread_id'] = 999
        await self.click(panel, 'Instagram')
        self.assert_held()

    async def test_confirmed_guard_validates_binding_before_any_native_dispatch(self):
        _, panel = await self.send('接收队列后绑定发生变化')
        await self.confirm(panel)
        child = self.router.bridges['instagram']
        child.codex = type('Native', (), {'call': AsyncMock(side_effect=AssertionError('Wrong target must not run'))})()
        child.thread_id = 'another-native'
        await child.dispatch_one(child.store.pending())
        child.codex.call.assert_not_awaited()
        self.assertIn(self.inbox()[0]['state'], ('blocked', 'failed'))
        self.assertEqual(self.inbox('meeting'), [])

    async def test_confirmed_guard_rechecks_topic_after_attachment_preparation(self):
        _, panel = await self.send('', caption='附件任务', photo=[{'file_id': 'file'}])
        await self.confirm(panel)
        child = self.router.bridges['instagram']
        child.codex = type('Native', (), {'call': AsyncMock(side_effect=AssertionError('Changed topic must not run'))})()

        async def preparing(*args):
            child.target['message_thread_id'] = 999
            return [{'type': 'text', 'text': '附件已准备'}]

        with patch('bridge.inputs_for', new=AsyncMock(side_effect=preparing)) as prepare:
            await child.dispatch_one(child.store.pending())
            prepare.assert_awaited_once()
        child.codex.call.assert_not_awaited()
        self.assertIn(self.inbox()[0]['state'], ('blocked', 'failed'))

    async def test_guarded_text_dispatches_to_exact_selected_native_id_once(self):
        update, panel = await self.send('确认后执行这个任务')
        await self.confirm(panel)
        child = self.router.bridges['instagram']
        child.codex = type('Native', (), {'call': AsyncMock(return_value={'turn': {'id': 'selected-turn'}})})()
        await child.dispatch_one(child.store.pending())
        child.codex.call.assert_awaited_once()
        method, params = child.codex.call.call_args.args
        self.assertEqual((method, params['threadId']), ('turn/start', 'instagram-native'))
        self.assertEqual(params['input'], [{'type': 'text', 'text': '确认后执行这个任务'}])
        self.assertEqual(params['clientUserMessageId'], 'telegram-' + str(update['update_id']))

    async def test_restart_keeps_unselected_message_and_sends_only_on_target_click(self):
        update, panel = await self.send('重启后仍保留这一条')
        original = self.router
        reloaded = TopicRouter(copy.deepcopy(self.config), self.path)
        reloaded.bot_id, reloaded.telegram = 444, self.telegram
        reloaded.picker.guard.clock = lambda: self.now
        self.router = reloaded
        try:
            await reloaded.picker.recover()
            self.assert_held()
            await self.click(panel, 'Instagram')
            rows = self.inbox()
            self.assertEqual(len(rows), 1)
            self.assertEqual((rows[0]['id'], rows[0]['text'], rows[0]['message_thread_id']),
                             (update['update_id'], '重启后仍保留这一条', 22))
        finally:
            for child in reloaded.bridges.values():
                child.store.db.close()
            reloaded.store.db.close()
            self.router = original

    async def test_sessions_pending_entry_can_reopen_an_unsent_main_message(self):
        _, panel = await self.send('尚未确定发给谁')
        menu = await self.menu(topic_id=None)
        pending = await self.click(menu, '待发送')
        self.assertIn('尚未确定发给谁', pending['text'] + str(pending['reply_markup']))
        self.assert_held()

    async def test_expired_draft_cannot_be_selected_or_fall_back_to_root(self):
        _, panel = await self.send('过期后不要执行')
        self.now += 901
        await self.click(panel, 'Instagram')
        self.assert_held()

    async def test_thread_messages_commands_aliases_and_force_reply_flows_bypass_main_guard(self):
        update = self.message('正常话题消息', topic=22)
        self.router.ingest(update)
        self.assertTrue(self.router.forward_one())
        self.assertEqual(self.inbox()[0]['text'], '正常话题消息')
        self.assertEqual(self.router.picker.guard.pending_count(), 0)
        _, menu = await self.send('/sessions')
        self.assertIn('新建会话', str(menu['reply_markup']))
        self.assertEqual(self.router.picker.guard.pending_count(), 0)
        for text in ('@instagram 明确的别名派单', '/to instagram 明确的命令派单'):
            self.router.ingest(self.message(text))
            await self.router.control.handle_one()
        requests = self.router.store.db.execute('SELECT payload FROM cc_requests').fetchall()
        self.assertEqual(len(requests), 2)
        self.assertTrue(all(json.loads(row[0])['thread_id'] == 'instagram-native' for row in requests))
        self.assertEqual(self.router.picker.guard.pending_count(), 0)
        unknown = self.message('/not-a-real-command')
        self.router.ingest(unknown)
        self.assertTrue(self.router.forward_one())
        await self.router.bridges['meeting'].dispatch_one(self.router.bridges['meeting'].store.pending())
        self.assertEqual(self.router.picker.guard.pending_count(), 0)

    async def test_creator_prompt_reply_with_alias_like_name_is_not_intercepted(self):
        menu = await self.menu(topic_id=None)
        browser = await self.click(menu, '新建会话')
        await self.click(browser, '使用当前目录')
        prompt = self.telegram.messages[self.telegram.next_id]
        await self.send('@instagram 这是会话名称', reply_to_message=copy.deepcopy(prompt))
        self.assertEqual(self.router.picker.guard.pending_count(), 0)
        self.assert_held()

    async def test_cc_prompt_reply_retains_explicit_target_and_is_not_intercepted(self):
        self.router.ingest(self.message('/to'))
        await self.router.control.handle_one()
        menu = self.telegram.messages[self.telegram.next_id]
        button = next(b for row in menu['reply_markup']['inline_keyboard'] for b in row if 'Instagram' in b['text'])
        self.sequence += 1
        self.router.ingest({'update_id': self.sequence, 'callback_query': {'id': str(self.sequence),
                           'from': {'id': 123}, 'message': copy.deepcopy(menu), 'data': button['callback_data']}})
        await self.router.control.handle_one()
        prompt = self.telegram.messages[self.telegram.next_id]
        self.router.ingest(self.message('明确回复派单提示', reply_to_message=copy.deepcopy(prompt)))
        await self.router.control.handle_one()
        self.assertEqual(self.router.picker.guard.pending_count(), 0)
        row = self.router.store.db.execute('SELECT payload FROM cc_requests').fetchone()
        self.assertEqual(json.loads(row[0])['text'], '明确回复派单提示')

    async def test_album_is_one_target_selection_and_all_original_parts_are_preserved(self):
        first, panel = await self.send('', caption='相册说明', media_group_id='album', photo=[{'file_id': 'one'}])
        second, _ = await self.send('', media_group_id='album', photo=[{'file_id': 'two'}])
        self.assert_held()
        self.assertEqual(self.router.picker.guard.pending_count(), 1)
        self.now += 3
        await self.pump()
        panel = self.telegram.messages[self.telegram.next_id]
        await self.confirm(panel)
        rows = self.inbox()
        self.assertEqual([r['id'] for r in rows], [first['update_id'], second['update_id']])
        self.assertEqual([json.loads(r['message_json'])['photo'][0]['file_id'] for r in rows], ['one', 'two'])
        self.assertTrue(all(r['media_group_id'] == 'album' and r['message_thread_id'] == 22 for r in rows))

    async def test_late_album_part_invalidates_target_click_and_parts_after_send_need_new_selection(self):
        await self.send('', caption='第一部分', media_group_id='album', photo=[{'file_id': 'one'}])
        self.now += 3
        await self.pump()
        panel = self.telegram.messages[self.telegram.next_id]
        stale_selection = self.callback(panel, 'Instagram')
        await self.send('', media_group_id='album', photo=[{'file_id': 'two'}])
        self.router.ingest(stale_selection)
        await self.router.picker.handle_one()
        self.assert_held()
        self.now += 3
        await self.pump()
        panel = self.telegram.messages[self.telegram.next_id]
        await self.click(panel, 'Instagram')
        self.assertEqual(len(self.inbox()), 2)
        await self.send('', media_group_id='album', photo=[{'file_id': 'three'}])
        self.now += 3
        await self.pump()
        self.assertEqual(len(self.inbox()), 2)
        self.assertEqual(self.router.picker.guard.pending_count(), 1)

    async def test_partial_album_handoff_recovery_preserves_committed_child_rows_without_duplicates(self):
        first, _ = await self.send('', caption='确认整个相册', media_group_id='album', photo=[{'file_id': 'one'}])
        second, _ = await self.send('', media_group_id='album', photo=[{'file_id': 'two'}])
        self.now += 3
        await self.pump()
        panel = self.telegram.messages[self.telegram.next_id]
        child = self.router.bridges['instagram']
        accept = child.store.accept
        count = 0

        def interrupted(*args, **kwargs):
            nonlocal count
            count += 1
            if count == 2:
                raise OSError('Simulated interruption after the first child commit')
            return accept(*args, **kwargs)

        with patch.object(child.store, 'accept', side_effect=interrupted):
            await self.click(panel, 'Instagram')
        self.assertEqual([r['id'] for r in self.inbox()], [first['update_id']])
        self.router.picker.guard.recover()
        self.router.picker.guard.recover()
        rows = self.inbox()
        self.assertEqual([r['id'] for r in rows], [first['update_id'], second['update_id']])
        self.assertTrue(all(r['message_thread_id'] == 22 and json.loads(r['main_guard_json']) for r in rows))
        self.assertEqual(self.router.picker.guard.pending_count(), 0)

    async def test_target_selector_pagination_can_choose_a_target_beyond_first_page(self):
        for index in range(9):
            key = 'extra' + str(index)
            target = {'key': key, 'name': '额外会话' + str(index), 'thread_id': 'native-' + key,
                      'message_thread_id': 100 + index}
            directory = self.path / key
            directory.mkdir()
            self.router.targets[key] = target
            self.router.bridges[key] = TargetBridge(self.config, directory, target, self.router.sessions_text)
        _, first = await self.send('请选择列表后面的会话')
        self.assertNotIn('额外会话8', str(first['reply_markup']))
        second = await self.click(first, '下一页')
        self.assertIn('额外会话8', str(second['reply_markup']))
        await self.click(second, '额外会话8')
        row = self.inbox('extra8')[0]
        self.assertEqual((row['text'], row['message_thread_id']), ('请选择列表后面的会话', 108))
        self.assertEqual(self.inbox(), [])
        self.assertEqual(self.inbox('meeting'), [])

    async def test_legacy_review_is_not_sent_on_restart_and_its_confirmation_still_works_once(self):
        update, panel = await self.send('升级前已经选好了目标')
        review = await self.legacy_review(panel)
        confirmation = self.callback(review, '确认发送')
        self.assert_held()
        original = self.router
        reloaded = TopicRouter(copy.deepcopy(self.config), self.path)
        reloaded.bot_id, reloaded.telegram = 444, self.telegram
        reloaded.picker.guard.clock = lambda: self.now
        self.router = reloaded
        try:
            await reloaded.picker.recover()
            self.assert_held()
            self.router.ingest(confirmation)
            await self.pump()
            self.assertEqual([(r['id'], r['text'], r['message_thread_id']) for r in self.inbox()],
                             [(update['update_id'], '升级前已经选好了目标', 22)])
            self.sequence += 1
            confirmation['update_id'] = self.sequence
            self.router.ingest(confirmation)
            await self.pump()
            self.assertEqual(len(self.inbox()), 1)
        finally:
            for child in reloaded.bridges.values():
                child.store.db.close()
            reloaded.store.db.close()
            self.router = original

    async def test_legacy_review_can_be_cancelled_without_sending_its_old_confirmation(self):
        _, panel = await self.send('旧版确认页也允许取消')
        review = await self.legacy_review(panel)
        stale = self.callback(review, '确认发送')
        await self.click(review, '取消')
        self.router.ingest(stale)
        await self.pump()
        self.assert_held()
        self.assertEqual(self.router.picker.guard.pending_count(), 0)

    async def test_quoted_old_photo_is_context_only_and_only_new_photo_downloads_after_confirmation(self):
        from test_outgoing_images import PNG
        photo = self.path / 'downloaded.png'
        photo.write_bytes(PNG)
        _, panel = await self.send('', caption='分析这张新图', photo=[{'file_id': 'new-photo'}],
                                   reply_to_message={'message_id': 77, 'message_thread_id': 11,
                                                     'caption': '旧图仅作引用', 'photo': [{'file_id': 'old-photo'}]})
        await self.confirm(panel)
        child = self.router.bridges['instagram']
        child.codex = type('Native', (), {'call': AsyncMock(return_value={'turn': {'id': 'photo-turn'}})})()
        with patch('attachments.download', new=AsyncMock(return_value=photo)) as download:
            await child.dispatch_one(child.store.pending())
        download.assert_awaited_once()
        self.assertEqual(download.call_args.args[1]['file_id'], 'new-photo')
        method, params = child.codex.call.call_args.args
        self.assertEqual((method, params['threadId']), ('turn/start', 'instagram-native'))
        self.assertEqual(sum(c['type'] == 'localImage' for c in params['input']), 1)
        self.assertIn('分析这张新图', params['input'][0]['text'])
        self.assertIn('旧图仅作引用', params['input'][0]['text'])
        self.assertIn('［引用内容］', params['input'][0]['text'])


if __name__ == '__main__':
    unittest.main()
