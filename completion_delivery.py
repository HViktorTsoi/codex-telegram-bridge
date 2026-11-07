"""Buffer task output, preview it as one draft, and notify after a full scan.

The caller owns native history reads: ``terminal`` only records an intent;
``seal`` is proof that the complete terminal history has been collected.
"""
import hashlib
import json
import secrets
import time

from message_format import utf16_size


TERMINAL = ('completed', 'failed', 'interrupted')


class CompletionDelivery:
    KEY = 'completion_delivery_v1'
    INTERVAL = 15
    LIMIT = 3800

    def __init__(self, bridge, clock=time.time):
        self.bridge, self.store, self.db, self.clock = bridge, bridge.store, bridge.store.db, clock
        self.enabled = bridge.target.get('notification_mode', bridge.config.get('notification_mode', 'all')) == 'completion'
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS completion_turns(
                turn_id TEXT,chat_id TEXT,topic_id INTEGER,native_id TEXT,
                stage TEXT,status TEXT,started_at REAL,notify INTEGER DEFAULT 0,
                notice_state TEXT DEFAULT 'none',notice_key TEXT,
                draft_id INTEGER,next_preview REAL DEFAULT 0,
                PRIMARY KEY(turn_id,chat_id,topic_id));
            CREATE TABLE IF NOT EXISTS completion_rows(
                key TEXT PRIMARY KEY,turn_id TEXT,chat_id TEXT,topic_id INTEGER);
        ''')
        state = self.state()
        if self.enabled:
            if state.get('route') != self.route() or state.get('enabled') is not True:
                self._new_window()
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
            self.db.execute("UPDATE outbox SET message_id=-1 WHERE message_id IS NULL AND key IN "
                            "(SELECT notice_key FROM completion_turns WHERE notice_state IN ('queued','sending'))")
            self.db.execute("UPDATE completion_turns SET notify=0,notice_state=CASE "
                            "WHEN notice_state='queued' THEN 'suppressed' WHEN notice_state='sending' THEN 'uncertain' "
                            "ELSE notice_state END")
        self.store.set(self.KEY, json.dumps({'route': self.route(), 'since': self.clock(), 'enabled': True}))

    def _current(self, row):
        return (not self.bridge.target.get('topic_missing')
                and self.bridge.target.get('thread_id') == self.bridge.thread_id
                and [row['native_id'], row['chat_id'], row['topic_id']] == self.route())

    def _group(self, turn_id, chat_id=None, topic_id=None):
        return self.db.execute('SELECT * FROM completion_turns WHERE turn_id=? AND chat_id=? AND topic_id=?',
                               (turn_id, str(chat_id or self.bridge.owner),
                                self.bridge.target['message_thread_id'] if topic_id is None else topic_id)).fetchone()

    def _create(self, turn_id, chat_id, topic_id, *, status=None, started=None, notify=False):
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO completion_turns '
                            '(turn_id,chat_id,topic_id,native_id,stage,status,started_at,notify,draft_id) '
                            'VALUES(?,?,?,?,?,?,?,?,?)',
                            (turn_id, str(chat_id), topic_id, self.bridge.thread_id,
                             'closing' if status in TERMINAL else 'active', status, started if started is not None else self.clock(),
                             int(notify), secrets.randbelow(2 ** 31 - 1) + 1))
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
        return bool(self.db.execute('SELECT 1 FROM outbox o JOIN completion_rows c ON c.key=o.key '
                                    'WHERE c.turn_id=? AND c.chat_id=? AND c.topic_id=? AND o.message_id IS NULL LIMIT 1',
                                    (group['turn_id'], group['chat_id'], group['topic_id'])).fetchone())

    def _notices(self):
        for group in self.db.execute("SELECT * FROM completion_turns WHERE stage='ready' AND notify=1 "
                                     "AND status IN ('completed','failed','interrupted') "
                                     "AND notice_state IN ('none','queued')").fetchall():
            if not self._current(group):
                with self.db:
                    self.db.execute("UPDATE completion_turns SET notice_state='blocked' WHERE turn_id=? AND chat_id=? AND topic_id=?",
                                    (group['turn_id'], group['chat_id'], group['topic_id']))
                continue
            if self._pending(group):
                continue
            digest = hashlib.sha256(json.dumps([group['turn_id'], group['chat_id'], group['topic_id']]).encode()).hexdigest()[:24]
            base = 'completion-notice:' + digest
            full = f"{base}:topic:{group['topic_id']}:0"
            with self.db:
                self.db.execute("UPDATE completion_turns SET notice_state='queued',notice_key=? "
                                'WHERE turn_id=? AND chat_id=? AND topic_id=?',
                                (full, group['turn_id'], group['chat_id'], group['topic_id']))
            text = {'completed': '✅ 本轮任务已完成。',
                    'failed': '❌ 本轮任务执行失败，请查看上方过程和 Codex 会话。',
                    'interrupted': '⏹ 本轮任务已停止。'}[group['status']]
            self.store.enqueue_at(base, group['chat_id'], text, group['topic_id'])

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
        return self.enabled and self._task_key(row['key'])

    def begin(self, row):
        if self.enabled:
            with self.db:
                self.db.execute("UPDATE completion_turns SET notice_state='sending' WHERE notice_key=? AND notice_state='queued'", (row['key'],))

    def sent(self, row, message_id):
        if self.enabled:
            with self.db:
                self.db.execute("UPDATE completion_turns SET notice_state='sent' WHERE notice_key=?", (row['key'],))

    def failed(self, row, error):
        """Handle completion-notice errors; True bypasses the sender's fallback."""
        if not self.enabled:
            return False
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

    async def update(self, telegram, *, turns=None, status=None, connected=True):
        if not self.enabled or not self.bridge.owner_allowed():
            return
        for turn in turns or []:
            self.watch(turn)
        self._sync_rows()
        groups = self.db.execute("SELECT * FROM completion_turns WHERE notify=1 AND notice_state NOT IN ('sent','failed','uncertain','blocked') "
                                 'ORDER BY started_at DESC').fetchall()
        group = next((g for g in groups if self._current(g)), None)
        if group is None or self.clock() < group['next_preview'] or self.state().get('draft_unsupported'):
            return
        if not connected:
            heading = '⚠️ 连接中断，等待恢复'
        elif group['stage'] == 'ready':
            heading = {'completed': '✅ 已完成，正在发送结果', 'failed': '❌ 执行失败，正在发送现有过程',
                       'interrupted': '⏹ 已停止，正在发送现有过程'}.get(group['status'], '⚠️ 状态待确认')
        elif group['stage'] == 'closing':
            heading = '⏳ 正在核对完整结果'
        elif any(flag in (status or {}).get('activeFlags', []) for flag in ('waitingOnApproval', 'waitingOnUserInput')):
            heading = '🙋 需要你处理 · 去 Codex'
        else:
            heading = '⏳ 正在工作'
        elapsed = max(0, int(self.clock() - group['started_at']))
        minutes, seconds = divmod(elapsed, 60)
        duration = f'{minutes}分{seconds}秒' if minutes else f'{seconds}秒'
        header = '━━━━ ' + heading + ' ━━━━\n' + duration + ' · 更新 ' + time.strftime('%H:%M:%S', time.localtime(self.clock()))
        body = self._body(group)
        text = header + ('\n\n' + body if body else '')
        if utf16_size(text) > self.LIMIT:
            note = '\n\n（过程较长，预览仅显示最近部分；完整过程将在完成后发送。）\n\n'
            text = header + note + self._tail(body, self.LIMIT - utf16_size(header + note))
        # Telegram animates updates that reuse a draft ID. A new ID replaces
        # the one temporary preview immediately, including status-only refreshes.
        draft_id = group['draft_id'] % (2 ** 31 - 1) + 1
        with self.db:
            self.db.execute('UPDATE completion_turns SET next_preview=?,draft_id=? WHERE turn_id=? AND chat_id=? AND topic_id=?',
                            (self.clock() + self.INTERVAL, draft_id, group['turn_id'], group['chat_id'], group['topic_id']))
        try:
            result = await telegram.call('sendMessageDraft', chat_id=int(group['chat_id']),
                                         message_thread_id=group['topic_id'], draft_id=draft_id, text=text)
            if result is not True:
                raise ConnectionError('Draft acknowledgement unavailable')
        except Exception as error:
            if getattr(error, 'code', None) in (400, 404):
                state = self.state()
                state['draft_unsupported'] = True
                self.store.set(self.KEY, json.dumps(state))
            elif getattr(error, 'code', None) == 429:
                with self.db:
                    self.db.execute('UPDATE completion_turns SET next_preview=? WHERE turn_id=? AND chat_id=? AND topic_id=?',
                                    (self.clock() + max(self.INTERVAL, getattr(error, 'retry_after', 0)),
                                     group['turn_id'], group['chat_id'], group['topic_id']))
            # Preview failure never creates an ordinary progress message.
