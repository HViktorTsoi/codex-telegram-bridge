"""Read-only, topic-local browser for native Codex agent trees."""
import asyncio
import inspect
import json
import logging
import math
import secrets
import time

from agents_catalog import AgentCatalog, AgentCatalogError
from attachments import payload_from
from bridge import TelegramError, permitted
from message_format import plain_chunks, utf16_size


LOG = logging.getLogger('codex_telegram_agents')


class AgentsMenuError(ValueError):
    """A safe explanation that can be shown to the owner."""


class DeliveryUncertain(ConnectionError):
    pass


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def decoded(value, default):
    try:
        result = json.loads(value)
        return result if isinstance(result, type(default)) else default
    except (TypeError, ValueError):
        return default


def text(value, limit=180, multiline=False):
    value = ''.join(c for c in str(value or '') if c >= ' ' or c in '\n\t')
    value = value.strip() if multiline else ' '.join(value.split())
    if utf16_size(value) <= limit:
        return value
    return next(plain_chunks(value, max(1, limit - 12)), '') + '…（已截断）'


def status(value):
    return {'active': '工作中', 'idle': '空闲', 'notLoaded': '未加载',
            'systemError': '系统错误', 'unknown': '状态未知'}.get(value, '状态未知')


class AgentsMenu:
    PREFIX = 'ag:'
    PAGE = 6
    TTL = 900
    TIMEOUT = 45

    def __init__(self, router, clock=time.time):
        self.router, self.db, self.clock = router, router.store.db, clock
        self.catalog = AgentCatalog(router)
        self.locks = {}
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS ag_jobs(
                id INTEGER PRIMARY KEY,payload TEXT,job_key TEXT,origin_json TEXT,
                bot_id INTEGER,owner TEXT,state TEXT DEFAULT 'pending',retry_at REAL DEFAULT 0,
                panel_id TEXT,error TEXT);
            CREATE TABLE IF NOT EXISTS ag_panels(
                id TEXT PRIMARY KEY,chat_id TEXT,topic_id INTEGER,bot_id INTEGER,owner TEXT,
                origin_json TEXT,root_json TEXT,message_id INTEGER,revision INTEGER DEFAULT 0,
                state TEXT DEFAULT 'creating',expires_at REAL,view TEXT DEFAULT 'agents',
                page INTEGER DEFAULT 0,filter TEXT DEFAULT 'all',agent_id TEXT);
            CREATE TABLE IF NOT EXISTS ag_buttons(
                token TEXT PRIMARY KEY,panel_id TEXT,revision INTEGER,action TEXT,value TEXT,
                state TEXT DEFAULT 'creating');
        ''')

    @staticmethod
    def _origin(update):
        return update.get('callback_query', {}).get('message') or update.get('message') or {}

    @staticmethod
    def _topic(message):
        return message.get('message_thread_id') or None

    @staticmethod
    def binding(target):
        return {'target_key': target['key'], 'thread_id': target['thread_id'],
                'topic_id': target['message_thread_id']}

    def _identity(self):
        bot = getattr(self.router, 'bot_id', None)
        username = str(getattr(self.router, 'username', '') or '').lstrip('@').casefold()
        expected = self.router.config.get('expected_bot_username', '').lstrip('@').casefold()
        if (type(bot) is not int or bot <= 0 or not self.router.owner_allowed()
                or (expected and expected != username)):
            raise AgentsMenuError('机器人身份或访问权限已变化，请重新发送 /agents。')
        return bot, str(self.router.owner)

    def _authorized(self, update):
        try:
            bot, owner = self._identity()
            origin, callback = self._origin(update), update.get('callback_query')
            actor = callback.get('from', {}) if callback else origin.get('from', {})
            if not permitted({'from': actor, 'chat': origin.get('chat', {})}, self.router.access(), owner):
                return False
            return not callback or (origin.get('from', {}).get('id') == bot
                and type(origin.get('message_id')) is int and origin['message_id'] > 0
                and origin.get('date') != 0)
        except (AgentsMenuError, TypeError, KeyError, ValueError):
            return False

    def _allowed(self, root_id):
        try:
            return bool(isinstance(root_id, str) and root_id and self.router.thread_allowed(root_id))
        except Exception:
            return False

    def _context(self, message):
        topic = self._topic(message)
        target = self.router.by_topic.get(topic) if topic is not None else None
        return {'topic_id': topic, 'binding': self.binding(target) if target else None}

    def _validate_context(self, context):
        self._identity()
        topic, binding = context.get('topic_id'), context.get('binding')
        target = self.router.by_topic.get(topic) if topic is not None else None
        if binding is None:
            if target is not None:
                raise AgentsMenuError('话题绑定已变化，请在当前话题重新发送 /agents。')
            return
        key = binding.get('target_key')
        actual, bridge = self.router.targets.get(key), self.router.bridges.get(key)
        if (not target or not actual or not bridge or target.get('topic_missing')
                or actual.get('topic_missing') or bridge.target.get('topic_missing')
                or self.binding(target) != binding or self.binding(actual) != binding
                or self.binding(bridge.target) != binding or bridge.thread_id != binding.get('thread_id')
                or not bridge.owner_allowed() or not self._allowed(binding.get('thread_id'))):
            raise AgentsMenuError('话题绑定已变化，请在当前话题重新发送 /agents。')
        store = getattr(bridge, 'store', None)
        if store is not None and store.get('bound_thread_id') != bridge.thread_id:
            raise AgentsMenuError('话题绑定已变化，请在当前话题重新发送 /agents。')

    def _current_root_binding(self, root_id):
        matches = [t for t in self.router.targets.values() if t.get('thread_id') == root_id]
        if len(matches) > 1:
            raise AgentsMenuError('主会话绑定信息不一致，请重新发送 /agents。')
        if not matches:
            return None, None
        target = matches[0]
        bridge = self.router.bridges.get(target['key'])
        if (not bridge or not bridge.owner_allowed() or bridge.thread_id != root_id
                or self.binding(bridge.target) != self.binding(target)):
            raise AgentsMenuError('主会话绑定已变化，请重新发送 /agents。')
        return target['key'], target['message_thread_id']

    def _validate_root(self, root):
        if not isinstance(root, dict) or not self._allowed(root.get('id')):
            raise AgentsMenuError('这个主会话不在当前机器人的会话范围内，请重新发送 /agents。')
        if self._current_root_binding(root['id']) != (root.get('bound_key'), root.get('topic_id')):
            raise AgentsMenuError('主会话绑定已变化，请重新发送 /agents。')

    def _validate_panel(self, update, panel):
        origin = self._origin(update)
        if (not self._authorized(update) or self._identity() != (panel['bot_id'], panel['owner'])
                or panel['chat_id'] != str(origin.get('chat', {}).get('id'))
                or panel['topic_id'] != self._topic(origin)):
            raise AgentsMenuError('代理面板的位置或权限已变化，请重新发送 /agents。')
        self._validate_context(decoded(panel['origin_json'], {}))

    def ingest(self, update):
        callback, message = update.get('callback_query'), update.get('message') or {}
        if callback:
            data = callback.get('data')
            if not isinstance(data, str) or not data.startswith(self.PREFIX):
                return False
        else:
            raw = message.get('text')
            words = raw.strip().split() if isinstance(raw, str) else []
            if (len(words) != 1 or payload_from(message) or any(message.get(k) for k in
                    ('forward_origin', 'forward_date', 'forward_from', 'forward_from_chat', 'is_automatic_forward'))):
                return False
            command, _, username = words[0].partition('@')
            if command.casefold() != '/agents' or (username and username.casefold() !=
                    str(getattr(self.router, 'username', '') or '').lstrip('@').casefold()):
                return False
        origin = self._origin(update)
        with self.db:
            if self._authorized(update):
                bot, owner = self._identity()
                context = self._context(origin)
                self.db.execute('INSERT OR IGNORE INTO ag_jobs'
                    '(id,payload,job_key,origin_json,bot_id,owner) VALUES(?,?,?,?,?,?)',
                    (update['update_id'], encoded(update), encoded([owner, self._topic(origin)]),
                     encoded(context), bot, owner))
            self.db.execute("UPDATE kv SET value=? WHERE key='offset'",
                (str(max(self.router.store.offset(), update['update_id'] + 1)),))
        return True

    async def _catalog_call(self, method, *args, **kwargs):
        result = getattr(self.catalog, method)(*args, **kwargs)
        return await result if inspect.isawaitable(result) else result

    async def _answer(self, update, message=None):
        if not update.get('callback_query') or not self._authorized(update):
            return
        params = {'callback_query_id': update['callback_query']['id']}
        if message:
            params.update(text=text(message, 180), show_alert=True)
        try:
            await asyncio.wait_for(self.router.telegram.call('answerCallbackQuery', **params), 5)
        except (TelegramError, ConnectionError, OSError, asyncio.TimeoutError):
            pass

    def _panel(self, panel_id):
        return self.db.execute('SELECT * FROM ag_panels WHERE id=?', (panel_id,)).fetchone()

    def _button(self, update):
        origin = self._origin(update)
        row = self.db.execute('SELECT * FROM ag_buttons WHERE token=?',
                              (update['callback_query']['data'][len(self.PREFIX):],)).fetchone()
        panel = self._panel(row['panel_id']) if row else None
        if (not panel or row['state'] != 'active' or panel['state'] != 'active'
                or row['revision'] != panel['revision'] or panel['expires_at'] <= self.clock()
                or panel['message_id'] != origin.get('message_id')):
            raise AgentsMenuError('这个代理按钮已过期或失效，请重新发送 /agents。')
        self._validate_panel(update, panel)
        return row, panel

    async def _emit(self, update, panel, body, rows, *, view, root=None,
                    page=0, selection='all', agent_id=None, closed=False):
        self._validate_panel(update, panel)
        if root:
            self._validate_root(root)
            binding = decoded(panel['origin_json'], {}).get('binding')
            if not binding or binding.get('thread_id') != root['id']:
                raise AgentsMenuError('代理面板的会话绑定已变化，请重新发送 /agents。')
        revision, tokens, keyboard = panel['revision'] + 1, [], []
        with self.db:
            for row in rows:
                buttons = []
                for title, action, value in row:
                    token = secrets.token_urlsafe(12)
                    self.db.execute('INSERT INTO ag_buttons(token,panel_id,revision,action,value) VALUES(?,?,?,?,?)',
                        (token, panel['id'], revision, action, encoded(value)))
                    tokens.append(token)
                    buttons.append({'text': text(title, 55), 'callback_data': self.PREFIX + token})
                keyboard.append(buttons)
            self.db.execute("UPDATE ag_panels SET state='updating' WHERE id=?", (panel['id'],))
        params = {'chat_id': panel['chat_id'], 'text': text(body, 3800, multiline=True),
                  'reply_markup': {'inline_keyboard': keyboard}, 'disable_web_page_preview': True}
        editing = panel['message_id'] is not None
        if editing:
            params['message_id'] = panel['message_id']
        else:
            params['disable_notification'] = True
            if panel['topic_id'] is not None:
                params['message_thread_id'] = panel['topic_id']
        # Do not await between this final authorization check and the request.
        self._validate_panel(update, panel)
        if root:
            self._validate_root(root)
        try:
            result = await self.router.telegram.call('editMessageText' if editing else 'sendMessage', **params)
        except TelegramError as error:
            if editing and error.code == 400 and 'message is not modified' in str(error).lower():
                result = {'message_id': panel['message_id']}
            else:
                with self.db:
                    self.db.execute("UPDATE ag_buttons SET state='discarded' WHERE panel_id=? AND revision=?",
                                    (panel['id'], revision))
                    state = panel['state'] if error.code in (400, 403, 404, 429) else 'uncertain'
                    self.db.execute('UPDATE ag_panels SET state=? WHERE id=?', (state, panel['id']))
                if error.code in (400, 403, 404, 429):
                    raise
                raise DeliveryUncertain('Telegram menu delivery was not confirmed') from None
        except (Exception, asyncio.CancelledError):
            with self.db:
                self.db.execute("UPDATE ag_panels SET state='uncertain' WHERE id=?", (panel['id'],))
            raise DeliveryUncertain('Telegram menu delivery was not confirmed') from None
        try:
            self._validate_panel(update, panel)
            if root:
                self._validate_root(root)
            mid = result.get('message_id') if isinstance(result, dict) else panel['message_id'] if editing and result is True else None
            if (type(mid) is not int or mid <= 0 or (editing and mid != panel['message_id'])
                    or isinstance(result, dict) and result.get('chat') and
                    str(result['chat'].get('id')) != panel['chat_id']
                    or isinstance(result, dict) and result.get('message_thread_id') not in (None, panel['topic_id'])):
                raise DeliveryUncertain('Telegram menu acknowledgement was not confirmed')
        except Exception:
            with self.db:
                self.db.execute("UPDATE ag_panels SET state='uncertain' WHERE id=?", (panel['id'],))
            raise DeliveryUncertain('Telegram menu acknowledgement was not confirmed') from None
        with self.db:
            self.db.execute("UPDATE ag_buttons SET state='expired' WHERE panel_id=?", (panel['id'],))
            if not closed:
                self.db.executemany("UPDATE ag_buttons SET state='active' WHERE token=?", [(v,) for v in tokens])
            self.db.execute('UPDATE ag_panels SET message_id=?,revision=?,state=?,expires_at=?,view=?,root_json=?,page=?,filter=?,agent_id=? WHERE id=?',
                (mid, revision, 'closed' if closed else 'active', self.clock() + self.TTL, view,
                 encoded(root), page, selection, agent_id, panel['id']))

    def _navigation(self, count, page, selection):
        pages = max(1, math.ceil(count / self.PAGE))
        page = max(0, min(int(page), pages - 1))
        nav = []
        if page:
            nav.append(('上一页', 'page', page - 1))
        if page + 1 < pages:
            nav.append(('下一页', 'page', page + 1))
        rows = [nav] if nav else []
        rows.append([('✓ 全部' if selection == 'all' else '全部', 'filter', 'all'),
                     ('✓ 工作中' if selection == 'active' else '工作中', 'filter', 'active')])
        return page, pages, rows

    async def _render(self, update, panel, *, view=None, root=None, page=None,
                      selection=None, agent_id=None, refresh=False):
        self._validate_panel(update, panel)
        view = view or panel['view']
        root = root if root is not None else decoded(panel['root_json'], {}) or None
        page = panel['page'] if page is None else page
        selection = selection or panel['filter']
        if selection not in ('all', 'active'):
            raise AgentsMenuError('筛选条件已失效，请重新发送 /agents。')
        self._validate_root(root)
        if view == 'detail':
            agent_id = agent_id or panel['agent_id']
            detail = await self._catalog_call('detail', root['id'], agent_id)
            if (not isinstance(detail, dict) or detail.get('id') != agent_id
                    or detail.get('root_id') != root['id']):
                raise AgentsMenuError('代理详情信息不完整，请刷新列表重试。')
            body = 'Codex 子代理 · ' + text(detail.get('name') or agent_id, 120)
            body += '\n状态：' + status(detail.get('status')) + (' · 已归档' if detail.get('archived') else '')
            body += '\n主会话：' + text(root.get('name') or root['id'], 100)
            body += '\n代理 ID：' + text(agent_id, 100)
            body += '\n父代理：' + text(detail.get('parent_id') or '无（主会话）', 100)
            body += '\n层级：' + text(detail.get('depth', '?'), 15)
            if detail.get('role'):
                body += '\n角色：' + text(detail['role'], 90)
            if detail.get('nickname'):
                body += '\n昵称：' + text(detail['nickname'], 90)
            if detail.get('agent_path'):
                body += '\n代理路径：' + text(detail['agent_path'], 200)
            body += '\n工作目录：' + text(detail.get('cwd') or '路径未知', 260)
            if detail.get('last_turn_status'):
                body += '\n上一轮状态：' + text(detail['last_turn_status'], 70)
            body += '\n\n任务\n' + text(detail.get('task') or '暂无公开任务文本。', 1000, multiline=True)
            body += '\n\n最新公开结果\n' + text(detail.get('latest_reply') or '暂无公开结果。', 1300, multiline=True)
            rows = [[('返回列表', 'back', None), ('刷新', 'refresh', None), ('关闭', 'close', None)]]
            await self._emit(update, panel, body, rows, view=view, root=root,
                             page=page, selection=selection, agent_id=agent_id)
            return
        if view != 'agents':
            raise AgentsMenuError('代理面板已失效，请重新发送 /agents。')
        agents = await self._catalog_call('agents', root['id'], refresh=refresh)
        if not isinstance(agents, list):
            raise AgentsMenuError('子代理目录信息不完整，请刷新重试。')
        records = [a for a in agents if isinstance(a, dict) and a.get('id') and
                   (selection == 'all' or a.get('status') == 'active')]
        if len([a for a in agents if not isinstance(a, dict) or not a.get('id')]):
            raise AgentsMenuError('子代理目录信息不完整，请刷新重试。')
        page, pages, navigation = self._navigation(len(records), page, selection)
        body = 'Codex 子代理 · ' + text(root.get('name') or root['id'], 100)
        body += '\n' + ('全部层级 · 含历史与归档' if selection == 'all' else '全部层级 · 工作中')
        body += f'\n共 {len(records)} 个子代理 · 第 {page + 1}/{pages} 页'
        rows = []
        for index, item in enumerate(records[page * self.PAGE:(page + 1) * self.PAGE], page * self.PAGE + 1):
            name = text(item.get('name') or item['id'], 70)
            body += f'\n\n{index}. {name}\n{status(item.get("status"))} · 层级 {text(item.get("depth", "?"), 10)}'
            if item.get('archived'):
                body += ' · 已归档'
            if item.get('preview'):
                body += '\n' + text(item['preview'], 125)
            rows.append([(f'{index}. {name}', 'detail', item['id'])])
        if not records:
            body += '\n\n这个主会话暂无子代理。' if selection == 'all' else '\n\n当前没有工作中的子代理。'
        rows += navigation + [[('刷新', 'refresh', None), ('关闭', 'close', None)]]
        await self._emit(update, panel, body, rows, view='agents', root=root, page=page, selection=selection)

    async def _open(self, update, job):
        context = decoded(job['origin_json'], {})
        self._validate_context(context)
        if not context.get('binding'):
            raise AgentsMenuError('请在目标 Codex 会话的已绑定话题发送 /agents，以查看该会话的全部子代理。')
        panel = self._panel(job['panel_id']) if job['panel_id'] else None
        if panel is not None and panel['state'] == 'uncertain':
            raise DeliveryUncertain('Previous menu send was not confirmed')
        if panel is None:
            binding = context.get('binding')
            target = self.router.targets[binding['target_key']]
            root = {'id': binding['thread_id'], 'name': target.get('name') or binding['thread_id'],
                    'bound_key': binding['target_key'], 'topic_id': binding['topic_id']}
            panel_id = secrets.token_urlsafe(15)
            with self.db:
                self.db.execute('INSERT INTO ag_panels'
                    '(id,chat_id,topic_id,bot_id,owner,origin_json,root_json,expires_at,view) VALUES(?,?,?,?,?,?,?,?,?)',
                    (panel_id, job['owner'], context.get('topic_id'), job['bot_id'], job['owner'],
                     job['origin_json'], encoded(root), self.clock() + self.TTL, 'agents'))
                self.db.execute('UPDATE ag_jobs SET panel_id=? WHERE id=?', (panel_id, job['id']))
            panel = self._panel(panel_id)
        await self._render(update, panel)

    async def _callback(self, update):
        button, panel = self._button(update)
        action, value = button['action'], json.loads(button['value'])
        await self._answer(update)
        if action == 'close':
            await self._emit(update, panel, '已关闭代理列表。', [], view=panel['view'],
                             root=decoded(panel['root_json'], {}) or None, closed=True)
        elif action == 'detail':
            await self._render(update, panel, view='detail', agent_id=value)
        elif action == 'back':
            await self._render(update, panel, view='agents')
        elif action == 'page':
            if type(value) is not int or value < 0:
                raise AgentsMenuError('页码已失效，请刷新列表。')
            await self._render(update, panel, page=value)
        elif action == 'filter':
            await self._render(update, panel, selection=value, page=0)
        elif action == 'refresh':
            await self._render(update, panel, refresh=True)
        else:
            raise AgentsMenuError('代理按钮已失效，请重新发送 /agents。')

    async def _problem(self, update, job, message):
        if not self._authorized(update):
            return
        if update.get('callback_query'):
            await self._answer(update, message)
            return
        # Keep errors in the original topic, including an unbound topic.
        params = {'chat_id': job['owner'], 'text': text(message, 1200, multiline=True),
                  'disable_notification': True, 'disable_web_page_preview': True}
        topic = decoded(job['origin_json'], {}).get('topic_id')
        if topic is not None:
            params['message_thread_id'] = topic
        try:
            result = await self.router.telegram.call('sendMessage', **params)
        except TelegramError:
            raise
        except (Exception, asyncio.CancelledError):
            raise DeliveryUncertain('Telegram menu error acknowledgement was not confirmed') from None
        if (not isinstance(result, dict) or type(result.get('message_id')) is not int
                or result['message_id'] <= 0
                or result.get('chat') and str(result['chat'].get('id')) != job['owner']
                or result.get('message_thread_id') not in (None, topic)):
            raise DeliveryUncertain('Telegram menu error acknowledgement was not confirmed')

    async def _process(self, row):
        lock = self.locks.setdefault(row['job_key'], asyncio.Lock())
        async with lock:
            with self.db:
                claimed = self.db.execute("UPDATE ag_jobs SET state='processing' WHERE id=? AND state='pending' AND retry_at<=?",
                                          (row['id'], self.clock())).rowcount
            if not claimed:
                return
            update = json.loads(row['payload'])
            if not self._authorized(update) or self._identity() != (row['bot_id'], row['owner']):
                with self.db:
                    self.db.execute("UPDATE ag_jobs SET state='denied' WHERE id=?", (row['id'],))
                return
            try:
                self._validate_context(decoded(row['origin_json'], {}))
                operation = self._callback(update) if update.get('callback_query') else self._open(update, row)
                await asyncio.wait_for(operation, self.TIMEOUT)
            except TelegramError as error:
                if error.code == 429:
                    delay = error.retry_after if isinstance(error.retry_after, (int, float)) else 3
                    with self.db:
                        self.db.execute("UPDATE ag_jobs SET state='pending',retry_at=?,error='Telegram:429' WHERE id=?",
                                        (self.clock() + max(1, delay), row['id']))
                    return
                with self.db:
                    self.db.execute("UPDATE ag_jobs SET state='failed',error=? WHERE id=?", ('Telegram:' + str(error.code), row['id']))
                await self._answer(update, 'Telegram 暂时无法更新面板，请重新发送 /agents。')
            except DeliveryUncertain:
                with self.db:
                    self.db.execute("UPDATE ag_jobs SET state='uncertain',error='Delivery unconfirmed; not retried' WHERE id=?", (row['id'],))
                LOG.warning('Agent menu delivery unconfirmed; preserving without retry')
            except Exception as error:
                safe = isinstance(error, (AgentCatalogError, AgentsMenuError))
                message = str(error) if safe else '代理目录暂时无法读取，未能确认当前状态。请稍后重新发送 /agents。'
                with self.db:
                    self.db.execute("UPDATE ag_jobs SET state='failed',error=? WHERE id=?", (type(error).__name__, row['id']))
                try:
                    await self._problem(update, row, message)
                except TelegramError as problem:
                    with self.db:
                        self.db.execute("UPDATE ag_jobs SET state=?,error=? WHERE id=?",
                            ('pending' if problem.code == 429 else 'failed' if problem.code in (400, 403, 404)
                             else 'uncertain', 'Telegram:' + str(problem.code), row['id']))
                        if problem.code == 429:
                            self.db.execute('UPDATE ag_jobs SET retry_at=? WHERE id=?',
                                            (self.clock() + max(1, problem.retry_after), row['id']))
                except Exception:
                    with self.db:
                        self.db.execute("UPDATE ag_jobs SET state='uncertain',error='Error notice unconfirmed' WHERE id=?", (row['id'],))
                LOG.warning('Agent menu unavailable: %s', type(error).__name__)
            else:
                with self.db:
                    self.db.execute("UPDATE ag_jobs SET state='done',error=NULL WHERE id=?", (row['id'],))

    async def handle_one(self):
        row = self.db.execute("SELECT * FROM ag_jobs WHERE state='pending' AND retry_at<=? ORDER BY id LIMIT 1",
                              (self.clock(),)).fetchone()
        if row is None:
            return False
        await self._process(row)
        return True

    def recover(self):
        with self.db:
            self.db.execute("UPDATE ag_jobs SET state='uncertain',error='Restart during menu operation; not retried' WHERE state='processing'")
            self.db.execute("UPDATE ag_panels SET state='uncertain' WHERE state IN ('creating','updating')")

    async def loop(self):
        jobs = {}
        try:
            while True:
                for key, task in list(jobs.items()):
                    if task.done():
                        try:
                            await task
                        except Exception as error:
                            LOG.warning('Agent menu job failed: %s', type(error).__name__)
                        jobs.pop(key)
                rows = self.db.execute("SELECT * FROM ag_jobs WHERE state='pending' AND retry_at<=? ORDER BY id",
                                       (self.clock(),)).fetchall()
                for row in rows:
                    if row['job_key'] not in jobs:
                        jobs[row['job_key']] = asyncio.create_task(self._process(row))
                await asyncio.sleep(.1)
        finally:
            for task in jobs.values():
                task.cancel()
            await asyncio.gather(*jobs.values(), return_exceptions=True)
