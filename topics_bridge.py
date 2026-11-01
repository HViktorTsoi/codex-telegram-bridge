#!/usr/bin/env python3
"""One owner-only Telegram inbox, with topics bound to existing Codex threads."""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import fcntl
import json
import logging
import os
from pathlib import Path
import re
import signal
import sqlite3
import time

import aiohttp

from attachments import payload_from
from bridge import BOT_COMMANDS, Bridge, Codex, RpcError, Store, Telegram, TelegramError, chunks, now_ms, permitted
from session_picker import topic_missing_error
from message_format import send_output
from activity_indicator import TopicActivity

LOG = logging.getLogger("codex_telegram_topics")
DEFAULT_STATE = Path.home() / ".local/state/codex-telegram"


def command_of(message):
    if payload_from(message):
        return ""
    parts = message.get("text", "").strip().split(maxsplit=1)
    return parts[0].split("@")[0].lower() if parts else ""


def topic_of(message):
    value = message.get("message_thread_id")
    return int(value) if value else None


class ExactCodex(Codex):
    async def connect(self):
        result = await super().connect()
        if result.get("thread", {}).get("id") != self.thread_id:
            raise RpcError("Resumed thread does not match configured target")
        return result

    async def items(self, turn_id):
        # Older native rollouts expose full turn pages but not thread/items/list.
        if not getattr(self, '_legacy_items', False):
            try:
                async for entry in super().items(turn_id):
                    yield entry
                return
            except RpcError as error:
                if 'not supported' not in str(error).lower():
                    raise
                self._legacy_items = True
        cursor = None
        while True:
            params = {'threadId': self.thread_id, 'limit': 5, 'sortDirection': 'desc', 'itemsView': 'full'}
            if cursor:
                params['cursor'] = cursor
            result = await self.call('thread/turns/list', params)
            for turn in result['data']:
                if turn['id'] == turn_id:
                    for item in turn.get('items', []):
                        yield {'turnId': turn_id, 'item': item, 'startedAtMs': item.get('startedAtMs'),
                               'completedAtMs': item.get('completedAtMs')}
                    return
            cursor = result.get('nextCursor')
            if not cursor:
                raise RpcError('Turn not found during legacy recovery')


class RoutedStore(Store):
    """Each target owns its queue; every reply also retains its Telegram topic."""

    def __init__(self, path, thread_id):
        super().__init__(path)
        for table, name, kind in (("inbox", "message_thread_id", "INTEGER"),
                                  ("inbox", "source_message_id", "INTEGER"),
                                  ("outbox", "message_thread_id", "INTEGER")):
            columns = {row[1] for row in self.db.execute(f"PRAGMA table_info({table})")}
            if name not in columns:
                self.db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {kind}")
        self.db.commit()
        previous = self.get("bound_thread_id")
        if previous and previous != thread_id:
            raise ValueError("Target queue is bound to another native thread")
        self.set("bound_thread_id", thread_id)

    def accept(self, update, message=None):
        with self.db:
            if message is not None:
                self.db.execute(
                    "INSERT OR IGNORE INTO inbox(id,chat_id,text,received_ms,client_id,message_json,"
                    "media_group_id,message_thread_id,source_message_id) VALUES(?,?,?,?,?,?,?,?,?)",
                    (update["update_id"], str(message["chat"]["id"]),
                     message.get("text") or message.get("caption", ""), now_ms(),
                     "telegram-" + str(update["update_id"]), json.dumps(payload_from(message), ensure_ascii=False),
                     str(message["media_group_id"]) if message.get("media_group_id") else None,
                     topic_of(message), message.get("message_id")),
                )
            self.db.execute("INSERT OR REPLACE INTO kv VALUES('offset',?)",
                            (str(max(int(self.get("offset", 0)), update["update_id"] + 1)),))

    def turn_routes(self, turn_id):
        return self.db.execute(
            "SELECT chat_id,message_thread_id,MIN(received_ms) AS since_ms FROM inbox "
            "WHERE turn_id=? GROUP BY chat_id,message_thread_id", (turn_id,)).fetchall()

    def enqueue_at(self, key, chat_id, text, topic_id):
        with self.db:
            if self.db.execute('SELECT 1 FROM outbox WHERE key=?', (f'{key}:topic:{topic_id or 0}:0',)).fetchone():
                return
            for i, (part, rich_text) in enumerate(self.output_parts(key, text)):
                self.db.execute(
                    "INSERT OR IGNORE INTO outbox(key,chat_id,text,message_thread_id,rich_text) VALUES(?,?,?,?,?)",
                    (f"{key}:topic:{topic_id or 0}:{i}", str(chat_id), part, topic_id, rich_text))

    def enqueue(self, key, chat_id, text):
        if key.startswith("input:"):
            row = self.db.execute("SELECT message_thread_id FROM inbox WHERE id=?",
                                  (int(key.split(":", 2)[1]),)).fetchone()
            if row is None:
                raise ValueError("Input reply has no durable topic")
            topics = [row[0]]
        elif key.startswith("turn:"):
            topics = [r["message_thread_id"] for r in self.turn_routes(key.split(":", 2)[1])
                      if r["chat_id"] == str(chat_id)]
        else:
            topics = [r[0] for r in self.db.execute(
                "SELECT DISTINCT message_thread_id FROM inbox WHERE state='running' AND chat_id=?",
                (str(chat_id),))]
        for topic_id in topics:
            self.enqueue_at(key, chat_id, text, topic_id)


