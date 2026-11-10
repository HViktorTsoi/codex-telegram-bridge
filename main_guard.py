"""Hold ordinary All-chat messages until an exact destination is selected."""
import copy
import json
import secrets
import time

from attachments import payload_from
from bridge import permitted
from message_format import plain_chunks, utf16_size
from reply_context import snapshot as reply_snapshot, serialized as reply_serialized, render as render_reply
from session_picker import BindingError, clean_name


class MainChatGuard:
    PAGE = 8
    TTL = 900
    STATES = ('held', 'review', 'expired')
    SERVICES = ('forum_topic_created', 'forum_topic_edited', 'forum_topic_closed',
                'forum_topic_reopened', 'general_forum_topic_hidden', 'general_forum_topic_unhidden',
                'pinned_message', 'message_auto_delete_timer_changed')

    def __init__(self, picker, clock=time.time):
        self.picker, self.router, self.db, self.clock = picker, picker.router, picker.db, clock
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS main_guard_drafts(
                id TEXT PRIMARY KEY,chat_id TEXT,album_id TEXT,state TEXT,revision INTEGER,
                selection_json TEXT,selected_revision INTEGER,panel_id INTEGER,last_at REAL,
                expires_at REAL,presented_revision INTEGER DEFAULT 0,error TEXT,confirmed_at REAL);
            CREATE TABLE IF NOT EXISTS main_guard_messages(
                update_id INTEGER PRIMARY KEY,draft_id TEXT,payload TEXT,reply_json TEXT,received_at REAL);
        ''')
        if 'confirmed_at' not in {row[1] for row in self.db.execute('PRAGMA table_info(main_guard_drafts)')}:
            self.db.execute('ALTER TABLE main_guard_drafts ADD COLUMN confirmed_at REAL')
            self.db.commit()

    def _save(self, draft_id, **values):
        allowed = {'state', 'revision', 'selection_json', 'selected_revision', 'panel_id', 'last_at',
                   'expires_at', 'presented_revision', 'error', 'confirmed_at'}
        assert values and values.keys() <= allowed
        with self.db:
            self.db.execute('UPDATE main_guard_drafts SET ' + ','.join(k + '=?' for k in values) + ' WHERE id=?',
                            (*values.values(), draft_id))

    def _draft(self, draft_id, chat_id=None):
        row = self.db.execute('SELECT * FROM main_guard_drafts WHERE id=?', (draft_id,)).fetchone()
        if not row or (chat_id is not None and row['chat_id'] != str(chat_id)):
            raise BindingError('待发送消息不存在，请从 /sessions 的“待发送”重新打开。')
        if row['state'] in ('held', 'review') and row['expires_at'] <= self.clock():
            self._save(draft_id, state='expired', selection_json=None, selected_revision=None)
            row = self.db.execute('SELECT * FROM main_guard_drafts WHERE id=?', (draft_id,)).fetchone()
        return row

    def _members(self, draft_id):
        return self.db.execute('SELECT * FROM main_guard_messages WHERE draft_id=? ORDER BY update_id',
                               (draft_id,)).fetchall()

    def is_message(self, update):
        return (not update.get('callback_query') and bool(self.db.execute(
            'SELECT 1 FROM main_guard_messages WHERE update_id=?', (update.get('update_id'),)).fetchone()))

    def ingest(self, update):
        if update.get('callback_query'):
            return False
        message = update.get('message', {})
        if message.get('message_thread_id') or any(key in message for key in self.SERVICES):
            return False
        text = message.get('text', '').lstrip()
        media = payload_from(message)
        if not media and (not text or text.startswith(('/', '@'))):
            return False
        # Explicit /to and inbox ForceReply prompts, including expired ones, retain
        # their own validation; they must never be caught as an unrelated draft.
        parent_id = message.get('reply_to_message', {}).get('message_id')
        if self.db.execute('SELECT 1 FROM cc_prompts WHERE message_id=? AND chat_id=?',
                           (parent_id, str(message.get('chat', {}).get('id')))).fetchone():
            return False
        if not permitted(message, self.router.access(), self.router.owner):
            return False
        now = self.clock()
        with self.db:
            old = self.db.execute('SELECT draft_id FROM main_guard_messages WHERE update_id=?',
                                  (update['update_id'],)).fetchone()
            if not old:
                chat_id = str(message['chat']['id'])
                album_id = str(message['media_group_id']) if message.get('media_group_id') else None
                draft = (self.db.execute("SELECT * FROM main_guard_drafts WHERE chat_id=? AND album_id=? "
                                         "AND state IN ('held','review','expired') ORDER BY last_at DESC LIMIT 1",
                                         (chat_id, album_id)).fetchone() if album_id else None)
                if draft:
                    draft_id = draft['id']
                    self.db.execute("UPDATE main_guard_drafts SET state='held',revision=revision+1,"
                                    'selection_json=NULL,selected_revision=NULL,last_at=?,expires_at=?,error=NULL WHERE id=?',
                                    (now, now + self.TTL, draft_id))
                else:
                    draft_id = secrets.token_urlsafe(12)
                    self.db.execute('INSERT INTO main_guard_drafts '
                                    '(id,chat_id,album_id,state,revision,last_at,expires_at) VALUES(?,?,?,?,?,?,?)',
                                    (draft_id, chat_id, album_id, 'held', 1, now, now + self.TTL))
                self.db.execute('INSERT INTO main_guard_messages VALUES(?,?,?,?,?)',
                                (update['update_id'], draft_id, json.dumps(update, ensure_ascii=False),
                                 json.dumps(reply_snapshot(message), ensure_ascii=False), now))
                self.db.execute('INSERT OR IGNORE INTO ui_jobs(id,payload) VALUES(?,?)',
                                (update['update_id'], json.dumps(update, ensure_ascii=False)))
            self.db.execute("UPDATE kv SET value=? WHERE key='offset'",
                            (str(max(self.router.store.offset(), update['update_id'] + 1)),))
        return True

    def ready(self, update):
        row = self.db.execute('SELECT d.album_id,d.last_at FROM main_guard_messages m '
                              'JOIN main_guard_drafts d ON d.id=m.draft_id WHERE m.update_id=?',
                              (update.get('update_id'),)).fetchone()
        return not row or not row['album_id'] or self.clock() - row['last_at'] >= 2

    def pending_count(self):
        return self.db.execute("SELECT COUNT(*) FROM main_guard_drafts WHERE chat_id=? "
                               "AND state IN ('held','review','expired')", (str(self.router.owner),)).fetchone()[0]

    def _preview(self, draft):
        texts, files = [], []
        labels = {'photo': '图片', 'document': '文件', 'voice': '语音', 'audio': '音频', 'video': '视频',
                  'video_note': '视频消息', 'animation': '动画', 'sticker': '贴纸',
                  'location': '位置', 'venue': '地点', 'contact': '联系人', 'poll': '投票'}
        members = self._members(draft['id'])
        for row in members:
            message = json.loads(row['payload'])['message']
            body = message.get('text') or message.get('caption') or ''
            if body:
                texts.append(body)
            for kind, label in labels.items():
                if message.get(kind):
                    name = message[kind].get('file_name') if isinstance(message[kind], dict) else None
                    files.append(label + ('：' + clean_name(name, 80) if name else ''))
        body = '\n\n'.join(texts)
        if len(body) > 1200:
            body = body[:1200] + '\n（预览已截断，原文完整保留。）'
        content = body or '（无文字正文）'
        if files:
            content += '\n附件：' + '、'.join(files[:12]) + ('…' if len(files) > 12 else '')
        if draft['album_id']:
            content = f'相册 · {len(members)} 条消息\n' + content
        if any(json.loads(row['reply_json']) for row in members):
            content += '\n包含原消息引用，选择会话后会一起传入。'
        return content

    async def _panel(self, update, text, rows, draft=None):
        if utf16_size(text) > 3800:
            text = next(plain_chunks(text, 3700)) + '\n\n（预览已截断，原文完整保留。）'
        if draft and draft['panel_id'] and not update.get('callback_query'):
            update = {'callback_query': {'message': {'message_id': draft['panel_id'],
                                                    'chat': {'id': int(draft['chat_id'])}}}}
        result = await self.picker.panel(update, text, rows)
        if draft:
            self._save(draft['id'], panel_id=result['message_id'], presented_revision=draft['revision'])
        return result

    @staticmethod
    def _binding(target):
        return {key: target[key] for key in ('key', 'thread_id', 'message_thread_id')}

    def _validate(self, selected):
        target = self.router.targets.get(selected.get('key'))
        bridge = self.router.bridges.get(selected.get('key'))
        if (not target or target.get('topic_missing') or self._binding(target) != selected
                or not bridge or bridge.thread_id != selected.get('thread_id')):
            raise BindingError('目标绑定已变化，消息已保留；请到 /sessions → 待发送，重新选择会话。')
        return target

    async def _selector(self, update, draft, page=0):
        if draft['state'] in ('queued', 'cancelled'):
            return await self._closed(update, draft)
        if draft['state'] == 'releasing':
            raise BindingError('这条消息正在投递，请稍后查看；不要重复选择。')
        targets = self.router.recent.ordered(t for t in self.router.targets.values() if not t.get('topic_missing'))
        pages = max(1, (len(targets) + self.PAGE - 1) // self.PAGE)
        page = max(0, min(int(page), pages - 1))
        base = {'id': draft['id'], 'revision': draft['revision'], 'send_on_select': True}
        rows = [[(clean_name(target['name'], 42), 'main:select', {**base, 'target': self._binding(target)})]
                for target in targets[page * self.PAGE:(page + 1) * self.PAGE]]
        navigation = []
        if page:
            navigation.append(('上一页', 'main:choose', {**base, 'page': page - 1}))
        if page + 1 < pages:
            navigation.append(('下一页', 'main:choose', {**base, 'page': page + 1}))
        if navigation:
            rows.append(navigation)
        rows.append([('取消', 'main:cancel', base), ('待发送', 'main:list', {})])
        text = ('All · 选择接收会话\n这条消息尚未发送给 Codex。\n\n' + self._preview(draft) +
                f"\n\n{self.router.config.get('host_id', '本机')} · 第 {page + 1}/{pages} 页\n"
                '按最近访问排序。点击本机会话即发送。')
        if not targets:
            text += '\n暂无可用的已绑定会话，请先通过 /sessions 添加或修复绑定。'
        return await self._panel(update, text, rows, draft)

    async def present(self, update):
        row = self.db.execute('SELECT draft_id FROM main_guard_messages WHERE update_id=?',
                              (update['update_id'],)).fetchone()
        if not row:
            return
        draft = self._draft(row['draft_id'])
        if draft['state'] in ('queued', 'cancelled', 'releasing'):
            return
        if draft['panel_id'] and draft['presented_revision'] == draft['revision']:
            return
        if draft['state'] == 'expired':
            return await self._panel(update, 'All · 待发送消息已过期\n原文和附件仍保留，请重新选择接收会话。\n\n' + self._preview(draft),
                                     [[('重新选择', 'main:choose', {'id': draft['id']})],
                                      [('取消', 'main:cancel', {'id': draft['id']})]], draft)
        return await self._selector(update, draft)

    async def listing(self, update, page=0):
        drafts = self.db.execute("SELECT id FROM main_guard_drafts WHERE chat_id=? "
                                 "AND state IN ('held','review','expired') ORDER BY last_at,id",
                                 (str(self.router.owner),)).fetchall()
        pages = max(1, (len(drafts) + self.PAGE - 1) // self.PAGE)
        page = max(0, min(int(page), pages - 1))
        text, rows = [f'All · 待发送\n共 {len(drafts)} 条，选择接收会话后发送。'], []
        for number, item in enumerate(drafts[page * self.PAGE:(page + 1) * self.PAGE], page * self.PAGE + 1):
            draft = self._draft(item['id'])
            preview = clean_name(self._preview(draft), 65)
            text.append(f'\n{number}. ' + preview + ('（已过期）' if draft['state'] == 'expired' else ''))
            rows.append([(f'{number}. ' + preview[:36], 'main:choose', {'id': draft['id']})])
        navigation = []
        if page:
            navigation.append(('上一页', 'main:list', {'page': page - 1}))
        if page + 1 < pages:
            navigation.append(('下一页', 'main:list', {'page': page + 1}))
        if navigation:
            rows.append(navigation)
        rows.append([('刷新待发送', 'main:list', {'page': page}), ('返回会话管理', 'home', {})])
        return await self._panel(update, '\n'.join(text), rows)

    async def _closed(self, update, draft):
        if draft['state'] == 'queued':
            selected = json.loads(draft['selection_json'])
            target = self.router.targets.get(selected['key'], {})
            text = '已加入「' + clean_name(target.get('name') or selected['key']) + '」的队列。\n原文与回复会出现在该会话的话题中。'
        else:
            text = '已取消，这条消息不会交给 Codex。'
        return await self._panel(update, text, [[('待发送', 'main:list', {}), ('返回会话管理', 'home', {})]], draft)

    def _release(self, draft):
        if not self.router.owner_allowed():
            raise BindingError('访问权限已变化，消息未继续投递；恢复权限后请重新选择。')
        if draft['state'] != 'releasing' or draft['selected_revision'] != draft['revision']:
            raise BindingError('相册内容或所选目标已变化，请重新选择接收会话。')
        selected = json.loads(draft['selection_json'])
        target = self._validate(selected)
        bridge = self.router.bridges[target['key']]
        binding = {'draft_id': draft['id'], 'target_key': target['key'],
                   'thread_id': target['thread_id'], 'topic_id': target['message_thread_id']}
        for row in self._members(draft['id']):
            self._validate(selected)
            if not self.router.owner_allowed():
                raise BindingError('访问权限已变化，消息未继续投递。')
            message = copy.deepcopy(json.loads(row['payload'])['message'])
            message['message_thread_id'] = target['message_thread_id']
            context = json.loads(row['reply_json'])
            existing = bridge.store.db.execute('SELECT text,message_json,media_group_id,message_thread_id,source_message_id,'
                                              'reply_json,main_guard_json,state FROM inbox WHERE id=?', (row['update_id'],)).fetchone()
            expected = (message.get('text') or message.get('caption', ''),
                        json.dumps(payload_from(message), ensure_ascii=False),
                        str(message['media_group_id']) if message.get('media_group_id') else None,
                        target['message_thread_id'], message.get('message_id'), reply_serialized({}, context))
            if existing:
                if (tuple(existing)[:6] != expected or json.loads(existing['main_guard_json'] or 'null') != binding
                        or existing['state'] in ('denied', 'cancelled', 'blocked', 'failed')):
                    raise BindingError('原投递标识已存在或已被拒绝，未覆盖旧任务；请核对后取消并重新发送。')
            else:
                bridge.store.accept({'update_id': row['update_id']}, message, reply_context=context, guard_binding=binding)
            # Mirror the user's text into the selected Telegram topic,
            # including on recovery after input import but before outbox commit.
            # This distinct key retains literal source text for quote rendering
            # and never becomes model input.
            body = message.get('text') or message.get('caption', '')
            if body:
                text = '📩 你在 All 说：\n' + body
                if context:
                    text += '\n\n' + render_reply(context)
                bridge.store.enqueue_at(f"main-guard-input:{draft['id']}:{row['update_id']}",
                                        draft['chat_id'], text, selected['message_thread_id'])
        self.router.recent.record(target['thread_id'], 'main-guard:' + draft['id'],
                                  accessed_at=draft['confirmed_at'] if draft['confirmed_at'] is not None else draft['last_at'])
        self._save(draft['id'], state='queued', error=None)

    async def handle(self, update, action, data):
        if not self.router.owner_allowed():
            raise BindingError('当前账号没有访问权限。')
        if action == 'main:list':
            return await self.listing(update, data.get('page', 0))
        origin = update.get('callback_query', {}).get('message') or update['message']
        draft = self._draft(data['id'], origin['chat']['id'])
        if draft['state'] in ('queued', 'cancelled'):
            return await self._closed(update, draft)
        if draft['state'] == 'releasing':
            raise BindingError('这条消息正在投递，不能更换目标或取消；请稍后查看所选话题。')
        if action == 'main:cancel':
            self._save(draft['id'], state='cancelled', selection_json=None, selected_revision=None)
            return await self._closed(update, self._draft(draft['id']))
        if action == 'main:choose':
            self._save(draft['id'], state='held', selection_json=None, selected_revision=None,
                       expires_at=self.clock() + self.TTL, error=None)
            return await self._selector(update, self._draft(draft['id']), data.get('page', 0))
        if draft['state'] == 'expired':
            raise BindingError('这条待发送消息已过期，原文仍保留；请从 /sessions → 待发送重新选择。')
        if data.get('revision') != draft['revision']:
            raise BindingError('相册内容已变化，消息未发送；请从 /sessions → 待发送重新选择。')
        if action == 'main:select':
            selected = data['target']
            self._validate(selected)
            if data.get('send_on_select') is not True:
                # A selection made on an old two-step panel was not a send
                # action. Refresh it instead of releasing it after an upgrade.
                self._save(draft['id'], state='held', selection_json=None, selected_revision=None,
                           expires_at=self.clock() + self.TTL, error=None)
                return await self._selector(update, self._draft(draft['id']))
        elif action == 'main:confirm':
            # Buttons already displayed by older versions can still complete
            # their frozen selection; opening or recovering one never sends it.
            if (draft['state'] != 'review' or draft['selected_revision'] != draft['revision']
                    or json.loads(draft['selection_json'] or 'null') != data.get('target')):
                raise BindingError('所选目标已变化，请重新选择接收会话。')
            selected = data['target']
            self._validate(selected)
        else:
            raise BindingError('未知操作，请重新打开待发送列表。')
        if draft['album_id'] and self.clock() - draft['last_at'] < 2:
            raise BindingError('相册仍在接收，消息未发送；请稍后重新选择接收会话。')
        # The click itself is the send action. Persist the complete intent
        # before importing any input so crash recovery keeps this exact route.
        self._save(draft['id'], state='releasing',
                   selection_json=json.dumps(selected, ensure_ascii=False, sort_keys=True),
                   selected_revision=draft['revision'], expires_at=self.clock() + self.TTL,
                   confirmed_at=self.clock())
        try:
            self._release(self._draft(draft['id']))
        except BindingError as error:
            self._save(draft['id'], state='held', selection_json=None, selected_revision=None, error=str(error))
            raise
        return await self._closed(update, self._draft(draft['id']))

    def recover(self):
        for row in self.db.execute("SELECT id FROM main_guard_drafts WHERE state='releasing'").fetchall():
            draft = self._draft(row['id'])
            try:
                self._release(draft)
            except BindingError as error:
                self._save(draft['id'], state='expired', selection_json=None, selected_revision=None, error=str(error))
