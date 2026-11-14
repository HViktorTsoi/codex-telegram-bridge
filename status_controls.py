"""Owner-only, exact-turn stop controls beside persistent status bars."""
import asyncio
import json
import logging
import re
import time

from attachments import payload_from
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

    @staticmethod
    def _binding(target):
        return {'target_key': target['key'], 'thread_id': target['thread_id'],
                'topic_id': target['message_thread_id']}

    def _bridge(self, binding, bot_id=None):
        key = binding.get('target_key')
        target, bridge = self.router.targets.get(key), self.router.bridges.get(key)
        if (not self.router.owner_allowed() or not target or not bridge or not bridge.owner_allowed()
                or not self.router.bot_id or bot_id is not None and self.router.bot_id != bot_id
                or target.get('topic_missing') or bridge.target.get('topic_missing')
                or self._binding(target) != binding or self._binding(bridge.target) != binding
                or bridge.thread_id != binding.get('thread_id')
                or bridge.store.get('bound_thread_id') != bridge.thread_id):
            return None
        return bridge

    @staticmethod
    def _ordinary(message):
        return bool(payload_from(message) or ((message.get('text') or message.get('caption') or '').strip()
                    and not (message.get('text') or message.get('caption') or '').lstrip().startswith('/')))

    def _pending_ids(self, bridge, source=None, *, cutoff_ms=None, cutoff_id=None):
        """Freeze existing IDs; never sweep messages accepted after a control."""
        result = set()
        if source is not None:
            cutoff_ms, cutoff_id = source['received_ms'], source['id']
            original = self.db.execute('SELECT received_ms FROM jobs WHERE id=? AND target_key=? AND thread_id=? '
                'AND topic_id=?', (source['id'], bridge.target['key'], bridge.thread_id,
                                  bridge.target['message_thread_id'])).fetchone()
            if original is not None and original['received_ms'] is not None:
                cutoff_ms = original['received_ms']
        for row in bridge.store.db.execute('SELECT * FROM inbox WHERE state=\'pending\' AND chat_id=? '
                'AND message_thread_id=?', (str(self.router.owner), bridge.target['message_thread_id'])):
            original = self.db.execute('SELECT received_ms FROM jobs WHERE id=? AND target_key=? AND thread_id=? '
                'AND topic_id=?', (row['id'], bridge.target['key'], bridge.thread_id,
                                  bridge.target['message_thread_id'])).fetchone()
            received = original['received_ms'] if original is not None and original['received_ms'] is not None else row['received_ms']
            if cutoff_ms is not None and ((received or 0) > cutoff_ms
                    or received == cutoff_ms and row['id'] > cutoff_id):
                continue
            if bridge.delivery._valid_receipt_source(row):
                result.add(row['id'])
        for row in self.db.execute("SELECT * FROM jobs WHERE state='pending' AND target_key=? AND thread_id=? "
                'AND topic_id=? AND chat_id=?', (bridge.target['key'], bridge.thread_id,
                bridge.target['message_thread_id'], str(self.router.owner))):
            if cutoff_ms is not None and ((row['received_ms'] or 0) > cutoff_ms
                    or row['received_ms'] == cutoff_ms and row['id'] > cutoff_id):
                continue
            try:
                message = json.loads(row['message_json'])
            except (TypeError, ValueError):
                continue
            if permitted(message, self.router.access(), self.router.owner) and self._ordinary(message):
                result.add(row['id'])
        return sorted(result)

    def _cancel_resolution(self, callback):
        if not self._authorized(callback) or not re.fullmatch(r'cancel:[0-9a-f]{24}', callback.get('data', '')):
            return None
        message = callback.get('message') or {}
        target = self.router.by_topic.get(message.get('message_thread_id'))
        if target is None:
            return None
        bridge = self._bridge(self._binding(target))
        if bridge is None:
            return None
        digest = callback['data'][7:]
        state = json.loads(bridge.store.get(CompletionPreview.KEY + digest, '{}'))
        try:
            identity = bridge.delivery.body_preview._bar_identity(state.get('desired_identity', {}))
        except (KeyError, TypeError, ValueError):
            return None
        preview = bridge.delivery.body_preview
        if (not identity or identity.get('turn_id') or identity.get('source_id') is None
                or state.get('finished') or state.get('closed') or state.get('transferred_to')
                or state.get('message_id') != message.get('message_id')
                or preview._key(identity) != CompletionPreview.KEY + digest
                or state.get('route') != [bridge.thread_id, str(self.router.owner), target['message_thread_id']]
                or not preview._bar_allowed(identity)):
            return None
        source = preview._source(identity)
        displayed = [button.get('callback_data') for row in state.get('sent_markup', {}).get('inline_keyboard', [])
                     for button in row]
        if (source is None or source['state'] != 'pending' or source['turn_id']
                or callback['data'] not in displayed):
            return None
        return bridge, identity, source

    def ingest(self, update, *, received_ms=None):
        callback = update.get('callback_query') or {}
        data = callback.get('data', '')
        message = update.get('message') or {}
        meta = None
        if callback:
            if not isinstance(data, str) or not data.startswith((self.PREFIX, 'cancel:')):
                return False
            allowed = self._authorized(callback)
            if allowed and data.startswith('cancel:'):
                resolved = self._cancel_resolution(callback)
                if resolved:
                    bridge, identity, source = resolved
                    meta = {'kind': 'cancel', 'binding': self._binding(bridge.target), 'bot_id': self.router.bot_id,
                            'owner': str(self.router.owner), 'ids': self._pending_ids(bridge, source),
                            'source_id': source['id']}
        else:
            words = str(message.get('text') or '').strip().split()
            command, _, username = words[0].partition('@') if len(words) == 1 else ('', '', '')
            if (command.casefold() != '/stop' or payload_from(message)
                    or message.get('forward_origin') or message.get('forward_date')
                    or username and username.casefold() != str(self.router.username or '').lstrip('@').casefold()):
                return False
            allowed = permitted(message, self.router.access(), self.router.owner) and bool(self.router.bot_id)
            target = (self.router.by_topic.get(message.get('message_thread_id'))
                      if message.get('message_thread_id') else self.router.root_target)
            bridge = self._bridge(self._binding(target)) if allowed and target else None
            cutoff_ms = int(time.time() * 1000) if received_ms is None else int(received_ms)
            group = bridge.store.db.execute("SELECT turn_id FROM completion_turns WHERE native_id=? AND chat_id=? "
                "AND topic_id=? AND stage='active' AND status='inProgress' AND started_at<=? "
                'ORDER BY started_at DESC,rowid DESC LIMIT 1',
                (bridge.thread_id, str(self.router.owner), bridge.target['message_thread_id'], cutoff_ms / 1000)).fetchone() if bridge else None
            meta = {'kind': 'command', 'binding': self._binding(target) if target else None,
                    'bot_id': self.router.bot_id, 'owner': str(self.router.owner),
                    'ids': self._pending_ids(bridge, cutoff_ms=cutoff_ms, cutoff_id=update['update_id']) if bridge else [],
                    'received_at': cutoff_ms / 1000,
                    'turn_id': group['turn_id'] if group else None,
                    'reply_topic': message.get('message_thread_id') or None}
        payload = dict(update)
        if meta is not None:
            payload['_stop_control'] = meta
        with self.db:
            if allowed:
                self.db.execute('INSERT OR IGNORE INTO status_stop_jobs(id,payload) VALUES(?,?)',
                                (update['update_id'], json.dumps(payload, ensure_ascii=False)))
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

    def _cancel_pending(self, bridge, meta):
        """Only pending rows in this frozen set can be withdrawn locally."""
        cancelled = set()
        binding, owner = meta['binding'], str(self.router.owner)
        with self.db:
            for identity in meta['ids']:
                row = self.db.execute('SELECT * FROM jobs WHERE id=? AND target_key=? AND thread_id=? '
                    'AND topic_id=? AND chat_id=?', (identity, binding['target_key'], binding['thread_id'],
                    binding['topic_id'], owner)).fetchone()
                if row is not None and row['state'] in ('pending', 'cancelled'):
                    if row['state'] == 'pending':
                        self.db.execute("UPDATE jobs SET state='cancelled' WHERE id=? AND state='pending'", (identity,))
                    cancelled.add(identity)
        with bridge.store.db:
            for identity in meta['ids']:
                row = bridge.store.db.execute('SELECT * FROM inbox WHERE id=? AND chat_id=? AND message_thread_id=?',
                    (identity, owner, binding['topic_id'])).fetchone()
                if (row is not None and row['state'] in ('pending', 'cancelled')
                        and not row['turn_id'] and bridge.delivery._valid_receipt_source(row)):
                    if row['state'] == 'pending':
                        bridge.store.db.execute("UPDATE inbox SET state='cancelled' WHERE id=? AND state='pending' AND turn_id IS NULL",
                                                (identity,))
                    cancelled.add(identity)
        self._refresh(bridge)
        return cancelled

    async def _cancelled_preview(self, bridge, cancelled):
        source = bridge.delivery._latest_receipt_source()
        if (source is None or source['id'] not in cancelled or source['state'] != 'cancelled' or source['turn_id']):
            return
        identity = {'native_id': bridge.thread_id, 'chat_id': str(self.router.owner),
                    'topic_id': bridge.target['message_thread_id'], 'source_id': source['id'], 'turn_id': None}
        preview = bridge.delivery.body_preview
        if not preview.load(identity).get('message_id'):
            return
        try:
            status = bridge.delivery._preview_text('⏹ 已取消待处理', source['received_ms'] / 1000, '')
            await asyncio.wait_for(preview.update_status(self.router.telegram, identity, status,
                                   '待处理消息已取消，未提交到 Codex。'), self.RPC_TIMEOUT)
        except Exception:
            pass  # The durable cancellation does not depend on a display edit.

    def _priority_notice(self, job, meta, text):
        bridge = self._bridge(meta['binding'], meta['bot_id'])
        if bridge is None or str(self.router.owner) != meta['owner']:
            return
        topic = meta.get('reply_topic', meta['binding']['topic_id'])
        if topic is None and meta['binding']['target_key'] != self.router.config['root_target']:
            return
        if topic not in (None, meta['binding']['topic_id']):
            return
        bridge.store.enqueue_at('stop-control-result:' + str(job['id']), self.router.owner, text, topic)

    async def _process_priority(self, job, update, meta):
        binding = meta.get('binding')
        bridge = self._bridge(binding, meta.get('bot_id')) if isinstance(binding, dict) else None
        if bridge is None or meta.get('owner') != str(self.router.owner):
            if update.get('callback_query'):
                await self._answer(update['callback_query'], '绑定或权限已变化，未取消任何消息。', True)
            elif (meta.get('bot_id') == self.router.bot_id and self.router.owner_allowed()
                    and permitted(update.get('message') or {}, self.router.access(), self.router.owner)):
                origin = update['message']
                params = {'chat_id': str(self.router.owner),
                          'text': '这个话题尚未绑定，或绑定已变化；未停止任何任务。请发送 /sessions 选择会话。',
                          'disable_notification': True}
                if origin.get('message_thread_id'):
                    params['message_thread_id'] = origin['message_thread_id']
                try:
                    await self.router.telegram.call('sendMessage', **params)
                except (TelegramError, ConnectionError, OSError, asyncio.TimeoutError):
                    pass
            self._finish(job, 'denied')
            return
        if meta['kind'] == 'cancel':
            resolved = self._cancel_resolution(update['callback_query'])
            if resolved is None or resolved[0] is not bridge or resolved[2]['id'] != meta['source_id']:
                await self._answer(update['callback_query'], '这条消息已开始提交或按钮已过期，请使用当前任务的停止按钮。', True)
                self._finish(job)
                return
            await self._answer(update['callback_query'], '正在取消尚未提交的消息…')
            fresh = self._cancel_resolution(update['callback_query'])
            if (self._bridge(binding, meta['bot_id']) is not bridge or fresh is None
                    or fresh[0] is not bridge or fresh[2]['id'] != meta['source_id']):
                self._finish(job, 'denied')
                return
        else:
            message = update.get('message') or {}
            if not permitted(message, self.router.access(), self.router.owner):
                self._finish(job, 'denied')
                return
        cancelled = self._cancel_pending(bridge, meta)
        await self._cancelled_preview(bridge, cancelled)
        if self._bridge(binding, meta['bot_id']) is not bridge:
            self._finish(job, 'denied')
            return
        prefix = f'已取消 {len(cancelled)} 条尚未提交的消息。' if cancelled else '当前没有可取消的待处理消息。'
        if meta['kind'] == 'cancel':
            self._priority_notice(job, meta, prefix + '未中断其他已开始的任务。')
            self._finish(job)
            return
        request_key = STOP_REQUEST_KEY + 'command:' + str(job['id'])
        previous = json.loads(bridge.store.get(request_key, '{}'))
        if previous.get('state') in ('sending', 'requested', 'uncertain'):
            text = ('已请求停止这条任务。' if previous['state'] == 'requested'
                    else '上次停止请求结果待核对，不会重复发送。')
            self._priority_notice(job, meta, prefix + text)
            self._finish(job)
            return
        if not bridge.ready.is_set():
            self._priority_notice(job, meta, prefix + 'Codex 当前未连接，无法确认或中断原生任务。')
            self._finish(job)
            return
        try:
            turns = await asyncio.wait_for(bridge.codex.turns(limit=1), self.RPC_TIMEOUT)
        except (ConnectionError, RpcError, asyncio.TimeoutError, OSError):
            self._priority_notice(job, meta, prefix + '暂时无法读取原生任务状态，尚未发送中断请求。')
            self._finish(job, 'failed', 'NativeReadUnavailable')
            return
        if self._bridge(binding, meta['bot_id']) is not bridge or not bridge.ready.is_set():
            self._finish(job, 'denied')
            return
        current = turns[0] if turns else None
        if not current or current.get('status') != 'inProgress':
            self._priority_notice(job, meta, prefix + '当前没有正在执行的原生任务。')
            self._finish(job)
            return
        selected = meta.get('turn_id')
        started = current.get('startedAt')
        if ((selected and current.get('id') != selected)
                or not selected and (not isinstance(started, (int, float)) or started > meta['received_at'])):
            self._priority_notice(job, meta, prefix + '当前任务已变化，未停止之后开始的任务。')
            self._finish(job)
            return
        task_identity = {'native_id': bridge.thread_id, 'chat_id': str(self.router.owner),
                         'topic_id': binding['topic_id'], 'turn_id': current['id']}
        task_key = STOP_REQUEST_KEY + bridge.delivery.body_preview._key(task_identity)[-24:]
        shared = json.loads(bridge.store.get(task_key, '{}'))
        if shared.get('state') in ('sending', 'requested', 'uncertain'):
            bridge.store.set(request_key, json.dumps(shared))
            text = ('已请求停止这条任务。' if shared['state'] == 'requested'
                    else '这条任务的停止请求结果待核对，不会重复发送。')
            self._priority_notice(job, meta, prefix + text)
            self._finish(job)
            return
        request = {'state': 'sending', 'route': bridge.delivery.route(), 'turn_id': current['id'],
                   'job_id': job['id'], 'requested_at': time.time()}
        bridge.store.set(request_key, json.dumps(request))
        bridge.store.set(task_key, json.dumps(request))
        self._refresh(bridge)
        try:
            await asyncio.wait_for(bridge.codex.call('turn/interrupt',
                {'threadId': bridge.thread_id, 'turnId': current['id']}), self.RPC_TIMEOUT)
        except RpcError:
            request['state'], text, state = 'rejected', 'Codex 未接受停止请求，请核对当前任务。', 'failed'
        except (ConnectionError, asyncio.TimeoutError, OSError):
            request['state'], text, state = 'uncertain', '停止请求结果未确认，不会自动重发；请核对当前任务。', 'uncertain'
        else:
            request['state'], text, state = 'requested', '已请求停止这条任务。', 'done'
        bridge.store.set(request_key, json.dumps(request))
        bridge.store.set(task_key, json.dumps(request))
        self._refresh(bridge)
        self._priority_notice(job, meta, prefix + text)
        self._finish(job, state)

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
        update = json.loads(job['payload'])
        callback = update.get('callback_query') or {}
        meta = update.get('_stop_control')
        topic = ((meta.get('binding') or {}).get('topic_id') if isinstance(meta, dict)
                 else (callback.get('message') or {}).get('message_thread_id'))
        lock = self.locks.setdefault(topic, asyncio.Lock())
        try:
            # Native history scans use dispatch_lock. Exact-turn interruption
            # must remain available while those scans or model work are busy.
            async with lock:
                if isinstance(meta, dict) and meta.get('kind') in ('command', 'cancel'):
                    await self._process_priority(job, update, meta)
                elif callback.get('data', '').startswith('cancel:'):
                    await self._answer(callback, '这个取消按钮已失效，未取消任何消息。', True)
                    self._finish(job)
                else:
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
        # Old releases queued /stop behind Native.ready. Recover only proven
        # owner messages from this exact original router binding, using the
        # original receipt cutoff instead of the restart time.
        for bridge in self.router.bridges.values():
            if self._bridge(self._binding(bridge.target)) is not bridge:
                continue
            rows = bridge.store.db.execute("SELECT * FROM inbox WHERE state='pending' AND turn_id IS NULL "
                'AND chat_id=? AND message_thread_id=? ORDER BY received_ms,id',
                (str(self.router.owner), bridge.target['message_thread_id'])).fetchall()
            for row in rows:
                try:
                    media = json.loads(row['message_json'] or '{}')
                except (TypeError, ValueError):
                    continue
                if media:
                    continue
                words = (row['text'] or '').strip().split()
                if len(words) != 1 or words[0].split('@')[0].casefold() != '/stop':
                    continue
                original = self.db.execute('SELECT * FROM jobs WHERE id=? AND target_key=? AND thread_id=? '
                    'AND topic_id=? AND chat_id=?', (row['id'], bridge.target['key'], bridge.thread_id,
                    bridge.target['message_thread_id'], str(self.router.owner))).fetchone()
                if original is None or row['received_ms'] is None:
                    continue
                try:
                    message = json.loads(original['message_json'])
                except (TypeError, ValueError):
                    continue
                if not permitted(message, self.router.access(), self.router.owner):
                    continue
                update = {'update_id': row['id'], 'message': message}
                cutoff = original['received_ms'] if original['received_ms'] is not None else row['received_ms']
                if self.ingest(update, received_ms=cutoff) and self.db.execute(
                        'SELECT 1 FROM status_stop_jobs WHERE id=?', (row['id'],)).fetchone():
                    with bridge.store.db:
                        bridge.store.db.execute("UPDATE inbox SET state='done' WHERE id=? AND state='pending' AND turn_id IS NULL",
                                                (row['id'],))

    async def loop(self):
        workers = {}
        try:
            while True:
                for topic, worker in list(workers.items()):
                    if worker.done():
                        worker.result()
                        del workers[topic]
                for job in self.db.execute("SELECT id,payload FROM status_stop_jobs WHERE state='pending' ORDER BY id"):
                    payload = json.loads(job['payload'])
                    meta = payload.get('_stop_control')
                    topic = ((meta.get('binding') or {}).get('topic_id') if isinstance(meta, dict)
                             else (payload.get('callback_query', {}).get('message') or {}).get('message_thread_id'))
                    if topic not in workers and len(workers) < 64:
                        workers[topic] = asyncio.create_task(self.handle_one(job['id']))
                await asyncio.sleep(0.1)
        finally:
            for worker in workers.values():
                worker.cancel()
            await asyncio.gather(*workers.values(), return_exceptions=True)
