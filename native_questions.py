"""Native question observation and exact-session answer submission."""
import hashlib
import json
import re
import time
from collections import Counter

from bridge import RpcError, now_ms


ANSWER_START = '<send_user_message_question_reply>'
ANSWER_END = '</send_user_message_question_reply>'


def answered_items(item):
    if item.get('type') != 'userMessage':
        return set()
    found = set()
    for content in item.get('content', []):
        text = content.get('text', '') if content.get('type') == 'text' else ''
        if ANSWER_START not in text or ANSWER_END not in text:
            continue
        try:
            entries = json.loads(text.split(ANSWER_START, 1)[1].split(ANSWER_END, 1)[0])
            for entry in entries:
                identity = json.loads(entry['questionItemId'])
                if (isinstance(identity, list) and len(identity) == 3
                        and identity[0] == 'request_user_input_async'
                        and isinstance(identity[1], str) and type(identity[2]) is int):
                    found.add((identity[1], identity[2]))
        except (ValueError, KeyError, TypeError):
            continue
    return found


class NativeQuestions:
    """Mixin used only by the topic bridge; ordinary dispatch stays unchanged."""

    async def handle_server_event(self, event):
        questions = getattr(self, 'questions', None)
        if questions is None:
            return False
        if event['method'] == 'item/completed':
            params = event['params']
            await questions.observe(self, params['turnId'], params['item'])
            return False
        return await questions.server_event(self, event)

    async def observe_question_history(self, turns):
        questions = getattr(self, 'questions', None)
        if questions is None:
            return
        key = 'native_questions_history_v1'
        state = json.loads(self.store.get(key, '{}'))
        now = time.time()
        first = not state
        if first:
            state = {'since': now, 'turns': {}}
        known = state['turns']
        for turn in reversed(turns):
            tid = turn['id']
            active = turn.get('status') == 'inProgress'
            if tid not in known:
                started = turn.get('startedAt')
                eligible = not first and (not isinstance(started, (int, float)) or started >= state['since'])
                known[tid] = {'watch': active or eligible, 'baseline': first, 'scan': 0, 'closed': False,
                              'seen': []}
            track = known[tid]
            if not track['watch'] or track['closed']:
                continue
            if active and now - track['scan'] < 15:
                continue
            entries = [entry async for entry in self.codex.items(tid)]
            # Answers precede questions so recovery cannot briefly reopen a
            # prompt already answered in the native client while disconnected.
            for entry in entries:
                if entry['item'].get('type') == 'userMessage':
                    await questions.observe(self, tid, entry['item'], replay=True)
            seen, occurrences = set(track.get('seen', [])), Counter()
            for entry in entries:
                item = entry['item']
                if (item.get('type') == 'agentMessage' and item.get('questions')
                        and not re.fullmatch(r'item-\d+', str(item.get('id', '')))):
                    fingerprint = hashlib.sha256(json.dumps(
                        [item.get('text'), item['questions']], sort_keys=True).encode()).hexdigest()
                    occurrences[fingerprint] += 1
                    identity = fingerprint + ':' + str(occurrences[fingerprint])
                    await questions.observe(self, tid, item, replay=track['baseline'] or identity in seen)
                    seen.add(identity)
            track.update(baseline=False, scan=now, closed=not active, seen=sorted(seen))
        # Keep a bounded snapshot; older question identities live in the UI DB.
        state['turns'] = {turn['id']: known[turn['id']] for turn in turns}
        self.store.set(key, json.dumps(state))

    async def submit_question_answer(self, question, answers, client_id):
        from choice_questions import ChoiceError
        async with self.dispatch_lock:
            binding = (question.get('target_key'), question.get('thread_id'), question.get('topic_id'))
            current = (self.target['key'], self.thread_id, self.target['message_thread_id'])
            if (binding != current or self.target.get('topic_missing')
                    or not self.owner_allowed() or not self.ready.is_set()):
                raise ChoiceError('会话绑定或连接已变化，这个答案尚未提交。')
            if (question.get('kind') != 'async' or not isinstance(answers, list)
                    or len(answers) != len(question['questions'])
                    or any(not isinstance(answer, str) or not answer.strip() for answer in answers)):
                raise ChoiceError('答案无效，请重新选择。')
            turns = await self.codex.turns(limit=30)
            original = next((turn for turn in turns if turn['id'] == question['turn_id']), None)
            if not original:
                raise ChoiceError('原问题已超出可核验的会话记录，请在 Codex 查看。')
            source, answered = None, set()
            for turn in turns:
                async for entry in self.codex.items(turn['id']):
                    item = entry['item']
                    answered.update(answered_items(item))
                    if turn['id'] == original['id'] and item.get('id') == question['item_id']:
                        source = item
            if (not source or source.get('type') != 'agentMessage' or source.get('delivery') != 'async'
                    or not source.get('questions')):
                raise ChoiceError('无法核验原问题，请在 Codex 查看。')
            native_questions = source['questions']
            if (len(native_questions) != len(question['questions'])
                    or any(native.get('title') != expected['title']
                           or (native.get('options') or []) != expected['options']
                           for native, expected in zip(native_questions, question['questions']))):
                raise ChoiceError('原问题内容已变化，请在 Codex 查看。')
            if any((question['item_id'], index) in answered for index in range(len(answers))):
                raise ChoiceError('这个问题已在 Codex 回答，请查看最新回复。')
            envelope = [{'questionItemId': json.dumps(
                            ['request_user_input_async', question['item_id'], index],
                            ensure_ascii=False, separators=(',', ':')),
                         'question': prompt['title'], 'answer': answer}
                        for index, (prompt, answer) in enumerate(zip(question['questions'], answers))]
            text = ANSWER_START + '\n' + json.dumps(envelope, ensure_ascii=False) + '\n' + ANSWER_END
            identity = -int(hashlib.sha256(('question-answer:' + client_id).encode()).hexdigest()[:15], 16) - 1
            with self.store.db:
                old = self.store.db.execute('SELECT client_id FROM inbox WHERE id=? OR client_id=?',
                                            (identity, client_id)).fetchone()
                if old is not None:
                    raise ChoiceError('这个答案已提交或等待核验，请勿重复发送。')
                self.store.db.execute('INSERT INTO inbox '
                    '(id,chat_id,text,received_ms,state,client_id,message_json,message_thread_id) '
                    'VALUES(?,?,?,?,?,?,?,?)', (identity, self.owner, text, now_ms(), 'dispatching',
                                               client_id, '{}', binding[2]))
            params = {'threadId': self.thread_id, 'input': [{'type': 'text', 'text': text}],
                      'clientUserMessageId': client_id}
            active = next((turn for turn in turns if turn.get('status') == 'inProgress'), None)
            try:
                if active:
                    result = await self.codex.call('turn/steer', {**params, 'expectedTurnId': active['id']})
                    turn_id = result['turnId']
                else:
                    result = await self.codex.call('turn/start', params)
                    turn_id = result['turn']['id']
            except RpcError:
                self.store.mark(identity, 'failed')
                raise ChoiceError('Codex 未接受本次答案，请在原会话查看问题状态。') from None
            self.store.mark(identity, 'running', turn_id)
            self.receipt_event.set()
            return {'turn_id': turn_id}