class TargetBridge(Bridge):
    def __init__(self, config, state_dir, target, sessions_text):
        # Reuse the proven one-thread dispatch/recovery implementation without a second poller.
        super().__init__({**config, "thread_id": target["thread_id"]}, state_dir)
        self.store.db.close()
        self.store = RoutedStore(Path(state_dir) / "bridge.sqlite", self.thread_id)
        self.codex = ExactCodex(config["socket_path"], self.thread_id)
        self.target, self.sessions_text = target, sessions_text
        self.on_missing_topic = None
        self.activity = TopicActivity(self.store)

    def record_item(self, turn_id, item, completed_ms=None, replay=False):
        if item.get("type") != "agentMessage" or not item.get("text", "").strip():
            return
        for route in self.store.turn_routes(turn_id):
            if completed_ms is not None and completed_ms < route["since_ms"]:
                continue
            if replay and completed_ms is None:
                continue
            self.store.enqueue_at(f"turn:{turn_id}:{item['id']}", route["chat_id"],
                                  item["text"], route["message_thread_id"])

    async def dispatch_one(self, row):
        if not self.owner_allowed():
            return await super().dispatch_one(row)
        command = command_of({"text": row["text"], **json.loads(row["message_json"] or "{}")})
        reply = None
        if command in {"/help", "/start", "/sessions"}:
            reply = (self.sessions_text() + "\n\n进入对应话题即可继续原会话。\n"
                     "/sessions 查看会话并添加绑定\n"
                     "/tasks 本机进度；/inbox 待我处理\n/to 选择本机会话派单；/aliases 查看 @别名\n"
                     "/status 查看此会话及完整 ID\n/model 查看或切换此会话模型\n"
                     "/effort 查看或修改此会话强度\n/stop 停止此会话的当前任务\n"
                     "/format rich 开启 Markdown 排版；/format plain 使用纯文本\n"
                     "支持图片、文件和相册；附件上限20MB。语音视频暂按文件接收。\n"
                     "未绑定话题不会创建或改用其他 Codex 会话。")
        elif command == "/status":
            settings = await self.codex.settings()
            if settings["thread"]["id"] != self.thread_id:
                raise RpcError("Thread identity mismatch")
            reply = (f"Telegram ↔ Codex 已连接\n主机：{self.config.get('host_id', '本机')}\n"
                     f"会话：{settings['thread'].get('name') or self.target['name']}\n"
                     f"ID：{self.thread_id}\n话题：{row['message_thread_id'] or '原私聊入口'}\n"
                     + self.settings_text(settings))
        if reply is None:
            return await super().dispatch_one(row)
        self.store.enqueue(f"input:{row['id']}:reply", row["chat_id"], reply)
        self.store.mark_batch(self.store.batch(row), "done")

    async def sender_loop(self):
        while True:
            row = self.store.db.execute(
                "SELECT * FROM outbox WHERE message_id IS NULL ORDER BY rowid LIMIT 1").fetchone()
            if row is None or row["retry_at"] > time.time() or not self.owner_allowed():
                await asyncio.sleep(0.5)
                continue
            try:
                sent = await send_output(self.telegram, chat_id=row['chat_id'], text=row['text'],
                                         rich_text=row['rich_text'], topic_id=row['message_thread_id'],
                                         label=self.target['name'])
                with self.store.db:
                    self.store.db.execute("UPDATE outbox SET message_id=? WHERE key=?",
                                          (sent["message_id"], row["key"]))
                LOG.info("Sent target %s message %s", self.target["key"], sent["message_id"])
            except (TelegramError, ConnectionError) as error:
                if isinstance(error, TelegramError) and error.code in {400, 403}:
                    with self.store.db:
                        self.store.db.execute("UPDATE outbox SET message_id=-1 WHERE key=?", (row["key"],))
                    LOG.error("Target delivery failed: %s", error)
                    if topic_missing_error(error) and row['message_thread_id'] and self.on_missing_topic:
                        try:
                            self.on_missing_topic(self.target['key'], row['message_thread_id'])
                        except (OSError, ValueError):
                            LOG.error('Could not persist unavailable Telegram topic')
                else:
                    delay = min(60, 2 ** min(row["attempts"] + 1, 6))
                    if isinstance(error, TelegramError):
                        delay = max(delay, error.retry_after)
                    with self.store.db:
                        self.store.db.execute("UPDATE outbox SET attempts=attempts+1,retry_at=? WHERE key=?",
                                              (time.time() + delay, row["key"]))
            await asyncio.sleep(0.4)

    async def update_activity(self, turns):
        if (not self.target.get('activity_indicator', self.config.get('activity_indicator', False))
                or not self.owner_allowed() or self.target.get('topic_missing') or self.telegram is None):
            return
        connected = self.ready.is_set() and turns is not None
        status = None
        if connected and any(t.get('status') == 'inProgress' for t in turns):
            try:
                thread = (await self.codex.call('thread/read', {'threadId': self.thread_id}))['thread']
                if thread['id'] != self.thread_id:
                    raise RpcError('Activity snapshot thread identity mismatch')
                status = thread.get('status')
            except (ConnectionError, RpcError, asyncio.TimeoutError):
                connected = False
        await self.activity.update(self.telegram, chat_id=self.owner,
                                   topic_id=self.target['message_thread_id'], name=self.target['name'],
                                   turns=turns, status=status, connected=connected)

    async def maintenance_loop(self):
        while True:
            turns = None
            if self.ready.is_set():
                try:
                    async with self.dispatch_lock:
                        turns = await self.reconcile()
                    if self.owner_allowed():
                        for row in self.store.db.execute(
                                "SELECT DISTINCT message_thread_id FROM inbox WHERE state='running'"):
                            params = {"chat_id": self.owner, "action": "typing"}
                            if row[0]:
                                params["message_thread_id"] = row[0]
                            await self.telegram.call("sendChatAction", **params)
                except (ConnectionError, RpcError, TelegramError, asyncio.TimeoutError):
                    pass
            try:
                await self.update_activity(turns)
            except (ConnectionError, RpcError, TelegramError, asyncio.TimeoutError) as error:
                LOG.warning('Topic activity update failed (%s)', getattr(error, 'code', type(error).__name__))
            await asyncio.sleep(4)


