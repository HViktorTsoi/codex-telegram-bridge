"""Explicit, durable new-session wizard; existing-session binding stays separate."""
import asyncio
import json
from pathlib import Path
import secrets
import time

from attachments import payload_from
from bridge import RpcError, TelegramError
from session_picker import BindingError, TopicCreationUnavailable, clean_name, directory_text
from session_scope import SessionScopeError


PROMPT_TITLE = '新建 Codex 会话 · '


class SessionCreator:
    def __init__(self, picker):
        self.picker, self.router, self.db = picker, picker.router, picker.db
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS ui_new_drafts(
                id TEXT PRIMARY KEY,chat_id TEXT,state TEXT,cwd TEXT,name TEXT,
                thread_id TEXT,panel_id INTEGER,expires_at REAL,updated_at REAL);
            CREATE TABLE IF NOT EXISTS ui_new_prompts(
                chat_id TEXT,message_id INTEGER,topic_id INTEGER,draft_id TEXT,
                kind TEXT,state TEXT,expires_at REAL,PRIMARY KEY(chat_id,message_id));
            CREATE TABLE IF NOT EXISTS ui_new_all(
                guard_id TEXT PRIMARY KEY,revision INTEGER NOT NULL,bot_id INTEGER NOT NULL,
                creator_id TEXT NOT NULL UNIQUE,binding_json TEXT);
        ''')

    def is_reply(self, update):
        message = update.get('message', {})
        parent = message.get('reply_to_message', {})
        if not parent or (self.router.bot_id and parent.get('from', {}).get('id') != self.router.bot_id):
            return False
        row = self.db.execute('SELECT 1 FROM ui_new_prompts WHERE chat_id=? AND message_id=?',
                              (str(message.get('chat', {}).get('id')), parent.get('message_id'))).fetchone()
        # Also catch an orphaned prompt if sendMessage succeeded but its response was lost.
        return bool(row or parent.get('text', '').startswith(PROMPT_TITLE))

    def draft(self, draft_id, chat_id, *, allow_expired=False):
        row = self.db.execute('SELECT * FROM ui_new_drafts WHERE id=? AND chat_id=?',
                              (draft_id, str(chat_id))).fetchone()
        if not row:
            raise BindingError('创建记录不存在，请重新打开 /sessions。')
        if (not allow_expired and row['expires_at'] <= time.time()
                and row['state'] in ('browse', 'naming', 'review')):
            raise BindingError('新建流程已过期，请重新打开 /sessions。')
        return row

    def save(self, draft_id, **values):
        allowed = {'state', 'cwd', 'name', 'thread_id', 'panel_id'}
        assert values and values.keys() <= allowed
        values.update(expires_at=time.time() + 900, updated_at=time.time())
        with self.db:
            self.db.execute('UPDATE ui_new_drafts SET ' + ','.join(k + '=?' for k in values) + ' WHERE id=?',
                            (*values.values(), draft_id))

    def expire_prompts(self, draft_id):
        with self.db:
            self.db.execute("UPDATE ui_new_prompts SET state='expired' WHERE draft_id=? AND state='active'",
                            (draft_id,))

    async def show(self, update, draft, text, rows):
        previous = draft['panel_id'] if not update.get('callback_query') else None
        if previous:
            # Retire the old actions before sending, including when the new
            # message's acknowledgement is lost. Never confirm a stale card.
            with self.db:
                self.db.execute("UPDATE ui_buttons SET state='expired' WHERE chat_id=? AND message_id=?",
                                (draft['chat_id'], previous))
        result = await self.picker.panel(update, text, rows)
        self.save(draft['id'], panel_id=result['message_id'])
        if previous and result['message_id'] != previous:
            try:
                await asyncio.wait_for(self.router.telegram.call('editMessageReplyMarkup',
                    chat_id=draft['chat_id'], message_id=previous,
                    reply_markup={'inline_keyboard': []}), 5)
            except (TelegramError, ConnectionError, OSError, asyncio.TimeoutError):
                pass  # The new card and saved input do not depend on cleanup.

    @staticmethod
    def resolve(value, base=None):
        try:
            path = Path(value).expanduser()
            if not path.is_absolute():
                path = Path(base or Path.home()) / path
            path = path.resolve(strict=True)
            if not path.is_dir():
                raise OSError()
        except (OSError, RuntimeError, TypeError, ValueError):
            raise BindingError('目录不存在或无法访问，请检查路径后重试。') from None
        return path

    async def start(self, update):
        origin = update.get('callback_query', {}).get('message') or update['message']
        draft_id = secrets.token_urlsafe(12)
        with self.db:
            self.db.execute('INSERT INTO ui_new_drafts VALUES(?,?,?,?,?,?,?,?,?)',
                            (draft_id, str(origin['chat']['id']), 'browse', str(Path.home().resolve()),
                             None, None, None, time.time() + 900, time.time()))
        await self.browse(update, self.draft(draft_id, origin['chat']['id']))

    async def browse(self, update, draft, path=None, page=0, hidden=False):
        if draft['state'] not in ('browse', 'naming', 'review'):
            return await self.resume(update, draft)
        path = self.resolve(path or draft['cwd'])

        def directories():
            try:
                return sorted((p.name for p in path.iterdir()
                               if (hidden or not p.name.startswith('.')) and p.is_dir()), key=str.casefold)
            except OSError:
                raise BindingError('无法列出这个目录，请返回上级或输入其他路径。') from None

        names = await asyncio.to_thread(directories)
        page_count = max(1, (len(names) + 9) // 10)
        page = max(0, min(int(page), page_count - 1))
        self.expire_prompts(draft['id'])
        self.save(draft['id'], state='browse', cwd=str(path))
        data = {'id': draft['id']}
        rows = [[('✅ 使用当前目录', 'new:use', data)]]
        rows.extend([(f'📁 {name}', 'new:browse', {**data, 'path': str(path / name), 'hidden': hidden})]
                    for name in names[page * 10:(page + 1) * 10])
        navigation = []
        if page:
            navigation.append(('上一页', 'new:browse', {**data, 'page': page - 1, 'hidden': hidden}))
        if page + 1 < page_count:
            navigation.append(('下一页', 'new:browse', {**data, 'page': page + 1, 'hidden': hidden}))
        if navigation:
            rows.append(navigation)
        if path.parent != path:
            rows.append([('⬆ 上级目录', 'new:browse', {**data, 'path': str(path.parent), 'hidden': hidden})])
        rows.extend([
            [('📂 新建文件夹', 'new:folder', data), ('✏️ 输入路径', 'new:path', data)],
            [('隐藏点号目录' if hidden else '显示点号目录', 'new:browse', {**data, 'hidden': not hidden}),
             ('取消', 'new:cancel', data)]])
        text = (f"{self.router.config.get('host_id', '本机')} · 新建 Codex 会话\n"
                f"📁 {directory_text({'cwd': str(path)}, 1200)}\n\n"
                f'选择已有文件夹，或直接使用当前目录。\n子目录第 {page + 1}/{page_count} 页，共 {len(names)} 个。')
        await self.show(update, draft, text, rows)

    async def prompt(self, update, draft, kind, error=None):
        labels = {'path': ('输入路径', '输入完整路径、~/路径，或相对于当前目录的路径。'),
                  'folder': ('新建文件夹', '输入当前目录下的新文件夹名称；支持中文和空格。'),
                  'name': ('会话名称', '输入新会话名称，最多 90 个字符。')}
        label, help_text = labels[kind]
        self.expire_prompts(draft['id'])
        self.save(draft['id'], state='naming' if kind == 'name' else 'browse')
        await self.show(update, draft, f"{label}\n📁 {directory_text(dict(draft), 1200)}\n\n请回复接下来的输入提示。",
                        [[('返回目录选择', 'new:browse', {'id': draft['id']}),
                          ('取消', 'new:cancel', {'id': draft['id']})]])
        text = PROMPT_TITLE + label + '\n' + help_text + '\n请使用 Telegram 的“回复”填写；/cancel 取消。'
        if error:
            text += '\n\n' + error
        result = await self.router.telegram.call('sendMessage', chat_id=draft['chat_id'], text=text,
                                                 reply_markup={'force_reply': True, 'selective': True,
                                                               'input_field_placeholder': label})
        with self.db:
            self.db.execute('INSERT INTO ui_new_prompts VALUES(?,?,?,?,?,?,?)',
                            (draft['chat_id'], result['message_id'], None, draft['id'], kind, 'active', time.time() + 900))

    async def reply(self, update):
        message = update['message']
        prompt = self.db.execute('SELECT * FROM ui_new_prompts WHERE chat_id=? AND message_id=?',
                                 (str(message['chat']['id']), message['reply_to_message']['message_id'])).fetchone()
        if (not prompt or prompt['state'] != 'active' or prompt['expires_at'] <= time.time()
                or prompt['topic_id'] != (message.get('message_thread_id') or None)):
            raise BindingError('这个输入提示已失效，请使用最新提示，或发送 /sessions 重新打开。')
        draft = self.draft(prompt['draft_id'], message['chat']['id'])
        with self.db:
            self.db.execute("UPDATE ui_new_prompts SET state='consumed' WHERE chat_id=? AND message_id=?",
                            (prompt['chat_id'], prompt['message_id']))
        text = message.get('text', '').strip()
        if text == '/cancel':
            return await self.cancel(update, draft)
        try:
            if not text or payload_from(message):
                raise BindingError('请用文字回复这个提示。')
            if draft['state'] not in ('browse', 'naming'):
                raise BindingError('当前步骤已完成，请使用最新按钮。')
            if prompt['kind'] == 'path':
                return await self.browse(update, draft, str(self.resolve(text, draft['cwd'])))
            if prompt['kind'] == 'folder':
                if text in ('.', '..') or any(c in text for c in '/\\') or any(ord(c) < 32 for c in text):
                    raise BindingError('请输入单个文件夹名，不要包含斜线、换行或 . / ..。')
                parent = self.resolve(draft['cwd'])
                folder = parent / text
                try:
                    folder.mkdir(mode=0o700, parents=False, exist_ok=False)
                except FileExistsError:
                    raise BindingError('同名文件或文件夹已存在；请换个名称，或返回目录选择已有文件夹。') from None
                except OSError:
                    raise BindingError('无法创建文件夹，请检查名称及目录写入权限。') from None
                return await self.browse(update, draft, str(folder))
            name = clean_name(text, 91)
            if len(name) > 90 or any(ord(c) < 32 for c in text):
                raise BindingError('请使用不超过 90 个字符的单行会话名称。')
            self.save(draft['id'], state='review', name=name)
            return await self.review(update, self.draft(draft['id'], draft['chat_id']))
        except BindingError as error:
            return await self.prompt(update, draft, prompt['kind'], str(error))

    async def review(self, update, draft, notice=None):
        await self.show(update, draft,
                        f"确认新建 Codex 会话？\n\n主机：{self.router.config.get('host_id', '本机')}\n"
                        f"名称：{draft['name']}\n目录：{directory_text(dict(draft), 1200)}\n\n"
                        '确认后新建空会话并连接新 Telegram 话题。模型和权限沿用本机默认设置；在话题发消息后才开始运行模型。'
                        + ('\n\n' + notice if notice else ''),
                        [[('确认新建并连接', 'new:confirm', {'id': draft['id']})],
                         [('修改名称', 'new:name', {'id': draft['id']}), ('修改目录', 'new:browse', {'id': draft['id']})],
                         [('取消', 'new:cancel', {'id': draft['id']})]])

    async def cancel(self, update, draft):
        if draft['thread_id'] or draft['state'] in ('creating', 'uncertain', 'blocked'):
            return await self.resume(update, draft)
        self.expire_prompts(draft['id'])
        self.save(draft['id'], state='cancelled')
        await self.show(update, draft, '已取消新建会话。此流程中已新建的文件夹会保留。',
                        [[('返回会话管理', 'home', {})]])

    def _check_access(self, draft, bot_id=None):
        if (str(self.router.owner) != draft['chat_id'] or not self.router.owner_allowed()
                or (bot_id is not None and self.router.bot_id != bot_id)):
            raise BindingError('会话创建的机器人或访问权限已变化，请恢复原设置后重试。')

    async def _name_new_thread(self, draft, bot_id=None):
        self._check_access(draft, bot_id)
        if not draft['thread_id']:
            if draft['state'] != 'review':
                raise BindingError('请先选择目录并填写会话名称。')
            cwd = str(self.resolve(draft['cwd']))
            if cwd != draft['cwd']:
                raise BindingError('目录位置已经改变，请重新选择目录后确认。')
            self.picker.check_topic_creation_available()
            if not self.router.bridges[self.router.root_target['key']].ready.is_set():
                raise BindingError('本机 Codex 正在连接，请稍后重试新建会话。')
            # Commit intent before the non-idempotent RPC. A lost response must never create twice.
            self.save(draft['id'], state='creating')
            try:
                result = await self.router.catalog_call('thread/start', {'cwd': cwd})
            except (RpcError, OSError, asyncio.TimeoutError, SessionScopeError):
                self.save(draft['id'], state='uncertain')
                raise BindingError('新建会话的返回结果未确认。请检查已有会话列表；不会自动重复创建。') from None
            thread = result.get('thread') if isinstance(result, dict) else None
            tid = thread.get('id') if isinstance(thread, dict) else None
            if not isinstance(tid, str) or not tid:
                self.save(draft['id'], state='uncertain')
                raise BindingError('新建会话的返回结果异常，请检查已有会话列表。')
            try:
                valid = (not any(t['thread_id'] == tid for t in self.router.targets.values())
                         and not thread.get('ephemeral') and thread.get('canAcceptDirectInput') is not False
                         and str(self.resolve(thread.get('cwd') or result.get('cwd') or cwd)) == cwd)
            except BindingError:
                valid = False
            if not valid:
                self.save(draft['id'], thread_id=tid, state='blocked')
                raise BindingError('新建会话信息与预期不符，已停止绑定；请检查已有会话列表。')
            # Keep the verified native ID before claiming the Bot scope. If the
            # process stops here, a later attempt can finish claiming this ID.
            self.save(draft['id'], thread_id=tid, state='received')
            draft = self.draft(draft['id'], draft['chat_id'])
        tid = draft['thread_id']
        self._check_access(draft, bot_id)
        if draft['state'] == 'received':
            try:
                self.router.session_scope.claim_created(tid)
            except SessionScopeError:
                self.save(draft['id'], state='blocked')
                raise BindingError('新会话已创建，但归属登记未完成，已停止命名和绑定；请核对会话后再处理。') from None
            self.save(draft['id'], state='created')
            draft = self.draft(draft['id'], draft['chat_id'])
        self.router.session_scope.require(tid)
        if draft['state'] == 'created':
            result = await self.router.catalog_call('thread/read', {'threadId': tid})
            thread = result.get('thread') if isinstance(result, dict) else None
            try:
                valid = (isinstance(thread, dict) and thread.get('id') == tid and thread.get('cwd')
                         and not thread.get('ephemeral') and thread.get('canAcceptDirectInput') is not False
                         and not any(t['thread_id'] == tid for t in self.router.targets.values())
                         and str(self.resolve(thread['cwd'])) == draft['cwd'])
            except BindingError:
                valid = False
            if not valid:
                self.save(draft['id'], state='blocked')
                raise BindingError('新会话信息无法核对，已停止命名和绑定，请检查已有会话列表。')
            self._check_access(draft, bot_id)
            await self.router.catalog_call('thread/name/set', {'threadId': tid, 'name': draft['name']})
            self.save(draft['id'], state='named')
            draft = self.draft(draft['id'], draft['chat_id'])
        self._check_access(draft, bot_id)
        return draft

    async def resume(self, update, draft, confirm=False):
        if draft['thread_id'] and draft['state'] != 'received':
            self.router.session_scope.require(draft['thread_id'])
        if draft['state'] == 'done':
            target = next((t for t in self.router.targets.values() if t['thread_id'] == draft['thread_id']), None)
            if target:
                return await self.picker.bound_panel(update, target)
            raise BindingError('新会话已经创建，请从“添加会话”重新选择绑定。')
        if draft['state'] in ('creating', 'uncertain', 'blocked'):
            return await self.show(update, draft,
                                   '上次新建会话的结果需要核对，不会自动再次创建。\n'
                                   '请到“添加会话”中检查最新会话，确认后绑定。' +
                                   (f"\n已返回 ID：{draft['thread_id']}" if draft['thread_id'] else ''),
                                   [[('检查已有会话', 'catalog', {}), ('返回', 'home', {})]])
        if not draft['thread_id'] and not confirm:
            if draft['state'] != 'review':
                raise BindingError('请先选择目录并填写会话名称。')
            return await self.review(update, draft)
        try:
            draft = await self._name_new_thread(draft)
        except TopicCreationUnavailable as error:
            return await self.review(update, draft, str(error) +
                                     '\n本次尚未新建 Codex 会话，名称和目录已保留，请稍后再确认。')
        tid = draft['thread_id']
        try:
            target = await self.picker.bind(tid)
        except TopicCreationUnavailable as error:
            draft = self.draft(draft['id'], draft['chat_id'])
            return await self.show(update, draft,
                                   'Codex 会话已创建并保留，暂未连接 Telegram。\n\n'
                                   f"名称：{draft['name']}\n目录：{directory_text(dict(draft), 1200)}\nID：{tid}\n\n"
                                   + str(error) + '\n稍后点击“重试连接”继续连接这个原会话，不会再次新建会话。',
                                   [[('重试连接', 'new:resume', {'id': draft['id']})],
                                    [('返回会话管理', 'home', {})]])
        self.save(draft['id'], state='done')
        await self.picker.bound_panel(update, target)

    def _all_request(self, guard_id):
        return self.db.execute('SELECT a.*,d.chat_id,d.thread_id,d.state FROM ui_new_all a '
                               'JOIN ui_new_drafts d ON d.id=a.creator_id WHERE a.guard_id=?',
                               (guard_id,)).fetchone()

    @staticmethod
    def _binding(target):
        return {key: target[key] for key in ('key', 'thread_id', 'message_thread_id')}

    def created_all_binding(self, draft_id, revision, chat_id):
        """Recover only an acknowledged, still-installed binding with no RPC."""
        request = self._all_request(draft_id)
        if (not request or request['revision'] != revision or request['chat_id'] != str(chat_id)
                or request['bot_id'] != self.router.bot_id or request['state'] not in ('named', 'done')
                or not self.router.owner_allowed() or not self.router.thread_allowed(request['thread_id'])):
            return None
        try:
            if request['binding_json']:
                binding = json.loads(request['binding_json'])
            else:
                operation = self.db.execute("SELECT target_json FROM binding_ops WHERE thread_id=? "
                                            "AND status IN ('topic_created','configured','active')",
                                            (request['thread_id'],)).fetchone()
                if not operation:
                    return None
                target = json.loads(operation['target_json'])
                if 'previous_message_thread_id' in target:
                    return None
                binding = self._binding(target)
            target = self.router.targets.get(binding['key'])
            if (binding['thread_id'] == request['thread_id'] and target and not target.get('topic_missing')
                    and self._binding(target) == binding):
                return binding
        except (KeyError, TypeError, ValueError):
            return None
        return None

    async def create_for_all(self, draft_id, revision, chat_id):
        """Provision one Home session for an authorized, frozen All message."""
        chat_id, bot_id = str(chat_id), self.router.bot_id
        if type(bot_id) is not int or bot_id <= 0:
            raise BindingError('当前 Bot 身份尚未确认，请稍后重试。')
        self._check_access({'chat_id': chat_id}, bot_id)
        request = self._all_request(draft_id)
        if not request:
            creator_id = secrets.token_urlsafe(12)
            cwd = str(self.resolve(Path.home()))
            now = time.time()
            name = '新会话 ' + time.strftime('%m-%d %H:%M', time.localtime(now)) + ' · ' + creator_id[-6:]
            with self.db:
                self.db.execute('INSERT INTO ui_new_drafts VALUES(?,?,?,?,?,?,?,?,?)',
                                (creator_id, chat_id, 'review', cwd, name, None, None, now + 900, now))
                self.db.execute('INSERT INTO ui_new_all(guard_id,revision,bot_id,creator_id) VALUES(?,?,?,?)',
                                (draft_id, revision, bot_id, creator_id))
            request = self._all_request(draft_id)
        if (request['revision'], request['chat_id'], request['bot_id']) != (revision, chat_id, bot_id):
            raise BindingError('原消息版本或 Bot 身份已变化，请从待发送查看原消息。')
        draft = self.draft(request['creator_id'], chat_id, allow_expired=True)
        if draft['state'] in ('creating', 'uncertain', 'blocked'):
            raise BindingError('上次新建会话的结果需要核对，不会再次创建。原消息保留在待发送。')
        binding = self.created_all_binding(draft_id, revision, chat_id)
        if binding is None:
            if draft['state'] == 'done' or any(t['thread_id'] == draft['thread_id'] for t in self.router.targets.values()):
                raise BindingError('新会话的话题绑定已变化，原消息保留在待发送。')
            draft = await self._name_new_thread(draft, bot_id)
            try:
                target = await self.picker.bind(draft['thread_id'])
            except TopicCreationUnavailable as error:
                raise BindingError(f"会话「{draft['name']}」已保留。{error}\n重试将连接原会话。") from None
            self._check_access(draft, bot_id)
            binding = self._binding(target)
            if binding['thread_id'] != draft['thread_id']:
                raise BindingError('新话题未连接预期的 Codex 会话，原消息保留在待发送。')
        with self.db:
            self.db.execute('UPDATE ui_new_all SET binding_json=? WHERE guard_id=?',
                            (json.dumps(binding, ensure_ascii=False), draft_id))
            self.db.execute("UPDATE ui_new_drafts SET state='done',updated_at=? WHERE id=?", (time.time(), draft['id']))
        return binding

    async def handle(self, update, action, data):
        if action == 'new:start':
            return await self.start(update)
        origin = update['callback_query']['message']
        draft = self.draft(data['id'], origin['chat']['id'])
        if action != 'new:resume' and origin['message_id'] != draft['panel_id']:
            raise BindingError('这个新建页面已更新，请使用最新卡片，或发送 /sessions 继续处理。')
        if action == 'new:cancel':
            return await self.cancel(update, draft)
        if draft['state'] == 'cancelled':
            raise BindingError('这个新建流程已取消，请重新打开 /sessions。')
        if action in ('new:resume', 'new:confirm') or draft['thread_id'] or draft['state'] in ('creating', 'uncertain', 'blocked'):
            return await self.resume(update, draft, confirm=action == 'new:confirm')
        if action == 'new:browse':
            return await self.browse(update, draft, data.get('path'), data.get('page', 0), data.get('hidden', False))
        if action in ('new:path', 'new:folder', 'new:name', 'new:use'):
            if action == 'new:use':
                self.resolve(draft['cwd'])
            return await self.prompt(update, draft, 'name' if action == 'new:use' else action.split(':')[1])
        raise BindingError('未知新建步骤，请重新打开 /sessions。')

    def unfinished(self, chat_id):
        rows = self.db.execute("SELECT * FROM ui_new_drafts d WHERE chat_id=? AND (state IN "
                               "('received','created','named','creating','uncertain','blocked') OR "
                               "(state='review' AND expires_at>?)) AND NOT EXISTS "
                               "(SELECT 1 FROM ui_new_all a WHERE a.creator_id=d.id) ORDER BY updated_at DESC LIMIT 5",
                               (str(chat_id), time.time())).fetchall()
        return [row for row in rows if not row['thread_id'] or row['state'] == 'received'
                or self.router.thread_allowed(row['thread_id'])]

    def recover(self):
        with self.db:
            self.db.execute("UPDATE ui_new_drafts SET state='uncertain' WHERE state='creating'")
