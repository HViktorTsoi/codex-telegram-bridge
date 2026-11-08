"""One durable status message per frozen route, edited beside the body draft."""
import hashlib
import json


class CompletionStatus:
    KEY = 'completion_status_v1'

    def __init__(self, delivery):
        self.delivery, self.store = delivery, delivery.store

    def _route(self, identity=None):
        if identity is None:
            return self.delivery.route()
        return [identity['native_id'], str(identity['chat_id']), int(identity['topic_id'])]

    def _key(self, identity=None):
        digest = hashlib.sha256(json.dumps(self._route(identity)).encode()).hexdigest()[:24]
        return self.KEY + ':' + digest

    def load(self, identity=None):
        return json.loads(self.store.get(self._key(identity), '{}'))

    def save(self, state, identity=None):
        if identity is None and state.get('route'):
            native, chat, topic = state['route']
            identity = {'native_id': native, 'chat_id': chat, 'topic_id': topic}
        self.store.set(self._key(identity), json.dumps(state, ensure_ascii=False))

    def _allowed(self, identity):
        return (self.delivery.enabled and self.delivery.preview_style == 'status-card'
                and self.delivery.bridge.owner_allowed() and self.delivery._current(identity))

    def _stale(self, identity):
        source = self.delivery._latest_receipt_source()
        if source is None or source['state'] not in ('pending', 'dispatching', 'uncertain', 'running'):
            return False
        if identity.get('source_id') == source['id']:
            return False
        return source['turn_id'] != identity.get('turn_id')

    async def update(self, telegram, identity, text, *, terminal=False):
        if not self._allowed(identity):
            return False
        state = self.load(identity)
        if state.get('delivery') == 'creating' and not state.get('message_id'):
            state['delivery'] = 'uncertain'
        state.update(route=self._route(identity), desired_text=text,
                     desired_identity=dict(identity), terminal=terminal)
        self.save(state, identity)
        return await self.flush(telegram)

    async def flush(self, telegram):
        state = self.load()
        identity = state.get('desired_identity')
        if not identity or not self._allowed(identity) or self._stale(identity):
            return False
        message_id = state.get('message_id')
        if state.get('delivery') == 'failed':
            return False  # A deleted message or permanent permission error needs explicit repair.
        if not message_id and state.get('delivery') in ('creating', 'uncertain', 'failed'):
            if state.get('delivery') == 'creating':
                state['delivery'] = 'uncertain'
                self.save(state, identity)
            return False
        if self.delivery.clock() < state.get('retry_at', 0):
            return False
        if message_id and state.get('sent_text') == state['desired_text']:
            return True
        state['delivery'] = 'editing' if message_id else 'creating'
        self.save(state, identity)  # A lost creation acknowledgement must never create a second card.
        route = {'chat_id': int(identity['chat_id'])}
        if message_id:
            route['message_id'] = message_id
        else:
            route.update(message_thread_id=identity['topic_id'], disable_notification=True)
        try:
            result = await telegram.call('editMessageText' if message_id else 'sendMessage',
                                         **route, text=state['desired_text'])
            if not message_id:
                if not isinstance(result, dict) or not result.get('message_id'):
                    raise ConnectionError('Status creation acknowledgement unavailable')
                state['message_id'] = result['message_id']
        except Exception as error:
            code = getattr(error, 'code', None)
            if message_id and code == 400 and 'message is not modified' in str(error).lower():
                pass
            else:
                state['retry_at'] = self.delivery.clock() + max(15, getattr(error, 'retry_after', 0))
                state['delivery'] = ('queued' if code == 429 else
                                     'failed' if code in (400, 403, 404) else
                                     'queued' if message_id else 'uncertain')
                self.save(state, identity)
                return False
        state.update(delivery='sent', sent_text=state['desired_text'], updated_at=self.delivery.clock(), retry_at=0)
        self.save(state, identity)
        return self._allowed(identity)
