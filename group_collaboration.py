"""A bounded, owner-started group smoke test for two existing local sessions."""
import asyncio
import hashlib
import json
import logging
import time

from attachments import payload_from
from bridge import TelegramError


LOG = logging.getLogger('codex_telegram_group_collaboration')
ACTIVE = ('cad_wait', 'cad_queued', 'manager_wait', 'manager_queued')
COMMANDS = ('/collab_test', '/collab_status', '/collab_stop')


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def input_id(run_id, stage):
    return -int(hashlib.sha256(('group-collab:' + run_id + ':' + stage).encode()).hexdigest()[:15], 16) - 1


class GroupCollaboration:
    SETTINGS = 'group_collaboration_settings_v1'
    TIMEOUT = 600

    def __init__(self, router, clock=time.time):
        self.router, self.db, self.clock = router, router.store.db, clock
        self.advance_lock, self.send_lock, self.job_lock = asyncio.Lock(), asyncio.Lock(), asyncio.Lock()
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS gc_jobs(id INTEGER PRIMARY KEY,payload TEXT,state TEXT DEFAULT 'pending',error TEXT);
            CREATE TABLE IF NOT EXISTS gc_runs(
                id TEXT PRIMARY KEY,bot_id INTEGER,owner TEXT,chat_id TEXT,topic_id INTEGER,
                participants TEXT,state TEXT,cad_reply TEXT,manager_reply TEXT,check_after REAL DEFAULT 0,
                created REAL,expires REAL,detail TEXT);
            CREATE TABLE IF NOT EXISTS gc_outbox(
                id TEXT PRIMARY KEY,run_id TEXT,bot_id INTEGER,owner TEXT,chat_id TEXT,topic_id INTEGER,
                text TEXT,state TEXT DEFAULT 'pending',retry_at REAL DEFAULT 0,message_id INTEGER,error TEXT);
        ''')

    def settings(self):
        row = self.db.execute('SELECT value FROM kv WHERE key=?', (self.SETTINGS,)).fetchone()
        try:
            value = json.loads(row[0]) if row else {}
            return value if isinstance(value, dict) else {}
        except (TypeError, ValueError):
            return {}

    def _identity(self):
        bot = getattr(self.router, 'bot_id', None)
        if type(bot) is not int or bot <= 0 or not self.router.owner_allowed():
            return None
        expected = self.router.config.get('expected_bot_username', '').lstrip('@').casefold()
        if expected and str(getattr(self.router, 'username', '')).lstrip('@').casefold() != expected:
            return None
        return bot, str(self.router.owner)

    def _participants(self):
        values = self.settings().get('participants')
        if (self.settings().get('enabled') is not True or not isinstance(values, list) or len(values) != 2
                or any(not isinstance(v, dict) for v in values)
                or len({v.get('key') for v in values}) != 2
                or [v.get('role') for v in values] != ['CAD', 'Telegram Manager']):
            return None
        result = []
        for value in values:
            target = self.router.targets.get(value.get('key'))
            bridge = self.router.bridges.get(value.get('key'))
            if (not target or not bridge or target.get('topic_missing') or bridge.target.get('topic_missing')
                    or target.get('thread_id') != value.get('thread_id') or bridge.thread_id != value.get('thread_id')
                    or target.get('message_thread_id') != value.get('topic_id')
                    or bridge.target.get('message_thread_id') != value.get('topic_id')):
                return None
            result.append({key: value[key] for key in ('key', 'thread_id', 'topic_id', 'role')})
        return result

    def _authorized(self, message):
        identity = self._identity()
        sender, chat = message.get('from', {}), message.get('chat', {})
        return bool(identity and str(sender.get('id')) == identity[1] and not sender.get('is_bot')
                    and chat.get('type') in ('private', 'group', 'supergroup')
                    and (chat.get('type') != 'private' or str(chat.get('id')) == identity[1]))

    def ingest(self, update):
        message = update.get('message') or {}
        text = message.get('text')
        if not isinstance(text, str) or payload_from(message) or 'forward_origin' in message:
            return False
        words = text.strip().split()
        if len(words) != 1:
            return False
        command, _, username = words[0].partition('@')
        if command.lower() not in COMMANDS or (username and username.casefold() !=
                str(getattr(self.router, 'username', '')).casefold()):
            return False
        with self.db:
            if self._authorized(message):
                self.db.execute('INSERT OR IGNORE INTO gc_jobs(id,payload) VALUES(?,?)',
                                (update['update_id'], encoded(update)))
            self.db.execute("UPDATE kv SET value=? WHERE key='offset'",
                            (str(max(self.router.store.offset(), update['update_id'] + 1)),))
        return True

    def _notice(self, key, message, text, run_id=None):
        bot, owner = self._identity()
        self.db.execute('INSERT OR IGNORE INTO gc_outbox(id,run_id,bot_id,owner,chat_id,topic_id,text) '
                        'VALUES(?,?,?,?,?,?,?)', (key, run_id, bot, owner, str(message['chat']['id']),
                        message.get('message_thread_id') or None, text))

    def _run_notice(self, run, suffix, text):
        self._notice(run['id'] + ':' + suffix, {'chat': {'id': run['chat_id']},
                     'message_thread_id': run['topic_id']}, text, run['id'])

    async def handle_one(self):
        async with self.job_lock:
            row = self.db.execute("SELECT * FROM gc_jobs WHERE state='pending' ORDER BY id LIMIT 1").fetchone()
            if row is None:
                return False
            update = json.loads(row['payload'])
            message = update['message']
            with self.db:
                self.db.execute("UPDATE gc_jobs SET state='processing' WHERE id=?", (row['id'],))
                if not self._authorized(message):
                    self.db.execute("UPDATE gc_jobs SET state='denied' WHERE id=?", (row['id'],))
                    return True
                key = 'gc-job:' + str(row['id'])
                command = message['text'].strip().split('@')[0].lower()
                if message['chat']['type'] == 'private':
                    self._notice(key, message, '请在你创建并加入本 Bot 的测试群中发送 /collab_test@' +
                                 str(self.router.username) + '。本轮仅测试 CAD → Telegram Manager 两句确认。')
                elif self._participants() is None:
                    self._notice(key, message, '本机协作测试尚未启用，或参与会话绑定已变化。')
                else:
                    run = self.db.execute('SELECT * FROM gc_runs WHERE chat_id=? AND topic_id IS ? '
                                          'ORDER BY created DESC LIMIT 1',
                                          (str(message['chat']['id']), message.get('message_thread_id') or None)).fetchone()
                    if command == '/collab_test':
                        busy = self.db.execute("SELECT * FROM gc_runs WHERE state IN "
                                               "('cad_wait','cad_queued','manager_wait','manager_queued') LIMIT 1").fetchone()
                        if busy:
                            self._notice(key, message, '已有一轮协作测试正在进行。用 /collab_status 查看；'
                                         '/collab_stop 停止继续接力。')
                        else:
                            rid = hashlib.sha256(encoded([self.router.bot_id, self.router.owner,
                                                  message['chat']['id'], row['id']]).encode()).hexdigest()
                            self.db.execute('INSERT OR IGNORE INTO gc_runs '
                                '(id,bot_id,owner,chat_id,topic_id,participants,state,created,expires) VALUES(?,?,?,?,?,?,?,?,?)',
                                (rid, self.router.bot_id, str(self.router.owner), str(message['chat']['id']),
                                 message.get('message_thread_id') or None, encoded(self._participants()),
                                 'cad_wait', self.clock(), self.clock() + self.TIMEOUT))
                            run = self.db.execute('SELECT * FROM gc_runs WHERE id=?', (rid,)).fetchone()
                            self._run_notice(run, 'start', '协作测试开始：CAD → Telegram Manager。\n'
                                             '只交换简短确认；正在工作的会话会等空闲后接续，不中断原任务。')
                    elif run is None:
                        self._notice(key, message, '本话题还没有协作测试。发送 /collab_test 开始。')
                    elif command == '/collab_stop':
                        if run['state'] in ACTIVE:
                            self.db.execute("UPDATE gc_runs SET state='stopped',detail='用户停止接力' WHERE id=?", (run['id'],))
                            self._cancel_pending(run)
                        self._notice(key, message, '本轮接力已停止；已开始的会话任务不会被强行打断。')
                    else:
                        labels = {'cad_wait': '等待 CAD 空闲', 'cad_queued': '等待 CAD 确认',
                                  'manager_wait': 'CAD 已确认，等待 Telegram Manager 空闲',
                                  'manager_queued': '等待 Telegram Manager 确认', 'done': '测试完成',
                                  'stopped': '已停止', 'timeout': '已超时，未继续接力', 'failed': '未完成，请检查会话状态'}
                        unknown = self.db.execute("SELECT 1 FROM gc_outbox WHERE run_id=? AND state='uncertain'",
                                                  (run['id'],)).fetchone()
                        self._notice(key, message, labels.get(run['state'], run['state']) +
                                     ('\n部分群消息的发送结果未确认，未自动重发。' if unknown else ''))
                self.db.execute("UPDATE gc_jobs SET state='done' WHERE id=?", (row['id'],))
            return True

    def _valid(self, run):
        return (self._identity() == (run['bot_id'], run['owner'])
                and self._participants() == json.loads(run['participants']))

    @staticmethod
    def expected(run, stage):
        role = 'CAD' if stage == 'cad' else 'Telegram Manager'
        return role + ' 协作测试确认 ' + run['id'][:12]

    def _prompt(self, run, stage):
        lead = ('本条是用户授权的 CAD 与 Telegram Manager 群聊互通测试。仅做消息确认，'
                '无需读取文件、使用工具或修改项目；不要继续任何额外任务。\n')
        if stage == 'manager':
            lead += 'CAD 已确认收到测试。其本次唯一回包：' + run['cad_reply'] + '\n'
        return lead + '请仅原样回复下面这一行，然后结束：\n' + self.expected(run, stage)

    async def _inject(self, run, stage):
        participant = json.loads(run['participants'])[0 if stage == 'cad' else 1]
        bridge = self.router.bridges[participant['key']]
        iid = input_id(run['id'], stage)
        existing = bridge.store.db.execute('SELECT * FROM inbox WHERE id=?', (iid,)).fetchone()
        if existing is not None:
            return
        if not bridge.ready.is_set() or bridge.store.db.execute(
                "SELECT 1 FROM inbox WHERE state IN ('pending','dispatching','running') LIMIT 1").fetchone():
            return
        if getattr(getattr(bridge.codex, 'turn_state', None), 'active', {}):
            return
        try:
            await asyncio.wait_for(bridge.dispatch_lock.acquire(), 1)
        except asyncio.TimeoutError:
            return
        try:
            if not self._valid(run) or not bridge.ready.is_set():
                return
            epoch = getattr(bridge.codex, 'user_input_epoch', None)
            turns = await asyncio.wait_for(bridge.codex.turns(limit=1), 3)
            if any(turn.get('status') == 'inProgress' for turn in turns):
                return
            if (not self._valid(run) or not bridge.ready.is_set() or self.clock() >= run['expires']
                    or epoch != getattr(bridge.codex, 'user_input_epoch', None)
                    or bridge.store.db.execute("SELECT 1 FROM inbox WHERE state IN ('pending','dispatching','running') LIMIT 1").fetchone()
                    or getattr(getattr(bridge.codex, 'turn_state', None), 'active', {})):
                return
            current = self.db.execute('SELECT state FROM gc_runs WHERE id=?', (run['id'],)).fetchone()
            if not current or current['state'] not in (stage + '_wait', stage + '_queued'):
                return
            text = self._prompt(run, stage)
            # The fixed inbox ID is an idempotent enqueue intent, not a retry of
            # a native RPC. Once inbox exists its normal reconciliation owns it.
            with self.db:
                self.db.execute('UPDATE gc_runs SET state=? WHERE id=?', (stage + '_queued', run['id']))
            message = {'from': {'id': int(self.router.owner)},
                       'chat': {'id': int(self.router.owner), 'type': 'private'},
                       'message_thread_id': participant['topic_id'], 'text': text}
            binding = {'target_key': participant['key'], 'thread_id': participant['thread_id'],
                       'topic_id': participant['topic_id']}
            bridge.store.accept({'update_id': iid}, message, guard_binding=binding)
        finally:
            bridge.dispatch_lock.release()

    def on_item(self, bridge, turn_id, item, replay=False):
        if (item.get('type') != 'agentMessage' or not isinstance(item.get('text'), str)
                or item.get('phase') not in (None, 'final_answer')):
            return
        for run in self.db.execute("SELECT * FROM gc_runs WHERE state IN ('cad_queued','manager_queued')").fetchall():
            if not self._valid(run):
                continue
            stage = 'cad' if run['state'] == 'cad_queued' else 'manager'
            participant = json.loads(run['participants'])[0 if stage == 'cad' else 1]
            if bridge is not self.router.bridges.get(participant['key']):
                continue
            row = bridge.store.db.execute('SELECT turn_id,client_id FROM inbox WHERE id=?',
                                           (input_id(run['id'], stage),)).fetchone()
            if (not row or row['turn_id'] != turn_id
                    or row['client_id'] != 'telegram-' + str(input_id(run['id'], stage))):
                continue
            expected = self.expected(run, stage)
            if not any(line.strip().strip('`> ').strip() == expected for line in item['text'].splitlines()):
                continue
            with self.db:
                self.db.execute('UPDATE gc_runs SET ' + stage + '_reply=? WHERE id=? AND state=?',
                                (expected, run['id'], run['state']))

    def _cancel_pending(self, run):
        self.db.execute("UPDATE gc_outbox SET state='cancelled' WHERE run_id=? AND state='pending'", (run['id'],))
        for stage, participant in zip(('cad', 'manager'), json.loads(run['participants'])):
            bridge = self.router.bridges.get(participant['key'])
            if bridge and bridge.thread_id == participant['thread_id']:
                iid = input_id(run['id'], stage)
                with bridge.store.db:
                    bridge.store.db.execute("UPDATE inbox SET state='cancelled' WHERE id=? AND state='pending' AND client_id=?",
                                            (iid, 'telegram-' + str(iid)))

    async def _advance(self):
        async with self.advance_lock:
            for run in self.db.execute("SELECT * FROM gc_runs WHERE state IN ('cad_wait','cad_queued','manager_wait','manager_queued')").fetchall():
                if not self._valid(run) or run['expires'] <= self.clock():
                    with self.db:
                        state = 'timeout' if run['expires'] <= self.clock() else 'failed'
                        self.db.execute('UPDATE gc_runs SET state=? WHERE id=?', (state, run['id']))
                        self._cancel_pending(run)
                        if self._identity() == (run['bot_id'], run['owner']):
                            self._run_notice(run, state, '本轮协作测试已结束，未继续接力。请用 /collab_status 查看。')
                    continue
                start = self.db.execute('SELECT state FROM gc_outbox WHERE id=?', (run['id'] + ':start',)).fetchone()
                if not start or start['state'] in ('pending', 'sending'):
                    continue
                if start['state'] != 'sent':
                    with self.db:
                        self.db.execute("UPDATE gc_runs SET state='failed',detail='测试群发送未确认，未投递会话' WHERE id=?",
                                        (run['id'],))
                    continue
                if run['state'] == 'cad_queued' and run['cad_reply']:
                    with self.db:
                        self.db.execute("UPDATE gc_runs SET state='manager_wait',check_after=0 WHERE id=?", (run['id'],))
                        self._run_notice(run, 'cad', 'CAD：已收到测试消息。\n等待 Telegram Manager 空闲后接续确认。')
                    continue
                if run['state'] == 'manager_queued' and run['manager_reply']:
                    with self.db:
                        self.db.execute("UPDATE gc_runs SET state='done' WHERE id=?", (run['id'],))
                        self._run_notice(run, 'done', 'Telegram Manager：已收到 CAD 的确认。\n本轮互通测试完成，已停止接力。')
                    continue
                stage = 'cad' if run['state'].startswith('cad') else 'manager'
                participant = json.loads(run['participants'])[0 if stage == 'cad' else 1]
                bridge = self.router.bridges[participant['key']]
                row = bridge.store.db.execute('SELECT state FROM inbox WHERE id=?',
                                               (input_id(run['id'], stage),)).fetchone()
                if row and row['state'] in ('failed', 'denied', 'cancelled', 'uncertain'):
                    with self.db:
                        self.db.execute("UPDATE gc_runs SET state='failed',detail=? WHERE id=?",
                                        ('原会话输入状态：' + row['state'], run['id']))
                        self._run_notice(run, 'failed', '会话未确认接收本次测试，已停止接力；不会自动重复投递。')
                elif self.clock() >= run['check_after']:
                    with self.db:
                        self.db.execute('UPDATE gc_runs SET check_after=? WHERE id=?',
                                        (self.clock() + 5, run['id']))
                    await self._inject(run, stage)

    async def _send_one(self):
        async with self.send_lock:
            row = self.db.execute("SELECT * FROM gc_outbox WHERE state='pending' ORDER BY rowid LIMIT 1").fetchone()
            if row is None or row['retry_at'] > self.clock():
                return False
            if self._identity() != (row['bot_id'], row['owner']):
                return False
            with self.db:
                self.db.execute("UPDATE gc_outbox SET state='sending' WHERE id=?", (row['id'],))
            params = {'chat_id': row['chat_id'], 'text': row['text'], 'disable_notification': True}
            if row['topic_id']:
                params['message_thread_id'] = row['topic_id']
            try:
                result = await self.router.telegram.call('sendMessage', **params)
                mid = result.get('message_id') if isinstance(result, dict) else None
                if (type(mid) is not int or mid <= 0 or (isinstance(result.get('chat'), dict)
                        and str(result['chat'].get('id')) != row['chat_id'])):
                    raise ConnectionError('Group message acknowledgement missing')
            except TelegramError as error:
                with self.db:
                    self.db.execute('UPDATE gc_outbox SET state=?,retry_at=?,error=? WHERE id=?',
                        ('pending' if error.code == 429 else 'failed', self.clock() + max(1, error.retry_after),
                         'Telegram:' + str(error.code), row['id']))
            except Exception as error:
                with self.db:
                    self.db.execute("UPDATE gc_outbox SET state='uncertain',error=? WHERE id=?",
                                    (type(error).__name__, row['id']))
            else:
                with self.db:
                    self.db.execute("UPDATE gc_outbox SET state='sent',message_id=? WHERE id=?", (mid, row['id']))
            return True

    async def tick(self):
        if self._identity() is None:
            return False
        await self._advance()
        await self._send_one()
        return True

    def recover(self):
        with self.db:
            self.db.execute("UPDATE gc_jobs SET state='pending' WHERE state='processing'")
            self.db.execute("UPDATE gc_outbox SET state='uncertain',error='Restart during send' WHERE state='sending'")

    async def loop(self):
        async def jobs():
            while True:
                try:
                    if not await self.handle_one():
                        await asyncio.sleep(.2)
                except Exception as error:
                    LOG.warning('Group collaboration command unavailable: %s', type(error).__name__)
                    await asyncio.sleep(1)

        async def advance():
            while True:
                try:
                    await self.tick()
                except Exception as error:
                    LOG.warning('Group collaboration unavailable: %s', type(error).__name__)
                await asyncio.sleep(.5)

        await asyncio.gather(jobs(), advance())
