"""Only accepted user interactions move the matching active preview."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock

from choice_questions import ChoiceError
from topics_bridge import TopicRouter


class PreviewInteractionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        path = Path(self.tmp.name)
        (path / 'access.json').write_text(json.dumps({'dmPolicy': 'allowlist', 'allowFrom': ['123']}))
        config = {'owner_id': '123', 'telegram_state_dir': str(path), 'socket_path': '/unused',
                  'thread_id': 'alpha-native', 'expected_bot_username': 'test_bot', 'mode': 'topics',
                  'host_id': 'local', 'root_target': 'alpha', 'notification_mode': 'completion',
                  'preview_style': 'status-bar', 'targets': [
                      {'key': 'alpha', 'name': 'Alpha', 'thread_id': 'alpha-native', 'message_thread_id': 11},
                      {'key': 'beta', 'name': 'Beta', 'thread_id': 'beta-native', 'message_thread_id': 22}]}
        (path / 'config.json').write_text(json.dumps(config))
        self.router = TopicRouter(config, path)
        self.router.bot_id, self.router.username = 444, 'test_bot'
        self.calls, self.messages, self.sequence = [], {}, 1000
        test = self

        class Telegram:
            async def call(self, method, **params):
                test.calls.append((method, copy.deepcopy(params)))
                if method == 'answerCallbackQuery':
                    return True
                if method.startswith('edit'):
                    result = {**test.messages[params['message_id']], **params}
                else:
                    test.sequence += 1
                    result = {**params, 'message_id': test.sequence, 'date': 1,
                              'chat': {'id': 123, 'type': 'private'}, 'from': {'id': 444, 'is_bot': True}}
                test.messages[result['message_id']] = result
                return copy.deepcopy(result)

        self.router.telegram = Telegram()
        for bridge in self.router.bridges.values():
            bridge.telegram = self.router.telegram
            bridge.delivery.note_interaction = Mock(return_value=True)
            bridge.submit_question_answer = AsyncMock(return_value={'turn_id': 'turn'})
            bridge.ready.set()
        self.bridge = self.router.bridges['alpha']

    async def asyncTearDown(self):
        for bridge in self.router.bridges.values():
            bridge.store.db.close()
        self.router.store.db.close()
        self.tmp.cleanup()

    def message(self, text, topic=11, sender=123, **extra):
        self.sequence += 1
        return {'update_id': self.sequence, 'message': {
            'message_id': self.sequence, 'date': 1, 'from': {'id': sender},
            'chat': {'id': 123, 'type': 'private'}, 'message_thread_id': topic, 'text': text, **extra}}

    def accept(self, update, *, binding=None):
        self.bridge.store.accept(update, update['message'], guard_binding=binding)

    async def question(self, values):
        item = {'type': 'agentMessage', 'id': 'call_question', 'delivery': 'async', 'text': '',
                'questions': values}
        await self.router.questions.observe(self.bridge, 'turn', item)
        await self.router.questions.flush_one()
        row = self.router.store.db.execute('SELECT * FROM choice_questions').fetchone()
        return self.router.questions._question(row)

    def current_question(self):
        row = self.router.store.db.execute('SELECT * FROM choice_questions').fetchone()
        return self.router.questions._question(row)

    async def test_new_owner_input_triggers_once_without_changing_durable_text(self):
        update = self.message('继续处理\n保留原文')
        self.accept(update)
        self.accept(update)
        self.bridge.delivery.note_interaction.assert_called_once_with(
            'telegram:' + str(update['update_id']), chat_id='123', topic_id=11)
        row = self.bridge.store.db.execute('SELECT * FROM inbox').fetchone()
        self.assertEqual(row['text'], update['message']['text'])
        self.assertEqual(row['state'], 'pending')
        self.assertTrue(self.bridge.receipt_event.is_set())

    async def test_system_commands_and_wrong_identity_or_binding_do_not_move(self):
        for command in ['/status', '/stop', '/sessions', '/model next', '/unknown']:
            self.accept(self.message(command))
        self.accept(self.message('other sender', sender=999))
        self.accept(self.message('wrong topic', topic=22))
        self.accept(self.message('wrong binding'), binding={
            'target_key': 'alpha', 'thread_id': 'other-native', 'topic_id': 11})
        self.bridge.delivery.note_interaction.assert_not_called()
        self.assertTrue(self.bridge.receipt_event.is_set())  # Original receipt hook remains intact.

    async def test_router_routes_interaction_to_only_the_selected_topic(self):
        update = self.message('Beta 的补充消息', topic=22)
        self.router.ingest(update)
        self.assertTrue(self.router.forward_one())
        self.bridge.delivery.note_interaction.assert_not_called()
        self.router.bridges['beta'].delivery.note_interaction.assert_called_once_with(
            'telegram:' + str(update['update_id']), chat_id='123', topic_id=22)

    async def test_attachment_caption_command_remains_model_content(self):
        update = self.message('', caption='/stop 只是图片说明', photo=[{'file_id': 'photo'}])
        self.accept(update)
        self.bridge.delivery.note_interaction.assert_called_once()

    async def test_preview_failure_never_blocks_accepted_input(self):
        self.bridge.delivery.note_interaction.side_effect = RuntimeError('optional display failure')
        update = self.message('输入仍须送达')
        self.accept(update)
        row = self.bridge.store.db.execute('SELECT text,state FROM inbox').fetchone()
        self.assertEqual(tuple(row), ('输入仍须送达', 'pending'))
        self.assertTrue(self.bridge.receipt_event.is_set())

    async def test_each_multiquestion_answer_moves_once_before_native_submission(self):
        question = await self.question([{'title': '第一题', 'options': ['A', 'B']},
                                        {'title': '第二题', 'options': ['C', 'D']}])
        await self.router.questions._answer(question, 'B')
        marker = 'choice:' + question['id'] + ':'
        self.bridge.delivery.note_interaction.assert_called_once_with(marker + '0', chat_id='123', topic_id=11)
        self.bridge.submit_question_answer.assert_not_awaited()
        with self.assertRaises(ChoiceError):
            await self.router.questions._answer(question, 'B')
        self.assertEqual(self.bridge.delivery.note_interaction.call_count, 1)
        await self.router.questions._answer(self.current_question(), 'D')
        self.assertEqual(self.bridge.delivery.note_interaction.call_count, 2)
        self.assertEqual(self.bridge.delivery.note_interaction.call_args.args, (marker + '1',))
        self.bridge.submit_question_answer.assert_awaited_once()

    async def test_force_reply_answer_uses_same_interaction_path(self):
        question = await self.question([{'title': '请补充', 'options': ['A']}])
        await self.router.questions._other(question)
        prompt_id = self.router.store.db.execute('SELECT message_id FROM choice_prompts').fetchone()[0]
        prompt = self.messages[prompt_id]
        update = self.message('自定义答案', reply_to_message=copy.deepcopy(prompt))
        self.router.ingest(update)
        await self.router.questions.handle_one()
        self.bridge.delivery.note_interaction.assert_called_once_with(
            'choice:' + question['id'] + ':0', chat_id='123', topic_id=11)
        self.bridge.submit_question_answer.assert_awaited_once()
        self.assertEqual(self.bridge.store.db.execute('SELECT COUNT(*) FROM inbox').fetchone()[0], 0)

    async def test_echo_moves_after_ack_and_unrelated_output_does_not(self):
        self.bridge.store.enqueue_at('native-user:turn:user', '123', '🖥 你在 Codex 客户端说：\n新输入', 11)
        row = self.bridge.store.db.execute('SELECT * FROM outbox ORDER BY rowid DESC LIMIT 1').fetchone()
        self.bridge.delivery.note_interaction.assert_not_called()
        await self.bridge.send_one(row)
        self.bridge.delivery.note_interaction.assert_called_once_with(
            'echo:' + row['key'], chat_id='123', topic_id=11)
        self.bridge.delivery.note_interaction.reset_mock()
        self.bridge.store.enqueue_at('control:status', '123', '系统回复', 11)
        control = self.bridge.store.db.execute('SELECT * FROM outbox ORDER BY rowid DESC LIMIT 1').fetchone()
        await self.bridge.send_one(control)
        self.bridge.delivery.note_interaction.assert_not_called()

    async def test_echo_send_failure_does_not_move_preview(self):
        self.bridge.store.enqueue_at('main-guard-input:draft:100', '123', '📩 你在 All 说：\n用户消息', 11)
        row = self.bridge.store.db.execute('SELECT * FROM outbox ORDER BY rowid DESC LIMIT 1').fetchone()
        self.bridge.telegram.call = AsyncMock(side_effect=ConnectionError('unknown send result'))
        with self.assertRaises(ConnectionError):
            await self.bridge.send_one(row)
        self.bridge.delivery.note_interaction.assert_not_called()


if __name__ == '__main__':
    unittest.main()
