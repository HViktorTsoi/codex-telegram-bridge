"""Durable temporary body previews beside a single live status draft."""
import hashlib
import json

from message_format import formatting_error, normalize_local_links, utf16_size


STOP_REQUEST_KEY = 'status_stop_v1:'


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
        task = [identity['turn_id']] if identity.get('turn_id') else ['source', identity['source_id']]
        frozen = self._route(identity) + task
        return self.KEY + hashlib.sha256(json.dumps(frozen).encode()).hexdigest()[:24]

    def load(self, identity):
        identity = self._bar_identity(identity)
        if not identity:
            return {}
        key, seen = self._key(identity), set()
        while key not in seen:
            seen.add(key)
            state = json.loads(self.store.get(key, '{}'))
            if state.get('route') and state['route'] != self._route(identity):
                return {}
            stored = self._bar_identity(state['desired_identity']) if state.get('desired_identity') else None
            if stored and (self._route(stored) != self._route(identity) or self._key(stored) != key):
                return {}
            target = state.get('transferred_to')
            if not target:
                return state
            if not target.startswith(self.KEY):
                return {}
            key = target
        return {}

    @staticmethod
    def _bar_identity(identity):
        value = dict(identity)
        value.update(chat_id=str(value['chat_id']), topic_id=int(value['topic_id']))
        if not value.get('native_id') or (not value.get('turn_id') and value.get('source_id') is None):
            return None
        return value

    def _save(self, state, identity):
        self.store.set(self._key(identity), json.dumps(state, ensure_ascii=False))

    def _current(self, identity):
        return self.delivery.bridge.owner_allowed() and self.delivery._current(identity)

    def _allowed(self, identity):
        if self.delivery.preview_style == 'status-bar':
            return self._bar_allowed(identity)
        return (self.delivery.enabled and self.delivery.preview_style == 'status-draft'
                and self._current(identity) and not self.delivery.status_card._stale(identity))

    def _bar_allowed(self, identity):
        if (not self.delivery.enabled or self.delivery.preview_style != 'status-bar'
                or not self._current(identity) or self.delivery.status_card._stale(identity)):
            return False
        if identity.get('source_id') is not None:
            source = self._source(identity)
            if source is None:
                return False
            if identity.get('turn_id') and source['turn_id'] != identity['turn_id']:
                return False
            latest = self.delivery._latest_receipt_source()
            if not identity.get('turn_id') and latest is not None and latest['id'] != source['id']:
                return False
        if identity.get('turn_id'):
            group = self.store.db.execute('SELECT rowid AS ordering_id,* FROM completion_turns '
                                         'WHERE native_id=? AND chat_id=? AND topic_id=? AND turn_id=?',
                                         (*self._route(identity), identity['turn_id'])).fetchone()
            if group is None:
                return False
            newer = self.store.db.execute(
                'SELECT 1 FROM completion_turns WHERE native_id=? AND chat_id=? AND topic_id=? '
                'AND turn_id<>? AND notify=1 AND (started_at>? OR (started_at=? AND rowid>?)) LIMIT 1',
                (*self._route(identity), identity['turn_id'], group['started_at'], group['started_at'],
                 group['ordering_id'])).fetchone()
            if newer:
                return False
        return True

    @staticmethod
    def status_markup(status, stop_callback=None):
        lines = [line.strip(' ━═─=\t') for line in status.splitlines() if line.strip(' ━═─=\t')]
        lines = (lines + ['任务已收到。', '处理中。'])[:2]
        rows = [[{'text': line, 'disabled': {}}] for line in lines]
        if stop_callback:
            rows[1].append({'text': '⏹ 停止当前任务', 'callback_data': stop_callback})
        return {'inline_keyboard': rows}

    def stop_callback(self, identity):
        """Expose a control only for this exact current, active native turn."""
        try:
            identity = self._bar_identity(identity)
            if not identity or not identity.get('turn_id') or not self._bar_allowed(identity):
                return None
            group = self.delivery._group(identity['turn_id'], identity['chat_id'], identity['topic_id'])
            if (not group or group['native_id'] != identity['native_id']
                    or group['stage'] != 'active' or group['status'] != 'inProgress'):
                return None
            preview = self.load(identity)
            if preview.get('finished') or preview.get('closed'):
                return None
            digest = self._key(identity)[-24:]
            request = json.loads(self.store.get(STOP_REQUEST_KEY + digest, '{}'))
            if request.get('state') in ('sending', 'requested', 'uncertain'):
                return None
            return 'stop:' + digest
        except (KeyError, TypeError, ValueError, AttributeError):
            return None  # Malformed identity or durable state cannot expose a control.

    def _source(self, identity):
        return self.store.db.execute('SELECT * FROM inbox WHERE id=? AND chat_id=? AND message_thread_id=?',
                                     (identity.get('source_id'), identity['chat_id'], identity['topic_id'])).fetchone()

    def _adopt(self, identity):
        """Move one pending creation intent into its real source/turn atomically."""
        key, route = self._key(identity), self._route(identity)
        existing = json.loads(self.store.get(key, '{}'))
        if existing:
            return
        candidates = []
        for old_key, value in self.store.db.execute('SELECT key,value FROM kv WHERE key LIKE ?',
                                                   (self.KEY + '%',)).fetchall():
            state = json.loads(value)
            original = state.get('desired_identity', {})
            if (state.get('route') != route or original.get('turn_id') or state.get('transferred_to')
                    or state.get('closed') or state.get('finished') or original.get('source_id') is None):
                continue
            if self._route(original) != route or old_key != self._key(original):
                continue
            source = self._source(original)
            if source is None:
                continue
            if identity.get('turn_id'):
                matched = source['turn_id'] == identity['turn_id']
            else:
                matched = not source['turn_id'] and source['state'] in ('pending', 'dispatching', 'uncertain')
            if matched:
                candidates.append((source['received_ms'], source['id'], old_key, state))
        if not candidates:
            return
        _, _, old_key, state = max(candidates, key=lambda candidate: candidate[:2])
        old_state = dict(state)
        state.update(desired_identity=dict(identity), turn_id=identity.get('turn_id'),
                     source_id=identity.get('source_id'))
        old_state['transferred_to'] = key
        with self.store.db:
            self.store.db.execute('INSERT OR REPLACE INTO kv VALUES(?,?)',
                                  (key, json.dumps(state, ensure_ascii=False)))
            self.store.db.execute('INSERT OR REPLACE INTO kv VALUES(?,?)',
                                  (old_key, json.dumps(old_state, ensure_ascii=False)))

    async def update_status(self, telegram, identity, status, body, *, rich_body=None):
        """Render two durable status rows, with a bound active-turn control."""
        try:
            identity = self._bar_identity(identity)
            if not identity:
                return False
            source = self._source(identity) if identity.get('source_id') is not None else None
            if not identity.get('turn_id') and source is not None and source['turn_id']:
                group = self.store.db.execute('SELECT 1 FROM completion_turns WHERE native_id=? AND chat_id=? '
                                              'AND topic_id=? AND turn_id=?',
                                              (*self._route(identity), source['turn_id'])).fetchone()
                if group:
                    identity['turn_id'] = source['turn_id']
            if (identity.get('turn_id') and source is not None and not source['turn_id']
                    and source['state'] in ('pending', 'dispatching', 'uncertain')):
                return False  # A native fallback cannot steal a newer unassigned receipt.
            if not self._bar_allowed(identity):
                return False
            self._adopt(identity)
            state = self.load(identity)
            if not body or not body.strip():
                body = state.get('desired_text') or state.get('sent_text') or '任务已收到。'
                rich_body = state.get('desired_display_text')
            markup = self.status_markup(status, self.stop_callback(identity))
            return await self.update(telegram, identity, body, reply_markup=markup, _status_bar=True,
                                     rich_body=rich_body)
        except Exception:
            return False

    def _delay(self, error):
        return max(15, getattr(error, 'retry_after', 0) or 0)

    def _text(self, body):
        limit = self.delivery.LIMIT
        if utf16_size(body) <= limit:
            return body
        note = '（过程较长，预览仅显示最近部分；完整过程将在完成后发送。）\n\n'
        return note + self.delivery._tail(body, limit - utf16_size(note))

    def _render_request(self, telegram):
        rich = (self.store.get('output_format', 'rich') == 'rich'
                and getattr(telegram, 'rich_supported', None) is not False)
        style = self.delivery.preview_body_style if rich else 'normal'
        return style, 'rich' if rich else 'plain'

    @staticmethod
    def _not_modified(error, message_id):
        return bool(message_id and getattr(error, 'code', None) == 400
                    and 'message is not modified' in str(error).lower())

    async def _render(self, telegram, identity, state, text, reply_markup=None, request=None):
        """Return the ACK, applied style and frozen render request for caching."""
        request = self._render_request(telegram) if request is None else request
        body_style, body_format = request
        message_id = state.get('message_id')
        route = {'chat_id': int(identity['chat_id'])}
        if message_id:
            route['message_id'] = message_id
        else:
            route.update(message_thread_id=identity['topic_id'], disable_notification=True)
        if reply_markup is not None:
            route['reply_markup'] = reply_markup
        if body_format == 'rich':
            display = state.get('desired_display_text')
            markdown = normalize_local_links(text if display is None else display)
            applied_style = body_style
            rich_message = {'markdown': markdown}
            if body_style == 'quote':
                rich_message = {'markdown': '\n'.join('> ' + line for line in markdown.split('\n'))}
            elif body_style == 'footer':
                try:
                    from preview_footer import render_footer_markdown
                    rich_message = {'html': render_footer_markdown(markdown)}
                except Exception:
                    # This boundary only formats local text, before any API
                    # request. A renderer failure must not become an unknown
                    # message creation or block ordinary output.
                    applied_style = 'normal'
            try:
                result = await telegram.call('editMessageText' if message_id else 'sendRichMessage',
                                             **route, rich_message=rich_message)
                return result, applied_style, request
            except Exception as error:
                if self._not_modified(error, message_id):
                    return True, applied_style, request
                # Only a known formatting rejection permits a second request.
                if not formatting_error(error) or (message_id and getattr(error, 'code', None) == 404):
                    raise
                if not message_id and getattr(error, 'code', None) == 404:
                    telegram.rich_supported = False
                    request = ('normal', 'plain')
                if not self._allowed(identity):
                    raise ConnectionError('Preview route or access changed during formatting fallback')
        try:
            result = await telegram.call('editMessageText' if message_id else 'sendMessage', **route, text=text)
        except Exception as error:
            if not self._not_modified(error, message_id):
                raise
            result = True
        return result, 'normal', request

    async def update(self, telegram, identity, body, *, reply_markup=None, _status_bar=False, rich_body=None):
        """Create quietly once, then edit only the known preview message ID."""
        try:
            identity = self._bar_identity(identity) if _status_bar else self._identity(identity)
            if (not identity or not body or not body.strip() or not self._allowed(identity)
                    or (not _status_bar and self.delivery.preview_style != 'status-draft')):
                return False
            state = self.load(identity)
            if state.get('finished'):
                return False
            message_id = state.get('message_id')
            if not message_id and state.get('delivery') == 'creating':
                state['delivery'] = 'uncertain'
                self._save(state, identity)
            if _status_bar:
                source = self._source(identity) if identity.get('source_id') is not None else None
                state.update(route=self._route(identity), turn_id=identity.get('turn_id'),
                             desired_identity=identity, desired_text=self._text(body), desired_markup=reply_markup,
                             source_id=identity.get('source_id'),
                             closed=bool(source is not None and not source['turn_id'] and source['state'] == 'failed'))
                self._save(state, identity)
            if (state.get('delivery') == 'failed'
                    or (not message_id and state.get('delivery') == 'uncertain')):
                return False
            text = self._text(body)
            state.update(route=self._route(identity), turn_id=identity.get('turn_id'),
                         desired_identity=identity, desired_text=text, desired_display_text=rich_body)
            if _status_bar:
                state['desired_markup'] = reply_markup
            self._save(state, identity)
            if self.delivery.clock() < state.get('retry_at', 0):
                return False
            request = self._render_request(telegram)
            display = rich_body if rich_body is not None and request[1] == 'rich' else text
            same_body = (message_id and state.get('sent_text') == text
                         and state.get('sent_display_text', state.get('sent_text')) == display
                         and state.get('sent_body_style_requested', 'normal') == request[0]
                         and state.get('sent_body_format_requested', 'rich') == request[1])
            if same_body and (not _status_bar or state.get('sent_markup') == reply_markup):
                return True
            state['delivery'] = 'editing' if message_id else 'creating'
            self._save(state, identity)  # Unknown creation must never be replayed.
            applied_style = state.get('sent_body_style', 'normal')
            try:
                if same_body and _status_bar:
                    result = await telegram.call('editMessageReplyMarkup', chat_id=int(identity['chat_id']),
                                                 message_id=message_id, reply_markup=reply_markup)
                else:
                    result, applied_style, request = await self._render(telegram, identity, state, text,
                                                                       reply_markup, request)
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
            state.update(sent_body_style=applied_style, sent_body_style_requested=request[0],
                         sent_body_format_requested=request[1],
                         sent_display_text=rich_body if rich_body is not None and request[1] == 'rich' else text)
            if _status_bar:
                state['sent_markup'] = reply_markup
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
            if self.store.db.execute('SELECT 1 FROM completion_turns WHERE native_id=? AND chat_id=? '
                                     'AND topic_id=? AND turn_id=?',
                                     (*self._route(identity), identity['turn_id'])).fetchone():
                self._adopt(identity)  # A fast final ACK may arrive before the first native preview.
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
                # Another record in this scan may already have adopted this source.
                state = json.loads(self.store.get(key, '{}'))
                identity = self._bar_identity(state['desired_identity'])
                if (not identity or key != self._key(identity) or state.get('route') != self._route(identity)
                        or state.get('transferred_to') or not self._current(identity)):
                    continue
                if not identity.get('turn_id'):
                    source = self._source(identity)
                    if state.get('closed') or source is None or not source['turn_id']:
                        continue
                    native = dict(identity, turn_id=source['turn_id'])
                    if not self._final_sent(native):
                        continue
                    self._adopt(native)
                    self.finish(native)
                    identity, state = native, self.load(native)
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
