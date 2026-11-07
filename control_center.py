"""Owner-only local overview, actionable inbox and durable existing-thread dispatch."""
import asyncio
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import secrets
import sqlite3
import time

from bridge import RpcError, TelegramError, permitted
from message_format import message_parts
from reply_context import snapshot as reply_snapshot, serialized as reply_serialized

LOG = logging.getLogger('codex_telegram_control')
COMMANDS = [{'command': 'tasks', 'description': '本机会话与进度'},
            {'command': 'inbox', 'description': '待我处理的问题与审批'},
            {'command': 'to', 'description': '选择原会话派单，或 /to 别名 任务'},
            {'command': 'aliases', 'description': '查看可用的 @会话别名'}]


def ident(value):
    return hashlib.sha256(value.encode()).hexdigest()


def negative_id(value):
    return -(int(ident(value)[:15], 16) + 1)


def clean(value, limit=240):
    text = str(value or '')
    text = re.sub(r'\b\d{6,12}:[A-Za-z0-9_-]{30,}\b', '[凭据已隐藏]', text)
    text = re.sub(r'\b(?:sk-|ghp_|github_pat_|olp_)[A-Za-z0-9_-]{15,}', '[凭据已隐藏]', text)
    return ' '.join(text.split())[:limit]


def alias(value):
    return re.sub(r'\s+', '_', str(value).strip()).casefold()


def read_json(path, default):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return default


def write_json(path, data):
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.' + secrets.token_hex(4) + '.tmp')
    with tmp.open('x') as stream:
        os.chmod(tmp, 0o600)
        json.dump(data, stream, ensure_ascii=False)
    tmp.replace(path)


def tables(db):
    db.executescript('''
        CREATE TABLE IF NOT EXISTS cc_jobs(id INTEGER PRIMARY KEY,payload TEXT,state TEXT DEFAULT 'pending');
        CREATE TABLE IF NOT EXISTS cc_buttons(token TEXT PRIMARY KEY,chat_id TEXT,topic_id INTEGER,
            message_id INTEGER,action TEXT,data TEXT,expires REAL);
        CREATE TABLE IF NOT EXISTS cc_prompts(message_id INTEGER,chat_id TEXT,topic_id INTEGER,data TEXT,
            expires REAL,PRIMARY KEY(message_id,chat_id));
        CREATE TABLE IF NOT EXISTS cc_requests(id TEXT PRIMARY KEY,payload TEXT,state TEXT DEFAULT 'pending',
            detail TEXT,updated REAL);
        CREATE TABLE IF NOT EXISTS cc_deliveries(id TEXT PRIMARY KEY,payload TEXT,state TEXT DEFAULT 'pending',
            inbox_id INTEGER,after_rowid INTEGER,detail TEXT,updated REAL);
        CREATE TABLE IF NOT EXISTS cc_dismissals(id TEXT PRIMARY KEY,until_at REAL,updated REAL);
    ''')
    columns = {r[1] for r in db.execute('PRAGMA table_info(notices)')}
    for name in ('rich_text', 'label'):
        if name not in columns:
            db.execute('ALTER TABLE notices ADD COLUMN ' + name + ' TEXT')
    db.commit()


def connection(state):
    db = sqlite3.connect(Path(state) / 'topics.sqlite', timeout=10)
    db.row_factory = sqlite3.Row
    tables(db)
    return db


def put_notice(db, key, owner, topic, text, label=None, rich=False):
    for index, (source, rendered) in enumerate(message_parts(text, rich=rich)):
        db.execute('INSERT OR IGNORE INTO notices(id,chat_id,topic_id,text,rich_text,label) VALUES(?,?,?,?,?,?)',
                   (negative_id('cc-notice:' + key + ':' + str(index)), str(owner), topic, source, rendered, label))


