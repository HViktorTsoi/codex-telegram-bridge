"""Durable per-turn status cards, optionally following new topic content."""
import html
import json
import logging
import time

from message_format import formatting_error

LOG = logging.getLogger('codex_telegram_activity')


class TopicActivity:
    KEY = 'topic_activity_v1'
    INTERVAL = 15
    STYLE_VERSION = 3

    def __init__(self, store, clock=time.time, position='inline'):
        self.store, self.clock, self.position = store, clock, position

    def load(self):
        return json.loads(self.store.get(self.KEY, '{}'))

    def save(self, state):
        self.store.set(self.KEY, json.dumps(state, ensure_ascii=False))

    @property
    def open(self):
        return bool(self.load().get('open'))

    def tail(self, state):
        """Observe delivered content in this topic, excluding status cards.

        Closed cards follow only their own turn's late output, so unrelated later
        messages cannot resurrect an old completed task.
        """
        topic_id = state['route'][1]
        query = 'SELECT MAX(message_id) FROM outbox WHERE message_thread_id=? AND message_id>0'
        args = [topic_id]
        if not state.get('open'):
            query += ' AND substr(key,1,?)=?'
            prefix = 'turn:' + state['turn_id'] + ':'
            args.extend([len(prefix), prefix])
        last = self.store.db.execute(query, args).fetchone()[0] or 0
        if state.get('open') and hasattr(self.store, 'turn_routes'):
            incoming = self.store.db.execute('SELECT MAX(source_message_id) FROM inbox WHERE message_thread_id=?',
                                              (topic_id,)).fetchone()[0] or 0
            last = max(last, incoming)
        return last

    async def cleanup(self, telegram, state):
        if self.clock() < state.get('cleanup_retry_at', 0):
            return
        for old in list(state.get('cleanup', []))[:5]:
            try:
                await telegram.call('deleteMessage', chat_id=old['chat_id'], message_id=old['message_id'])
            except Exception as error:
                code = getattr(error, 'code', None)
                if code not in (400, 403):
                    state['cleanup_retry_at'] = self.clock() + max(15, getattr(error, 'retry_after', 0))
                    self.save(state)
                    return  # Deletion can safely retry; never repeat the new send.
                if not any(word in str(error).lower() for word in ('message to delete not found', 'message_id_invalid')):
                    LOG.warning('Previous status card cannot be deleted (%s)', code)
            state['cleanup'].remove(old)
            state['cleanup_retry_at'] = 0
            self.save(state)

    async def update(self, telegram, *, chat_id, topic_id, name, turns=None, status=None, connected=True):
        now, state = self.clock(), self.load()
        route = [str(chat_id), int(topic_id)]
        if state.get('cleanup'):
            await self.cleanup(telegram, state)
        if state and state.get('route') != route:
            # A repaired topic gets a new message; never edit/reuse the old route.
            state = {}
        active = next((t for t in turns or [] if t.get('status') == 'inProgress'), None)
        if connected and active and active['id'] != state.get('turn_id'):
            if state.get('open'):
                # Finish the previous card before a quickly-following turn replaces it.
                await self.update(telegram, chat_id=chat_id, topic_id=topic_id, name=name,
                                  turns=[t for t in turns if t['id'] != active['id']], status=status)
                state = self.load()
            started = active.get('startedAt')
            state = {'turn_id': active['id'], 'route': route, 'open': True,
                     'started_at': started if isinstance(started, (int, float)) else now,
                     'delivery': 'new', 'cleanup': state.get('cleanup', [])}
            self.save(state)
        if state.get('open') and self.position == 'bottom' and not state.get('bottom_tracking'):
            state['bottom_tracking'] = True
            self.save(state)
        move = (self.position == 'bottom' and state.get('bottom_tracking') and state.get('message_id')
                and state.get('delivery') not in ('creating', 'uncertain', 'failed')
                and self.tail(state) > state['message_id'])
        restyle = (not state.get('open') and state.get('message_id')
                   and state.get('phase') in ('completed', 'failed', 'interrupted')
                   and state.get('style_version') != self.STYLE_VERSION and state.get('delivery') != 'failed')
        if not state or (not state.get('open') and not restyle and not move):
            return
        if restyle or not state.get('open'):
            phase = state['phase']  # Styling an old card must not change its known terminal state.
        elif not connected:
            phase = 'unknown'
        elif active and active['id'] == state['turn_id']:
            flags = (status or {}).get('activeFlags', [])
            phase = 'waiting' if any(f in flags for f in ('waitingOnApproval', 'waitingOnUserInput')) else 'working'
            if (status or {}).get('type') == 'systemError':
                phase = 'unknown'
        else:
            turn = next((t for t in turns or [] if t.get('id') == state['turn_id']), None)
            phase = (turn or {}).get('status')
            if phase not in ('completed', 'failed', 'interrupted'):
                phase = 'unknown'  # Missing history is not proof of completion.
        terminal = phase in ('completed', 'failed', 'interrupted')
        if state.get('finished_at') is None and terminal:
            turn = next((t for t in turns or [] if t.get('id') == state['turn_id']), {})
            state['finished_at'] = turn.get('completedAt') or now
        elapsed = max(0, int((state.get('finished_at') if terminal else now) - state['started_at']))
        minutes, seconds = divmod(elapsed, 60)
        duration = f'{minutes}分{seconds}秒' if minutes else f'{seconds}秒'
        heading = {'working': '⏳ 正在工作', 'waiting': '🙋 需要你处理',
                   'unknown': '⚠️ 状态待确认', 'completed': '✅ 已完成',
                   'failed': '❌ 执行失败', 'interrupted': '⏹ 已停止'}[phase]
        # The topic title already identifies the session; keep the card to two lines.
        hint = ' · 去Codex' if phase == 'waiting' else ''
        update_label = '' if phase == 'waiting' else '更新 '
        display_time = state.get('updated_at', now) if restyle else now
        text = f'━━━━ {heading} ━━━━\n{duration} · {update_label}' + time.strftime('%H:%M:%S', time.localtime(display_time)) + hint
        if now < state.get('retry_at', 0):
            return
        if (phase == state.get('phase') and state.get('style_version') == self.STYLE_VERSION
                and now - state.get('updated_at', 0) < self.INTERVAL):
            return
        message_id = state.get('message_id')
        if not message_id and ((terminal and not state.get('replaces'))
                               or state.get('delivery') in ('creating', 'uncertain', 'failed')):
            # A lost send response must not cause duplicate status messages.
            if terminal:
                state.update(open=False, phase=phase)
                self.save(state)
            return
        if message_id and state.get('delivery') == 'failed':
            if terminal:
                state.update(open=False, phase=phase)
                self.save(state)
            return
        if move:
            # Save intent before send. A crash/lost response keeps the old card and
            # does not cause another replacement send after reconnect.
            state['replaces'] = {'chat_id': route[0], 'message_id': message_id}
            state.pop('message_id', None)
            message_id = None
        if not message_id:
            state['delivery'] = 'creating'
            self.save(state)
        try:
            result = await self.render(telegram, state, text)
            if not message_id:
                state['message_id'] = result['message_id']
        except Exception as error:
            code = getattr(error, 'code', None)
            if message_id and code == 400 and 'message is not modified' in str(error).lower():
                pass
            else:
                state['retry_at'] = now + max(15, getattr(error, 'retry_after', 0))
                if code == 429:
                    state['delivery'] = 'retry'
                elif code in (400, 403):
                    state['delivery'] = 'failed'
                elif not message_id:
                    state['delivery'] = 'uncertain'
                self.save(state)
                raise
        state.update(delivery='sent', phase=phase, updated_at=now, retry_at=0, open=not terminal, style_version=self.STYLE_VERSION)
        if state.get('replaces'):
            state.setdefault('cleanup', []).append(state.pop('replaces'))
        self.save(state)
        if state.get('cleanup'):
            await self.cleanup(telegram, state)

    async def render(self, telegram, state, text):
        message_id = state.get('message_id')
        route = {'chat_id': state['route'][0]}
        if message_id:
            route['message_id'] = message_id
        else:
            route.update(message_thread_id=state['route'][1], disable_notification=True)
        if state.get('style') != 'plain' and getattr(telegram, 'rich_supported', None) is not False:
            first, second = text.split('\n', 1)
            rich = {'html': '<p><b>' + html.escape(first) + '</b><br>' + html.escape(second) + '</p>'}
            try:
                result = await telegram.call('editMessageText' if message_id else 'sendRichMessage',
                                             **route, rich_message=rich)
                state['style'] = 'bold'
                return result
            except Exception as error:
                # Only definite format rejection can fall back; never resend on a lost response.
                if not formatting_error(error):
                    raise
                state['style'] = 'plain'
                self.save(state)
        return await telegram.call('editMessageText' if message_id else 'sendMessage', **route, text=text)
