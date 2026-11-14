"""Topic-local file records and deliberate resends, without native model calls."""
import asyncio
import hashlib
import json
import logging
import math
from pathlib import Path
import secrets
import stat
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from attachments import media_items, payload_from
from bridge import TelegramError, permitted
from outgoing_images import ImageError, MAX_BYTES, send_image


LOG = logging.getLogger('codex_telegram_files')
MEDIA_METHODS = {
    'photo': ('sendPhoto', 'photo'), 'document': ('sendDocument', 'document'),
    'voice': ('sendVoice', 'voice'), 'audio': ('sendAudio', 'audio'),
    'video': ('sendVideo', 'video'), 'video_note': ('sendVideoNote', 'video_note'),
    'animation': ('sendAnimation', 'animation'), 'sticker': ('sendSticker', 'sticker'),
}


class FilesError(RuntimeError):
    pass


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def label(value, limit=110):
    # File names are plain labels, never Markdown or local-path instructions.
    value = str(value or '').replace('\\', '/').rsplit('/', 1)[-1]
    return ' '.join(value.split())[:limit] or '未命名文件'


def size_label(value):
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value) or value < 0:
        return '大小未知'
    for unit in ('B', 'KB', 'MB', 'GB'):
        if value < 1024 or unit == 'GB':
            return (str(int(value)) if unit == 'B' else f'{value:.1f}') + ' ' + unit
        value /= 1024


def json_value(text, default):
    try:
        value = json.loads(text) if text else default
        return value if isinstance(value, type(default)) else default
    except (TypeError, ValueError):
        return default

def timestamp(value, *, milliseconds=False):
    try:
        result = float(value) / (1000 if milliseconds else 1)
        return result if math.isfinite(result) and 0 < result < 32503680000 else 0
    except (TypeError, ValueError, OverflowError):
        return 0


