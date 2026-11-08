"""Buffer task output, preview it as one draft, and notify after a full scan.

The caller owns native history reads: ``terminal`` only records an intent;
``seal`` is proof that the complete terminal history has been collected.
"""
import asyncio
import json
import secrets
import time

from message_format import utf16_size
from completion_batch import CompletionBatches


TERMINAL = ('completed', 'failed', 'interrupted')


class CompletionDelivery:
    KEY = 'completion_delivery_v1'
    RECEIPT_KEY = 'completion_receipt_v1'
    INTERVAL = 15
    LIMIT = 3800
    RECEIPT_GAP = 1

    def __init__(self, bridge, clock=time.time):
        self.bridge, self.store, self.db, self.clock = bridge, bridge.store, bridge.store.db, clock
        self.preview_lock = asyncio.Lock()
        self._preview_status = None
        self._preview_status_turn_id = None
        self.enabled = bridge.target.get('notification_mode', bridge.config.get('notification_mode', 'all')) == 'completion'
        self.preview_style = bridge.target.get('preview_style', bridge.config.get('preview_style', 'plain'))
        if self.preview_style not in ('plain', 'block'):
            raise ValueError('preview_style must be plain or block')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS completion_turns(
                turn_id TEXT,chat_id TEXT,topic_id INTEGER,native_id TEXT,
                stage TEXT,status TEXT,started_at REAL,notify INTEGER DEFAULT 0,
                notice_state TEXT DEFAULT 'none',notice_key TEXT,
                draft_id INTEGER,next_preview REAL DEFAULT 0,
                PRIMARY KEY(turn_id,chat_id,topic_id));
            CREATE TABLE IF NOT EXISTS completion_rows(
                key TEXT PRIMARY KEY,turn_id TEXT,chat_id TEXT,topic_id INTEGER);
            CREATE INDEX IF NOT EXISTS completion_receipt_inbox_route
                ON inbox(chat_id,message_thread_id,received_ms DESC,id DESC);
        ''')
        self.batches = CompletionBatches(self)
        state = self.state()
        if self.enabled:
            if state.get('route') != self.route() or state.get('enabled') is not True:
                self._new_window()
            else:
                # A supported renderer may work even if the previous one did
                # not. Retain the task boundary and all completion ledgers.
                state = self.state()
                if state.get('preview_style', 'plain') != self.preview_style:
                    state['preview_style'] = self.preview_style
                    state.pop('draft_unsupported', None)
                    self.store.set(self.KEY, json.dumps(state))
        elif state:
            state['enabled'] = False
            self.store.set(self.KEY, json.dumps(state))

    def route(self):
        return [self.bridge.thread_id, str(self.bridge.owner), int(self.bridge.target['message_thread_id'])]

    def state(self):
        return json.loads(self.store.get(self.KEY, '{}'))

    def _new_window(self):
        # Re-enabling must not announce turns completed while this policy was
        # off. Actual active turns are re-adopted by watch(inProgress); their
        # buffered content and exact destination remain untouched.
        with self.db:
            self.batches.suppress_notices()
            self.db.execute("UPDATE outbox SET message_id=-1 WHERE message_id IS NULL AND key IN "
                            "(SELECT notice_key FROM completion_turns WHERE notice_state IN ('queued','sending'))")
            self.db.execute("UPDATE completion_turns SET notify=0,notice_state=CASE "
                            "WHEN notice_state='queued' THEN 'suppressed' WHEN notice_state='sending' THEN 'uncertain' "
                            "ELSE notice_state END")
            self.db.execute('INSERT OR REPLACE INTO kv VALUES(?,?)',
                            (self.KEY, json.dumps({'route': self.route(), 'since': self.clock(), 'enabled': True,
                                                   'preview_style': self.preview_style})))
            self.db.execute('INSERT OR REPLACE INTO kv VALUES(?,?)', (self.RECEIPT_KEY, '{}'))

    def _current(self, row):
        return (not self.bridge.target.get('topic_missing')
                and self.bridge.target.get('thread_id') == self.bridge.thread_id
                and [row['native_id'], row['chat_id'], row['topic_id']] == self.route())

    def _group(self, turn_id, chat_id=None, topic_id=None):
        return self.db.execute('SELECT * FROM completion_turns WHERE turn_id=? AND chat_id=? AND topic_id=?',
                               (turn_id, str(chat_id or self.bridge.owner),
                                self.bridge.target['message_thread_id'] if topic_id is None else topic_id)).fetchone()

    def _create(self, turn_id, chat_id, topic_id, *, status=None, started=None, notify=False):
        draft_id = secrets.randbelow(2 ** 31 - 1) + 1
        receipt = self._receipt_state()
        if receipt.get('route') == self.route() and str(chat_id) == str(self.bridge.owner) and topic_id == self.bridge.target['message_thread_id']:
            source = self.db.execute('SELECT * FROM inbox WHERE id=?', (receipt.get('source_id'),)).fetchone()
            if source and source['turn_id'] == turn_id and self._valid_receipt_source(source):
                draft_id = receipt['draft_id']
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO completion_turns '
                            '(turn_id,chat_id,topic_id,native_id,stage,status,started_at,notify,draft_id) '
                            'VALUES(?,?,?,?,?,?,?,?,?)',
                            (turn_id, str(chat_id), topic_id, self.bridge.thread_id,
                             'closing' if status in TERMINAL else 'active', status, started if started is not None else self.clock(),
                             int(notify), draft_id))
        return self._group(turn_id, chat_id, topic_id)

    def _watch(self, turn, live=False):
        if not self.enabled or not turn.get('id') or turn.get('status') not in ('inProgress', *TERMINAL):
            return
        if self.bridge.target.get('thread_id') != self.bridge.thread_id or self.bridge.target.get('topic_missing'):
            return
        if self.state().get('route') != self.route():
            self._new_window()
        tid, status = turn['id'], turn['status']
        group = self._group(tid)
        if group is None and self.db.execute('SELECT 1 FROM completion_turns WHERE turn_id=? LIMIT 1', (tid,)).fetchone():
            return  # A repaired topic must not claim an already-frozen task.
        started = turn.get('startedAt')
        started = started if isinstance(started, (int, float)) else None
        fresh = started is not None and started >= self.state().get('since', self.clock())
        notify = live or status == 'inProgress' or fresh
        if group is None:
            # An old completed turn with no queued content is historical baseline.
            pending = self.db.execute("SELECT 1 FROM outbox WHERE message_id IS NULL AND "
                                      "(substr(key,1,?)=? OR substr(key,1,?)=?) LIMIT 1",
                                      (len('turn:' + tid + ':'), 'turn:' + tid + ':',
                                       len('native-user:' + tid + ':'), 'native-user:' + tid + ':')).fetchone()
            if not notify and not pending:
                return
            group = self._create(tid, self.bridge.owner, self.bridge.target['message_thread_id'],
                                 status=status, started=started, notify=notify)
        if group['status'] in TERMINAL and status == 'inProgress':
            return  # A delayed active snapshot cannot reopen a terminal turn.
        if status == 'inProgress' and self._preview_status_turn_id != tid:
            self._preview_status = None
            self._preview_status_turn_id = tid
        with self.db:
            self.db.execute('UPDATE completion_turns SET status=?,notify=MAX(notify,?),'
                            "stage=CASE WHEN stage IN ('active','closing') AND ? IN ('completed','failed','interrupted') "
                            "THEN 'closing' ELSE stage END WHERE turn_id=? AND chat_id=? AND topic_id=?",
                            (status, int(notify), status, tid, group['chat_id'], group['topic_id']))

    def watch(self, turn):
        self._watch(turn)

    def terminal(self, turn):
        if turn.get('status') in TERMINAL:
            self._watch(turn, live=True)

    def needs_scan(self, turn_id):
        if not self.enabled:
            return False
        group = self._group(turn_id)
        return bool(group and self._current(group) and group['stage'] in ('active', 'closing') and group['status'] in TERMINAL)

    def collect_output(self, turn_id):
        """Historical baseline may drain saved rows but must not import history."""
        group = self._group(turn_id)
        return bool(self.enabled and group and self._current(group) and group['notify'])

    def seal(self, turn):
        """Release only a known terminal turn after its full native item scan."""
        if not self.enabled or turn.get('status') not in TERMINAL:
            return False
        self.watch(turn)
        with self.db:
            result = self.db.execute("UPDATE completion_turns SET stage='ready',status=? WHERE turn_id=? "
                                     "AND native_id=? AND chat_id=? AND topic_id=? AND stage IN ('active','closing')",
                                     (turn['status'], turn['id'], self.bridge.thread_id, str(self.bridge.owner), self.bridge.target['message_thread_id']))
        return bool(result.rowcount)

    @staticmethod
    def _task_key(key):
        if key.startswith('image-error:'):
            key = key[len('image-error:'):]
        return key.startswith(('turn:', 'native-user:', 'main-guard-input:', 'request:'))

    def _associate(self, row):
        """Return a frozen group, False for unassigned input, or None for controls."""
        packet_group = self.batches.group(row)
        if packet_group is not None:
            return packet_group
        saved = self.db.execute('SELECT * FROM completion_rows WHERE key=?', (row['key'],)).fetchone()
        if saved:
            return self._group(saved['turn_id'], saved['chat_id'], saved['topic_id'])
        key = row['key']
        while key.startswith('image-error:'):
            key = key[len('image-error:'):]
        tid = None
        if key.startswith(('turn:', 'native-user:')):
            tid = key.split(':', 2)[1]
        elif key.startswith('main-guard-input:'):
            parts = key.split(':')
            try:
                source = self.db.execute('SELECT turn_id,state FROM inbox WHERE id=?', (int(parts[2]),)).fetchone()
            except (ValueError, IndexError):
                return False
            if source and source['turn_id']:
                tid = source['turn_id']
            elif source and source['state'] in ('failed', 'denied', 'blocked', 'cancelled'):
                return None  # A rejected native input has no running task to wait for.
            else:
                return False
        elif key.startswith('request:'):
            source = self.db.execute("SELECT turn_id FROM inbox WHERE chat_id=? AND message_thread_id=? "
                                     "AND state='running' AND turn_id IS NOT NULL ORDER BY received_ms DESC LIMIT 1",
                                     (row['chat_id'], row['message_thread_id'])).fetchone()
            if source:
                tid = source['turn_id']
            else:
                source = self.db.execute("SELECT turn_id FROM completion_turns WHERE chat_id=? AND topic_id=? "
                                         "AND stage IN ('active','closing') ORDER BY started_at DESC LIMIT 1",
                                         (row['chat_id'], row['message_thread_id'])).fetchone()
                tid = source['turn_id'] if source else None
        if not tid:
            return None
        group = self._group(tid, row['chat_id'], row['message_thread_id'])
        if group is None:
            if self.db.execute('SELECT 1 FROM completion_turns WHERE turn_id=? LIMIT 1', (tid,)).fetchone():
                with self.db:
                    self.db.execute('UPDATE outbox SET message_id=-1 WHERE key=?', (row['key'],))
                return False
            group = self._create(tid, row['chat_id'], row['message_thread_id'])
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO completion_rows VALUES(?,?,?,?)',
                            (row['key'], tid, row['chat_id'], row['message_thread_id']))
        return group

    def _sync_rows(self):
        # Retry unassigned All inputs after their native turn has been resolved.
        rows = self.db.execute('SELECT o.* FROM outbox o LEFT JOIN completion_rows c ON c.key=o.key '
                               'WHERE c.key IS NULL AND o.message_id IS NULL ORDER BY o.rowid').fetchall()
        for row in rows:
            if self._task_key(row['key']):
                self._associate(row)

    def _pending(self, group):
        return self.batches.pending(group) or bool(self.db.execute('SELECT 1 FROM outbox o JOIN completion_rows c ON c.key=o.key '
                                    'WHERE c.turn_id=? AND c.chat_id=? AND c.topic_id=? AND o.message_id IS NULL LIMIT 1',
                                    (group['turn_id'], group['chat_id'], group['topic_id'])).fetchone())

    def _notices(self):
        self.batches.prepare()

    def next_row(self):
        if not self.enabled:
            return self.db.execute('SELECT * FROM outbox WHERE message_id IS NULL ORDER BY rowid LIMIT 1').fetchone()
        self._sync_rows()
        self._notices()
        blocked = set()
        for row in self.db.execute('SELECT * FROM outbox WHERE message_id IS NULL ORDER BY rowid').fetchall():
            notice = self.db.execute('SELECT * FROM completion_turns WHERE notice_key=?', (row['key'],)).fetchone()
            if notice:
                # Late terminal items and image-error notices must go first even
                # if the completion notification was already durably queued.
                if (notice['notice_state'] == 'queued' and self._current(notice)
                        and not self._pending(notice) and row['retry_at'] <= self.clock()):
                    return row
                if not self._current(notice):
                    with self.db:
                        self.db.execute('UPDATE outbox SET message_id=-1 WHERE key=?', (row['key'],))
                continue
            group = self._associate(row)
            if group is False:
                continue
            if group is not None:
                if not self._current(group):
                    with self.db:
                        self.db.execute('UPDATE outbox SET message_id=-1 WHERE key=?', (row['key'],))
                    continue
                if group['stage'] != 'ready':
                    continue
                if group['notify']:
                    packet = self.batches.group(row)
                    if packet is None and row['media_json'] is None:
                        continue  # Canonical text is delivered by its aggregate.
                    if packet is not None and self.batches.media_pending(group):
                        continue
            order = ((group['turn_id'], group['chat_id'], group['topic_id']) if group is not None
                     else (row['key'].rsplit(':', 1)[0], row['chat_id'], row['message_thread_id']))
            if order in blocked:
                continue
            if row['retry_at'] > self.clock():
                blocked.add(order)
                continue
            return row
        return None

    def silent(self, row):
        return self.enabled and (self._task_key(row['key']) or row['key'].startswith('completion-part:'))

    def begin(self, row):
        if self.enabled:
            self.batches.begin(row)
            with self.db:
                self.db.execute("UPDATE completion_turns SET notice_state='sending' WHERE notice_key=? AND notice_state='queued'", (row['key'],))

    def sent(self, row, message_id):
        if self.enabled:
            self.batches.sent(row)
            with self.db:
                self.db.execute("UPDATE completion_turns SET notice_state='sent' WHERE notice_key=?", (row['key'],))

    def failed(self, row, error):
        """Handle completion-notice errors; True bypasses the sender's fallback."""
        if not self.enabled:
            return False
        if self.batches.failed(row, error):
            return True
        group = self.db.execute('SELECT * FROM completion_turns WHERE notice_key=?', (row['key'],)).fetchone()
        if group is None:
            return False
        code = getattr(error, 'code', None)
        with self.db:
            if code == 429:
                self.db.execute("UPDATE completion_turns SET notice_state='queued' WHERE notice_key=?", (row['key'],))
                self.db.execute('UPDATE outbox SET attempts=attempts+1,retry_at=? WHERE key=?',
                                (self.clock() + max(3, getattr(error, 'retry_after', 3)), row['key']))
            else:
                self.db.execute('UPDATE completion_turns SET notice_state=? WHERE notice_key=?',
                                ('failed' if code in (400, 403) else 'uncertain', row['key']))
                self.db.execute('UPDATE outbox SET message_id=-1 WHERE key=?', (row['key'],))
        return True

    def recover(self):
        if self.enabled:
            self.batches.recover()
            with self.db:
                self.db.execute("UPDATE completion_turns SET notice_state='sent' WHERE notice_state='sending' AND notice_key IN "
                                '(SELECT key FROM outbox WHERE message_id>0)')
                self.db.execute("UPDATE outbox SET message_id=-1 WHERE message_id IS NULL AND key IN "
                                "(SELECT notice_key FROM completion_turns WHERE notice_state='sending')")
                self.db.execute("UPDATE completion_turns SET notice_state='uncertain' WHERE notice_state='sending'")

    def _body(self, group):
        parts, previous = [], None
        for row in self.db.execute('SELECT o.* FROM outbox o JOIN completion_rows c ON c.key=o.key '
                                   'WHERE c.turn_id=? AND c.chat_id=? AND c.topic_id=? ORDER BY o.rowid',
                                   (group['turn_id'], group['chat_id'], group['topic_id'])):
            base = row['key'].rsplit(':', 1)[0]
            text = row['text']
            if row['media_json']:
                media = json.loads(row['media_json'])
                text = '［图片：' + (media.get('caption') or media.get('name') or '图片') + '，将在完成后发送］'
            if text:
                parts.append(('\n\n' if parts and base != previous else '') + text)
                previous = base
        return ''.join(parts)

    @staticmethod
    def _tail(text, budget):
        total, result = 0, []
        for char in reversed(text):
            width = 2 if ord(char) > 0xFFFF else 1
            if total + width > budget:
                break
            result.append(char)
            total += width
        return ''.join(reversed(result))

    def _receipt_state(self):
        return json.loads(self.store.get(self.RECEIPT_KEY, '{}'))

    def _clear_receipt(self):
        if self.store.get(self.RECEIPT_KEY, '{}') != '{}':
            self.store.set(self.RECEIPT_KEY, '{}')

    def _valid_receipt_source(self, row):
        if (not self.bridge.owner_allowed() or self.bridge.target.get('topic_missing')
                or self.bridge.target.get('thread_id') != self.bridge.thread_id
                or str(row['chat_id']) != str(self.bridge.owner)
                or row['message_thread_id'] != self.bridge.target['message_thread_id']):
            return False
        try:
            media = json.loads(row['message_json'] or '{}')
            guard = json.loads(row['main_guard_json']) if row['main_guard_json'] else None
        except (TypeError, ValueError):
            return False
        if not isinstance(media, dict) or (guard is not None and not isinstance(guard, dict)):
            return False
        text = row['text'] or ''
        if not media and (not text.strip() or text.lstrip().startswith('/')):
            return False
        if guard is not None and (guard.get('target_key') != self.bridge.target['key']
                      or guard.get('thread_id') != self.bridge.thread_id
                      or guard.get('topic_id') != self.bridge.target['message_thread_id']):
            return False
        return True

    def _latest_receipt_source(self):
        # Include terminal rows when selecting the latest task. A completed or
        # failed newest input must not resurrect an older receipt after restart.
        for row in self.db.execute('SELECT * FROM inbox WHERE chat_id=? AND message_thread_id=? '
                                   'ORDER BY received_ms DESC,id DESC',
                                   (str(self.bridge.owner), self.bridge.target['message_thread_id'])):
            if self._valid_receipt_source(row):
                return row
        return None

    def _unresolved_receipt_source(self):
        receipt = self._receipt_state()
        if receipt.get('route') != self.route():
            return None
        source = self._latest_receipt_source()
        if (source is None or source['id'] != receipt.get('source_id')
                or source['state'] not in ('pending', 'dispatching', 'uncertain', 'running')):
            return None
        return source

    def reschedule_receipt(self):
        """Make a pending next-task preview due after a normal message clears it.

        Callers already hold preview_lock while confirming delivery; this only
        adjusts display timers and never changes input or completion identity.
        """
        if not self.enabled:
            return
        receipt = self._receipt_state()
        if receipt.get('route') == self.route():
            receipt.update(next_preview=0, attempted_at=0)
            self.store.set(self.RECEIPT_KEY, json.dumps(receipt))
        with self.db:
            self.db.execute("UPDATE completion_turns SET next_preview=0 WHERE native_id=? AND chat_id=? AND topic_id=? "
                            "AND stage IN ('active','closing')", self.route())

    def _preview_text(self, heading, started_at, body):
        elapsed = max(0, int(self.clock() - started_at))
        minutes, seconds = divmod(elapsed, 60)
        duration = f'{minutes}分{seconds}秒' if minutes else f'{seconds}秒'
        header = '━━━━ ' + heading + ' ━━━━\n' + duration + ' · 更新 ' + time.strftime('%H:%M:%S', time.localtime(self.clock()))
        text = header + ('\n\n' + body if body else '')
        if utf16_size(text) > self.LIMIT:
            note = '\n\n（过程较长，预览仅显示最近部分；完整过程将在完成后发送。）\n\n'
            text = header + note + self._tail(body, self.LIMIT - utf16_size(header + note))
        return text

    async def _send_preview(self, telegram, identity, text):
        if not self.bridge.owner_allowed() or not self._current(identity):
            return False
        try:
            route = {'chat_id': int(identity['chat_id']), 'message_thread_id': identity['topic_id'],
                     'draft_id': identity['draft_id']}
            if self.preview_style == 'block':
                status, _, body = text.partition('\n\n')
                blocks = [{'type': 'thinking', 'text': status}]
                if body:
                    blocks.append({'type': 'thinking', 'text': body})
                result = await telegram.call('sendRichMessageDraft', **route,
                                             rich_message={'blocks': blocks})
            else:
                result = await telegram.call('sendMessageDraft', **route, text=text)
            if result is not True:
                raise ConnectionError('Draft acknowledgement unavailable')
            return bool(self.bridge.owner_allowed() and self._current(identity))
        except Exception as error:
            if not self.bridge.owner_allowed() or not self._current(identity):
                return False  # An old response must not disable a repaired topic.
            if getattr(error, 'code', None) in (400, 404):
                state = self.state()
                state['draft_unsupported'] = True
                self.store.set(self.KEY, json.dumps(state))
            elif getattr(error, 'code', None) == 429:
                retry_at = self.clock() + max(self.INTERVAL, getattr(error, 'retry_after', 0))
                state = self.state()
                state['draft_retry_at'] = retry_at
                self.store.set(self.KEY, json.dumps(state))
                if identity.get('turn_id'):
                    with self.db:
                        self.db.execute('UPDATE completion_turns SET next_preview=? WHERE turn_id=? AND chat_id=? AND topic_id=?',
                                        (retry_at, identity['turn_id'], identity['chat_id'], identity['topic_id']))
            # A receipt or progress failure must never create a normal message.
            return False

    async def receipt(self, telegram, *, connected=True):
        """Preview durable input acceptance without waiting for native work.

        The separate receipt state is a display identity, never a synthetic
        native turn. The actual native group adopts it once input is accepted.
        """
        async with self.preview_lock:
            if not self.enabled or not self.bridge.owner_allowed():
                self._clear_receipt()
                return False
            if (self.state().get('draft_unsupported')
                    or self.clock() < self.state().get('draft_retry_at', 0)):
                return False
            source = self._latest_receipt_source()
            if source is None or source['state'] not in ('pending', 'dispatching', 'uncertain', 'running'):
                self._clear_receipt()
                return False
            previous = self._receipt_state()
            same_route = previous.get('route') == self.route()
            same_source = same_route and previous.get('source_id') == source['id']
            if same_route and self.clock() < previous.get('attempted_at', 0) + self.RECEIPT_GAP:
                return False
            group = self._group(source['turn_id']) if source['turn_id'] else None
            if group is not None and group['stage'] == 'ready':
                self._clear_receipt()
                return False
            if group is None:
                group = next((g for g in self.db.execute(
                    "SELECT * FROM completion_turns WHERE stage IN ('active','closing') ORDER BY started_at DESC")
                    if self._current(g)), None)
            if group is not None and not self._current(group):
                self._clear_receipt()
                return False
            # Existing native tasks keep their accumulated draft and normal
            # refresh schedule. New supplementary input may acknowledge sooner.
            if same_source and group is not None:
                cached_status = self._preview_status if self._preview_status_turn_id == group['turn_id'] else None
                return bool(await self._update(telegram, status=cached_status, connected=connected))
            if same_source and self.clock() < previous.get('next_preview', 0):
                return False
            if source['state'] == 'uncertain':
                heading = '⚠️ 已收到，正在核对投递状态'
            elif not connected:
                heading = '📨 已收到，等待连接恢复'
            elif group is not None:
                heading = '📨 已收到新消息，任务处理中'
            elif source['state'] == 'dispatching':
                heading = '⏳ 已收到，正在提交任务'
            elif source['state'] == 'running':
                heading = '⏳ 已收到，正在获取任务状态'
            else:
                heading = '📨 已收到，正在准备任务'
            draft_id = (group['draft_id'] if group is not None else
                        previous['draft_id'] if same_route else secrets.randbelow(2 ** 31 - 1) + 1)
            receipt = {'route': self.route(), 'source_id': source['id'], 'draft_id': draft_id,
                       'received_at': source['received_ms'] / 1000, 'attempted_at': self.clock(),
                       'next_preview': self.clock() + self.INTERVAL}
            self.store.set(self.RECEIPT_KEY, json.dumps(receipt))
            if group is not None:
                self._sync_rows()
                body, started = self._body(group), group['started_at']
                with self.db:
                    self.db.execute('UPDATE completion_turns SET next_preview=? WHERE turn_id=? AND chat_id=? AND topic_id=?',
                                    (receipt['next_preview'], group['turn_id'], group['chat_id'], group['topic_id']))
            else:
                body, started = '', receipt['received_at']
            identity = {'native_id': self.bridge.thread_id, 'chat_id': str(self.bridge.owner),
                        'topic_id': self.bridge.target['message_thread_id'], 'draft_id': draft_id,
                        'turn_id': group['turn_id'] if group is not None else None}
            return await self._send_preview(telegram, identity, self._preview_text(heading, started, body))

    async def update(self, telegram, *, turns=None, status=None, connected=True):
        async with self.preview_lock:
            return await self._update(telegram, turns=turns, status=status, connected=connected)

    async def _update(self, telegram, *, turns=None, status=None, connected=True):
        if not self.enabled or not self.bridge.owner_allowed():
            return
        for turn in turns or []:
            self.watch(turn)
        self._sync_rows()
        groups = self.db.execute("SELECT * FROM completion_turns WHERE notify=1 AND notice_state NOT IN ('sent','failed','uncertain','blocked') "
                                 'ORDER BY started_at DESC').fetchall()
        receipt_source = self._unresolved_receipt_source()
        group = next((g for g in groups if self._current(g)
                      and not (g['stage'] == 'ready' and receipt_source is not None
                               and receipt_source['turn_id'] != g['turn_id'])), None)
        if status is not None:
            active = next((t for t in turns or [] if t.get('status') == 'inProgress'), None)
            status_turn = active['id'] if active else group['turn_id'] if group is not None and group['stage'] == 'active' else None
            if status_turn is not None:
                self._preview_status = status
                self._preview_status_turn_id = status_turn
        if (group is None or self.clock() < group['next_preview'] or self.state().get('draft_unsupported')
                or self.clock() < self.state().get('draft_retry_at', 0)):
            return
        if not connected:
            heading = '⚠️ 连接中断，等待恢复'
        elif group['stage'] == 'ready':
            heading = {'completed': '✅ 已完成，正在发送结果', 'failed': '❌ 执行失败，正在发送现有过程',
                       'interrupted': '⏹ 已停止，正在发送现有过程'}.get(group['status'], '⚠️ 状态待确认')
        elif group['stage'] == 'closing':
            heading = '⏳ 正在核对完整结果'
        elif (self._preview_status_turn_id == group['turn_id']
              and any(flag in (self._preview_status or {}).get('activeFlags', [])
                      for flag in ('waitingOnApproval', 'waitingOnUserInput'))):
            heading = '🙋 需要你处理 · 去 Codex'
        else:
            heading = '⏳ 正在工作'
        body = self._body(group)
        text = self._preview_text(heading, group['started_at'], body)
        # Keep one ID per task. Client animation is not reliably disabled by
        # rotating IDs; on supported iPads the rich thinking blocks display
        # each public status/progress block without revealing letters.
        with self.db:
            self.db.execute('UPDATE completion_turns SET next_preview=? WHERE turn_id=? AND chat_id=? AND topic_id=?',
                            (self.clock() + self.INTERVAL, group['turn_id'], group['chat_id'], group['topic_id']))
        return await self._send_preview(telegram, dict(group), text)
