"""Durable temporary body previews beside a single live status draft."""
import hashlib
import json

from message_format import formatting_error, normalize_local_links, utf16_size


class CompletionPreview:
    KEY = 'completion_preview_v1:'

    def __init__(self, delivery):
        self.delivery, self.store = delivery, delivery.store

    @staticmethod
    def _identity(identity):
        value = dict(identity)
        value.update(chat_id=str(value['chat_id']), topic_id=int(value['topic_id']))
        if not value.get('native_id') or not value.get('turn_id'):
            return None
        return value

    @staticmethod
    def _route(identity):
        return [identity['native_id'], identity['chat_id'], identity['topic_id']]

    def _key(self, identity):
        frozen = self._route(identity) + [identity['turn_id']]
        return self.KEY + hashlib.sha256(json.dumps(frozen).encode()).hexdigest()[:24]

    def load(self, identity):
        identity = self._identity(identity)
        return json.loads(self.store.get(self._key(identity), '{}')) if identity else {}

    def _save(self, state, identity):
        self.store.set(self._key(identity), json.dumps(state, ensure_ascii=False))

    def _current(self, identity):
        return self.delivery.bridge.owner_allowed() and self.delivery._current(identity)

    def _allowed(self, identity):
        return (self.delivery.enabled and self.delivery.preview_style == 'status-draft'
                and self._current(identity) and not self.delivery.status_card._stale(identity))

    def _delay(self, error):
        return max(15, getattr(error, 'retry_after', 0) or 0)

    def _text(self, body):
        limit = self.delivery.LIMIT
        if utf16_size(body) <= limit:
            return body
        note = '（过程较长，预览仅显示最近部分；完整过程将在完成后发送。）\n\n'
        return note + self.delivery._tail(body, limit - utf16_size(note))

    async def _render(self, telegram, identity, state, text):
        message_id = state.get('message_id')
        route = {'chat_id': int(identity['chat_id'])}
        if message_id:
            route['message_id'] = message_id
        else:
            route.update(message_thread_id=identity['topic_id'], disable_notification=True)
        if self.store.get('output_format', 'rich') == 'rich' and getattr(telegram, 'rich_supported', None) is not False:
            try:
                return await telegram.call('editMessageText' if message_id else 'sendRichMessage',
                                           **route, rich_message={'markdown': normalize_local_links(text)})
            except Exception as error:
                # Only a known formatting rejection permits a second request.
                if not formatting_error(error) or (message_id and getattr(error, 'code', None) == 404):
                    raise
                if not message_id and getattr(error, 'code', None) == 404:
                    telegram.rich_supported = False
                if not self._allowed(identity):
                    raise ConnectionError('Preview route or access changed during formatting fallback')
        return await telegram.call('editMessageText' if message_id else 'sendMessage', **route, text=text)

    async def update(self, telegram, identity, body):
        """Create quietly once, then edit only the known preview message ID."""
        try:
            identity = self._identity(identity)
            if not identity or not body or not body.strip() or not self._allowed(identity):
                return False
            state = self.load(identity)
            if state.get('finished'):
                return False
            message_id = state.get('message_id')
            if not message_id and state.get('delivery') == 'creating':
                state['delivery'] = 'uncertain'
                self._save(state, identity)
            if (state.get('delivery') == 'failed'
                    or (not message_id and state.get('delivery') == 'uncertain')):
                return False
            text = self._text(body)
            state.update(route=self._route(identity), turn_id=identity['turn_id'],
                         desired_identity=identity, desired_text=text)
            self._save(state, identity)
            if self.delivery.clock() < state.get('retry_at', 0):
                return False
            if message_id and state.get('sent_text') == text:
                return True
            state['delivery'] = 'editing' if message_id else 'creating'
            self._save(state, identity)  # Unknown creation must never be replayed.
            try:
                result = await self._render(telegram, identity, state, text)
                if not message_id:
                    mid = result.get('message_id') if isinstance(result, dict) else None
                    if (not isinstance(mid, int) or isinstance(mid, bool) or mid <= 0
                            or ('message_thread_id' in result and result['message_thread_id'] != identity['topic_id'])
                            or ('chat' in result and str(result['chat'].get('id')) != identity['chat_id'])):
                        raise ConnectionError('Preview creation acknowledgement unavailable')
                    state['message_id'] = mid
                elif result is not True and (not isinstance(result, dict) or result.get('message_id') != message_id):
                    raise ConnectionError('Preview edit acknowledgement unavailable')
            except Exception as error:
                code = getattr(error, 'code', None)
                if not (message_id and code == 400 and 'message is not modified' in str(error).lower()):
                    state.update(delivery=('queued' if code == 429 else
                                           'failed' if code in (400, 403, 404) else
                                           'queued' if message_id else 'uncertain'),
                                 retry_at=self.delivery.clock() + self._delay(error))
                    self._save(state, identity)
                    return False
            state.update(delivery='sent', sent_text=text, retry_at=0, updated_at=self.delivery.clock())
            self._save(state, identity)
            return bool(self._allowed(identity))
        except Exception:
            return False  # Optional preview failures cannot affect task delivery.

    def finish(self, identity):
        """Queue only this turn's known temporary preview after its final ACK."""
        try:
            identity = self._identity(identity)
            if not identity or not self._current(identity):
                return
            state = self.load(identity)
            if not state:
                return
            state['finished'] = True
            message_id = state.get('message_id')
            if message_id and not state.get('cleanup_state'):
                state.update(cleanup_message_id=message_id, cleanup_state='queued', cleanup_retry_at=0)
            self._save(state, identity)
        except Exception:
            return  # Final acknowledgement must remain successful.

    def _final_sent(self, identity):
        return bool(self.store.db.execute(
            'SELECT 1 FROM completion_turns g JOIN outbox o ON o.key=g.notice_key '
            'WHERE g.native_id=? AND g.chat_id=? AND g.topic_id=? AND g.turn_id=? '
            "AND g.notice_state='sent' AND o.message_id>0 "
            'AND o.chat_id=g.chat_id AND o.message_thread_id=g.topic_id LIMIT 1',
            (*self._route(identity), identity['turn_id'])).fetchone())

    async def cleanup(self, telegram):
        """Delete known temporary IDs safely, including after a style change."""
        try:
            if not self.delivery.bridge.owner_allowed():
                return
            rows = self.store.db.execute('SELECT key,value FROM kv WHERE key LIKE ?', (self.KEY + '%',)).fetchall()
        except Exception:
            return
        attempted = 0
        for key, value in rows:
            try:
                state = json.loads(value)
                identity = self._identity(state['desired_identity'])
                if (not identity or key != self._key(identity) or state.get('route') != self._route(identity)
                        or not self._current(identity)):
                    continue
                if not state.get('finished') and state.get('message_id') and self._final_sent(identity):
                    # Recover a crash after the canonical final ACK but before finish().
                    self.finish(identity)
                    state = self.load(identity)
                mid = state.get('cleanup_message_id')
                if (not state.get('finished')
                        or state.get('cleanup_state') not in ('queued', 'deleting')
                        or not isinstance(mid, int) or isinstance(mid, bool) or mid <= 0
                        or mid != state.get('message_id')
                        or self.delivery.clock() < state.get('cleanup_retry_at', 0)):
                    continue
                if self.store.db.execute('SELECT 1 FROM outbox WHERE chat_id=? AND message_thread_id=? '
                                         'AND message_id=? LIMIT 1',
                                         (identity['chat_id'], identity['topic_id'], mid)).fetchone():
                    state.update(cleanup_state='failed', cleanup_retry_at=0)
                    self._save(state, identity)
                    continue  # Never delete a canonical delivered output.
                if attempted >= 5:
                    return
                attempted += 1
                state['cleanup_state'] = 'deleting'
                self._save(state, identity)
                try:
                    result = await telegram.call('deleteMessage', chat_id=int(identity['chat_id']), message_id=mid)
                    if result is not True:
                        raise ConnectionError('Preview deletion acknowledgement unavailable')
                except Exception as error:
                    code = getattr(error, 'code', None)
                    absent = code == 400 and any(s in str(error).lower() for s in
                                                ('message to delete not found', 'message_id_invalid'))
                    state['cleanup_state'] = ('deleted' if absent else
                                              'failed' if code in (400, 403, 404) else 'queued')
                    state['cleanup_retry_at'] = (self.delivery.clock() + self._delay(error)
                                                 if state['cleanup_state'] == 'queued' else 0)
                else:
                    state.update(cleanup_state='deleted', cleanup_retry_at=0)
                self._save(state, identity)
            except Exception:
                continue