class RouterStore:
    def __init__(self, path, initial_offset=0):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS kv(key TEXT PRIMARY KEY,value TEXT);
            CREATE TABLE IF NOT EXISTS jobs(
                id INTEGER PRIMARY KEY,target_key TEXT,thread_id TEXT,topic_id INTEGER,
                chat_id TEXT,message_id INTEGER,message_json TEXT,
                error TEXT,state TEXT DEFAULT 'pending');
            CREATE TABLE IF NOT EXISTS albums(
                chat_id TEXT,media_group_id TEXT,target_key TEXT,thread_id TEXT,topic_id INTEGER,
                PRIMARY KEY(chat_id,media_group_id));
            CREATE TABLE IF NOT EXISTS notices(
                id INTEGER PRIMARY KEY,chat_id TEXT,topic_id INTEGER,text TEXT,
                message_id INTEGER,attempts INTEGER DEFAULT 0,retry_at REAL DEFAULT 0);
        """)
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO kv VALUES('offset',?)", (str(initial_offset),))

    def offset(self):
        return int(self.db.execute("SELECT value FROM kv WHERE key='offset'").fetchone()[0])

    def accept(self, update, message, target=None, error=None):
        with self.db:
            if message is not None:
                topic_id = topic_of(message)
                if target and message.get("media_group_id"):
                    key = (str(message["chat"]["id"]), str(message["media_group_id"]))
                    album = self.db.execute("SELECT * FROM albums WHERE chat_id=? AND media_group_id=?", key).fetchone()
                    if album and (album["target_key"], album["thread_id"], album["topic_id"]) != (
                            target["key"], target["thread_id"], topic_id):
                        target, error = None, "相册目标与首张图片不同，请在同一已绑定话题重新发送。"
                    elif not album:
                        self.db.execute("INSERT INTO albums VALUES(?,?,?,?,?)",
                                        (*key, target["key"], target["thread_id"], topic_id))
                self.db.execute(
                    "INSERT OR IGNORE INTO jobs(id,target_key,thread_id,topic_id,chat_id,message_id,message_json,error) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (update["update_id"], target["key"] if target else None,
                     target["thread_id"] if target else None, topic_id, str(message["chat"]["id"]),
                     message.get("message_id"), json.dumps(message, ensure_ascii=False), error))
            self.db.execute("UPDATE kv SET value=? WHERE key='offset'",
                            (str(max(self.offset(), update["update_id"] + 1)),))

    def pending(self):
        return self.db.execute("SELECT * FROM jobs WHERE state='pending' ORDER BY id LIMIT 1").fetchone()

    def finish(self, row):
        with self.db:
            if row["error"]:
                self.db.execute("INSERT OR IGNORE INTO notices(id,chat_id,topic_id,text) VALUES(?,?,?,?)",
                                (row["id"], row["chat_id"], row["topic_id"], row["error"]))
            self.db.execute("UPDATE jobs SET state='done' WHERE id=?", (row["id"],))


class TopicRouter:
    def __init__(self, config, state_dir):
        self.config, self.state_dir = config, Path(state_dir)
        self.owner = str(config["owner_id"])
        self.targets = {t["key"]: dict(t) for t in config["targets"]}
        if len(self.targets) != len(config["targets"]) or not self.targets:
            raise ValueError("Target keys must be unique")
        if any(not re.fullmatch(r"[a-z0-9_-]{1,40}", key) for key in self.targets):
            raise ValueError("Invalid target key")
        all_topics = {int(t["message_thread_id"]) for t in self.targets.values()}
        self.by_topic = {int(t["message_thread_id"]): t for t in self.targets.values() if not t.get('topic_missing')}
        if len(all_topics) != len(self.targets) or any(x <= 0 for x in all_topics):
            raise ValueError("Each target requires a distinct positive Telegram topic")
        if len({t['thread_id'] for t in self.targets.values()}) != len(self.targets):
            raise ValueError("Each target must use a distinct existing native thread")
        self.root_target = self.targets[config["root_target"]]
        if self.root_target['thread_id'] != config['thread_id']:
            raise ValueError("Original private-chat binding must match root target")
        self.store = RouterStore(self.state_dir / "topics.sqlite", config.get("initial_telegram_offset", 0))
        self.bridges = {}
        for key, target in self.targets.items():
            path = self.state_dir / "targets" / key
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
            self.bridges[key] = TargetBridge(config, path, target, self.sessions_text)
            self.bridges[key].on_missing_topic = self.mark_topic_missing
        self.username, self.last_poll_at, self.last_error = None, None, None
        self.telegram = None
        self.bot_id = None
        self.tasks, self.started_targets = [], set()
        self.running, self.stopping, self.failure = False, False, None
        from session_picker import SessionPicker
        self.picker = SessionPicker(self)
        from control_center import ControlCenter
        self.control = ControlCenter(self)

    def access(self):
        try:
            return json.loads((Path(self.config["telegram_state_dir"]) / "access.json").read_text())
        except (OSError, ValueError):
            return {}

    def owner_allowed(self):
        return permitted({"from": {"id": self.owner}, "chat": {"id": self.owner, "type": "private"}},
                         self.access(), self.owner)

    def sessions_text(self):
        lines = ["已绑定的原 Codex 会话："]
        for key, target in self.targets.items():
            ready = self.bridges.get(key) and self.bridges[key].ready.is_set()
            state = '话题不可用，可重建' if target.get('topic_missing') else ('已连接' if ready else '连接中')
            lines.append(f"• {target['name']}（{state}）")
        lines.append("原私聊入口继续连接「" + self.root_target['name'] + "」。")
        return "\n".join(lines)

    def ingest(self, update):
        if self.control.ingest(update):
            return
        if self.picker.ingest(update):
            return
        message = update.get("message", {})
        if not permitted(message, self.access(), self.owner):
            self.store.accept(update, None)
            return
        if any(k in message for k in ("forum_topic_created", "forum_topic_edited", "forum_topic_closed",
                                      "forum_topic_reopened", "general_forum_topic_hidden", "general_forum_topic_unhidden")):
            self.store.accept(update, None)
            return
        topic_id = topic_of(message)
        target = self.by_topic.get(topic_id) if topic_id else self.root_target
        error = None if target else "此话题尚未绑定 Codex 会话。发送 /sessions 可选择已有会话并创建绑定话题。"
        self.store.accept(update, message, target, error)

    def forward_one(self):
        row = self.store.pending()
        if row is None:
            return False
        if not self.owner_allowed():
            with self.store.db:
                self.store.db.execute("UPDATE jobs SET state='denied' WHERE id=?", (row["id"],))
            return True
        if not row["error"]:
            target = self.bridges.get(row["target_key"])
            if target is None or target.thread_id != row["thread_id"]:
                # A configuration edit must never redirect accepted work to a different thread.
                with self.store.db:
                    self.store.db.execute("UPDATE jobs SET state='blocked' WHERE id=?", (row["id"],))
                return True
            target.store.accept({"update_id": row["id"]}, json.loads(row["message_json"]))
        self.store.finish(row)
        return True

    async def forwarding_loop(self):
        while True:
            while self.forward_one():
                pass
            await asyncio.sleep(0.2)

    async def poll_loop(self):
        while True:
            try:
                updates = await self.telegram.call("getUpdates", offset=self.store.offset(), timeout=25,
                                                   allowed_updates=["message", "callback_query"])
                self.last_poll_at, self.last_error = time.time(), None
                for update in updates:
                    self.ingest(update)
            except TelegramError as error:
                if error.code in {401, 409}:
                    raise
                self.last_error = f"Telegram poll: {error.code}"
                await asyncio.sleep(max(3, error.retry_after))
            except (ConnectionError, ValueError):
                self.last_error = "Telegram poll unavailable"
                await asyncio.sleep(3)

    async def notices_loop(self):
        while True:
            row = self.store.db.execute(
                "SELECT * FROM notices WHERE message_id IS NULL ORDER BY id LIMIT 1").fetchone()
            if row is None or row["retry_at"] > time.time() or not self.owner_allowed():
                await asyncio.sleep(0.5)
                continue
            try:
                sent = await send_output(self.telegram, chat_id=row['chat_id'], text=row['text'],
                                         rich_text=row['rich_text'], topic_id=row['topic_id'], label=row['label'])
                with self.store.db:
                    self.store.db.execute("UPDATE notices SET message_id=? WHERE id=?", (sent["message_id"], row["id"]))
            except (TelegramError, ConnectionError) as error:
                with self.store.db:
                    if isinstance(error, TelegramError) and error.code in {400, 403}:
                        self.store.db.execute("UPDATE notices SET message_id=-1 WHERE id=?", (row["id"],))
                    else:
                        delay = max(3, getattr(error, "retry_after", 0), min(60, 2 ** min(row["attempts"] + 1, 6)))
                        self.store.db.execute("UPDATE notices SET attempts=attempts+1,retry_at=? WHERE id=?",
                                              (time.time() + delay, row["id"]))

    def write_health(self):
        counts = {key: 0 for key in ("pending_inputs", "running_inputs", "unsent_messages")}
        targets = {}
        for key, bridge in self.bridges.items():
            local_counts = bridge.store.counts()
            for name, count in local_counts.items():
                counts[name] += count
            targets[key] = {"name": bridge.target['name'], "thread_id": bridge.thread_id,
                            "message_thread_id": bridge.target['message_thread_id'],
                            "topic_missing": bool(bridge.target.get('topic_missing')),
                            "connected": bridge.ready.is_set(), "last_error": bridge.last_error, **local_counts}
        pending = self.store.db.execute("SELECT COUNT(*) FROM jobs WHERE state='pending'").fetchone()[0]
        counts['pending_inputs'] += pending
        counts['unsent_messages'] += self.store.db.execute("SELECT COUNT(*) FROM notices WHERE message_id IS NULL").fetchone()[0]
        ui_pending = self.store.db.execute("SELECT COUNT(*) FROM ui_jobs WHERE state IN ('pending','processing')").fetchone()[0]
        data = {"pid": os.getpid(), "updated_at": time.time(), "mode": "topics", "bot_username": self.username,
                "thread_id": self.config['thread_id'], "connected": all(b.ready.is_set() for b in self.bridges.values()),
                "last_poll_at": self.last_poll_at, "last_error": self.last_error,
                "blocked_inputs": self.store.db.execute("SELECT COUNT(*) FROM jobs WHERE state='blocked'").fetchone()[0],
                "targets": targets, "ui_pending": ui_pending,
                "bindings_waiting_review": self.store.db.execute("SELECT COUNT(*) FROM binding_ops WHERE status='uncertain'").fetchone()[0], **counts}
        tmp = self.state_dir / "health.json.tmp"
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
        tmp.replace(self.state_dir / "health.json")

    async def health_loop(self):
        while True:
            self.write_health()
            await asyncio.sleep(4)

    async def catalog_call(self, method, params):
        bridge = self.bridges[self.root_target['key']]
        if not bridge.ready.is_set():
            raise ConnectionError('Native conversation service is connecting')
        return await bridge.codex.call(method, params)

    async def verify_existing(self, thread_id):
        # No model, permission or history overrides; this must remain the exact native ID.
        metadata = (await self.catalog_call('thread/read', {'threadId': thread_id}))['thread']
        if metadata['id'] != thread_id or metadata.get('ephemeral') or metadata.get('canAcceptDirectInput') is False:
            raise ValueError('Native conversation is not bindable')
        client = ExactCodex(self.config['socket_path'], thread_id)
        try:
            result = await client.connect()
            return result['thread']
        finally:
            await client.close()

    def spawn(self, coro):
        task = asyncio.create_task(coro)
        self.tasks.append(task)
        def completed(done):
            if self.stopping or done.cancelled():
                return
            error = done.exception() or RuntimeError('Topic bridge worker exited')
            if self.failure and not self.failure.done():
                self.failure.set_exception(error)
        task.add_done_callback(completed)

    def start_target(self, bridge):
        key = bridge.target['key']
        if key in self.started_targets:
            return
        self.started_targets.add(key)
        bridge.telegram, bridge.username = self.telegram, self.username
        for coro in (bridge.connection_loop(False), bridge.input_loop(), bridge.event_loop(),
                     bridge.sender_loop(), bridge.maintenance_loop()):
            self.spawn(coro)

    def persist_targets(self, updated):
        """Compare and atomically persist additions, repairs and unavailable flags."""
        config_path = self.state_dir / 'config.json'
        with (self.state_dir / 'config.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            original = config_path.read_bytes()
            current = json.loads(original)
            for field in ('owner_id', 'socket_path', 'telegram_state_dir', 'thread_id', 'root_target', 'expected_bot_username'):
                if current.get(field) != self.config.get(field):
                    raise ValueError('Configuration changed; reload before binding')
            persisted = {t['key']: t for t in current['targets']}
            if persisted != self.targets and persisted != updated:
                raise ValueError('Target configuration changed; reload before binding')
            if persisted != updated:
                current['targets'] = list(updated.values())
                backups = self.state_dir / 'binding-config-backups'
                backups.mkdir(exist_ok=True, mode=0o700)
                backup = backups / (str(time.time_ns()) + '.json')
                fd = os.open(backup, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                with os.fdopen(fd, 'wb') as output:
                    output.write(original)
                tmp = self.state_dir / ('config.json.binding.' + str(time.time_ns()) + '.tmp')
                fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                with os.fdopen(fd, 'w') as output:
                    json.dump(current, output, ensure_ascii=False, indent=2)
                    output.write('\n')
                    output.flush()
                    os.fsync(output.fileno())
                tmp.replace(config_path)
                directory = os.open(self.state_dir, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
        self.config.update(current)

    def mark_topic_missing(self, key, topic_id):
        target = self.targets[key]
        # A late failure for a replaced topic must not invalidate its new topic.
        if target['message_thread_id'] != topic_id or target.get('topic_missing'):
            return
        updated = {**target, 'topic_missing': True, 'topic_missing_reason': 'message thread not found'}
        self.persist_targets({**self.targets, key: updated})
        self.targets[key] = updated
        self.bridges[key].target = updated
        self.by_topic.pop(topic_id, None)
        if key == self.config['root_target']:
            self.root_target = updated
        self.picker.operation(target['thread_id'], updated, 'missing')

    async def install_target(self, target, replace_topic=False, expected_topic_id=None):
        # The recorded topic is reused after a crash; this method never creates one.
        replacing = None
        for existing in self.targets.values():
            if existing['thread_id'] == target['thread_id']:
                if existing['message_thread_id'] == target['message_thread_id']:
                    return
                if (not replace_topic or existing['message_thread_id'] != expected_topic_id
                        or existing['key'] != target['key']):
                    raise ValueError('Native conversation already has another topic')
                replacing = existing
        if (target['key'] in self.targets and not replacing) or int(target['message_thread_id']) in self.by_topic:
            raise ValueError('Target key or topic already bound')
        if not re.fullmatch(r'[a-z0-9_-]{1,40}', target['key']) or int(target['message_thread_id']) <= 0:
            raise ValueError('Invalid target binding')
        if replacing:
            bridge = self.bridges[target['key']]
        else:
            path = self.state_dir / 'targets' / target['key']
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
            bridge = TargetBridge(self.config, path, target, self.sessions_text)
            bridge.on_missing_topic = self.mark_topic_missing
        try:
            self.persist_targets({**self.targets, target['key']: dict(target)})
        except Exception:
            if not replacing:
                bridge.store.db.close()
            raise
        if replacing:
            self.by_topic.pop(replacing['message_thread_id'], None)
        self.targets[target['key']] = dict(target)
        bridge.target = self.targets[target['key']]
        if target['key'] == self.config['root_target']:
            self.root_target = self.targets[target['key']]
        if not target.get('topic_missing'):
            self.by_topic[int(target['message_thread_id'])] = self.targets[target['key']]
        self.bridges[target['key']] = bridge
        if self.running:
            self.start_target(bridge)

    async def run(self):
        if not self.owner_allowed():
            raise RuntimeError("Configured owner is not in the allowlist")
        text = (Path(self.config['telegram_state_dir']) / '.env').read_text()
        match = re.search(r"^\s*TELEGRAM_BOT_TOKEN\s*=\s*(.*)$", text, re.M)
        if not match:
            raise RuntimeError("Telegram token is missing")
        async with aiohttp.ClientSession() as session:
            self.telegram = Telegram(match[1].strip().strip("\"'"), session)
            me = await self.telegram.call("getMe")
            self.username = me['username']
            self.bot_id = me['id']
            if self.username.lower() != self.config['expected_bot_username'].lstrip('@').lower():
                raise RuntimeError("Bot identity mismatch; no polling started")
            if not me.get('has_topics_enabled'):
                raise RuntimeError("Enable private topics in BotFather before starting topic mode")
            if (await self.telegram.call('getWebhookInfo')).get('url'):
                raise RuntimeError("Bot already has a webhook; leaving it unchanged")
            scope = {'type': 'chat', 'chat_id': int(self.owner)}
            from control_center import COMMANDS
            commands = [*BOT_COMMANDS, {'command': 'sessions', 'description': '会话列表与添加绑定'}, *COMMANDS]
            if await self.telegram.call('getMyCommands', scope=scope) != commands:
                await self.telegram.call('setMyCommands', scope=scope, commands=commands)
            self.failure = asyncio.get_running_loop().create_future()
            self.running = True
            try:
                await self.picker.recover()
                for bridge in self.bridges.values():
                    self.start_target(bridge)
                for coro in (self.poll_loop(), self.forwarding_loop(), self.notices_loop(), self.health_loop(), self.picker.loop()):
                    self.spawn(coro)
                for coro in (self.control.jobs_loop(), self.control.delivery_loop(), self.control.refresh_loop()):
                    self.spawn(coro)
                self.spawn(self.control.sync_loop())
                await self.failure
            finally:
                self.stopping, self.running = True, False
                for task in self.tasks:
                    task.cancel()
                await asyncio.gather(*self.tasks, return_exceptions=True)
                for bridge in self.bridges.values():
                    await bridge.codex.close()
                    bridge.ready.clear()
                self.write_health()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['run', 'status'])
    parser.add_argument('--state-dir', type=Path, default=DEFAULT_STATE)
    args = parser.parse_args()
    if args.action == 'status':
        print((args.state_dir / 'health.json').read_text())
        return
    os.umask(0o077)
    args.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock = (args.state_dir / 'bridge.lock').open('a')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit('The Telegram bridge is already running')
    config = json.loads((args.state_dir / 'config.json').read_text())
    if config.get('setup_required') or config.get('mode') != 'topics':
        raise SystemExit('Existing-thread topic setup is incomplete')
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    router = TopicRouter(config, args.state_dir)

    async def serve():
        task = asyncio.create_task(router.run())
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            loop.add_signal_handler(sig, task.cancel)
        with contextlib.suppress(asyncio.CancelledError):
            await task
    try:
        asyncio.run(serve())
    except (RuntimeError, TelegramError) as error:
        LOG.error('Topic bridge stopped: %s', error)
        raise SystemExit(1)


if __name__ == '__main__':
    main()