def export_bundle(state):
    state = Path(state)
    config = read_json(state / 'config.json', {})
    db = connection(state)
    try:
        snapshot = read_json(state / 'control/local.json', {'host': config.get('host_id'), 'generated': 0, 'entries': []})
        requests = [dict(r) for r in db.execute("SELECT * FROM cc_requests WHERE state IN ('pending','queued','running','uncertain') OR updated>?", (time.time() - 86400,))
                    if json.loads(r['payload']).get('source_host') == config['host_id']
                    and json.loads(r['payload']).get('target_host') == config['host_id']]
        dismissals = [dict(r) for r in db.execute('SELECT * FROM cc_dismissals')]
        deliveries, relays = [], []
        targets = {t['key']: t for t in config.get('targets', [])}
        for row in db.execute("SELECT * FROM cc_deliveries WHERE state IN ('pending','importing','queued','running') OR updated>?", (time.time() - 86400,)).fetchall():
            receipt = {'id': row['id'], 'state': row['state'], 'detail': row['detail']}
            request = json.loads(row['payload'])
            if request.get('source_host') != config['host_id'] or request.get('target_host') != config['host_id']:
                continue
            receipt['source_host'] = request['source_host']
            target = targets.get(request['target_key'])
            if row['inbox_id'] is not None and target and target['thread_id'] == request['thread_id']:
                path = state / 'targets' / target['key'] / 'bridge.sqlite'
                if path.exists():
                    child = sqlite3.connect('file:' + str(path) + '?mode=ro', uri=True)
                    child.row_factory = sqlite3.Row
                    try:
                        inbox = child.execute('SELECT state,turn_id FROM inbox WHERE id=?', (row['inbox_id'],)).fetchone()
                        if inbox:
                            receipt['state'] = {'pending': 'queued', 'dispatching': 'queued', 'done': 'done',
                                                'denied': 'failed', 'failed': 'failed', 'uncertain': 'uncertain',
                                                'running': 'running'}.get(inbox['state'], 'uncertain')
                            if receipt['state'] != row['state']:
                                db.execute('UPDATE cc_deliveries SET state=?,updated=? WHERE id=?',
                                           (receipt['state'], time.time(), row['id']))
                            if inbox['turn_id']:
                                prefix = 'turn:' + inbox['turn_id'] + ':'
                                control_prefix = 'input:' + str(row['inbox_id']) + ':'
                                for output in child.execute('SELECT key,text FROM outbox WHERE rowid>? AND (substr(key,1,?)=? OR substr(key,1,?)=?) ORDER BY rowid LIMIT 500',
                                                            (row['after_rowid'] or 0, len(prefix), prefix, len(control_prefix), control_prefix)):
                                    if output['key'].startswith(prefix) or output['key'].startswith('input:' + str(row['inbox_id']) + ':'):
                                        if request['source_host'] == config['host_id'] and request.get('source_topic') == target['message_thread_id']:
                                            continue
                                        relays.append({'id': ident(row['id'] + ':' + output['key']), 'request_id': row['id'],
                                                       'source_host': request['source_host'], 'text': output['text']})
                    finally:
                        child.close()
            deliveries.append(receipt)
        db.commit()
        return {'version': 1, 'owner': str(config['owner_id']), 'snapshot': snapshot,
                'requests': requests, 'dismissals': dismissals, 'deliveries': deliveries, 'relays': relays}
    finally:
        db.close()


def receive_bundle(state, payload):
    state = Path(state)
    config = read_json(state / 'config.json', {})
    host, owner = config['host_id'], str(config['owner_id'])
    if payload.get('version') != 1 or str(payload.get('owner')) != owner:
        raise ValueError('Control owner/protocol mismatch')
    db = connection(state)
    rejected = {}
    try:
        with db:
            for request in payload.get('incoming', []):
                rid = request.get('id', '')
                if (not re.fullmatch('[0-9a-f]{64}', rid) or request.get('target_host') != host
                        or request.get('source_host') != host or str(request.get('owner')) != owner
                        or not re.fullmatch(r'[a-z0-9_-]{1,40}', str(request.get('target_key', '')))
                        or not isinstance(request.get('thread_id'), str) or not 0 < len(request['thread_id']) <= 128
                        or not isinstance(request.get('topic_id'), int) or request['topic_id'] <= 0
                        or not isinstance(request.get('target_name'), str)
                        or (request.get('source_topic') is not None and
                            (not isinstance(request['source_topic'], int) or request['source_topic'] <= 0))
                        or not isinstance(request.get('text'), str) or not 0 < len(request['text']) <= 16000):
                    rejected[rid] = '派单目标或身份不匹配'
                    continue
                saved = db.execute('SELECT payload FROM cc_deliveries WHERE id=?', (rid,)).fetchone()
                serialized = json.dumps(request, ensure_ascii=False, sort_keys=True)
                if saved and saved['payload'] != serialized:
                    rejected[rid] = '派单 ID 冲突，未重新投递'
                    continue
                db.execute('INSERT OR IGNORE INTO cc_deliveries(id,payload,updated) VALUES(?,?,?)',
                           (rid, serialized, time.time()))
            for change in payload.get('dismissals', []):
                previous = db.execute('SELECT updated FROM cc_dismissals WHERE id=?', (change['id'],)).fetchone()
                if not previous or previous['updated'] < change['updated']:
                    db.execute('INSERT OR REPLACE INTO cc_dismissals VALUES(?,?,?)',
                               (change['id'], change['until_at'], change['updated']))
            for receipt in payload.get('receipts', []):
                row = db.execute('SELECT * FROM cc_requests WHERE id=?', (receipt['id'],)).fetchone()
                if not row or receipt.get('state') not in ('pending', 'queued', 'running', 'done', 'failed', 'uncertain'):
                    continue
                request = json.loads(row['payload'])
                if request.get('source_host') != host or request.get('target_host') != host:
                    continue
                if row['state'] != receipt['state'] or row['detail'] != receipt.get('detail'):
                    db.execute('UPDATE cc_requests SET state=?,detail=?,updated=? WHERE id=?',
                               (receipt['state'], receipt.get('detail'), time.time(), receipt['id']))
                if receipt['state'] in ('failed', 'uncertain'):
                    put_notice(db, 'dispatch-error:' + receipt['id'], owner, request.get('source_topic'),
                               '派单状态：' + ('送达结果待确认，请勿重复发送。' if receipt['state'] == 'uncertain' else clean(receipt.get('detail') or '目标未接受任务。')),
                               label=request['target_host'] + '/' + request['target_name'])
            for relay in payload.get('relays', []):
                row = db.execute('SELECT payload FROM cc_requests WHERE id=?', (relay['request_id'],)).fetchone()
                if not row:
                    continue
                request = json.loads(row['payload'])
                if request.get('source_host') != host or request.get('target_host') != host:
                    continue
                put_notice(db, 'relay:' + relay['id'], owner, request.get('source_topic'), relay['text'],
                           label=request['target_host'] + '/' + request['target_name'], rich=True)
        return {'rejected': rejected}
    finally:
        db.close()


