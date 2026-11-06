"""Explicit, durable new-session wizard; existing-session binding stays separate."""
import asyncio
from pathlib import Path
import secrets
import time

from attachments import payload_from
from bridge import RpcError
from session_picker import BindingError, clean_name, directory_text


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

    def draft(self, draft_id, chat_id):
        row = self.db.execute('SELECT * FROM ui_new_drafts WHERE id=? AND chat_id=?',
                              (draft_id, str(chat_id))).fetchone()
        if not row:
            raise BindingError('创建记录不存在，请重新打开 /sessions。')
        if row['expires_at'] <= time.time() and row['state'] in ('browse', 'naming', 'review'):
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
        # Text replies update the same management panel, rather than the user's message.
        if draft['panel_id'] and not update.get('callback_query'):
            update = {'callback_query': {'message': {
                'message_id': draft['panel_id'], 'chat': {'id': int(draft['chat_id'])}}}}
        result = await self.picker.panel(update, text, rows)
        self.save(draft['id'], panel_id=result['message_id'])

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

    async def review(self, update, draft):
        await self.show(update, draft,
                        f"确认新建 Codex 会话？\n\n主机：{self.router.config.get('host_id', '本机')}\n"
                        f"名称：{draft['name']}\n目录：{directory_text(dict(draft), 1200)}\n\n"
                        '确认后新建空会话并连接新 Telegram 话题。模型和权限沿用本机默认设置；在话题发消息后才开始运行模型。',
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

    async def resume(self, update, draft, confirm=False):
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
        if not draft['thread_id']:
            if draft['state'] != 'review':
                raise BindingError('请先选择目录并填写会话名称。')
            if not confirm:
                return await self.review(update, draft)
            cwd = str(self.resolve(draft['cwd']))
            if cwd != draft['cwd']:
                raise BindingError('目录位置已经改变，请重新选择目录后确认。')
            # Commit intent before the non-idempotent RPC. A lost response must never create twice.
            self.save(draft['id'], state='creating')
            try:
                result = await self.router.catalog_call('thread/start', {'cwd': cwd})
            except (RpcError, ConnectionError, asyncio.TimeoutError):
                self.save(draft['id'], state='uncertain')
                raise BindingError('新建会话的返回结果未确认。请检查已有会话列表；不会自动重复创建。') from None
            thread = result.get('thread') if isinstance(result, dict) else None
            tid = thread.get('id') if isinstance(thread, dict) else None
            if not isinstance(tid, str) or not tid:
                self.save(draft['id'], state='uncertain')
                raise BindingError('新建会话的返回结果异常，请检查已有会话列表。')
            self.save(draft['id'], thread_id=tid, state='created')
            if (any(t['thread_id'] == tid for t in self.router.targets.values())
                    or thread.get('ephemeral') or thread.get('canAcceptDirectInput') is False
                    or str(self.resolve(thread.get('cwd') or result.get('cwd') or cwd)) != cwd):
                self.save(draft['id'], state='blocked')
                raise BindingError('新建会话信息与预期不符，已停止绑定；请检查已有会话列表。')
            draft = self.draft(draft['id'], draft['chat_id'])
        # The ID is durable before naming or binding; retries operate on this same session.
        tid = draft['thread_id']
        if draft['state'] == 'created':
            result = await self.router.catalog_call('thread/read', {'threadId': tid})
            thread = result.get('thread', {})
            try:
                valid = (thread.get('id') == tid and thread.get('cwd')
                         and not thread.get('ephemeral') and thread.get('canAcceptDirectInput') is not False
                         and not any(t['thread_id'] == tid for t in self.router.targets.values())
                         and str(self.resolve(thread['cwd'])) == draft['cwd'])
            except BindingError:
                valid = False
            if not valid:
                self.save(draft['id'], state='blocked')
                raise BindingError('新会话信息无法核对，已停止命名和绑定，请检查已有会话列表。')
            await self.router.catalog_call('thread/name/set', {'threadId': tid, 'name': draft['name']})
            self.save(draft['id'], state='named')
        target = await self.picker.bind(tid)
        self.save(draft['id'], state='done')
        await self.picker.bound_panel(update, target)

    async def handle(self, update, action, data):
        if action == 'new:start':
            return await self.start(update)
        origin = update['callback_query']['message']
        draft = self.draft(data['id'], origin['chat']['id'])
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
        return self.db.execute("SELECT * FROM ui_new_drafts WHERE chat_id=? AND state IN "
                               "('created','named','creating','uncertain','blocked') ORDER BY updated_at DESC LIMIT 5",
                               (str(chat_id),)).fetchall()

    def recover(self):
        with self.db:
            self.db.execute("UPDATE ui_new_drafts SET state='uncertain' WHERE state='creating'")
