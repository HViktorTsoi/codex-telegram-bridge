"""Local, durable ordering by accepted user access rather than bot activity."""
import math
import time


class RecentSessions:
    """Track an access event once, even when its delivery is replayed.

    Bot API does not report merely opening a private topic. Callers record an
    accepted user interaction or confirmed handoff, preserving its original time.
    Rendering a menu, model replies and status refreshes do not call ``record``.
    """

    def __init__(self, db, clock=time.time):
        self.db, self.clock = db, clock
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS session_access_events(
                event_id TEXT PRIMARY KEY,thread_id TEXT NOT NULL,accessed_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS session_recent_access(
                thread_id TEXT PRIMARY KEY,accessed_at REAL NOT NULL);
        ''')

    def record(self, thread_id, event_id, accessed_at=None):
        if not isinstance(thread_id, str) or not thread_id or not isinstance(event_id, str) or not event_id:
            return False
        try:
            accessed_at = float(self.clock() if accessed_at is None else accessed_at)
        except (TypeError, ValueError, OverflowError):
            return False
        if not math.isfinite(accessed_at) or accessed_at < 0:
            return False
        with self.db:
            inserted = self.db.execute('INSERT OR IGNORE INTO session_access_events VALUES(?,?,?)',
                                       (event_id, thread_id, accessed_at)).rowcount
            if not inserted:
                return False
            self.db.execute('INSERT INTO session_recent_access VALUES(?,?) '
                            'ON CONFLICT(thread_id) DO UPDATE SET '
                            'accessed_at=MAX(session_recent_access.accessed_at,excluded.accessed_at)',
                            (thread_id, accessed_at))
        return True

    def times(self):
        return {row[0]: row[1] for row in self.db.execute('SELECT thread_id,accessed_at FROM session_recent_access')}

    def bootstrap(self, bridges):
        """Seed each native ID once from its pre-feature accepted input queue."""
        for bridge in bridges:
            event_id = 'bootstrap:' + bridge.thread_id
            if self.db.execute('SELECT 1 FROM session_access_events WHERE event_id=?', (event_id,)).fetchone():
                continue
            received = bridge.store.db.execute("SELECT MAX(received_ms) FROM inbox WHERE state NOT IN "
                                               "('denied','cancelled','failed','blocked')").fetchone()[0]
            self.record(bridge.thread_id, event_id, (received or 0) / 1000)

    def last(self, thread_id):
        row = self.db.execute('SELECT accessed_at FROM session_recent_access WHERE thread_id=?', (thread_id,)).fetchone()
        return row[0] if row else 0

    def ordered(self, targets):
        recent = self.times()
        # Python's stable sort preserves the configured order for equal or absent
        # access times; renaming a session cannot change a tie's position.
        return sorted(targets, key=lambda target: -recent.get(target.get('thread_id') or target.get('id'), 0))
