"""One editable, durable status message per observed native turn and topic."""
import json
import time


class TopicActivity:
    KEY = 'topic_activity_v1'
    INTERVAL = 15

    def __init__(self, store, clock=time.time):
        self.store, self.clock = store, clock

    def load(self):
        return json.loads(self.store.get(self.KEY, '{}'))

    def save(self, state):
        self.store.set(self.KEY, json.dumps(state, ensure_ascii=False))

    @property
    def open(self):
        return bool(self.load().get('open'))

    async def update(self, telegram, *, chat_id, topic_id, name, turns=None, status=None, connected=True):
        now, state = self.clock(), self.load()
        route = [str(chat_id), int(topic_id)]
        if state and state.get('route') != route:
            # A repaired topic gets a new message; never edit/reuse the old route.
            state = {}
        active = next((t for t in turns or [] if t.get('status') == 'inProgress'), None)
        if connected and active and active['id'] != state.get('turn_id'):
            if state.get('open'):
                # Finish the previous card before a quickly-following turn replaces it.
                await self.update(telegram, chat_id=chat_id, topic_id=topic_id, name=name,
                                  turns=[t for t in turns if t['id'] != active['id']], status=status)
            started = active.get('startedAt')
            state = {'turn_id': active['id'], 'route': route, 'open': True,
                     'started_at': started if isinstance(started, (int, float)) else now,
                     'delivery': 'new'}
            self.save(state)
        if not state or not state.get('open'):
            return
        if not connected:
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
        duration = f'{minutes} 分 {seconds} 秒' if minutes else f'{seconds} 秒'
        heading = {'working': '⏳ Codex 正在工作', 'waiting': '🙋 Codex 需要你处理',
                   'unknown': '⚠️ 连接或会话状态待确认', 'completed': '✅ 本轮已完成',
                   'failed': '❌ 本轮执行失败', 'interrupted': '⏹ 本轮已停止'}[phase]
        text = f'{heading}\n会话：{str(name)[:128]}\n' + ('用时：' if terminal else '已用时：') + duration
        if phase == 'waiting':
            text += '\n请到原 Codex 会话查看待处理问题或审批。'
        elif phase == 'unknown':
            text += '\n尚未确认任务结束，正在等待恢复状态。'
        text += '\n状态更新：' + time.strftime('%H:%M:%S', time.localtime(now))
        if now < state.get('retry_at', 0):
            return
        if phase == state.get('phase') and now - state.get('updated_at', 0) < self.INTERVAL:
            return
        message_id = state.get('message_id')
        if not message_id and (terminal or state.get('delivery') in ('creating', 'uncertain', 'failed')):
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
        if not message_id:
            state['delivery'] = 'creating'
            self.save(state)
        try:
            if message_id:
                result = await telegram.call('editMessageText', chat_id=state['route'][0],
                                             message_id=message_id, text=text)
            else:
                result = await telegram.call('sendMessage', chat_id=state['route'][0],
                                             message_thread_id=state['route'][1],
                                             text=text, disable_notification=True)
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
        state.update(delivery='sent', phase=phase, updated_at=now, retry_at=0, open=not terminal)
        self.save(state)
