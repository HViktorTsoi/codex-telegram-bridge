#!/usr/bin/env python3
"""One owner-only Telegram inbox, with topics bound to existing Codex threads."""
from __future__ import annotations

import argparse
import asyncio
import contextlib
from collections import Counter
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
from bridge import BOT_COMMANDS, Bridge, Codex, RpcError, Store, Telegram, TelegramError, chunks, now_ms, permitted, refresh_health_snapshot
from session_picker import topic_missing_error
from message_format import send_output
from activity_indicator import TopicActivity
from native_mirror import NativeMirror
from reply_context import serialized as reply_serialized

LOG = logging.getLogger("codex_telegram_topics")
DEFAULT_STATE = Path.home() / ".local/state/codex-telegram"
GLOBAL_COMMANDS = {'/start', '/help', '/sessions', '/tasks', '/inbox', '/to', '/aliases'}


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
    """Reply to the original topic, with global command panels in the main chat."""

    def __init__(self, path, thread_id):
        super().__init__(path)
        self.on_input = None
        for table, name, kind in (("inbox", "message_thread_id", "INTEGER"),
                                  ("inbox", "source_message_id", "INTEGER"),
                                  ("inbox", "main_guard_json", "TEXT"),
                                  ("outbox", "message_thread_id", "INTEGER")):
            columns = {row[1] for row in self.db.execute(f"PRAGMA table_info({table})")}
            if name not in columns:
                self.db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {kind}")
        self.db.commit()
        previous = self.get("bound_thread_id")
        if previous and previous != thread_id:
            raise ValueError("Target queue is bound to another native thread")
        self.set("bound_thread_id", thread_id)

    def accept(self, update, message=None, *, reply_context=None, guard_binding=None):
        inserted = False
        with self.db:
            if message is not None:
                inserted = bool(self.db.execute(
                    "INSERT OR IGNORE INTO inbox(id,chat_id,text,received_ms,client_id,message_json,"
                    "media_group_id,message_thread_id,source_message_id,reply_json,main_guard_json) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (update["update_id"], str(message["chat"]["id"]),
                     message.get("text") or message.get("caption", ""), now_ms(),
                     "telegram-" + str(update["update_id"]), json.dumps(payload_from(message), ensure_ascii=False),
                     str(message["media_group_id"]) if message.get("media_group_id") else None,
                     topic_of(message), message.get("message_id"), reply_serialized(message, reply_context),
                     json.dumps(guard_binding, sort_keys=True) if guard_binding else None),
                ).rowcount)
            self.db.execute("INSERT OR REPLACE INTO kv VALUES('offset',?)",
                            (str(max(int(self.get("offset", 0)), update["update_id"] + 1)),))
        if inserted and self.on_input is not None:
            self.on_input()

    def turn_routes(self, turn_id):
        return self.db.execute(
            "SELECT chat_id,message_thread_id,MIN(received_ms) AS since_ms FROM inbox "
            "WHERE turn_id=? GROUP BY chat_id,message_thread_id", (turn_id,)).fetchall()

    def enqueue_at(self, key, chat_id, text, topic_id):
        self.enqueue_parts(f'{key}:topic:{topic_id or 0}', chat_id, text, topic_id)

    def enqueue(self, key, chat_id, text):
        if key.startswith("input:"):
            row = self.db.execute("SELECT message_thread_id,text,message_json FROM inbox WHERE id=?",
                                  (int(key.split(":", 2)[1]),)).fetchone()
            if row is None:
                raise ValueError("Input reply has no durable topic")
            message = {"text": row['text'], **json.loads(row['message_json'] or '{}')}
            topics = [None if command_of(message) in GLOBAL_COMMANDS else row['message_thread_id']]
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
        self.activity = TopicActivity(self.store, position=target.get('activity_position', config.get('activity_position', 'bottom')))
        self.mirror = NativeMirror(self.store)
        from completion_delivery import CompletionDelivery
        self.delivery = CompletionDelivery(self)
        self.receipt_event = asyncio.Event()
        self.store.on_input = self.receipt_event.set
        self.store.db.execute('''CREATE TABLE IF NOT EXISTS completion_ready_items(
            turn_id TEXT,fingerprint TEXT,pending INTEGER DEFAULT 1,PRIMARY KEY(turn_id,fingerprint))''')
        if 'pending' not in {r[1] for r in self.store.db.execute('PRAGMA table_info(completion_ready_items)')}:
            self.store.db.execute('ALTER TABLE completion_ready_items ADD COLUMN pending INTEGER DEFAULT 1')
        self.store.db.commit()

    def validate_destination(self, rows):
        for row in rows:
            if not row['main_guard_json']:
                continue
            try:
                expected = json.loads(row['main_guard_json'])
                valid = (expected['target_key'] == self.target['key']
                         and expected['thread_id'] == self.thread_id
                         and expected['thread_id'] == self.target['thread_id']
                         and expected['topic_id'] == self.target['message_thread_id']
                         and row['message_thread_id'] == expected['topic_id']
                         and not self.target.get('topic_missing'))
            except (ValueError, TypeError, KeyError):
                valid = False
            if not valid:
                self.store.mark_batch(rows, 'failed')
                self.store.enqueue_at(f"main-guard-rejected:{rows[0]['id']}", rows[0]['chat_id'],
                                      '所选会话的绑定已变化，这条 All 消息尚未执行。请重新发送并选择目标会话。', None)
                return False
        return True

    def mirror_allowed(self):
        return (self.target.get('native_mirror', self.config.get('native_mirror', False))
                and self.owner_allowed() and not self.target.get('topic_missing'))

    async def update_native_mirror(self, turns):
        if not self.mirror_allowed():
            self.mirror.suspend()
        elif turns is not None:
            await self.mirror.update(self, turns)

    def record_item(self, turn_id, item, completed_ms=None, replay=False):
        self.store.images.observe(turn_id, item, completed_ms)
        if not replay and self.mirror_allowed():
            self.mirror.note_event(self, turn_id, item)
        if item.get("type") != "agentMessage" or not item.get("text", "").strip():
            return
        if self.delivery.enabled:
            # Completion events identify finished items. Readback supplies stable
            # content identities even when native history renumbers item IDs.
            fingerprint = NativeMirror.fingerprint(item)
            if not replay and fingerprint:
                with self.store.db:
                    self.store.db.execute('INSERT INTO completion_ready_items VALUES(?,?,1) '
                                          'ON CONFLICT(turn_id,fingerprint) DO UPDATE SET pending=1',
                                          (turn_id, fingerprint))
            return
        for route in self.store.turn_routes(turn_id):
            if completed_ms is not None and completed_ms < route["since_ms"]:
                continue
            if replay and completed_ms is None:
                continue
            self.store.enqueue_at(f"turn:{turn_id}:{item['id']}", route["chat_id"],
                                  self.store.images.agent_text(turn_id, item), route["message_thread_id"])

    def finish_turn(self, turn):
        if self.delivery.enabled:
            if turn.get('status') not in ('completed', 'failed', 'interrupted'):
                return
            self.delivery.terminal(turn)
        super().finish_turn(turn)

    async def reconcile(self, replay=False):
        turns = await super().reconcile(replay=replay)
        if not self.delivery.enabled:
            return turns
        for turn in turns:
            self.delivery.watch(turn)
        # Desktop items must be durably copied before their turn is released.
        await self.update_native_mirror(turns)
        for turn in turns:
            terminal = turn.get('status') in ('completed', 'failed', 'interrupted')
            if terminal and not self.delivery.needs_scan(turn['id']):
                continue
            routes = self.store.turn_routes(turn['id'])
            ready_rows = self.store.db.execute(
                'SELECT fingerprint,pending FROM completion_ready_items WHERE turn_id=?', (turn['id'],)).fetchall()
            ready = {r['fingerprint'] for r in ready_rows}
            collect = self.delivery.collect_output(turn['id'])
            if terminal or (routes and any(r['pending'] for r in ready_rows)):
                occurrences = Counter()
                legacy_sources = {}
                prefix = 'turn:' + turn['id'] + ':'
                for old in self.store.db.execute('SELECT * FROM outbox WHERE substr(key,1,?)=? ORDER BY rowid',
                                                  (len(prefix), prefix)):
                    base = old['key'].rsplit(':topic:', 1)[0]
                    if base.startswith(prefix + 'completion-content-') or old['media_json']:
                        continue
                    source = (old['chat_id'], old['message_thread_id'], base)
                    legacy_sources[source] = legacy_sources.get(source, '') + old['text']
                legacy = Counter((chat, topic, text) for (chat, topic, _), text in legacy_sources.items())
                history_occurrences = Counter()
                window_since = int(self.delivery.state()['since'] * 1000)
                # Exhausting this iterator is the proof for releasing a terminal
                # turn; a partial/failed scan leaves every task row held.
                async for entry in self.codex.items(turn['id']):
                    item = entry['item']
                    self.store.images.observe(turn['id'], item, entry.get('completedAtMs'))
                    fingerprint = NativeMirror.fingerprint(item)
                    if item.get('type') != 'agentMessage' or not fingerprint:
                        continue
                    occurrences[fingerprint] += 1
                    if not collect:
                        continue
                    if not terminal and fingerprint not in ready:
                        continue
                    identity = 'completion-content-' + fingerprint + '-' + str(occurrences[fingerprint])
                    body = self.store.images.agent_text(turn['id'], item)
                    for route in routes:
                        completed_ms = entry.get('completedAtMs')
                        source = (route['chat_id'], route['message_thread_id'], body)
                        history_occurrences[source] += 1
                        cutoff = max(route['since_ms'], window_since)
                        if completed_ms is not None and completed_ms < cutoff:
                            continue
                        if completed_ms is None and history_occurrences[source] <= legacy[source]:
                            continue  # Legacy delivery already owns this history occurrence.
                        self.store.enqueue_at(f"turn:{turn['id']}:{identity}", route['chat_id'],
                                              body, route['message_thread_id'])
                with self.store.db:
                    for fingerprint in occurrences:
                        self.store.db.execute('UPDATE completion_ready_items SET pending=0 WHERE turn_id=? AND fingerprint=?',
                                              (turn['id'], fingerprint))
            if terminal:
                self.delivery.seal(turn)
        return turns

    async def dispatch_one(self, row):
        if not self.owner_allowed():
            return await super().dispatch_one(row)
        if not self.validate_destination(self.store.batch(row)):
            return
        command = command_of({"text": row["text"], **json.loads(row["message_json"] or "{}")})
        reply = None
        if command in {"/help", "/start", "/sessions"}:
            reply = (self.sessions_text() + "\n\n进入对应话题即可继续原会话。\n"
                     "All 普通消息先选择接收会话，再确认发送；/sessions → 待发送可找回未发内容。\n"
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
            if self.delivery.enabled:
                reply += ('\n通知：完成后正常提醒；首次状态卡静音'
                          if self.delivery.preview_style == 'status-card' else '\n通知：仅整轮任务结束时提醒')
                if self.delivery.preview_style == 'block':
                    reply += '\n预览：整块累计进度'
                elif self.delivery.preview_style == 'status-card':
                    reply += '\n预览：独立状态消息＋累计正文草稿'
        if reply is None:
            result = await super().dispatch_one(row)
            if self.delivery.enabled:
                accepted = self.store.db.execute('SELECT turn_id,state FROM inbox WHERE id=?', (row['id'],)).fetchone()
                if accepted and accepted['turn_id'] and accepted['state'] == 'running':
                    self.delivery.watch({'id': accepted['turn_id'], 'status': 'inProgress'})
                self.receipt_event.set()
            return result
        self.store.enqueue(f"input:{row['id']}:reply", row["chat_id"], reply)
        self.store.mark_batch(self.store.batch(row), "done")

    async def send_one(self, row):
        """Deliver one eligible row while the caller owns the preview lock."""
        self.delivery.begin(row)
        self.store.images.begin(row)
        sent = await send_output(self.telegram, chat_id=row['chat_id'], text=row['text'],
                                 rich_text=row['rich_text'], topic_id=row['message_thread_id'], media_json=row['media_json'],
                                 disable_notification=self.delivery.silent(row) if self.delivery.enabled else None)
        self.store.images.sent(row, sent['message_id'])
        self.delivery.sent(row, sent['message_id'])
        if self.delivery.enabled and row['key'].startswith('completion-notice:'):
            await self.delivery.complete_status(self.telegram, row)
            self.delivery.reschedule_receipt()
            self.receipt_event.set()
        LOG.info("Sent target %s message %s", self.target["key"], sent["message_id"])

    async def sender_loop(self):
        self.store.images.recover()
        self.delivery.recover()
        while True:
            row = self.delivery.next_row()
            if row is None or row["retry_at"] > time.time() or not self.owner_allowed():
                await asyncio.sleep(0.5)
                continue
            try:
                if self.delivery.enabled:
                    async with self.delivery.preview_lock:
                        # An in-flight receipt must finish before a real message
                        # clears it; late rows/permission changes may defer this row.
                        current = self.delivery.next_row()
                        if current is None or current['key'] != row['key'] or not self.owner_allowed():
                            continue
                        await self.send_one(current)
                else:
                    await self.send_one(row)
            except (TelegramError, ConnectionError) as error:
                if self.delivery.failed(row, error):
                    if topic_missing_error(error) and row['message_thread_id'] and self.on_missing_topic:
                        try:
                            self.on_missing_topic(self.target['key'], row['message_thread_id'])
                        except (OSError, ValueError):
                            LOG.error('Could not persist unavailable Telegram topic')
                elif isinstance(error, TelegramError) and error.code in {400, 403}:
                    with self.store.db:
                        self.store.db.execute("UPDATE outbox SET message_id=-1 WHERE key=?", (row["key"],))
                    LOG.error("Target delivery failed: %s", error)
                    if topic_missing_error(error) and row['message_thread_id'] and self.on_missing_topic:
                        try:
                            self.on_missing_topic(self.target['key'], row['message_thread_id'])
                        except (OSError, ValueError):
                            LOG.error('Could not persist unavailable Telegram topic')
                    self.store.images.failed(row)
                elif row['media_json'] and getattr(error, 'code', None) != 429:
                    self.store.images.failed(row, uncertain=getattr(error, 'uncertain', True))
                else:
                    self.store.images.retry(row)
                    delay = min(60, 2 ** min(row["attempts"] + 1, 6))
                    if isinstance(error, TelegramError):
                        delay = max(delay, error.retry_after)
                    with self.store.db:
                        self.store.db.execute("UPDATE outbox SET attempts=attempts+1,retry_at=? WHERE key=?",
                                              (time.time() + delay, row["key"]))
            await asyncio.sleep(0.4)

    async def update_activity(self, turns):
        if self.delivery.enabled:
            if not self.owner_allowed() or self.target.get('topic_missing') or self.telegram is None:
                return
            previous = self.activity.load()
            if (previous.get('open') and previous.get('message_id')
                    and previous.get('route') == [str(self.owner), int(self.target['message_thread_id'])]
                    and self.delivery.clock() >= previous.get('retire_retry_at', 0)):
                try:
                    await self.telegram.call('deleteMessage', chat_id=self.owner, message_id=previous['message_id'])
                except (TelegramError, ConnectionError) as error:
                    if getattr(error, 'code', None) not in (400, 403):
                        previous['retire_retry_at'] = self.delivery.clock() + max(15, getattr(error, 'retry_after', 0))
                    else:
                        previous.update(open=False, delivery='retired')
                else:
                    previous.update(open=False, delivery='retired')
                self.activity.save(previous)
            connected = self.ready.is_set() and turns is not None
            status = None
            if connected and any(t.get('status') == 'inProgress' for t in turns):
                try:
                    thread = (await self.codex.call('thread/read', {'threadId': self.thread_id}))['thread']
                    if thread['id'] != self.thread_id:
                        raise RpcError('Draft snapshot thread identity mismatch')
                    status = thread.get('status')
                except (ConnectionError, RpcError, asyncio.TimeoutError):
                    connected = False
            await self.delivery.update(self.telegram, turns=turns, status=status, connected=connected)
            return
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

    async def preview_loop(self):
        """Acknowledge durable input without waiting for native work or history."""
        self.receipt_event.set()  # Recover still-pending receipts after restart.
        while True:
            try:
                await asyncio.wait_for(self.receipt_event.wait(), timeout=1)
            except asyncio.TimeoutError:
                pass
            else:
                self.receipt_event.clear()
                await asyncio.sleep(0.1)  # Coalesce a burst of album/input parts.
                self.receipt_event.clear()
            if not self.delivery.enabled or self.telegram is None:
                continue
            try:
                await self.delivery.receipt(self.telegram, connected=self.ready.is_set())
            except (ConnectionError, TelegramError, OSError, asyncio.TimeoutError) as error:
                LOG.warning('Receipt preview failed (%s)', getattr(error, 'code', type(error).__name__))

    async def maintenance_loop(self):
        while True:
            turns = None
            if self.ready.is_set():
                try:
                    async with self.dispatch_lock:
                        turns = await self.reconcile()
                        await self.update_native_mirror(turns)
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
                error TEXT,state TEXT DEFAULT 'pending',received_ms INTEGER);
            CREATE TABLE IF NOT EXISTS albums(
                chat_id TEXT,media_group_id TEXT,target_key TEXT,thread_id TEXT,topic_id INTEGER,
                PRIMARY KEY(chat_id,media_group_id));
            CREATE TABLE IF NOT EXISTS notices(
                id INTEGER PRIMARY KEY,chat_id TEXT,topic_id INTEGER,text TEXT,
                message_id INTEGER,attempts INTEGER DEFAULT 0,retry_at REAL DEFAULT 0);
        """)
        with self.db:
            if 'received_ms' not in {row[1] for row in self.db.execute('PRAGMA table_info(jobs)')}:
                self.db.execute('ALTER TABLE jobs ADD COLUMN received_ms INTEGER')
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
                    "INSERT OR IGNORE INTO jobs(id,target_key,thread_id,topic_id,chat_id,message_id,message_json,error,received_ms) "
                    "VALUES(?,?,?,?,?,?,?,?,?)",
                    (update["update_id"], target["key"] if target else None,
                     target["thread_id"] if target else None, topic_id, str(message["chat"]["id"]),
                     message.get("message_id"), json.dumps(message, ensure_ascii=False), error, now_ms()))
            self.db.execute("UPDATE kv SET value=? WHERE key='offset'",
                            (str(max(self.offset(), update["update_id"] + 1)),))

    def pending(self):
        return self.db.execute("SELECT * FROM jobs WHERE state='pending' ORDER BY id LIMIT 1").fetchone()

    def finish(self, row):
        with self.db:
            if row["error"]:
                message = json.loads(row['message_json'])
                reply_topic = None if command_of(message) in GLOBAL_COMMANDS else row['topic_id']
                self.db.execute("INSERT OR IGNORE INTO notices(id,chat_id,topic_id,text) VALUES(?,?,?,?)",
                                (row["id"], row["chat_id"], reply_topic, row["error"]))
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
        from recent_sessions import RecentSessions
        self.recent = RecentSessions(self.store.db)
        self.recent.bootstrap(self.bridges.values())
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
        lines.append("All 是管理入口；普通消息需选择并确认接收会话。")
        return "\n".join(lines)

    def ingest(self, update):
        if self.picker.creator.is_reply(update):
            self.picker.ingest(update)
            return
        if self.picker.renamer.is_reply(update):
            self.picker.ingest(update)
            return
        if self.picker.guard.ingest(update):
            return
        if self.control.ingest(update):
            return
        if self.picker.ingest(update):
            return
        message = update.get("message", {})
        if not permitted(message, self.access(), self.owner):
            self.store.accept(update, None)
            return
        if any(k in message for k in ("forum_topic_created", "forum_topic_edited", "forum_topic_closed",
                                      "forum_topic_reopened", "general_forum_topic_hidden", "general_forum_topic_unhidden",
                                      "pinned_message")):
            self.store.accept(update, None)
            return
        topic_id = topic_of(message)
        if command_of(message) in {'/help', '/start'}:
            target = self.root_target
        else:
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
            message = json.loads(row["message_json"])
            target.store.accept({"update_id": row["id"]}, message)
            command = command_of(message)
            if not command.startswith('/') or command in {'/status', '/model', '/effort', '/format', '/stop'}:
                received = row['received_ms']
                if received is not None:
                    accessed_at = received / 1000
                else:
                    # Old router jobs have no receipt column. Prefer their source
                    # timestamp, then the durable child receipt, never replay-now.
                    child = target.store.db.execute('SELECT received_ms FROM inbox WHERE id=?', (row['id'],)).fetchone()
                    accessed_at = message.get('date') or ((child[0] or 0) / 1000 if child else 0)
                self.recent.record(row['thread_id'], 'telegram:' + str(row['id']), accessed_at)
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
                                         rich_text=row['rich_text'], topic_id=row['topic_id'])
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
                            "notification_mode": 'completion' if bridge.delivery.enabled else 'all',
                            "preview_style": bridge.delivery.preview_style,
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
        data['main_messages_waiting_confirmation'] = self.picker.guard.pending_count()
        tmp = self.state_dir / "health.json.tmp"
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
        tmp.replace(self.state_dir / "health.json")

    async def health_loop(self):
        while True:
            refresh_health_snapshot(self)
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
        if bridge.delivery.enabled:
            self.spawn(bridge.preview_loop())

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

    def rename_target(self, key, binding, old_name, new_name):
        """Commit a name-only change without rebinding or restarting the session."""
        target = self.targets.get(key)
        bridge = self.bridges.get(key)
        if (not target or not bridge or target.get('topic_missing')
                or {field: target[field] for field in ('key', 'thread_id', 'message_thread_id')} != binding
                or bridge.thread_id != binding['thread_id']
                or target['name'] not in (old_name, new_name)):
            raise ValueError('Session binding or name changed before metadata commit')
        if any(t['key'] != key and t['name'].casefold() == new_name.casefold() for t in self.targets.values()):
            raise ValueError('Another bound session already uses that name')
        updated = {**target, 'name': new_name}
        self.persist_targets({**self.targets, key: updated})
        self.targets[key] = updated
        bridge.target = updated
        self.by_topic[updated['message_thread_id']] = updated
        if key == self.config['root_target']:
            self.root_target = updated
        with self.store.db:
            row = self.store.db.execute('SELECT target_json FROM binding_ops WHERE thread_id=?',
                                        (updated['thread_id'],)).fetchone()
            if row:
                saved = json.loads(row['target_json'])
                saved['name'] = new_name
                self.store.db.execute('UPDATE binding_ops SET target_json=?,updated_at=? WHERE thread_id=?',
                                      (json.dumps(saved, ensure_ascii=False), time.time(), updated['thread_id']))
            for row in self.store.db.execute('SELECT id,payload FROM ui_catalogs').fetchall():
                groups = json.loads(row['payload'])
                changed = False
                for group in groups:
                    for thread in group['threads']:
                        if thread['id'] == updated['thread_id']:
                            thread['name'] = new_name
                            changed = True
                if changed:
                    self.store.db.execute('UPDATE ui_catalogs SET payload=? WHERE id=?',
                                          (json.dumps(groups, ensure_ascii=False), row['id']))
        # Keep name-derived aliases usable immediately, without waiting for the
        # next native snapshot or making history/model requests just to rename.
        from control_center import alias, clean, read_json, write_json
        path = self.state_dir / 'control/local.json'
        snapshot = read_json(path, {'host': self.control.host, 'generated': 0, 'entries': []})
        entry = next((e for e in snapshot.get('entries', []) if e.get('thread_id') == updated['thread_id']), None)
        if entry is None:
            entry = {'host': self.control.host, 'thread_id': updated['thread_id'],
                     'target_key': key, 'topic_id': updated['message_thread_id'], 'status': {}, 'updated': 0}
            snapshot.setdefault('entries', []).append(entry)
        entry['name'] = clean(new_name, 50)
        entry.update(target_key=key, topic_id=updated['message_thread_id'], topic_missing=False)
        entry['aliases'] = list(dict.fromkeys([*(alias(a) for a in updated.get('aliases', [])),
                                              alias(entry['name']), key]))
        write_json(path, snapshot)

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
            commands = [*BOT_COMMANDS, {'command': 'sessions', 'description': '会话列表与添加绑定'},
                        {'command': 'rename', 'description': '修改会话名称，同步话题名称'}, *COMMANDS]
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
                self.spawn(self.picker.home_loop())
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
                refresh_health_snapshot(self)


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
