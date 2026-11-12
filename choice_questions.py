"""Native Codex questions rendered as topic-local, durable Telegram buttons."""
import asyncio
import hashlib
import json
import logging
import re
import secrets
import time

from attachments import payload_from
from bridge import TelegramError, permitted
from message_format import plain_chunks


LOG = logging.getLogger('codex_telegram_questions')
ACTIVE = ('queued', 'active')
TERMINAL = ('answered', 'resolved', 'expired', 'uncertain')
PROMPT_TITLE = '回答 Codex 问题：'


class ChoiceError(RuntimeError):
    pass


class AnswerNotSubmitted(ChoiceError):
    """A native preflight failed before creating a submission intent."""

    def __init__(self, message, *, retryable=True):
        super().__init__(message)
        self.retryable = retryable


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


def _questions(values, kind):
    if not isinstance(values, list) or not 1 <= len(values) <= 32:
        return None
    result = []
    for index, value in enumerate(values):
        if not isinstance(value, dict) or value.get('isSecret'):
            return None
        title = value.get('question') or value.get('title')
        qid = value.get('id') if kind == 'sync' else str(index)
        if not isinstance(title, str) or not title.strip() or not isinstance(qid, str) or not qid:
            return None
        options, descriptions = [], []
        choices = value.get('options') or []
        if not isinstance(choices, list) or len(choices) > 30:
            return None
        for choice in choices:
            label = choice.get('label') if isinstance(choice, dict) else choice
            if not isinstance(label, str) or not label.strip():
                return None
            options.append(label)
            descriptions.append(str(choice.get('description') or '') if isinstance(choice, dict) else '')
        result.append({'id': qid, 'title': title, 'options': options, 'descriptions': descriptions})
    return result if len({row['id'] for row in result}) == len(result) else None


