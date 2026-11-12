"""All replies select a local bound topic without adding quoted model input."""
import copy
import json
import unittest
from unittest.mock import AsyncMock, patch

import test_main_guard as guard_tests


class AllReplyRouteTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = guard_tests.MainGuardTests.asyncSetUp
    asyncTearDown = guard_tests.MainGuardTests.asyncTearDown
    message = guard_tests.MainGuardTests.message
    pump = guard_tests.MainGuardTests.pump
    inbox = guard_tests.MainGuardTests.inbox
    callback = guard_tests.MainGuardTests.callback

    @staticmethod
    def parent(topic=22, **extra):
        return {'message_id': 80, 'message_thread_id': topic,
                'text': '原文不应作为本次模型输入 PARENT_ONLY', **extra}

    async def ingest(self, update):
        self.router.ingest(update)
        await self.pump()
        while self.router.forward_one():
            pass

    def assert_no_context(self, row):
        self.assertFalse(json.loads(row['reply_json']) if row['reply_json'] else None)

    def assert_no_selector(self):
        self.assertFalse(any('选择接收会话' in message.get('text', '')
                             for message in self.telegram.messages.values()))

    def known_output(self, key='instagram', message_id=80):
        child = self.router.bridges[key]
        source = 'turn:previous:known-reply-' + str(message_id)
        child.store.enqueue_at(source, '123', '已发送的原文 PARENT_ONLY', child.target['message_thread_id'])
        with child.store.db:
            child.store.db.execute('UPDATE outbox SET message_id=? WHERE key LIKE ?', (message_id, source + ':%'))

    def known_preview(self, message_id=80, **changes):
        child = self.router.bridges['instagram']
        identity = {'native_id': child.thread_id, 'chat_id': '123', 'topic_id': 22,
                    'turn_id': 'preview-' + str(message_id), 'draft_id': 1}
        state = {'route': [child.thread_id, '123', 22], 'message_id': message_id, 'delivery': 'sent',
                 'desired_identity': identity, **changes}
        child.delivery.body_preview._save(state, identity)

    async def test_all_reply_routes_to_its_bound_topic_and_passes_only_new_text_to_codex(self):
        update = self.message('修改这里 NEW_ONLY', reply_to_message=self.parent(),
                              quote={'text': 'PARENT_ONLY', 'position': 0})
        await self.ingest(update)
        self.assertEqual(self.router.picker.guard.pending_count(), 0)
        self.assert_no_selector()
        self.assertEqual(self.inbox('meeting'), [])
        self.assertEqual(len(self.inbox()), 1)
        row = self.inbox()[0]
        self.assertEqual((row['text'], row['message_thread_id']), ('修改这里 NEW_ONLY', 22))
        self.assert_no_context(row)
        child = self.router.bridges['instagram']
        child.codex = type('Native', (), {'call': AsyncMock(return_value={'turn': {'id': 'new-turn'}})})()
        await child.dispatch_one(child.store.pending())
        method, params = child.codex.call.await_args.args
        self.assertEqual(method, 'turn/start')
        self.assertEqual(params['threadId'], 'instagram-native')
        model_text = '\n'.join(item.get('text', '') for item in params['input'])
        self.assertIn('NEW_ONLY', model_text)
        self.assertNotIn('PARENT_ONLY', model_text)
        self.assertNotIn('引用内容', model_text)
        echoes = child.store.db.execute('SELECT text FROM outbox WHERE key LIKE ?',
                                        ('main-guard-input:%',)).fetchall()
        self.assertTrue(any('NEW_ONLY' in row['text'] for row in echoes))
        self.assertFalse(any('PARENT_ONLY' in row['text'] for row in echoes))

    async def test_inside_thread_reply_keeps_current_destination_and_quoted_context(self):
        update = self.message('在话题里处理', topic=11, reply_to_message=self.parent(22))
        await self.ingest(update)
        row = self.inbox('meeting')[0]
        self.assertEqual(row['message_thread_id'], 11)
        self.assertIn('PARENT_ONLY', json.loads(row['reply_json'])['text'])
        self.assertEqual(self.inbox(), [])
        child = self.router.bridges['meeting']
        child.codex = type('Native', (), {'call': AsyncMock(return_value={'turn': {'id': 'inside'}})})()
        await child.dispatch_one(child.store.pending())
        model_text = '\n'.join(item.get('text', '') for item in child.codex.call.await_args.args[1]['input'])
        self.assertIn('在话题里处理', model_text)
        self.assertIn('PARENT_ONLY', model_text)
        self.assertIn('［引用内容］', model_text)

    async def test_plain_all_message_still_requires_a_destination(self):
        await self.ingest(self.message('普通消息不能猜目标'))
        self.assertEqual(self.router.picker.guard.pending_count(), 1)
        self.assertEqual(self.inbox(), [])
        self.assertEqual(self.inbox('meeting'), [])

    async def test_unbound_deleted_foreign_and_implicit_reply_parents_do_not_auto_route(self):
        parents = [self.parent(999), self.parent(True), self.parent('22'),
                   self.parent(22, chat={'id': -999, 'type': 'supergroup'}),
                   self.parent(22, forum_topic_created={'name': '自动附带的话题记录'})]
        for parent in parents:
            with self.subTest(parent=parent):
                await self.ingest(self.message('保留待选择', reply_to_message=parent))
        self.assertEqual(self.router.picker.guard.pending_count(), len(parents))
        self.assertEqual(self.inbox(), [])
        self.assertEqual(self.inbox('meeting'), [])
        self.router.targets['instagram']['topic_missing'] = True
        await self.ingest(self.message('已删除的话题', reply_to_message=self.parent()))
        self.assertEqual(self.inbox(), [])

    async def test_external_reply_is_not_a_same_chat_destination(self):
        await self.ingest(self.message('外部引用保留选择', external_reply={
            'message_id': 80, 'message_thread_id': 22, 'chat': {'id': -999, 'type': 'supergroup'},
            'origin': {'type': 'user', 'sender_user': {'id': 999}}, 'text': '外部原文'}))
        self.assertEqual(self.router.picker.guard.pending_count(), 1)
        self.assertEqual(self.inbox(), [])

    async def test_same_chat_external_reply_uses_known_outbox_identity_and_only_new_model_input(self):
        self.known_output()
        await self.ingest(self.message('只发新的这句 EXTERNAL_NEW', external_reply={
            'message_id': 80, 'chat': {'id': 123, 'type': 'private'},
            'origin': {'type': 'user', 'sender_user': {'id': 444}}, 'text': 'PARENT_ONLY'}))
        self.assert_no_selector()
        row = self.inbox()[0]
        self.assertEqual(row['message_thread_id'], 22)
        self.assert_no_context(row)
        self.assertEqual(self.inbox('meeting'), [])
        child = self.router.bridges['instagram']
        child.codex = type('Native', (), {'call': AsyncMock(return_value={'turn': {'id': 'external-turn'}})})()
        await child.dispatch_one(child.store.pending())
        model_text = '\n'.join(item.get('text', '') for item in child.codex.call.await_args.args[1]['input'])
        self.assertIn('EXTERNAL_NEW', model_text)
        self.assertNotIn('PARENT_ONLY', model_text)
        self.assertNotIn('［引用内容］', model_text)

    async def test_parent_without_topic_uses_known_outbox_identity(self):
        self.known_output()
        await self.ingest(self.message('省略topic仍精确定位', reply_to_message={
            'message_id': 80, 'chat': {'id': 123, 'type': 'private'}, 'text': 'PARENT_ONLY'}))
        self.assert_no_selector()
        self.assertEqual(self.inbox()[0]['message_thread_id'], 22)
        self.assert_no_context(self.inbox()[0])
        self.assertEqual(self.inbox('meeting'), [])

    async def test_parent_without_topic_uses_known_inbox_source_identity(self):
        child = self.router.bridges['instagram']
        child.store.accept({'update_id': 10}, {'message_id': 80, 'from': {'id': 123},
            'chat': {'id': 123, 'type': 'private'}, 'message_thread_id': 22, 'text': '以前发过的话'})
        child.store.mark(10, 'done')
        update = self.message('回复以前的本人消息', reply_to_message={'message_id': 80, 'text': 'PARENT_ONLY'})
        await self.ingest(update)
        self.assert_no_selector()
        new = [row for row in self.inbox() if row['id'] == update['update_id']]
        self.assertEqual(len(new), 1)
        self.assertEqual(new[0]['message_thread_id'], 22)
        self.assert_no_context(new[0])

    async def test_active_preview_message_identity_routes_without_a_topic_field(self):
        self.known_preview()
        await self.ingest(self.message('回复正在更新的过程', external_reply={
            'message_id': 80, 'chat': {'id': 123, 'type': 'private'}, 'text': 'PARENT_ONLY'}))
        self.assert_no_selector()
        self.assertEqual(self.inbox()[0]['message_thread_id'], 22)
        self.assert_no_context(self.inbox()[0])

    async def test_foreign_or_missing_chat_does_not_resolve_a_known_message_id_collision(self):
        self.known_output()
        references = [
            {'external_reply': {'message_id': 80, 'chat': {'id': -999, 'type': 'supergroup'}}},
            {'external_reply': {'message_id': 80}},
            {'reply_to_message': {'message_id': 80, 'chat': {'id': -999, 'type': 'supergroup'}}},
        ]
        for reference in references:
            await self.ingest(self.message('不能靠碰撞ID导流', **reference))
        self.assertEqual(self.inbox(), [])
        self.assertEqual(self.inbox('meeting'), [])
        self.assertEqual(self.router.picker.guard.pending_count(), len(references))

    async def test_message_identity_ambiguous_across_bound_targets_requires_selection(self):
        self.known_output('instagram')
        self.known_output('meeting')
        await self.ingest(self.message('冲突不能猜目标', external_reply={
            'message_id': 80, 'chat': {'id': 123, 'type': 'private'}}))
        self.assertEqual(self.inbox(), [])
        self.assertEqual(self.inbox('meeting'), [])
        self.assertEqual(self.router.picker.guard.pending_count(), 1)

    async def test_deleted_or_wrong_route_previews_cannot_supply_a_destination(self):
        invalid = [{'cleanup_state': 'deleted'}, {'closed': True},
                   {'route': ['other-native', '123', 22]}, {'route': ['instagram-native', '999', 22]},
                   {'route': ['instagram-native', '123', 11]}, {'delivery': 'uncertain'}]
        for index, changes in enumerate(invalid):
            identity = 80 + index
            self.known_preview(identity, **changes)
            await self.ingest(self.message('无效预览不能导流', reply_to_message={'message_id': identity}))
        self.assertEqual(self.inbox(), [])
        self.assertEqual(self.inbox('meeting'), [])
        self.assertEqual(self.router.picker.guard.pending_count(), len(invalid))

    async def test_reply_does_not_change_global_command_destination_or_turn_it_into_model_input(self):
        update = self.message('/sessions', reply_to_message=self.parent())
        await self.ingest(update)
        self.assertEqual(self.router.picker.guard.pending_count(), 0)
        self.assertEqual(self.inbox(), [])
        self.assertTrue(self.telegram.messages)
        self.assertTrue(all(not message.get('message_thread_id') for message in self.telegram.messages.values()))
        self.assertTrue(all(method in ('thread/read', 'thread/list') for method, _ in self.native_calls))

    async def test_duplicate_auto_reply_imports_and_echoes_once(self):
        update = self.message('恰好发送一次', reply_to_message=self.parent())
        await self.ingest(update)
        await self.ingest(copy.deepcopy(update))
        self.assertEqual(len(self.inbox()), 1)
        child = self.router.bridges['instagram']
        count = child.store.db.execute('SELECT COUNT(*) FROM outbox WHERE key LIKE ?',
                                        ('main-guard-input:%',)).fetchone()[0]
        self.assertEqual(count, 1)

    async def test_album_first_reply_routes_other_members_without_copying_quoted_context(self):
        first = self.message('', photo=[{'file_id': 'new-photo-1'}], caption='两张新图片',
                             media_group_id='album-auto', reply_to_message=self.parent())
        second = self.message('', photo=[{'file_id': 'new-photo-2'}], media_group_id='album-auto')
        self.router.ingest(first)
        self.router.ingest(second)
        self.now += 3
        await self.pump()
        while self.router.forward_one():
            pass
        self.assert_no_selector()
        self.assertEqual(self.router.picker.guard.pending_count(), 0)
        rows = self.inbox()
        self.assertEqual(len(rows), 2)
        self.assertEqual([row['message_thread_id'] for row in rows], [22, 22])
        self.assertEqual([json.loads(row['message_json'])['photo'][0]['file_id'] for row in rows],
                         ['new-photo-1', 'new-photo-2'])
        for row in rows:
            self.assert_no_context(row)

    async def test_binding_change_after_auto_route_is_blocked_before_native_dispatch(self):
        await self.ingest(self.message('不能改投给新绑定', reply_to_message=self.parent()))
        child = self.router.bridges['instagram']
        child.target['thread_id'] = 'changed-native'
        child.codex = type('Native', (), {'call': AsyncMock(side_effect=AssertionError('No retargeting'))})()
        await child.dispatch_one(child.store.pending())
        child.codex.call.assert_not_awaited()
        self.assertIn(self.inbox()[0]['state'], ('blocked', 'failed'))

    async def test_binding_change_before_automatic_release_keeps_input_for_explicit_selection(self):
        self.router.ingest(self.message('接收时冻结目标', reply_to_message=self.parent()))
        self.router.targets['instagram']['thread_id'] = 'replacement-native'
        await self.pump()
        self.assertEqual(self.inbox(), [])
        self.assertEqual(self.inbox('meeting'), [])
        self.assertEqual(self.router.picker.guard.pending_count(), 1)

    async def test_conflicting_reply_topics_in_one_album_require_selection(self):
        self.router.ingest(self.message('', photo=[{'file_id': 'new-1'}], media_group_id='conflict',
                                       reply_to_message=self.parent(22)))
        self.router.ingest(self.message('', photo=[{'file_id': 'new-2'}], media_group_id='conflict',
                                       reply_to_message=self.parent(11)))
        self.now += 3
        await self.pump()
        self.assertEqual(self.inbox(), [])
        self.assertEqual(self.inbox('meeting'), [])
        self.assertEqual(self.router.picker.guard.pending_count(), 1)

    async def test_pending_auto_reply_survives_restart_with_frozen_target_and_no_quote(self):
        from topics_bridge import TopicRouter
        update = self.message('重启也只发送一次', reply_to_message=self.parent())
        self.router.ingest(update)
        original = self.router
        reloaded = TopicRouter(copy.deepcopy(self.config), self.path)
        reloaded.telegram = self.telegram
        reloaded.picker.guard.clock = lambda: self.now
        self.router = reloaded
        try:
            await self.pump()
            while reloaded.forward_one():
                pass
            self.assertEqual(len(self.inbox()), 1)
            self.assert_no_context(self.inbox()[0])
            self.assert_no_selector()
            await self.ingest(copy.deepcopy(update))
            self.assertEqual(len(self.inbox()), 1)
        finally:
            self.router = original
            for child in reloaded.bridges.values():
                child.store.db.close()
            reloaded.store.db.close()

    async def test_legacy_album_draft_cannot_be_auto_released_by_a_new_reply_member(self):
        first = self.message('', photo=[{'file_id': 'legacy-photo'}], media_group_id='legacy-album')
        self.router.ingest(first)
        # The added column is SQL NULL on pre-upgrade rows. This must differ
        # from a fresh neutral member encoded as JSON 'null'.
        with self.router.store.db:
            self.router.store.db.execute('UPDATE main_guard_messages SET reply_binding_json=NULL WHERE update_id=?',
                                         (first['update_id'],))
        self.router.ingest(self.message('', photo=[{'file_id': 'new-photo'}], media_group_id='legacy-album',
                                       reply_to_message=self.parent()))
        self.now += 3
        await self.pump()
        self.assertEqual(self.inbox(), [])
        self.assertEqual(self.inbox('meeting'), [])
        self.assertEqual(self.router.picker.guard.pending_count(), 1)
        self.assertTrue(any('选择接收会话' in message.get('text', '')
                            for message in self.telegram.messages.values()))

    async def test_invalid_all_reply_still_omits_original_after_manual_selection(self):
        await self.ingest(self.message('只发新要求 AFTER_SELECT', reply_to_message=self.parent(999)))
        panel = self.telegram.messages[self.telegram.next_id]
        await guard_tests.MainGuardTests.click(self, panel, 'Instagram')
        row = self.inbox()[0]
        self.assert_no_context(row)
        child = self.router.bridges['instagram']
        child.codex = type('Native', (), {'call': AsyncMock(return_value={'turn': {'id': 'manual-reply'}})})()
        await child.dispatch_one(child.store.pending())
        model_text = '\n'.join(item.get('text', '') for item in child.codex.call.await_args.args[1]['input'])
        self.assertIn('AFTER_SELECT', model_text)
        self.assertNotIn('PARENT_ONLY', model_text)
        self.assertNotIn('［引用内容］', model_text)

    async def test_auto_release_recovery_after_import_before_echo_remains_idempotent(self):
        update = self.message('恢复后只发送一次', reply_to_message=self.parent())
        self.router.ingest(update)
        child = self.router.bridges['instagram']
        enqueue = child.store.enqueue_at

        def fail_echo(key, *args, **kwargs):
            if key.startswith('main-guard-input:'):
                raise RuntimeError('Simulated crash after input import, before echo enqueue')
            return enqueue(key, *args, **kwargs)

        with patch.object(child.store, 'enqueue_at', side_effect=fail_echo):
            with self.assertRaises(RuntimeError):
                await self.router.picker.guard.present(update)
        self.assertEqual(len(self.inbox()), 1)
        self.assert_no_context(self.inbox()[0])
        self.assertEqual(child.store.db.execute("SELECT COUNT(*) FROM outbox WHERE key LIKE 'main-guard-input:%'").fetchone()[0], 0)
        self.router.picker.guard.recover()
        self.router.picker.guard.recover()
        await self.ingest(copy.deepcopy(update))
        self.assertEqual(len(self.inbox()), 1)
        self.assertEqual(child.store.db.execute("SELECT COUNT(*) FROM outbox WHERE key LIKE 'main-guard-input:%'").fetchone()[0], 1)
        self.assertEqual(self.router.picker.guard.pending_count(), 0)
        self.assert_no_selector()

    async def test_unauthorized_sender_never_selects_a_reply_target(self):
        await self.ingest(self.message('其他人不能发给会话', sender=999, reply_to_message=self.parent()))
        self.assertEqual(self.inbox(), [])
        self.assertEqual(self.inbox('meeting'), [])
        self.assertEqual(self.router.picker.guard.pending_count(), 0)
