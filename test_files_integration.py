"""Files UI routing and status controls without real Telegram or native calls."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from completion_preview import CompletionPreview
from topics_bridge import TopicRouter


class FilesRouterIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name)
        (self.path / 'access.json').write_text(json.dumps({
            'dmPolicy': 'allowlist', 'allowFrom': ['123']}))
        self.config = {
            'owner_id': '123', 'host_id': 'local', 'mode': 'topics',
            'telegram_state_dir': str(self.path), 'expected_bot_username': 'test_bot',
            'socket_path': str(self.path / 'unused.sock'),
            'thread_id': 'native-manager', 'root_target': 'manager',
            'notification_mode': 'completion', 'preview_style': 'status-bar',
            'native_mirror': False,
            'targets': [
                {'key': 'manager', 'name': 'Manager', 'thread_id': 'native-manager',
                 'message_thread_id': 11},
                {'key': 'other', 'name': 'Other', 'thread_id': 'native-other',
                 'message_thread_id': 22},
            ],
        }
        (self.path / 'config.json').write_text(json.dumps(self.config))
        self.router = TopicRouter(copy.deepcopy(self.config), self.path)
        self.router.bot_id, self.router.username = 456, 'test_bot'
        self.calls, self.messages, self.sequence = [], {}, 1000
        tests = self

        class Telegram:
            async def call(self, method, **params):
                tests.calls.append((method, copy.deepcopy(params)))
                if method == 'answerCallbackQuery':
                    return True
                if method in ('sendMessage', 'sendRichMessage', 'sendPhoto', 'sendDocument'):
                    tests.sequence += 1
                    result = {**copy.deepcopy(params), 'message_id': tests.sequence,
                              'from': {'id': 456, 'is_bot': True},
                              'chat': {'id': int(params['chat_id']), 'type': 'private'}, 'date': 1}
                    tests.messages[tests.sequence] = result
                    return copy.deepcopy(result)
                if method in ('editMessageText', 'editMessageReplyMarkup'):
                    result = tests.messages[params['message_id']]
                    result.update(copy.deepcopy(params))
                    return copy.deepcopy(result)
                if method == 'deleteMessage':
                    tests.messages.pop(params['message_id'], None)
                    return True
                raise AssertionError('Unexpected Telegram method: ' + method)

            async def upload(self, method, field, path, **params):
                return await self.call(method, **params, field=field, path=str(path))

        self.router.telegram = Telegram()
        for bridge in self.router.bridges.values():
            bridge.telegram = self.router.telegram
            bridge.ready.set()
            bridge.delivery.clock = lambda: 1000
            bridge.codex.call = AsyncMock(side_effect=AssertionError('Files UI must not call native RPC'))

    async def asyncTearDown(self):
        for bridge in self.router.bridges.values():
            bridge.store.db.close()
        self.router.store.db.close()
        self.tmp.cleanup()

    @staticmethod
    def message(update_id, text='/files', topic=22, **extra):
        message = {'message_id': update_id + 2000, 'date': 1, 'text': text,
                   'from': {'id': 123}, 'chat': {'id': 123, 'type': 'private'}}
        if topic is not None:
            message['message_thread_id'] = topic
        message.update(extra)
        return {'update_id': update_id, 'message': message}

    @staticmethod
    def callback(update_id, data, message):
        return {'update_id': update_id, 'callback_query': {
            'id': 'callback-' + str(update_id), 'from': {'id': 123},
            'message': copy.deepcopy(message), 'data': data}}

    def rows(self, table):
        return self.router.store.db.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0]

    def assert_no_native(self):
        for bridge in self.router.bridges.values():
            bridge.codex.call.assert_not_awaited()

    def formal(self):
        return [(method, params) for method, params in self.calls
                if method in ('sendMessage', 'sendRichMessage', 'sendPhoto', 'sendDocument')]

    @staticmethod
    def controls(markup):
        return [button for row in markup['inline_keyboard'] for button in row
                if button.get('callback_data')]

    async def open_files(self, update_id=100, topic=22):
        self.router.ingest(self.message(update_id, topic=topic))
        await self.router.files_menu.handle_one()
        return self.formal()[-1][1]

    def seed_upload(self, key, update_id, name):
        bridge = self.router.bridges[key]
        message = self.message(update_id, text='', topic=bridge.target['message_thread_id'],
                               document={'file_id': 'file-' + name, 'file_unique_id': 'unique-' + name,
                                         'file_name': name, 'file_size': 123, 'mime_type': 'text/plain'})['message']
        bridge.store.accept({'update_id': update_id}, message)
        bridge.store.mark(update_id, 'done')

    async def test_menu_is_shared_by_existing_and_newly_installed_bridges(self):
        for bridge in self.router.bridges.values():
            self.assertIs(bridge.files_menu, self.router.files_menu)
        await self.router.install_target({'key': 'new', 'name': 'New', 'thread_id': 'native-new',
                                          'message_thread_id': 33})
        self.assertIs(self.router.bridges['new'].files_menu, self.router.files_menu)
        self.router.bridges['new'].codex.call = AsyncMock(
            side_effect=AssertionError('Files UI must not call native RPC'))
        self.assert_no_native()

    async def test_files_commands_bypass_native_input_and_remain_responsive_while_busy(self):
        bridge = self.router.bridges['other']
        await bridge.dispatch_lock.acquire()
        try:
            bridge.ready.clear()
            for update in (self.message(100), self.message(101, text='/files@test_bot', topic=11)):
                self.router.ingest(update)
            self.assertEqual(self.rows('fl_jobs'), 2)
            for table in ('jobs', 'cc_jobs', 'cc_requests', 'ui_jobs'):
                self.assertEqual(self.rows(table), 0)
            self.assertEqual(self.router.picker.guard.pending_count(), 0)
            await self.router.files_menu.handle_one()
            await self.router.files_menu.handle_one()
            self.assertEqual([p.get('message_thread_id') for _, p in self.formal()], [22, 11])
            self.assertTrue(all(b.store.pending() is None for b in self.router.bridges.values()))
            self.assert_no_native()
        finally:
            bridge.dispatch_lock.release()

    async def test_all_files_command_gives_local_hint_without_listing_another_threads_files(self):
        self.seed_upload('manager', 1, 'manager-private.txt')
        self.seed_upload('other', 2, 'other-private.txt')
        shown = await self.open_files(topic=None)
        self.assertIsNone(shown.get('message_thread_id'))
        self.assertIn('话题', shown['text'])
        self.assertNotIn('manager-private.txt', shown['text'])
        self.assertNotIn('other-private.txt', shown['text'])
        self.assert_no_native()

    async def test_thread_file_list_contains_only_its_own_uploads(self):
        self.seed_upload('manager', 1, 'manager-only.txt')
        self.seed_upload('other', 2, 'other-only.txt')
        shown = await self.open_files()
        display = shown['text'] + json.dumps(shown.get('reply_markup', {}), ensure_ascii=False)
        self.assertEqual(shown['message_thread_id'], 22)
        self.assertIn('other-only.txt', display)
        self.assertNotIn('manager-only.txt', display)
        self.assertEqual(self.rows('jobs'), 0)
        self.assert_no_native()

    async def test_unbound_topic_does_not_fall_back_to_root_files(self):
        self.seed_upload('manager', 1, 'manager-private.txt')
        shown = await self.open_files(topic=999)
        display = shown['text'] + json.dumps(shown.get('reply_markup', {}), ensure_ascii=False)
        self.assertEqual(shown['message_thread_id'], 999)
        self.assertNotIn('manager-private.txt', display)
        self.assertEqual(self.rows('jobs'), 0)
        self.assert_no_native()

    async def test_opener_copied_to_other_topic_cannot_list_original_thread_files(self):
        self.seed_upload('other', 1, 'other-private.txt')
        bridge = self.router.bridges['other']
        opener = self.router.files_menu.entry_callback(bridge)
        origin = {'message_id': 88, 'date': 1, 'from': {'id': 456, 'is_bot': True},
                  'chat': {'id': 123, 'type': 'private'}, 'message_thread_id': 11,
                  'text': 'copied button'}
        self.router.ingest(self.callback(100, opener, origin))
        await self.router.files_menu.handle_one()
        for method, shown in self.calls:
            self.assertNotIn('other-private.txt', json.dumps(shown, ensure_ascii=False))
            if method in ('sendMessage', 'sendRichMessage'):
                self.assertEqual(shown.get('message_thread_id'), 11)
        self.assertEqual(self.rows('jobs'), 0)
        self.assert_no_native()

    def test_files_caption_and_forwarded_text_keep_normal_attachment_and_topic_routing(self):
        updates = [
            self.message(100, text='', caption='/files', document={'file_id': 'document'}),
            self.message(101, photo=[{'file_id': 'photo'}]),
            self.message(102, forward_origin={'type': 'hidden_user', 'sender_user_name': 'Example'}),
        ]
        for update in updates:
            self.router.ingest(update)
        self.assertEqual(self.rows('fl_jobs'), 0)
        while self.router.forward_one():
            pass
        rows = self.router.bridges['other'].store.db.execute(
            'SELECT text,message_json,message_thread_id FROM inbox ORDER BY id').fetchall()
        self.assertEqual(len(rows), 3)
        self.assertTrue(all(row['message_thread_id'] == 22 for row in rows))
        self.assertEqual(json.loads(rows[0]['message_json']), {
            'caption': '/files', 'document': {'file_id': 'document'}})
        self.assertEqual(json.loads(rows[1]['message_json']), {'photo': [{'file_id': 'photo'}]})
        self.assertEqual(rows[2]['text'], '/files')
        self.assertIsNone(self.router.bridges['manager'].store.pending())
        self.assert_no_native()

    def test_session_name_force_reply_keeps_literal_files_as_wizard_input(self):
        parent = {'message_id': 77, 'from': {'id': 456},
                  'text': '新建 Codex 会话 · 会话名称\n请输入名称'}
        with patch.object(self.router.files_menu, 'ingest', wraps=self.router.files_menu.ingest) as ingest:
            self.router.ingest(self.message(100, topic=None, reply_to_message=parent))
        ingest.assert_not_called()
        self.assertEqual(self.rows('fl_jobs'), 0)
        self.assertEqual(self.rows('ui_jobs'), 1)
        queued = json.loads(self.router.store.db.execute('SELECT payload FROM ui_jobs').fetchone()[0])
        self.assertEqual(queued['message']['text'], '/files')
        self.assertEqual(queued['message']['reply_to_message'], parent)
        self.assert_no_native()

    def test_question_force_reply_bypasses_files_parser_and_preserves_quote_context(self):
        with self.router.store.db:
            self.router.store.db.execute('INSERT INTO choice_prompts '
                '(chat_id,message_id,topic_id,bot_id,question_id,position) VALUES(?,?,?,?,?,?)',
                ('123', 77, 22, 456, 'native-question', 0))
        parent = {'message_id': 77, 'from': {'id': 456}, 'text': '回答 Codex 问题：'}
        with patch.object(self.router.files_menu, 'ingest', wraps=self.router.files_menu.ingest) as ingest:
            self.router.ingest(self.message(100, reply_to_message=parent))
            self.router.ingest(self.message(101, text='正常回答', reply_to_message=parent))
        ingest.assert_not_called()
        self.assertEqual(self.rows('fl_jobs'), 0)
        self.assertEqual(self.rows('choice_jobs'), 1)
        queued = json.loads(self.router.store.db.execute('SELECT payload FROM choice_jobs').fetchone()[0])
        self.assertEqual(queued['message']['text'], '正常回答')
        self.assertEqual(queued['message']['reply_to_message'], parent)
        self.assert_no_native()

    def test_unauthorized_commands_do_not_reach_menu_or_any_session(self):
        for update in (self.message(100, **{'from': {'id': 999}}),
                       self.message(101, **{'chat': {'id': -100, 'type': 'supergroup'}}),
                       self.message(102, **{'from': {'id': 123, 'is_bot': True}})):
            self.router.ingest(update)
        self.assertEqual(self.rows('fl_jobs'), 0)
        self.assertEqual(self.rows('jobs'), 0)
        self.assertTrue(all(b.store.pending() is None for b in self.router.bridges.values()))
        self.assert_no_native()

    async def test_status_files_entry_opens_same_thread_without_steering_or_losing_stop(self):
        bridge = self.router.bridges['other']
        bridge.store.enqueue_at('turn:working:progress', bridge.owner, '处理中的正文', 22)
        await bridge.delivery.update(self.router.telegram, turns=[{
            'id': 'working', 'status': 'inProgress', 'startedAt': 999}])
        group = bridge.delivery._group('working')
        preview = bridge.delivery.body_preview
        message = self.messages[preview.load(dict(group))['message_id']]
        rows = message['reply_markup']['inline_keyboard']
        self.assertEqual(rows[0][1]['text'], '📎 文件')
        self.assertTrue(rows[0][1]['callback_data'].startswith('fl:'))
        self.assertTrue(rows[1][1]['callback_data'].startswith('stop:'))
        original = copy.deepcopy(message['reply_markup'])
        self.router.ingest(self.callback(100, rows[0][1]['callback_data'], message))
        await self.router.files_menu.handle_one()
        self.assertEqual(self.formal()[-1][1]['message_thread_id'], 22)
        self.assertEqual(message['reply_markup'], original)
        self.assertEqual(self.rows('jobs'), 0)
        self.assertTrue(all(b.store.pending() is None for b in self.router.bridges.values()))
        self.assert_no_native()

    async def test_terminal_reply_keeps_files_button_and_removes_stop(self):
        bridge = self.router.bridges['other']
        bridge.store.enqueue_at('turn:finished:answer', bridge.owner, '最终结果', 22)
        bridge.delivery.watch({'id': 'finished', 'status': 'inProgress', 'startedAt': 999})
        bridge.delivery.seal({'id': 'finished', 'status': 'completed', 'startedAt': 999})
        bridge.delivery.next_row()
        group = bridge.delivery._group('finished')
        row = bridge.store.db.execute('SELECT * FROM outbox WHERE key=?', (group['notice_key'],)).fetchone()
        markup = bridge.delivery.final_markup(row)
        controls = self.controls(markup)
        self.assertEqual([button['text'] for button in controls], ['📎 文件'])
        self.assertTrue(controls[0]['callback_data'].startswith('fl:'))
        self.assertEqual([len(line) for line in markup['inline_keyboard']], [2, 1])
        self.assert_no_native()

    async def test_optional_file_entry_failure_does_not_suppress_status_or_stop(self):
        bridge = self.router.bridges['other']
        bridge.store.enqueue_at('turn:working:progress', bridge.owner, '处理中的正文', 22)
        with patch.object(self.router.files_menu, 'entry_callback', side_effect=ValueError('optional UI')):
            await bridge.delivery.update(self.router.telegram, turns=[{
                'id': 'working', 'status': 'inProgress', 'startedAt': 999}])
        group = bridge.delivery._group('working')
        preview = bridge.delivery.body_preview
        shown = self.messages[preview.load(dict(group))['message_id']]
        markup = shown['reply_markup']
        self.assertEqual([len(row) for row in markup['inline_keyboard']], [1, 2])
        self.assertEqual([b['text'] for b in self.controls(markup)], ['⏹ 停止当前任务'])
        self.assert_no_native()

    async def test_wrong_status_identity_cannot_get_a_files_entry(self):
        bridge = self.router.bridges['other']
        preview = bridge.delivery.body_preview
        current = {'native_id': bridge.thread_id, 'chat_id': '123', 'topic_id': 22,
                   'turn_id': 'current'}
        for identity in (dict(current, native_id='native-manager'),
                         dict(current, chat_id='999'), dict(current, topic_id=11)):
            self.assertIsNone(preview.files_callback(identity))
        bridge.target['topic_missing'] = True
        self.assertIsNone(preview.files_callback(current))
        self.assert_no_native()

    def test_legacy_preview_without_menu_preserves_two_status_rows_and_stop(self):
        bridge = self.router.bridges['other']
        del bridge.files_menu
        preview = bridge.delivery.body_preview
        self.assertIsNone(preview.files_callback())
        plain = CompletionPreview.status_markup('工作中\n1 秒', 'stop:existing')
        self.assertEqual([len(row) for row in plain['inline_keyboard']], [1, 2])
        self.assertEqual(self.controls(plain), [{'text': '⏹ 停止当前任务', 'callback_data': 'stop:existing'}])
        self.assert_no_native()


if __name__ == '__main__':
    unittest.main()
