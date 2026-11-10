"""Owner-only Telegram buttons for binding existing native Codex conversations."""
import asyncio
import hashlib
import json
import secrets
import time
from pathlib import Path

from bridge import RpcError, TelegramError, permitted


class BindingError(Exception):
    """An expected binding failure that can be shown to the owner."""


class TopicCreationUnavailable(BindingError):
    """Telegram explicitly refused topic creation; the native session is safe."""

    def __init__(self, retry_after=300):
        self.retry_after = max(1, int(retry_after))
        super().__init__('Telegram 暂时无法为当前 Bot 创建话题，'
                         f'请约 {self.retry_after} 秒后手动重试。')


TOPIC_CREATION_COOLDOWN_KEY = 'topic_creation_cooldown_v1:'
TOPIC_CREATION_COOLDOWN_SECONDS = 300


def clean_name(value, limit=90):
    return ' '.join(str(value).split())[:limit]


def directory_text(thread, limit=220):
    value = thread.get('cwd')
    if not isinstance(value, str) or not value:
        return '未提供'
    value = value.replace('\r', '\\r').replace('\n', '\\n').replace('\t', '\\t')
    if len(value) > limit:
        head = (limit - 1) // 2
        value = value[:head] + '…' + value[-(limit - head - 1):]
    return value


def topic_missing_error(error):
    if not isinstance(error, TelegramError) or error.code != 400:
        return False
    description = str(error).lower()
    return any(term in description for term in (
        'message thread not found', 'topic_id_invalid', 'topic_deleted', 'topic_not_found'))