class ChoiceQuestions:
    TTL = 86400

    def __init__(self, router):
        self.router, self.db = router, router.store.db
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS choice_questions(
                id TEXT PRIMARY KEY,kind TEXT,target_key TEXT,thread_id TEXT,topic_id INTEGER,
                chat_id TEXT,bot_id INTEGER,turn_id TEXT,item_id TEXT,request_id TEXT,epoch TEXT,
                questions TEXT,answers TEXT DEFAULT '[]',position INTEGER DEFAULT 0,
                state TEXT DEFAULT 'queued',message_id INTEGER,expires_at REAL,
                dirty INTEGER DEFAULT 1,retry_at REAL DEFAULT 0,reason TEXT);
            CREATE TABLE IF NOT EXISTS choice_buttons(
                token TEXT PRIMARY KEY,question_id TEXT,position INTEGER,action TEXT,value INTEGER,
                message_id INTEGER,bot_id INTEGER);
            CREATE TABLE IF NOT EXISTS choice_prompts(
                chat_id TEXT,message_id INTEGER,topic_id INTEGER,bot_id INTEGER,
                question_id TEXT,position INTEGER,state TEXT DEFAULT 'active',
                PRIMARY KEY(chat_id,message_id));
            CREATE TABLE IF NOT EXISTS choice_prompt_claims(
                question_id TEXT,position INTEGER,state TEXT,
                PRIMARY KEY(question_id,position));
            CREATE TABLE IF NOT EXISTS choice_jobs(
                id INTEGER PRIMARY KEY,payload TEXT,state TEXT DEFAULT 'pending',error TEXT);
            CREATE TABLE IF NOT EXISTS choice_native_answers(
                thread_id TEXT,item_id TEXT,PRIMARY KEY(thread_id,item_id));
        ''')

    @staticmethod
    def _topic(message):
        return message.get('message_thread_id') or None

    @staticmethod
    def _origin(update):
        return update.get('callback_query', {}).get('message') or update.get('message', {})

    def _authorized(self, update):
        origin = self._origin(update)
        callback = update.get('callback_query')
        identity = {'from': callback.get('from', {}) if callback else origin.get('from', {}),
                    'chat': origin.get('chat', {})}
        if not permitted(identity, self.router.access(), self.router.owner):
            return False
        if callback:
            return (bool(self.router.bot_id) and origin.get('from', {}).get('id') == self.router.bot_id
                    and bool(origin.get('message_id')) and origin.get('date') != 0)
        return True

    def _binding(self, bridge):
        target = bridge.target
        return {'target_key': target['key'], 'thread_id': bridge.thread_id,
                'topic_id': target['message_thread_id']}

    def _bridge(self, question):
        target = self.router.targets.get(question['target_key'])
        bridge = self.router.bridges.get(question['target_key'])
        expected = {key: question[key] for key in ('target_key', 'thread_id', 'topic_id')}
        if (not self.router.owner_allowed() or not target or not bridge or target.get('topic_missing')
                or bridge.target.get('topic_missing') or self._binding(bridge) != expected
                or target['thread_id'] != question['thread_id']
                or target['message_thread_id'] != question['topic_id']
                or str(self.router.owner) != question['chat_id']
                or self.router.bot_id != question['bot_id']):
            raise ChoiceError('会话绑定已变化，这个问题按钮已失效。')
        return bridge

    def _remember(self, bridge, kind, turn_id, item_id, questions, request_id=None, epoch=None):
        binding = self._binding(bridge)
        if not item_id or not turn_id or not self.router.bot_id or bridge.target.get('topic_missing'):
            return
        identity = hashlib.sha256(_json([bridge.thread_id, kind, turn_id, item_id]).encode()).hexdigest()
        existing = self.db.execute('SELECT * FROM choice_questions WHERE id=?', (identity,)).fetchone()
        with self.db:
            if existing:
                # A replay confirms a pending request on this exact connection;
                # it never retries an uncertain or already submitted answer.
                if kind == 'sync' and existing['state'] in ACTIVE:
                    if existing['questions'] != _json(questions):
                        self._close(identity, 'expired', '原生问题内容已变化。')
                    else:
                        self.db.execute('UPDATE choice_questions SET request_id=?,epoch=? WHERE id=?',
                                        (_json(request_id), epoch, identity))
                return
            if kind == 'async' and self.db.execute(
                    'SELECT 1 FROM choice_native_answers WHERE thread_id=? AND item_id=?',
                    (bridge.thread_id, item_id)).fetchone():
                return
            self.db.execute('INSERT INTO choice_questions '
                            '(id,kind,target_key,thread_id,topic_id,chat_id,bot_id,turn_id,item_id,'
                            'request_id,epoch,questions,expires_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                            (identity, kind, binding['target_key'], binding['thread_id'], binding['topic_id'],
                             str(self.router.owner), self.router.bot_id, turn_id, item_id,
                             _json(request_id), epoch, _json(questions), time.time() + self.TTL))

    def _close(self, identity, state, reason):
        self.db.execute('UPDATE choice_questions SET state=?,reason=?,dirty=1 WHERE id=?',
                        (state, reason, identity))
        self.db.execute("UPDATE choice_prompts SET state='closed' WHERE question_id=?", (identity,))
        self.db.execute("UPDATE choice_prompt_claims SET state='closed' WHERE question_id=?", (identity,))

    async def observe(self, bridge, turn_id, item, replay=False):
        if item.get('type') == 'agentMessage' and item.get('delivery') == 'async':
            questions = _questions(item.get('questions'), 'async')
            if questions and not replay:
                self._remember(bridge, 'async', turn_id, item.get('id'), questions)
        if item.get('type') != 'userMessage':
            return
        content = item.get('content', [])
        text = item.get('text', '')
        if isinstance(content, list):
            text += '\n'.join(part.get('text', '') for part in content if isinstance(part, dict)
                              and isinstance(part.get('text'), str))
        for match in re.finditer(r'<send_user_message_question_reply>\s*(.*?)\s*</send_user_message_question_reply>', text, re.S):
            try:
                answers = json.loads(match[1])
                if not isinstance(answers, list):
                    continue
                for answer in answers:
                    qid = json.loads(answer.get('questionItemId', ''))
                    if (not isinstance(qid, list) or len(qid) != 3 or qid[0] != 'request_user_input_async'
                            or not isinstance(qid[1], str) or not isinstance(qid[2], int)
                            or not isinstance(answer.get('answer'), str)):
                        continue
                    with self.db:
                        self.db.execute('INSERT OR IGNORE INTO choice_native_answers VALUES(?,?)',
                                        (bridge.thread_id, qid[1]))
                        for row in self.db.execute("SELECT id FROM choice_questions WHERE thread_id=? AND item_id=? "
                                                   "AND kind='async' AND state IN ('queued','publishing','active','submitting','uncertain')",
                                                   (bridge.thread_id, qid[1])).fetchall():
                            self._close(row['id'], 'resolved', '已在 Codex 中回答。')
            except (ValueError, TypeError, AttributeError):
                continue

    async def server_event(self, bridge, event):
        method, params = event.get('method'), event.get('params', {})
        epoch = event.get('_user_input_epoch')
        supported = method in ('item/tool/requestUserInput', 'serverRequest/resolved')
        if (params.get('threadId') != bridge.thread_id or not epoch
                or epoch != getattr(bridge.codex, 'user_input_epoch', None)):
            return supported
        if method == 'item/tool/requestUserInput' and 'id' in event:
            questions = _questions(params.get('questions'), 'sync')
            if questions:
                pending = getattr(bridge.codex, 'pending_user_inputs', {}).get(event['id'])
                if (not isinstance(pending, dict) or pending.get('threadId') != bridge.thread_id
                        or pending.get('turnId') != params.get('turnId')
                        or pending.get('itemId') != params.get('itemId')
                        or _questions(pending.get('questions'), 'sync') != questions):
                    return True
                self._remember(bridge, 'sync', params.get('turnId'), params.get('itemId'),
                               questions, event['id'], epoch)
                return True
        elif method == 'serverRequest/resolved':
            with self.db:
                rows = self.db.execute("SELECT id FROM choice_questions WHERE thread_id=? AND kind='sync' "
                                       "AND request_id=? AND epoch=? AND state IN ('queued','publishing','active','submitting')",
                                       (bridge.thread_id, _json(params.get('requestId')), epoch)).fetchall()
                for row in rows:
                    self._close(row['id'], 'resolved', '已在 Codex 中处理，或问题已结束。')
            return True
        return False

    def _prompt(self, message):
        parent = message.get('reply_to_message') or {}
        if parent.get('from', {}).get('id') != self.router.bot_id:
            return None
        return self.db.execute('SELECT * FROM choice_prompts WHERE chat_id=? AND message_id=?',
                               (str(message.get('chat', {}).get('id')), parent.get('message_id'))).fetchone()

    def ingest(self, update):
        callback, message = update.get('callback_query'), update.get('message', {})
        if callback:
            if not str(callback.get('data', '')).startswith('cq:'):
                return False
        else:
            if not self._prompt(message):
                return False
            command = message.get('text', '').split(maxsplit=1)
            if command and command[0].startswith('/') and command[0].split('@')[0].lower() != '/cancel':
                return False
        with self.db:
            if self._authorized(update):
                self.db.execute('INSERT OR IGNORE INTO choice_jobs(id,payload) VALUES(?,?)',
                                (update['update_id'], _json(update)))
            self.db.execute("UPDATE kv SET value=? WHERE key='offset'",
                            (str(max(self.router.store.offset(), update['update_id'] + 1)),))
        return True

    @staticmethod
    def _question(row):
        result = dict(row)
        result['questions'] = json.loads(result['questions'])
        result['answers'] = json.loads(result['answers'])
        result['request_id'] = json.loads(result['request_id'])
        return result

    def _current(self, identity, position=None):
        row = self.db.execute('SELECT * FROM choice_questions WHERE id=?', (identity,)).fetchone()
        if not row or row['state'] != 'active' or row['expires_at'] <= time.time():
            raise ChoiceError('问题已回答或已过期，请查看当前问题。')
        if position is not None and row['position'] != position:
            raise ChoiceError('这个选项已处理，请使用更新后的问题按钮。')
        question = self._question(row)
        self._bridge(question)
        return question

    def _sync_ready(self, bridge, question):
        params = getattr(bridge.codex, 'pending_user_inputs', {}).get(question['request_id'])
        if (question['epoch'] != getattr(bridge.codex, 'user_input_epoch', None) or not isinstance(params, dict)
                or params.get('threadId') != question['thread_id'] or params.get('turnId') != question['turn_id']
                or params.get('itemId') != question['item_id']
                or _questions(params.get('questions'), 'sync') != question['questions']):
            raise ChoiceError('Codex 正在重新连接，或此问题已在客户端处理。请稍后重试当前按钮。')

    async def _render(self, row):
        question = self._question(row)
        state, position = question['state'], question['position']
        if state in ACTIVE:
            self._bridge(question)
        elif (not self.router.owner_allowed() or question['chat_id'] != str(self.router.owner)
                or question['bot_id'] != self.router.bot_id):
            raise ChoiceError('不再允许更新此问题。')
        keyboard, tokens = [], []
        if state in ACTIVE:
            item = question['questions'][position]
            text = f"Codex 需要你的选择 · {position + 1}/{len(question['questions'])}\n\n{item['title']}"
            for index, label in enumerate(item['options']):
                text += f'\n\n{index + 1}. {label}'
                if item['descriptions'][index]:
                    text += '\n' + item['descriptions'][index]
                token = secrets.token_urlsafe(12)
                tokens.append((token, question['id'], position, 'choose', index, question['message_id'], question['bot_id']))
                keyboard.append([{'text': (f'{index + 1}. ' + ' '.join(label.split()))[:60], 'callback_data': 'cq:' + token}])
            token = secrets.token_urlsafe(12)
            tokens.append((token, question['id'], position, 'other', None, question['message_id'], question['bot_id']))
            keyboard.append([{'text': '自行输入', 'callback_data': 'cq:' + token}])
            if question.get('reason'):
                text += '\n\n' + question['reason']
        else:
            text = 'Codex 问题\n\n'
            text += '\n\n'.join(item['title'] + ('\n已选择：' + question['answers'][index]
                                                if index < len(question['answers']) else '')
                                for index, item in enumerate(question['questions']))
            text += '\n\n' + {'answered': '已提交给原 Codex 会话。', 'resolved': '此问题已处理。',
                    'expired': '此问题已失效。', 'uncertain': '提交或发送结果尚未确认，请在 Codex 客户端查看。'}[state]
            if question.get('reason'):
                text += '\n' + question['reason']
        text = next(plain_chunks(text, 3800))
        params = {'chat_id': question['chat_id'], 'text': text,
                  'reply_markup': {'inline_keyboard': keyboard}}
        editing = bool(question['message_id'])
        if editing:
            params['message_id'] = question['message_id']
        else:
            params['message_thread_id'] = question['topic_id']
            # Claim before the network call: an unknown send result is never
            # retried as a new Telegram message after a crash or timeout.
            with self.db:
                claimed = self.db.execute("UPDATE choice_questions SET state='publishing',dirty=0 WHERE id=? AND state='queued'",
                                          (question['id'],)).rowcount
            if not claimed:
                return
        try:
            result = await self.router.telegram.call('editMessageText' if editing else 'sendMessage', **params)
            message_id = result.get('message_id') if isinstance(result, dict) else question['message_id'] if editing else None
            if not message_id or (editing and message_id != question['message_id']):
                raise ConnectionError('Question message acknowledgement missing')
        except TelegramError as error:
            if editing and error.code == 400 and 'message is not modified' in str(error).lower():
                message_id = question['message_id']
            else:
                raise
        with self.db:
            self.db.execute('DELETE FROM choice_buttons WHERE question_id=?', (question['id'],))
            self.db.executemany('INSERT INTO choice_buttons VALUES(?,?,?,?,?,?,?)',
                                [(token, qid, pos, action, value, message_id, bot)
                                 for token, qid, pos, action, value, _, bot in tokens])
            if editing:
                self.db.execute('UPDATE choice_questions SET dirty=0,retry_at=0 WHERE id=? AND state=? AND position=?',
                                (question['id'], state, position))
            else:
                self.db.execute("UPDATE choice_questions SET message_id=?,"
                                "state=CASE WHEN state='publishing' THEN 'active' ELSE state END,"
                                "dirty=CASE WHEN state='publishing' THEN 0 ELSE dirty END WHERE id=?",
                                (message_id, question['id']))

    async def _notice(self, origin, text):
        params = {'chat_id': str(origin['chat']['id']), 'text': text, 'disable_notification': True}
        if self._topic(origin):
            params['message_thread_id'] = self._topic(origin)
        await self.router.telegram.call('sendMessage', **params)

    async def _other(self, question):
        with self.db:
            claim = self.db.execute('SELECT state FROM choice_prompt_claims WHERE question_id=? AND position=?',
                                    (question['id'], question['position'])).fetchone()
            if claim and claim['state'] in ('creating', 'active', 'uncertain'):
                raise ChoiceError('请回复已经发出的文字回答提示；若未收到，请在 Codex 客户端回答。')
            self.db.execute("INSERT INTO choice_prompt_claims VALUES(?,?,'creating') "
                            "ON CONFLICT(question_id,position) DO UPDATE SET state='creating'",
                            (question['id'], question['position']))
        title = next(plain_chunks(question['questions'][question['position']]['title'], 3300))
        params = {'chat_id': question['chat_id'], 'message_thread_id': question['topic_id'],
                  'text': PROMPT_TITLE + '\n' + title + '\n\n请回复这条消息输入答案；/cancel 取消文字输入。',
                  'reply_markup': {'force_reply': True, 'selective': True}}
        try:
            result = await self.router.telegram.call('sendMessage', **params)
            if not isinstance(result, dict) or not result.get('message_id'):
                raise ConnectionError('Question reply prompt acknowledgement missing')
        except TelegramError:
            with self.db:
                self.db.execute("UPDATE choice_prompt_claims SET state='closed' WHERE question_id=? AND position=?",
                                (question['id'], question['position']))
            raise
        except BaseException:
            with self.db:
                self.db.execute("UPDATE choice_prompt_claims SET state='uncertain' WHERE question_id=? AND position=?",
                                (question['id'], question['position']))
            raise
        with self.db:
            self.db.execute("UPDATE choice_prompts SET state='closed' WHERE question_id=?", (question['id'],))
            self.db.execute('INSERT INTO choice_prompts(chat_id,message_id,topic_id,bot_id,question_id,position) VALUES(?,?,?,?,?,?)',
                            (question['chat_id'], result['message_id'], question['topic_id'], question['bot_id'],
                             question['id'], question['position']))
            self.db.execute("UPDATE choice_prompt_claims SET state='active' WHERE question_id=? AND position=? AND state='creating'",
                            (question['id'], question['position']))

    async def _answer(self, question, answer):
        bridge = self._bridge(question)
        if question['kind'] == 'sync':
            self._sync_ready(bridge, question)
        answers = [*question['answers'], answer]
        final = len(answers) == len(question['questions'])
        with self.db:
            # Save only live input prompts. A safe preflight failure may reopen
            # these exact prompts, never older cancelled/closed reply entries.
            prompts = self.db.execute("SELECT chat_id,message_id FROM choice_prompts "
                                      "WHERE question_id=? AND position=? AND state='active'",
                                      (question['id'], question['position'])).fetchall()
            claim = self.db.execute("SELECT 1 FROM choice_prompt_claims WHERE question_id=? "
                                    "AND position=? AND state='active'",
                                    (question['id'], question['position'])).fetchone()
            changed = self.db.execute('UPDATE choice_questions SET answers=?,position=?,state=?,reason=NULL,dirty=1 '
                                      "WHERE id=? AND position=? AND state='active'",
                                      (_json(answers), question['position'] + (not final), 'submitting' if final else 'active',
                                       question['id'], question['position'])).rowcount
            if changed:
                self.db.execute("UPDATE choice_prompts SET state='closed' WHERE question_id=?", (question['id'],))
                self.db.execute("UPDATE choice_prompt_claims SET state='closed' WHERE question_id=?", (question['id'],))
        if not changed:
            raise ChoiceError('这个问题已处理。')
        note = getattr(bridge, 'note_user_interaction', None)
        if note is not None:
            try:
                note('choice:' + question['id'] + ':' + str(question['position']))
            except Exception as error:
                LOG.warning('Question preview interaction failed: %s', type(error).__name__)
        if not final:
            return
        try:
            if question['kind'] == 'sync':
                await asyncio.wait_for(bridge.codex.respond_user_input(question['request_id'],
                    {item['id']: {'answers': [value]} for item, value in zip(question['questions'], answers)}), 30)
            else:
                # Native submission owns its timeout and durable send boundary;
                # an outer timeout cannot tell preflight from an unknown send.
                await bridge.submit_question_answer(question, answers, 'telegram-choice-' + question['id'])
        except AnswerNotSubmitted as error:
            with self.db:
                restored = self.db.execute("UPDATE choice_questions SET state=?,answers=?,position=?,reason=?,dirty=1 "
                                          "WHERE id=? AND state='submitting'",
                                          ('active' if error.retryable else 'expired', _json(question['answers']),
                                           question['position'], str(error), question['id'])).rowcount
                if restored and error.retryable:
                    for prompt in prompts:
                        self.db.execute("UPDATE choice_prompts SET state='active' WHERE chat_id=? AND message_id=? "
                                        "AND question_id=? AND position=? AND state='closed'",
                                        (prompt['chat_id'], prompt['message_id'], question['id'], question['position']))
                    if claim:
                        self.db.execute("UPDATE choice_prompt_claims SET state='active' WHERE question_id=? "
                                        "AND position=? AND state='closed'", (question['id'], question['position']))
            raise
        except BaseException:
            with self.db:
                self.db.execute("UPDATE choice_questions SET state='uncertain',dirty=1 WHERE id=? AND state='submitting'",
                                (question['id'],))
            raise
        with self.db:
            self.db.execute("UPDATE choice_questions SET state='answered',dirty=1 WHERE id=? AND state='submitting'",
                            (question['id'],))

    async def _callback(self, update):
        callback, origin = update['callback_query'], self._origin(update)
        try:
            await self.router.telegram.call('answerCallbackQuery', callback_query_id=callback['id'])
        except (TelegramError, ConnectionError, asyncio.TimeoutError):
            pass
        button = self.db.execute('SELECT * FROM choice_buttons WHERE token=?', (callback['data'][3:],)).fetchone()
        if not button or button['message_id'] != origin['message_id'] or button['bot_id'] != self.router.bot_id:
            raise ChoiceError('这个选项按钮已失效。')
        question = self._current(button['question_id'], button['position'])
        if question['chat_id'] != str(origin['chat']['id']) or question['topic_id'] != self._topic(origin):
            raise ChoiceError('这个选项不属于当前话题。')
        if button['action'] == 'other':
            return await self._other(question)
        return await self._answer(question, question['questions'][question['position']]['options'][button['value']])

    async def _reply(self, update):
        message = update['message']
        prompt = self._prompt(message)
        if (not prompt or prompt['state'] != 'active' or prompt['bot_id'] != self.router.bot_id
                or prompt['topic_id'] != self._topic(message)):
            raise ChoiceError('文字回答入口已失效，请使用当前问题的按钮。')
        question = self._current(prompt['question_id'], prompt['position'])
        text = message.get('text', '').strip()
        command = text.split(maxsplit=1)[0].split('@')[0].lower() if text else ''
        if command == '/cancel':
            with self.db:
                self.db.execute("UPDATE choice_prompts SET state='closed' WHERE chat_id=? AND message_id=?",
                                (prompt['chat_id'], prompt['message_id']))
                self.db.execute("UPDATE choice_prompt_claims SET state='closed' WHERE question_id=? AND position=?",
                                (question['id'], question['position']))
            return await self._notice(message, '已取消文字输入，仍可使用原问题的选项按钮。')
        if not text or payload_from(message):
            raise ChoiceError('请用文字回复问题，不要附带文件。')
        if len(text) > 12000:
            raise ChoiceError('答案过长，请缩短到 12000 字符以内。')
        return await self._answer(question, text)

    async def handle_one(self):
        row = self.db.execute("SELECT * FROM choice_jobs WHERE state='pending' ORDER BY id LIMIT 1").fetchone()
        if not row:
            return False
        update = json.loads(row['payload'])
        with self.db:
            allowed = self._authorized(update) and self.router.owner_allowed()
            claimed = self.db.execute("UPDATE choice_jobs SET state=? WHERE id=? AND state='pending'",
                                      ('processing' if allowed else 'denied', row['id'])).rowcount
        if not claimed or not allowed:
            return True
        try:
            if update.get('callback_query'):
                await self._callback(update)
            else:
                await self._reply(update)
            with self.db:
                self.db.execute("UPDATE choice_jobs SET state='done' WHERE id=?", (row['id'],))
        except Exception as error:
            with self.db:
                self.db.execute("UPDATE choice_jobs SET state='failed',error=? WHERE id=?", (type(error).__name__, row['id']))
            detail = str(error) if isinstance(error, ChoiceError) else '暂时无法确认问题操作结果，请在 Codex 客户端查看。'
            try:
                await self._notice(self._origin(update), detail)
            except (TelegramError, ConnectionError, OSError, asyncio.TimeoutError):
                pass
            LOG.warning('Question operation failed: %s', type(error).__name__)
        return True

    async def flush_one(self):
        row = self.db.execute("SELECT * FROM choice_questions WHERE dirty=1 AND retry_at<=? "
                              "AND state IN ('queued','active','answered','resolved','expired','uncertain') ORDER BY rowid LIMIT 1",
                              (time.time(),)).fetchone()
        if not row:
            return False
        try:
            if row['state'] not in (*ACTIVE, *TERMINAL):
                return False
            if row['state'] in ACTIVE:
                if row['expires_at'] <= time.time():
                    with self.db:
                        self._close(row['id'], 'expired', '问题已过期。')
                    return True
                self._bridge(self._question(row))
            if not row['message_id'] and row['state'] != 'queued':
                with self.db:
                    self.db.execute('UPDATE choice_questions SET dirty=0 WHERE id=?', (row['id'],))
                return True
            await self._render(row)
        except ChoiceError as error:
            with self.db:
                self._close(row['id'], 'expired', str(error))
                if row['state'] in TERMINAL:
                    self.db.execute('UPDATE choice_questions SET dirty=0 WHERE id=?', (row['id'],))
        except TelegramError as error:
            with self.db:
                if error.code == 429:
                    self.db.execute("UPDATE choice_questions SET state=CASE WHEN state='publishing' THEN 'queued' ELSE state END,"
                                    'dirty=1,retry_at=? WHERE id=?', (time.time() + max(1, error.retry_after), row['id']))
                else:
                    self._close(row['id'], 'expired', 'Telegram 无法显示这个问题。')
                    self.db.execute('UPDATE choice_questions SET dirty=0 WHERE id=?', (row['id'],))
            LOG.warning('Question display refused: %s', error.code)
        except Exception as error:
            with self.db:
                self.db.execute("UPDATE choice_questions SET state='uncertain',dirty=0 WHERE id=? AND state='publishing'", (row['id'],))
                self.db.execute('UPDATE choice_questions SET retry_at=? WHERE id=? AND message_id IS NOT NULL',
                                (time.time() + 15, row['id']))
            LOG.warning('Question display failed: %s', type(error).__name__)
        return True

    def recover(self):
        with self.db:
            self.db.execute("UPDATE choice_jobs SET state='uncertain' WHERE state='processing'")
            self.db.execute("UPDATE choice_questions SET state='uncertain',dirty=1 WHERE state IN ('publishing','submitting')")
            self.db.execute("UPDATE choice_prompt_claims SET state='uncertain' WHERE state='creating'")

    def sweep(self):
        for row in self.db.execute("SELECT * FROM choice_questions WHERE state IN ('queued','active')").fetchall():
            try:
                if row['expires_at'] <= time.time():
                    raise ChoiceError('问题已过期。')
                self._bridge(self._question(row))
            except ChoiceError as error:
                with self.db:
                    self._close(row['id'], 'expired', str(error))

    async def _input_loop(self):
        while True:
            if not await self.handle_one():
                await asyncio.sleep(.2)

    async def _display_loop(self):
        next_sweep = 0
        while True:
            if time.monotonic() >= next_sweep:
                self.sweep()
                next_sweep = time.monotonic() + 5
            if not await self.flush_one():
                await asyncio.sleep(.2)

    async def loop(self):
        await asyncio.gather(self._input_loop(), self._display_loop())
