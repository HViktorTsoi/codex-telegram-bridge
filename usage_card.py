"""One cached, event-driven account quota card in the owner's main chat."""
import asyncio
from collections import deque
from datetime import datetime, timedelta, timezone
import json
import logging
import math
import time

from attachments import payload_from
from bridge import RpcError, TelegramError, permitted


LOG = logging.getLogger('codex_telegram_usage')


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def snapshot(payload):
    """Keep only displayable quota fields, never account or credential data."""
    if not isinstance(payload, dict):
        return None
    values = payload.get('rateLimitsByLimitId')
    if not isinstance(values, dict) or not values:
        one = payload.get('rateLimits')
        values = {one.get('limitId') or 'codex': one} if isinstance(one, dict) else {}
    buckets = {}
    for key, value in list(values.items())[:16]:
        if not isinstance(key, str) or not isinstance(value, dict):
            continue
        bucket = {'name': str(value.get('limitName') or key)[:60],
                  'plan': str(value.get('planType') or '')[:40]}
        for field in ('primary', 'secondary'):
            window = value.get(field)
            if not isinstance(window, dict):
                continue
            percent, minutes, reset = (window.get(name) for name in
                                       ('usedPercent', 'windowDurationMins', 'resetsAt'))
            if (_number(percent) and percent >= 0 and _number(minutes) and minutes > 0
                    and (reset is None or (_number(reset) and reset > 0))):
                bucket[field] = {'used': percent, 'minutes': minutes, 'reset': reset}
        if 'primary' in bucket or 'secondary' in bucket:
            buckets[key[:80]] = bucket
    return buckets or None


def _window_name(minutes):
    if minutes % 1440 == 0:
        return f'{minutes / 1440:g}天'
    if minutes % 60 == 0:
        return f'{minutes / 60:g}h'
    return f'{minutes:g}min'


def _quota_bar(used):
    remaining = max(0, min(100, 100 - used))
    filled = int(remaining / 10 + .5)
    return '█' * filled + '░' * (10 - filled) + f' 剩余 {remaining:g}%'


def _remaining(seconds):
    minutes = max(1, math.ceil(seconds / 60))
    days, minutes = divmod(minutes, 1440)
    hours, minutes = divmod(minutes, 60)
    return ''.join((f'{days}天' if days else '', f'{hours}小时' if hours else '',
                    f'{minutes}分钟' if minutes else '')) + '后'


