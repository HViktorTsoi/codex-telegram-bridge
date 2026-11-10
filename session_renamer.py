"""Explicit, durable metadata-only renaming of a bound native session and topic."""
import asyncio
import inspect
import json
import secrets
import time
import unicodedata

from attachments import payload_from
from bridge import RpcError, TelegramError, permitted
from message_format import plain_chunks, utf16_size
from session_picker import BindingError, clean_name, topic_missing_error


PROMPT_TITLE = '会话改名 · 输入新名称'
PENDING = ('native_pending', 'topic_pending', 'config_pending')


class SessionRenamer:
    TTL = 900
    PAGE = 8

    def __init__(self, picker, clock=time.time):
        self.picker, self.router, self.db, self.clock = picker, picker.router, picker.db, clock
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS ui_renames(
                id TEXT PRIMARY KEY,chat_id TEXT,binding_json TEXT,old_name TEXT,
                native_old_name TEXT,new_name TEXT,stage TEXT,panel_id INTEGER,
                expires_at REAL,updated_at REAL,error TEXT,confirmed_at REAL);
            CREATE TABLE IF NOT EXISTS ui_rename_prompts(
                chat_id TEXT,message_id INTEGER,topic_id INTEGER,rename_id TEXT,
                state TEXT,expires_at REAL,PRIMARY KEY(chat_id,message_id));
        ''')

    def is_reply(self, update):
        message = update.get('message', {})
        parent = message.get('reply_to_message', {})
        if not parent or (self.router.bot_id and parent.get('from', {}).get('id') != self.router.bot_id):
            return False
        row = self.db.execute('SELECT 1 FROM ui_rename_prompts WHERE chat_id=? AND message_id=?',
                              (str(message.get('chat', {}).get('id')), parent.get('message_id'))).fetchone()
        return bool(row or parent.get('text', '').startswith(PROMPT_TITLE))

    @staticmethod
    def binding(target):
        return {k: target[k] for k in ('key', 'thread_id', 'message_thread_id')}

    def _save(self, rename_id, **values):
        assert values and values.keys() <= {'new_name', 'stage', 'panel_id', 'expires_at', 'error', 'confirmed_at'}
        values['updated_at'] = self.clock()
        with self.db:
            self.db.execute('UPDATE ui_renames SET ' + ','.join(k + '=?' for k in values) + ' WHERE id=?',
                            (*values.values(), rename_id))

    def _draft(self, rename_id, chat_id=None):
        row = self.db.execute('SELECT * FROM ui_renames WHERE id=?', (rename_id,)).fetchone()
        if not row or (chat_id is not None and row['chat_id'] != str(chat_id)):
            raise BindingError('改名记录不存在，请重新打开 /sessions。')
        if row['stage'] in ('input', 'review') and row['expires_at'] <= self.clock():
            raise BindingError('改名流程已过期，请重新选择会话并填写名称。')
        return row

    def _target(self, draft):
        if not self.router.owner_allowed():
            raise BindingError('访问权限已变化，已停止同步。')
        frozen = json.loads(draft['binding_json'])
        target = self.router.route_targets().get(frozen['key'])
        bridge = self.router.route_bridges().get(frozen['key'])
        if (not target or (not target.get('temporary_all') and target.get('topic_missing'))
                or self.binding(target) != frozen
                or not bridge or bridge.thread_id != frozen['thread_id']
                or self.binding(bridge.target) != frozen
                or target['name'] not in (draft['old_name'], draft['new_name'])):
            raise BindingError('会话绑定或名称已变化，已停止同步。请重新打开 /sessions 核对后改名。')
        return target

    def _other_pending(self, thread_id, exclude=None):
        for row in self.db.execute("SELECT * FROM ui_renames WHERE stage IN ('native_pending','topic_pending','config_pending')").fetchall():
            if row['id'] != exclude and json.loads(row['binding_json'])['thread_id'] == thread_id:
                return row
        return None

    def validate_name(self, value, thread_id):
        if not isinstance(value, str) or any(unicodedata.category(c) in ('Cc', 'Zl', 'Zp') for c in value):
            raise BindingError('请使用单行名称，不要包含换行或控制字符。')
        name = ' '.join(value.strip().split())
        if (not name or not any(unicodedata.category(c)[0] not in ('C', 'Z') for c in name)
                or utf16_size(name) > 90):
            raise BindingError('请输入非空名称，最多 90 个字符；部分表情会占两个字符。')
        if any(t['thread_id'] != thread_id and str(t['name']).casefold() == name.casefold()
               for t in self.router.route_targets().values()):
            raise BindingError('本机已有同名会话，请换一个名称。')
        for pending in self.db.execute("SELECT binding_json,new_name FROM ui_renames WHERE stage IN "
                                       "('native_pending','topic_pending','config_pending')").fetchall():
            if (json.loads(pending['binding_json'])['thread_id'] != thread_id
                    and str(pending['new_name']).casefold() == name.casefold()):
                raise BindingError('这个名称已有另一项改名待同步，请先完成已有记录或换一个名称。')
        return name

    def _expire_prompts(self, rename_id):
        with self.db:
            self.db.execute("UPDATE ui_rename_prompts SET state='expired' WHERE rename_id=? AND state='active'", (rename_id,))

    async def _panel(self, update, text, rows, draft=None):
        if utf16_size(text) > 3800:
            text = next(plain_chunks(text, 3700)) + '\n（显示已截断。）'
        if draft and draft['panel_id'] and not update.get('callback_query'):
            update = {'callback_query': {'message': {'message_id': draft['panel_id'],
                                                    'chat': {'id': int(draft['chat_id'])}}}}
        result = await self.picker.panel(update, text, rows)
        if draft:
            self._save(draft['id'], panel_id=result['message_id'])
        return result

    async def command(self, update):
        if not self.router.owner_allowed():
            raise BindingError('当前账号没有访问权限。')
        message = update['message']
        pieces = message.get('text', '').strip().split(maxsplit=1)
        proposed = pieces[1] if len(pieces) > 1 else None
        topic = message.get('message_thread_id')
        if topic:
            target = self.router.by_topic.get(topic)
            if not target:
                raise BindingError('这个话题尚未绑定会话，请从 /sessions 选择。')
            return await self.start(update, target['thread_id'], proposed)
        return await self.choose(update, proposed=proposed)

    async def choose(self, update, page=0, proposed=None):
        targets = self.router.recent.ordered([t for t in self.router.route_targets().values()
                                              if not t.get('topic_missing')])
        pages = max(1, (len(targets) + self.PAGE - 1) // self.PAGE)
        page = max(0, min(int(page), pages - 1))
        rows = [[(clean_name(t['name'], 40) + (' 【临时】' if t.get('temporary_all') else ''),
                   'rename:select', {'thread_id': t['thread_id'], 'proposed': proposed})]
                for t in targets[page * self.PAGE:(page + 1) * self.PAGE]]
        navigation = []
        if page:
            navigation.append(('上一页', 'rename:choose', {'page': page - 1, 'proposed': proposed}))
        if page + 1 < pages:
            navigation.append(('下一页', 'rename:choose', {'page': page + 1, 'proposed': proposed}))
        if navigation:
            rows.append(navigation)
        rows.append([('返回会话管理', 'home', {})])
        return await self._panel(update, f'会话改名 · 选择会话\n按最近访问排序，第 {page + 1}/{pages} 页。\n请选择要修改名称的会话。', rows)

    async def start(self, update, thread_id, proposed=None):
        if not self.router.owner_allowed():
            raise BindingError('当前账号没有访问权限。')
        target = next((t for t in self.router.route_targets().values() if t['thread_id'] == thread_id), None)
        if not target or (not target.get('temporary_all') and target.get('topic_missing')):
            raise BindingError('目标会话或入口不可用，请先从 /sessions 核对。')
        pending = self._other_pending(thread_id)
        if pending:
            return await self.pending_panel(update, pending)
        frozen = self.binding(target)
        result = await self.router.catalog_call('thread/read', {'threadId': thread_id})
        thread = result.get('thread') if isinstance(result, dict) else None
        current = self.router.route_targets().get(target['key'])
        if (not isinstance(thread, dict) or thread.get('id') != thread_id or not current
                or (not current.get('temporary_all') and current.get('topic_missing'))
                or self.binding(current) != frozen
                or not self.router.owner_allowed()):
            raise BindingError('会话信息已变化，请重新选择。')
        origin = update.get('callback_query', {}).get('message') or update['message']
        rename_id = secrets.token_urlsafe(12)
        with self.db:
            self.db.execute('INSERT INTO ui_renames VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                            (rename_id, str(origin['chat']['id']), json.dumps(frozen, sort_keys=True), target['name'],
                             thread.get('name'), None, 'input', None, self.clock() + self.TTL, self.clock(), None, None))
        draft = self._draft(rename_id)
        if proposed is not None:
            try:
                name = self.validate_name(proposed, thread_id)
            except BindingError as error:
                return await self.prompt(update, draft, str(error))
            self._save(rename_id, new_name=name, stage='review')
            return await self.review(update, self._draft(rename_id))
        return await self.prompt(update, draft)

    async def prompt(self, update, draft, error=None):
        self._expire_prompts(draft['id'])
        await self._panel(update, '修改名称\n当前：' + draft['old_name'] + '\n请回复接下来的名称输入提示。',
                          [[('取消', 'rename:cancel', {'id': draft['id']})]], draft)
        text = PROMPT_TITLE + '\n请输入新名称（最多 90 个字符）。回复 /cancel 取消。'
        if error:
            text += '\n\n' + error
        result = await self.router.telegram.call('sendMessage', chat_id=draft['chat_id'], text=text,
                                                 reply_markup={'force_reply': True, 'selective': True,
                                                               'input_field_placeholder': '新会话名称'})
        with self.db:
            self.db.execute('INSERT INTO ui_rename_prompts VALUES(?,?,?,?,?,?)',
                            (draft['chat_id'], result['message_id'], None, draft['id'], 'active', self.clock() + self.TTL))

    async def reply(self, update):
        message = update['message']
        if not permitted(message, self.router.access(), self.router.owner):
            raise BindingError('当前账号没有访问权限。')
        prompt = self.db.execute('SELECT * FROM ui_rename_prompts WHERE chat_id=? AND message_id=?',
                                 (str(message['chat']['id']), message['reply_to_message']['message_id'])).fetchone()
        if (not prompt or prompt['state'] != 'active' or prompt['expires_at'] <= self.clock()
                or prompt['topic_id'] != (message.get('message_thread_id') or None)):
            raise BindingError('这个改名提示已失效，请使用最新提示或重新打开 /sessions。')
        draft = self._draft(prompt['rename_id'], message['chat']['id'])
        if draft['stage'] != 'input':
            raise BindingError('这一步已经处理，请使用最新按钮。')
        with self.db:
            self.db.execute("UPDATE ui_rename_prompts SET state='consumed' WHERE chat_id=? AND message_id=?",
                            (prompt['chat_id'], prompt['message_id']))
        if message.get('text', '').strip() == '/cancel' and not payload_from(message):
            return await self.cancel(update, draft)
        try:
            if payload_from(message):
                raise BindingError('请用文字回复名称提示，附件不会用作会话名称。')
            name = self.validate_name(message.get('text', ''), json.loads(draft['binding_json'])['thread_id'])
        except BindingError as error:
            return await self.prompt(update, draft, str(error))
        self._target(draft)
        self._save(draft['id'], stage='review', new_name=name, expires_at=self.clock() + self.TTL)
        return await self.review(update, self._draft(draft['id']))

    async def review(self, update, draft):
        binding = json.loads(draft['binding_json'])
        temporary = self.router.route_targets().get(binding['key'], {}).get('temporary_all')
        if temporary:
            text = (f"确认改名？\n\n原名称：{draft['old_name']}\n新名称：{draft['new_name']}\n"
                    f"主机：{self.router.config.get('host_id', '本机')}\n会话：{binding['thread_id']}\n\n"
                    '确认后修改 Codex 会话名称和临时 All 标签。')
        else:
            text = (f"确认改名？\n\n原名称：{draft['old_name']}\n新名称：{draft['new_name']}\n"
                    f"主机：{self.router.config.get('host_id', '本机')}\n会话：{binding['thread_id']}\n话题：{binding['message_thread_id']}\n\n"
                    '确认后同时修改 Codex 会话名和 Telegram 话题名。')
        if draft['native_old_name'] != draft['old_name']:
            text += '\n当前 Codex 名称：' + clean_name(draft['native_old_name'] or '未命名', 220)
        return await self._panel(update, text,
                                 [[('确认改名', 'rename:confirm', {'id': draft['id']})],
                                  [('取消', 'rename:cancel', {'id': draft['id']})]], draft)

    async def cancel(self, update, draft):
        if draft['stage'] in PENDING or draft['confirmed_at'] is not None:
            return await self.pending_panel(update, draft)
        self._expire_prompts(draft['id'])
        self._save(draft['id'], stage='cancelled')
        return await self._panel(update, '已取消改名。', [[('返回会话管理', 'home', {})]], draft)

    async def pending_panel(self, update, draft):
        phase = {'native_pending': 'Codex 名称仍待核对。', 'topic_pending': 'Codex 名称已更新，Telegram 话题仍待同步。',
                 'config_pending': '两个名称已更新，本机记录仍待同步。'}
        text = '名称同步尚未完成\n' + phase.get(draft['stage'], '请核对当前名称。')
        text += '\n目标名称：' + str(draft['new_name'] or '') + '\n点击继续时只会同步这个原会话及其入口。'
        return await self._panel(update, text,
                                 [[('继续同步名称', 'rename:resume', {'id': draft['id']})],
                                  [('返回会话管理', 'home', {})]], draft)

    async def _native(self, draft):
        binding = json.loads(draft['binding_json'])
        result = await self.router.catalog_call('thread/read', {'threadId': binding['thread_id']})
        thread = result.get('thread') if isinstance(result, dict) else None
        if not isinstance(thread, dict) or thread.get('id') != binding['thread_id']:
            raise BindingError('原生会话 ID 无法核对，已停止同步。')
        current = thread.get('name')
        allowed = (draft['native_old_name'], draft['new_name']) if draft['stage'] == 'native_pending' else (draft['new_name'],)
        if current not in allowed:
            raise BindingError('Codex 名称已被其他操作更改，已停止同步；请重新选择会话核对。')
        return current

    async def apply(self, update, draft):
        try:
            if not self.router.owner_allowed():
                raise BindingError('访问权限已变化，已停止同步。')
            self._target(draft)
            binding = json.loads(draft['binding_json'])
            if self._other_pending(binding['thread_id'], draft['id']):
                raise BindingError('这个会话已有另一项改名待同步，请先处理较早的记录。')
            current = await self._native(draft)
            self._target(draft)
            if draft['stage'] == 'native_pending':
                if current != draft['new_name']:
                    await self.router.catalog_call('thread/name/set', {'threadId': binding['thread_id'], 'name': draft['new_name']})
                    self._target(draft)
                    if await self._native(draft) != draft['new_name']:
                        raise RpcError('Native name readback has not confirmed the new name')
                self._save(draft['id'], stage='config_pending' if binding['message_thread_id'] is None else 'topic_pending', error=None)
                draft = self._draft(draft['id'])
            if draft['stage'] == 'topic_pending':
                self._target(draft)
                try:
                    result = await self.router.telegram.call('editForumTopic', chat_id=draft['chat_id'],
                                                             message_thread_id=binding['message_thread_id'], name=draft['new_name'])
                    if result is not True:
                        raise ConnectionError('Topic rename response unavailable')
                except TelegramError as error:
                    if error.code != 400 or not any(term in str(error).lower() for term in
                                                   ('topic_not_modified', 'topic not modified', 'message is not modified')):
                        raise
                self._target(draft)
                self._save(draft['id'], stage='config_pending', error=None)
                draft = self._draft(draft['id'])
            if draft['stage'] == 'config_pending':
                self._target(draft)
                await self._native(draft)
                self._target(draft)
                if binding['message_thread_id'] is None:
                    result = self.router.rename_temporary_route(binding['key'], binding,
                                                                draft['old_name'], draft['new_name'])
                else:
                    result = self.router.rename_target(binding['key'], binding, draft['old_name'], draft['new_name'])
                if inspect.isawaitable(result):
                    await result
                if hasattr(self.router, 'recent'):
                    self.router.recent.record(binding['thread_id'], 'rename:' + draft['id'],
                                              accessed_at=draft['confirmed_at'] or self.clock())
                self._save(draft['id'], stage='done', error=None)
        except BindingError as error:
            self._save(draft['id'], stage='blocked', error=str(error))
            raise
        except ValueError as error:
            self._save(draft['id'], stage='blocked', error='Configuration changed')
            raise BindingError('本机配置已变化，已停止同步，请重新打开 /sessions 核对。') from None
        except (RpcError, TelegramError, ConnectionError, asyncio.TimeoutError, OSError) as error:
            if topic_missing_error(error):
                self._save(draft['id'], stage='blocked', error='Original topic missing')
                raise BindingError('原话题已经失效，已停止同步；请从 /sessions 重建话题后核对名称。') from None
            self._save(draft['id'], error=type(error).__name__)
            return await self.pending_panel(update, self._draft(draft['id']))
        target = self.router.route_targets()[binding['key']]
        if target.get('temporary_all'):
            return await self.picker.temporary_panel(update, target)
        return await self.picker.bound_panel(update, target)

    async def handle(self, update, action, data):
        if not self.router.owner_allowed():
            raise BindingError('当前账号没有访问权限。')
        if action in ('rename:start', 'rename:select'):
            return await self.start(update, data['thread_id'], data.get('proposed'))
        if action == 'rename:choose':
            return await self.choose(update, data.get('page', 0), data.get('proposed'))
        origin = update.get('callback_query', {}).get('message') or update['message']
        draft = self._draft(data['id'], origin['chat']['id'])
        if draft['stage'] == 'done':
            target = self.router.route_targets().get(json.loads(draft['binding_json'])['key'])
            if target:
                if target.get('temporary_all'):
                    return await self.picker.temporary_panel(update, target)
                return await self.picker.bound_panel(update, target)
            raise BindingError('原会话已不在绑定列表中，请重新打开 /sessions。')
        if draft['stage'] in ('cancelled', 'blocked'):
            raise BindingError('此改名流程已取消或停止，请重新选择会话。')
        if action == 'rename:cancel':
            return await self.cancel(update, draft)
        if action not in ('rename:confirm', 'rename:resume'):
            raise BindingError('未知改名操作，请重新打开 /sessions。')
        if draft['stage'] == 'review' and action == 'rename:confirm':
            self._target(draft)
            binding = json.loads(draft['binding_json'])
            self.validate_name(draft['new_name'], binding['thread_id'])
            if self._other_pending(binding['thread_id'], draft['id']):
                raise BindingError('这个会话已有改名待同步，请先继续已有记录。')
            self._expire_prompts(draft['id'])
            self._save(draft['id'], stage='native_pending', confirmed_at=self.clock())
            draft = self._draft(draft['id'])
        elif draft['stage'] not in PENDING or draft['confirmed_at'] is None:
            raise BindingError('请先填写名称并点击“确认改名”。')
        return await self.apply(update, draft)

    def unfinished(self):
        return self.db.execute("SELECT * FROM ui_renames WHERE chat_id=? AND stage IN "
                               "('native_pending','topic_pending','config_pending') ORDER BY updated_at DESC LIMIT 5",
                               (str(self.router.owner),)).fetchall()

    def recover(self):
        # Metadata assignments can retry the same value. Keep the saved stage and
        # frozen destination; replayed UI work or an explicit resume finishes it.
        return None
