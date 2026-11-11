"""Topic-local model, reasoning and display settings without model turns."""
import asyncio
from contextlib import asynccontextmanager
import json
import logging
import secrets
import time

from attachments import payload_from
from bridge import TelegramError, permitted
from message_format import plain_chunks


LOG = logging.getLogger('codex_telegram_settings')
COMMANDS = {'/model': 'model', '/effort': 'effort', '/format': 'format'}


class SettingsError(RuntimeError):
    pass


def encoded(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


def label(value, length=60):
    return ' '.join(str(value or '').split())[:length]


@asynccontextmanager
async def _unlocked():
    yield


class SettingsMenu:
    PAGE = 8
    TTL = 900
    TIMEOUT = 12

    def __init__(self, router):
        self.router, self.db = router, router.store.db
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS settings_jobs(
                id INTEGER PRIMARY KEY,payload TEXT,target_key TEXT,state TEXT DEFAULT 'pending',error TEXT);
            CREATE TABLE IF NOT EXISTS settings_panels(
                id TEXT PRIMARY KEY,chat_id TEXT,topic_id INTEGER,bot_id INTEGER,binding TEXT,
                mode TEXT,page INTEGER DEFAULT 0,model_snapshot TEXT,revision INTEGER DEFAULT 0,
                message_id INTEGER,state TEXT DEFAULT 'creating',expires_at REAL);
            CREATE TABLE IF NOT EXISTS settings_buttons(
                token TEXT PRIMARY KEY,panel_id TEXT,revision INTEGER,action TEXT,value TEXT,
                state TEXT DEFAULT 'creating');
            CREATE TABLE IF NOT EXISTS settings_applies(
                token TEXT PRIMARY KEY,update_id INTEGER,state TEXT,error TEXT);
        ''')

    @staticmethod
    def _origin(update):
        return update.get('callback_query', {}).get('message') or update.get('message', {})

    @staticmethod
    def _topic(message):
        return message.get('message_thread_id') or None

    @staticmethod
    def binding(target):
        return {'target_key': target['key'], 'thread_id': target['thread_id'],
                'topic_id': target['message_thread_id']}

    def _authorized(self, update):
        origin, callback = self._origin(update), update.get('callback_query')
        identity = {'from': callback.get('from', {}) if callback else origin.get('from', {}),
                    'chat': origin.get('chat', {})}
        if not permitted(identity, self.router.access(), self.router.owner):
            return False
        return not callback or (bool(self.router.bot_id)
            and origin.get('from', {}).get('id') == self.router.bot_id
            and bool(origin.get('message_id')) and origin.get('date') != 0)

    def _command_target(self, message):
        topic = self._topic(message)
        return self.router.by_topic.get(topic) if topic else self.router.root_target

    def ingest(self, update):
        callback, message = update.get('callback_query'), update.get('message', {})
        if callback:
            if not str(callback.get('data', '')).startswith('sm:'):
                return False
            found = self.db.execute('SELECT p.binding FROM settings_buttons b JOIN settings_panels p '
                                    'ON p.id=b.panel_id WHERE b.token=?', (callback['data'][3:],)).fetchone()
            target_key = json.loads(found['binding'])['target_key'] if found else '__invalid__'
        else:
            words = message.get('text', '').strip().split()
            if (len(words) != 1 or words[0].split('@')[0].lower() not in COMMANDS
                    or payload_from(message)):
                return False
            target = self._command_target(message)
            target_key = target['key'] if target else '__unbound__'
        with self.db:
            if self._authorized(update):
                self.db.execute('INSERT OR IGNORE INTO settings_jobs(id,payload,target_key) VALUES(?,?,?)',
                                (update['update_id'], encoded(update), target_key))
            self.db.execute("UPDATE kv SET value=? WHERE key='offset'",
                            (str(max(self.router.store.offset(), update['update_id'] + 1)),))
        return True

    def _bridge(self, binding, *, native=False):
        target, bridge = (self.router.targets.get(binding['target_key']),
                          self.router.bridges.get(binding['target_key']))
        if (not self.router.owner_allowed() or not target or not bridge
                or target.get('topic_missing') or bridge.target.get('topic_missing')
                or self.binding(target) != binding or self.binding(bridge.target) != binding
                or bridge.thread_id != binding['thread_id']):
            raise SettingsError('会话绑定已变化，请重新发送设置命令。')
        if native and not bridge.ready.is_set():
            raise SettingsError('这个会话正在连接，请稍后刷新设置菜单。')
        return bridge

    async def _snapshot(self, binding, mode):
        bridge = self._bridge(binding, native=mode != 'format')
        display = bridge.store.get('output_format', 'rich')
        if mode == 'format':
            return bridge, {}, [], display
        native = bridge.codex
        settings = await native.settings()
        models = await native.models()
        self._bridge(binding, native=True)
        if bridge.codex is not native or settings.get('thread', {}).get('id') != binding['thread_id']:
            raise SettingsError('原生会话身份未能核验，请刷新菜单。')
        if not isinstance(models, list):
            raise SettingsError('可用模型列表暂不可读，请刷新菜单。')
        valid, seen = [], set()
        for model in models:
            mid = model.get('model') or model.get('id') if isinstance(model, dict) else None
            if not isinstance(mid, str) or not mid or model.get('hidden') or mid in seen:
                continue
            seen.add(mid)
            efforts = [entry.get('reasoningEffort') for entry in model.get('supportedReasoningEfforts', [])
                       if isinstance(entry, dict) and isinstance(entry.get('reasoningEffort'), str)
                       and entry['reasoningEffort']]
            valid.append({'id': mid, 'efforts': list(dict.fromkeys(efforts)),
                          'default': model.get('defaultReasoningEffort')})
        return bridge, settings, valid, display

    async def _notice(self, update, text):
        origin = self._origin(update)
        params = {'chat_id': str(origin['chat']['id']), 'text': text, 'disable_notification': True}
        if self._topic(origin):
            params['message_thread_id'] = self._topic(origin)
        await self.router.telegram.call('sendMessage', **params)

    def _panel_row(self, identity):
        return self.db.execute('SELECT * FROM settings_panels WHERE id=?', (identity,)).fetchone()

    async def _render(self, update, panel, mode, page=0, note='', snapshot=None):
        binding = json.loads(panel['binding'])
        bridge, settings, models, display = snapshot or await self._snapshot(binding, mode)
        self._bridge(binding, native=mode != 'format')
        origin = self._origin(update)
        if (panel['bot_id'] != self.router.bot_id or panel['chat_id'] != str(self.router.owner)
                or panel['chat_id'] != str(origin['chat']['id']) or panel['topic_id'] != self._topic(origin)):
            raise SettingsError('设置菜单的位置或机器人身份已变化，请重新打开。')
        revision = panel['revision'] + 1
        current_model, effort = settings.get('model'), settings.get('reasoningEffort')
        heading = {'model': '模型 Model', 'effort': '推理强度 Effort', 'format': '显示格式 Format'}[mode]
        text = '会话设置 · ' + label(bridge.target['name'], 90) + '\n' + heading
        if mode != 'format':
            text += '\n\n当前 model：' + str(current_model or '默认') + '\n当前 effort：' + str(effort or '默认')
        text += '\n当前格式：' + ('Rich Markdown' if display == 'rich' else '纯文本')
        if note:
            text += '\n\n' + note
        rows = []
        if mode == 'model':
            total = max(1, (len(models) + self.PAGE - 1) // self.PAGE)
            page = max(0, min(int(page), total - 1))
            for model in models[page * self.PAGE:(page + 1) * self.PAGE]:
                rows.append([(('✓ ' if model['id'] == current_model else '') + label(model['id']),
                              'model', model['id'])])
            if not models:
                text += '\n\n当前没有可用模型，请刷新重试。'
            if total > 1:
                text += f'\n\n第 {page + 1}/{total} 页'
                navigation = []
                if page:
                    navigation.append(('上一页', 'page', page - 1))
                if page + 1 < total:
                    navigation.append(('下一页', 'page', page + 1))
                rows.append(navigation)
        elif mode == 'effort':
            model = next((model for model in models if model['id'] == current_model), None)
            choices = model['efforts'] if model else []
            rows.extend([[('✓ ' + value if value == effort else value, 'effort', value)] for value in choices])
            if not choices:
                text += '\n\n当前模型没有可选择的强度，请先选择模型或刷新。'
        else:
            rows.extend([[('✓ ' + name if value == display else name, 'format', value)]
                         for name, value in [('Rich Markdown', 'rich'), ('纯文本', 'plain')]])
        rows.append([(name, 'tab', value) for name, value in
                     [('模型', 'model'), ('强度', 'effort'), ('格式', 'format')]])
        rows.append([('刷新', 'refresh', None), ('关闭', 'close', None)])
        keyboard, tokens = [], []
        with self.db:
            for row in rows:
                keys = []
                for name, action, value in row:
                    token = secrets.token_urlsafe(12)
                    tokens.append(token)
                    self.db.execute('INSERT INTO settings_buttons(token,panel_id,revision,action,value) VALUES(?,?,?,?,?)',
                                    (token, panel['id'], revision, action, encoded(value)))
                    keys.append({'text': label(name), 'callback_data': 'sm:' + token})
                keyboard.append(keys)
        text = next(plain_chunks(text, 3800))
        params = {'chat_id': panel['chat_id'], 'text': text, 'reply_markup': {'inline_keyboard': keyboard}}
        editing = bool(panel['message_id'])
        if editing:
            params['message_id'] = panel['message_id']
        elif panel['topic_id']:
            params['message_thread_id'] = panel['topic_id']
        try:
            result = await self.router.telegram.call('editMessageText' if editing else 'sendMessage', **params)
        except TelegramError as error:
            if editing and error.code == 400 and 'message is not modified' in str(error).lower():
                result = {'message_id': panel['message_id']}
            else:
                raise
        mid = result.get('message_id') if isinstance(result, dict) else panel['message_id'] if editing and result is True else None
        if not mid or (editing and mid != panel['message_id']):
            raise ConnectionError('Settings menu acknowledgement unavailable')
        with self.db:
            self.db.execute("UPDATE settings_buttons SET state='expired' WHERE panel_id=?", (panel['id'],))
            self.db.executemany("UPDATE settings_buttons SET state='active' WHERE token=?", [(token,) for token in tokens])
            self.db.execute("UPDATE settings_panels SET mode=?,page=?,model_snapshot=?,revision=?,message_id=?,"
                            "state='active',expires_at=? WHERE id=?", (mode, page, current_model, revision, mid,
                                                                        time.time() + self.TTL, panel['id']))

    async def _command(self, update):
        message = update['message']
        target = self._command_target(message)
        if target is None:
            raise SettingsError('这个话题尚未绑定会话，请先发送 /sessions 绑定。')
        binding = self.binding(target)
        mode = COMMANDS[message['text'].strip().split('@')[0].lower()]
        panel_id = secrets.token_urlsafe(15)
        with self.db:
            self.db.execute('INSERT INTO settings_panels(id,chat_id,topic_id,bot_id,binding,mode,expires_at) VALUES(?,?,?,?,?,?,?)',
                            (panel_id, str(message['chat']['id']), self._topic(message), self.router.bot_id,
                             encoded(binding), mode, time.time() + self.TTL))
        await self._render(update, self._panel_row(panel_id), mode)

    def _button(self, update):
        callback, origin = update['callback_query'], self._origin(update)
        button = self.db.execute('SELECT * FROM settings_buttons WHERE token=?', (callback['data'][3:],)).fetchone()
        panel = self._panel_row(button['panel_id']) if button else None
        if (not panel or button['state'] != 'active' or button['revision'] != panel['revision']
                or panel['state'] not in ('active', 'uncertain') or panel['expires_at'] <= time.time()
                or panel['chat_id'] != str(origin['chat']['id']) or panel['topic_id'] != self._topic(origin)
                or panel['bot_id'] != self.router.bot_id or panel['message_id'] != origin['message_id']):
            raise SettingsError('这个设置按钮已失效，请重新发送设置命令。')
        self._bridge(json.loads(panel['binding']))
        if panel['state'] == 'uncertain' and button['action'] not in ('refresh', 'close'):
            raise SettingsError('上次设置结果尚未确认，请先点击刷新。')
        return button, panel

    async def _apply(self, update, button, panel):
        binding, action, value = json.loads(panel['binding']), button['action'], json.loads(button['value'])
        bridge = self._bridge(binding, native=action != 'format')
        async with (bridge.dispatch_lock if action != 'format' else _unlocked()):
            snapshot = await self._snapshot(binding, action)
            _, settings, models, display = snapshot
            changes = {}
            if action == 'model':
                selected = next((model for model in models if model['id'] == value), None)
                if selected is None:
                    return await self._render(update, panel, action, note='该模型已不可用，请重新选择。', snapshot=snapshot)
                changes['model'] = selected['id']
                effort = settings.get('reasoningEffort')
                if effort not in selected['efforts']:
                    effort = selected['default']
                if effort:
                    changes['effort'] = effort
            elif action == 'effort':
                selected = next((model for model in models if model['id'] == settings.get('model')), None)
                if settings.get('model') != panel['model_snapshot']:
                    return await self._render(update, panel, action, note='模型已变化，已刷新当前模型的可选强度。', snapshot=snapshot)
                if not selected or value not in selected['efforts']:
                    return await self._render(update, panel, action, note='该强度已不可用，请重新选择。', snapshot=snapshot)
                changes['effort'] = value
            elif value not in ('rich', 'plain'):
                raise SettingsError('显示格式无效，请刷新菜单。')
            # Recheck after every metadata await and immediately before the
            # persistent claim and settings write. The native ID never changes.
            self._bridge(binding, native=action != 'format')
            with self.db:
                claimed = self.db.execute("UPDATE settings_buttons SET state='used' WHERE token=? AND state='active'",
                                          (button['token'],)).rowcount
                if not claimed:
                    raise SettingsError('这个选项已经处理，请刷新菜单。')
                self.db.execute("UPDATE settings_buttons SET state='used' WHERE panel_id=? AND revision=? "
                                "AND action IN ('model','effort','format')", (panel['id'], panel['revision']))
                self.db.execute('INSERT INTO settings_applies(token,update_id,state) VALUES(?,?,?)',
                                (button['token'], update['update_id'], 'sending'))
            try:
                if action == 'format':
                    bridge.store.set('output_format', value)
                    if value == 'rich' and getattr(bridge, 'telegram', None) is not None:
                        bridge.telegram.rich_supported = None
                    if hasattr(bridge, 'receipt_event'):
                        bridge.receipt_event.set()
                    note = '已更新此会话的显示格式。'
                    snapshot = (bridge, settings, models, value)
                else:
                    native = bridge.codex
                    await native.call('thread/settings/update', {'threadId': binding['thread_id'], **changes})
                    actual = await native.settings()
                    self._bridge(binding, native=True)
                    if bridge.codex is not native or actual.get('thread', {}).get('id') != binding['thread_id']:
                        raise SettingsError('设置已提交，但当前会话身份无法回读确认，请刷新。')
                    expected = {'model' if field == 'model' else 'reasoningEffort': desired for field, desired in changes.items()}
                    if any(actual.get(field) != desired for field, desired in expected.items()):
                        raise SettingsError('设置已提交，但回读参数不同，请刷新查看当前值。')
                    snapshot = (bridge, actual, models, display)
                    note = '已更新此会话参数，从下一轮开始生效。'
                with self.db:
                    self.db.execute("UPDATE settings_applies SET state='done' WHERE token=?", (button['token'],))
            except BaseException as error:
                with self.db:
                    self.db.execute("UPDATE settings_applies SET state='uncertain',error=? WHERE token=?",
                                    (type(error).__name__, button['token']))
                    self.db.execute("UPDATE settings_panels SET state='uncertain' WHERE id=?", (panel['id'],))
                raise
        await self._render(update, self._panel_row(panel['id']), action, panel['page'], note, snapshot)

    async def _callback(self, update):
        try:
            await self.router.telegram.call('answerCallbackQuery', callback_query_id=update['callback_query']['id'])
        except (TelegramError, ConnectionError, asyncio.TimeoutError):
            pass
        button, panel = self._button(update)
        action, value = button['action'], json.loads(button['value'])
        if action == 'close':
            with self.db:
                self.db.execute("UPDATE settings_panels SET state='closed' WHERE id=?", (panel['id'],))
                self.db.execute("UPDATE settings_buttons SET state='expired' WHERE panel_id=?", (panel['id'],))
            await self.router.telegram.call('editMessageText', chat_id=panel['chat_id'], message_id=panel['message_id'],
                                            text='已关闭会话设置。', reply_markup={'inline_keyboard': []})
        elif action in ('tab', 'page', 'refresh'):
            mode = value if action == 'tab' else panel['mode']
            page = value if action == 'page' else panel['page'] if action == 'refresh' else 0
            await self._render(update, panel, mode, page)
        else:
            await self._apply(update, button, panel)

    async def _process(self, row):
        update = json.loads(row['payload'])
        with self.db:
            allowed = self._authorized(update) and self.router.owner_allowed()
            claimed = self.db.execute("UPDATE settings_jobs SET state=? WHERE id=? AND state='pending'",
                                      ('processing' if allowed else 'denied', row['id'])).rowcount
        if not claimed or not allowed:
            return
        try:
            action = self._callback(update) if update.get('callback_query') else self._command(update)
            await asyncio.wait_for(action, self.TIMEOUT)
            with self.db:
                self.db.execute("UPDATE settings_jobs SET state='done' WHERE id=?", (row['id'],))
        except Exception as error:
            with self.db:
                self.db.execute("UPDATE settings_jobs SET state='failed',error=? WHERE id=?", (type(error).__name__, row['id']))
            text = str(error) if isinstance(error, SettingsError) else '暂时无法确认设置结果，请重新打开或刷新菜单。'
            try:
                await self._notice(update, text)
            except (TelegramError, ConnectionError, OSError, asyncio.TimeoutError):
                pass
            LOG.warning('Settings menu operation failed: %s', type(error).__name__)

    async def handle_one(self):
        row = self.db.execute("SELECT * FROM settings_jobs WHERE state='pending' ORDER BY id LIMIT 1").fetchone()
        if row is None:
            return False
        await self._process(row)
        return True

    def recover(self):
        with self.db:
            self.db.execute("UPDATE settings_jobs SET state='uncertain' WHERE state='processing'")
            self.db.execute("UPDATE settings_applies SET state='uncertain' WHERE state='sending'")
            self.db.execute("UPDATE settings_panels SET state='uncertain' WHERE id IN "
                            "(SELECT b.panel_id FROM settings_buttons b JOIN settings_applies a ON a.token=b.token "
                            "WHERE a.state='uncertain') AND state='active'")

    async def loop(self):
        active = {}
        try:
            while True:
                for key, task in list(active.items()):
                    if task.done():
                        await task
                        active.pop(key)
                rows = self.db.execute("SELECT * FROM settings_jobs WHERE state='pending' ORDER BY id").fetchall()
                for row in rows:
                    key = row['target_key']
                    if key not in active:
                        active[key] = asyncio.create_task(self._process(row))
                await asyncio.sleep(.1)
        finally:
            for task in active.values():
                task.cancel()
            await asyncio.gather(*active.values(), return_exceptions=True)
