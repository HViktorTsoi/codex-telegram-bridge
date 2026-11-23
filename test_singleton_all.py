"""Single-topic All input uses the same durable, identity-checked handoff as selection."""
import copy
import json
import unittest
from unittest.mock import AsyncMock, patch

from completion_delivery import CompletionDelivery
from native_mirror import NativeMirror
import test_main_guard as fixtures
from topics_bridge import TargetBridge, TopicRouter


class SingletonAllTests(unittest.IsolatedAsyncioTestCase):
    asyncTearDown = fixtures.MainGuardTests.asyncTearDown
    message = fixtures.MainGuardTests.message
    send = fixtures.MainGuardTests.send
    pump = fixtures.MainGuardTests.pump
    menu = fixtures.MainGuardTests.menu
    callback = fixtures.MainGuardTests.callback
    click = fixtures.MainGuardTests.click
    assert_held = fixtures.MainGuardTests.assert_held

    async def asyncSetUp(self):
        await fixtures.MainGuardTests.asyncSetUp(self)
        self.other_target = copy.deepcopy(self.config['targets'][1])
        self.config['targets'] = [self.config['targets'][0]]
        self.catalog = self.router.bridges['meeting'].codex
        self.reload_router()

    def reload_router(self):
        for child in self.router.bridges.values():
            child.store.db.close()
        self.router.store.db.close()
        (self.path / 'config.json').write_text(json.dumps(self.config))
        self.router = TopicRouter(copy.deepcopy(self.config), self.path)
        self.router.bot_id, self.router.telegram = 444, self.telegram
        self.router.picker.guard.clock = lambda: self.now
        root = self.router.bridges['meeting']
        root.ready.set()
        root.codex = self.catalog
        entries = [{'host': 'zbox', 'thread_id': t['thread_id'], 'name': t['name'],
                    'target_key': t['key'], 'topic_id': t['message_thread_id'],
                    'aliases': [t['key'], t['name']], 'status': {'type': 'idle'},
                    'questions': [], 'updated': self.now}
                   for t in self.config['targets']]
        (self.path / 'control/local.json').write_text(json.dumps(
            {'host': 'zbox', 'generated': self.now, 'entries': entries}))

    def add_other(self):
        target = copy.deepcopy(self.other_target)
        self.router.targets[target['key']] = target
        self.router.by_topic[target['message_thread_id']] = target
        self.router.bridges[target['key']] = TargetBridge(
            self.config, self.path / 'targets' / target['key'], target, self.router.sessions_text)
        self.config['targets'] = [self.config['targets'][0], copy.deepcopy(target)]

    def remove_other(self):
        child = self.router.bridges.pop('instagram')
        child.store.db.close()
        target = self.router.targets.pop('instagram')
        self.router.by_topic.pop(target['message_thread_id'], None)
        self.config['targets'] = [self.config['targets'][0]]

    def inbox(self, key='meeting'):
        return fixtures.MainGuardTests.inbox(self, key)

    def assert_no_selector(self):
        self.assertFalse(any('选择接收会话' in message.get('text', '')
                             for message in self.telegram.messages.values()))

    def assert_no_all_echo(self):
        for child in self.router.bridges.values():
            self.assertEqual(child.store.db.execute(
                "SELECT COUNT(*) FROM outbox WHERE key LIKE 'main-guard-input:%'").fetchone()[0], 0)

    async def test_plain_all_text_reaches_the_only_native_thread_without_a_selector(self):
        text = '下载这个视频\nhttps://example.test/watch?v=1&sig=keep-this\n第二段 **保留格式**'
        update, _ = await self.send(text)
        rows = self.inbox()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual((row['id'], row['text'], row['message_thread_id'], row['source_message_id']),
                         (update['update_id'], text, 11, update['message']['message_id']))
        self.assertEqual(json.loads(row['main_guard_json'])['thread_id'], 'meeting-native')
        self.assertEqual(self.router.picker.guard.pending_count(), 0)
        self.assert_no_selector()
        self.assert_no_all_echo()
        self.assertEqual(self.telegram.created, [])
        child = self.router.bridges['meeting']
        child.codex = type('Native', (), {'call': AsyncMock(return_value={'turn': {'id': 'one-turn'}})})()
        await child.dispatch_one(child.store.pending())
        child.codex.call.assert_awaited_once()
        method, params = child.codex.call.await_args.args
        self.assertEqual((method, params['threadId']), ('turn/start', 'meeting-native'))
        self.assertEqual(params['input'], [{'type': 'text', 'text': text}])
        self.assertEqual(params['clientUserMessageId'], 'telegram-' + str(update['update_id']))

    async def test_photo_document_and_video_payloads_keep_literal_command_like_captions(self):
        examples = [
            ('photo', [{'file_id': 'new-photo', 'width': 100, 'height': 80}], '/stop'),
            ('document', {'file_id': 'new-document', 'file_name': '原始材料.pdf'}, '@meeting 作为文件说明'),
            ('video', {'file_id': 'new-video', 'file_name': '原片.mp4', 'mime_type': 'video/mp4'}, ''),
        ]
        for kind, payload, caption in examples:
            with self.subTest(kind=kind):
                update, _ = await self.send('', **{kind: payload, 'caption': caption})
                row = next(r for r in self.inbox() if r['id'] == update['update_id'])
                self.assertEqual(row['text'], caption)
                self.assertEqual(json.loads(row['message_json'])[kind], payload)
                self.assertEqual(row['message_thread_id'], 11)
                self.assertTrue(json.loads(row['main_guard_json']))
        self.assertEqual(len(self.inbox()), len(examples))
        self.assertEqual(self.router.store.db.execute('SELECT COUNT(*) FROM cc_jobs').fetchone()[0], 0)
        self.assertEqual(self.router.store.db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0], 0)
        self.assert_no_selector()
        self.assert_no_all_echo()

    async def test_album_waits_for_all_parts_then_preserves_every_original_part_once(self):
        first = self.message('', caption='整组原文', media_group_id='one-album',
                             photo=[{'file_id': 'photo-1'}])
        second = self.message('', media_group_id='one-album', photo=[{'file_id': 'photo-2'}])
        self.router.ingest(first)
        await self.pump()
        self.assertEqual(self.inbox(), [])
        self.router.ingest(second)
        self.router.ingest(copy.deepcopy(first))
        await self.pump()
        self.assertEqual(self.inbox(), [])
        self.now += 3
        await self.pump()
        rows = self.inbox()
        self.assertEqual([r['id'] for r in rows], [first['update_id'], second['update_id']])
        self.assertEqual([json.loads(r['message_json'])['photo'][0]['file_id'] for r in rows],
                         ['photo-1', 'photo-2'])
        self.assertTrue(all(r['media_group_id'] == 'one-album' and r['message_thread_id'] == 11 for r in rows))
        self.assertEqual(self.router.picker.guard.pending_count(), 0)
        self.assert_no_selector()
        self.assert_no_all_echo()

    async def test_a_late_album_part_is_delivered_as_new_input_without_reimporting_earlier_parts(self):
        first, _ = await self.send('', media_group_id='late-album', photo=[{'file_id': 'one'}])
        self.now += 3
        await self.pump()
        second, _ = await self.send('', media_group_id='late-album', photo=[{'file_id': 'two'}])
        self.assertEqual([r['id'] for r in self.inbox()], [first['update_id']])
        self.now += 3
        await self.pump()
        self.assertEqual([r['id'] for r in self.inbox()], [first['update_id'], second['update_id']])
        self.assert_no_selector()

    async def test_replay_and_restart_deliver_input_once_without_adding_a_topic_echo(self):
        update = self.message('跨重启只投递一次')
        self.router.ingest(update)
        self.reload_router()
        await self.router.picker.recover()
        await self.pump()
        self.router.ingest(copy.deepcopy(update))
        await self.pump()
        self.reload_router()
        await self.router.picker.recover()
        self.router.ingest(copy.deepcopy(update))
        await self.pump()
        self.assertEqual([(r['id'], r['text']) for r in self.inbox()],
                         [(update['update_id'], '跨重启只投递一次')])
        self.assert_no_all_echo()
        self.assert_no_selector()

    async def test_crash_after_input_import_preserves_no_echo_when_a_second_target_is_added(self):
        update = self.message('导入后中断仍只投一次')
        self.router.ingest(update)
        with patch.object(self.router.recent, 'record', side_effect=RuntimeError('after durable input import')):
            with self.assertRaises(RuntimeError):
                await self.router.picker.guard.present(update)
        self.assertEqual(len(self.inbox()), 1)
        self.assert_no_all_echo()
        self.add_other()
        self.reload_router()
        self.router.picker.guard.recover()
        self.router.picker.guard.recover()
        self.router.ingest(copy.deepcopy(update))
        await self.pump()
        self.assertEqual(len(self.inbox()), 1)
        self.assert_no_all_echo()
        self.assertEqual(self.router.picker.guard.pending_count(), 0)

    async def test_multi_target_handoff_still_recovers_its_echo_after_other_target_is_removed(self):
        self.add_other()
        update, panel = await self.send('多会话中选中的输入仍需回显')
        child = self.router.bridges['meeting']
        with patch.object(child.store, 'enqueue_at', side_effect=OSError('before echo commit')):
            await self.click(panel, '会议记录')
        self.assertEqual([(r['id'], r['text']) for r in self.inbox()],
                         [(update['update_id'], '多会话中选中的输入仍需回显')])
        self.assert_no_all_echo()
        self.remove_other()
        self.reload_router()
        await self.router.picker.recover()
        await self.router.picker.recover()
        self.assertEqual(len(self.inbox()), 1)
        echoes = self.router.bridges['meeting'].store.db.execute(
            "SELECT text,message_thread_id FROM outbox WHERE key LIKE 'main-guard-input:%'").fetchall()
        self.assertEqual([(r['text'], r['message_thread_id']) for r in echoes],
                         [('📩 你在 All 说：\n多会话中选中的输入仍需回显', 11)])

    async def test_upgraded_pending_singleton_draft_uses_no_echo_for_its_new_handoff(self):
        update = self.message('旧版尚未投递的单会话输入')
        self.router.ingest(update)
        with self.router.store.db:
            self.router.store.db.execute('ALTER TABLE main_guard_drafts DROP COLUMN mirror_input')
        self.reload_router()
        await self.router.picker.recover()
        await self.pump()
        self.assertEqual([(r['id'], r['text']) for r in self.inbox()],
                         [(update['update_id'], '旧版尚未投递的单会话输入')])
        self.assert_no_all_echo()

    async def test_upgraded_legacy_releasing_singleton_keeps_one_echo_without_reimporting(self):
        update = self.message('旧版已经开始投递的输入')
        self.router.ingest(update)
        with patch.object(self.router.recent, 'record', side_effect=RuntimeError('old interrupted handoff')):
            with self.assertRaises(RuntimeError):
                await self.router.picker.guard.present(update)
        self.assertEqual(len(self.inbox()), 1)
        with self.router.store.db:
            self.router.store.db.execute('ALTER TABLE main_guard_drafts DROP COLUMN mirror_input')
        self.reload_router()
        await self.router.picker.recover()
        await self.router.picker.recover()
        self.assertEqual(len(self.inbox()), 1)
        echoes = self.router.bridges['meeting'].store.db.execute(
            "SELECT text FROM outbox WHERE key LIKE 'main-guard-input:%'").fetchall()
        self.assertEqual([r['text'] for r in echoes], ['📩 你在 All 说：\n旧版已经开始投递的输入'])

    async def test_singleton_preview_and_completed_packets_contain_only_assistant_output(self):
        original = 'USER_INPUT_ONLY 不应在预览或完成消息中重复'
        update, _ = await self.send(original)
        child = self.router.bridges['meeting']
        shown = []

        async def display(method, **params):
            shown.append((method, copy.deepcopy(params)))
            return {'message_id': 800 + len(shown)}

        child.telegram = type('Telegram', (), {'call': staticmethod(display)})()
        child.delivery = CompletionDelivery(child, clock=lambda: self.now)
        child.store.mark(update['update_id'], 'running', 'singleton-work')
        turn = {'id': 'singleton-work', 'status': 'inProgress', 'startedAt': self.now}
        child.store.enqueue_at('turn:singleton-work:progress', child.owner, '正在整理材料', 11)
        await child.delivery.update(child.telegram, turns=[turn])
        preview_calls = copy.deepcopy(shown)
        self.assertTrue(any('正在整理材料' in json.dumps(params, ensure_ascii=False)
                            for _, params in preview_calls))
        child.store.enqueue_at('turn:singleton-work:final', child.owner, '整理完成，结果如下。', 11)
        child.delivery.seal({**turn, 'status': 'completed'})
        packets = []
        for _ in range(10):
            row = child.delivery.next_row()
            if row is None:
                break
            packets.append(row['text'])
            await child.send_one(row)
        else:
            self.fail('Completed singleton output failed to drain')
        self.assertEqual(packets, ['正在整理材料', '整理完成，结果如下。'])
        for _, params in shown:
            displayed = json.dumps(params, ensure_ascii=False)
            self.assertNotIn('USER_INPUT_ONLY', displayed)
            self.assertNotIn('你在 All 说', displayed)
        self.assertEqual(self.inbox()[0]['text'], original)
        self.assert_no_all_echo()

    async def test_singleton_still_mirrors_new_codex_client_input_exactly_once(self):
        child = self.router.bridges['meeting']
        child.config['native_mirror'] = True
        child.mirror = NativeMirror(child.store, clock=lambda: self.now)
        native_text = '**Codex客户端输入**\n第二行保留'

        class Native:
            async def items(self, turn_id):
                yield {'item': {'id': 'desktop-input', 'type': 'userMessage',
                               'content': [{'type': 'text', 'text': native_text}]}}

        child.codex = Native()
        await child.update_native_mirror([])
        self.now += 1
        turn = {'id': 'desktop-work', 'status': 'inProgress', 'startedAt': self.now}
        await child.update_native_mirror([turn])
        self.now += 16
        await child.update_native_mirror([turn])
        echoes = child.store.db.execute(
            "SELECT text,rich_text,message_thread_id FROM outbox WHERE key LIKE 'native-user:%'").fetchall()
        self.assertEqual(len(echoes), 1)
        self.assertEqual(echoes[0]['text'], '🖥 你在 Codex 客户端说：\n' + native_text)
        self.assertEqual(echoes[0]['message_thread_id'], 11)
        self.assertIn('> ', echoes[0]['rich_text'])
        self.assert_no_all_echo()

    async def test_two_bound_targets_are_not_reduced_to_one_by_visibility_or_availability(self):
        self.add_other()
        self.router.targets['instagram']['topic_missing'] = True
        self.router.bridges['instagram'].owner_allowed = lambda: False
        await self.send('两个绑定仍需要选择')
        self.assertEqual(self.inbox(), [])
        self.assertEqual(self.inbox('instagram'), [])
        self.assertEqual(self.router.picker.guard.pending_count(), 1)

    async def test_invalid_sole_target_is_held_and_does_not_run_after_it_is_repaired(self):
        self.router.targets['meeting']['topic_missing'] = True
        update = self.message('接收时目标无效')
        self.router.ingest(update)
        self.router.targets['meeting']['topic_missing'] = False
        await self.pump()
        self.assertEqual(self.inbox(), [])
        self.assertEqual(self.router.picker.guard.pending_count(), 1)

    async def test_scope_revoked_after_acceptance_blocks_automatic_handoff(self):
        from test_session_scope_integration import ScopeIntegrationTests
        ScopeIntegrationTests.enable_scope(self)
        update = self.message('归属迁出后不应投递')
        self.router.ingest(update)
        self.assignments['meeting-native'] = 'research'
        ScopeIntegrationTests.write_scope(self)
        await self.pump()
        self.assertEqual(self.inbox(), [])

    def write_scope(self):
        from test_session_scope_integration import ScopeIntegrationTests
        ScopeIntegrationTests.write_scope(self)

    async def test_owner_access_revoked_after_acceptance_blocks_automatic_handoff(self):
        self.router.ingest(self.message('权限撤销后不应发送'))
        (self.path / 'access.json').write_text(json.dumps({'dmPolicy': 'allowlist', 'allowFrom': []}))
        await self.pump()
        self.assertEqual(self.inbox(), [])

    async def test_foreign_sender_does_not_create_a_draft_or_reach_the_only_thread(self):
        await self.send('外人不能借单目标进入', sender=999)
        self.assertEqual(self.inbox(), [])
        self.assertEqual(self.router.picker.guard.pending_count(), 0)

    async def test_a_second_binding_added_before_handoff_requires_explicit_selection(self):
        self.router.ingest(self.message('接收后增加了另一个目标'))
        self.add_other()
        await self.pump()
        self.assertEqual(self.inbox(), [])
        self.assertEqual(self.inbox('instagram'), [])
        self.assertEqual(self.router.picker.guard.pending_count(), 1)

    async def test_binding_native_or_topic_changes_cannot_retarget_a_frozen_singleton(self):
        for field, value in [('thread_id', 'replacement-native'), ('message_thread_id', 999)]:
            with self.subTest(field=field):
                self.router.ingest(self.message('冻结目标不能跟随更换 ' + field))
                original = self.router.targets['meeting'][field]
                self.router.targets['meeting'][field] = value
                await self.pump()
                self.assertEqual(self.inbox(), [])
                self.router.targets['meeting'][field] = original
        self.assertEqual(self.router.picker.guard.pending_count(), 2)

    async def test_already_queued_singleton_still_rechecks_identity_before_native_dispatch(self):
        await self.send('排队后也不能改投')
        child = self.router.bridges['meeting']
        child.thread_id = 'replacement-native'
        child.codex = type('Native', (), {'call': AsyncMock(side_effect=AssertionError('Must not run'))})()
        await child.dispatch_one(child.store.pending())
        child.codex.call.assert_not_awaited()
        self.assertIn(self.inbox()[0]['state'], ('blocked', 'failed'))

    async def test_unpresented_multi_target_draft_is_not_auto_released_after_only_one_remains(self):
        self.add_other()
        old = self.message('之前需要选择的消息')
        self.router.ingest(old)
        self.remove_other()
        self.reload_router()
        await self.router.picker.recover()
        await self.pump()
        self.assertEqual(self.inbox(), [])
        self.assertEqual(self.router.picker.guard.pending_count(), 1)
        new, _ = await self.send('现在新消息可以直发')
        self.assertEqual([r['id'] for r in self.inbox()], [new['update_id']])

    async def test_legacy_sql_null_policy_cannot_be_auto_released_after_upgrade(self):
        update = self.message('升级前接收的旧消息')
        self.router.ingest(update)
        with self.router.store.db:
            self.router.store.db.execute('UPDATE main_guard_messages SET singleton_binding_json=NULL WHERE update_id=?',
                                         (update['update_id'],))
        self.reload_router()
        await self.router.picker.recover()
        await self.pump()
        self.assertEqual(self.inbox(), [])
        self.assertEqual(self.router.picker.guard.pending_count(), 1)

    async def test_album_with_legacy_member_is_not_authorized_by_a_new_singleton_member(self):
        old = self.message('', media_group_id='legacy-parts', photo=[{'file_id': 'old-part'}])
        self.router.ingest(old)
        with self.router.store.db:
            self.router.store.db.execute('UPDATE main_guard_messages SET singleton_binding_json=NULL WHERE update_id=?',
                                         (old['update_id'],))
        self.router.ingest(self.message('', media_group_id='legacy-parts', photo=[{'file_id': 'new-part'}]))
        self.now += 3
        await self.pump()
        self.assertEqual(self.inbox(), [])
        self.assertEqual(self.router.picker.guard.pending_count(), 1)

    async def test_album_member_received_while_two_targets_exist_prevents_automatic_release(self):
        self.router.ingest(self.message('', media_group_id='mixed-policy', photo=[{'file_id': 'single-part'}]))
        self.add_other()
        self.router.ingest(self.message('', media_group_id='mixed-policy', photo=[{'file_id': 'two-target-part'}]))
        self.remove_other()
        self.now += 3
        await self.pump()
        self.assertEqual(self.inbox(), [])
        self.assertEqual(self.router.picker.guard.pending_count(), 1)

    async def test_presented_album_is_not_auto_released_when_a_later_part_arrives_with_one_target(self):
        self.router.ingest(self.message('', media_group_id='already-presented', photo=[{'file_id': 'old'}]))
        self.add_other()
        self.now += 3
        await self.pump()
        self.assertTrue(self.telegram.messages)
        self.remove_other()
        self.router.ingest(self.message('', media_group_id='already-presented', photo=[{'file_id': 'late'}]))
        self.now += 3
        await self.pump()
        self.assertEqual(self.inbox(), [])
        self.assertEqual(self.router.picker.guard.pending_count(), 1)

    async def test_uncertain_selector_send_cannot_later_be_reinterpreted_as_automatic_permission(self):
        self.router.ingest(self.message('', media_group_id='uncertain-selector', photo=[{'file_id': 'old'}]))
        self.add_other()
        self.now += 3
        with patch.object(self.telegram, 'call', new=AsyncMock(side_effect=ConnectionError('Panel ACK unknown'))):
            await self.pump()
        self.remove_other()
        self.router.ingest(self.message('', media_group_id='uncertain-selector', photo=[{'file_id': 'new'}]))
        self.now += 3
        await self.pump()
        self.assertEqual(self.inbox(), [])
        self.assertEqual(self.router.picker.guard.pending_count(), 1)

    async def test_new_album_part_does_not_revive_expired_automatic_permission(self):
        self.router.ingest(self.message('', media_group_id='expired-album', photo=[{'file_id': 'expired'}]))
        self.now += 901
        self.router.ingest(self.message('', media_group_id='expired-album', photo=[{'file_id': 'new'}]))
        self.now += 3
        await self.pump()
        self.assertEqual(self.inbox(), [])
        self.assertEqual(self.router.picker.guard.pending_count(), 1)

    async def test_foreign_or_unresolved_reply_does_not_fall_back_to_the_only_target(self):
        references = [
            {'reply_to_message': {'message_id': 70, 'message_thread_id': 999, 'text': 'unknown topic'}},
            {'external_reply': {'message_id': 70, 'chat': {'id': -999, 'type': 'supergroup'}}},
        ]
        for reference in references:
            await self.send('引用目标不可确认', **reference)
        self.assertEqual(self.inbox(), [])
        self.assertEqual(self.router.picker.guard.pending_count(), len(references))

    async def test_expired_unpresented_singleton_draft_does_not_run(self):
        self.router.ingest(self.message('过期的自动发送资格'))
        self.now += 901
        await self.pump()
        self.assertEqual(self.inbox(), [])
        self.assertEqual(self.router.picker.guard.pending_count(), 1)

    async def test_all_reply_still_omits_parent_text_and_inside_thread_reply_keeps_it(self):
        parent = {'message_id': 70, 'message_thread_id': 11, 'text': 'PARENT_ONLY 引用正文'}
        first, _ = await self.send('ALL_NEW', reply_to_message=parent,
                                   quote={'text': 'PARENT_ONLY', 'position': 0})
        row = next(r for r in self.inbox() if r['id'] == first['update_id'])
        self.assertFalse(json.loads(row['reply_json']) if row['reply_json'] else None)
        self.assertEqual(row['text'], 'ALL_NEW')
        inside = self.message('THREAD_NEW', topic=11, reply_to_message=parent)
        self.router.ingest(inside)
        self.assertTrue(self.router.forward_one())
        row = next(r for r in self.inbox() if r['id'] == inside['update_id'])
        self.assertIn('PARENT_ONLY', json.loads(row['reply_json'])['text'])

    async def test_commands_alias_dispatch_and_creator_prompt_replies_bypass_singleton_guard(self):
        for command in ['/sessions', '/model', '/effort', '/format', '/stop']:
            self.router.ingest(self.message(command))
        await self.pump()
        self.assertEqual(self.router.store.db.execute('SELECT COUNT(*) FROM main_guard_messages').fetchone()[0], 0)
        for text in ['@meeting 显式别名任务', '/to meeting 显式派单任务']:
            self.router.ingest(self.message(text))
            await self.router.control.handle_one()
        requests = self.router.store.db.execute('SELECT payload FROM cc_requests').fetchall()
        self.assertEqual(len(requests), 2)
        self.assertTrue(all(json.loads(row['payload'])['thread_id'] == 'meeting-native' for row in requests))
        self.assertEqual(self.inbox(), [])
        menu = await self.menu(topic_id=None)
        browser = await self.click(menu, '新建会话')
        await self.click(browser, '使用当前目录')
        prompt = self.telegram.messages[self.telegram.next_id]
        await self.send('Media 新名字', reply_to_message=copy.deepcopy(prompt))
        self.assertEqual(self.router.picker.guard.pending_count(), 0)
        self.assertEqual(self.inbox(), [])

    async def test_rename_prompt_reply_is_not_a_new_model_task(self):
        menu = await self.menu(topic_id=None)
        detail = await self.click(menu, '会议记录')
        await self.click(detail, '修改名称')
        prompt = max((m for m in self.telegram.messages.values()
                      if m.get('reply_markup', {}).get('force_reply')), key=lambda m: m['message_id'])
        await self.send('新的 Media 名称', reply_to_message=copy.deepcopy(prompt))
        self.assertEqual(self.router.picker.guard.pending_count(), 0)
        self.assertEqual(self.inbox(), [])
        self.assertTrue(all(method in ('thread/read', 'thread/list') for method, _ in self.native_calls))


if __name__ == '__main__':
    unittest.main()
