"""Persistent chat-wide command shortcuts, without native model interactions."""
import asyncio
import copy
import hashlib
import json
import logging
import time

from attachments import payload_from
from bridge import TelegramError, permitted


LOG = logging.getLogger('codex_telegram_keyboard')
BUTTON_COMMANDS = {
    '📚 会话': '/sessions',
    '📋 任务': '/tasks',
    '📥 待处理': '/inbox',
    '🤖 模型': '/model',
    '🧠 强度': '/effort',
    '🧩 技能': '/skills',
    '⏹ 停止': '/stop',
    '❓ 帮助': '/help',
}


def markup():
    # Omit style so Telegram renders the buttons using the client's theme.
    return {'keyboard': [[{'text': label} for label in ('📚 会话', '📋 任务', '⏹ 停止')]],
            'is_persistent': True, 'resize_keyboard': True, 'one_time_keyboard': False}


def normalize_update(update, protected=False):
    """Translate only exact, unformatted shortcut text; retain original metadata.

    Simple reply-keyboard presses are ordinary text messages. Telegram supplies
    no button-origin flag, so typed copies of these exact reserved labels behave
    identically. Protected input prompts and media stay with their own handlers.
    """
    if protected or update.get('callback_query'):
        return update
    message = update.get('message')
    if (not isinstance(message, dict) or payload_from(message)
            or 'forward_origin' in message or message.get('entities')):
        return update
    text = message.get('text')
    command = BUTTON_COMMANDS.get(text) if isinstance(text, str) else None
    if command is None:
        return update
    result = copy.deepcopy(update)
    result['message']['text'] = command
    return result


