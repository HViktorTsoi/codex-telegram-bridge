"""Durable text coalescing for a sealed completion-delivery turn.

Canonical outbox rows remain available for previews and auditing. A transaction
hands their delivery to bounded packets; packets never become preview sources.
"""
import hashlib
import json

from message_format import message_parts, utf16_size
from input_echo import ITEM_SEPARATOR, aggregate_parts, has_echo


STATUS = {
    'completed': '',
    'failed': '❌ 本轮任务执行失败，请查看上方过程和 Codex 会话。',
    'interrupted': '⏹ 本轮任务已停止。',
}
LEGACY_SUCCESS = '✅ 本轮任务已完成。'


class CompletionBatches:
    SPLIT_KEY = 'completion_split_notice_v1:'

    def __init__(self, delivery):
        self.delivery, self.db = delivery, delivery.db
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS completion_packets(
                key TEXT PRIMARY KEY,turn_id TEXT,chat_id TEXT,topic_id INTEGER,
                generation INTEGER,state TEXT DEFAULT 'queued',begun INTEGER DEFAULT 0);
            CREATE TABLE IF NOT EXISTS completion_packet_sources(
                source_key TEXT PRIMARY KEY,turn_id TEXT,chat_id TEXT,topic_id INTEGER,
                generation INTEGER);
            CREATE TABLE IF NOT EXISTS completion_final_sources(
                turn_id TEXT,chat_id TEXT,topic_id INTEGER,source_base TEXT,
                PRIMARY KEY(turn_id,chat_id,topic_id));
            CREATE INDEX IF NOT EXISTS completion_packets_route
                ON completion_packets(turn_id,chat_id,topic_id);
            CREATE INDEX IF NOT EXISTS completion_packet_sources_route
                ON completion_packet_sources(turn_id,chat_id,topic_id);
            CREATE INDEX IF NOT EXISTS completion_rows_route
                ON completion_rows(turn_id,chat_id,topic_id);
        ''')

    @staticmethod
    def identity(group):
        return group['turn_id'], group['chat_id'], group['topic_id']

    @staticmethod
    def digest(group):
        return hashlib.sha256(json.dumps(list(CompletionBatches.identity(group))).encode()).hexdigest()[:24]

    def group(self, row):
        packet = self.db.execute('SELECT * FROM completion_packets WHERE key=?', (row['key'],)).fetchone()
        return (self.delivery._group(packet['turn_id'], packet['chat_id'], packet['topic_id'])
                if packet else None)

    def pending(self, group):
        return bool(self.db.execute('SELECT 1 FROM completion_packets p JOIN outbox o ON o.key=p.key '
                                    'WHERE p.turn_id=? AND p.chat_id=? AND p.topic_id=? '
                                    "AND p.state='queued' AND o.message_id IS NULL LIMIT 1",
                                    self.identity(group)).fetchone())

    def media_pending(self, group):
        return bool(self.db.execute('SELECT 1 FROM completion_rows c JOIN outbox o ON o.key=c.key '
                                    'WHERE c.turn_id=? AND c.chat_id=? AND c.topic_id=? '
                                    'AND o.message_id IS NULL AND o.media_json IS NOT NULL LIMIT 1',
                                    self.identity(group)).fetchone())

    @staticmethod
    def body(rows):
        parts, previous = [], None
        for row in rows:
            base = row['key'].rsplit(':', 1)[0]
            if row['text']:
                parts.append((ITEM_SEPARATOR if parts and base != previous else '') + row['text'])
                previous = base
        return ''.join(parts)

    @staticmethod
    def without_status(text):
        for status in (LEGACY_SUCCESS, *STATUS.values()):
            if not status:
                continue
            if text == status:
                return ''
            suffix = '\n\n' + status
            if text.endswith(suffix):
                return text[:-len(suffix)]
        return text

    def choose_final(self, turn_id, chat_id, topic_id, candidates):
        """Freeze a native item's identity only after its complete history scan."""
        group = self.delivery._group(turn_id, chat_id, topic_id)
        if group is None or group['stage'] == 'ready':
            return
        eligible = []
        for source_base, phase in candidates:
            exists = self.db.execute('SELECT 1 FROM outbox WHERE chat_id=? AND message_thread_id=? '
                                     'AND substr(key,1,?)=? AND (message_id IS NULL OR message_id=-2) LIMIT 1',
                                     (str(chat_id), topic_id, len(source_base) + 1, source_base + ':')).fetchone()
            if exists:
                eligible.append((source_base, phase))
        marked = [source for source, phase in eligible if phase == 'final_answer']
        if not eligible:
            return  # Legacy identities remain eligible for the queue fallback.
        chosen = marked[-1] if marked else eligible[-1][0]
        with self.db:
            self.db.execute('INSERT INTO completion_final_sources VALUES(?,?,?,?) '
                            'ON CONFLICT(turn_id,chat_id,topic_id) DO UPDATE SET source_base=excluded.source_base',
                            (turn_id, str(chat_id), topic_id, chosen))

    def _final_base(self, group, sources):
        saved = self.db.execute('SELECT source_base FROM completion_final_sources '
                                'WHERE turn_id=? AND chat_id=? AND topic_id=?', self.identity(group)).fetchone()
        if saved:
            return saved['source_base']
        # Legacy/direct queue producers have no phase metadata. Freeze the last
        # public assistant item, excluding transport notices and user echoes.
        prefix = 'turn:' + group['turn_id'] + ':'
        candidates = [row['key'].rsplit(':', 1)[0] for row in sources
                      if row['key'].startswith(prefix)
                      and not row['key'].startswith(prefix + 'delivery-warning-')
                      and not row['key'].startswith(prefix + 'status:')]
        chosen = candidates[-1] if candidates else ''
        if chosen:
            self.db.execute('INSERT OR IGNORE INTO completion_final_sources VALUES(?,?,?,?)',
                            (*self.identity(group), chosen))
        return chosen

    def _insert_packet(self, group, generation, index, text, rich_text, retry_at):
        key = f"completion-part:{self.digest(group)}:{generation}:{index}:topic:{group['topic_id']}:0"
        self.db.execute('INSERT INTO outbox(key,chat_id,text,rich_text,message_thread_id,retry_at) '
                        'VALUES(?,?,?,?,?,?)',
                        (key, group['chat_id'], text, rich_text, group['topic_id'], retry_at))
        self.db.execute('INSERT INTO completion_packets(key,turn_id,chat_id,topic_id,generation) VALUES(?,?,?,?,?)',
                        (key, *self.identity(group), generation))

    def _generation(self, group):
        return 1 + self.db.execute('SELECT COALESCE(MAX(generation),0) FROM ('
                                  'SELECT generation FROM completion_packets WHERE turn_id=? AND chat_id=? AND topic_id=? '
                                  'UNION ALL SELECT generation FROM completion_packet_sources '
                                  'WHERE turn_id=? AND chat_id=? AND topic_id=?)',
                                  (*self.identity(group), *self.identity(group))).fetchone()[0]

    def _sources(self, group, include_claimed=False):
        return self.db.execute('SELECT o.* FROM outbox o JOIN completion_rows c ON c.key=o.key '
                               'LEFT JOIN completion_packet_sources s ON s.source_key=o.key '
                               'WHERE c.turn_id=? AND c.chat_id=? AND c.topic_id=? AND o.media_json IS NULL '
                               'AND (o.message_id IS NULL' + (' OR s.source_key IS NOT NULL' if include_claimed else '') + ') '
                               'ORDER BY o.rowid', self.identity(group)).fetchall()

    def _prepare(self, group):
        if group['status'] == 'completed':
            return self._prepare_completed(group)
        if self.media_pending(group):
            return  # Snapshot upload failures into the same final text, too.
        pending = self._sources(group)
        final_key = group['notice_key'] or f"completion-notice:{self.digest(group)}:topic:{group['topic_id']}:0"
        final = self.db.execute('SELECT * FROM outbox WHERE key=?', (final_key,)).fetchone()
        final_mutable = (group['notice_state'] in ('none', 'queued')
                         and (final is None or final['message_id'] is None))
        if not pending and (final is not None or group['notice_state'] != 'none'):
            return
        begun = self.db.execute('SELECT 1 FROM completion_packets WHERE turn_id=? AND chat_id=? AND topic_id=? '
                                'AND begun=1 LIMIT 1', self.identity(group)).fetchone()
        rebuild = final_mutable and not begun
        sources = self._sources(group, include_claimed=True) if rebuild else pending
        generation = self._generation(group)
        retry_at = max(max((row['retry_at'] for row in sources), default=0),
                       final['retry_at'] if final_mutable and final else 0)
        text = self.body(sources)
        if not rebuild and final_mutable and final and text:
            text = ITEM_SEPARATOR + text  # The immutable prefix belongs to an earlier public item.
        rich = self.delivery.store.get('output_format', 'rich') == 'rich'
        footer = STATUS[group['status']] if final_mutable else ''
        limit = self.delivery.LIMIT - utf16_size('\n\n' + footer) if footer else self.delivery.LIMIT
        parts = list(aggregate_parts(sources, rich=rich, limit=limit,
                                    prefix=ITEM_SEPARATOR if not rebuild and final_mutable and final else '')
                     if has_echo(sources)
                     else message_parts(text, rich=rich, limit=limit))
        # Do not route through Store.enqueue_at here: its context manager would
        # commit the handoff before all packet/source updates are durable.
        with self.db:
            if rebuild:
                self.db.execute('UPDATE outbox SET message_id=-1 WHERE message_id IS NULL AND key IN '
                                '(SELECT key FROM completion_packets WHERE turn_id=? AND chat_id=? AND topic_id=? '
                                "AND state='queued' AND begun=0)", self.identity(group))
                self.db.execute("UPDATE completion_packets SET state='superseded' WHERE turn_id=? AND chat_id=? AND topic_id=? "
                                "AND state='queued' AND begun=0", self.identity(group))
            index = 0
            if not rebuild and final_mutable and final:
                # A delivered/attempted prefix is immutable. Preserve the old
                # unsent tail as one quiet packet, then place fresh text last.
                old_text = self.without_status(final['text'])
                old_rich = self.without_status(final['rich_text']) if final['rich_text'] is not None else None
                if old_text:
                    self._insert_packet(group, generation, index, old_text, old_rich, final['retry_at'])
                    index += 1
            quiet_parts = parts[:-1] if footer and parts else parts
            for raw, formatted in quiet_parts:
                self._insert_packet(group, generation, index, raw, formatted, retry_at)
                index += 1
            if footer:
                raw, formatted = parts[-1] if parts else ('', None)
                final_suffix = '\n\n' + footer if raw else footer
                formatted = formatted + final_suffix if formatted is not None else None
                self.db.execute('INSERT INTO outbox(key,chat_id,text,rich_text,message_thread_id,retry_at) '
                                'VALUES(?,?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET '
                                'text=excluded.text,rich_text=excluded.rich_text,retry_at=excluded.retry_at',
                                (final_key, group['chat_id'], raw + final_suffix, formatted, group['topic_id'], retry_at))
                self.db.execute("UPDATE completion_turns SET notice_state='queued',notice_key=? "
                                'WHERE turn_id=? AND chat_id=? AND topic_id=?', (final_key, *self.identity(group)))
            for source in sources:
                self.db.execute('INSERT INTO completion_packet_sources VALUES(?,?,?,?,?) '
                                'ON CONFLICT(source_key) DO UPDATE SET generation=excluded.generation',
                                (source['key'], *self.identity(group), generation))
                self.db.execute('UPDATE outbox SET message_id=-2 WHERE key=? AND message_id IS NULL', (source['key'],))

    def _prepare_completed(self, group):
        if self.media_pending(group):
            return
        pending = self._sources(group)
        final_key = group['notice_key'] or f"completion-notice:{self.digest(group)}:topic:{group['topic_id']}:0"
        final = self.db.execute('SELECT * FROM outbox WHERE key=?', (final_key,)).fetchone()
        final_mutable = (group['notice_state'] in ('none', 'queued')
                         and (final is None or final['message_id'] is None))
        if not pending and (final is not None or group['notice_state'] != 'none'):
            return
        begun = self.db.execute('SELECT 1 FROM completion_packets WHERE turn_id=? AND chat_id=? AND topic_id=? '
                                'AND begun=1 LIMIT 1', self.identity(group)).fetchone()
        # Once any prefix was attempted its body and the prepared final tail
        # remain frozen. Late progress/warnings become additional quiet packets.
        rebuild = final_mutable and not begun and (final is None or not final['attempts'])
        sources = self._sources(group, include_claimed=True) if rebuild else pending
        generation = self._generation(group)
        retry_at = max(max((row['retry_at'] for row in sources), default=0),
                       final['retry_at'] if final_mutable and final else 0)
        rich = self.delivery.store.get('output_format', 'rich') == 'rich'
        with self.db:
            final_base = self._final_base(group, sources)
            if rebuild:
                self.db.execute('UPDATE outbox SET message_id=-1 WHERE message_id IS NULL AND key IN '
                                '(SELECT key FROM completion_packets WHERE turn_id=? AND chat_id=? AND topic_id=? '
                                "AND state='queued' AND begun=0)", self.identity(group))
                self.db.execute("UPDATE completion_packets SET state='superseded' WHERE turn_id=? AND chat_id=? AND topic_id=? "
                                "AND state='queued' AND begun=0", self.identity(group))
            final_sources = [row for row in sources if row['key'].rsplit(':', 1)[0] == final_base] if rebuild else []
            history = [row for row in sources if row not in final_sources]
            history_parts = list(aggregate_parts(history, rich=rich, limit=self.delivery.LIMIT))
            index = 0
            for raw, formatted in history_parts:
                self._insert_packet(group, generation, index, raw, formatted, retry_at)
                index += 1
            if rebuild or (final_mutable and final is None):
                final_body = self.body(final_sources) or '本轮没有文本回复。'
                final_parts = list(message_parts(final_body, rich=rich, limit=self.delivery.LIMIT))
                for raw, formatted in final_parts[:-1]:
                    self._insert_packet(group, generation, index, raw, formatted, retry_at)
                    index += 1
                raw, formatted = final_parts[-1]
                self.db.execute('INSERT INTO outbox(key,chat_id,text,rich_text,message_thread_id,retry_at) '
                                'VALUES(?,?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET '
                                'text=excluded.text,rich_text=excluded.rich_text,retry_at=excluded.retry_at',
                                (final_key, group['chat_id'], raw, formatted, group['topic_id'], retry_at))
                self.db.execute("UPDATE completion_turns SET notice_state='queued',notice_key=? "
                                'WHERE turn_id=? AND chat_id=? AND topic_id=?', (final_key, *self.identity(group)))
                self.db.execute('INSERT OR REPLACE INTO kv VALUES(?,?)', (self.SPLIT_KEY + final_key, '1'))
            for source in sources:
                self.db.execute('INSERT INTO completion_packet_sources VALUES(?,?,?,?,?) '
                                'ON CONFLICT(source_key) DO UPDATE SET generation=excluded.generation',
                                (source['key'], *self.identity(group), generation))
                self.db.execute('UPDATE outbox SET message_id=-2 WHERE key=? AND message_id IS NULL', (source['key'],))

    def prepare(self):
        for group in self.db.execute("SELECT * FROM completion_turns WHERE stage='ready' AND notify=1 "
                                     "AND status IN ('completed','failed','interrupted') "
                                     "AND (notice_state IN ('none','queued') OR EXISTS ("
                                     'SELECT 1 FROM completion_rows c JOIN outbox o ON o.key=c.key '
                                     'WHERE c.turn_id=completion_turns.turn_id AND c.chat_id=completion_turns.chat_id '
                                     'AND c.topic_id=completion_turns.topic_id AND o.message_id IS NULL '
                                     'AND o.media_json IS NULL))').fetchall():
            if self.delivery._current(group):
                self._prepare(group)
            elif group['notice_state'] in ('none', 'queued'):
                with self.db:
                    self.db.execute("UPDATE completion_turns SET notice_state='blocked' WHERE turn_id=? AND chat_id=? AND topic_id=?",
                                    self.identity(group))

    def suppress_notices(self):
        """A policy window may suppress alerts, never the queued result body."""
        for group in self.db.execute("SELECT * FROM completion_turns WHERE notice_state='queued'").fetchall():
            row = self.db.execute('SELECT * FROM outbox WHERE key=?', (group['notice_key'],)).fetchone()
            if row and row['message_id'] is None:
                split = self.delivery.store.get(self.SPLIT_KEY + row['key']) == '1'
                text = row['text'] if split else self.without_status(row['text'])
                rich = row['rich_text'] if split or row['rich_text'] is None else self.without_status(row['rich_text'])
                if text:
                    self._insert_packet(group, self._generation(group), 0, text, rich, row['retry_at'])

    def begin(self, row):
        with self.db:
            self.db.execute("UPDATE completion_packets SET state='sending',begun=1 WHERE key=? AND state='queued'", (row['key'],))

    def sent(self, row):
        with self.db:
            self.db.execute("UPDATE completion_packets SET state='sent',begun=1 WHERE key=?", (row['key'],))

    def failed(self, row, error):
        if not self.db.execute('SELECT 1 FROM completion_packets WHERE key=?', (row['key'],)).fetchone():
            return False
        code = getattr(error, 'code', None)
        with self.db:
            if code == 429:
                self.db.execute("UPDATE completion_packets SET state='queued' WHERE key=?", (row['key'],))
                self.db.execute('UPDATE outbox SET attempts=attempts+1,retry_at=? WHERE key=?',
                                (self.delivery.clock() + max(3, getattr(error, 'retry_after', 3)), row['key']))
            else:
                self.db.execute('UPDATE completion_packets SET state=? WHERE key=?',
                                ('failed' if code in (400, 403) else 'uncertain', row['key']))
                self.db.execute('UPDATE outbox SET message_id=-1 WHERE key=?', (row['key'],))
                self._warning(row, uncertain=code not in (400, 403))
        return True

    def _warning(self, row, uncertain=True):
        group = self.group(row)
        if group is None:
            return
        digest = hashlib.sha256(row['key'].encode()).hexdigest()[:24]
        text = ('⚠️ 部分正文发送结果未获确认，未自动重发；完整记录保留在 Codex，可要求重发。' if uncertain else
                '⚠️ 部分正文发送失败；完整记录保留在 Codex，可要求重发。')
        key = f"turn:{group['turn_id']}:delivery-warning-{digest}:topic:{group['topic_id']}:0"
        rich = text if self.delivery.store.get('output_format', 'rich') == 'rich' else None
        self.db.execute('INSERT OR IGNORE INTO outbox(key,chat_id,text,rich_text,message_thread_id) VALUES(?,?,?,?,?)',
                        (key, group['chat_id'], text, rich, group['topic_id']))

    def recover(self):
        uncertain = self.db.execute("SELECT o.* FROM completion_packets p JOIN outbox o ON o.key=p.key "
                                    "WHERE p.state='sending' AND o.message_id IS NULL").fetchall()
        with self.db:
            self.db.execute("UPDATE completion_packets SET state='sent',begun=1 WHERE state='sending' AND key IN "
                            '(SELECT key FROM outbox WHERE message_id>0)')
            self.db.execute('UPDATE outbox SET message_id=-1 WHERE message_id IS NULL AND key IN '
                            "(SELECT key FROM completion_packets WHERE state='sending')")
            self.db.execute("UPDATE completion_packets SET state='uncertain' WHERE state='sending'")
            for row in uncertain:
                self._warning(row)
