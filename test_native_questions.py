import asyncio
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from choice_questions import ChoiceError
from native_questions import ANSWER_START, ANSWER_END, NativeQuestions, answered_items
from topics_bridge import RoutedStore


class NativeQuestionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.bridge = NativeQuestions()
        bridge = self.bridge
        bridge.store = RoutedStore(Path(self.tmp.name) / 'bridge.sqlite', 'native')
        bridge.thread_id, bridge.owner = 'native', '123'
        bridge.target = {'key': 'manager', 'thread_id': 'native', 'message_thread_id': 42}
        bridge.owner_allowed = lambda: True
        bridge.ready, bridge.receipt_event = asyncio.Event(), asyncio.Event()
        bridge.ready.set()
        bridge.dispatch_lock = asyncio.Lock()
        bridge.questions = type('Questions', (), {
            'observe': AsyncMock(), 'server_event': AsyncMock(return_value=True)})()
        self.question = {'kind': 'async', 'target_key': 'manager', 'thread_id': 'native', 'topic_id': 42,
                         'turn_id': 'turn', 'item_id': 'call_question',
                         'questions': [{'id': '0', 'title': 'Pick one', 'options': ['A', 'B']}]}
        self.source = {'id': 'call_question', 'type': 'agentMessage', 'delivery': 'async',
                       'text': '', 'questions': [{'title': 'Pick one', 'options': ['A', 'B']}]}
        self.turns = [{'id': 'turn', 'status': 'inProgress', 'startedAt': 100}]
        self.items = {'turn': [copy.deepcopy(self.source)]}
        test = self

        class Native:
            async def turns(self, limit=30):
                return copy.deepcopy(test.turns)

            async def items(self, tid):
                for item in test.items[tid]:
                    yield {'item': copy.deepcopy(item)}

        bridge.codex = Native()
        bridge.codex.call = AsyncMock(return_value={'turnId': 'turn'})

    async def asyncTearDown(self):
        self.bridge.store.db.close()
        self.tmp.cleanup()

    async def test_active_answer_uses_native_envelope_and_exact_turn_without_overrides(self):
        await self.bridge.submit_question_answer(self.question, ['B'], 'telegram-choice-one')
        method, params = self.bridge.codex.call.call_args.args
        self.assertEqual(method, 'turn/steer')
        self.assertEqual(set(params), {'threadId', 'expectedTurnId', 'input', 'clientUserMessageId'})
        self.assertEqual(params['expectedTurnId'], 'turn')
        text = params['input'][0]['text']
        entries = json.loads(text.split(ANSWER_START)[1].split(ANSWER_END)[0])
        self.assertEqual(entries, [{'questionItemId': '["request_user_input_async","call_question",0]',
                                    'question': 'Pick one', 'answer': 'B'}])
        row = self.bridge.store.db.execute('SELECT * FROM inbox').fetchone()
        self.assertEqual((row['state'], row['turn_id'], row['message_thread_id']), ('running', 'turn', 42))
        self.assertTrue(self.bridge.receipt_event.is_set())

    async def test_finished_turn_answer_starts_next_turn_with_same_session(self):
        self.turns[0]['status'] = 'completed'
        self.bridge.codex.call.return_value = {'turn': {'id': 'next'}}
        await self.bridge.submit_question_answer(self.question, ['A'], 'telegram-choice-two')
        method, params = self.bridge.codex.call.call_args.args
        self.assertEqual(method, 'turn/start')
        self.assertEqual(set(params), {'threadId', 'input', 'clientUserMessageId'})
        self.assertEqual(params['threadId'], 'native')
        self.assertEqual(self.bridge.store.running()[0]['turn_id'], 'next')

    async def test_rebinding_or_native_answer_prevents_submission(self):
        changed = {**self.question, 'topic_id': 99}
        with self.assertRaises(ChoiceError):
            await self.bridge.submit_question_answer(changed, ['A'], 'telegram-choice-three')
        envelope = ANSWER_START + json.dumps([{'questionItemId': '["request_user_input_async","call_question",0]',
                                               'answer': 'B'}]) + ANSWER_END
        self.items['turn'].append({'type': 'userMessage', 'content': [{'type': 'text', 'text': envelope}]})
        with self.assertRaisesRegex(ChoiceError, '已在 Codex 回答'):
            await self.bridge.submit_question_answer(self.question, ['A'], 'telegram-choice-four')
        self.bridge.codex.call.assert_not_awaited()

    async def test_changed_options_or_synthetic_history_id_does_not_answer(self):
        self.items['turn'][0]['questions'][0]['options'] = ['different']
        with self.assertRaisesRegex(ChoiceError, '内容已变化'):
            await self.bridge.submit_question_answer(self.question, ['A'], 'telegram-choice-options')
        self.items['turn'][0]['id'] = 'item-1'
        with self.assertRaisesRegex(ChoiceError, '核验原问题'):
            await self.bridge.submit_question_answer(self.question, ['A'], 'telegram-choice-synthetic')
        self.bridge.codex.call.assert_not_awaited()

    async def test_unknown_rpc_result_keeps_durable_route_and_cannot_submit_twice(self):
        self.bridge.codex.call.side_effect = ConnectionError('write result unknown')
        with self.assertRaises(ConnectionError):
            await self.bridge.submit_question_answer(self.question, ['A'], 'telegram-choice-unknown')
        row = self.bridge.store.db.execute('SELECT * FROM inbox').fetchone()
        self.assertEqual(row['state'], 'dispatching')
        with self.assertRaisesRegex(ChoiceError, '勿重复'):
            await self.bridge.submit_question_answer(self.question, ['A'], 'telegram-choice-unknown')
        self.assertEqual(self.bridge.codex.call.await_count, 1)

    async def test_history_baselines_old_questions_and_recovers_only_new_items(self):
        with patch('native_questions.time.time', return_value=100):
            await self.bridge.observe_question_history(self.turns)
        self.assertTrue(self.bridge.questions.observe.await_args.kwargs['replay'])
        with patch('native_questions.time.time', return_value=120):
            await self.bridge.observe_question_history(self.turns)
        self.assertTrue(self.bridge.questions.observe.await_args.kwargs['replay'])
        new = copy.deepcopy(self.source)
        new['id'], new['questions'][0]['title'] = 'call_new', 'New question'
        self.items['turn'].append(new)
        with patch('native_questions.time.time', return_value=140):
            await self.bridge.observe_question_history(self.turns)
        self.assertFalse(self.bridge.questions.observe.await_args.kwargs['replay'])
        self.assertEqual(self.bridge.questions.observe.await_args.args[2]['id'], 'call_new')

    async def test_history_never_publishes_legacy_synthetic_question_ids(self):
        with patch('native_questions.time.time', return_value=100):
            await self.bridge.observe_question_history(self.turns)
        self.bridge.questions.observe.reset_mock()
        synthetic = copy.deepcopy(self.source)
        synthetic['id'] = 'item-123'
        self.items['turn'] = [synthetic]
        with patch('native_questions.time.time', return_value=120):
            await self.bridge.observe_question_history(self.turns)
        self.bridge.questions.observe.assert_not_awaited()

    async def test_live_items_are_observed_without_suppressing_normal_output(self):
        event = {'method': 'item/completed', 'params': {'turnId': 'turn', 'item': self.source}}
        self.assertFalse(await self.bridge.handle_server_event(event))
        self.bridge.questions.observe.assert_awaited_once_with(self.bridge, 'turn', self.source)
        resolved = {'method': 'serverRequest/resolved', 'params': {}}
        self.assertTrue(await self.bridge.handle_server_event(resolved))

    async def test_answer_parser_rejects_plain_text_and_malformed_metadata(self):
        for text in ['normal text', ANSWER_START + 'not JSON' + ANSWER_END,
                     ANSWER_START + json.dumps([{'questionItemId': 'invalid'}]) + ANSWER_END]:
            self.assertEqual(answered_items({'type': 'userMessage', 'content': [{'type': 'text', 'text': text}]}), set())


if __name__ == '__main__':
    unittest.main()