class SessionPicker:
    def __init__(self, router):
        self.router, self.db = router, router.store.db
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS ui_jobs(
                id INTEGER PRIMARY KEY,payload TEXT,state TEXT DEFAULT 'pending',error TEXT);
            CREATE TABLE IF NOT EXISTS ui_buttons(
                token TEXT PRIMARY KEY,chat_id TEXT,topic_id INTEGER,message_id INTEGER,
                action TEXT,data TEXT,expires_at REAL,state TEXT DEFAULT 'active');
            CREATE TABLE IF NOT EXISTS binding_ops(
                thread_id TEXT PRIMARY KEY,target_json TEXT,status TEXT,error TEXT,updated_at REAL);
            CREATE TABLE IF NOT EXISTS ui_catalogs(
                id TEXT PRIMARY KEY,payload TEXT,expires_at REAL);
            CREATE TABLE IF NOT EXISTS ui_thread_directories(
                thread_id TEXT PRIMARY KEY,cwd TEXT,updated_at REAL);
        ''')
        from session_creator import SessionCreator
        self.creator = SessionCreator(self)
        from main_guard import MainChatGuard
        self.guard = MainChatGuard(self)
        from session_renamer import SessionRenamer
        self.renamer = SessionRenamer(self)

    def ingest(self, update):
        callback = update.get('callback_query')
        message = update.get('message', {})
        if callback is not None:
            if not callback.get('data', '').startswith('ui:'):
                return False
            message = callback.get('message', {})
            identity = {'from': callback.get('from', {}), 'chat': message.get('chat', {})}
            valid = (permitted(identity, self.router.access(), self.router.owner)
                     and message.get('message_id') and message.get('date') != 0
                     and (not self.router.bot_id or message.get('from', {}).get('id') == self.router.bot_id))
        else:
            text = message.get('text', '').strip().split(maxsplit=1)
            # A caption or a media message must never execute a control command.
            from attachments import payload_from
            if (not self.creator.is_reply(update) and not self.renamer.is_reply(update)
                    and (not text or text[0].split('@')[0].lower() not in ('/sessions', '/rename') or payload_from(message))):
                return False
            valid = permitted(message, self.router.access(), self.router.owner)
        with self.db:
            if valid:
                self.db.execute('INSERT OR IGNORE INTO ui_jobs(id,payload) VALUES(?,?)',
                                (update['update_id'], json.dumps(update, ensure_ascii=False)))
            self.db.execute("UPDATE kv SET value=? WHERE key='offset'",
                            (str(max(self.router.store.offset(), update['update_id'] + 1)),))
        return True

    def _button(self, chat_id, topic_id, label, action, data):
        token = secrets.token_urlsafe(12)
        with self.db:
            self.db.execute('INSERT INTO ui_buttons(token,chat_id,topic_id,action,data,expires_at) VALUES(?,?,?,?,?,?)',
                            (token, str(chat_id), topic_id, action, json.dumps(data), time.time() + 900))
        return {'text': clean_name(label, 55), 'callback_data': 'ui:' + token}

    async def panel(self, update, text, rows):
        callback = update.get('callback_query')
        origin = callback['message'] if callback else update['message']
        chat_id = str(origin['chat']['id'])
        topic_id = None  # Management menus live in the main private conversation.
        keyboard = [[self._button(chat_id, topic_id, *entry) for entry in row] for row in rows]
        tokens = [b['callback_data'][3:] for row in keyboard for b in row]
        params = {'chat_id': chat_id, 'text': text, 'reply_markup': {'inline_keyboard': keyboard}}
        if callback and not origin.get('message_thread_id'):
            params['message_id'] = origin['message_id']
            try:
                result = await self.router.telegram.call('editMessageText', **params)
            except TelegramError as error:
                if error.code != 400 or 'message to edit not found' not in str(error).lower():
                    raise
                params.pop('message_id')
                if topic_id:
                    params['message_thread_id'] = topic_id
                result = await self.router.telegram.call('sendMessage', **params)
        else:
            if topic_id:
                params['message_thread_id'] = topic_id
            result = await self.router.telegram.call('sendMessage', **params)
        message_id = result['message_id']
        with self.db:
            # Buttons are tied to the exact chat, topic and message that displayed them.
            self.db.execute("UPDATE ui_buttons SET state='expired' WHERE chat_id=? AND message_id=?",
                            (chat_id, origin['message_id'] if callback else message_id))
            for token in tokens:
                self.db.execute('UPDATE ui_buttons SET message_id=?,state=\'active\' WHERE token=?',
                                (message_id, token))
        return result

    async def home(self, update):
        targets = self.router.recent.ordered(self.router.targets.values())[:12]
        cached = self._directory_values()
        directories = [self.cached_directory(t['thread_id'], 90, cached) for t in targets]
        lines = ['已绑定的原 Codex 会话（最近使用在前）：']
        for number, (target, directory) in enumerate(zip(targets, directories), 1):
            ready = self.router.bridges[target['key']].ready.is_set()
            status = '话题不可用，可重建' if target.get('topic_missing') else ('已连接' if ready else '连接中')
            lines.append(f"{number}. {clean_name(target['name'], 40)}（{status}）\n目录：{directory}")
        temporary = list(self.router.temporary_routes.values())
        if temporary:
            lines.append('临时 All 路由：')
            for number, target in enumerate(temporary, len(targets) + 1):
                state = '已开启' if target.get('enabled') else '已关闭'
                lines.append(f"{number}. {clean_name(target['name'], 40)} 【临时】（{state}，回复回到 All）")
        if len(self.router.targets) > len(targets):
            lines.append('更多会话请点击“添加会话”查看。')
        lines.append('All 是管理入口；普通消息选择接收会话后立即发送。')
        text = '\n\n'.join(lines) + '\n\n点击“添加会话”，选择本机已有的 Codex 会话并绑定到新话题。'
        text += '\n点击已绑定会话可管理或重新创建其话题入口。'
        rows = [[(f"{number}. {clean_name(t['name'], 40)}", 'select', {'thread_id': t['thread_id']})]
                for number, t in enumerate(targets, 1)]
        for number, target in enumerate(temporary, len(targets) + 1):
            rows.append([(f"{number}. {clean_name(target['name'], 40)} 【临时】",
                          'temp:select', {'key': target['key']})])
        # Keep session entries above; management actions stay together at the bottom.
        rows.extend([[('➕ 添加会话', 'catalog', {}), ('➕ 添加临时 All', 'temp:catalog', {})],
                     [('🆕 新建会话', 'new:start', {})]])
        count = self.guard.pending_count()
        if count:
            rows.append([(f'📨 待发送（{count}）', 'main:list', {})])
        for draft in self.creator.unfinished(self.router.owner):
            rows.append([('继续处理：' + clean_name(draft['name'] or '未确认的新会话', 30),
                          'new:resume', {'id': draft['id']})])
        for rename in self.renamer.unfinished():
            rows.append([('继续同步名称：' + clean_name(rename['new_name'] or '待确认', 25),
                          'rename:resume', {'id': rename['id']})])
        rows.append([('刷新', 'home', {})])
        text += '\n点击“新建会话”可选择或新建工作目录，并创建全新的 Codex 会话。'
        await self.panel(update, text, rows)

    def cache_directory(self, thread):
        """Retain display metadata; binding still requires a fresh native check."""
        thread_id = thread.get('id')
        if not isinstance(thread_id, str) or not thread_id or 'cwd' not in thread:
            return
        cwd = thread['cwd']
        cwd = cwd if isinstance(cwd, str) and cwd else None
        with self.db:
            self.db.execute('INSERT INTO ui_thread_directories VALUES(?,?,?) '
                            'ON CONFLICT(thread_id) DO UPDATE SET cwd=excluded.cwd,updated_at=excluded.updated_at',
                            (thread_id, cwd, time.time()))

    def _directory_values(self):
        # Historical picker catalogs are a display fallback only. Their expired
        # buttons and binding confirmations retain the usual validation.
        values = {}
        for row in self.db.execute('SELECT payload FROM ui_catalogs ORDER BY expires_at DESC'):
            try:
                groups = json.loads(row['payload'])
                for group in groups if isinstance(groups, list) else []:
                    if not isinstance(group, dict) or 'cwd' not in group:
                        continue
                    cwd = group['cwd']
                    if cwd is not None and not isinstance(cwd, str):
                        continue
                    for thread in group.get('threads', []):
                        if isinstance(thread, dict) and isinstance(thread.get('id'), str):
                            values.setdefault(thread['id'], cwd)
            except (TypeError, ValueError):
                continue
        try:
            snapshot = json.loads((Path(self.router.state_dir) / 'control/local.json').read_text())
            host = self.router.config.get('host_id')
            if isinstance(snapshot, dict) and (host is None or snapshot.get('host') == host):
                for entry in snapshot.get('entries', []):
                    if not isinstance(entry, dict) or not isinstance(entry.get('thread_id'), str) or 'cwd' not in entry:
                        continue
                    if host is not None and entry.get('host', host) != host:
                        continue
                    cwd = entry['cwd']
                    if cwd is None or isinstance(cwd, str):
                        values[entry['thread_id']] = cwd
        except (OSError, TypeError, ValueError):
            pass
        # Successful exact-thread reads take precedence over fallback snapshots.
        values.update({row['thread_id']: row['cwd'] for row in self.db.execute('SELECT * FROM ui_thread_directories')})
        return values

    def cached_directory(self, thread_id, limit=220, values=None):
        values = self._directory_values() if values is None else values
        return directory_text({'cwd': values[thread_id]}, limit) if thread_id in values else '目录暂不可用'

    async def directory(self, thread_id, limit=220):
        try:
            result = await asyncio.wait_for(self.router.catalog_call('thread/read', {'threadId': thread_id}), 5)
            thread = result['thread']
            if thread.get('id') != thread_id:
                return '暂时无法读取'
            self.cache_directory(thread)
            return directory_text(thread, limit)
        except (RpcError, ConnectionError, asyncio.TimeoutError):
            return '暂时无法读取'

    async def new_catalog(self):
        threads, seen, cursors, cursor = [], set(), set(), None
        for _ in range(100):
            params = {'limit': 100, 'sortKey': 'updated_at', 'sortDirection': 'desc', 'archived': False}
            if cursor:
                params['cursor'] = cursor
            result = await self.router.catalog_call('thread/list', params)
            for thread in result.get('data', []):
                if (thread.get('ephemeral') or thread.get('canAcceptDirectInput') is False
                        or thread['id'] in seen):
                    continue
                seen.add(thread['id'])
                self.cache_directory(thread)
                cwd = thread.get('cwd')
                cwd = cwd if isinstance(cwd, str) and cwd else None
                threads.append({'id': thread['id'], 'name': thread.get('name'), 'cwd': cwd})
            cursor = result.get('nextCursor')
            if not cursor:
                break
            if cursor in cursors:
                raise BindingError('原生会话列表分页异常，请重新加载。')
            cursors.add(cursor)
        else:
            raise BindingError('原生会话列表过大，暂时无法完整分组，请稍后重试。')
        # Sort the complete native result before grouping, so each directory's
        # first session also determines its position. Unknown access times keep
        # the native updated-at order as a stable fallback within this snapshot.
        groups = {}
        for thread in self.router.recent.ordered(threads):
            groups.setdefault(thread['cwd'], []).append({'id': thread['id'], 'name': thread['name']})
        catalog_id = secrets.token_urlsafe(12)
        payload = [{'cwd': cwd, 'threads': threads} for cwd, threads in groups.items()]
        with self.db:
            self.db.execute('DELETE FROM ui_catalogs WHERE expires_at<=?', (time.time(),))
            self.db.execute('INSERT INTO ui_catalogs VALUES(?,?,?)',
                            (catalog_id, json.dumps(payload, ensure_ascii=False), time.time() + 900))
        return catalog_id, payload

    async def catalog(self, update, data):
        temporary = bool(data.get('temporary'))
        catalog_id = data.get('catalog_id')
        if catalog_id:
            saved = self.db.execute('SELECT payload FROM ui_catalogs WHERE id=? AND expires_at>?',
                                    (catalog_id, time.time())).fetchone()
            if not saved:
                raise BindingError('这个目录列表已过期，请点击“重新加载”或重新打开 /sessions。')
            groups = json.loads(saved['payload'])
            with self.db:
                self.db.execute('UPDATE ui_catalogs SET expires_at=? WHERE id=?', (time.time() + 900, catalog_id))
            group_index = max(0, min(int(data.get('group', 0)), len(groups) - 1))
        else:
            catalog_id, groups = await self.new_catalog()
            group_index = next((i for i, g in enumerate(groups) if 'directory' in data and g['cwd'] == data['directory']), 0)
        host = self.router.config.get('host_id', '本机')
        if not groups:
            reload_data = {'temporary': True} if temporary else {}
            return await self.panel(update, f'{host} · ' + ('添加临时 All 路由' if temporary else '添加会话')
                                    + '\n未找到可用会话；可先在电脑上创建会话，再点“重新加载”。',
                                    [[('重新加载', 'catalog', reload_data), ('返回', 'home', {})]])
        if 'group' not in data and 'directory' not in data:
            return await self.directory_index(update, catalog_id, groups, data.get('dir_page', 0), temporary)
        group = groups[group_index]
        threads = group['threads']
        page_count = (len(threads) + 11) // 12
        page = max(0, min(int(data.get('page', 0)), page_count - 1))
        position = {'catalog_id': catalog_id, 'group': group_index, 'page': page}
        if temporary:
            position['temporary'] = True
        bound = {t['thread_id']: t for t in self.router.targets.values()}
        rows = []
        for number, thread in enumerate(threads[page * 12:(page + 1) * 12], page * 12 + 1):
            label = clean_name(thread.get('name') or '未命名会话', 32) + ' · ' + thread['id'][-6:]
            if thread['id'] in bound:
                label = ('⚠ 话题失效 ' if bound[thread['id']].get('topic_missing') else '✓ 已绑定 ') + label
            rows.append([(f'{number}. {label}', 'select', {'thread_id': thread['id'], 'catalog': position,
                                                           'temporary': temporary})])
        navigation = []
        if page:
            navigation.append(('上一页', 'catalog', {**position, 'page': page - 1}))
        if page + 1 < page_count:
            navigation.append(('下一页', 'catalog', {**position, 'page': page + 1}))
        if navigation:
            rows.append(navigation)
        directories = []
        if group_index:
            directories.append(('◀ 上个目录', 'catalog', {**position, 'group': group_index - 1, 'page': 0}))
        if group_index + 1 < len(groups):
            directories.append(('下个目录 ▶', 'catalog', {**position, 'group': group_index + 1, 'page': 0}))
        if directories:
            rows.append(directories)
        rows.append([('返回目录列表', 'catalog', {'catalog_id': catalog_id, 'dir_page': group_index // 12,
                                                  **({'temporary': True} if temporary else {})}),
                     ('重新加载', 'catalog', {'directory': group['cwd'], **({'temporary': True} if temporary else {})})])
        text = (f'{host} · ' + ('添加临时 All 路由' if temporary else '添加会话')
                + f'\n目录 {group_index + 1}/{len(groups)}\n'
                f"📁 {directory_text(group, 1200)}\n\n本目录 {len(threads)} 个会话，最近使用在前；未使用的按原生更新时间排序。")
        if page_count > 1:
            text += f'\n会话第 {page + 1}/{page_count} 页。'
        text += '\n请选择要接入的原会话：'
        await self.panel(update, text, rows)

    async def directory_index(self, update, catalog_id, groups, page=0, temporary=False):
        page_count = (len(groups) + 11) // 12
        page = max(0, min(int(page), page_count - 1))
        rows = []
        for index, group in enumerate(groups[page * 12:(page + 1) * 12], page * 12):
            label = f"{index + 1}. 📁 {directory_text(group, 36)} · {len(group['threads'])}个"
            rows.append([(label, 'catalog', {'catalog_id': catalog_id, 'group': index, 'page': 0,
                                             **({'temporary': True} if temporary else {})})])
        navigation = []
        if page:
            navigation.append(('上一页目录', 'catalog', {'catalog_id': catalog_id, 'dir_page': page - 1,
                                                         **({'temporary': True} if temporary else {})}))
        if page + 1 < page_count:
            navigation.append(('下一页目录', 'catalog', {'catalog_id': catalog_id, 'dir_page': page + 1,
                                                         **({'temporary': True} if temporary else {})}))
        if navigation:
            rows.append(navigation)
        rows.append([('重新加载', 'catalog', {'dir_page': page, **({'temporary': True} if temporary else {})}),
                     ('返回', 'home', {})])
        host = self.router.config.get('host_id', '本机')
        text = (f'{host} · ' + ('添加临时 All 路由' if temporary else '添加会话')
                + '\n本机会话目录\n\n'
                f"共 {len(groups)} 个目录，{sum(len(g['threads']) for g in groups)} 个会话。\n请选择目录，查看该目录的会话。")
        if page_count > 1:
            text += f'\n目录列表第 {page + 1}/{page_count} 页。'
        await self.panel(update, text, rows)

    async def selected(self, update, thread_id, catalog=None, temporary=False):
        if temporary:
            existing = next((t for t in self.router.temporary_routes.values() if t['thread_id'] == thread_id), None)
            if existing:
                return await self.temporary_panel(update, existing, catalog=catalog)
            if any(t['thread_id'] == thread_id for t in self.router.targets.values()):
                raise BindingError('这个会话已经绑定正式 Telegram 话题，不能重复添加为临时 All 路由。')
        else:
            existing = next((t for t in self.router.targets.values() if t['thread_id'] == thread_id), None)
            if existing:
                if existing.get('topic_missing'):
                    return await self.repair_selected(update, thread_id, existing['message_thread_id'])
                return await self.bound_panel(update, existing, already=True, catalog=catalog)
        result = await self.router.catalog_call('thread/read', {'threadId': thread_id})
        thread = result['thread']
        if thread['id'] != thread_id or thread.get('ephemeral') or thread.get('canAcceptDirectInput') is False:
            raise BindingError('所选会话当前不可绑定，请重新选择。')
        self.cache_directory(thread)
        if temporary:
            text = (f"确认添加临时 All 路由？\n\n名称：{clean_name(thread.get('name') or '未命名会话', 140)}\n"
                    f"主机：{self.router.config.get('host_id', '本机')}\nID：{thread_id}\n"
                    f"目录：{directory_text(thread)}\n\n不会创建 Telegram 话题；添加后立即在 All 启用此路由，回复会回到 All 并标注此会话名称。")
            rows = [[('确认添加临时路由', 'temp:confirm', {'thread_id': thread_id, 'catalog': catalog})],
                    [('返回会话列表', 'catalog', catalog or {'temporary': True}), ('取消', 'home', {})]]
        else:
            text = (f"确认接入这个原会话？\n\n名称：{clean_name(thread.get('name') or '未命名会话', 140)}\n"
                    f"主机：{self.router.config.get('host_id', '本机')}\nID：{thread_id}\n"
                    f"目录：{directory_text(thread)}\n\n确认后会创建新 Telegram 话题，沿用此会话的历史和设置。")
            rows = [[('确认绑定到新话题', 'confirm', {'thread_id': thread_id, 'catalog': catalog})],
                    [('返回会话列表', 'catalog', catalog or {}), ('取消', 'home', {})]]
        await self.panel(update, text, rows)

    async def temporary_panel(self, update, target, catalog=None):
        enabled = bool(target.get('enabled'))
        text = (f"临时 All 路由已存在。\n\n名称：{clean_name(target['name'], 140)}\n"
                f"标签：{clean_name(target['name'], 140)} 【临时】\n"
                f"ID：{target['thread_id']}\n状态：{'已开启' if enabled else '已关闭'}\n"
                '不会创建 Telegram 话题；开启后 All 选择此路由，回复回到 All 并带有该名称标签。')
        await self.panel(update, text, [
            [(('关闭' if enabled else '开启') + '临时路由', 'temp:toggle',
              {'key': target['key'], 'enabled': enabled})],
            [('修改名称', 'temp:rename', {'key': target['key'], 'thread_id': target['thread_id']})],
            [('取消临时绑定', 'temp:cancel', {'key': target['key'], 'thread_id': target['thread_id'],
                                               'enabled': enabled})],
            [('继续添加', 'temp:catalog', {}), ('返回', 'home', {})]])

    async def temporary_cancel(self, update, target, expected_enabled=None):
        enabled = bool(target.get('enabled'))
        if expected_enabled is not None and enabled != bool(expected_enabled):
            raise BindingError('这个临时路由状态已变化，请重新打开 /sessions。')
        text = (f"确认取消临时绑定？\n\n名称：{clean_name(target['name'], 140)}\n"
                f"ID：{target['thread_id']}\n状态：{'已开启' if enabled else '已关闭'}\n\n"
                '取消后会从当前 Bot 的 /sessions 和 All 选择列表移除；不会删除或修改原 Codex 会话、历史或本地队列。')
        await self.panel(update, text, [
            [('确认取消临时绑定', 'temp:remove', {'key': target['key'],
                                                   'thread_id': target['thread_id'],
                                                   'enabled': enabled})],
            [('返回临时路由', 'temp:select', {'key': target['key']})]])

    async def add_temporary(self, thread_id):
        existing = next((t for t in self.router.temporary_routes.values() if t['thread_id'] == thread_id), None)
        if existing:
            return existing
        if any(t['thread_id'] == thread_id for t in self.router.targets.values()):
            raise BindingError('这个会话已经绑定正式 Telegram 话题，不能重复添加为临时 All 路由。')
        thread = await self.router.verify_existing(thread_id)
        self.cache_directory(thread)
        name = clean_name(thread.get('name') or 'Codex ' + thread_id[-8:])
        if any(t['name'].casefold() == name.casefold() for t in self.router.temporary_routes.values()):
            name += ' · ' + thread_id[-6:]
        target = {'key': 'temp_' + hashlib.sha256(thread_id.encode()).hexdigest()[:32],
                  'name': name, 'thread_id': thread_id, 'enabled': True, 'label': name}
        return self.router.install_temporary_route(target)

    def operation(self, thread_id, target, status, error=None):
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO binding_ops VALUES(?,?,?,?,?)',
                            (thread_id, json.dumps(target, ensure_ascii=False), status, error, time.time()))

    def _topic_creation_cooldown(self):
        # The runtime bot identity comes from getMe; never reuse another Bot's
        # circuit breaker merely because its state directory was copied.
        route = [self.router.bot_id, str(self.router.owner)]
        key = TOPIC_CREATION_COOLDOWN_KEY + hashlib.sha256(json.dumps(route).encode()).hexdigest()[:24]
        return key, route

    def check_topic_creation_available(self):
        key, route = self._topic_creation_cooldown()
        row = self.db.execute('SELECT value FROM kv WHERE key=?', (key,)).fetchone()
        if not row:
            return
        try:
            saved = json.loads(row['value'])
            remaining = saved['until'] - time.time() if saved.get('route') == route else 0
        except (KeyError, TypeError, ValueError):
            return
        if remaining > 0:
            raise TopicCreationUnavailable(int(remaining + 0.999))

    async def bind(self, thread_id, allow_create=True):
        existing = next((t for t in self.router.targets.values() if t['thread_id'] == thread_id), None)
        if existing:
            if existing.get('topic_missing'):
                return await self.repair(thread_id, existing['message_thread_id'], allow_create)
            return existing
        previous = self.db.execute('SELECT * FROM binding_ops WHERE thread_id=?', (thread_id,)).fetchone()
        if previous:
            if previous['status'] in ('topic_created', 'configured', 'active'):
                target = json.loads(previous['target_json'])
                await self.router.install_target(target)
                self.operation(thread_id, target, 'active')
                return target
            if previous['status'] in ('creating', 'uncertain'):
                raise BindingError('上一次创建话题的结果尚未确认。请先核对 Telegram 中的话题，避免重复创建。')
        if not allow_create:
            raise BindingError('这个确认按钮已处理，请重新打开 /sessions。')
        thread = await self.router.verify_existing(thread_id)
        self.cache_directory(thread)
        name = clean_name(thread.get('name') or 'Codex ' + thread_id[-8:])
        if any(t['name'] == name for t in self.router.targets.values()):
            name += ' · ' + thread_id[-6:]
        target = {'key': 'session_' + hashlib.sha256(thread_id.encode()).hexdigest()[:32],
                  'name': name, 'thread_id': thread_id}
        target = await self.create_topic(thread_id, target)
        await self.router.install_target(target)
        self.operation(thread_id, target, 'active')
        return target

    async def create_topic(self, thread_id, target):
        self.check_topic_creation_available()
        self.operation(thread_id, target, 'creating')
        try:
            topic = await self.router.telegram.call('createForumTopic', chat_id=int(self.router.owner), name=target['name'])
        except TelegramError as error:
            if error.code == 400 and 'BOT_TOPIC_WRONG_HOME' in str(error).upper():
                key, route = self._topic_creation_cooldown()
                now = time.time()
                with self.db:
                    # Refusal and cooldown must survive together. Recovering
                    # a UI job must not repeat the request between these writes.
                    self.db.execute('INSERT OR REPLACE INTO binding_ops VALUES(?,?,?,?,?)',
                                    (thread_id, json.dumps(target, ensure_ascii=False), 'failed',
                                     'BOT_TOPIC_WRONG_HOME', now))
                    self.db.execute('INSERT OR REPLACE INTO kv VALUES(?,?)',
                                    (key, json.dumps({'route': route, 'code': 'BOT_TOPIC_WRONG_HOME',
                                                      'until': now + TOPIC_CREATION_COOLDOWN_SECONDS})))
                raise TopicCreationUnavailable(TOPIC_CREATION_COOLDOWN_SECONDS) from None
            status = 'uncertain' if error.code >= 500 else 'failed'
            self.operation(thread_id, target, status, 'Telegram ' + str(error.code))
            if status == 'uncertain':
                raise BindingError('Telegram 服务异常，话题创建结果待核对；不会自动重复创建。') from None
            raise BindingError('Telegram 未能创建话题，请检查话题功能后重新选择绑定。') from None
        except (ConnectionError, asyncio.TimeoutError):
            self.operation(thread_id, target, 'uncertain', 'Topic creation response unavailable')
            raise BindingError('话题创建结果未确认，请先检查 Telegram；不会自动重复创建。') from None
        topic_id = topic.get('message_thread_id')
        if not isinstance(topic_id, int) or topic_id <= 0:
            self.operation(thread_id, target, 'uncertain', 'Invalid topic response')
            raise BindingError('Telegram 的话题返回结果异常，需要先核对实际话题。')
        target['message_thread_id'] = topic_id
        self.operation(thread_id, target, 'topic_created')
        return target

    async def repair_selected(self, update, thread_id, topic_id):
        target = next((t for t in self.router.targets.values() if t['thread_id'] == thread_id), None)
        if not target:
            return await self.selected(update, thread_id)
        if target['message_thread_id'] != topic_id:
            return await self.bound_panel(update, target, already=True)
        thread = (await self.router.catalog_call('thread/read', {'threadId': thread_id}))['thread']
        if thread['id'] != thread_id:
            raise BindingError('会话 ID 不一致，请重新打开 /sessions。')
        self.cache_directory(thread)
        text = (f"重新创建这个会话的 Telegram 话题？\n\n名称：{clean_name(thread.get('name') or target['name'])}\n"
                f"ID：{thread_id}\n旧话题：{topic_id}\n\n新话题仍连接这个原会话，保留其历史和设置。"
                '旧话题的待回传消息不会被自动重发；已执行的请求也不会重复执行。')
        await self.panel(update, text, [[('确认重新创建话题', 'repair_confirm', {'thread_id': thread_id, 'topic_id': topic_id})],
                                        [('取消', 'home', {})]])

    async def repair(self, thread_id, topic_id, allow_create=True):
        existing = next((t for t in self.router.targets.values() if t['thread_id'] == thread_id), None)
        if not existing:
            raise BindingError('原绑定已改变，请重新打开 /sessions。')
        if existing['message_thread_id'] != topic_id:
            return existing  # A second click must not replace the topic created by the first.
        previous = self.db.execute('SELECT * FROM binding_ops WHERE thread_id=?', (thread_id,)).fetchone()
        if previous:
            saved = json.loads(previous['target_json'])
            if saved.get('previous_message_thread_id') == topic_id:
                if previous['status'] in ('topic_created', 'configured'):
                    await self.router.install_target(saved, replace_topic=True, expected_topic_id=topic_id)
                    self.operation(thread_id, saved, 'active')
                    return saved
                if previous['status'] in ('creating', 'uncertain'):
                    raise BindingError('上次重建话题的结果待核对，不会自动重复创建。')
        if not allow_create:
            raise BindingError('这个重建按钮已处理，请重新打开 /sessions。')
        thread = await self.router.verify_existing(thread_id)
        self.cache_directory(thread)
        target = {k: v for k, v in existing.items() if k not in ('message_thread_id', 'topic_missing', 'topic_missing_reason')}
        target.update({'name': clean_name(thread.get('name') or existing['name']), 'previous_message_thread_id': topic_id})
        target = await self.create_topic(thread_id, target)
        await self.router.install_target(target, replace_topic=True, expected_topic_id=topic_id)
        self.operation(thread_id, target, 'active')
        return target

    async def bound_panel(self, update, target, already=False, catalog=None):
        directory = await self.directory(target['thread_id'])
        text = (('此会话已经绑定。' if already else '已绑定，立即生效。') +
                f"\n\n请进入话题「{target['name']}」继续聊天。\nID：{target['thread_id']}\n目录：{directory}\n"
                '在该话题发送 /status 可核对会话。')
        await self.panel(update, text, [
            [('修改名称', 'rename:start', {'thread_id': target['thread_id']})],
            [('重新创建话题', 'repair_select', {'thread_id': target['thread_id'], 'topic_id': target['message_thread_id']})],
            [('继续添加', 'catalog', catalog or {}), ('返回', 'home', {})]])

    def _direct_home(self, update):
        if update.get('callback_query') or self.creator.is_reply(update) or self.renamer.is_reply(update):
            return False
        from attachments import payload_from
        message = update.get('message', {})
        words = message.get('text', '').strip().split(maxsplit=1)
        return bool(words and words[0].split('@')[0].lower() == '/sessions' and not payload_from(message))

    async def handle_one(self, lane=None):
        if lane not in (None, 'home', 'actions'):
            raise ValueError('Unknown session picker lane')
        job = None
        for candidate in self.db.execute("SELECT * FROM ui_jobs WHERE state='pending' ORDER BY id"):
            if lane is not None:
                direct_home = self._direct_home(json.loads(candidate['payload']))
                if direct_home != (lane == 'home'):
                    continue
            job = candidate
            break
        if job is None:
            return False
        if not self.router.owner_allowed():
            with self.db:
                self.db.execute("UPDATE ui_jobs SET state='denied' WHERE id=?", (job['id'],))
            return True
        update = json.loads(job['payload'])
        if not update.get('callback_query') and self.guard.is_message(update) and not self.guard.ready(update):
            return False
        with self.db:
            claimed = self.db.execute("UPDATE ui_jobs SET state='processing' WHERE id=? AND state='pending'", (job['id'],))
            if not claimed.rowcount:
                return False
        update = json.loads(job['payload'])
        callback = update.get('callback_query')
        words = update.get('message', {}).get('text', '').strip().split(maxsplit=1)
        try:
            if not callback:
                if self.creator.is_reply(update):
                    await self.creator.reply(update)
                elif self.renamer.is_reply(update):
                    await self.renamer.reply(update)
                elif self.guard.is_message(update):
                    await self.guard.present(update)
                elif words and words[0].split('@')[0].lower() == '/rename':
                    await self.renamer.command(update)
                else:
                    await self.home(update)
            else:
                try:
                    await self.router.telegram.call('answerCallbackQuery', callback_query_id=callback['id'])
                except (TelegramError, ConnectionError):
                    pass  # Old callback acknowledgements can expire while the durable job is pending.
                token = callback.get('data', '')
                button = self.db.execute('SELECT * FROM ui_buttons WHERE token=?', (token[3:],)).fetchone() if token.startswith('ui:') else None
                origin = callback['message']
                if (not button or button['expires_at'] < time.time() or button['state'] == 'expired'
                        or button['message_id'] != origin['message_id'] or button['chat_id'] != str(origin['chat']['id'])
                        or button['topic_id'] != (origin.get('message_thread_id') or None)):
                    raise BindingError('这个按钮已失效，请发送 /sessions 重新打开。')
                data, action = json.loads(button['data']), button['action']
                if action.startswith('new:'):
                    await self.creator.handle(update, action, data)
                elif action.startswith('rename:'):
                    await self.renamer.handle(update, action, data)
                elif action.startswith('main:'):
                    await self.guard.handle(update, action, data)
                elif action == 'temp:select':
                    target = self.router.temporary_routes.get(data.get('key'))
                    if target is None:
                        raise BindingError('未知临时路由，请重新打开 /sessions。')
                    await self.temporary_panel(update, target)
                elif action == 'temp:toggle':
                    current = self.router.temporary_routes.get(data.get('key'))
                    if current is None:
                        raise BindingError('未知临时路由，请重新打开 /sessions。')
                    expected = data.get('enabled')
                    if bool(current.get('enabled')) != bool(expected):
                        raise BindingError('这个开关已更新，请重新打开 /sessions。')
                    self.router.toggle_temporary_route(data['key'], not bool(expected), expected)
                    if expected:
                        await self.router.stop_target(data['key'])
                    await self.home(update)
                elif action == 'temp:cancel':
                    target = self.router.temporary_routes.get(data.get('key'))
                    if target is None or target['thread_id'] != data.get('thread_id'):
                        raise BindingError('未知临时路由，请重新打开 /sessions。')
                    await self.temporary_cancel(update, target, data.get('enabled'))
                elif action == 'temp:remove':
                    allow_remove = button['state'] == 'active'
                    with self.db:
                        self.db.execute("UPDATE ui_buttons SET state='consumed' WHERE token=?", (button['token'],))
                    if not allow_remove:
                        raise BindingError('这个按钮已处理，请重新打开 /sessions。')
                    removed = await self.router.remove_temporary_route(
                        data['key'], data.get('thread_id'), data.get('enabled'))
                    await self.panel(update, f"已取消「{clean_name(removed['name'], 140)}」的临时绑定。\n"
                                     '原 Codex 会话、历史和本地队列均已保留。',
                                     [[('返回会话管理', 'home', {})]])
                elif action == 'temp:rename':
                    target = self.router.temporary_routes.get(data.get('key'))
                    if target is None:
                        target = next((item for item in self.router.temporary_routes.values()
                                       if item['thread_id'] == data.get('thread_id')), None)
                    if target is None:
                        raise BindingError('未知临时路由，请重新打开 /sessions。')
                    await self.renamer.start(update, target['thread_id'])
                elif action == 'temp:catalog':
                    await self.catalog(update, {**data, 'temporary': True})
                elif action == 'temp:confirm':
                    allow_create = button['state'] == 'active'
                    with self.db:
                        self.db.execute("UPDATE ui_buttons SET state='consumed' WHERE token=?", (button['token'],))
                    if not allow_create:
                        raise BindingError('这个按钮已处理，请重新打开 /sessions。')
                    target = await self.add_temporary(data['thread_id'])
                    await self.temporary_panel(update, target)
                elif action == 'home':
                    await self.home(update)
                elif action == 'catalog':
                    await self.catalog(update, data)
                elif action == 'select':
                    await self.selected(update, data['thread_id'], data.get('catalog'), data.get('temporary', False))
                elif action == 'confirm':
                    allow_create = button['state'] == 'active'
                    with self.db:
                        self.db.execute("UPDATE ui_buttons SET state='consumed' WHERE token=?", (button['token'],))
                    target = await self.bind(data['thread_id'], allow_create=allow_create)
                    await self.bound_panel(update, target, catalog=data.get('catalog'))
                elif action == 'repair_select':
                    await self.repair_selected(update, data['thread_id'], data['topic_id'])
                elif action == 'repair_confirm':
                    allow_create = button['state'] == 'active'
                    with self.db:
                        self.db.execute("UPDATE ui_buttons SET state='consumed' WHERE token=?", (button['token'],))
                    target = await self.repair(data['thread_id'], data['topic_id'], allow_create)
                    await self.bound_panel(update, target)
                else:
                    raise BindingError('未知按钮，请发送 /sessions 重新打开。')
            with self.db:
                self.db.execute("UPDATE ui_jobs SET state='done' WHERE id=?", (job['id'],))
        except (BindingError, RpcError, ConnectionError, asyncio.TimeoutError, OSError, ValueError, TelegramError) as error:
            detail = str(error) if isinstance(error, BindingError) else '暂时无法完成，请稍后发送 /sessions 重试。'
            with self.db:
                self.db.execute("UPDATE ui_jobs SET state='failed',error=? WHERE id=?", (type(error).__name__, job['id']))
                origin = callback['message'] if callback else update['message']
                self.db.execute('INSERT OR IGNORE INTO notices(id,chat_id,topic_id,text) VALUES(?,?,?,?)',
                                (job['id'], str(origin['chat']['id']), None, detail))
        return True

    async def loop(self):
        while True:
            if not await self.handle_one(lane='actions'):
                await asyncio.sleep(0.2)

    async def home_loop(self):
        while True:
            if not await self.handle_one(lane='home'):
                await asyncio.sleep(0.2)

    async def recover(self):
        self.creator.recover()
        self.guard.recover()
        self.renamer.recover()
        with self.db:
            self.db.execute("UPDATE ui_jobs SET state='pending' WHERE state='processing'")
            # createForumTopic has no idempotency key; an interrupted call needs review.
            self.db.execute("UPDATE binding_ops SET status='uncertain' WHERE status='creating'")
        for row in self.db.execute("SELECT * FROM binding_ops WHERE status IN ('topic_created','configured','active')").fetchall():
            target = json.loads(row['target_json'])
            old_topic = target.get('previous_message_thread_id')
            await self.router.install_target(target, replace_topic=old_topic is not None, expected_topic_id=old_topic)
            self.operation(row['thread_id'], target, 'active')
