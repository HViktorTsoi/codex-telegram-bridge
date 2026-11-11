"""Mirror new local native conversation items without generating or replaying input."""
import hashlib
import json
import time
from collections import Counter


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
        store.db.execute('''CREATE TABLE IF NOT EXISTS native_mirror_ready(
            turn_id TEXT,topic_id INTEGER,fingerprint TEXT,pending INTEGER DEFAULT 1,
            PRIMARY KEY(turn_id,topic_id,fingerprint))''')
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
        if not state.get('enabled') or state.get('route') != route or state.get('protocol') != 3:
            # Baseline existing items; an active desktop turn can still produce new ones.
            active_items = {}
            for turn in turns:
                if turn.get('status') == 'inProgress':
                    entries = [entry async for entry in bridge.codex.items(turn['id'])]
                    for entry in entries:
                        self.store.images.observe(turn['id'], entry['item'], entry.get('completedAtMs'))
                    active_items[turn['id']] = [entry['item'] for entry in entries
                                               if entry['item'].get('type') in ('userMessage', 'agentMessage')]
            with self.store.db:
                for turn in turns:
                    items = active_items.get(turn['id'])
                    telegram_input = any(str(item.get('clientId', '')).startswith('telegram-') for item in items or [])
                    self.store.db.execute('INSERT OR REPLACE INTO native_mirror_turns VALUES(?,?,?,?,?)',
                                          (turn['id'], route[1], 'watch' if items is not None else 'ignored', now, telegram_input))
                    for item, identity, fingerprint in self.identities(items or []):
                        self.store.db.execute('INSERT OR REPLACE INTO native_mirror_seen VALUES(?,?,?,?,0)',
                                              (turn['id'], identity, route[1], fingerprint))
            self.store.set(self.KEY, json.dumps({'enabled': True, 'route': route, 'since': int(now), 'protocol': 3}))
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
            dirty = self.store.db.execute('SELECT 1 FROM native_mirror_ready WHERE turn_id=? AND topic_id=? AND pending=1',
                                         (turn['id'], route[1])).fetchone()
            if not terminal and not dirty and row['last_scan'] and now - row['last_scan'] < self.INTERVAL:
                continue
            # During an active turn only user inputs are complete in a history snapshot.
            # Agent text is mirrored from item/completed, or from a terminal-turn replay.
            occurrences = Counter()
            async for entry in bridge.codex.items(turn['id']):
                item = entry['item']
                self.store.images.observe(turn['id'], item, entry.get('completedAtMs'))
                fingerprint = self.fingerprint(item)
                if fingerprint:
                    occurrences[fingerprint] += 1
                    identity = 'native-content-' + fingerprint + '-' + str(occurrences[fingerprint])
                    self.record(bridge, turn['id'], item, identity, allow_agent=terminal)
            with self.store.db:
                self.store.db.execute('UPDATE native_mirror_turns SET state=?,last_scan=? WHERE turn_id=?',
                                      ('closed' if terminal else 'watch', now, turn['id']))
                self.store.db.execute('UPDATE native_mirror_ready SET pending=0 WHERE turn_id=? AND topic_id=?',
                                      (turn['id'], route[1]))

    def note_event(self, bridge, turn_id, item):
        """Events prove completion and trigger a read; their unstable IDs are ignored."""
        state = self.state()
        if (not state.get('enabled') or state.get('route') != [bridge.thread_id, bridge.target['message_thread_id']]
                or item.get('type') not in ('userMessage', 'agentMessage')):
            return
        row = self.store.db.execute('SELECT state FROM native_mirror_turns WHERE turn_id=?', (turn_id,)).fetchone()
        if row and row['state'] in ('ignored', 'closed'):
            return
        fingerprint = self.fingerprint(item)
        if not fingerprint:
            return
        with self.store.db:
            self.store.db.execute('INSERT OR REPLACE INTO native_mirror_ready VALUES(?,?,?,1)',
                                  (turn_id, bridge.target['message_thread_id'], fingerprint))

    def record(self, bridge, turn_id, item, identity, allow_agent=True):
        state = self.state()
        if not state.get('enabled') or state.get('route') != [bridge.thread_id, bridge.target['message_thread_id']]:
            return
        row = self.store.db.execute('SELECT * FROM native_mirror_turns WHERE turn_id=?', (turn_id,)).fetchone()
        if not row or row['state'] == 'ignored' or row['topic_id'] != bridge.target['message_thread_id']:
            return
        fingerprint = self.fingerprint(item)
        seen = self.store.db.execute('SELECT * FROM native_mirror_seen WHERE turn_id=? AND item_id=? AND topic_id=?',
                                     (turn_id, identity, row['topic_id'])).fetchone()
        if seen and (seen['sent'] or seen['fingerprint'] == fingerprint):
            return
        kind = item.get('type')
        if kind == 'userMessage':
            client_id = item.get('clientId')
            if client_id and (client_id.startswith('telegram-') or self.store.db.execute(
                    'SELECT 1 FROM inbox WHERE client_id=?', (client_id,)).fetchone()):
                with self.store.db:
                    self.store.db.execute('UPDATE native_mirror_turns SET telegram_input=1 WHERE turn_id=?', (turn_id,))
                    self.store.db.execute('INSERT OR REPLACE INTO native_mirror_seen VALUES(?,?,?,?,1)',
                                          (turn_id, identity, row['topic_id'], fingerprint))
                return
            parts = []
            for content in item.get('content', []):
                if content.get('type') == 'text':
                    parts.append(content.get('text', ''))
                elif content.get('type') in ('image', 'localImage'):
                    parts.append('[图片附件，未自动上传]')
            body = '\n'.join(parts).strip()
            key = 'native-user:' + turn_id + ':' + identity
            text = '🖥 你在 Codex 客户端说：\n' + body
        elif kind == 'agentMessage':
            if not allow_agent and not self.store.db.execute(
                    'SELECT 1 FROM native_mirror_ready WHERE turn_id=? AND topic_id=? AND fingerprint=?',
                    (turn_id, row['topic_id'], fingerprint)).fetchone():
                return
            if item.get('phase') not in (None, 'commentary', 'partial_answer', 'final_answer'):
                return
            # Telegram-origin turns already have their durable route and output path.
            if row['telegram_input'] or self.store.db.execute('SELECT 1 FROM inbox WHERE turn_id=?', (turn_id,)).fetchone():
                return
            body = item.get('text', '').strip()
            if '[external_agent_tool' in body:
                return
            key, text = 'turn:' + turn_id + ':' + identity, self.store.images.agent_text(turn_id, item)
        else:
            return
        if body:
            self.store.enqueue_at(key, bridge.owner, text, row['topic_id'])
            if kind == 'userMessage':
                note = getattr(bridge, 'note_user_interaction', None)
                if callable(note):
                    note('native:' + turn_id + ':' + identity,
                         chat_id=bridge.owner, topic_id=row['topic_id'])
            with self.store.db:
                self.store.db.execute('INSERT OR REPLACE INTO native_mirror_seen VALUES(?,?,?,?,1)',
                                      (turn_id, identity, row['topic_id'], fingerprint))

    @staticmethod
    def fingerprint(item):
        kind = item.get('type')
        if kind == 'userMessage':
            body = '\n'.join(c.get('text', '') if c.get('type') == 'text' else '[图片附件，未自动上传]'
                             for c in item.get('content', []) if c.get('type') in ('text', 'image', 'localImage')).strip()
        elif kind == 'agentMessage' and item.get('phase') in (None, 'commentary', 'partial_answer', 'final_answer'):
            body = item.get('text', '').strip()
            if '[external_agent_tool' in body:
                return None
        else:
            return None
        if not body:
            return None
        visible = {'type': kind, 'text': body}
        return hashlib.sha256(json.dumps(visible, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    def identities(self, items):
        occurrences = Counter()
        for item in items:
            fingerprint = self.fingerprint(item)
            if fingerprint:
                occurrences[fingerprint] += 1
                yield item, 'native-content-' + fingerprint + '-' + str(occurrences[fingerprint]), fingerprint
