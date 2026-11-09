"""Durable display-only draft identities following ordinary topic messages."""
import hashlib
import json
import secrets


class StatusDraftPosition:
    KEY = 'status_draft_position_v1:'
    MAX_MESSAGE_ID = 2 ** 31 - 1  # Telegram cloud IDs are int32; injected jobs use other IDs.

    def __init__(self, delivery):
        self.delivery, self.store, self.db = delivery, delivery.store, delivery.db

    def _source(self, source_id, route):
        return self.db.execute('SELECT * FROM inbox WHERE id=? AND chat_id=? AND message_thread_id=?',
                               (source_id, route[1], route[2])).fetchone() if source_id is not None else None

    def _tail(self, identity):
        route = self.delivery.route()
        incoming = self.db.execute('SELECT MAX(source_message_id) FROM inbox WHERE chat_id=? '
                                   'AND message_thread_id=? AND id>=0 AND source_message_id BETWEEN 1 AND ?',
                                   (*route[1:], self.MAX_MESSAGE_ID)).fetchone()[0] or 0
        outgoing = self.db.execute('SELECT MAX(message_id) FROM outbox WHERE chat_id=? '
                                   'AND message_thread_id=? AND message_id BETWEEN 1 AND ?',
                                   (*route[1:], self.MAX_MESSAGE_ID)).fetchone()[0] or 0
        preview = self.delivery.body_preview.load(identity).get('message_id') or 0
        if not isinstance(preview, int) or isinstance(preview, bool) or not 0 < preview <= self.MAX_MESSAGE_ID:
            preview = 0
        return max(0, incoming, outgoing, preview)

    def prepare(self, identity):
        """Return a fresh identity, or None when this task no longer owns display."""
        try:
            result = dict(identity)
            result.update(chat_id=str(result['chat_id']), topic_id=int(result['topic_id']))
            if (not self.delivery.enabled or self.delivery.preview_style != 'status-draft'
                    or not self.delivery.bridge.owner_allowed() or not self.delivery._current(result)
                    or self.delivery.status_card._stale(result)):
                return None
            route = self.delivery.route()
            key = self.KEY + hashlib.sha256(json.dumps(route).encode()).hexdigest()[:24]
            with self.db:
                source = self._source(result.get('source_id'), route)
                turn_id = result.get('turn_id') or (source['turn_id'] if source is not None else None)
                group = self.db.execute('SELECT rowid AS position_sequence,* FROM completion_turns WHERE native_id=? AND chat_id=? '
                                        'AND topic_id=? AND turn_id=?', (*route, turn_id)).fetchone() if turn_id else None
                if result.get('turn_id') and group is None:
                    return None
                if group is not None and self.db.execute(
                        'SELECT 1 FROM completion_turns WHERE native_id=? AND chat_id=? AND topic_id=? '
                        'AND turn_id<>? AND notify=1 AND (started_at>? '
                        'OR (started_at=? AND rowid>?)) LIMIT 1',
                        (*route, group['turn_id'], group['started_at'], group['started_at'],
                         group['position_sequence'])).fetchone():
                    return None  # A late native-only task cannot reclaim a newer task's display.
                receipt = self.delivery._receipt_state()
                receipt_source = self._source(receipt.get('source_id'), route) if receipt.get('route') == route else None
                receipt_matches = receipt_source is not None and (
                    receipt.get('source_id') == result.get('source_id')
                    or (group is not None and receipt_source['turn_id'] == group['turn_id']))
                draft_id = group['draft_id'] if group is not None else receipt.get('draft_id') if receipt_matches else None
                if not isinstance(draft_id, int) or isinstance(draft_id, bool) or draft_id <= 0:
                    return None
                state = json.loads(self.store.get(key, '{}'))
                same = bool(state and state.get('route') == route and (
                    (turn_id and state.get('turn_id') == turn_id)
                    or (not turn_id and not state.get('turn_id')
                        and state.get('source_id') == result.get('source_id'))))
                if not same and state.get('route') == route and turn_id and not state.get('turn_id'):
                    adopted = self._source(state.get('source_id'), route)
                    same = adopted is not None and adopted['turn_id'] == turn_id
                tail = self._tail(result)
                ack = self.delivery.state().get('status_draft_ack', {})
                legacy = (not same and ack.get('route') == route and 'position_tail' not in ack
                          and ((turn_id and ack.get('turn_id') == turn_id)
                               or (ack.get('turn_id') is None and ack.get('draft_id') == draft_id)))
                # A fast next task may inherit the previous receipt's display
                # ID. Its newer message must reanchor that reused ID too.
                rotate = legacy or (state.get('route') == route and state.get('draft_id') == draft_id
                                    and (tail > state.get('position_tail', 0)
                                         or state.get('position_tail', 0) > self.MAX_MESSAGE_ID))
                if rotate:
                    previous = {draft_id, state.get('draft_id'), ack.get('draft_id')}
                    while draft_id in previous:
                        draft_id = secrets.randbelow(2 ** 31 - 1) + 1
                state = {'route': route, 'turn_id': turn_id, 'source_id': result.get('source_id'),
                         'draft_id': draft_id, 'position_tail': tail}
                self.db.execute('INSERT OR REPLACE INTO kv VALUES(?,?)',
                                (key, json.dumps(state, ensure_ascii=False)))
                if group is not None:
                    self.db.execute('UPDATE completion_turns SET draft_id=? WHERE native_id=? AND chat_id=? '
                                    'AND topic_id=? AND turn_id=?', (draft_id, *route, group['turn_id']))
                if receipt_matches:
                    receipt['draft_id'] = draft_id
                    self.db.execute('INSERT OR REPLACE INTO kv VALUES(?,?)',
                                    (self.delivery.RECEIPT_KEY, json.dumps(receipt, ensure_ascii=False)))
            result.update(draft_id=draft_id, position_tail=tail)
            return result
        except Exception:
            return None  # Optional position repair cannot affect native task delivery.