def conversation_text(turn):
    users, agents = [], []
    for item in turn.get('items', []):
        if item.get('type') == 'userMessage':
            users.append('\n'.join(c.get('text', '') for c in item.get('content', []) if c.get('type') == 'text'))
        elif item.get('type') == 'agentMessage' and '[external_agent_tool' not in item.get('text', ''):
            agents.append(item.get('text', ''))
    return users, agents


def questions_for(entry, turns):
    if not turns:
        return []
    latest = turns[0]
    flags = entry.get('status', {}).get('activeFlags', [])
    base = entry['host'] + ':' + entry['thread_id'] + ':' + latest['id']
    if any(f in flags for f in ('waitingOnApproval', 'waitingOnUserInput')):
        return [{'id': ident(base + ':native'), 'kind': 'native', 'question': '原 Codex 会话正在等待输入或审批，请到原会话处理。', 'turn_id': latest['id']}]
    _, texts = conversation_text(latest)
    if not texts or latest.get('status') not in ('completed', 'inProgress'):
        return []
    # Only the most recent turn may suggest a pending business question. Never turn
    # ordinary recommendations or old questions into a confirmed blocking request.
    text = '\n'.join(texts[-2:])
    lines = [clean(line, 400) for line in text.splitlines() if ('？' in line or '?' in line)]
    explicit = [line for line in lines if re.search(r'请确认|请提供|需要你|请告知|要不要|你想|你希望|请问', line)
                and not line.startswith(('建议', '后续建议', '下一步建议'))]
    if not explicit:
        return []
    question = '\n'.join(explicit[-2:])
    return [{'id': ident(base + ':' + question), 'kind': 'question', 'question': question, 'turn_id': latest['id']}]


