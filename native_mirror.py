"""Mirror new local native conversation items without generating or replaying input."""
import hashlib
import json
import time


class NativeMirror:
    KEY = 'native_mirror_v1'
    INTERVAL = 15

    def __init__(self, store, clock=time.time):
        self.store, self.clock = store, clock
        store.db.execute('''CREATE TABLE IF NOT EXISTS native_mirror_turns(
            turn_id TEXT PRIMARY KEY,topic_id INTEGER,state TEXT,last_scan REAL DEFAULT 0,
            telegram_input INTEGER DEFAULT 0)''')
        if 'telegram_input' not in {row[1] for row in store.db.execute('PRAGMA table_info(native_mirror_turns)')}:
            store.db.execute('ALTER TABLE native_mirror_turns ADD COLUMN telegram_input INTEGER DEFAULT 0')
        store.db.execute('''CREATE TABLE IF NOT EXISTS native_mirror_seen(
            turn_id TEXT,item_id TEXT,topic_id INTEGER,fingerprint TEXT,sent INTEGER DEFAULT 0,
            PRIMARY KEY(turn_id,item_id,topic_id))''')
        store.db.commit()

    def state(self):
        return json.loads(self.store.get(self.KEY, '{}'))

    def suspend(self):
        state = self.state()
        if state.get('enabled'):
            state['enabled'] = False
            self.store.set(self.KEY, json.dumps(state))

    async def update(self, bridge, turns):
        route = [bridge.thread_id, bridge.target['message_thread_id']]
        now, state = self.clock(), self.state()
        if not state.get('enabled') or state.get('route') != route:
            # Baseline existing items; an active desktop turn can still produce new ones.
            active_items = {}
            for turn in turns:
                if turn.get('status') == 'inProgress':
                    active_items[turn['id']] = [entry['item'] async for entry in bridge.codex.items(turn['id'])
                                               if entry['item'].get('type') in ('userMessage', 'agentMessage')]
            with self.store.db:
                for turn in turns:
                    items = active_items.get(turn['id'])
                    telegram_input = any(str(item.get('clientId', '')).startswith('telegram-') for item in items or [])
                    self.store.db.execute('INSERT OR REPLACE INTO native_mirror_turns VALUES(?,?,?,?,?)',
                                          (turn['id'], route[1], 'watch' if items is not None else 'ignored', now, telegram_input))
                    for item in items or []:
                        if item.get('id'):
                            self.store.db.execute('INSERT OR REPLACE INTO native_mirror_seen VALUES(?,?,?,?,0)',
                                                  (turn['id'], item['id'], route[1], self.fingerprint(item)))
            self.store.set(self.KEY, json.dumps({'enabled': True, 'route': route, 'since': int(now)}))
            return
        # Native recovery, like the existing bridge, covers the latest 30 turns.
        for turn in reversed(turns):
            row = self.store.db.execute('SELECT * FROM native_mirror_turns WHERE turn_id=?', (turn['id'],)).fetchone()
            if row is None:
                started = turn.get('startedAt')
                eligible = isinstance(started, (int, float)) and started >= state['since']
                with self.store.db:
                    self.store.db.execute('INSERT INTO native_mirror_turns(turn_id,topic_id,state) VALUES(?,?,?)',
                                          (turn['id'], route[1], 'watch' if eligible else 'ignored'))
                row = self.store.db.execute('SELECT * FROM native_mirror_turns WHERE turn_id=?', (turn['id'],)).fetchone()
            if row['topic_id'] != route[1] or row['state'] != 'watch':
                continue
            terminal = turn.get('status') in ('completed', 'interrupted', 'failed')
            if not terminal and row['last_scan'] and now - row['last_scan'] < self.INTERVAL:
                continue
            # During an active turn only user inputs are complete in a history snapshot.
            # Agent text is mirrored from item/completed, or from a terminal-turn replay.
            async for entry in bridge.codex.items(turn['id']):
                self.record(bridge, turn['id'], entry['item'], allow_agent=terminal)
            with self.store.db:
                self.store.db.execute('UPDATE native_mirror_turns SET state=?,last_scan=? WHERE turn_id=?',
                                      ('closed' if terminal else 'watch', now, turn['id']))

    def record(self, bridge, turn_id, item, allow_agent=True):
        state = self.state()
        if not state.get('enabled') or state.get('route') != [bridge.thread_id, bridge.target['message_thread_id']]:
            return
        row = self.store.db.execute('SELECT * FROM native_mirror_turns WHERE turn_id=?', (turn_id,)).fetchone()
        if not row or row['state'] == 'ignored' or row['topic_id'] != bridge.target['message_thread_id'] or not item.get('id'):
            return
        fingerprint = self.fingerprint(item)
        seen = self.store.db.execute('SELECT * FROM native_mirror_seen WHERE turn_id=? AND item_id=? AND topic_id=?',
                                     (turn_id, item['id'], row['topic_id'])).fetchone()
        if seen and (seen['sent'] or seen['fingerprint'] == fingerprint):
            return
        kind = item.get('type')
        if kind == 'userMessage':
            client_id = item.get('clientId')
            if client_id and (client_id.startswith('telegram-') or self.store.db.execute(
                    'SELECT 1 FROM inbox WHERE client_id=?', (client_id,)).fetchone()):
                with self.store.db:
                    self.store.db.execute('UPDATE native_mirror_turns SET telegram_input=1 WHERE turn_id=?', (turn_id,))
                return
            parts = []
            for content in item.get('content', []):
                if content.get('type') == 'text':
                    parts.append(content.get('text', ''))
                elif content.get('type') in ('image', 'localImage'):
                    parts.append('[图片附件，未自动上传]')
            body = '\n'.join(parts).strip()
            key = 'native-user:' + turn_id + ':' + item['id']
            text = '🖥 你在 Codex 客户端说：\n' + body
        elif kind == 'agentMessage' and allow_agent:
            if item.get('phase') not in (None, 'commentary', 'partial_answer', 'final_answer'):
                return
            # Telegram-origin turns already have their durable route and output path.
            if row['telegram_input'] or self.store.db.execute('SELECT 1 FROM inbox WHERE turn_id=?', (turn_id,)).fetchone():
                return
            body = item.get('text', '').strip()
            if '[external_agent_tool' in body:
                return
            key, text = 'turn:' + turn_id + ':' + item['id'], body
        else:
            return
        if body:
            self.store.enqueue_at(key, bridge.owner, text, row['topic_id'])
            with self.store.db:
                self.store.db.execute('INSERT OR REPLACE INTO native_mirror_seen VALUES(?,?,?,?,1)',
                                      (turn_id, item['id'], row['topic_id'], fingerprint))

    @staticmethod
    def fingerprint(item):
        visible = {key: item.get(key) for key in ('type', 'content', 'text', 'phase')}
        return hashlib.sha256(json.dumps(visible, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
