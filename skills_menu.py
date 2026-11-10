"""Read-only local skills menus with explicit, durable task prompts."""
import asyncio
import copy
import json
import logging
from pathlib import Path
import secrets
import time

from attachments import payload_from
from bridge import RpcError, TelegramError, permitted
from message_format import plain_chunks, utf16_size


LOG = logging.getLogger('codex_telegram_skills')
PROMPT_TITLE = '使用技能 · '


class SkillsError(Exception):
    pass


def clean(value, limit=240):
    return ' '.join(str(value or '').split())[:limit]


class SkillsMenu:
    PAGE = 8
    TTL = 900
    TIMEOUT = 5

    def __init__(self, router):
        self.router, self.db = router, router.store.db
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS skills_jobs(
                id INTEGER PRIMARY KEY,payload TEXT,state TEXT DEFAULT 'pending',error TEXT);
            CREATE TABLE IF NOT EXISTS skills_buttons(
                token TEXT PRIMARY KEY,chat_id TEXT,topic_id INTEGER,message_id INTEGER,
                bot_id INTEGER,action TEXT,data TEXT,expires_at REAL,state TEXT DEFAULT 'active');
            CREATE TABLE IF NOT EXISTS skills_catalogs(
                id TEXT PRIMARY KEY,chat_id TEXT,topic_id INTEGER,bot_id INTEGER,
                target_json TEXT,cwd TEXT,skills_json TEXT,query TEXT,warning INTEGER,
                expires_at REAL);
            CREATE TABLE IF NOT EXISTS skills_prompts(
                id TEXT PRIMARY KEY,chat_id TEXT,topic_id INTEGER,message_id INTEGER,bot_id INTEGER,
                catalog_id TEXT,selection_json TEXT,state TEXT,update_id INTEGER,
                expires_at REAL);
            CREATE UNIQUE INDEX IF NOT EXISTS skills_prompt_message
                ON skills_prompts(chat_id,message_id) WHERE message_id IS NOT NULL;
        ''')

    @staticmethod
    def binding(target):
        return {'target_key': target['key'], 'thread_id': target['thread_id'],
                'topic_id': target['message_thread_id']}

    @staticmethod
    def _origin(update):
        return update.get('callback_query', {}).get('message') or update.get('message', {})

    @staticmethod
    def _topic(message):
        return message.get('message_thread_id') or None

    def _authorized(self, update):
        callback = update.get('callback_query')
        message = self._origin(update)
        identity = {'from': callback.get('from', {}) if callback else message.get('from', {}),
                    'chat': message.get('chat', {})}
        if not permitted(identity, self.router.access(), self.router.owner):
            return False
        if callback:
            return (bool(self.router.bot_id) and message.get('from', {}).get('id') == self.router.bot_id
                    and bool(message.get('message_id')) and message.get('date') != 0)
        return True

    def _prompt_reply(self, message):
        parent = message.get('reply_to_message') or {}
        if not parent or not self.router.bot_id or parent.get('from', {}).get('id') != self.router.bot_id:
            return False
        known = self.db.execute('SELECT 1 FROM skills_prompts WHERE chat_id=? AND message_id=?',
                                (str(message.get('chat', {}).get('id')), parent.get('message_id'))).fetchone()
        return bool(known or parent.get('text', '').startswith(PROMPT_TITLE))

    def ingest(self, update):
        callback = update.get('callback_query')
        message = update.get('message', {})
        if callback:
            if not str(callback.get('data', '')).startswith('sk:'):
                return False
        else:
            text = message.get('text', '').strip()
            words = text.split(maxsplit=1)
            command = words[0].split('@')[0].lower() if words else ''
            prompt_reply = self._prompt_reply(message)
            if prompt_reply and command.startswith('/') and command not in ('/cancel', '/skills') and not payload_from(message):
                return False  # A new system command keeps its normal routing.
            if not prompt_reply and (command != '/skills' or payload_from(message)):
                return False
        with self.db:
            if self._authorized(update):
                self.db.execute('INSERT OR IGNORE INTO skills_jobs(id,payload) VALUES(?,?)',
                                (update['update_id'], json.dumps(update, ensure_ascii=False)))
            self.db.execute("UPDATE kv SET value=? WHERE key='offset'",
                            (str(max(self.router.store.offset(), update['update_id'] + 1)),))
        return True

    def _target(self, binding):
        target = self.router.targets.get(binding.get('target_key'))
        bridge = self.router.bridges.get(binding.get('target_key'))
        if (not self.router.owner_allowed() or not target or target.get('topic_missing') or not bridge
                or self.binding(target) != binding or bridge.thread_id != binding.get('thread_id')
                or bridge.target.get('message_thread_id') != binding.get('topic_id')
                or bridge.target.get('topic_missing')):
            raise SkillsError('会话绑定已变化，请重新发送 /skills 选择会话。')
        return target

    def _catalog(self, catalog_id, origin):
        row = self.db.execute('SELECT * FROM skills_catalogs WHERE id=?', (catalog_id,)).fetchone()
        if (not row or row['chat_id'] != str(origin['chat']['id'])
                or row['topic_id'] != self._topic(origin) or row['bot_id'] != self.router.bot_id
                or row['expires_at'] <= time.time()):
            raise SkillsError('技能列表已过期，请重新发送 /skills。')
        self._target(json.loads(row['target_json']))
        return row

    async def _panel(self, update, text, rows=()):
        origin = self._origin(update)
        chat_id, topic_id = str(origin['chat']['id']), self._topic(origin)
        tokens, keyboard = [], []
        with self.db:
            for row in rows:
                rendered = []
                for label, action, data in row:
                    token = secrets.token_urlsafe(12)
                    self.db.execute('INSERT INTO skills_buttons '
                                    '(token,chat_id,topic_id,bot_id,action,data,expires_at) VALUES(?,?,?,?,?,?,?)',
                                    (token, chat_id, topic_id, self.router.bot_id, action,
                                     json.dumps(data, ensure_ascii=False), time.time() + self.TTL))
                    tokens.append(token)
                    rendered.append({'text': clean(label, 55), 'callback_data': 'sk:' + token})
                keyboard.append(rendered)
        if utf16_size(text) > 3800:
            text = next(plain_chunks(text, 3700)) + '\n\n（内容较长，已截断显示。）'
        params = {'chat_id': chat_id, 'text': text, 'reply_markup': {'inline_keyboard': keyboard}}
        editing = bool(update.get('callback_query'))
        if editing:
            params['message_id'] = origin['message_id']
        elif topic_id:
            params['message_thread_id'] = topic_id
        try:
            result = await self.router.telegram.call('editMessageText' if editing else 'sendMessage', **params)
        except TelegramError as error:
            if editing and error.code == 400 and 'message is not modified' in str(error).lower():
                result = origin
            else:
                raise
        message_id = result.get('message_id') if isinstance(result, dict) else origin.get('message_id') if editing and result is True else None
        if not message_id or (editing and message_id != origin['message_id']):
            raise ConnectionError('Skill menu acknowledgement unavailable')
        with self.db:
            self.db.execute("UPDATE skills_buttons SET state='expired' WHERE chat_id=? AND message_id=?",
                            (chat_id, message_id))
            for token in tokens:
                self.db.execute("UPDATE skills_buttons SET message_id=?,state='active' WHERE token=?",
                                (message_id, token))
        return result

    async def _notice(self, update, text):
        origin = self._origin(update)
        params = {'chat_id': str(origin['chat']['id']), 'text': text}
        if self._topic(origin):
            params['message_thread_id'] = self._topic(origin)
        return await self.router.telegram.call('sendMessage', **params)

    async def _fetch(self, binding, force=False):
        self._target(binding)
        metadata = await asyncio.wait_for(self.router.catalog_call('thread/read', {'threadId': binding['thread_id']}), self.TIMEOUT)
        thread = metadata.get('thread') if isinstance(metadata, dict) else None
        if not isinstance(thread, dict):
            raise SkillsError('会话目录信息不完整，请稍后重试。')
        cwd = thread.get('cwd')
        if thread.get('id') != binding['thread_id'] or not isinstance(cwd, str) or not Path(cwd).is_absolute():
            raise SkillsError('无法读取这个会话的工作目录，请稍后重试。')
        result = await asyncio.wait_for(self.router.catalog_call('skills/list', {'cwds': [cwd], 'forceReload': bool(force)}), self.TIMEOUT)
        self._target(binding)
        groups = result.get('data') if isinstance(result, dict) else None
        if not isinstance(groups, list):
            raise SkillsError('技能列表信息不完整，请稍后重试。')
        group = next((item for item in groups if isinstance(item, dict) and item.get('cwd') == cwd), None)
        if group is None:
            raise SkillsError('技能查询没有返回这个工作目录，请刷新重试。')
        if not isinstance(group.get('skills'), list):
            raise SkillsError('技能列表信息不完整，请稍后重试。')
        skills, seen, malformed = [], set(), False
        for item in group['skills']:
            if not isinstance(item, dict):
                malformed = True
                continue
            name, path = item.get('name'), item.get('path')
            if (not isinstance(name, str) or not name.strip() or not isinstance(path, str)
                    or not Path(path).is_absolute() or not isinstance(item.get('enabled'), bool)):
                malformed = True
                continue
            if (name, path) in seen:
                continue
            seen.add((name, path))
            skills.append({'name': name, 'path': path, 'description': str(item.get('description') or ''),
                           'enabled': item['enabled'], 'scope': str(item.get('scope') or ''),
                           'pluginId': item.get('pluginId')})
        return cwd, skills, bool(group.get('errors') or malformed)

    async def _selector(self, update, query='', page=0):
        targets = self.router.recent.ordered(t for t in self.router.targets.values() if not t.get('topic_missing'))
        pages = max(1, (len(targets) + self.PAGE - 1) // self.PAGE)
        page = max(0, min(int(page), pages - 1))
        rows = [[(target['name'], 'target', {'binding': self.binding(target), 'query': query})]
                for target in targets[page * self.PAGE:(page + 1) * self.PAGE]]
        nav = []
        if page:
            nav.append(('上一页', 'targets', {'query': query, 'page': page - 1}))
        if page + 1 < pages:
            nav.append(('下一页', 'targets', {'query': query, 'page': page + 1}))
        if nav:
            rows.append(nav)
        rows.append([('取消', 'cancel', {})])
        await self._panel(update, self.router.config.get('host_id', '本机') + ' · 技能\n请选择本机会话，再查看它可用的技能。'
                          + ('\n关键词：' + clean(query, 100) if query else '')
                          + f'\n第 {page + 1}/{pages} 页，共 {len(targets)} 个会话。', rows)

    async def _load(self, update, binding, query='', force=False):
        origin = self._origin(update)
        cwd, skills, warning = await self._fetch(binding, force)
        catalog_id = secrets.token_urlsafe(12)
        with self.db:
            self.db.execute('INSERT INTO skills_catalogs VALUES(?,?,?,?,?,?,?,?,?,?)',
                            (catalog_id, str(origin['chat']['id']), self._topic(origin), self.router.bot_id,
                             json.dumps(binding, sort_keys=True), cwd, json.dumps(skills, ensure_ascii=False),
                             query[:200], int(warning), time.time() + self.TTL))
        await self._list(update, catalog_id)

    def _filtered(self, catalog):
        query = catalog['query'].casefold()
        return [(index, item) for index, item in enumerate(json.loads(catalog['skills_json']))
                if not query or query in (item['name'] + ' ' + item['description']).casefold()]

    async def _list(self, update, catalog_id, page=0):
        catalog = self._catalog(catalog_id, self._origin(update))
        target = self._target(json.loads(catalog['target_json']))
        items = self._filtered(catalog)
        pages = max(1, (len(items) + self.PAGE - 1) // self.PAGE)
        page = max(0, min(int(page), pages - 1))
        text = [f"{target['name']} · 可用技能", '目录：' + catalog['cwd'],
                f'第 {page + 1}/{pages} 页，共 {len(items)} 项。']
        if catalog['query']:
            text.append('关键词：' + clean(catalog['query'], 100))
        rows = []
        for number, (index, item) in enumerate(items[page * self.PAGE:(page + 1) * self.PAGE], page * self.PAGE + 1):
            label = f"{number}. {item['name']}" + ('（已禁用）' if not item['enabled'] else '')
            text.append('\n' + label + '\n' + clean(item['description'], 150))
            rows.append([(label, 'detail', {'catalog': catalog_id, 'index': index, 'page': page})])
        if not items:
            text.append('\n没有匹配的技能。' if catalog['query'] else '\n这个工作目录没有返回技能。')
        if catalog['warning']:
            text.append('\n部分技能读取失败，列表可能不完整。')
        nav = []
        if page:
            nav.append(('上一页', 'list', {'catalog': catalog_id, 'page': page - 1}))
        if page + 1 < pages:
            nav.append(('下一页', 'list', {'catalog': catalog_id, 'page': page + 1}))
        if nav:
            rows.append(nav)
        rows.append([('刷新', 'refresh', {'catalog': catalog_id}), ('取消', 'cancel', {'catalog': catalog_id})])
        await self._panel(update, '\n'.join(text), rows)

    def _selection(self, data, origin):
        catalog = self._catalog(data['catalog'], origin)
        skills = json.loads(catalog['skills_json'])
        index = data.get('index')
        if not isinstance(index, int) or isinstance(index, bool) or not 0 <= index < len(skills):
            raise SkillsError('技能选择已失效，请重新打开 /skills。')
        return catalog, skills[index]

    async def _detail(self, update, data):
        catalog, skill = self._selection(data, self._origin(update))
        text = (skill['name'] + '\n\n' + clean(skill['description'], 1900)
                + '\n\n状态：' + ('已启用' if skill['enabled'] else '已禁用，不能使用')
                + '\n范围：' + clean(skill['scope'], 80) + '\n路径：' + skill['path'])
        rows = []
        if skill['enabled']:
            rows.append([('使用此技能', 'use', data)])
        rows.append([('返回列表', 'list', {'catalog': data['catalog'], 'page': data.get('page', 0)}),
                     ('取消', 'cancel', {'catalog': data['catalog']})])
        await self._panel(update, text, rows)

    async def _fresh(self, selection):
        cwd, skills, _ = await self._fetch(selection['binding'])
        if cwd != selection['skill']['cwd']:
            raise SkillsError('会话工作目录已变化，请重新发送 /skills 选择技能。')
        if not any(item['enabled'] and item['name'] == selection['skill']['name']
                   and item['path'] == selection['skill']['path'] for item in skills):
            raise SkillsError('这个技能已不可用或已禁用，请刷新 /skills 后重选。')

    async def _prompt(self, update, button, data):
        origin = self._origin(update)
        previous = self.db.execute('SELECT * FROM skills_prompts WHERE id=?', (button['token'],)).fetchone()
        if previous:
            text = ('请回复已经显示的技能输入提示。' if previous['state'] == 'active' else
                    '这个技能输入提示已经处理或未获确认，请重新发送 /skills。')
            return await self._notice(update, text)
        catalog, skill = self._selection(data, origin)
        if not skill['enabled']:
            raise SkillsError('这个技能已禁用，不能使用。')
        selection = {'binding': json.loads(catalog['target_json']),
                     'skill': {'name': skill['name'], 'path': skill['path'], 'cwd': catalog['cwd']}}
        await self._fresh(selection)
        target = self._target(selection['binding'])
        with self.db:
            self.db.execute('INSERT INTO skills_prompts VALUES(?,?,?,?,?,?,?,?,?,?)',
                            (button['token'], str(origin['chat']['id']), self._topic(origin), None,
                             self.router.bot_id, catalog['id'], json.dumps(selection, sort_keys=True),
                             'creating', None, time.time() + self.TTL))
            self.db.execute("UPDATE skills_buttons SET state='consumed' WHERE token=?", (button['token'],))
        params = {'chat_id': str(origin['chat']['id']),
                  'text': PROMPT_TITLE + clean(skill['name'], 150) + '\n会话：' + clean(target['name'], 90)
                          + '\n请输入任务要求，并使用 Telegram 的“回复”发送。'
                          '\n此提示只接收文字；附件请直接发到目标话题。\n/cancel 取消。',
                  'reply_markup': {'force_reply': True, 'selective': True, 'input_field_placeholder': '输入任务要求'}}
        if self._topic(origin):
            params['message_thread_id'] = self._topic(origin)
        try:
            result = await self.router.telegram.call('sendMessage', **params)
            if not isinstance(result, dict) or not result.get('message_id'):
                raise ConnectionError('Skill prompt acknowledgement unavailable')
        except Exception:
            with self.db:
                self.db.execute("UPDATE skills_prompts SET state='uncertain' WHERE id=?", (button['token'],))
            raise
        with self.db:
            self.db.execute("UPDATE skills_prompts SET state='active',message_id=? WHERE id=?",
                            (result['message_id'], button['token']))

    async def _reply(self, update):
        message = update['message']
        parent = message.get('reply_to_message') or {}
        prompt = self.db.execute('SELECT * FROM skills_prompts WHERE chat_id=? AND message_id=?',
                                 (str(message['chat']['id']), parent.get('message_id'))).fetchone()
        if (not prompt or prompt['topic_id'] != self._topic(message) or prompt['bot_id'] != self.router.bot_id
                or prompt['expires_at'] <= time.time() or prompt['state'] not in ('active', 'submitting', 'submitted')):
            raise SkillsError('这个技能输入提示已失效，请重新发送 /skills。')
        text = message.get('text', '')
        words = text.strip().split(maxsplit=1)
        command = words[0].split('@')[0].lower() if words else ''
        if command == '/cancel':
            if prompt['state'] != 'active':
                raise SkillsError('这条任务已经提交，请在目标话题用 /stop 停止。')
            with self.db:
                self.db.execute("UPDATE skills_prompts SET state='cancelled' WHERE id=?", (prompt['id'],))
            return await self._notice(update, '已取消使用技能。')
        if payload_from(message) or not text.strip():
            raise SkillsError('请用文字回复这个提示；提示仍可继续使用。附件请直接发到目标话题。')
        if prompt['state'] != 'active' and prompt['update_id'] != update['update_id']:
            raise SkillsError('这个提示已经提交过任务，请重新发送 /skills 开始下一条。')
        selection = json.loads(prompt['selection_json'])
        target = self._target(selection['binding'])
        bridge = self.router.bridges[target['key']]
        existing = bridge.store.db.execute('SELECT * FROM inbox WHERE id=?', (update['update_id'],)).fetchone()
        routed = copy.deepcopy(message)
        routed.pop('reply_to_message', None)
        routed.pop('external_reply', None)
        routed.pop('quote', None)
        routed['message_thread_id'] = target['message_thread_id']
        expected_skill = selection['skill']
        if existing:
            try:
                same = (existing['text'] == text and existing['message_thread_id'] == target['message_thread_id']
                        and json.loads(existing['skill_json'] or 'null') == expected_skill
                        and json.loads(existing['main_guard_json'] or 'null') == selection['binding'])
            except (KeyError, TypeError, ValueError):
                same = False
            if not same or existing['state'] in ('denied', 'cancelled', 'blocked', 'failed'):
                raise SkillsError('任务投递记录冲突，未覆盖旧任务，请核对目标会话。')
        else:
            await self._fresh(selection)
            with self.db:
                self.db.execute("UPDATE skills_prompts SET state='submitting',update_id=? WHERE id=?",
                                (update['update_id'], prompt['id']))
            self._target(selection['binding'])
            bridge.store.accept({'update_id': update['update_id']}, routed, reply_context={},
                                guard_binding=selection['binding'], skill=expected_skill)
        if prompt['topic_id'] is None:
            bridge.store.enqueue_at('main-guard-input:skills:' + str(update['update_id']), self.router.owner,
                                    '📩 你在 All 说：\n' + text, target['message_thread_id'])
        self.router.recent.record(target['thread_id'], 'skill-task:' + str(update['update_id']))
        with self.db:
            self.db.execute("UPDATE skills_prompts SET state='submitted',update_id=? WHERE id=?",
                            (update['update_id'], prompt['id']))
        await self._notice(update, '已提交到「' + clean(target['name'], 90) + '」。')

    async def _command(self, update):
        message = update['message']
        words = message.get('text', '').strip().split(maxsplit=1)
        query = words[1][:200] if len(words) > 1 else ''
        topic = self._topic(message)
        if topic is None:
            return await self._selector(update, query)
        target = self.router.by_topic.get(topic)
        if not target:
            raise SkillsError('此话题尚未绑定会话，请先发送 /sessions 绑定。')
        return await self._load(update, self.binding(target), query)

    async def _callback(self, update):
        callback, origin = update['callback_query'], self._origin(update)
        try:
            await self.router.telegram.call('answerCallbackQuery', callback_query_id=callback['id'])
        except (TelegramError, ConnectionError, asyncio.TimeoutError):
            pass
        token = callback.get('data', '')[3:]
        button = self.db.execute('SELECT * FROM skills_buttons WHERE token=?', (token,)).fetchone()
        if (not button or button['chat_id'] != str(origin['chat']['id'])
                or button['topic_id'] != self._topic(origin) or button['message_id'] != origin['message_id']
                or button['bot_id'] != self.router.bot_id or button['expires_at'] <= time.time()
                or button['state'] == 'expired' or (button['state'] != 'active' and button['action'] != 'use')):
            raise SkillsError('这个技能按钮已失效，请重新发送 /skills。')
        action, data = button['action'], json.loads(button['data'])
        if action == 'targets':
            return await self._selector(update, data.get('query', ''), data.get('page', 0))
        if action == 'target':
            return await self._load(update, data['binding'], data.get('query', ''))
        if action == 'list':
            return await self._list(update, data['catalog'], data.get('page', 0))
        if action == 'detail':
            return await self._detail(update, data)
        if action == 'refresh':
            catalog = self._catalog(data['catalog'], origin)
            return await self._load(update, json.loads(catalog['target_json']), catalog['query'], True)
        if action == 'use':
            return await self._prompt(update, button, data)
        if action == 'cancel':
            if data.get('catalog'):
                self._catalog(data['catalog'], origin)
                with self.db:
                    self.db.execute("UPDATE skills_prompts SET state='cancelled' WHERE catalog_id=? AND state='active'",
                                    (data['catalog'],))
            return await self._panel(update, '已关闭技能菜单。')
        raise SkillsError('这个技能操作已失效，请重新发送 /skills。')

    async def handle_one(self):
        job = self.db.execute("SELECT * FROM skills_jobs WHERE state='pending' ORDER BY id LIMIT 1").fetchone()
        if job is None:
            return False
        update = json.loads(job['payload'])
        with self.db:
            state = 'processing' if self._authorized(update) and self.router.owner_allowed() else 'denied'
            claimed = self.db.execute("UPDATE skills_jobs SET state=? WHERE id=? AND state='pending'", (state, job['id']))
        if not claimed.rowcount or state == 'denied':
            return True
        try:
            words = update.get('message', {}).get('text', '').strip().split(maxsplit=1)
            command = words[0].split('@')[0].lower() if words else ''
            if update.get('callback_query'):
                await self._callback(update)
            elif self._prompt_reply(update['message']) and command != '/skills':
                await self._reply(update)
            else:
                await self._command(update)
            with self.db:
                self.db.execute("UPDATE skills_jobs SET state='done' WHERE id=?", (job['id'],))
        except Exception as error:
            with self.db:
                self.db.execute("UPDATE skills_jobs SET state='failed',error=? WHERE id=?", (type(error).__name__, job['id']))
            detail = str(error) if isinstance(error, SkillsError) else '暂时无法完成技能操作，请稍后发送 /skills 重试。'
            try:
                await self._notice(update, detail)
            except (TelegramError, ConnectionError, OSError, asyncio.TimeoutError):
                pass
            if not isinstance(error, (SkillsError, RpcError, TelegramError, ConnectionError, OSError, asyncio.TimeoutError)):
                LOG.warning('Skill menu operation failed: %s', type(error).__name__)
        return True

    def recover(self):
        with self.db:
            self.db.execute("UPDATE skills_jobs SET state='pending' WHERE state='processing'")
            self.db.execute("UPDATE skills_prompts SET state='uncertain' WHERE state='creating'")

    async def loop(self):
        while True:
            if not await self.handle_one():
                await asyncio.sleep(0.2)