class ControlCenter:
    PAGE = 6

    def __init__(self, router):
        self.router, self.db = router, router.store.db
        self.host = router.config.get('host_id', '本机')
        tables(self.db)
        self.isolate_local()

    def isolate_local(self):
        """Retire old cross-host queues without interrupting accepted native work."""
        with self.db:
            for table in ('cc_requests', 'cc_deliveries'):
                for row in self.db.execute('SELECT * FROM ' + table + " WHERE state IN ('pending','importing','queued','running','uncertain')").fetchall():
                    request = json.loads(row['payload'])
                    if request.get('source_host') == self.host and request.get('target_host') == self.host:
                        continue
                    if table == 'cc_deliveries' and row['inbox_id'] is not None:
                        bridge = self.router.bridges.get(request.get('target_key'))
                        if bridge:
                            with bridge.store.db:
                                bridge.store.db.execute("UPDATE inbox SET state='cancelled' WHERE id=? AND state='pending' AND client_id=?",
                                                        (row['inbox_id'], 'telegram-' + str(row['inbox_id'])))
                    self.db.execute('UPDATE ' + table + " SET state='cancelled',detail=?,updated=? WHERE id=?",
                                    ('已切换为本机管理，跨机派单已关闭。', time.time(), row['id']))
            for row in self.db.execute('SELECT id,label FROM notices WHERE message_id IS NULL AND label IS NOT NULL').fetchall():
                if '/' in row['label'] and row['label'].split('/', 1)[0] != self.host:
                    self.db.execute('UPDATE notices SET message_id=-1 WHERE id=?', (row['id'],))

    def ingest(self, update):
        callback, message = update.get('callback_query'), update.get('message', {})
        if callback:
            if not callback.get('data', '').startswith('cc:'):
                return False
            origin = callback.get('message', {})
            valid = (permitted({'from': callback.get('from', {}), 'chat': origin.get('chat', {})},
                               self.router.access(), self.router.owner)
                     and origin.get('date') != 0 and origin.get('message_id')
                     and (not self.router.bot_id or origin.get('from', {}).get('id') == self.router.bot_id))
        else:
            text = message.get('text', '')
            command = text.strip().split(maxsplit=1)[0].split('@')[0].lower() if text.strip() else ''
            prompt = self.db.execute('SELECT * FROM cc_prompts WHERE message_id=? AND chat_id=?',
                                     (message.get('reply_to_message', {}).get('message_id', -1), str(message.get('chat', {}).get('id')))).fetchone()
            recognized = command in ('/tasks', '/inbox', '/to', '/aliases') or text.lstrip().startswith('@') or bool(prompt)
            caption = message.get('caption', '').lstrip()
            recognized = recognized or caption.startswith('@')
            if not recognized:
                return False
            valid = permitted(message, self.router.access(), self.router.owner)
        with self.db:
            if valid:
                self.db.execute('INSERT OR IGNORE INTO cc_jobs(id,payload) VALUES(?,?)',
                                (update['update_id'], json.dumps(update, ensure_ascii=False)))
            self.db.execute("UPDATE kv SET value=? WHERE key='offset'", (str(max(self.router.store.offset(), update['update_id'] + 1)),))
        return True

    def view(self):
        local = read_json(self.router.state_dir / 'control/local.json', {'host': self.host, 'generated': 0, 'entries': []})
        nodes = {self.host: local}
        entries = []
        for node in nodes.values():
            for raw in node.get('entries', []):
                if raw.get('host') != self.host:
                    continue
                entry = dict(raw)
                entry['stale'] = time.time() - node.get('generated', 0) > 60
                entries.append(entry)
        return entries, nodes

    @staticmethod
    def selector(entry):
        return entry['aliases'][0]

    def resolve(self, selector):
        entries, _ = self.view()
        host, name = selector.split('/', 1) if '/' in selector else (None, selector)
        if host is not None and alias(host) != alias(self.host):
            raise ValueError('只支持本机会话，请切换到对应机器的 Telegram Bot。')
        matches = [e for e in entries if e.get('target_key') and (host is None or alias(e['host']) == alias(host))
                   and alias(name) in [alias(a) for a in e['aliases']]]
        if not matches:
            raise ValueError('未找到已绑定会话，发送 /aliases 查看可用别名。')
        if len(matches) > 1:
            raise ValueError('本机别名重名，请使用 /to 按钮选择具体会话。')
        return matches[0]

    async def panel(self, origin, text, buttons=(), edit=False):
        keyboard, tokens = [], []
        for row in buttons:
            rendered = []
            for label, action, data in row:
                token = secrets.token_urlsafe(12)
                with self.db:
                    self.db.execute('INSERT INTO cc_buttons VALUES(?,?,?,?,?,?,?)',
                                    (token, str(origin['chat']['id']), None, None,
                                     action, json.dumps(data, ensure_ascii=False), time.time() + 900))
                tokens.append(token)
                rendered.append({'text': label[:55], 'callback_data': 'cc:' + token})
            keyboard.append(rendered)
        params = {'chat_id': str(origin['chat']['id']), 'text': text[:4000], 'reply_markup': {'inline_keyboard': keyboard}}
        if edit and not origin.get('message_thread_id'):
            params['message_id'] = origin['message_id']
            try:
                result = await self.router.telegram.call('editMessageText', **params)
            except TelegramError as error:
                if error.code != 400 or 'message is not modified' not in str(error).lower():
                    raise
                result = origin
        else:
            result = await self.router.telegram.call('sendMessage', **params)
        with self.db:
            for token in tokens:
                self.db.execute('UPDATE cc_buttons SET message_id=? WHERE token=?', (result['message_id'], token))
        return result

    def ref(self, entry):
        return {'host': entry['host'], 'thread_id': entry['thread_id']}

    def find(self, data):
        return next((e for e in self.view()[0] if e['host'] == data['host'] and e['thread_id'] == data['thread_id']), None)

    async def overview(self, origin, kind='tasks', page=0, edit=False, mode='bound'):
        entries, nodes = self.view()
        if kind == 'inbox':
            hidden = {r['id']: r['until_at'] for r in self.db.execute('SELECT * FROM cc_dismissals')}
            entries = [dict(e, item=q) for e in entries for q in e.get('questions', [])
                       if hidden.get(q['id'], 0) < time.time()]
            entries.sort(key=lambda e: (e['stale'], e['item']['kind'] != 'native', -e.get('updated', 0)))
            heading = '待我处理 · ' + str(len(entries)) + ' 项\n“待确认”来自最新回复，需你核对；原生等待单独标记。'
        else:
            if mode == 'bound':
                entries = [e for e in entries if e.get('target_key')]
            entries.sort(key=lambda e: (e['stale'], not bool(e.get('status', {}).get('activeFlags')),
                                       e.get('status', {}).get('type') != 'active', -e.get('updated', 0)))
            heading = '本机会话 · ' + str(len(entries)) + (' 个已绑定' if mode == 'bound' else ' 个可见会话')
        page = max(0, min(page, max(0, (len(entries) - 1) // self.PAGE)))
        start = page * self.PAGE
        lines, rows = [heading], []
        for entry in entries[start:start + self.PAGE]:
            status = entry.get('status', {})
            badge = ('⚠️ 数据过期' if entry['stale'] else '🙋 等待处理' if status.get('activeFlags')
                     else '⏳ 工作中' if status.get('type') == 'active' else '✅ 上轮完成' if entry.get('last_status') == 'completed'
                     else '⏹ 已停止' if entry.get('last_status') == 'interrupted' else '❌ 失败' if entry.get('last_status') == 'failed' else '空闲')
            lines.append('\n' + entry['host'] + ' / ' + clean(entry['name'], 40) + ' · ' + badge)
            if kind == 'inbox':
                lines.append(('原生等待：' if entry['item']['kind'] == 'native' else '待确认：') + clean(entry['item']['question'], 180))
                data = {**self.ref(entry), 'item_id': entry['item']['id']}
            else:
                lines.append(clean(entry.get('latest_reply') or entry.get('latest_input') or '暂无近期内容', 130))
                data = self.ref(entry)
            rows.append([(clean(entry['name'], 30) + ' · 查看', 'detail', data)])
        if not entries:
            lines.append('\n暂无待处理事项。' if kind == 'inbox' else '\n正在同步会话，请稍后刷新。')
        rows.append([('上一页', kind, {'page': page - 1, 'mode': mode}), ('刷新', kind, {'page': page, 'mode': mode}),
                     ('下一页', kind, {'page': page + 1, 'mode': mode})])
        rows.append([('总览', 'tasks', {}), ('待我处理', 'inbox', {}), ('全部会话' if mode == 'bound' else '已绑定', 'tasks', {'mode': 'all' if mode == 'bound' else 'bound'})])
        for host in [self.host]:
            node = nodes.get(host)
            age = max(0, int(time.time() - node['generated'])) if node and node.get('generated') else None
            lines.append('\n' + host + '：' + (f'{age} 秒前同步' if age is not None else '等待同步'))
        await self.panel(origin, '\n'.join(lines), rows, edit)

    async def detail(self, origin, data, edit=False):
        entry = self.find(data)
        if not entry:
            raise ValueError('会话已不在当前目录中，请刷新 /tasks。')
        text = entry['host'] + ' / ' + entry['name'] + '\nID：' + entry['thread_id']
        text += '\n最近输入：' + clean(entry.get('latest_input'), 500)
        text += '\n最近回复：' + clean(entry.get('latest_reply'), 900)
        rows = []
        if entry.get('target_key'):
            text += '\n派单：@' + self.selector(entry) + ' 任务内容'
            rows.append([('派单 / 补充说明', 'prompt', self.ref(entry))])
        else:
            text += '\n尚未绑定话题，请到 @' + entry.get('bot_username', '') + ' 使用 /sessions 绑定原会话。'
        for item in entry.get('questions', []):
            if data.get('item_id') and data['item_id'] != item['id']:
                continue
            text += '\n\n' + ('原生等待：' if item['kind'] == 'native' else '待确认：') + item['question']
            ref = {**self.ref(entry), 'item_id': item['id']}
            if item['kind'] != 'native' and entry.get('target_key'):
                rows.append([('回复此问题', 'prompt', ref), ('已处理', 'dismiss', {**ref, 'seconds': 31536000})])
            rows.append([('稍后 1 小时', 'dismiss', {**ref, 'seconds': 3600})])
        rows.append([('返回总览', 'tasks', {}), ('待我处理', 'inbox', {})])
        await self.panel(origin, text, rows, edit)

    async def prompt(self, origin, data):
        entry = self.find(data)
        if not entry or not entry.get('target_key'):
            raise ValueError('请先绑定目标原会话。')
        params = {'chat_id': str(origin['chat']['id']), 'text': '请回复这条消息，给 ' + entry['host'] + '/' + entry['name'] + ' 派单或补充说明。',
                  'reply_markup': {'force_reply': True, 'selective': True}}
        result = await self.router.telegram.call('sendMessage', **params)
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO cc_prompts VALUES(?,?,?,?,?)',
                            (result['message_id'], str(origin['chat']['id']), None,
                             json.dumps({**data, 'target_key': entry['target_key'], 'topic_id': entry['topic_id']}, ensure_ascii=False), time.time() + 900))

    async def dispatch(self, origin, update_id, entry, text, question=None, reply_context=None):
        if entry.get('host') != self.host:
            raise ValueError('只支持本机会话，请切换到对应机器的 Telegram Bot。')
        if not text.strip():
            raise ValueError('请提供任务正文，例如 @论文 检查最新批注。')
        if entry.get('topic_missing') or not entry.get('target_key'):
            raise ValueError('目标话题不可用，请先在目标 Bot 用 /sessions 绑定或重建。')
        if question:
            text = '针对待确认事项：' + question + '\n我的回复：' + text
        rid = ident(self.host + ':' + str(self.router.bot_id) + ':' + str(update_id))
        request = {'id': rid, 'owner': self.router.owner, 'source_host': self.host,
                   'source_topic': None, 'target_host': entry['host'],
                   'target_key': entry['target_key'], 'target_name': entry['name'], 'thread_id': entry['thread_id'],
                   'topic_id': entry['topic_id'], 'text': text, 'created': time.time()}
        if reply_context:
            request['reply_context'] = reply_context
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO cc_requests(id,payload,updated) VALUES(?,?,?)',
                            (rid, json.dumps(request, ensure_ascii=False, sort_keys=True), time.time()))
        await self.panel(origin, '已入派单队列 → ' + entry['host'] + '/' + entry['name'] +
                         '\n保持原会话；派单回执和回复汇总发送到主对话。',
                         [[('查看目标', 'detail', self.ref(entry)), ('本机状态', 'tasks', {})]])

    async def handle_one(self):
        row = self.db.execute("SELECT * FROM cc_jobs WHERE state='pending' ORDER BY id LIMIT 1").fetchone()
        if not row:
            return False
        update = json.loads(row['payload'])
        callback = update.get('callback_query')
        origin = callback['message'] if callback else update['message']
        with self.db:
            self.db.execute("UPDATE cc_jobs SET state='processing' WHERE id=?", (row['id'],))
        if not self.router.owner_allowed():
            with self.db:
                self.db.execute("UPDATE cc_jobs SET state='denied' WHERE id=?", (row['id'],))
            return True
        try:
            if callback:
                try:
                    await self.router.telegram.call('answerCallbackQuery', callback_query_id=callback['id'])
                except (TelegramError, ConnectionError):
                    pass
                button = self.db.execute('SELECT * FROM cc_buttons WHERE token=?', (callback['data'][3:],)).fetchone()
                if (not button or button['expires'] < time.time() or button['chat_id'] != str(origin['chat']['id'])
                        or button['message_id'] != origin['message_id'] or button['topic_id'] != (origin.get('message_thread_id') or None)):
                    raise ValueError('按钮已失效，请重新发送 /tasks 或 /inbox。')
                action, data = button['action'], json.loads(button['data'])
                if action in ('tasks', 'inbox'):
                    await self.overview(origin, action, data.get('page', 0), True, data.get('mode', 'bound'))
                elif action == 'detail':
                    await self.detail(origin, data, True)
                elif action == 'prompt':
                    await self.prompt(origin, data)
                elif action == 'dismiss':
                    entry = self.find(data)
                    item = next((q for q in (entry or {}).get('questions', []) if q['id'] == data['item_id']), None)
                    if not item:
                        raise ValueError('事项已变化，请刷新 Inbox。')
                    seconds = min(3600, data['seconds']) if item['kind'] == 'native' else data['seconds']
                    with self.db:
                        self.db.execute('INSERT OR REPLACE INTO cc_dismissals VALUES(?,?,?)',
                                        (item['id'], time.time() + seconds, time.time()))
                    await self.overview(origin, 'inbox', edit=True)
            else:
                if any(k in origin for k in ('photo', 'document', 'voice', 'video', 'audio', 'animation')):
                    raise ValueError('派单目前支持文字；附件请进入目标 Bot 的原话题发送。')
                text = origin.get('text', '').strip()
                prompt = self.db.execute('SELECT * FROM cc_prompts WHERE message_id=? AND chat_id=?',
                                         (origin.get('reply_to_message', {}).get('message_id', -1), str(origin['chat']['id']))).fetchone()
                if prompt:
                    if prompt['expires'] < time.time() or prompt['topic_id'] != (origin.get('message_thread_id') or None):
                        raise ValueError('派单回复已过期，请重新选择会话。')
                    data = json.loads(prompt['data']);entry = self.find(data)
                    if not entry or entry.get('target_key') != data['target_key'] or entry.get('topic_id') != data['topic_id']:
                        raise ValueError('目标绑定已变化，请重新选择。')
                    q = next((q['question'] for q in entry.get('questions', []) if q['id'] == data.get('item_id')), None)
                    if data.get('item_id') and q is None:
                        raise ValueError('问题已变化或已解决，请刷新 /inbox 后再回复。')
                    await self.dispatch(origin, row['id'], entry, text, q)
                elif text.startswith('@'):
                    parts = text[1:].split(maxsplit=1)
                    selector, body = parts[0], parts[1] if len(parts) > 1 else ''
                    await self.dispatch(origin, row['id'], self.resolve(selector), body, reply_context=reply_snapshot(origin))
                else:
                    pieces = text.split(maxsplit=2);command = pieces[0].split('@')[0].lower()
                    if command in ('/tasks', '/inbox'):
                        await self.overview(origin, command[1:], mode='all' if len(pieces) > 1 and pieces[1] == 'all' else 'bound')
                    elif command == '/aliases':
                        entries = [e for e in self.view()[0] if e.get('target_key')]
                        await self.panel(origin, '本机派单别名（重名时用 /to 按钮选择）：\n' + '\n'.join('@' + self.selector(e) + ' → ' + e['name'] for e in entries))
                    elif command == '/to' and len(pieces) == 3:
                        await self.dispatch(origin, row['id'], self.resolve(pieces[1]), pieces[2], reply_context=reply_snapshot(origin))
                    else:
                        entries = self.router.recent.ordered([e for e in self.view()[0] if e.get('target_key')])
                        await self.panel(origin, '按最近访问排序。选择目标，再回复弹出的派单消息；也可发送 @别名 任务内容。',
                                         [[(e['host'] + '/' + clean(e['name'], 30), 'prompt', self.ref(e))] for e in entries[:30]])
            state = 'done'
        except (ValueError, RpcError, TelegramError, ConnectionError, OSError, asyncio.TimeoutError) as error:
            state = 'failed'
            with self.db:
                put_notice(self.db, 'control-error:' + str(row['id']), self.router.owner,
                           None,
                           str(error) if isinstance(error, ValueError) else '暂时无法完成，请稍后重试 /tasks、/inbox 或 /to。')
        with self.db:
            self.db.execute('UPDATE cc_jobs SET state=? WHERE id=?', (state, row['id']))
        return True

    async def jobs_loop(self):
        with self.db:
            self.db.execute("UPDATE cc_jobs SET state='pending' WHERE state='processing'")
        while True:
            if not await self.handle_one():
                await asyncio.sleep(0.2)

    def deliver_one(self):
        row = self.db.execute("SELECT * FROM cc_deliveries WHERE state IN ('pending','importing') ORDER BY updated,rowid LIMIT 1").fetchone()
        if not row:
            return False
        request = json.loads(row['payload'])
        target = self.router.targets.get(request['target_key'])
        if (request.get('source_host') != self.host or request.get('target_host') != self.host
                or not self.router.owner_allowed() or not target or target['thread_id'] != request['thread_id']
                or target['message_thread_id'] != request['topic_id'] or target.get('topic_missing')):
            with self.db:
                self.db.execute("UPDATE cc_deliveries SET state='failed',detail=?,updated=? WHERE id=?",
                                ('目标绑定或访问权限已变化，未投递。', time.time(), row['id']))
            return True
        store = self.router.bridges[target['key']].store
        inbox_id = row['inbox_id'] if row['inbox_id'] is not None else negative_id('dispatch:' + row['id'])
        after = row['after_rowid'] if row['after_rowid'] is not None else store.db.execute('SELECT COALESCE(MAX(rowid),0) FROM outbox').fetchone()[0]
        with self.db:
            self.db.execute("UPDATE cc_deliveries SET state='importing',inbox_id=?,after_rowid=?,updated=? WHERE id=?",
                            (inbox_id, after, time.time(), row['id']))
        existing = store.db.execute('SELECT text,message_thread_id,reply_json FROM inbox WHERE id=?', (inbox_id,)).fetchone()
        expected_context = reply_serialized({}, request.get('reply_context'))
        if existing and tuple(existing) != (request['text'], target['message_thread_id'], expected_context):
            with self.db:
                self.db.execute("UPDATE cc_deliveries SET state='failed',detail=? WHERE id=?", ('派单标识冲突，未覆盖旧任务。', row['id']))
            return True
        message = {'from': {'id': int(self.router.owner)}, 'chat': {'id': int(self.router.owner), 'type': 'private'},
                   'message_thread_id': target['message_thread_id'], 'text': request['text']}
        store.accept({'update_id': inbox_id}, message, reply_context=request.get('reply_context'))
        self.router.recent.record(target['thread_id'], 'dispatch:' + row['id'],
                                  accessed_at=request.get('created') or row['updated'])
        with self.db:
            self.db.execute("UPDATE cc_deliveries SET state='queued',updated=? WHERE id=?", (time.time(), row['id']))
        return True

    async def delivery_loop(self):
        while True:
            self.deliver_one()
            await asyncio.sleep(0.2)

    async def refresh(self):
        previous = read_json(self.router.state_dir / 'control/local.json', {})
        cache = {e['thread_id']: e for e in previous.get('entries', [])}
        threads, cursor = [], None
        for _ in range(5):
            params = {'limit': 100, 'sortKey': 'updated_at', 'sortDirection': 'desc', 'archived': False}
            if cursor:
                params['cursor'] = cursor
            result = await self.router.catalog_call('thread/list', params)
            threads.extend(result.get('data', []));cursor = result.get('nextCursor')
            if not cursor:
                break
        bound = {t['thread_id']: t for t in self.router.targets.values()}
        entries = []
        for thread in threads:
            if thread.get('ephemeral') or thread.get('canAcceptDirectInput') is False:
                continue
            target = bound.get(thread['id'], {})
            old = cache.get(thread['id'], {})
            entry = {**old, 'host': self.host, 'thread_id': thread['id'], 'name': clean(thread.get('name') or target.get('name') or thread.get('preview') or '未命名', 50),
                     'status': thread.get('status', {}), 'updated': thread.get('updatedAt', 0),
                     'target_key': target.get('key'), 'topic_id': target.get('message_thread_id'),
                     'topic_missing': bool(target.get('topic_missing')), 'bot_username': self.router.username or self.router.config.get('expected_bot_username', '')}
            configured = target.get('aliases', [])
            entry['aliases'] = list(dict.fromkeys([*(alias(a) for a in configured), alias(entry['name']), target.get('key', thread['id'])]))
            active = entry['status'].get('type') == 'active'
            if not old or old.get('updated') != entry['updated'] or (active and time.time() - old.get('details_at', 0) > 15):
                try:
                    history = await self.router.catalog_call('thread/turns/list', {'threadId': thread['id'], 'limit': 2, 'sortDirection': 'desc', 'itemsView': 'full'})
                    turns = history.get('data', [])
                    entry['last_status'] = turns[0].get('status') if turns else None
                    users, agents = conversation_text(turns[0]) if turns else ([], [])
                    entry['latest_input'] = clean(users[-1] if users else '', 600)
                    entry['latest_reply'] = clean(agents[-1] if agents else '', 1200)
                    entry['questions'] = questions_for(entry, turns)
                    entry['details_at'] = time.time()
                except (RpcError, ConnectionError, asyncio.TimeoutError):
                    entry['detail_error'] = True
            elif entry['status'].get('activeFlags'):
                entry['questions'] = [{'id': ident(self.host + ':' + thread['id'] + ':waiting'), 'kind': 'native',
                                       'question': '原 Codex 会话正在等待输入或审批，请到原会话处理。'}]
            elif any(q.get('kind') == 'native' for q in entry.get('questions', [])):
                entry['questions'] = []
            entries.append(entry)
        write_json(self.router.state_dir / 'control/local.json', {'host': self.host, 'generated': time.time(), 'entries': entries})

    async def refresh_loop(self):
        while True:
            try:
                await self.refresh()
            except (RpcError, ConnectionError, OSError, ValueError, asyncio.TimeoutError):
                LOG.warning('Control snapshot temporarily unavailable')
            await asyncio.sleep(15)

    async def synchronize(self):
        bundle = export_bundle(self.router.state_dir)
        requests = [json.loads(row['payload']) for row in bundle['requests']
                    if row['state'] in ('pending', 'queued', 'running', 'uncertain')]
        requests.sort(key=lambda r: (r.get('created', 0), r['id']))
        result = receive_bundle(self.router.state_dir, {
            'version': 1, 'owner': self.router.owner, 'incoming': requests,
            'receipts': bundle['deliveries'], 'relays': bundle['relays']})
        if result['rejected']:
            receive_bundle(self.router.state_dir, {
                'version': 1, 'owner': self.router.owner,
                'receipts': [{'id': rid, 'state': 'failed', 'detail': reason}
                             for rid, reason in result['rejected'].items()]})

    async def sync_loop(self):
        while True:
            try:
                await self.synchronize()
            except (OSError, ValueError, sqlite3.Error):
                LOG.warning('Local control queue temporarily unavailable')
            await asyncio.sleep(1)