class QuickKeyboard:
    KEY = 'quick_keyboard_v1'
    TEXT = ('底部常用操作：会话、任务、停止。\n'
            '停止按所在话题执行；在 All 中操作主会话。\n'
            '未展开时可点输入框旁的键盘图标；/keyboard 可重新显示。')

    def __init__(self, router, clock=time.time):
        self.router, self.db, self.clock = router, router.store.db, clock
        self.enabled = router.config.get('quick_keyboard', True) is True
        self.lock = asyncio.Lock()
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS quick_keyboard_jobs(
                id TEXT PRIMARY KEY,update_id INTEGER,bot_id INTEGER,chat_id TEXT,
                fingerprint TEXT,origin_topic_id INTEGER,state TEXT DEFAULT 'pending',
                attempts INTEGER DEFAULT 0,retry_at REAL DEFAULT 0,message_id INTEGER,
                error TEXT,created_at REAL);
            CREATE INDEX IF NOT EXISTS quick_keyboard_pending
                ON quick_keyboard_jobs(fingerprint,state,retry_at,created_at);
        ''')

    def state(self):
        row = self.db.execute('SELECT value FROM kv WHERE key=?', (self.KEY,)).fetchone()
        try:
            value = json.loads(row[0]) if row else {}
            return value if isinstance(value, dict) else {}
        except (TypeError, ValueError):
            return {}

    def _identity(self):
        bot_id = getattr(self.router, 'bot_id', None)
        if (not self.enabled or type(bot_id) is not int or bot_id <= 0
                or not getattr(self.router, 'telegram', None) or not self.router.owner_allowed()):
            return None
        username = str(getattr(self.router, 'username', '') or '').lstrip('@').casefold()
        expected = str(self.router.config.get('expected_bot_username', '') or '').lstrip('@').casefold()
        if expected and username != expected:
            return None
        owner = str(self.router.owner)
        if not owner.isdigit() or int(owner) <= 0:
            return None
        payload = {'bot_id': bot_id, 'chat_id': owner, 'layout': markup()}
        fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        return bot_id, owner, fingerprint

    def ensure_startup(self):
        """Insert one frozen installation intent per verified Bot/owner/layout."""
        identity = self._identity()
        if identity is None:
            return False
        bot_id, owner, fingerprint = identity
        state = self.state()
        if state.get('fingerprint') == fingerprint and state.get('state') in ('sent', 'sending', 'uncertain'):
            return False
        if self.db.execute('SELECT 1 FROM quick_keyboard_jobs WHERE fingerprint=? LIMIT 1',
                           (fingerprint,)).fetchone():
            return False  # A manual request can win the first-startup race.
        with self.db:
            return bool(self.db.execute('INSERT OR IGNORE INTO quick_keyboard_jobs '
                '(id,bot_id,chat_id,fingerprint,created_at) VALUES(?,?,?,?,?)',
                ('startup:' + fingerprint, bot_id, owner, fingerprint, self.clock())).rowcount)

    def ingest(self, update):
        if not self.enabled or update.get('callback_query'):
            return False
        message = update.get('message', {})
        if (not isinstance(message, dict) or not isinstance(message.get('text'), str)
                or message['text'].strip() != '/keyboard'
                or payload_from(message) or 'forward_origin' in message
                or not permitted(message, self.router.access(), self.router.owner)):
            return False
        identity = self._identity()
        if identity is None:
            return False
        bot_id, owner, fingerprint = identity
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO quick_keyboard_jobs '
                '(id,update_id,bot_id,chat_id,fingerprint,origin_topic_id,created_at) VALUES(?,?,?,?,?,?,?)',
                ('manual:' + fingerprint + ':' + str(update['update_id']), update['update_id'],
                 bot_id, owner, fingerprint, message.get('message_thread_id'), self.clock()))
            self.db.execute("UPDATE kv SET value=? WHERE key='offset'",
                            (str(max(self.router.store.offset(), update['update_id'] + 1)),))
        return True

    def _finish(self, job, state, error=None, message_id=None, retry_at=0):
        snapshot = {'bot_id': job['bot_id'], 'chat_id': job['chat_id'],
                    'fingerprint': job['fingerprint'], 'job_id': job['id'],
                    'state': state, 'message_id': message_id, 'updated_at': self.clock()}
        if error:
            snapshot['error'] = error
        with self.db:
            self.db.execute('UPDATE quick_keyboard_jobs SET state=?,error=?,message_id=?,retry_at=? WHERE id=?',
                            (state, error, message_id, retry_at, job['id']))
            self.db.execute('INSERT OR REPLACE INTO kv VALUES(?,?)',
                            (self.KEY, json.dumps(snapshot, ensure_ascii=False)))

    def recover(self):
        # A process may have died after sending but before storing the ACK.
        with self.db:
            self.db.execute("UPDATE quick_keyboard_jobs SET state='uncertain',error='Restart during send' "
                            "WHERE state='sending'")
            state = self.state()
            if state.get('state') == 'sending':
                state.update(state='uncertain', error='Restart during send', updated_at=self.clock())
                self.db.execute('INSERT OR REPLACE INTO kv VALUES(?,?)',
                                (self.KEY, json.dumps(state, ensure_ascii=False)))

    async def handle_one(self):
        async with self.lock:
            identity = self._identity()
            if identity is None:
                return False
            bot_id, owner, fingerprint = identity
            job = self.db.execute("SELECT * FROM quick_keyboard_jobs WHERE fingerprint=? AND bot_id=? "
                "AND chat_id=? AND state='pending' AND retry_at<=? ORDER BY created_at,rowid LIMIT 1",
                (fingerprint, bot_id, owner, self.clock())).fetchone()
            if job is None:
                return False
            with self.db:
                claimed = self.db.execute("UPDATE quick_keyboard_jobs SET state='sending',attempts=attempts+1 "
                                          "WHERE id=? AND state='pending'", (job['id'],)).rowcount
                if not claimed:
                    return False
                self._finish(job, 'sending')
            telegram = self.router.telegram
            try:
                result = await telegram.call('sendMessage', chat_id=owner, text=self.TEXT,
                                             disable_notification=True, reply_markup=markup())
                message_id = result.get('message_id') if isinstance(result, dict) else None
                chat = result.get('chat') if isinstance(result, dict) else None
                sender = result.get('from') if isinstance(result, dict) else None
                if (type(message_id) is not int or message_id <= 0
                        or (isinstance(chat, dict) and str(chat.get('id')) != owner)
                        or (isinstance(sender, dict) and sender.get('id') != bot_id)):
                    self._finish(job, 'uncertain', 'Message acknowledgement unavailable')
                else:
                    self._finish(job, 'sent', message_id=message_id)
            except TelegramError as error:
                if error.code == 429:
                    self._finish(job, 'pending', 'Telegram:429',
                                 retry_at=self.clock() + max(3, error.retry_after))
                else:
                    self._finish(job, 'failed' if error.code in (400, 401, 403, 404) else 'uncertain',
                                 'Telegram:' + str(error.code))
            except Exception as error:
                # Unknown delivery results never become an automatic second
                # installation. A new explicit /keyboard remains available.
                self._finish(job, 'uncertain', type(error).__name__)
                LOG.warning('Shortcut keyboard delivery unavailable (%s)', type(error).__name__)
            return True

    async def loop(self):
        while True:
            try:
                self.ensure_startup()
                await self.handle_one()
            except Exception as error:
                LOG.warning('Shortcut keyboard unavailable (%s)', type(error).__name__)
            await asyncio.sleep(.25)
