"""Native questions stay tied to their original topic, item and connection."""
import copy
import json
import sqlite3
import time
from types import SimpleNamespace
import unittest

from choice_questions import AnswerNotSubmitted, ChoiceQuestions
from bridge import TelegramError
from message_format import utf16_size


class QuestionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        db = sqlite3.connect(':memory:')
        db.row_factory = sqlite3.Row
        db.execute('CREATE TABLE kv(key TEXT PRIMARY KEY,value TEXT)')
        db.execute("INSERT INTO kv VALUES('offset','0')")
        store = SimpleNamespace(db=db, offset=lambda: int(db.execute("SELECT value FROM kv WHERE key='offset'").fetchone()[0]))
        self.router = SimpleNamespace(store=store, owner='123', bot_id=444, targets={}, bridges={})
        self.router.access = lambda: {'dmPolicy': 'allowlist', 'allowFrom': ['123']}
        self.router.owner_allowed = lambda: True
        self.calls, self.native, self.sequence, self.messages = [], [], 100, {}
        self.send_error, self.submit_error, self.on_send = None, None, None
        test = self

        class Telegram:
            async def call(self, method, **params):
                test.calls.append((method, copy.deepcopy(params)))
                if method == 'answerCallbackQuery':
                    return True
                if method == 'sendMessage':
                    if test.send_error:
                        raise test.send_error
                    test.sequence += 1
                    result = {**params, 'message_id': test.sequence, 'date': 1,
                              'from': {'id': 444, 'is_bot': True}, 'chat': {'id': 123, 'type': 'private'}}
                    test.messages[result['message_id']] = result
                    if test.on_send:
                        await test.on_send()
                    return copy.deepcopy(result)
                if method == 'editMessageText':
                    result = {**test.messages[params['message_id']], **params}
                    test.messages[result['message_id']] = result
                    return copy.deepcopy(result)
                raise AssertionError(method)

        class Native:
            def __init__(self):
                self.user_input_epoch, self.pending_user_inputs = 'epoch-1', {}

            async def respond_user_input(self, request_id, answers):
                test.native.append(('sync', request_id, copy.deepcopy(answers)))
                self.pending_user_inputs.pop(request_id)
                if test.submit_error:
                    raise test.submit_error

        self.router.telegram = Telegram()
        for key, topic in [('alpha', 42), ('beta', 99)]:
            target = {'key': key, 'thread_id': key + '-native', 'message_thread_id': topic}
            bridge = SimpleNamespace(target=target, thread_id=target['thread_id'], codex=Native())

            async def submit(question, answers, client_id):
                self.native.append(('async', copy.deepcopy(question), copy.deepcopy(answers), client_id))
                if self.submit_error:
                    raise self.submit_error

            bridge.submit_question_answer = submit
            self.router.targets[key], self.router.bridges[key] = target, bridge
        self.bridge = self.router.bridges['alpha']
        self.ui = ChoiceQuestions(self.router)

    async def asyncTearDown(self):
        self.router.store.db.close()

    def item(self, count=1, **changes):
        return {'id': 'agent-question', 'type': 'agentMessage', 'text': '请选', 'delivery': 'async',
                'questions': [{'title': '选择方案 ' + str(index), 'options': ['甲', '乙']} for index in range(count)],
                **changes}

    async def show(self, count=1, bridge=None):
        await self.ui.observe(bridge or self.bridge, 'turn-1', self.item(count))
        await self.ui.flush_one()
        return self.panel()

    def panel(self, index=0):
        rows = self.router.store.db.execute('SELECT message_id FROM choice_questions ORDER BY rowid').fetchall()
        return copy.deepcopy(self.messages[rows[index]['message_id']])

    def row(self, index=0):
        return self.router.store.db.execute('SELECT * FROM choice_questions ORDER BY rowid').fetchall()[index]

    def callback(self, panel, label='1. 甲', **changes):
        button = next(button for row in panel['reply_markup']['inline_keyboard'] for button in row if button['text'] == label)
        self.sequence += 1
        result = {'update_id': self.sequence, 'callback_query': {'id': str(self.sequence), 'from': {'id': 123},
                    'message': copy.deepcopy(panel), 'data': button['callback_data']}}
        result['callback_query'].update(changes)
        return result

    async def click(self, panel=None, label='1. 甲', **changes):
        update = self.callback(panel or self.panel(), label, **changes)
        self.assertTrue(self.ui.ingest(update))
        await self.ui.handle_one()
        await self.ui.flush_one()
        return update

    async def start_two_text_questions(self, next_options=None):
        questions = [{'title': '第一个问题', 'options': []},
                     {'title': '第二个问题', 'options': next_options or []}]
        await self.ui.observe(self.bridge, 'turn-1', self.item(questions=questions))
        await self.ui.flush_one()
        await self.click(label='自行输入')
        return copy.deepcopy(self.messages[self.sequence])

    async def answer_prompt(self, prompt, text):
        self.sequence += 1
        update = {'update_id': self.sequence, 'message': {
            'message_id': self.sequence, 'from': {'id': 123}, 'chat': {'id': 123, 'type': 'private'},
            'message_thread_id': 42, 'text': text, 'reply_to_message': copy.deepcopy(prompt)}}
        self.assertTrue(self.ui.ingest(update))
        await self.ui.handle_one()
        return update

    def prompt_claim(self, position):
        return self.router.store.db.execute('SELECT * FROM choice_prompt_claims WHERE question_id=? AND position=?',
                                            (self.row()['id'], position)).fetchone()

    def force_reply_attempts(self):
        return [params for method, params in self.calls if method == 'sendMessage'
                and params.get('reply_markup', {}).get('force_reply')]

    async def sync(self, request_id='request-1'):
        params = {'threadId': self.bridge.thread_id, 'turnId': 'turn-1', 'itemId': 'sync-item',
                  'questions': [{'id': 'q-id', 'header': '方案', 'question': '同步选择？',
                                 'options': [{'label': '甲', 'description': '第一个选项'}, {'label': '乙'}]}]}
        self.bridge.codex.pending_user_inputs[request_id] = params
        event = {'method': 'item/tool/requestUserInput', 'id': request_id, 'params': params,
                 '_user_input_epoch': self.bridge.codex.user_input_epoch}
        await self.ui.server_event(self.bridge, event)
        await self.ui.flush_one()
        return event

    def native_answer(self, item_id='agent-question'):
        answer = [{'questionItemId': json.dumps(['request_user_input_async', item_id, 0]),
                   'question': '选择方案', 'answer': '甲'}]
        return {'type': 'userMessage', 'id': 'native-answer', 'content': [{'type': 'text',
                'text': '<send_user_message_question_reply>\n' + json.dumps(answer) + '\n</send_user_message_question_reply>'}]}

    async def test_async_question_is_immediate_and_has_no_model_call(self):
        panel = await self.show()
        self.assertEqual(panel['message_thread_id'], 42)
        self.assertIn('选择方案 0', panel['text'])
        self.assertEqual(len(panel['reply_markup']['inline_keyboard']), 3)
        self.assertTrue(all(len(button['callback_data']) < 64 for row in panel['reply_markup']['inline_keyboard'] for button in row))
        self.assertEqual(self.native, [])
        self.assertEqual(self.row()['state'], 'active')

    async def test_single_answer_submits_original_identity_once_even_after_recovery(self):
        panel = await self.show()
        update = await self.click(panel)
        self.assertEqual(self.row()['state'], 'answered')
        self.assertEqual(self.native[0][0], 'async')
        self.assertEqual(self.native[0][1]['thread_id'], 'alpha-native')
        self.assertEqual(self.native[0][1]['item_id'], 'agent-question')
        self.assertEqual(self.native[0][2], ['甲'])
        self.assertTrue(self.native[0][3].startswith('telegram-choice-'))
        self.ui = ChoiceQuestions(self.router)
        self.ui.recover()
        self.ui.ingest(update)
        await self.ui.handle_one()
        await self.click(panel)
        self.assertEqual(len(self.native), 1)
        self.assertEqual(self.panel()['reply_markup']['inline_keyboard'], [])

    async def test_multiple_questions_advance_and_submit_all_answers_together(self):
        panel = await self.show(2)
        await self.click(panel, '2. 乙')
        self.assertEqual(self.native, [])
        self.assertIn('2/2', self.panel()['text'])
        await self.click(self.panel(), '1. 甲')
        self.assertEqual(self.native[0][2], ['乙', '甲'])
        await self.click(panel)
        self.assertEqual(len(self.native), 1)

    async def test_sync_uses_rpc_response_and_exact_question_id(self):
        await self.sync(73)
        await self.click()
        self.assertEqual(self.native, [('sync', 73, {'q-id': {'answers': ['甲']}})])

    async def test_sync_requires_current_connection_replay(self):
        event = await self.sync()
        old_panel = self.panel()
        self.bridge.codex.user_input_epoch = 'epoch-2'
        self.bridge.codex.pending_user_inputs = {}
        await self.click(old_panel)
        self.assertEqual(self.native, [])
        self.assertEqual(self.row()['state'], 'active')
        event['_user_input_epoch'] = 'epoch-2'
        event['id'] = 'replayed-request'
        self.bridge.codex.pending_user_inputs[event['id']] = event['params']
        await self.ui.server_event(self.bridge, event)
        await self.click(old_panel)
        self.assertEqual(self.native, [('sync', 'replayed-request', {'q-id': {'answers': ['甲']}})])

    async def test_resolved_request_closes_buttons_and_old_epoch_is_ignored(self):
        event = await self.sync()
        resolved = {'method': 'serverRequest/resolved', '_user_input_epoch': 'old',
                    'params': {'threadId': 'alpha-native', 'requestId': event['id']}}
        await self.ui.server_event(self.bridge, resolved)
        self.assertEqual(self.row()['state'], 'active')
        resolved['_user_input_epoch'] = 'epoch-1'
        await self.ui.server_event(self.bridge, resolved)
        await self.ui.flush_one()
        self.assertEqual(self.row()['state'], 'resolved')
        self.assertEqual(self.panel()['reply_markup']['inline_keyboard'], [])

    async def test_owner_bot_topic_and_message_checks_prevent_cross_routing(self):
        panel = await self.show()
        for kind in ('owner', 'bot', 'topic', 'message', 'chat'):
            with self.subTest(kind=kind):
                changed = copy.deepcopy(panel)
                extra = {}
                if kind == 'owner': extra['from'] = {'id': 456}
                if kind == 'bot': changed['from']['id'] = 555
                if kind == 'topic': changed['message_thread_id'] = 99
                if kind == 'message': changed['message_id'] = 789
                if kind == 'chat': changed['chat']['id'] = 456
                await self.click(changed, **extra)
                self.assertEqual(self.native, [])
        self.assertGreater(self.router.store.offset(), 0)

    async def test_rebinding_invalidates_and_removes_old_keyboard(self):
        panel = await self.show()
        self.bridge.target['message_thread_id'] = 43
        self.ui.sweep()
        await self.ui.flush_one()
        self.assertEqual(self.row()['state'], 'expired')
        self.assertEqual(self.panel()['reply_markup']['inline_keyboard'], [])
        await self.click(panel)
        self.assertEqual(self.native, [])

    async def test_free_text_uses_exact_reply_prompt_and_preserves_other_commands(self):
        panel = await self.show()
        await self.click(panel, '自行输入')
        prompt = copy.deepcopy(self.messages[self.sequence])
        self.assertTrue(prompt['reply_markup']['force_reply'])
        self.sequence += 1
        message = {'message_id': self.sequence, 'from': {'id': 123}, 'chat': {'id': 123, 'type': 'private'},
                   'message_thread_id': 42, 'text': '自定义答案', 'reply_to_message': prompt}
        self.assertFalse(self.ui.ingest({'update_id': 801, 'message': {**message, 'reply_to_message': {}}}))
        self.assertFalse(self.ui.ingest({'update_id': 802, 'message': {**message, 'text': '/stop'}}))
        self.assertTrue(self.ui.ingest({'update_id': 803, 'message': message}))
        await self.ui.handle_one()
        self.assertEqual(self.native[0][2], ['自定义答案'])

    async def test_partial_text_answer_queues_next_free_reply_without_submitting(self):
        first = await self.start_two_text_questions()
        update = await self.answer_prompt(first, '没有；没有')
        self.assertEqual(json.loads(self.row()['answers']), ['没有；没有'])
        self.assertEqual((self.row()['position'], self.row()['state']), (1, 'active'))
        self.assertEqual(self.prompt_claim(1)['state'], 'queued')
        self.assertEqual(self.router.store.db.execute('SELECT state FROM choice_jobs WHERE id=?',
                                                     (update['update_id'],)).fetchone()[0], 'done')
        self.assertEqual(self.native, [])
        self.assertTrue(await self.ui.flush_one())  # Refresh the existing question panel.
        self.assertTrue(await self.ui.flush_one())  # Publish the queued next input prompt.
        second = copy.deepcopy(self.messages[self.sequence])
        self.assertTrue(second['reply_markup']['force_reply'])
        self.assertIn('2/2', second['text'])
        self.assertIn('第二个问题', second['text'])
        self.assertNotEqual(first['message_id'], second['message_id'])
        await self.answer_prompt(second, '目前都没有')
        self.assertEqual(self.native[0][2], ['没有；没有', '目前都没有'])
        self.assertEqual(len(self.native), 1)

    async def test_text_input_continues_for_next_question_with_options(self):
        first = await self.start_two_text_questions(['甲', '乙'])
        await self.answer_prompt(first, '没有')
        await self.ui.flush_one()
        await self.ui.flush_one()
        second = copy.deepcopy(self.messages[self.sequence])
        self.assertTrue(second['reply_markup']['force_reply'])
        self.assertIn('2/2', second['text'])
        self.assertIn('甲', second['text'])
        self.assertIn('乙', second['text'])
        self.assertTrue(self.panel()['reply_markup']['inline_keyboard'])
        await self.answer_prompt(second, '目前都没有')
        self.assertEqual(self.native[0][2], ['没有', '目前都没有'])

    async def test_button_answer_queues_prompt_when_next_question_has_no_options(self):
        questions = [{'title': '选择方案', 'options': ['甲', '乙']},
                     {'title': '补充说明', 'options': []}]
        await self.ui.observe(self.bridge, 'turn-1', self.item(questions=questions))
        await self.ui.flush_one()
        await self.click(label='1. 甲')
        self.assertEqual(self.prompt_claim(1)['state'], 'queued')
        self.assertEqual(self.native, [])
        await self.ui.flush_one()
        prompt = copy.deepcopy(self.messages[self.sequence])
        self.assertTrue(prompt['reply_markup']['force_reply'])
        self.assertIn('补充说明', prompt['text'])
        await self.answer_prompt(prompt, '补充答案')
        self.assertEqual(self.native[0][2], ['甲', '补充答案'])

    async def test_manual_input_claims_queued_prompt_before_display_without_duplicates(self):
        first = await self.start_two_text_questions(['甲', '乙'])
        await self.answer_prompt(first, '第一条答案')
        await self.ui.flush_one()
        await self.click(label='自行输入')
        self.assertEqual(self.prompt_claim(1)['state'], 'active')
        self.assertFalse(await self.ui.flush_one())
        self.assertEqual(len(self.force_reply_attempts()), 2)
        self.assertEqual(json.loads(self.row()['answers']), ['第一条答案'])

    async def test_restart_during_prompt_creation_does_not_resend(self):
        first = await self.start_two_text_questions()
        await self.answer_prompt(first, '第一条答案')
        self.router.store.db.execute("UPDATE choice_prompt_claims SET state='creating' WHERE position=1")
        self.ui = ChoiceQuestions(self.router)
        self.ui.recover()
        self.assertEqual(self.prompt_claim(1)['state'], 'uncertain')
        await self.ui.flush_one()
        self.assertFalse(await self.ui.flush_one())
        self.assertEqual(len(self.force_reply_attempts()), 1)
        self.assertEqual(json.loads(self.row()['answers']), ['第一条答案'])
        self.assertEqual(self.native, [])

    async def test_queued_next_prompt_survives_restart_and_sends_once(self):
        first = await self.start_two_text_questions()
        await self.answer_prompt(first, '第一条答案')
        self.ui = ChoiceQuestions(self.router)
        self.ui.recover()
        self.assertEqual(self.prompt_claim(1)['state'], 'queued')
        await self.ui.flush_one()
        await self.ui.flush_one()
        self.assertEqual(self.prompt_claim(1)['state'], 'active')
        self.ui = ChoiceQuestions(self.router)
        self.ui.recover()
        self.assertFalse(await self.ui.flush_one())
        self.assertEqual(len(self.force_reply_attempts()), 2)
        self.assertEqual(json.loads(self.row()['answers']), ['第一条答案'])
        self.assertEqual(self.native, [])

    async def test_upgrade_queues_missing_next_prompt_without_replaying_saved_answer(self):
        first = await self.start_two_text_questions(['甲', '乙'])
        update = await self.answer_prompt(first, '没有；没有')
        await self.ui.flush_one()
        self.router.store.db.execute('DELETE FROM choice_prompt_claims WHERE question_id=? AND position=1',
                                     (self.row()['id'],))
        self.assertIsNone(self.prompt_claim(1))
        self.assertEqual(self.row()['dirty'], 0)
        self.ui = ChoiceQuestions(self.router)
        self.ui.recover()
        self.assertEqual(self.prompt_claim(1)['state'], 'queued')
        await self.ui.flush_one()
        second = copy.deepcopy(self.messages[self.sequence])
        self.assertTrue(second['reply_markup']['force_reply'])
        self.assertIn('2/2', second['text'])
        self.assertIn('甲', second['text'])
        self.assertEqual(self.prompt_claim(1)['state'], 'active')
        self.assertEqual(json.loads(self.row()['answers']), ['没有；没有'])
        self.assertEqual(self.router.store.db.execute('SELECT state FROM choice_jobs WHERE id=?',
                                                     (update['update_id'],)).fetchone()[0], 'done')
        self.assertEqual(self.native, [])
        self.ui.recover()
        self.assertFalse(await self.ui.flush_one())
        self.assertEqual(len(self.force_reply_attempts()), 2)

    async def test_unknown_next_prompt_send_never_replays_or_loses_partial_answer(self):
        first = await self.start_two_text_questions()
        update = await self.answer_prompt(first, '第一条答案')
        await self.ui.flush_one()
        self.send_error = ConnectionError('acknowledgement lost')
        await self.ui.flush_one()
        self.assertEqual(self.prompt_claim(1)['state'], 'uncertain')
        self.assertEqual((self.row()['state'], self.row()['position']), ('active', 1))
        self.assertEqual(json.loads(self.row()['answers']), ['第一条答案'])
        self.assertEqual(self.router.store.db.execute('SELECT state FROM choice_jobs WHERE id=?',
                                                     (update['update_id'],)).fetchone()[0], 'done')
        self.send_error = None
        self.ui = ChoiceQuestions(self.router)
        self.ui.recover()
        await self.ui.flush_one()
        await self.answer_prompt(first, '误回旧题')
        await self.ui.flush_one()
        await self.click(label='自行输入')
        self.assertEqual(self.prompt_claim(1)['state'], 'uncertain')
        self.assertEqual(len(self.force_reply_attempts()), 2)
        self.assertEqual(json.loads(self.row()['answers']), ['第一条答案'])
        self.assertEqual(self.native, [])

    async def test_rate_limited_next_prompt_retries_without_expiring_question(self):
        first = await self.start_two_text_questions()
        await self.answer_prompt(first, '第一条答案')
        await self.ui.flush_one()
        self.send_error = TelegramError('sendMessage', 429, 'Too Many Requests', retry_after=20)
        started = time.time()
        await self.ui.flush_one()
        self.assertEqual(self.prompt_claim(1)['state'], 'queued')
        self.assertEqual(self.row()['state'], 'active')
        self.assertGreaterEqual(self.row()['retry_at'], started + 20)
        self.assertFalse(await self.ui.flush_one())
        self.router.store.db.execute('UPDATE choice_questions SET retry_at=0')
        self.send_error = None
        await self.ui.flush_one()
        self.assertEqual(self.prompt_claim(1)['state'], 'active')
        self.assertEqual(json.loads(self.row()['answers']), ['第一条答案'])
        self.assertEqual(self.native, [])

    async def test_reply_to_prior_prompt_does_not_answer_current_question(self):
        first = await self.start_two_text_questions(['甲', '乙'])
        await self.answer_prompt(first, '第一条答案')
        await self.ui.flush_one()
        await self.ui.flush_one()
        await self.answer_prompt(first, '目前都没有')
        self.assertEqual(json.loads(self.row()['answers']), ['第一条答案'])
        self.assertEqual((self.row()['state'], self.row()['position']), ('active', 1))
        self.assertEqual(self.prompt_claim(1)['state'], 'active')
        self.assertEqual(self.native, [])
        notice = self.calls[-1][1]['text']
        self.assertRegex(notice, r'第\s*1\s*题.*已记录')
        self.assertRegex(notice, r'第\s*2(?:/2)?\s*题')
        self.assertEqual(len(self.force_reply_attempts()), 2)

    async def test_next_prompt_ack_does_not_reopen_question_resolved_during_send(self):
        first = await self.start_two_text_questions()
        await self.answer_prompt(first, '第一条答案')
        await self.ui.flush_one()

        async def resolve_during_send():
            await self.ui.observe(self.bridge, 'native-answer-turn', self.native_answer())

        self.on_send = resolve_during_send
        await self.ui.flush_one()
        self.on_send = None
        self.assertEqual(self.row()['state'], 'resolved')
        self.assertEqual(self.prompt_claim(1)['state'], 'closed')
        second = copy.deepcopy(self.messages[self.sequence])
        prompt = self.router.store.db.execute('SELECT state FROM choice_prompts WHERE message_id=?',
                                             (second['message_id'],)).fetchone()
        self.assertIsNotNone(prompt)
        self.assertEqual(prompt['state'], 'closed')
        await self.answer_prompt(second, '过时回复')
        self.assertEqual(self.row()['state'], 'resolved')
        self.assertEqual(self.native, [])

    async def test_native_answer_prevents_old_question_and_closes_existing_one(self):
        await self.ui.observe(self.bridge, 'answer-turn', self.native_answer(), replay=True)
        await self.ui.observe(self.bridge, 'turn-1', self.item())
        self.assertEqual(self.router.store.db.execute('SELECT count(*) FROM choice_questions').fetchone()[0], 0)
        item = self.item(id='another-question')
        await self.ui.observe(self.bridge, 'turn-2', item)
        await self.ui.flush_one()
        await self.ui.observe(self.bridge, 'answer-turn', self.native_answer('another-question'))
        await self.ui.flush_one()
        self.assertEqual(self.row()['state'], 'resolved')
        self.assertEqual(self.panel()['reply_markup']['inline_keyboard'], [])

    async def test_history_baseline_plain_lists_and_secret_questions_do_not_publish(self):
        await self.ui.observe(self.bridge, 'turn-1', self.item(), replay=True)
        await self.ui.observe(self.bridge, 'turn-2', self.item(questions=[{'title': '密码', 'isSecret': True}]))
        await self.ui.observe(self.bridge, 'turn-3', self.item(delivery=None, text='A. 甲 B. 乙'))
        await self.ui.server_event(self.bridge, {'id': 5, 'method': 'item/permissions/requestApproval',
                                               'params': {'threadId': 'alpha-native'}, '_user_input_epoch': 'epoch-1'})
        self.assertFalse(await self.ui.flush_one())
        self.assertEqual(self.calls, [])

    async def test_unknown_native_outcome_is_never_resubmitted(self):
        panel = await self.show()
        self.submit_error = ConnectionError('connection lost after send')
        await self.click(panel)
        self.assertEqual(self.row()['state'], 'uncertain')
        self.ui.recover()
        self.submit_error = None
        await self.click(panel)
        self.assertEqual(len(self.native), 1)

    async def test_unsubmitted_preflight_restores_buttons_for_explicit_retry(self):
        await self.show()
        self.submit_error = AnswerNotSubmitted('本次答案尚未提交，请稍后重试当前按钮。')
        await self.click()
        self.assertEqual(self.row()['state'], 'active')
        self.assertEqual(json.loads(self.row()['answers']), [])
        self.assertIn('尚未提交', self.panel()['text'])
        self.assertTrue(self.panel()['reply_markup']['inline_keyboard'])
        self.assertFalse(await self.ui.handle_one())
        self.assertEqual(len(self.native), 1)
        self.submit_error = None
        await self.click()
        self.assertEqual(self.row()['state'], 'answered')
        self.assertIsNone(self.row()['reason'])
        self.assertEqual(self.native[-1][2], ['甲'])

    async def test_unsubmitted_final_reply_preserves_prior_answers_and_live_prompt(self):
        await self.show(2)
        await self.click(label='2. 乙')
        await self.click(label='自行输入')
        prompt = copy.deepcopy(self.messages[self.sequence])
        message = {'message_id': 950, 'from': {'id': 123}, 'chat': {'id': 123, 'type': 'private'},
                   'message_thread_id': 42, 'text': '补充答案', 'reply_to_message': prompt}
        self.submit_error = AnswerNotSubmitted('本次答案尚未提交，请重试。')
        self.ui.ingest({'update_id': 950, 'message': message})
        await self.ui.handle_one()
        self.assertEqual(self.row()['state'], 'active')
        self.assertEqual(self.row()['position'], 1)
        self.assertEqual(json.loads(self.row()['answers']), ['乙'])
        self.assertEqual(self.router.store.db.execute('SELECT state FROM choice_prompts').fetchone()[0], 'active')
        self.assertEqual(self.router.store.db.execute('SELECT state FROM choice_prompt_claims').fetchone()[0], 'active')
        self.submit_error = None
        self.ui.ingest({'update_id': 951, 'message': {**message, 'message_id': 951}})
        await self.ui.handle_one()
        self.assertEqual(self.row()['state'], 'answered')
        self.assertEqual(self.native[-1][2], ['乙', '补充答案'])

    async def test_safe_retry_does_not_reopen_cancelled_prompts(self):
        await self.show()
        await self.click(label='自行输入')
        prompt = copy.deepcopy(self.messages[self.sequence])
        self.ui.ingest({'update_id': 960, 'message': {'message_id': 960, 'from': {'id': 123},
            'chat': {'id': 123, 'type': 'private'}, 'message_thread_id': 42,
            'text': '/cancel', 'reply_to_message': prompt}})
        await self.ui.handle_one()
        self.submit_error = AnswerNotSubmitted('本次答案尚未提交，请重试。')
        await self.click()
        self.assertEqual(self.row()['state'], 'active')
        self.assertEqual(self.router.store.db.execute('SELECT state FROM choice_prompts').fetchone()[0], 'closed')
        self.assertEqual(self.router.store.db.execute('SELECT state FROM choice_prompt_claims').fetchone()[0], 'closed')

    async def test_definitively_invalid_question_is_not_reopened(self):
        panel = await self.show()
        self.submit_error = AnswerNotSubmitted('原问题内容已变化，答案尚未提交。', retryable=False)
        await self.click()
        self.assertEqual(self.row()['state'], 'expired')
        self.assertIn('尚未提交', self.panel()['text'])
        self.assertEqual(self.panel()['reply_markup']['inline_keyboard'], [])
        self.submit_error = None
        await self.click(panel)
        self.assertEqual(len(self.native), 1)

    async def test_native_answer_during_preflight_failure_stays_resolved(self):
        await self.show()
        await self.click(label='自行输入')
        async def submit(question, answers, client_id):
            await self.ui.observe(self.bridge, 'new-turn', self.native_answer())
            raise AnswerNotSubmitted('核验超时，答案尚未提交。')
        self.bridge.submit_question_answer = submit
        await self.click()
        self.assertEqual(self.row()['state'], 'resolved')
        self.assertEqual(self.panel()['reply_markup']['inline_keyboard'], [])
        self.assertEqual(self.router.store.db.execute('SELECT state FROM choice_prompts').fetchone()[0], 'closed')

    async def test_observed_native_answer_resolves_unknown_submission_without_retry(self):
        await self.show()
        self.submit_error = TimeoutError('acknowledgement lost')
        await self.click()
        self.assertEqual(self.row()['state'], 'uncertain')
        await self.ui.observe(self.bridge, 'answer-turn', self.native_answer(), replay=True)
        await self.ui.flush_one()
        self.assertEqual(self.row()['state'], 'resolved')
        self.assertEqual(self.panel()['reply_markup']['inline_keyboard'], [])
        self.assertEqual(len(self.native), 1)

    async def test_sync_timeout_stays_uncertain_and_cannot_be_retried(self):
        await self.sync()
        panel = self.panel()
        self.submit_error = TimeoutError('sync write acknowledgement missing')
        await self.click(panel)
        self.assertEqual(self.row()['state'], 'uncertain')
        self.submit_error = None
        await self.click(panel)
        self.assertEqual(len(self.native), 1)

    async def test_unknown_telegram_send_is_not_duplicated_after_restart(self):
        await self.ui.observe(self.bridge, 'turn-1', self.item())
        self.send_error = ConnectionError('unknown send outcome')
        await self.ui.flush_one()
        self.ui.recover()
        self.send_error = None
        await self.ui.flush_one()
        await self.ui.observe(self.bridge, 'turn-1', self.item())
        self.assertFalse(await self.ui.flush_one())
        self.assertEqual(sum(method == 'sendMessage' for method, _ in self.calls), 1)
        self.assertEqual(self.row()['state'], 'uncertain')

    async def test_expiry_removes_buttons_without_answering(self):
        await self.show()
        self.router.store.db.execute('UPDATE choice_questions SET expires_at=?', (time.time() - 1,))
        self.ui.sweep()
        await self.ui.flush_one()
        self.assertEqual(self.row()['state'], 'expired')
        self.assertEqual(self.panel()['reply_markup']['inline_keyboard'], [])
        self.assertEqual(self.native, [])

    async def test_resolution_while_publishing_keeps_message_id_and_removes_keyboard(self):
        async def resolve():
            await self.ui.observe(self.bridge, 'answer-turn', self.native_answer())
        self.on_send = resolve
        await self.show()
        self.assertEqual(self.row()['state'], 'resolved')
        self.assertTrue(self.row()['message_id'])
        await self.ui.flush_one()
        self.assertEqual(self.panel()['reply_markup']['inline_keyboard'], [])

    async def test_separate_topics_keep_separate_questions(self):
        await self.show()
        await self.show(bridge=self.router.bridges['beta'])
        alpha, beta = self.panel(), self.panel(1)
        await self.click(beta, '2. 乙')
        self.assertEqual(self.native[0][1]['thread_id'], 'beta-native')
        self.assertEqual(self.native[0][1]['topic_id'], 99)
        self.assertEqual(self.row()['state'], 'active')
        await self.click(alpha)
        self.assertEqual(self.native[1][1]['thread_id'], 'alpha-native')

    async def test_already_resolved_queued_server_event_does_not_publish(self):
        event = await self.sync()
        self.router.store.db.execute('DELETE FROM choice_questions')
        self.bridge.codex.pending_user_inputs.clear()
        self.assertTrue(await self.ui.server_event(self.bridge, event))
        self.assertFalse(await self.ui.flush_one())
        self.assertEqual(self.router.store.db.execute('SELECT count(*) FROM choice_questions').fetchone()[0], 0)

    async def test_rate_limit_retries_only_after_telegram_retry_after(self):
        await self.ui.observe(self.bridge, 'turn-1', self.item())
        self.send_error = TelegramError('sendMessage', 429, 'Too Many Requests', retry_after=20)
        started = time.time()
        await self.ui.flush_one()
        self.assertEqual(self.row()['state'], 'queued')
        self.assertGreaterEqual(self.row()['retry_at'], started + 20)
        self.assertFalse(await self.ui.flush_one())
        self.router.store.db.execute('UPDATE choice_questions SET retry_at=0')
        self.send_error = None
        await self.ui.flush_one()
        self.assertEqual(self.row()['state'], 'active')

    async def test_permanent_telegram_refusal_is_not_retried(self):
        await self.ui.observe(self.bridge, 'turn-1', self.item())
        self.send_error = TelegramError('sendMessage', 400, 'message thread not found')
        await self.ui.flush_one()
        self.assertEqual(self.row()['state'], 'expired')
        self.send_error = None
        self.assertFalse(await self.ui.flush_one())
        self.assertEqual(len(self.calls), 1)

    async def test_unknown_force_reply_send_is_not_repeated_on_another_click(self):
        panel = await self.show()
        self.send_error = ConnectionError('response lost after upload')
        await self.click(panel, '自行输入')
        self.send_error = None
        self.ui.recover()
        await self.click(panel, '自行输入')
        prompts = [params for method, params in self.calls if method == 'sendMessage'
                   and params.get('reply_markup', {}).get('force_reply')]
        self.assertEqual(len(prompts), 1)
        self.assertEqual(self.row()['state'], 'active')
        await self.click(panel)
        self.assertEqual(len(self.native), 1)

    async def test_free_reply_prompt_uses_utf16_budget_and_cancel_keeps_options(self):
        await self.ui.observe(self.bridge, 'turn-1', self.item(questions=[{'title': '😀' * 5000, 'options': ['甲']}]))
        await self.ui.flush_one()
        panel = self.panel()
        await self.click(panel, '自行输入')
        prompt = copy.deepcopy(self.messages[self.sequence])
        self.assertLessEqual(utf16_size(prompt['text']), 3800)
        update = {'update_id': 999, 'message': {'message_id': 999, 'from': {'id': 123},
                  'chat': {'id': 123, 'type': 'private'}, 'message_thread_id': 42,
                  'text': '/cancel@test_bot 取消', 'reply_to_message': prompt}}
        self.assertTrue(self.ui.ingest(update))
        await self.ui.handle_one()
        self.assertEqual(self.native, [])
        self.assertEqual(self.row()['state'], 'active')
        await self.click(panel)
        self.assertEqual(len(self.native), 1)


if __name__ == '__main__':
    unittest.main()
