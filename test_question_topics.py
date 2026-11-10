"""Exercise native questions through the real topic router and bridge mixins."""
import copy
import json
from pathlib import Path
import tempfile
import unittest

from bridge import Codex
from native_questions import ANSWER_END, ANSWER_START
from topics_bridge import TopicRouter


class QuestionTopicTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        path = Path(self.tmp.name)
        (path / 'access.json').write_text(json.dumps({'dmPolicy': 'allowlist', 'allowFrom': ['123']}))
        config = {
            'owner_id': '123', 'telegram_state_dir': str(path), 'socket_path': '/unused',
            'thread_id': 'meeting-native', 'expected_bot_username': 'test_bot',
            'mode': 'topics', 'host_id': 'local', 'root_target': 'meeting',
            'notification_mode': 'completion', 'preview_style': 'status-bar',
            'targets': [
                {'key': 'meeting', 'name': '会议记录', 'thread_id': 'meeting-native', 'message_thread_id': 11},
                {'key': 'instagram', 'name': 'Instagram', 'thread_id': 'instagram-native', 'message_thread_id': 22},
            ],
        }
        (path / 'config.json').write_text(json.dumps(config))
        self.router = TopicRouter(config, path)
        self.router.bot_id, self.router.username = 444, 'test_bot'
        self.sequence, self.calls, self.native_calls, self.rpc_results = 100, [], [], []
        self.messages = {}
        test = self

        class Telegram:
            async def call(self, method, **params):
                test.calls.append((method, copy.deepcopy(params)))
                if method == 'answerCallbackQuery':
                    return True
                if method == 'sendMessage':
                    test.sequence += 1
                    result = {**params, 'message_id': test.sequence, 'date': 1,
                              'chat': {'id': 123, 'type': 'private'}, 'from': {'id': 444, 'is_bot': True}}
                    test.messages[result['message_id']] = result
                    return copy.deepcopy(result)
                if method == 'editMessageText':
                    result = {**test.messages[params['message_id']], **params}
                    test.messages[result['message_id']] = result
                    return copy.deepcopy(result)
                raise AssertionError('Unexpected Telegram operation: ' + method)

        class Native(Codex):
            def __init__(self, tid):
                super().__init__('/unused', tid)
                self.history = {}
                self.turn_rows = []
                native = self

                class Socket:
                    closed = False

                    async def send_json(self, response):
                        test.rpc_results.append((native.thread_id, copy.deepcopy(response)))

                self.ws = Socket()

            async def turns(self, limit=30):
                return copy.deepcopy(self.turn_rows)

            async def items(self, turn_id):
                for item in self.history.get(turn_id, []):
                    yield {'item': copy.deepcopy(item)}

            async def call(self, method, params):
                test.native_calls.append((self.thread_id, method, copy.deepcopy(params)))
                if method == 'turn/steer':
                    return {'turnId': params['expectedTurnId']}
                raise AssertionError('Unexpected native model operation: ' + method)

            async def settings(self):
                return {'thread': {'id': self.thread_id, 'name': self.thread_id, 'status': {'type': 'idle'}},
                        'model': 'test-model', 'reasoningEffort': 'high'}

        self.telegram = self.router.telegram = Telegram()
        for child in self.router.bridges.values():
            child.codex, child.telegram = Native(child.thread_id), self.telegram
            child.ready.set()

    async def asyncTearDown(self):
        for child in self.router.bridges.values():
            child.store.db.close()
        self.router.store.db.close()
        self.tmp.cleanup()

    def input_count(self, key):
        return self.router.bridges[key].store.db.execute('SELECT COUNT(*) FROM inbox').fetchone()[0]

    def panel(self):
        row = self.router.store.db.execute('SELECT * FROM choice_questions ORDER BY rowid DESC LIMIT 1').fetchone()
        return copy.deepcopy(self.messages[row['message_id']])

    def callback(self, panel, label):
        button = next(button for line in panel['reply_markup']['inline_keyboard']
                      for button in line if label in button['text'])
        self.sequence += 1
        return {'update_id': self.sequence, 'callback_query': {
            'id': str(self.sequence), 'from': {'id': 123},
            'message': copy.deepcopy(panel), 'data': button['callback_data']}}

    async def click(self, panel, label):
        update = self.callback(panel, label)
        self.router.ingest(update)
        self.assertTrue(await self.router.questions.handle_one())
        return update

    async def test_async_buttons_bypass_completion_hold_and_submit_only_to_original_topic(self):
        child = self.router.bridges['instagram']
        self.assertTrue(child.delivery.enabled)
        item = {'id': 'call_instagram_question', 'type': 'agentMessage', 'delivery': 'async',
                'phase': 'final_answer', 'text': '选择短片风格',
                'questions': [{'title': '选择短片风格', 'options': ['纪实', '动画']}]}
        child.codex.turn_rows = [{'id': 'instagram-turn', 'status': 'inProgress'}]
        child.codex.history['instagram-turn'] = [copy.deepcopy(item)]
        event = {'method': 'item/completed', 'params': {
            'threadId': child.thread_id, 'turnId': 'instagram-turn', 'item': item}}
        self.assertFalse(await child.handle_server_event(event))
        child.record_item('instagram-turn', item)
        self.assertEqual(child.store.db.execute('SELECT COUNT(*) FROM outbox').fetchone()[0], 0)
        self.assertTrue(await self.router.questions.flush_one())
        panel = self.panel()
        self.assertEqual(panel['message_thread_id'], 22)
        self.assertIn('选择短片风格', panel['text'])
        self.assertEqual(self.native_calls, [])
        await self.click(panel, '动画')
        self.assertEqual(len(self.native_calls), 1)
        native, method, params = self.native_calls[0]
        self.assertEqual((native, method, params['threadId'], params['expectedTurnId']),
                         ('instagram-native', 'turn/steer', 'instagram-native', 'instagram-turn'))
        body = params['input'][0]['text']
        answer = json.loads(body.split(ANSWER_START, 1)[1].split(ANSWER_END, 1)[0])
        self.assertEqual(answer, [{'questionItemId': '["request_user_input_async","call_instagram_question",0]',
                                  'question': '选择短片风格', 'answer': '动画'}])
        row = child.store.db.execute('SELECT * FROM inbox').fetchone()
        self.assertEqual((row['state'], row['turn_id'], row['message_thread_id']), ('running', 'instagram-turn', 22))
        self.assertEqual(self.input_count('meeting'), 0)
        await self.click(panel, '动画')
        self.assertEqual(len(self.native_calls), 1)
        self.assertEqual(self.input_count('instagram'), 1)

    async def test_sync_buttons_use_original_rpc_response_without_starting_a_turn(self):
        child = self.router.bridges['meeting']
        params = {'threadId': child.thread_id, 'turnId': 'meeting-turn', 'itemId': 'sync-question',
                  'questions': [{'id': 'summary_style', 'header': '摘要', 'question': '选择摘要方式',
                                 'options': [{'label': '详细', 'description': '保留细节'},
                                             {'label': '精简', 'description': '只留要点'}]}]}
        child.codex.pending_user_inputs['server-original-7'] = copy.deepcopy(params)
        event = {'id': 'server-original-7', 'method': 'item/tool/requestUserInput', 'params': params,
                 '_user_input_epoch': child.codex.user_input_epoch}
        self.assertTrue(await child.handle_server_event(event))
        self.assertTrue(await self.router.questions.flush_one())
        panel = self.panel()
        self.assertEqual(panel['message_thread_id'], 11)
        await self.click(panel, '精简')
        self.assertEqual(self.rpc_results, [('meeting-native', {
            'id': 'server-original-7', 'result': {'answers': {'summary_style': {'answers': ['精简']}}}})])
        self.assertEqual(self.native_calls, [])
        self.assertEqual(self.input_count('meeting'), 0)
        self.assertEqual(self.input_count('instagram'), 0)
        await self.click(panel, '精简')
        self.assertEqual(len(self.rpc_results), 1)

    async def test_ordinary_unquoted_topic_command_keeps_existing_route(self):
        self.sequence += 1
        update = {'update_id': self.sequence, 'message': {
            'message_id': self.sequence, 'date': 1, 'from': {'id': 123},
            'chat': {'id': 123, 'type': 'private'}, 'message_thread_id': 22, 'text': '/status'}}
        self.router.ingest(update)
        self.assertFalse(await self.router.questions.handle_one())
        self.assertTrue(self.router.forward_one())
        child = self.router.bridges['instagram']
        row = child.store.pending()
        self.assertEqual((row['text'], row['message_thread_id']), ('/status', 22))
        await child.dispatch_one(row)
        output = child.store.db.execute('SELECT * FROM outbox').fetchone()
        self.assertEqual(output['message_thread_id'], 22)
        self.assertIn('instagram-native', output['text'])
        self.assertEqual(self.input_count('meeting'), 0)
        self.assertEqual(self.router.store.db.execute('SELECT COUNT(*) FROM choice_questions').fetchone()[0], 0)
        self.assertEqual(self.native_calls, [])
        self.assertEqual(self.rpc_results, [])


if __name__ == '__main__':
    unittest.main()
