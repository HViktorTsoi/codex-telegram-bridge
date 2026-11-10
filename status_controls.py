"""Owner-only, exact-turn stop controls beside persistent status bars."""
import asyncio
import json
import logging
import re
import time

from bridge import RpcError, TelegramError, permitted
from completion_preview import CompletionPreview, STOP_REQUEST_KEY


LOG = logging.getLogger('codex_telegram_status_controls')


class StatusControls:
    PREFIX = 'stop:'
    RPC_TIMEOUT = 12

    def __init__(self, router):
        self.router, self.db = router, router.store.db
        self.locks = {}
        self.db.execute('''CREATE TABLE IF NOT EXISTS status_stop_jobs(
            id INTEGER PRIMARY KEY,payload TEXT,state TEXT DEFAULT 'pending',error TEXT)''')
        self.db.commit()

    def _authorized(self, callback):
        message = callback.get('message') or {}
        identity = {'from': callback.get('from') or {}, 'chat': message.get('chat') or {}}
        return (permitted(identity, self.router.access(), self.router.owner)
                and bool(self.router.bot_id)
                and message.get('from', {}).get('id') == self.router.bot_id
                and message.get('date') != 0)

    def ingest(self, update):
        callback = update.get('callback_query') or {}
        data = callback.get('data', '')
        if not isinstance(data, str) or not data.startswith(self.PREFIX):
            return False
        with self.db:
            if self._authorized(callback):
                self.db.execute('INSERT OR IGNORE INTO status_stop_jobs(id,payload) VALUES(?,?)',
                                (update['update_id'], json.dumps(update, ensure_ascii=False)))
            self.db.execute("UPDATE kv SET value=? WHERE key='offset'",
                            (str(max(self.router.store.offset(), update['update_id'] + 1)),))
        return True

    def _resolve(self, callback):
        """Read the exact displayed preview; never follow an adopted alias."""
        if not self._authorized(callback) or not self.router.owner_allowed():
            return None
        data, message = callback.get('data', ''), callback.get('message') or {}
        if not re.fullmatch(r'stop:[0-9a-f]{24}', data):
            return None
        topic, mid = message.get('message_thread_id'), message.get('message_id')
        if (not isinstance(topic, int) or isinstance(topic, bool) or topic <= 0
                or not isinstance(mid, int) or isinstance(mid, bool) or mid <= 0):
            return None
        target = self.router.by_topic.get(topic)
        if not target or target.get('topic_missing'):
            return None
        bridge = self.router.bridges.get(target['key'])
        current = self.router.targets.get(target['key'])
        if (not bridge or not current or not bridge.owner_allowed()
                or current.get('topic_missing') or bridge.target.get('topic_missing')
                or current['thread_id'] != bridge.thread_id
                or bridge.target['thread_id'] != bridge.thread_id
                or current['message_thread_id'] != topic or bridge.target['message_thread_id'] != topic
                or not bridge.delivery.enabled or bridge.delivery.preview_style != 'status-bar'):
            return None
        preview = bridge.delivery.body_preview
        state = json.loads(bridge.store.get(CompletionPreview.KEY + data[len(self.PREFIX):], '{}'))
        try:
            identity = preview._bar_identity(state.get('desired_identity', {}))
        except (KeyError, TypeError, ValueError):
            return None
        if (not identity or not identity.get('turn_id') or state.get('finished') or state.get('closed')
                or state.get('transferred_to') or state.get('message_id') != mid
                or preview._key(identity) != CompletionPreview.KEY + data[len(self.PREFIX):]
                or state.get('route') != [bridge.thread_id, str(message['chat']['id']), topic]
                or preview._route(identity) != state.get('route') or not preview._bar_allowed(identity)):
            return None
        group = bridge.delivery._group(identity['turn_id'], identity['chat_id'], identity['topic_id'])
        if (not group or group['native_id'] != bridge.thread_id or group['stage'] != 'active'
                or group['status'] != 'inProgress'):
            return None
        displayed = [button.get('callback_data') for row in state.get('sent_markup', {}).get('inline_keyboard', [])
                     for button in row]
        request = json.loads(bridge.store.get(STOP_REQUEST_KEY + data[len(self.PREFIX):], '{}'))
        if data not in displayed and request.get('state') not in ('sending', 'requested', 'uncertain'):
            return None
        return bridge, identity, data[len(self.PREFIX):]

    async def _answer(self, callback, text, alert=False):
        if not self._authorized(callback):
            return
        try:
            await self.router.telegram.call('answerCallbackQuery', callback_query_id=callback['id'],
                                            text=text, show_alert=alert)
        except (TelegramError, ConnectionError, asyncio.TimeoutError, OSError):
            pass  # Expired callback acknowledgements cannot change task execution.

    def _notice(self, job, bridge, identity, text):
        # A late response may explain an old action, but must never move to a
        # repaired topic or another native session.
        current = self.router.bridges.get(bridge.target['key'])
        if (current is bridge and self.router.owner_allowed() and bridge.owner_allowed()
                and bridge.delivery._current(identity)):
            bridge.store.enqueue_at('stop-result:' + str(job['id']), identity['chat_id'], text, identity['topic_id'])

    @staticmethod
    def _refresh(bridge):
        bridge.delivery.reschedule_receipt()
        bridge.receipt_event.set()

    def _finish(self, job, state='done', error=None):
        with self.db:
            self.db.execute('UPDATE status_stop_jobs SET state=?,error=? WHERE id=?', (state, error, job['id']))

    async def _process(self, job, callback):
        resolved = self._resolve(callback)
        if resolved is None:
            await self._answer(callback, '这个停止按钮已失效，未停止任何任务。', True)
            self._finish(job)
            return
        bridge, identity, digest = resolved
        request_key = STOP_REQUEST_KEY + digest
        previous = json.loads(bridge.store.get(request_key, '{}'))
        if previous.get('state') in ('sending', 'requested', 'uncertain'):
            text = ('停止请求结果待核对，不会重复发送。' if previous['state'] in ('sending', 'uncertain')
                    else '已请求停止这条任务。')
            await self._answer(callback, text, previous['state'] != 'requested')
            self._finish(job)
            return
        if not bridge.ready.is_set():
            await self._answer(callback, 'Codex 尚未连接，请稍后再点。', True)
            self._finish(job)
            return
        await self._answer(callback, '正在核对并停止这条任务…')
        try:
            turns = await asyncio.wait_for(bridge.codex.turns(limit=1), self.RPC_TIMEOUT)
        except (ConnectionError, RpcError, asyncio.TimeoutError, OSError):
            self._notice(job, bridge, identity, '暂时无法读取任务状态，尚未发送停止请求；请稍后再点。')
            self._finish(job, 'failed', 'NativeReadUnavailable')
            return
        fresh = self._resolve(callback)
        frozen_fields = ('native_id', 'chat_id', 'topic_id', 'turn_id')
        if (fresh is None or fresh[0] is not bridge
                or any(fresh[1].get(key) != identity.get(key) for key in frozen_fields)
                or not bridge.ready.is_set()):
            self._notice(job, bridge, identity, '状态或绑定已变化，未发送停止请求。')
            self._finish(job)
            return
        if not turns or turns[0].get('id') != identity['turn_id'] or turns[0].get('status') != 'inProgress':
            self._notice(job, bridge, identity, '这条任务已不在运行，未停止其他任务。')
            self._finish(job)
            return
        request = {'state': 'sending', 'route': bridge.delivery.route(), 'turn_id': identity['turn_id'],
                   'message_id': callback['message']['message_id'], 'job_id': job['id'], 'requested_at': time.time()}
        bridge.store.set(request_key, json.dumps(request))  # Durable before the native side effect.
        self._refresh(bridge)
        try:
            await asyncio.wait_for(bridge.codex.call('turn/interrupt',
                                   {'threadId': identity['native_id'], 'turnId': identity['turn_id']}), self.RPC_TIMEOUT)
        except RpcError:
            request['state'] = 'rejected'
            text, state = 'Codex 未接受停止请求，请检查会话状态后重试。', 'failed'
        except (ConnectionError, asyncio.TimeoutError, OSError):
            request['state'] = 'uncertain'
            text, state = '停止请求结果尚未确认，不会自动重发；请检查当前任务状态。', 'uncertain'
        else:
            request['state'] = 'requested'
            text, state = '已请求停止这条任务。', 'done'
        bridge.store.set(request_key, json.dumps(request))
        self._refresh(bridge)
        self._notice(job, bridge, identity, text)
        self._finish(job, state)

    async def handle_one(self, job_id=None):
        query = "SELECT * FROM status_stop_jobs WHERE state='pending'"
        job = self.db.execute(query + (' AND id=?' if job_id is not None else '') + ' ORDER BY id LIMIT 1',
                              (job_id,) if job_id is not None else ()).fetchone()
        if job is None:
            return False
        with self.db:
            if not self.db.execute("UPDATE status_stop_jobs SET state='processing' WHERE id=? AND state='pending'",
                                   (job['id'],)).rowcount:
                return False
        callback = json.loads(job['payload']).get('callback_query') or {}
        topic = (callback.get('message') or {}).get('message_thread_id')
        lock = self.locks.setdefault(topic, asyncio.Lock())
        try:
            # Native history scans use dispatch_lock. Exact-turn interruption
            # must remain available while those scans or model work are busy.
            async with lock:
                await self._process(job, callback)
        except Exception as error:
            LOG.warning('Status stop control failed (%s)', type(error).__name__)
            self._finish(job, 'failed', type(error).__name__)
            await self._answer(callback, '暂时无法停止，请检查会话状态。', True)
        return True

    def recover(self):
        for bridge in self.router.bridges.values():
            for key, value in bridge.store.db.execute('SELECT key,value FROM kv WHERE key LIKE ?',
                                                       (STOP_REQUEST_KEY + '%',)).fetchall():
                request = json.loads(value)
                if request.get('state') == 'sending':
                    request['state'] = 'uncertain'
                    bridge.store.set(key, json.dumps(request))
        with self.db:
            self.db.execute("UPDATE status_stop_jobs SET state='pending' WHERE state='processing'")

    async def loop(self):
        workers = {}
        try:
            while True:
                for topic, worker in list(workers.items()):
                    if worker.done():
                        worker.result()
                        del workers[topic]
                for job in self.db.execute("SELECT id,payload FROM status_stop_jobs WHERE state='pending' ORDER BY id"):
                    topic = (json.loads(job['payload']).get('callback_query', {}).get('message') or {}).get('message_thread_id')
                    if topic not in workers and len(workers) < 64:
                        workers[topic] = asyncio.create_task(self.handle_one(job['id']))
                await asyncio.sleep(0.1)
        finally:
            for worker in workers.values():
                worker.cancel()
            await asyncio.gather(*workers.values(), return_exceptions=True)