class FilesMenu:
    PREFIX = 'fl:'
    PAGE = 6
    TTL = 900
    TIMEOUT = 20

    def __init__(self, router, clock=time.time):
        self.router, self.db, self.clock = router, router.store.db, clock
        self.job_locks, self.send_locks = {}, {}
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS fl_entries(
                token TEXT PRIMARY KEY,bot_id INTEGER,owner TEXT,binding TEXT,fingerprint TEXT UNIQUE);
            CREATE TABLE IF NOT EXISTS fl_jobs(
                id INTEGER PRIMARY KEY,payload TEXT,target_key TEXT,state TEXT DEFAULT 'pending',
                retry_at REAL DEFAULT 0,error TEXT,binding TEXT,bot_id INTEGER,owner TEXT);
            CREATE TABLE IF NOT EXISTS fl_panels(
                id TEXT PRIMARY KEY,chat_id TEXT,topic_id INTEGER,bot_id INTEGER,binding TEXT,
                message_id INTEGER,revision INTEGER DEFAULT 0,state TEXT DEFAULT 'creating',expires_at REAL,page INTEGER DEFAULT 0);
            CREATE TABLE IF NOT EXISTS fl_buttons(
                token TEXT PRIMARY KEY,panel_id TEXT,revision INTEGER,action TEXT,value TEXT,
                state TEXT DEFAULT 'creating');
            CREATE TABLE IF NOT EXISTS fl_sends(
                id TEXT PRIMARY KEY,token TEXT UNIQUE,panel_id TEXT,binding TEXT,bot_id INTEGER,
                chat_id TEXT,topic_id INTEGER,file_json TEXT,state TEXT DEFAULT 'pending',
                retry_at REAL DEFAULT 0,message_id INTEGER,error TEXT,notified INTEGER DEFAULT 0);
        ''')

    @staticmethod
    def binding(target):
        return {'target_key': target['key'], 'thread_id': target['thread_id'],
                'topic_id': target['message_thread_id']}

    @staticmethod
    def _origin(update):
        return update.get('callback_query', {}).get('message') or update.get('message', {})

    def _identity(self):
        bot = getattr(self.router, 'bot_id', None)
        expected = self.router.config.get('expected_bot_username', '').lstrip('@').casefold()
        username = str(getattr(self.router, 'username', '') or '').lstrip('@').casefold()
        if (type(bot) is not int or bot <= 0 or not self.router.owner_allowed()
                or (expected and expected != username)):
            raise FilesError('机器人身份或访问权限已变化，请重新打开文件列表。')
        return bot, str(self.router.owner)

    def _authorized(self, update):
        try:
            bot, owner = self._identity()
        except FilesError:
            return False
        origin, callback = self._origin(update), update.get('callback_query')
        actor = callback.get('from', {}) if callback else origin.get('from', {})
        if not permitted({'from': actor, 'chat': origin.get('chat', {})},
                         self.router.access(), owner):
            return False
        return not callback or (origin.get('from', {}).get('id') == bot
            and type(origin.get('message_id')) is int and origin['message_id'] > 0
            and origin.get('date') != 0)

    def _bridge(self, binding):
        self._identity()
        key = binding.get('target_key')
        target, bridge = self.router.targets.get(key), self.router.bridges.get(key)
        if (not target or not bridge or target.get('topic_missing') or bridge.target.get('topic_missing')
                or self.binding(target) != binding or self.binding(bridge.target) != binding
                or bridge.thread_id != binding.get('thread_id')
                or bridge.store.get('bound_thread_id') != bridge.thread_id):
            raise FilesError('会话绑定已变化，请在当前话题重新打开文件列表。')
        return bridge

    def entry_callback(self, bridge):
        """A route-bound opener can live on either an active or completed bar."""
        try:
            binding = self.binding(bridge.target)
            if self._bridge(binding) is not bridge:
                return None
            bot, owner = self._identity()
            fingerprint = hashlib.sha256(encoded([bot, owner, binding]).encode()).hexdigest()
            row = self.db.execute('SELECT token FROM fl_entries WHERE fingerprint=?', (fingerprint,)).fetchone()
            if row is None:
                token = secrets.token_urlsafe(12)
                with self.db:
                    self.db.execute('INSERT OR IGNORE INTO fl_entries VALUES(?,?,?,?,?)',
                                    (token, bot, owner, encoded(binding), fingerprint))
                row = self.db.execute('SELECT token FROM fl_entries WHERE fingerprint=?', (fingerprint,)).fetchone()
            return 'fl:open:' + row['token']
        except (FilesError, KeyError, TypeError, ValueError):
            return None

    def ingest(self, update):
        callback, message = update.get('callback_query'), update.get('message') or {}
        target_key, frozen = '__invalid__', None
        if callback:
            data = callback.get('data')
            if not isinstance(data, str) or not data.startswith(self.PREFIX):
                return False
            if data.startswith('fl:open:'):
                row = self.db.execute('SELECT binding FROM fl_entries WHERE token=?', (data[8:],)).fetchone()
            else:
                row = self.db.execute('SELECT p.binding FROM fl_buttons b JOIN fl_panels p ON p.id=b.panel_id '
                                      'WHERE b.token=?', (data[3:],)).fetchone()
            if row:
                frozen = json_value(row['binding'], {})
                target_key = frozen.get('target_key', '__invalid__')
        else:
            words = str(message.get('text') or '').strip().split()
            if len(words) != 1 or payload_from(message) or message.get('forward_origin') or message.get('forward_date'):
                return False
            command, _, username = words[0].partition('@')
            if command.casefold() != '/files' or (username and username.casefold() !=
                    str(getattr(self.router, 'username', '') or '').lstrip('@').casefold()):
                return False
            target = self.router.by_topic.get(message.get('message_thread_id'))
            target_key = target['key'] if target else '__unbound__'
            frozen = self.binding(target) if target else None
        with self.db:
            if self._authorized(update):
                bot, owner = self._identity()
                self.db.execute('INSERT OR IGNORE INTO fl_jobs(id,payload,target_key,binding,bot_id,owner) VALUES(?,?,?,?,?,?)',
                                (update['update_id'], encoded(update), target_key, encoded(frozen), bot, owner))
            self.db.execute("UPDATE kv SET value=? WHERE key='offset'",
                            (str(max(self.router.store.offset(), update['update_id'] + 1)),))
        return True

    @staticmethod
    def _snapshot_path(bridge, media):
        """Only this target's immutable outgoing snapshot stores are eligible."""
        try:
            path = Path(media['path'])
            directories = (bridge.store.images.directory, bridge.store.images.files_directory)
            if (not path.is_absolute() or path.is_symlink()
                    or path.parent not in directories or any(parent.is_symlink() for parent in path.parents)):
                raise FilesError('文件快照不可用，请重新生成或发送原文件。')
            info = path.lstat()
            if (not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= MAX_BYTES
                    or path.name != media['sha256'] + path.suffix
                    or info.st_size != media.get('bytes')):
                raise FilesError('文件快照不可用，请重新生成或发送原文件。')
            return path, info
        except (OSError, KeyError, TypeError, ValueError):
            raise FilesError('文件快照不可用，请重新生成或发送原文件。') from None

    def inventory(self, bridge):
        """Read known media records only; do not scan projects, tools or prose."""
        self._bridge(self.binding(bridge.target))
        owner, records = str(self.router.owner), []
        for row in bridge.store.db.execute('SELECT id,message_json,received_ms FROM inbox '
                "WHERE chat_id=? AND message_json IS NOT NULL AND message_json LIKE '%\"file_id\"%' "
                'ORDER BY received_ms DESC,id DESC', (owner,)):
            payload = json_value(row['message_json'], {})
            try:
                items = media_items(payload)
            except (TypeError, ValueError, KeyError, AttributeError):
                continue
            for item in items:
                if item.get('kind') not in MEDIA_METHODS or not isinstance(item.get('file_id'), str) or not item['file_id']:
                    continue
                identity = encoded(['in', item['kind'], item.get('file_unique_id') or item['file_id']])
                record = {'source': 'in', 'source_id': row['id'], 'identity': identity,
                          'name': label(item.get('file_name')), 'kind': item['kind'],
                          'size': item.get('file_size'), 'mime': item.get('mime_type'),
                          'stamp': timestamp(row['received_ms'], milliseconds=True), 'item': item}
                records.append(record)
        for row in bridge.store.db.execute('SELECT o.rowid AS ordering,o.key,o.media_json,t.started_at '
                'FROM outbox o LEFT JOIN completion_rows c ON c.key=o.key '
                'LEFT JOIN completion_turns t ON t.turn_id=c.turn_id AND t.chat_id=c.chat_id '
                'AND t.topic_id=c.topic_id AND t.native_id=? '
                'WHERE o.chat_id=? AND o.media_json IS NOT NULL AND o.message_id>0 '
                'ORDER BY o.rowid DESC', (bridge.thread_id, owner)):
            media = json_value(row['media_json'], {})
            if (not media.get('sha256') or not media.get('name') or media.get('delivery') != 'sent'
                    or media.get('method') not in ('sendPhoto', 'sendDocument')):
                continue
            stamp = timestamp(row['started_at'])
            # Legacy rows predate the completion ledger; only their recorded
            # snapshot metadata is consulted, never a directory scan.
            if not stamp:
                try:
                    stamp = timestamp(self._snapshot_path(bridge, media)[1].st_mtime)
                except FilesError:
                    stamp = 0
            records.append({'source': 'out', 'source_id': row['key'],
                            'identity': encoded(['out', media['sha256'], media['name'], media.get('kind', 'image')]),
                            'name': label(media['name']), 'kind': media.get('kind', 'image'),
                            'size': media.get('bytes'), 'mime': media.get('mime'), 'stamp': stamp,
                            'media': media})
        ordered, seen = [], set()
        for record in sorted(records, key=lambda r: (r['stamp'], str(r['source_id'])), reverse=True):
            if record['identity'] not in seen:
                seen.add(record['identity'])
                ordered.append(record)
        return ordered

    def _record(self, bridge, record):
        """Re-read the selected immutable ledger reference before a resend."""
        if record.get('source') == 'in':
            row = bridge.store.db.execute('SELECT message_json FROM inbox WHERE id=? AND chat_id=?',
                    (record.get('source_id'), str(self.router.owner))).fetchone()
            items = media_items(json_value(row['message_json'], {})) if row else []
            if not any(encoded(item) == encoded(record.get('item')) for item in items):
                raise FilesError('这条上传记录已变化，请刷新文件列表。')
        elif record.get('source') == 'out':
            row = bridge.store.db.execute('SELECT media_json,message_id FROM outbox WHERE key=? AND chat_id=?',
                    (record.get('source_id'), str(self.router.owner))).fetchone()
            media = json_value(row['media_json'], {}) if row else {}
            if not row or row['message_id'] <= 0 or encoded(media) != encoded(record.get('media')):
                raise FilesError('这条回传记录已变化，请刷新文件列表。')
            self._snapshot_path(bridge, media)
        else:
            raise FilesError('文件记录无效，请重新打开文件列表。')
        return record

    async def _notice(self, update, text):
        if not self._authorized(update):
            return
        origin = self._origin(update)
        params = {'chat_id': str(origin['chat']['id']), 'text': text, 'disable_notification': True}
        if origin.get('message_thread_id'):
            params['message_thread_id'] = origin['message_thread_id']
        await self.router.telegram.call('sendMessage', **params)

    async def _answer(self, update, text=None, alert=False):
        if update.get('callback_query') and self._authorized(update):
            params = {'callback_query_id': update['callback_query']['id']}
            if text:
                params.update(text=text, show_alert=alert)
            try:
                await self.router.telegram.call('answerCallbackQuery', **params)
            except (TelegramError, ConnectionError, OSError, asyncio.TimeoutError):
                pass

    def _panel(self, identity):
        return self.db.execute('SELECT * FROM fl_panels WHERE id=?', (identity,)).fetchone()

    async def _render(self, update, panel, page=0):
        binding = json_value(panel['binding'], {})
        bridge = self._bridge(binding)
        origin, bot_owner = self._origin(update), self._identity()
        if (bot_owner != (panel['bot_id'], panel['chat_id'])
                or origin.get('message_thread_id') != panel['topic_id']
                or str(origin['chat']['id']) != panel['chat_id']):
            raise FilesError('文件列表位置已变化，请在目标话题重新打开。')
        records = self.inventory(bridge)
        pages = max(1, math.ceil(len(records) / self.PAGE))
        page = max(0, min(int(page), pages - 1))
        revision = panel['revision'] + 1
        text = '会话文件 · ' + label(bridge.target['name'], 90) + f'\n共 {len(records)} 项 · 第 {page + 1}/{pages} 页'
        if not records:
            text += '\n\n还没有已记录的上传或回传附件。'
        rows = []
        for index, record in enumerate(records[page * self.PAGE:(page + 1) * self.PAGE], page * self.PAGE + 1):
            direction = '你上传的' if record['source'] == 'in' else 'Codex 回传的'
            unavailable = False
            if record['source'] == 'out':
                try:
                    self._snapshot_path(bridge, record['media'])
                except FilesError:
                    unavailable = True
            suffix = ' · 快照已不可用' if unavailable else ''
            date = datetime.fromtimestamp(record['stamp'], ZoneInfo('Asia/Hong_Kong')).strftime('%m/%d %H:%M') if record['stamp'] else '时间未知'
            text += f'\n\n{index}. {record["name"]}\n{direction} · {label(record.get("mime") or record["kind"], 70)} · {size_label(record["size"])} · {date}{suffix}'
            rows.append([(f'{index}. {label(record["name"], 48)}', 'unavailable' if unavailable else 'send', record)])
        navigation = []
        if page:
            navigation.append(('上一页', 'page', page - 1))
        if page + 1 < pages:
            navigation.append(('下一页', 'page', page + 1))
        if navigation:
            rows.append(navigation)
        rows.append([('刷新', 'refresh', None), ('关闭', 'close', None)])
        text += '\n\n点文件可在本话题重发。列表仅含已记录的附件。'
        keyboard, tokens = [], []
        with self.db:
            for row in rows:
                keys = []
                for name, action, value in row:
                    if action == 'unavailable':
                        keys.append({'text': name, 'disabled': {}})
                        continue
                    token = secrets.token_urlsafe(12)
                    self.db.execute('INSERT INTO fl_buttons(token,panel_id,revision,action,value) VALUES(?,?,?,?,?)',
                                    (token, panel['id'], revision, action, encoded(value)))
                    tokens.append(token)
                    keys.append({'text': name, 'callback_data': 'fl:' + token})
                keyboard.append(keys)
        params = {'chat_id': panel['chat_id'], 'text': text,
                  'reply_markup': {'inline_keyboard': keyboard}, 'disable_notification': True}
        editing = bool(panel['message_id'])
        if editing:
            params.pop('disable_notification')
            params['message_id'] = panel['message_id']
        else:
            params['message_thread_id'] = panel['topic_id']
        try:
            result = await self.router.telegram.call('editMessageText' if editing else 'sendMessage', **params)
        except TelegramError as error:
            if editing and error.code == 400 and 'message is not modified' in str(error).lower():
                result = {'message_id': panel['message_id']}
            else:
                raise
        self._bridge(binding)
        if not self._authorized(update) or self._identity() != bot_owner:
            raise FilesError('文件列表权限或身份已变化，请重新打开。')
        mid = result.get('message_id') if isinstance(result, dict) else panel['message_id'] if editing and result is True else None
        if (type(mid) is not int or mid <= 0 or (editing and mid != panel['message_id'])
                or isinstance(result, dict) and result.get('chat') and str(result['chat']['id']) != panel['chat_id']
                or isinstance(result, dict) and result.get('message_thread_id') not in (None, panel['topic_id'])):
            raise ConnectionError('File menu acknowledgement unavailable')
        with self.db:
            self.db.execute("UPDATE fl_buttons SET state='expired' WHERE panel_id=?", (panel['id'],))
            self.db.executemany("UPDATE fl_buttons SET state='active' WHERE token=?", [(token,) for token in tokens])
            self.db.execute("UPDATE fl_panels SET revision=?,message_id=?,state='active',expires_at=?,page=? WHERE id=?",
                            (revision, mid, self.clock() + self.TTL, page, panel['id']))
        return mid

    async def _open(self, update, binding):
        self._bridge(binding)
        origin, (bot, owner) = self._origin(update), self._identity()
        if origin.get('message_thread_id') != binding['topic_id'] or str(origin['chat']['id']) != owner:
            raise FilesError('请在目标话题重新打开文件列表。')
        identity = secrets.token_urlsafe(15)
        with self.db:
            self.db.execute('INSERT INTO fl_panels(id,chat_id,topic_id,bot_id,binding,expires_at) VALUES(?,?,?,?,?,?)',
                            (identity, owner, binding['topic_id'], bot, encoded(binding), self.clock() + self.TTL))
        await self._render(update, self._panel(identity))
        with self.db:
            self.db.execute("UPDATE fl_panels SET state='expired' WHERE binding=? AND bot_id=? AND chat_id=? AND id<>?",
                            (encoded(binding), bot, owner, identity))

    def _button(self, update):
        callback, origin = update['callback_query'], self._origin(update)
        button = self.db.execute('SELECT * FROM fl_buttons WHERE token=?', (callback['data'][3:],)).fetchone()
        panel = self._panel(button['panel_id']) if button else None
        if (not panel or button['state'] != 'active' or button['revision'] != panel['revision']
                or panel['state'] != 'active' or panel['expires_at'] <= self.clock()
                or (panel['bot_id'], panel['chat_id']) != self._identity()
                or panel['chat_id'] != str(origin['chat']['id']) or panel['topic_id'] != origin.get('message_thread_id')
                or panel['message_id'] != origin['message_id']):
            raise FilesError('这个文件按钮已失效，请重新打开文件列表。')
        self._bridge(json_value(panel['binding'], {}))
        return button, panel

    async def _callback(self, update):
        await self._answer(update)
        data = update['callback_query']['data']
        if data.startswith('fl:open:'):
            entry = self.db.execute('SELECT * FROM fl_entries WHERE token=?', (data[8:],)).fetchone()
            if not entry or (entry['bot_id'], entry['owner']) != self._identity():
                raise FilesError('这个文件入口已失效，请在当前话题发送 /files。')
            return await self._open(update, json_value(entry['binding'], {}))
        button, panel = self._button(update)
        action, value = button['action'], json.loads(button['value'])
        if action == 'close':
            with self.db:
                self.db.execute("UPDATE fl_panels SET state='closed' WHERE id=?", (panel['id'],))
                self.db.execute("UPDATE fl_buttons SET state='expired' WHERE panel_id=?", (panel['id'],))
            await self.router.telegram.call('editMessageText', chat_id=panel['chat_id'], message_id=panel['message_id'],
                                            text='已关闭文件列表。', reply_markup={'inline_keyboard': []})
        elif action in ('page', 'refresh'):
            page = value if action == 'page' else self._current_page(panel)
            await self._render(update, panel, page)
        elif action == 'send':
            binding = json_value(panel['binding'], {})
            bridge = self._bridge(binding)
            self._record(bridge, value)
            with self.db:
                claimed = self.db.execute("UPDATE fl_buttons SET state='used' WHERE token=? AND state='active'",
                                          (button['token'],)).rowcount
                if not claimed:
                    raise FilesError('这次发送已处理，请刷新列表后再次选择。')
                self.db.execute('INSERT OR IGNORE INTO fl_sends '
                    '(id,token,panel_id,binding,bot_id,chat_id,topic_id,file_json) VALUES(?,?,?,?,?,?,?,?)',
                    ('file-send:' + button['token'], button['token'], panel['id'], panel['binding'], panel['bot_id'],
                     panel['chat_id'], panel['topic_id'], encoded(value)))
        else:
            raise FilesError('文件操作无效，请刷新列表。')

    @staticmethod
    def _current_page(panel):
        return panel['page']

    async def _process(self, row):
        update = json.loads(row['payload'])
        lock = self.job_locks.setdefault(row['target_key'], asyncio.Lock())
        async with lock:
            with self.db:
                if not self.db.execute("UPDATE fl_jobs SET state='processing' WHERE id=? AND state='pending'", (row['id'],)).rowcount:
                    return
            if not self._authorized(update) or self._identity() != (row['bot_id'], row['owner']):
                with self.db:
                    self.db.execute("UPDATE fl_jobs SET state='denied' WHERE id=?", (row['id'],))
                return
            try:
                if update.get('callback_query'):
                    await asyncio.wait_for(self._callback(update), self.TIMEOUT)
                else:
                    message = update['message']
                    binding = json_value(row['binding'], {})
                    target = self.router.by_topic.get(message.get('message_thread_id'))
                    if target is None and not binding:
                        await self._notice(update, '请在目标话题发送 /files，或点击话题中的 📎 文件。')
                    else:
                        if not target or self.binding(target) != binding:
                            raise FilesError('会话绑定已变化，请在当前话题重新打开文件列表。')
                        await asyncio.wait_for(self._open(update, binding), self.TIMEOUT)
            except Exception as error:
                state = 'failed' if isinstance(error, (FilesError, TelegramError)) else 'uncertain'
                with self.db:
                    self.db.execute('UPDATE fl_jobs SET state=?,error=? WHERE id=?', (state, type(error).__name__, row['id']))
                text = str(error) if isinstance(error, FilesError) else '文件列表暂时不可用，请重新发送 /files。'
                try:
                    await self._notice(update, text)
                except (TelegramError, ConnectionError, OSError, asyncio.TimeoutError):
                    pass
                LOG.warning('File menu operation failed: %s', type(error).__name__)
            else:
                with self.db:
                    self.db.execute("UPDATE fl_jobs SET state='done' WHERE id=?", (row['id'],))

    def _validate_send(self, row):
        if self._identity() != (row['bot_id'], row['chat_id']):
            raise FilesError('机器人身份已变化，文件未发送。')
        binding = json_value(row['binding'], {})
        bridge = self._bridge(binding)
        if binding['topic_id'] != row['topic_id'] or row['chat_id'] != str(self.router.owner):
            raise FilesError('文件接收话题已变化，文件未发送。')
        record = self._record(bridge, json_value(row['file_json'], {}))
        return bridge, record

    async def _send_notice(self, row, text):
        if row['notified']:
            return
        try:
            binding = json_value(row['binding'], {})
            self._bridge(binding)
            if self._identity() != (row['bot_id'], row['chat_id']) or binding['topic_id'] != row['topic_id']:
                return
        except FilesError:
            return
        with self.db:
            if not self.db.execute('UPDATE fl_sends SET notified=1 WHERE id=? AND notified=0', (row['id'],)).rowcount:
                return
        try:
            await self.router.telegram.call('sendMessage', chat_id=row['chat_id'], message_thread_id=row['topic_id'],
                                            text=text, disable_notification=True)
        except (TelegramError, ConnectionError, OSError, asyncio.TimeoutError):
            pass

    async def _send(self, row):
        lock = self.send_locks.setdefault(row['binding'], asyncio.Lock())
        async with lock:
            try:
                bridge, record = self._validate_send(row)
            except FilesError as error:
                with self.db:
                    self.db.execute("UPDATE fl_sends SET state='failed',error=? WHERE id=?", (type(error).__name__, row['id']))
                fresh = self.db.execute('SELECT * FROM fl_sends WHERE id=?', (row['id'],)).fetchone()
                await self._send_notice(fresh, str(error))
                return
            with self.db:
                if not self.db.execute("UPDATE fl_sends SET state='sending' WHERE id=? AND state='pending'", (row['id'],)).rowcount:
                    return
            route = {'chat_id': row['chat_id'], 'message_thread_id': row['topic_id'], 'disable_notification': True}
            try:
                if record['source'] == 'in':
                    method, field = MEDIA_METHODS[record['item']['kind']]
                    result = await asyncio.wait_for(self.router.telegram.call(method, **route,
                                                    **{field: record['item']['file_id']}), self.TIMEOUT)
                else:
                    media = dict(record['media'])
                    media['caption'] = label(media['name'], 150)
                    result = await asyncio.wait_for(send_image(self.router.telegram, encoded(media), route), 60)
                mid = result.get('message_id') if isinstance(result, dict) else None
                if (type(mid) is not int or mid <= 0
                        or isinstance(result.get('chat'), dict) and str(result['chat']['id']) != row['chat_id']
                        or result.get('message_thread_id') not in (None, row['topic_id'])):
                    raise ConnectionError('File resend acknowledgement unavailable')
            except TelegramError as error:
                if error.code == 429:
                    with self.db:
                        self.db.execute("UPDATE fl_sends SET state='pending',retry_at=?,error='Telegram:429' WHERE id=?",
                                        (self.clock() + max(1, error.retry_after), row['id']))
                    return
                state, problem = ('failed', '文件未能重发，请刷新列表后重试。') if error.code in (400, 403, 404) else (
                    'uncertain', '文件发送结果未确认，未自动重发；请先检查本话题，必要时刷新列表后手动重发。')
                failure = 'Telegram:' + str(error.code)
            except (FilesError, ImageError) as error:
                state, failure, problem = 'failed', type(error).__name__, '文件快照已不可用或发生变化，请重新生成或发送原文件。'
            except Exception as error:
                state, failure, problem = 'uncertain', type(error).__name__, '文件发送结果未确认，未自动重发；请先检查本话题，必要时刷新列表后手动重发。'
            else:
                with self.db:
                    self.db.execute("UPDATE fl_sends SET state='sent',message_id=?,error=NULL WHERE id=?", (mid, row['id']))
                return
            with self.db:
                self.db.execute('UPDATE fl_sends SET state=?,error=? WHERE id=?', (state, failure, row['id']))
            await self._send_notice(self.db.execute('SELECT * FROM fl_sends WHERE id=?', (row['id'],)).fetchone(), problem)

    async def send_one(self):
        row = self.db.execute("SELECT * FROM fl_sends WHERE state='pending' AND retry_at<=? ORDER BY rowid LIMIT 1", (self.clock(),)).fetchone()
        if row is None:
            return False
        await self._send(row)
        return True

    async def handle_one(self):
        row = self.db.execute("SELECT * FROM fl_jobs WHERE state='pending' ORDER BY id LIMIT 1").fetchone()
        if row is not None:
            await self._process(row)
        sent = await self.send_one()
        return row is not None or sent

    def recover(self):
        with self.db:
            self.db.execute("UPDATE fl_jobs SET state='uncertain',error='Restart during menu operation' WHERE state='processing'")
            self.db.execute("UPDATE fl_sends SET state='uncertain',error='Restart during file send' WHERE state='sending'")
            self.db.execute("UPDATE fl_panels SET state='uncertain' WHERE state='creating'")

    async def _notices(self):
        for row in self.db.execute("SELECT * FROM fl_sends WHERE state='uncertain' AND notified=0 ORDER BY rowid").fetchall():
            await self._send_notice(row, '文件发送结果未确认，未自动重发；请先检查本话题，必要时刷新列表后手动重发。')

    async def loop(self):
        jobs, sends = {}, {}
        try:
            while True:
                for tasks in (jobs, sends):
                    for key, task in list(tasks.items()):
                        if task.done():
                            try:
                                await task
                            except Exception as error:
                                LOG.warning('File operation unavailable: %s', type(error).__name__)
                            tasks.pop(key)
                for row in self.db.execute("SELECT * FROM fl_jobs WHERE state='pending' ORDER BY id").fetchall():
                    if row['target_key'] not in jobs:
                        jobs[row['target_key']] = asyncio.create_task(self._process(row))
                for row in self.db.execute("SELECT * FROM fl_sends WHERE state='pending' AND retry_at<=? ORDER BY rowid", (self.clock(),)).fetchall():
                    if row['binding'] not in sends:
                        sends[row['binding']] = asyncio.create_task(self._send(row))
                await self._notices()
                await asyncio.sleep(.15)
        finally:
            tasks = [*jobs.values(), *sends.values()]
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
