"""Shared-group smoke-test routing stays separate from private session input."""
import copy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from topics_bridge import TopicRouter


class GroupCollaborationIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name)
        (self.path / 'access.json').write_text(json.dumps({'dmPolicy': 'allowlist', 'allowFrom': ['123']}))
        self.config = {
            'owner_id': '123', 'host_id': 'local', 'telegram_state_dir': str(self.path),
            'expected_bot_username': 'test_bot', 'socket_path': str(self.path / 'unused.sock'),
            'thread_id': 'native-manager', 'root_target': 'manager',
            'notification_mode': 'completion', 'preview_style': 'status-bar',
            'targets': [
                {'key': 'manager', 'name': 'Telegram Manager', 'thread_id': 'native-manager', 'message_thread_id': 11},
                {'key': 'cad', 'name': 'CAD', 'thread_id': 'native-cad', 'message_thread_id': 22},
            ],
        }
        (self.path / 'config.json').write_text(json.dumps(self.config))
        self.router = TopicRouter(copy.deepcopy(self.config), self.path)
        self.router.bot_id, self.router.username = 456, 'test_bot'
        self.router.telegram = SimpleNamespace(call=AsyncMock(side_effect=AssertionError('No Telegram calls during ingest')))
        for bridge in self.router.bridges.values():
            bridge.codex.call = AsyncMock(side_effect=AssertionError('No native calls during ingest'))

    async def asyncTearDown(self):
        for bridge in self.router.bridges.values():
            bridge.store.db.close()
        self.router.store.db.close()
        self.tmp.cleanup()

    def enable(self):
        participants = [
            {'key': 'cad', 'thread_id': 'native-cad', 'topic_id': 22, 'role': 'CAD'},
            {'key': 'manager', 'thread_id': 'native-manager', 'topic_id': 11, 'role': 'Telegram Manager'},
        ]
        with self.router.store.db:
            self.router.store.db.execute('INSERT OR REPLACE INTO kv VALUES(?,?)',
                (self.router.collaboration.SETTINGS, json.dumps({'enabled': True, 'participants': participants})))
        return participants

    @staticmethod
    def message(update_id, text='/collab_test@test_bot', *, group=True, topic=None, **changes):
        message = {'message_id': update_id + 1000, 'date': 1, 'text': text, 'from': {'id': 123},
                   'chat': {'id': -100987 if group else 123, 'type': 'supergroup' if group else 'private'}}
        if topic is not None:
            message['message_thread_id'] = topic
        message.update(changes)
        return {'update_id': update_id, 'message': message}

    def rows(self, table):
        return self.router.store.db.execute('SELECT count(*) FROM ' + table).fetchone()[0]

    def assert_no_native_or_private_dispatch(self):
        for table in ('jobs', 'cc_jobs', 'cc_requests', 'ui_jobs', 'choice_jobs', 'usage_jobs'):
            self.assertEqual(self.rows(table), 0, table)
        self.assertEqual(self.router.picker.guard.pending_count(), 0)
        for bridge in self.router.bridges.values():
            self.assertEqual(bridge.store.db.execute('SELECT count(*) FROM inbox').fetchone()[0], 0)
            bridge.codex.call.assert_not_awaited()
        self.router.telegram.call.assert_not_awaited()

    async def test_disabled_feature_handles_only_its_explicit_command_without_model_dispatch(self):
        self.assertIsNone(self.router.collaboration._participants())
        self.router.ingest(self.message(100))
        self.assertEqual(self.rows('gc_jobs'), 1)
        await self.router.collaboration.handle_one()
        self.assertEqual(self.rows('gc_runs'), 0)
        self.assertEqual(self.rows('gc_outbox'), 1)
        text = self.router.store.db.execute('SELECT text FROM gc_outbox').fetchone()[0]
        self.assertIn('尚未启用', text)
        self.assert_no_native_or_private_dispatch()

    async def test_group_owner_command_enters_only_trial_lane_and_freezes_existing_bindings(self):
        participants = self.enable()
        update = self.message(100, topic=77)
        self.router.ingest(update)
        self.router.ingest(copy.deepcopy(update))
        self.assertEqual(self.rows('gc_jobs'), 1)
        self.assertEqual(self.router.store.offset(), 101)
        self.assert_no_native_or_private_dispatch()
        await self.router.collaboration.handle_one()
        run = self.router.store.db.execute('SELECT * FROM gc_runs').fetchone()
        self.assertEqual((run['chat_id'], run['topic_id'], run['state']), ('-100987', 77, 'cad_wait'))
        self.assertEqual(json.loads(run['participants']), participants)
        self.assert_no_native_or_private_dispatch()

    def test_foreign_bot_forwarded_attachment_and_other_bot_commands_never_enter_trial(self):
        self.enable()
        updates = [
            self.message(100, **{'from': {'id': 999}}),
            self.message(101, **{'from': {'id': 123, 'is_bot': True}}),
            self.message(102, text='/collab_test@another_bot'),
            self.message(103, forward_origin={'type': 'hidden_user', 'sender_user_name': 'Forwarded'}),
            self.message(104, document={'file_id': 'file', 'file_name': 'input.txt'}),
            self.message(105, photo=[{'file_id': 'image'}]),
            self.message(106, text='/collab_test extra text'),
        ]
        for update in updates:
            self.router.ingest(update)
        self.assertEqual(self.rows('gc_jobs'), 0)
        self.assertEqual(self.rows('gc_runs'), 0)
        self.assert_no_native_or_private_dispatch()

    def test_normal_private_text_keeps_original_session_route(self):
        self.enable()
        update = self.message(100, text='继续 CAD 的原任务', group=False, topic=22)
        self.router.ingest(update)
        self.assertEqual(self.rows('gc_jobs'), 0)
        self.assertEqual(self.rows('jobs'), 1)
        self.assertTrue(self.router.forward_one())
        row = self.router.bridges['cad'].store.pending()
        self.assertEqual((row['text'], row['message_thread_id']), ('继续 CAD 的原任务', 22))
        self.assertIsNone(self.router.bridges['manager'].store.pending())
        for bridge in self.router.bridges.values():
            bridge.codex.call.assert_not_awaited()

    def test_private_sessions_command_keeps_existing_picker_lane(self):
        self.router.ingest(self.message(100, text='/sessions', group=False))
        self.assertEqual(self.rows('ui_jobs'), 1)
        self.assertEqual(self.rows('gc_jobs'), 0)
        self.assertEqual(self.rows('jobs'), 0)
        self.router.telegram.call.assert_not_awaited()

    def test_existing_question_reply_keeps_question_identity_and_never_becomes_group_input(self):
        with self.router.store.db:
            self.router.store.db.execute('INSERT INTO choice_prompts '
                '(chat_id,message_id,topic_id,bot_id,question_id,position) VALUES(?,?,?,?,?,?)',
                ('123', 77, 22, 456, 'native-question', 0))
        parent = {'message_id': 77, 'from': {'id': 456}, 'text': '回答 Codex 问题：'}
        self.router.ingest(self.message(100, text='我的正常答案', group=False, topic=22, reply_to_message=parent))
        self.assertEqual(self.rows('gc_jobs'), 0)
        self.assertEqual(self.rows('choice_jobs'), 1)
        queued = json.loads(self.router.store.db.execute('SELECT payload FROM choice_jobs').fetchone()[0])
        self.assertEqual(queued['message']['reply_to_message'], parent)
        self.assertEqual(self.rows('jobs'), 0)

    def test_session_name_prompt_reply_is_not_intercepted_as_private_trial_command(self):
        with self.router.store.db:
            self.router.store.db.execute('INSERT INTO ui_new_prompts '
                '(chat_id,message_id,topic_id,draft_id,kind,state,expires_at) VALUES(?,?,?,?,?,?,?)',
                ('123', 88, None, 'draft', 'name', 'active', 9999999999))
        parent = {'message_id': 88, 'from': {'id': 456}, 'text': '新建 Codex 会话 · 会话名称'}
        self.router.ingest(self.message(100, text='/collab_test', group=False, reply_to_message=parent))
        self.assertEqual(self.rows('gc_jobs'), 0)
        self.assertEqual(self.rows('ui_jobs'), 1)
        queued = json.loads(self.router.store.db.execute('SELECT payload FROM ui_jobs').fetchone()[0])
        self.assertEqual(queued['message']['text'], '/collab_test')

    async def test_newly_installed_target_gets_the_same_safe_observer(self):
        target = {'key': 'extra', 'name': 'Extra', 'thread_id': 'native-extra', 'message_thread_id': 33}
        await self.router.install_target(target)
        self.assertEqual(self.router.bridges['extra'].on_collaboration_item, self.router.collaboration.on_item)
        self.assertEqual(self.rows('gc_runs'), 0)

    def test_all_existing_targets_have_trial_observer_without_changing_delivery(self):
        for child in self.router.bridges.values():
            self.assertEqual(child.on_collaboration_item, self.router.collaboration.on_item)
            self.assertTrue(child.delivery.enabled)

    def test_completed_agent_message_reaches_observer_before_completion_delivery_returns(self):
        child = self.router.bridges['cad']
        observer = child.on_collaboration_item = Mock()
        item = {'id': 'cad-answer', 'type': 'agentMessage', 'phase': 'final_answer', 'text': '短确认'}
        child.record_item('cad-turn', item)
        observer.assert_called_once_with(child, 'cad-turn', item, replay=False)
        self.assertEqual(child.store.db.execute('SELECT count(*) FROM completion_ready_items').fetchone()[0], 1)
        self.assertEqual(child.store.db.execute('SELECT count(*) FROM outbox').fetchone()[0], 0)

    def test_replay_is_observable_but_stays_out_of_live_completion_projection(self):
        child = self.router.bridges['cad']
        observer = child.on_collaboration_item = Mock()
        item = {'id': 'cad-replayed-answer', 'type': 'agentMessage', 'text': '重放短确认'}
        child.record_item('cad-turn', item, replay=True)
        observer.assert_called_once_with(child, 'cad-turn', item, replay=True)
        self.assertEqual(child.store.db.execute('SELECT count(*) FROM completion_ready_items').fetchone()[0], 0)

    def test_observer_error_does_not_prevent_normal_completion_recording(self):
        child = self.router.bridges['cad']
        child.on_collaboration_item = Mock(side_effect=RuntimeError('observer unavailable'))
        item = {'id': 'cad-answer', 'type': 'agentMessage', 'text': '原会话完成回复'}
        with patch('topics_bridge.LOG.warning') as warning:
            child.record_item('cad-turn', item)
        warning.assert_called_once()
        self.assertEqual(child.store.db.execute('SELECT count(*) FROM completion_ready_items').fetchone()[0], 1)

    def test_user_messages_and_empty_assistant_items_do_not_reach_observer(self):
        child = self.router.bridges['cad']
        observer = child.on_collaboration_item = Mock()
        child.record_item('cad-turn', {'id': 'user', 'type': 'userMessage', 'content': [{'type': 'text', 'text': '输入'}]})
        child.record_item('cad-turn', {'id': 'empty', 'type': 'agentMessage', 'text': '  '})
        observer.assert_not_called()


if __name__ == '__main__':
    unittest.main()