class UsageCard:
    KEY = 'usage_card_v1'
    ENABLED = 'usage_card_enabled_v1'
    POLL = 900
    COOLDOWN = 60
    EDIT_INTERVAL = 15

    def __init__(self, router, clock=time.time):
        self.router, self.db, self.clock = router, router.store.db, clock
        self.db.execute('CREATE TABLE IF NOT EXISTS usage_jobs('
                        'id INTEGER PRIMARY KEY,payload TEXT,state TEXT DEFAULT \'pending\',error TEXT)')
        self.state = self._load(self.KEY, {})
        self.enabled = router.config.get('usage_card', False) is True or self._load(self.ENABLED, False) is True
        self.events = deque(maxlen=16)
        self.account_change = None
        self.generation = self.event_sequence = 0
        self.fetch_lock, self.job_lock, self.card_lock = asyncio.Lock(), asyncio.Lock(), asyncio.Lock()

    def _load(self, key, default):
        row = self.db.execute('SELECT value FROM kv WHERE key=?', (key,)).fetchone()
        try:
            return json.loads(row[0]) if row else default
        except (TypeError, ValueError):
            return default

    def _save(self):
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO kv VALUES(?,?)',
                            (self.KEY, json.dumps(self.state, ensure_ascii=False)))

    def _root(self):
        target = self.router.root_target
        bridge = self.router.bridges.get(target['key'])
        return bridge if bridge and bridge.thread_id == target['thread_id'] else None

    def _identity(self):
        root = self._root()
        bot = getattr(self.router, 'bot_id', None)
        if not root or type(bot) is not int or bot <= 0 or not self.router.owner_allowed():
            return None
        expected = self.router.config.get('expected_bot_username', '').lstrip('@').casefold()
        if expected and str(getattr(self.router, 'username', '')).lstrip('@').casefold() != expected:
            return None
        return {'bot': bot, 'owner': str(self.router.owner), 'thread': root.thread_id,
                'socket': self.router.config.get('socket_path')}

    def _bind(self):
        identity = self._identity()
        if identity is None:
            return None
        if self.state.get('identity') != identity:
            previous_identity = self.state.get('identity')
            last_attempt = self.state.get('last_attempt', 0)
            projection = {}
            if (isinstance(previous_identity, dict) and all(previous_identity.get(key) == identity[key]
                                                            for key in ('bot', 'owner'))):
                projection = {key: self.state[key] for key in ('message_id', 'delivery', 'sent_text', 'pin_state',
                              'pin_retry_at', 'edited_at', 'retry_at') if key in self.state}
            self.state = {'identity': identity, 'last_attempt': last_attempt}
            self.state.update(projection)
            if previous_identity:
                self.events.clear()
                self.account_change = None
            self.generation += 1
            self._save()
        return identity

    def on_account_event(self, event):
        """Socket-reader hook: bounded in-memory work, no I/O or locks."""
        if not self.enabled or event.get('method') not in ('account/rateLimits/updated', 'account/updated'):
            return
        root = self._root()
        epoch = event.get('_account_epoch')
        if epoch is not None and (not root or epoch != getattr(root.codex, 'user_input_epoch', None)):
            return
        self.event_sequence += 1
        if event['method'] == 'account/updated':
            self.generation += 1  # Invalidate an in-flight response immediately.
            self.events.clear()
            self.account_change = (epoch,)
        self.events.append((event['method'], event.get('params', {}), epoch))

    def _drain(self):
        changed = False
        if self.account_change is not None:
            epoch, = self.account_change
            root = self._root()
            if epoch is None or (root and epoch == getattr(root.codex, 'user_input_epoch', None)):
                self.state.update(buckets=None, sample_at=0, next_fetch=0, source='账号状态已变化')
                changed = True
            self.account_change = None
        while self.events:
            method, params, epoch = self.events.popleft()
            root = self._root()
            if epoch is not None and (not root or epoch != getattr(root.codex, 'user_input_epoch', None)):
                continue
            if method == 'account/updated':
                self.state.update(buckets=None, sample_at=0, next_fetch=0, source='账号状态已变化')
                changed = True
            else:
                values = snapshot(params)
                if values:
                    merged = dict(self.state.get('buckets') or {})
                    merged.update(values)
                    self.state.update(buckets=merged, sample_at=self.clock(), next_fetch=self.clock() + self.POLL,
                                      source='事件推送', error=None)
                    changed = True
        if changed:
            self._save()

    async def _fetch(self, force=False):
        if self.fetch_lock.locked() or not self.enabled:
            return False
        async with self.fetch_lock:
            identity, root, now = self._identity(), self._root(), self.clock()
            if (identity is None or root is None or not root.ready.is_set()
                    or now - self.state.get('last_attempt', 0) < self.COOLDOWN
                    or (not force and now < self.state.get('next_fetch', 0))):
                return False
            native = root.codex
            epoch = getattr(native, 'user_input_epoch', None)
            generation, sequence = self.generation, self.event_sequence
            self.state.update(last_attempt=now, next_fetch=now + self.POLL)
            self._save()
            try:
                result = await asyncio.wait_for(native.call('account/rateLimits/read', {}), 12)
            except (RpcError, ConnectionError, OSError, asyncio.TimeoutError) as error:
                self.state.update(error=type(error).__name__, next_fetch=now + self.POLL)
                self._save()
                return False
            if (identity != self._identity() or root is not self._root() or native is not root.codex
                    or epoch != getattr(native, 'user_input_epoch', None) or generation != self.generation
                    or sequence != self.event_sequence):
                return False
            self.state.update(buckets=snapshot(result), sample_at=self.clock(), next_fetch=self.clock() + self.POLL,
                              source='额度查询', error=None)
            self._save()
            return True

    def text(self):
        now = self.clock()
        lines = []
        buckets = self.state.get('buckets') or {}
        displayed = sorted(buckets.items(), key=lambda pair: (pair[0] != 'codex', pair[0]))[:8]
        multiple_windows = sum(bool(bucket.get(field)) for _, bucket in displayed
                               for field in ('primary', 'secondary')) > 1
        for key, bucket in displayed:
            if len(buckets) > 1:
                lines.append(' '.join(bucket['name'].split()))
            for field in ('primary', 'secondary'):
                window = bucket.get(field)
                if not window:
                    continue
                reset, used = window['reset'], window['used']
                expired = reset is not None and reset <= now
                prefix = _window_name(window['minutes']) + ' ' if multiple_windows else ''
                lines.append(prefix + _quota_bar(used))
                if expired:
                    lines.append('\u00a0' * 4 + '重置时间已到，等待同步（上次记录）')
                elif reset is not None:
                    try:
                        stamp = datetime.fromtimestamp(reset, timezone(timedelta(hours=8))).strftime('%m/%d %H:%M')
                    except (ValueError, OverflowError, OSError):
                        stamp = '未知'
                    lines.append('\u00a0' * 4 + '重置：' + stamp + '（香港，' + _remaining(reset - now) + '）')
        if not buckets:
            lines.append('暂无可用额度数据' if self.state.get('sample_at') else '等待额度同步')
        if self.state.get('sample_at'):
            age = max(0, int((now - self.state['sample_at']) // 60))
            lines.append(f'缓存 {age} 分钟')
        if self.state.get('error'):
            lines.append('连接暂不可用，保留上次记录')
        return '\n'.join(lines)[:3800]

    @staticmethod
    def _origin(update):
        return update.get('callback_query', {}).get('message') or update.get('message', {})

    def ingest(self, update):
        callback, origin = update.get('callback_query'), self._origin(update)
        if callback:
            if callback.get('data') != 'uc:refresh':
                return False
        else:
            words = str(origin.get('text', '')).strip().split()
            if (len(words) != 1 or words[0].split('@')[0].lower() != '/usage' or payload_from(origin)
                    or 'forward_origin' in origin
                    or ('@' in words[0] and words[0].split('@')[1].casefold()
                        != str(getattr(self.router, 'username', '')).casefold())):
                return False
        identity = {'from': callback.get('from', {}) if callback else origin.get('from', {}),
                    'chat': origin.get('chat', {})}
        allowed = self.router.owner_allowed() and permitted(identity, self.router.access(), self.router.owner)
        with self.db:
            if allowed:
                self.db.execute('INSERT OR IGNORE INTO usage_jobs(id,payload) VALUES(?,?)',
                                (update['update_id'], json.dumps(update, ensure_ascii=False)))
            self.db.execute("UPDATE kv SET value=? WHERE key='offset'",
                            (str(max(self.router.store.offset(), update['update_id'] + 1)),))
        return True

    async def handle_one(self):
        async with self.job_lock:
            row = self.db.execute("SELECT * FROM usage_jobs WHERE state='pending' ORDER BY id LIMIT 1").fetchone()
            if row is None:
                return False
            update = json.loads(row['payload'])
            origin, callback = self._origin(update), update.get('callback_query')
            with self.db:
                self.db.execute("UPDATE usage_jobs SET state='processing' WHERE id=?", (row['id'],))
            try:
                identity = self._bind()
                if identity is None:
                    raise ValueError('Usage route unavailable')
                if callback:
                    if (str(origin.get('chat', {}).get('id')) != identity['owner']
                            or origin.get('from', {}).get('id') != identity['bot']
                            or origin.get('message_thread_id') or origin.get('date') == 0
                            or origin.get('message_id') != self.state.get('message_id')):
                        raise ValueError('Stale usage callback')
                    await self.router.telegram.call('answerCallbackQuery', callback_query_id=callback['id'],
                                                    text='使用缓存显示；额度查询最短间隔 60 秒。')
                    if self.enabled:
                        await self._fetch(force=True)
                else:
                    self.enabled = True
                    with self.db:
                        self.db.execute('INSERT OR REPLACE INTO kv VALUES(?,?)', (self.ENABLED, 'true'))
                    if self.state.get('delivery') == 'deleted':
                        for name in ('message_id', 'delivery', 'sent_text', 'pin_state', 'retry_at'):
                            self.state.pop(name, None)
                        self._save()
                await self.tick()
            except Exception as error:
                with self.db:
                    self.db.execute("UPDATE usage_jobs SET state='failed',error=? WHERE id=?",
                                    (type(error).__name__, row['id']))
            else:
                with self.db:
                    self.db.execute("UPDATE usage_jobs SET state='done' WHERE id=?", (row['id'],))
            return True

    async def _publish(self):
        async with self.card_lock:
            identity, now = self._identity(), self.clock()
            if not identity or identity != self.state.get('identity') or now < self.state.get('retry_at', 0):
                return
            mid, status = self.state.get('message_id'), self.state.get('delivery')
            if status in ('uncertain', 'creating', 'deleted', 'failed'):
                return
            text = self.text()
            if mid and text != self.state.get('sent_text') and now - self.state.get('edited_at', 0) < self.EDIT_INTERVAL:
                return
            if not mid or text != self.state.get('sent_text'):
                self.state['delivery'] = 'sent' if mid else 'creating'
                self._save()
                params = {'chat_id': identity['owner'], 'text': text,
                          'reply_markup': {'inline_keyboard': [[{'text': '刷新额度', 'callback_data': 'uc:refresh'}]]}}
                params.update({'message_id': mid} if mid else {'disable_notification': True})
                try:
                    result = await self.router.telegram.call('editMessageText' if mid else 'sendMessage', **params)
                    new_mid = result.get('message_id') if isinstance(result, dict) else None
                    if (type(new_mid) is not int or new_mid <= 0 or (mid and new_mid != mid)
                            or (isinstance(result.get('chat'), dict) and str(result['chat'].get('id')) != identity['owner'])
                            or (isinstance(result.get('from'), dict) and result['from'].get('id') != identity['bot'])):
                        raise ConnectionError('Usage message acknowledgement missing')
                    self.state['message_id'] = new_mid
                except TelegramError as error:
                    if mid and error.code == 400 and 'message is not modified' in str(error).lower():
                        pass
                    else:
                        deleted = mid and error.code == 400 and 'not found' in str(error).lower()
                        self.state.update(delivery='deleted' if deleted else
                                          ('sent' if mid else 'none') if error.code == 429 else 'failed',
                                          retry_at=now + max(1, error.retry_after) if error.code == 429 else 0)
                        self._save()
                        return
                except Exception:
                    self.state.update(delivery='sent' if mid else 'uncertain', retry_at=now + 15)
                    self._save()
                    return
                self.state.update(delivery='sent', sent_text=text, edited_at=now, retry_at=0)
                self._save()
            if (self.state.get('pin_state') in ('pinned', 'uncertain', 'failed', 'pinning')
                    or now < self.state.get('pin_retry_at', 0)):
                return
            self.state['pin_state'] = 'pinning'
            self._save()
            try:
                result = await self.router.telegram.call('pinChatMessage', chat_id=identity['owner'],
                    message_id=self.state['message_id'], disable_notification=True)
                if result is not True:
                    raise ConnectionError('Usage pin acknowledgement missing')
            except Exception as error:
                code = getattr(error, 'code', None)
                self.state.update(pin_state='pending' if code == 429 else
                                  'failed' if code in (400, 403, 404) else 'uncertain',
                                  pin_retry_at=now + max(1, getattr(error, 'retry_after', 0)) if code == 429 else 0)
            else:
                self.state.update(pin_state='pinned', pin_retry_at=0)
            self._save()

    def recover(self):
        with self.db:
            self.db.execute("UPDATE usage_jobs SET state='uncertain' WHERE state='processing'")
        if self.state.get('delivery') == 'creating':
            self.state['delivery'] = 'uncertain'
        if self.state.get('pin_state') == 'pinning':
            self.state['pin_state'] = 'uncertain'
        self._save()

    async def tick(self):
        if not self.enabled or self._bind() is None:
            return False
        self._drain()
        await self._fetch()
        self._drain()
        await self._publish()
        return True

    async def loop(self):
        while True:
            try:
                await self.handle_one()
                await self.tick()
            except Exception as error:
                LOG.warning('Usage card unavailable: %s', type(error).__name__)
            await asyncio.sleep(1)
