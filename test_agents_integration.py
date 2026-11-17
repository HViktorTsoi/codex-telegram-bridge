"""Agent browsing stays read-only and preserves the existing topic router."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from session_scope import SessionScope, SessionScopeError
from topics_bridge import TopicRouter


class AgentsRouterIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name)
        self.access_path = self.path / 'access.json'
        self.access_path.write_text(json.dumps({
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
        self.calls, self.native_calls, self.messages, self.sequence = [], [], {}, 1000
        self.native_threads = {}
        for index, name in enumerate(('manager', 'other')):
            root_id, child_id = 'native-' + name, 'child-' + name
            self.native_threads[root_id] = {
                'id': root_id, 'name': name.title(), 'source': 'cli',
                'canAcceptDirectInput': True, 'status': {'type': 'active'},
                'cwd': '/fixture/' + name, 'updatedAt': 100 - index,
            }
            self.native_threads[child_id] = {
                'id': child_id, 'name': name + '-agent',
                'source': {'subAgent': {'thread_spawn': {
                    'parent_thread_id': root_id, 'depth': 1,
                    'agent_path': '/root/' + name,
                }}},
                'parentThreadId': root_id, 'canAcceptDirectInput': False,
                'status': {'type': 'idle'}, 'preview': name + ' public task',
                'agentRole': 'researcher', 'updatedAt': 90 - index,
            }
        self.native_threads['grandchild-other'] = {
            'id': 'grandchild-other', 'name': 'other-historical-grandchild',
            'source': {'subAgent': {'thread_spawn': {
                'parent_thread_id': 'child-other', 'depth': 2,
                'agent_path': '/root/other/grandchild',
            }}},
            'parentThreadId': 'child-other', 'canAcceptDirectInput': False,
            'status': {'type': 'notLoaded'}, 'updatedAt': 80,
        }
        self.archived_ids = {'grandchild-other'}
        tests = self

        class Telegram:
            async def call(self, method, **params):
                tests.calls.append((method, copy.deepcopy(params)))
                if method == 'answerCallbackQuery':
                    return True
                if method in ('sendMessage', 'sendRichMessage'):
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

        self.router.telegram = Telegram()
        for key, bridge in self.router.bridges.items():
            bridge.telegram = self.router.telegram
            bridge.ready.set()

            async def native(method, params, key=key):
                tests.native_calls.append((key, method, copy.deepcopy(params)))
                # Any control/model RPC fails the test before it can do work.
                if method == 'thread/read':
                    return {'thread': copy.deepcopy(tests.native_threads[params['threadId']])}
                if method == 'thread/list':
                    root_id = params.get('ancestorThreadId')
                    rows = []
                    for row in tests.native_threads.values():
                        if bool(params.get('archived')) != (row['id'] in tests.archived_ids):
                            continue
                        if not root_id:
                            if isinstance(row.get('source'), str):
                                rows.append(row)
                            continue
                        parent = row.get('parentThreadId')
                        while parent and parent != root_id:
                            parent = tests.native_threads[parent].get('parentThreadId')
                        if parent == root_id:
                            rows.append(row)
                    return {'data': copy.deepcopy(rows), 'nextCursor': None}
                if method == 'thread/turns/list':
                    return {'data': [{'id': 'fixture-turn', 'status': 'completed', 'items': [
                        {'type': 'agentMessage', 'text': params['threadId'] + ' public answer'},
                    ]}], 'nextCursor': None}
                raise AssertionError('Agent UI must not call native control RPC: ' + method)

            bridge.codex.call = AsyncMock(side_effect=native)

    async def asyncTearDown(self):
        for bridge in self.router.bridges.values():
            bridge.store.db.close()
        self.router.store.db.close()
        self.tmp.cleanup()

    @staticmethod
    def message(update_id, text='/agents', topic=22, **extra):
        message = {'message_id': update_id + 2000, 'date': 1, 'text': text,
                   'from': {'id': 123}, 'chat': {'id': 123, 'type': 'private'}}
        if topic is not None:
            message['message_thread_id'] = topic
        message.update(extra)
        return {'update_id': update_id, 'message': message}

    def rows(self, table):
        return self.router.store.db.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0]

    def sent(self):
        return [params for method, params in self.calls
                if method in ('sendMessage', 'sendRichMessage')]

    async def open_agents(self, update_id=100, topic=22, **extra):
        self.router.ingest(self.message(update_id, topic=topic, **extra))
        self.assertTrue(await self.router.agents_menu.handle_one())
        return self.sent()[-1]

    @staticmethod
    def display(message):
        return message['text'] + json.dumps(message.get('reply_markup', {}), ensure_ascii=False)

    def assert_read_only(self):
        self.assertTrue(all(method in ('thread/list', 'thread/read', 'thread/turns/list')
                            for _, method, _ in self.native_calls))
        self.assertTrue(all(bridge.store.pending() is None
                            for bridge in self.router.bridges.values()))

    async def test_all_command_gives_topic_hint_without_catalog_or_manager_fallback(self):
        shown = await self.open_agents(topic=None)
        display = self.display(shown)
        self.assertIsNone(shown.get('message_thread_id'))
        self.assertIn('话题', display)
        self.assertNotIn('Manager', display)
        self.assertNotIn('Other', display)
        self.assertNotIn('manager public task', display)
        self.assertNotIn('other public task', display)
        self.assertEqual(self.native_calls, [])
        self.assertEqual(self.rows('jobs'), 0)
        self.assertEqual(self.router.picker.guard.pending_count(), 0)
        self.assert_read_only()

    async def test_topic_command_browses_its_native_root_and_keeps_its_topic(self):
        shown = await self.open_agents()
        self.assertEqual(shown['message_thread_id'], 22)
        self.assertIn('other-agent', self.display(shown))
        self.assertIn('other-historical-grandchild', self.display(shown))
        self.assertNotIn('manager-agent', self.display(shown))
        lists = [params for _, method, params in self.native_calls if method == 'thread/list']
        self.assertTrue(lists)
        self.assertTrue(all(params.get('ancestorThreadId') == 'native-other' for params in lists))
        self.assertEqual({params['archived'] for params in lists}, {False, True})
        self.assertFalse(any(params.get('threadId') == 'native-manager'
                             for _, _, params in self.native_calls))
        self.assertEqual(self.rows('jobs'), 0)
        self.assert_read_only()

    async def test_topic_command_remains_ui_only_while_its_dispatch_is_busy(self):
        bridge = self.router.bridges['other']
        await bridge.dispatch_lock.acquire()
        try:
            bridge.ready.clear()
            shown = await self.open_agents(text='/agents@test_bot')
            self.assertEqual(shown['message_thread_id'], 22)
            self.assertIn('other-agent', self.display(shown))
            for table in ('jobs', 'fl_jobs', 'cc_jobs', 'cc_requests', 'ui_jobs'):
                self.assertEqual(self.rows(table), 0)
            self.assert_read_only()
        finally:
            bridge.dispatch_lock.release()

    async def test_unbound_topic_does_not_fall_back_to_manager(self):
        shown = await self.open_agents(topic=999)
        self.assertEqual(shown['message_thread_id'], 999)
        self.assertNotIn('manager-agent', self.display(shown))
        self.assertNotIn('manager public task', self.display(shown))
        self.assertEqual(self.native_calls, [])
        self.assertEqual(self.rows('jobs'), 0)
        self.assert_read_only()

    def test_attachments_and_forwarded_commands_keep_original_topic_input(self):
        updates = [
            self.message(100, text='', caption='/agents', document={'file_id': 'document'}),
            self.message(101, photo=[{'file_id': 'photo'}]),
            self.message(102, forward_origin={'type': 'hidden_user', 'sender_user_name': 'Example'}),
            self.message(103, forward_date=1),
        ]
        for update in updates:
            self.router.ingest(update)
        self.assertEqual(self.rows('ag_jobs'), 0)
        while self.router.forward_one():
            pass
        rows = self.router.bridges['other'].store.db.execute(
            'SELECT text,message_json,message_thread_id FROM inbox ORDER BY id').fetchall()
        self.assertEqual(len(rows), len(updates))
        self.assertTrue(all(row['message_thread_id'] == 22 for row in rows))
        self.assertEqual(json.loads(rows[0]['message_json']), {
            'caption': '/agents', 'document': {'file_id': 'document'}})
        self.assertEqual(json.loads(rows[1]['message_json']), {'photo': [{'file_id': 'photo'}]})
        self.assertEqual([row['text'] for row in rows[1:]], ['/agents'] * 3)
        self.assertIsNone(self.router.bridges['manager'].store.pending())
        self.assertEqual(self.native_calls, [])

    def test_normal_topic_input_and_all_destination_guard_are_preserved(self):
        self.router.ingest(self.message(100, text='normal topic input'))
        self.router.ingest(self.message(101, text='All still chooses a destination', topic=None))
        self.assertEqual(self.rows('ag_jobs'), 0)
        self.assertEqual(self.router.picker.guard.pending_count(), 1)
        while self.router.forward_one():
            pass
        rows = self.router.bridges['other'].store.db.execute('SELECT text FROM inbox').fetchall()
        self.assertEqual([row['text'] for row in rows], ['normal topic input'])
        self.assertIsNone(self.router.bridges['manager'].store.pending())
        self.assertEqual(self.native_calls, [])

    def test_name_prompt_reply_preserves_literal_agents_as_wizard_input(self):
        parent = {'message_id': 77, 'from': {'id': 456},
                  'text': '新建 Codex 会话 · 会话名称\n请输入名称'}
        update = self.message(100, topic=None, reply_to_message=parent)
        with patch.object(self.router.agents_menu, 'ingest',
                          wraps=self.router.agents_menu.ingest) as ingest:
            self.router.ingest(update)
        ingest.assert_not_called()
        self.assertEqual(self.rows('ag_jobs'), 0)
        self.assertEqual(self.rows('ui_jobs'), 1)
        queued = json.loads(self.router.store.db.execute('SELECT payload FROM ui_jobs').fetchone()[0])
        self.assertEqual(queued, update)
        self.assertEqual(self.native_calls, [])

    def test_question_prompt_reply_bypasses_agents_parser_and_keeps_original_input(self):
        with self.router.store.db:
            self.router.store.db.execute('INSERT INTO choice_prompts '
                '(chat_id,message_id,topic_id,bot_id,question_id,position) VALUES(?,?,?,?,?,?)',
                ('123', 77, 22, 456, 'native-question', 0))
        parent = {'message_id': 77, 'from': {'id': 456}, 'text': '回答 Codex 问题：'}
        update = self.message(100, reply_to_message=parent, quote={'text': 'original quote', 'position': 0})
        with patch.object(self.router.agents_menu, 'ingest',
                          wraps=self.router.agents_menu.ingest) as ingest:
            self.router.ingest(update)
        ingest.assert_not_called()
        self.assertEqual(self.rows('ag_jobs'), 0)
        queued = json.loads(self.router.store.db.execute('SELECT message_json FROM jobs').fetchone()[0])
        self.assertEqual(queued, update['message'])
        self.assertEqual(self.native_calls, [])

    async def test_foreign_actor_chat_and_bot_cannot_enqueue_or_read_agents(self):
        for update in (self.message(100, **{'from': {'id': 999}}),
                       self.message(101, **{'chat': {'id': -100, 'type': 'supergroup'}}),
                       self.message(102, **{'from': {'id': 123, 'is_bot': True}})):
            self.router.ingest(update)
        self.assertEqual(self.rows('ag_jobs'), 0)
        self.assertEqual(self.rows('jobs'), 0)
        self.assertFalse(await self.router.agents_menu.handle_one())
        self.assertEqual(self.sent(), [])
        self.assertEqual(self.native_calls, [])
        self.assert_read_only()

    async def test_revoked_owner_cannot_enqueue_agents_or_read_native_metadata(self):
        self.access_path.write_text(json.dumps({'dmPolicy': 'allowlist', 'allowFrom': []}))
        self.router.ingest(self.message(100))
        self.assertEqual(self.rows('ag_jobs'), 0)
        self.assertFalse(await self.router.agents_menu.handle_one())
        self.assertEqual(self.sent(), [])
        self.assertEqual(self.native_calls, [])
        self.assert_read_only()

    async def test_authorized_parent_grants_child_browsing_without_weakening_catalog_scope(self):
        registry = self.path / 'scope.json'
        registry.write_text(json.dumps({
            'version': 1, 'scopes': ['general', 'research', 'development'],
            'default_scope': 'general',
            'assignments': {'native-manager': 'research', 'native-other': 'research'},
        }))
        original_registry = registry.read_bytes()
        self.router.config.update(session_scope_file=str(registry), session_scope='research')
        self.router.session_scope = SessionScope(self.router.config)
        for bridge in self.router.bridges.values():
            bridge.config.update(session_scope_file=str(registry), session_scope='research')
            bridge.session_scope = SessionScope(bridge.config)
        self.assertFalse(self.router.thread_allowed('child-other'))
        with self.assertRaises(SessionScopeError):
            await self.router.catalog_call('thread/read', {'threadId': 'child-other'})
        self.assertEqual(self.native_calls, [])
        catalog = self.router.agents_menu.catalog
        agents = await catalog.agents('native-other')
        self.assertEqual([agent['id'] for agent in agents], ['child-other', 'grandchild-other'])
        self.assertEqual([agent['depth'] for agent in agents], [1, 2])
        self.assertEqual([agent['archived'] for agent in agents], [False, True])
        detail = await catalog.detail('native-other', 'child-other')
        self.assertEqual(detail['id'], 'child-other')
        self.assertEqual(detail['root_id'], 'native-other')
        self.assertEqual(detail['latest_reply'], 'child-other public answer')
        self.assertEqual(registry.read_bytes(), original_registry)
        with self.assertRaises(SessionScopeError):
            await self.router.catalog_call('thread/read', {'threadId': 'child-other'})
        self.assertFalse(self.router.thread_allowed('child-other'))
        self.assertEqual(set(self.router.targets), {'manager', 'other'})
        self.assert_read_only()


if __name__ == '__main__':
    unittest.main()
