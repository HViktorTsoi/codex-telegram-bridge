"""Independent, owner-only recovery for a stopped Telegram receiver.

This module never receives Telegram updates or edits the bridge's input queues.
"""
import asyncio
import base64
import fcntl
import hashlib
import hmac
import json
import logging
import math
import os
from pathlib import Path
import re
import secrets
import sqlite3
import sys
import time
from urllib.parse import urlsplit

import aiohttp
from bridge import Telegram, TelegramError, permitted


LOG = logging.getLogger('codex_telegram_recovery')
PENDING = ('pending', 'dispatching', 'uncertain')
REASONS = {
    'telegram_poll_conflict': '同一个 Bot 被其它接收器同时轮询，消息接收已暂停。',
    'telegram_auth_failed': 'Telegram 凭据失效，需要先更新 Bot 配置。',
    'receiver_stopped': '消息接收器已停止，原有队列仍然保留。',
}


class GuardianError(RuntimeError):
    def __init__(self, message, status=409):
        super().__init__(message)
        self.status = status


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


def _read(path, default=None):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {} if default is None else default


class Guardian:
    TTL = 86400
    COOLDOWN = 60
    GRACE = 15
    HEALTH_WAIT = 35

    def __init__(self, state_dir, repo_dir, public_base_url=None, *, alerts_only=False, telegram=None, clock=time.time,
                 process_probe=None, start_bridge=None, wait_healthy=None):
        self.state_dir, self.repo_dir = Path(state_dir).resolve(), Path(repo_dir).resolve()
        self.alerts_only = bool(alerts_only)
        parsed = urlsplit(public_base_url or '')
        if (not self.alerts_only and (parsed.scheme != 'https' or not parsed.hostname or parsed.username
                or parsed.password or parsed.query or parsed.fragment)):
            raise ValueError('Recovery requires an HTTPS public base URL')
        self.public_base_url = (public_base_url or '').rstrip('/')
        self.base_url = self.public_base_url
        self.clock = clock
        self.telegram, self.session = telegram, None
        self._injected_telegram = telegram is not None
        self._verified = None
        self.last_delivery_error = None
        self._process_probe = process_probe or self._probe_process
        self._starter = start_bridge or self._start_default
        self._waiter = wait_healthy or self._await_healthy
        self._recover_lock = asyncio.Lock()
        self.directory = self.state_dir / 'recovery'
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        secret_path = self.directory / 'secret'
        try:
            fd = os.open(secret_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass
        else:
            with os.fdopen(fd, 'wb') as output:
                output.write(secrets.token_bytes(32))
        self.secret = secret_path.read_bytes()
        if len(self.secret) != 32:
            raise ValueError('Recovery secret is invalid')
        self.db = sqlite3.connect(self.directory / 'guardian.sqlite')
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS kv(key TEXT PRIMARY KEY,value TEXT);
            CREATE TABLE IF NOT EXISTS incidents(
                id TEXT PRIMARY KEY,source_id TEXT UNIQUE,at REAL,code INTEGER,reason TEXT,
                bot_id INTEGER,bot_username TEXT,host_id TEXT,token_digest TEXT);
            CREATE TABLE IF NOT EXISTS cards(
                id TEXT PRIMARY KEY,incident_id TEXT,token_hash TEXT UNIQUE,action_nonce TEXT,
                target_key TEXT,thread_id TEXT,topic_id INTEGER,owner_id TEXT,bot_id INTEGER,
                host_id TEXT,token_digest TEXT,state TEXT DEFAULT 'queued',message_id INTEGER,
                expires_at REAL,retry_at REAL DEFAULT 0,result TEXT,dirty INTEGER DEFAULT 0,
                UNIQUE(incident_id,target_key));
            CREATE TABLE IF NOT EXISTS requests(
                card_id TEXT PRIMARY KEY,nonce TEXT,state TEXT,result TEXT,at REAL);
        ''')
        (self.directory / 'guardian.sqlite').chmod(0o600)
        previous_url = self._get('public_base_url')
        previous_mode = self._get('alerts_only')
        with self.db:
            if ((previous_url is not None and previous_url != self.public_base_url)
                    or (previous_mode is not None and previous_mode != str(int(self.alerts_only)))):
                self.db.execute("UPDATE cards SET dirty=1 WHERE state='sent' AND message_id IS NOT NULL AND expires_at>?", (self.clock(),))
            self._set('public_base_url', self.public_base_url)
            self._set('alerts_only', int(self.alerts_only))
            self.db.execute("UPDATE cards SET state='uncertain' WHERE state='sending'")
            self.db.execute("UPDATE requests SET state='uncertain' WHERE state='starting'")

    def config(self):
        config = _read(self.state_dir / 'config.json')
        if (config.get('mode') != 'topics' or not isinstance(config.get('targets'), list)
                or not config.get('owner_id') or not config.get('expected_bot_username')):
            raise GuardianError('本机桥接配置暂不可读取。', 503)
        return config

    @property
    def owner_id(self):
        return str(self.config()['owner_id'])

    def bot_token(self):
        config = self.config()
        text = (Path(config['telegram_state_dir']) / '.env').read_text()
        match = re.search(r'^\s*TELEGRAM_BOT_TOKEN\s*=\s*(.*)$', text, re.M)
        if not match:
            raise GuardianError('本机 Telegram 凭据暂不可读取。', 503)
        token = match[1].strip().strip('\"\'')
        if not token:
            raise GuardianError('本机 Telegram 凭据暂不可读取。', 503)
        return token

    def _allowed(self):
        config = self.config()
        access = _read(Path(config['telegram_state_dir']) / 'access.json')
        owner = str(config['owner_id'])
        return permitted({'from': {'id': owner}, 'chat': {'id': owner, 'type': 'private'}}, access, owner)

    def _token_digest(self):
        return hashlib.sha256(self.bot_token().encode()).hexdigest()

    def _get(self, key, default=None):
        row = self.db.execute('SELECT value FROM kv WHERE key=?', (key,)).fetchone()
        return row[0] if row else default

    def _set(self, key, value):
        self.db.execute('INSERT OR REPLACE INTO kv VALUES(?,?)', (key, str(value)))

    def _card_token(self, identity):
        return base64.urlsafe_b64encode(hmac.new(self.secret, ('card:' + identity).encode(), hashlib.sha256).digest()).decode().rstrip('=')

    def resolve_card(self, token):
        if self.alerts_only:
            raise GuardianError('网页恢复入口尚未启用，请联系维护者检查。', 403)
        if not isinstance(token, str) or not re.fullmatch(r'[A-Za-z0-9_-]{43}', token):
            raise GuardianError('恢复入口不存在。', 404)
        row = self.db.execute('SELECT c.*,i.code,i.reason,i.bot_username FROM cards c '
                              'JOIN incidents i ON i.id=c.incident_id WHERE token_hash=?',
                              (hashlib.sha256(token.encode()).hexdigest(),)).fetchone()
        return self._validate_card(row)

    def _validate_card(self, row):
        if not row:
            raise GuardianError('恢复入口不存在。', 404)
        if not self.alerts_only and row['expires_at'] <= self.clock():
            raise GuardianError('恢复入口已过期，请查看最新故障提示。', 410)
        config = self.config()
        target = next((t for t in config['targets'] if t.get('key') == row['target_key']), None)
        if (not self._allowed() or row['owner_id'] != str(config['owner_id'])
                or row['host_id'] != config.get('host_id') or row['token_digest'] != self._token_digest()
                or row['bot_username'].lower() != config['expected_bot_username'].lstrip('@').lower()
                or not target or target.get('topic_missing') or target.get('thread_id') != row['thread_id']
                or target.get('message_thread_id') != row['topic_id']):
            raise GuardianError('原话题、账号或会话绑定已变化，入口已失效。', 403)
        return {**dict(row), 'session_name': str(target.get('name') or '当前会话'), 'card_id': row['id']}

    async def _ensure_telegram(self):
        if not self._allowed():
            raise GuardianError('当前账号不再允许使用此机器人。', 403)
        digest = self._token_digest()
        config = self.config()
        expected = config['expected_bot_username'].lstrip('@').lower()
        if self._verified and self._verified[:2] == (digest, expected):
            return self._verified[2]
        if not self._injected_telegram:
            if self.session is None or self.session.closed:
                self.session = aiohttp.ClientSession()
            self.telegram = Telegram(self.bot_token(), self.session)
        me = await asyncio.wait_for(self.telegram.call('getMe'), 10)
        if not isinstance(me.get('id'), int) or str(me.get('username', '')).lower() != expected:
            raise GuardianError('机器人身份与本机配置不符。', 403)
        self._verified = (digest, expected, me)
        return me

    def _probe_process(self, pid):
        path = Path('/proc') / str(pid)
        if not path.exists():
            return {'exists': False, 'exact': False}
        try:
            args = (path / 'cmdline').read_bytes().rstrip(b'\0').split(b'\0')
            expected = ['-u', str(self.repo_dir / 'topics_bridge.py'), 'run', '--state-dir', str(self.state_dir)]
            exact = (len(args) == 6 and Path(os.fsdecode(args[0])).resolve() == Path(sys.executable).resolve()
                     and [os.fsdecode(arg) for arg in args[1:]] == expected)
            return {'exists': True, 'exact': exact}
        except OSError:
            return {'exists': True, 'exact': False}

    def _lock_free(self):
        try:
            with (self.state_dir / 'bridge.lock').open('a') as handle:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return True
        except (BlockingIOError, OSError):
            return False

    def receiver_state(self):
        health = _read(self.state_dir / 'health.json')
        pid = health.get('pid')
        probe = self._process_probe(pid) if type(pid) is int and pid > 0 else {'exists': False, 'exact': False}
        if probe.get('exists'):
            if not probe.get('exact'):
                return 'unsafe', health
            fresh = self.clock() - float(health.get('updated_at') or 0) < 30
            last_poll = health.get('last_poll_at')
            polled = (type(last_poll) in (int, float) and math.isfinite(last_poll)
                      and 0 < last_poll <= self.clock() + 5)
            return ('healthy' if fresh and polled and health.get('connected')
                    and not health.get('last_error') else 'running'), health
        if not self._lock_free():
            return 'busy', health
        maintenance = _read(self.directory / 'maintenance.json')
        if float(maintenance.get('until') or 0) > self.clock():
            return 'maintenance', health
        return 'stopped', health

    def _queue_counts(self, card):
        collected = {}
        try:
            with sqlite3.connect('file:' + str(self.state_dir / 'topics.sqlite') + '?mode=ro', uri=True) as db:
                db.row_factory = sqlite3.Row
                for row in db.execute("SELECT id,message_json FROM jobs WHERE target_key=? AND state='pending' AND error IS NULL", (card['target_key'],)):
                    message = json.loads(row['message_json'] or '{}')
                    collected[row['id']] = ('pending', message)
        except (sqlite3.Error, OSError, ValueError):
            pass
        try:
            path = self.state_dir / 'targets' / card['target_key'] / 'bridge.sqlite'
            with sqlite3.connect('file:' + str(path) + '?mode=ro', uri=True) as db:
                db.row_factory = sqlite3.Row
                for row in db.execute("SELECT id,state,message_json FROM inbox WHERE state IN ('pending','dispatching','uncertain','running')"):
                    collected[row['id']] = (row['state'], json.loads(row['message_json'] or '{}'))
        except (sqlite3.Error, OSError, ValueError):
            pass
        pending = [(state, message) for state, message in collected.values() if state in PENDING]
        return {'pending_messages': len(pending),
                'pending_files': sum(bool(any(key in message for key in ('document', 'photo', 'video', 'audio', 'voice', 'animation'))) for _, message in pending),
                'uncertain_inputs': sum(state == 'uncertain' for state, _ in collected.values()),
                'running_inputs': sum(state == 'running' for state, _ in collected.values())}

    async def status(self, token):
        card = self.resolve_card(token)
        state, health = self.receiver_state()
        request = self.db.execute('SELECT * FROM requests WHERE card_id=?', (card['id'],)).fetchone()
        last_attempt = self._get('last_attempt_at')
        cooldown = max(0, math.ceil(self.COOLDOWN - (self.clock() - float(last_attempt)))) if last_attempt is not None else 0
        can = state == 'stopped' and card['code'] != 401 and not request and cooldown == 0
        return {'session_name': card['session_name'], 'receiver_state': state,
                'reason': REASONS.get(card['reason'], REASONS['receiver_stopped']),
                **self._queue_counts(card), 'can_recover': can, 'cooldown_seconds': cooldown,
                'action_nonce': card['action_nonce'], 'shared_receiver': True,
                'local_sessions': len(self.config()['targets']),
                'last_result': json.loads(request['result']) if request and request['result'] else None}

    async def _start_default(self):
        process = await asyncio.create_subprocess_exec(
            sys.executable, str(self.repo_dir / 'manage.py'), 'start',
            '--state-dir', str(self.state_dir), '--session-name', 'codex-telegram-bridge',
            cwd=str(self.repo_dir), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            await asyncio.wait_for(process.communicate(), 10)
        except BaseException:
            if process.returncode is None:
                process.kill()
                await process.wait()
            raise
        if process.returncode:
            raise GuardianError('接收器启动未成功，请联系维护者查看。', 503)

    async def _await_healthy(self, started, previous_pid):
        deadline = asyncio.get_running_loop().time() + self.HEALTH_WAIT
        while asyncio.get_running_loop().time() < deadline:
            state, health = self.receiver_state()
            if (state == 'healthy' and health.get('pid') != previous_pid
                    and float(health.get('updated_at') or 0) >= started
                    and float(health.get('last_poll_at') or 0) >= started):
                return True
            await asyncio.sleep(.5)
        return False

    async def recover(self, token, request_key):
        card = self.resolve_card(token)
        if not isinstance(request_key, str) or not hmac.compare_digest(request_key, card['action_nonce']):
            raise GuardianError('恢复请求无效，请重新打开维护页。', 403)
        async with self._recover_lock:
            card = self.resolve_card(token)
            prior = self.db.execute('SELECT * FROM requests WHERE card_id=?', (card['id'],)).fetchone()
            if prior:
                if prior['result']:
                    return json.loads(prior['result'])
                return {'ok': False, 'state': prior['state'], 'message': '上次恢复仍在核对或结果未知，请检查状态，不要重复提交。'}
            state, health = self.receiver_state()
            if state == 'healthy':
                return {'ok': True, 'state': state, 'message': '消息接收器已经在线，无需重新启动。'}
            if state != 'stopped':
                raise GuardianError('接收器仍在运行或由其它进程占用，请先检查状态。', 409)
            if card['code'] == 401:
                raise GuardianError('Telegram 凭据失效，需要维护者先修复配置。', 409)
            last_attempt = self._get('last_attempt_at')
            elapsed = self.clock() - float(last_attempt) if last_attempt is not None else self.COOLDOWN
            if elapsed < self.COOLDOWN:
                raise GuardianError('刚进行过恢复检查，请稍后再试。', 429)
            with self.db:
                self.db.execute('INSERT INTO requests VALUES(?,?,?,?,?)',
                                (card['id'], request_key, 'starting', None, self.clock()))
                self._set('last_attempt_at', self.clock())
            try:
                # The final binding and lock checks happen after the durable
                # claim, immediately before the only permitted start command.
                self.resolve_card(token)
                fresh_state, _ = self.receiver_state()
                if fresh_state != 'stopped':
                    raise GuardianError('接收器状态已变化，本次没有重复启动。', 409)
                started = self.clock()
                await self._starter()
                ready = await self._waiter(started, health.get('pid'))
                self.resolve_card(token)
                result = {'ok': bool(ready), 'state': 'healthy' if ready else 'unconfirmed',
                          'message': ('消息接收已恢复，原队列会继续处理。'
                                      if ready else '启动结果尚未确认；不会自动重复启动，请检查状态。')}
            except BaseException as error:
                with self.db:
                    self.db.execute("UPDATE requests SET state='uncertain' WHERE card_id=?", (card['id'],))
                if isinstance(error, asyncio.CancelledError):
                    raise
                result = {'ok': False, 'state': 'uncertain',
                          'message': str(error) if isinstance(error, GuardianError) else '恢复结果尚未确认，请检查状态。'}
            with self.db:
                self.db.execute('UPDATE requests SET state=?,result=? WHERE card_id=?',
                                ('done' if result['ok'] else 'uncertain', _json(result), card['id']))
                self.db.execute('UPDATE cards SET result=?,dirty=CASE WHEN message_id IS NOT NULL THEN 1 ELSE 0 END,'
                                "state=CASE WHEN message_id IS NULL AND state='queued' AND ? THEN 'resolved' ELSE state END "
                                'WHERE incident_id=?', (_json(result), int(result['ok']), card['incident_id']))
            await self._flush_cards()
            return result

    def _affected_targets(self, fault):
        config = self.config()
        current = {t['key']: t for t in config['targets']}
        wanted = []
        for candidate in fault.get('targets', []):
            if not isinstance(candidate, dict):
                continue
            target = current.get(candidate.get('target_key'))
            if (target and not target.get('topic_missing') and target.get('thread_id') == candidate.get('thread_id')
                    and target.get('message_thread_id') == candidate.get('topic_id')):
                wanted.append(target)
        if not wanted:
            for target in current.values():
                card = {'target_key': target['key']}
                if not target.get('topic_missing') and any(self._queue_counts(card).values()):
                    wanted.append(target)
        if not wanted:
            root = current.get(config.get('root_target'))
            if root and not root.get('topic_missing'):
                wanted = [root]
        return {t['key']: t for t in wanted}.values()

    async def _new_incident(self, fault):
        me = await self._ensure_telegram()
        config = self.config()
        identity = hashlib.sha256((str(config.get('host_id')) + ':' + str(fault['id'])).encode()).hexdigest()
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO incidents VALUES(?,?,?,?,?,?,?,?,?)',
                (identity, str(fault['id']), fault['at'], fault.get('code'), fault.get('reason', 'receiver_stopped'),
                 me['id'], me['username'], config.get('host_id'), self._token_digest()))
            for target in self._affected_targets(fault):
                cid = secrets.token_hex(16)
                token = self._card_token(cid)
                self.db.execute('INSERT OR IGNORE INTO cards '
                    '(id,incident_id,token_hash,action_nonce,target_key,thread_id,topic_id,owner_id,bot_id,host_id,token_digest,expires_at) '
                    'VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                    (cid, identity, hashlib.sha256(token.encode()).hexdigest(), secrets.token_urlsafe(24),
                     target['key'], target['thread_id'], target['message_thread_id'], str(config['owner_id']), me['id'],
                     config.get('host_id'), self._token_digest(), self.clock() + self.TTL))
        return identity

    def _card_text(self, card):
        counts = self._queue_counts(card)
        result = json.loads(card['result']) if card.get('result') else None
        recovered = bool(result and result.get('ok'))
        text = ('✅ ' if recovered else '⚠️ ') + card['session_name']
        text += ' · 消息接收已恢复\n\n' if recovered else ' · 消息接收异常\n\n'
        text += ('此前：' if recovered else '') + REASONS.get(card['reason'], REASONS['receiver_stopped'])
        text += f"\n待处理消息：{counts['pending_messages']} · 附件：{counts['pending_files']}"
        text += '\n原队列和会话绑定已保留。'
        if not recovered:
            text += ('\n请联系维护者检查，恢复后会在本话题更新。' if self.alerts_only
                     else '\n恢复会检查本机此 Bot 的消息接收器。')
        if result:
            text += '\n\n' + result['message']
        return text

    async def _flush_cards(self):
        try:
            await self._ensure_telegram()
        except Exception as error:
            self.last_delivery_error = type(error).__name__
            LOG.warning('Recovery bot unavailable: %s', type(error).__name__)
            return
        state, _ = self.receiver_state()
        if state == 'healthy':
            with self.db:
                self.db.execute("UPDATE cards SET state='resolved',dirty=0 WHERE state='queued' AND message_id IS NULL")
        rows = self.db.execute("SELECT * FROM cards WHERE (state='queued' OR dirty=1) AND retry_at<=? ORDER BY rowid LIMIT 5", (self.clock(),)).fetchall()
        had_error, delivered = False, False
        for saved in rows:
            token = self._card_token(saved['id']) if not self.alerts_only else None
            try:
                if self.alerts_only:
                    row = self.db.execute('SELECT c.*,i.code,i.reason,i.bot_username FROM cards c '
                                          'JOIN incidents i ON i.id=c.incident_id WHERE c.id=?', (saved['id'],)).fetchone()
                    card = self._validate_card(row)
                else:
                    card = self.resolve_card(token)
            except GuardianError:
                with self.db:
                    self.db.execute("UPDATE cards SET state='expired',dirty=0 WHERE id=?", (saved['id'],))
                continue
            editing = bool(card['message_id'])
            if not editing:
                with self.db:
                    claimed = self.db.execute("UPDATE cards SET state='sending' WHERE id=? AND state='queued'", (card['id'],)).rowcount
                if not claimed:
                    continue
            keyboard = [] if self.alerts_only else [[{'text': '检查并恢复', 'web_app': {
                'url': self.public_base_url + '/r/' + token}}]]
            params = {'chat_id': card['owner_id'], 'text': self._card_text(card),
                      'reply_markup': {'inline_keyboard': keyboard}, 'protect_content': True}
            if editing:
                params['message_id'] = card['message_id']
            else:
                params['message_thread_id'] = card['topic_id']
            try:
                sent = await asyncio.wait_for(self.telegram.call('editMessageText' if editing else 'sendMessage', **params), 10)
                mid = sent.get('message_id') if isinstance(sent, dict) else card['message_id'] if editing and sent is True else None
                if type(mid) is not int or mid <= 0 or (editing and mid != card['message_id']):
                    raise ConnectionError('Recovery card acknowledgement unavailable')
            except Exception as error:
                code = getattr(error, 'code', None)
                if editing and code == 400 and 'message is not modified' in str(error).lower():
                    mid = card['message_id']
                else:
                    with self.db:
                        self.db.execute('UPDATE cards SET state=?,dirty=?,retry_at=? WHERE id=?',
                            ('queued' if code == 429 else 'failed' if code in (400, 403, 404) else 'sent' if editing else 'uncertain',
                             int(editing and code not in (400, 403, 404)), self.clock() + max(5, getattr(error, 'retry_after', 5)), card['id']))
                    self.last_delivery_error = type(error).__name__
                    had_error = True
                    LOG.warning('Recovery card delivery failed: %s', type(error).__name__)
                    continue
            with self.db:
                self.db.execute("UPDATE cards SET state='sent',message_id=?,dirty=0,retry_at=0 WHERE id=?", (mid, card['id']))
            delivered = True
        if delivered and not had_error:
            self.last_delivery_error = None

    async def monitor_once(self):
        state, health = self.receiver_state()
        now = self.clock()
        if state in ('healthy', 'running'):
            with self.db:
                self._set('last_live', now)
                self._set('last_live_pid', health.get('pid'))
                if state == 'healthy':
                    self._set('last_healthy', now)
                    recovered = _json({'ok': True, 'state': 'healthy',
                                       'message': '消息接收已恢复，原队列会继续处理。'})
                    self.db.execute("UPDATE cards SET result=?,dirty=1 WHERE message_id IS NOT NULL "
                                    "AND state='sent' AND result IS NULL AND (? OR expires_at>?)",
                                    (recovered, int(self.alerts_only), now))
            await self._flush_cards()
            return
        if state in ('busy', 'unsafe'):
            return
        fault = _read(self.state_dir / 'control/recovery-fault.json')
        fresh_fault = (isinstance(fault.get('id'), str) and isinstance(fault.get('at'), (int, float))
                       and fault['at'] <= now + 5 and fault['at'] > float(self._get('last_healthy', '0'))
                       and fault['at'] >= float(health.get('last_poll_at') or 0)
                       and fault.get('host_id') == self.config().get('host_id'))
        if state == 'maintenance' and not fresh_fault:
            return
        if fresh_fault:
            await self._new_incident(fault)
        else:
            last_live = float(self._get('last_live', str(now)))
            if self._get('last_live') is None:
                with self.db:
                    self._set('last_live', now)
            if now - last_live < self.GRACE:
                return
            pid = health.get('pid') or self._get('last_live_pid', 'unknown')
            await self._new_incident({'id': 'receiver-stopped:' + str(pid), 'at': now,
                                      'reason': 'receiver_stopped', 'targets': []})
        await self._flush_cards()

    async def loop(self):
        while True:
            try:
                await self.monitor_once()
            except (GuardianError, TelegramError, OSError, sqlite3.Error, ConnectionError, asyncio.TimeoutError) as error:
                LOG.warning('Recovery monitor unavailable: %s', type(error).__name__)
            await asyncio.sleep(3)

    async def close(self):
        if self.session:
            await self.session.close()
        self.db.close()
