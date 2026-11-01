#!/usr/bin/env python3
"""Relay one allowlisted Telegram DM to an existing local Codex thread."""

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
from attachments import AttachmentError, inputs_for, payload_from
from message_format import message_parts, send_output

LOG = logging.getLogger("codex_telegram")
DEFAULT_STATE = Path.home() / ".local/state/codex-telegram"
BOT_COMMANDS = [
    {"command": "start", "description": "开始使用 Codex 对话"},
    {"command": "model", "description": "查看或切换模型，例如 /model gpt-6.1-sol"},
    {"command": "effort", "description": "查看或修改推理强度，例如 /effort high"},
    {"command": "format", "description": "回复格式：/format rich 或 /format plain"},
    {"command": "status", "description": "查看连接状态、模型和推理强度"},
    {"command": "stop", "description": "停止当前任务"},
    {"command": "help", "description": "查看命令说明"},
]


def now_ms():
    return int(time.time() * 1000)


def chunks(text, limit=3800):
    """Keep every chunk below Telegram's limit, including UTF-16 surrogates."""
    part, size = [], 0
    for char in text:
        width = 2 if ord(char) > 0xFFFF else 1
        if size + width > limit:
            yield "".join(part)
            part, size = [], 0
        part.append(char)
        size += width
    if part:
        yield "".join(part)


def permitted(message, access, owner):
    sender, chat = message.get("from", {}), message.get("chat", {})
    return (
        access.get("dmPolicy") == "allowlist"
        and str(owner) in {str(x) for x in access.get("allowFrom", [])}
        and str(sender.get("id")) == str(owner)
        and str(chat.get("id")) == str(owner)
        and chat.get("type") == "private"
        and not sender.get("is_bot", False)
    )


class Store:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS kv(key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE IF NOT EXISTS inbox(
                id INTEGER PRIMARY KEY, chat_id TEXT, text TEXT, received_ms INTEGER,
                state TEXT DEFAULT 'pending', turn_id TEXT, client_id TEXT);
            CREATE TABLE IF NOT EXISTS outbox(
                key TEXT PRIMARY KEY, chat_id TEXT, text TEXT,
                message_id INTEGER, attempts INTEGER DEFAULT 0,
                retry_at REAL DEFAULT 0);
        """)
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(inbox)")}
        with self.db:
            for name in ["message_json", "media_group_id"]:
                if name not in columns:
                    self.db.execute(f"ALTER TABLE inbox ADD COLUMN {name} TEXT")
            if 'rich_text' not in {row[1] for row in self.db.execute('PRAGMA table_info(outbox)')}:
                self.db.execute('ALTER TABLE outbox ADD COLUMN rich_text TEXT')

    def get(self, key, default=None):
        row = self.db.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def set(self, key, value):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO kv VALUES(?,?)", (key, str(value)))

    def accept(self, update, message=None):
        """Persist input and the next poll offset in one transaction."""
        with self.db:
            if message is not None:
                self.db.execute(
                    "INSERT OR IGNORE INTO inbox(id,chat_id,text,received_ms,client_id,message_json,media_group_id) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (update["update_id"], str(message["chat"]["id"]),
                     message.get("text") or message.get("caption", ""), now_ms(),
                     "telegram-" + str(update["update_id"]), json.dumps(payload_from(message), ensure_ascii=False),
                     str(message["media_group_id"]) if message.get("media_group_id") else None),
                )
            offset = max(int(self.get("offset", 0)), update["update_id"] + 1)
            self.db.execute("INSERT OR REPLACE INTO kv VALUES('offset',?)", (str(offset),))

    def pending(self):
        row = self.db.execute("SELECT * FROM inbox WHERE state='pending' ORDER BY received_ms,id LIMIT 1").fetchone()
        if row and row["media_group_id"]:
            last = self.db.execute("SELECT MAX(received_ms) FROM inbox WHERE media_group_id=? AND state='pending'",
                                   (row["media_group_id"],)).fetchone()[0]
            if now_ms() - last < 2000:
                return None
        return row

    def batch(self, row):
        if not row["media_group_id"]:
            return [row]
        return self.db.execute("SELECT * FROM inbox WHERE media_group_id=? AND state='pending' ORDER BY id",
                               (row["media_group_id"],)).fetchall()

    def mark_batch(self, rows, state, turn_id=None):
        with self.db:
            for row in rows:
                self.db.execute("UPDATE inbox SET state=?,turn_id=COALESCE(?,turn_id),client_id=? WHERE id=?",
                                (state, turn_id, rows[0]["client_id"], row["id"]))

    def mark(self, update_id, state, turn_id=None):
        with self.db:
            self.db.execute(
                "UPDATE inbox SET state=?,turn_id=COALESCE(?,turn_id) WHERE id=?",
                (state, turn_id, update_id),
            )

    def enqueue(self, key, chat_id, text):
        with self.db:
            if self.db.execute('SELECT 1 FROM outbox WHERE key=?', (f'{key}:0',)).fetchone():
                return  # Preserve already-queued legacy chunk boundaries during recovery.
            for i, (part, rich_text) in enumerate(self.output_parts(key, text)):
                self.db.execute(
                    "INSERT OR IGNORE INTO outbox(key,chat_id,text,rich_text) VALUES(?,?,?,?)",
                    (f"{key}:{i}", str(chat_id), part, rich_text),
                )

    def output_parts(self, key, text):
        return message_parts(text, rich=key.startswith('turn:') and self.get('output_format', 'rich') == 'rich')

    def routes(self, turn_id):
        return self.db.execute(
            "SELECT chat_id,MIN(received_ms) AS since_ms FROM inbox WHERE turn_id=? GROUP BY chat_id",
            (turn_id,),
        ).fetchall()

    def running(self):
        return self.db.execute(
            "SELECT DISTINCT turn_id FROM inbox WHERE state='running' AND turn_id IS NOT NULL"
        ).fetchall()

    def counts(self):
        return {
            "pending_inputs": self.db.execute("SELECT COUNT(*) FROM inbox WHERE state='pending'").fetchone()[0],
            "running_inputs": self.db.execute("SELECT COUNT(*) FROM inbox WHERE state='running'").fetchone()[0],
            "unsent_messages": self.db.execute("SELECT COUNT(*) FROM outbox WHERE message_id IS NULL").fetchone()[0],
        }


class RpcError(Exception):
    pass


class Codex:
    def __init__(self, socket_path, thread_id):
        self.socket_path, self.thread_id = socket_path, thread_id
        self.pending, self.sequence = {}, 0
        self.events = asyncio.Queue()
        self.session = self.ws = self.reader = None

    async def connect(self):
        self.session = aiohttp.ClientSession(
            connector=aiohttp.UnixConnector(path=self.socket_path),
            timeout=aiohttp.ClientTimeout(total=30),
        )
        self.ws = await self.session.ws_connect("http://localhost/", heartbeat=20, max_msg_size=32 * 1024 * 1024)
        self.reader = asyncio.create_task(self._read())
        await self.call("initialize", {
            "clientInfo": {"name": "codex_telegram", "title": "Telegram", "version": "0.1.0"},
            "capabilities": {"experimentalApi": True},
        })
        await self.ws.send_json({"method": "initialized", "params": {}})
        return await self.call("thread/resume", {"threadId": self.thread_id, "excludeTurns": True})

    async def _read(self):
        try:
            async for message in self.ws:
                if message.type != aiohttp.WSMsgType.TEXT:
                    continue
                data = json.loads(message.data)
                if "id" in data and "method" not in data:
                    future = self.pending.get(data["id"])
                    if future and not future.done():
                        if "error" in data:
                            future.set_exception(RpcError(data["error"].get("message", "RPC error")))
                        else:
                            future.set_result(data.get("result", {}))
                elif "method" in data:
                    params = data.get("params", {})
                    if params.get("threadId") == self.thread_id:
                        method = data["method"]
                        if method in {"item/completed", "turn/completed"} or "id" in data:
                            self.events.put_nowait(data)
        finally:
            for future in list(self.pending.values()):
                if not future.done():
                    future.set_exception(ConnectionError("Codex socket disconnected"))

    async def call(self, method, params):
        if self.ws is None or self.ws.closed:
            raise ConnectionError("Codex is disconnected")
        self.sequence += 1
        request_id = self.sequence
        future = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        try:
            await self.ws.send_json({"id": request_id, "method": method, "params": params})
            return await asyncio.wait_for(future, 30)
        finally:
            self.pending.pop(request_id, None)

    async def turns(self, limit=10):
        return (await self.call("thread/turns/list", {
            "threadId": self.thread_id, "limit": limit, "sortDirection": "desc", "itemsView": "notLoaded",
        }))["data"]

    async def settings(self):
        # Reading a loaded thread this way also keeps this client subscribed.
        return await self.call("thread/resume", {"threadId": self.thread_id, "excludeTurns": True})

    async def models(self):
        models, cursor = [], None
        while True:
            params = {"limit": 100, "includeHidden": False}
            if cursor:
                params["cursor"] = cursor
            result = await self.call("model/list", params)
            models.extend(model for model in result["data"] if not model.get("hidden", False))
            cursor = result.get("nextCursor")
            if not cursor:
                return models

    async def items(self, turn_id):
        cursor = None
        while True:
            params = {"threadId": self.thread_id, "turnId": turn_id, "limit": 100, "sortDirection": "asc"}
            if cursor:
                params["cursor"] = cursor
            result = await self.call("thread/items/list", params)
            for entry in result["data"]:
                yield entry
            cursor = result.get("nextCursor")
            if not cursor:
                break

    async def close(self):
        if self.ws:
            await self.ws.close()
        if self.reader:
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await self.reader
        if self.session:
            await self.session.close()


class TelegramError(Exception):
    def __init__(self, method, code, description, retry_after=3):
        self.code, self.retry_after = code, retry_after
        super().__init__(f"{method}: {code} {description}")


class Telegram:
    def __init__(self, token, session):
        self.token, self.session = token, session

    async def call(self, method, **params):
        try:
            async with self.session.post(
                f"https://api.telegram.org/bot{self.token}/{method}", json=params,
                timeout=aiohttp.ClientTimeout(total=45),
            ) as response:
                data = await response.json()
        except (aiohttp.ClientError, asyncio.TimeoutError) as error:
            # aiohttp exception strings can contain the token-bearing URL.
            raise ConnectionError(f"Telegram {method}: {type(error).__name__}") from None
        if not data.get("ok"):
            raise TelegramError(method, data.get("error_code", response.status),
                                data.get("description", "API error").replace(self.token, "[redacted]"),
                                data.get("parameters", {}).get("retry_after", 3))
        return data["result"]


class Bridge:
    def __init__(self, config, state_dir):
        self.config, self.state_dir = config, Path(state_dir)
        self.thread_id, self.owner = config["thread_id"], str(config["owner_id"])
        self.telegram_dir = Path(config["telegram_state_dir"])
        self.store = Store(self.state_dir / "bridge.sqlite")
        if self.store.get('output_format') is None:
            self.store.set('output_format', config.get('output_format', 'rich'))
        self.codex = Codex(config["socket_path"], self.thread_id)
        self.ready, self.dispatch_lock = asyncio.Event(), asyncio.Lock()
        self.last_error, self.username, self.last_poll_at = None, None, None
        self.telegram = None

    def access(self):
        try:
            return json.loads((self.telegram_dir / "access.json").read_text())
        except (OSError, ValueError):
            return {}

    def owner_allowed(self):
        return permitted({"from": {"id": self.owner}, "chat": {"id": self.owner, "type": "private"}}, self.access(), self.owner)

    def token(self):
        content = (self.telegram_dir / ".env").read_text()
        match = re.search(r"^\s*TELEGRAM_BOT_TOKEN\s*=\s*(.*)$", content, re.MULTILINE)
        if not match:
            raise RuntimeError("Telegram token is missing")
        return match[1].strip().strip("\"'")

    def record_item(self, turn_id, item, completed_ms=None, replay=False):
        if item.get("type") != "agentMessage" or not item.get("text", "").strip():
            return
        for route in self.store.routes(turn_id):
            if completed_ms is not None and completed_ms < route["since_ms"]:
                continue
            if replay and completed_ms is None:
                continue  # Do not replay old output with an unknown timestamp.
            self.store.enqueue(f"turn:{turn_id}:{item['id']}", route["chat_id"], item["text"])

    def finish_turn(self, turn):
        status = turn.get("status")
        if status in {"failed", "interrupted"}:
            text = "本次任务已停止。" if status == "interrupted" else "本次任务执行失败，请在 Codex 会话中查看详情。"
            for route in self.store.routes(turn["id"]):
                self.store.enqueue(f"turn:{turn['id']}:status", route["chat_id"], text)
        with self.store.db:
            self.store.db.execute("UPDATE inbox SET state='done' WHERE turn_id=?", (turn["id"],))

    async def reconcile(self, replay=False):
        """Recover outputs after reconnect without re-executing accepted input."""
        turns = {turn["id"]: turn for turn in await self.codex.turns(limit=30)}
        for row in self.store.db.execute("SELECT * FROM inbox WHERE state='dispatching'").fetchall():
            found = None
            for turn_id in list(turns)[:5]:
                async for entry in self.codex.items(turn_id):
                    if entry["item"].get("clientId") == row["client_id"]:
                        found = turn_id
                        break
                if found:
                    break
            if found:
                self.store.mark(row["id"], "running", found)
            else:
                # An interrupted RPC can have committed server-side. Never blindly repeat an action.
                self.store.mark(row["id"], "uncertain")
                self.store.enqueue(f"input:{row['id']}:uncertain", row["chat_id"],
                                   "连接曾中断，尚未确认上一条消息是否送达 Codex。请先检查当前会话，避免重复执行。")
        for row in self.store.running():
            turn = turns.get(row["turn_id"])
            if replay or (turn and turn["status"] != "inProgress"):
                async for entry in self.codex.items(row["turn_id"]):
                    self.record_item(row["turn_id"], entry["item"], entry.get("completedAtMs"), replay=True)
            if turn and turn["status"] != "inProgress":
                self.finish_turn(turn)
        return list(turns.values())

    async def connection_loop(self, notify):
        while True:
            try:
                result = await self.codex.connect()
                async with self.dispatch_lock:
                    await self.reconcile(replay=True)
                self.ready.set()
                self.last_error = None
                LOG.info("Connected to existing Codex thread %s", self.thread_id)
                if notify:
                    self.store.enqueue("setup:connected", self.owner,
                        "Codex 文字对话已接入。这里连接的是你刚才配置 Telegram 的同一条 Codex 会话，可以直接继续聊天。\n\n"
                        "请回复一条“Telegram 连接测试”，验证收发。\n/status 查看状态；/stop 停止当前任务；/help 查看说明。")
                    notify = False
                await self.codex.reader
                raise ConnectionError("Codex socket closed")
            except (OSError, RpcError, asyncio.TimeoutError, aiohttp.ClientError, ValueError) as error:
                self.last_error = f"Codex: {type(error).__name__}"
                LOG.warning("Codex connection retry: %s", type(error).__name__)
            finally:
                self.ready.clear()
                await self.codex.close()
            await asyncio.sleep(3)

    async def poll_loop(self):
        while True:
            try:
                updates = await self.telegram.call("getUpdates", offset=int(self.store.get("offset", 0)),
                                                   timeout=25, allowed_updates=["message"])
                self.last_poll_at = time.time()
                if self.last_error and self.last_error.startswith("Telegram"):
                    self.last_error = None
                access = self.access()
                for update in updates:
                    message = update.get("message", {})
                    accepted = message if permitted(message, access, self.owner) else None
                    self.store.accept(update, accepted)
                    if accepted:
                        LOG.info("Accepted Telegram update %s", update["update_id"])
            except TelegramError as error:
                if error.code in {401, 409}:
                    LOG.error("Telegram polling stopped: %s", error)
                    raise  # Do not compete with another process using this bot.
                self.last_error = str(error)
                await asyncio.sleep(max(3, error.retry_after))
            except (ConnectionError, ValueError) as error:
                self.last_error = f"Telegram poll: {type(error).__name__}"
                LOG.warning("Telegram poll retry: %s", type(error).__name__)
                await asyncio.sleep(3)

    @staticmethod
    def model_id(model):
        return model.get("model") or model["id"]

    @staticmethod
    def efforts(model):
        return [entry["reasoningEffort"] for entry in model.get("supportedReasoningEfforts", [])]

    @staticmethod
    def settings_text(settings):
        return f"model：{settings.get('model') or '默认'}\neffort：{settings.get('reasoningEffort') or '默认'}"

    async def model_command(self, command, argument):
        """Handle settings commands without starting a model turn."""
        settings = await self.codex.settings()
        models = await self.codex.models()
        lookup = {str(key).lower(): model for model in models
                  for key in [model.get("id"), model.get("model")] if key}
        current = lookup.get(str(settings.get("model", "")).lower())
        if not argument:
            reply = "当前会话参数\n" + self.settings_text(settings)
            if command == "/model":
                lines = [f"{self.model_id(model)}（{', '.join(self.efforts(model)) or '默认强度'}）"
                         for model in models]
                reply += "\n\n可选模型及 effort：\n" + "\n".join(lines)
                reply += "\n\n切换模型：/model <模型 ID>\n修改强度：/effort <级别>"
            else:
                choices = self.efforts(current) if current else []
                reply += "\n\n当前模型可选 effort：" + (", ".join(choices) or "请先用 /model 选择可用模型")
                reply += "\n用法：/effort <级别>"
            return reply
        if len(argument.split()) != 1:
            return f"用法：{command} <{'模型 ID' if command == '/model' else '级别'}>。每次只设置一个参数。"

        argument = argument.lower()
        note = ""
        if command == "/model":
            selected = lookup.get(argument)
            if selected is None:
                return f"模型不可用：{argument}\n发送 /model 查看当前可选模型。"
            choices = self.efforts(selected)
            effort = settings.get("reasoningEffort")
            if effort not in choices:
                effort = selected.get("defaultReasoningEffort")
                if effort:
                    note = f"\n该模型已使用支持的默认 effort：{effort}。"
            changes = {"model": self.model_id(selected)}
            if effort:
                changes["effort"] = effort
        else:
            if current is None:
                return "当前模型不在可选列表中，请先发送 /model 查看可用模型。"
            choices = self.efforts(current)
            if argument not in choices:
                return f"当前模型不支持 effort：{argument}\n可选值：{', '.join(choices) or '默认'}"
            changes = {"effort": argument}

        await self.codex.call("thread/settings/update", {"threadId": self.thread_id, **changes})
        actual = await self.codex.settings()
        expected = {"model": changes.get("model", settings.get("model")),
                    "reasoningEffort": changes.get("effort", settings.get("reasoningEffort"))}
        if any(actual.get(key) != value for key, value in expected.items()):
            return "设置请求已提交，但服务回读的参数与目标不同。\n当前参数：\n" + self.settings_text(actual)
        return "已更新此会话参数，从下一轮开始生效。\n" + self.settings_text(actual) + note

    async def dispatch_one(self, row):
        update_id, text, chat_id = row["id"], row["text"], row["chat_id"]
        batch = self.store.batch(row)
        if not self.owner_allowed():
            self.store.mark_batch(batch, "denied")
            return
        payload = json.loads(row["message_json"] or "{}")
        content = bool(payload)
        parts = text.strip().split(maxsplit=1)
        command = parts[0].split("@")[0].lower() if parts and not content else ""
        argument = parts[1].strip() if len(parts) > 1 else ""
        reply = None
        if not text and not content:
            reply = "此消息暂未包含可读取内容。支持文字、图片、文件、位置和联系人；语音与视频可作为附件接收。"
        elif command in {"/start", "/help"}:
            reply = ("这里已连接你现有的 Codex 会话，直接发送文字即可继续对话。\n"
                     "/model 查看当前模型和可选模型\n/model <模型 ID> 切换模型\n"
                     "/effort 查看当前强度和可选级别\n/effort <级别> 修改推理强度\n"
                     "/format rich 开启 Markdown 排版；/format plain 使用纯文本\n"
                     "/status 查看状态、model 和 effort\n/stop 停止当前任务\n/help 查看说明\n\n"
                     "支持文字、图片、文件、位置和联系人；相册会合并发送。附件目前上限20MB。\n"
                     "语音和视频可接收为文件，尚未自动转写或提取视频帧。\n"
                     "参数修改从下一轮开始生效，沿用此会话的上下文。\n"
                     "会话忙碌时，新消息会作为补充要求加入当前任务。电脑和 Codex 服务运行期间可用。")
        elif command in {"/model", "/effort"}:
            reply = await self.model_command(command, argument)
        elif command == '/format':
            if argument and argument.lower() not in {'rich', 'plain'}:
                reply = '用法：/format rich 或 /format plain。'
            else:
                if argument:
                    self.store.set('output_format', argument.lower())
                    if argument.lower() == 'rich' and self.telegram is not None:
                        self.telegram.rich_supported = None
                mode = self.store.get('output_format', 'rich')
                reply = ('当前回复格式：' + ('Rich Markdown' if mode == 'rich' else '纯文本') +
                         '\n对之后生成的回复生效；格式解析失败时保留原文发送。\n/format rich 开启排版；/format plain 使用纯文本。')
        elif command == "/status":
            settings = await self.codex.settings()
            status = settings["thread"]
            reply = (f"Telegram ↔ Codex 已连接\n会话：{status.get('name') or self.thread_id}\n"
                     f"状态：{status['status']['type']}\n" + self.settings_text(settings) + "\n支持文字、图片和文件；附件上限20MB。")
        elif command == "/stop":
            turns = await self.codex.turns(limit=1)
            if turns and turns[0]["status"] == "inProgress":
                await self.codex.call("turn/interrupt", {"threadId": self.thread_id, "turnId": turns[0]["id"]})
                reply = "已请求停止当前任务。"
            else:
                reply = "当前没有正在执行的任务。"
        elif command.startswith("/"):
            reply = "未识别的命令。发送 /help 查看支持的命令。"
        if reply is not None:
            self.store.enqueue(f"input:{update_id}:reply", chat_id, reply)
            self.store.mark_batch(batch, "done")
            return
        try:
            user_input = await inputs_for(batch, self.telegram, self.state_dir / "inbox")
        except AttachmentError as error:
            self.store.enqueue(f"input:{update_id}:attachment_error", chat_id, str(error))
            self.store.mark_batch(batch, "failed")
            return
        if not self.owner_allowed():
            self.store.mark_batch(batch, "denied")
            return
        self.store.mark_batch(batch, "dispatching")
        params = {"threadId": self.thread_id,
                  "input": user_input,
                  "clientUserMessageId": row["client_id"]}
        # turn/start also accepts input while a turn is active; no settings are overridden.
        result = await self.codex.call("turn/start", params)
        turn_id = result["turn"]["id"]
        self.store.mark_batch(batch, "running", turn_id)
        LOG.info("Delivered update %s to Codex turn %s", update_id, turn_id)

    async def input_loop(self):
        while True:
            await self.ready.wait()
            row = self.store.pending()
            if row is None:
                await asyncio.sleep(0.3)
                continue
            try:
                async with self.dispatch_lock:
                    await self.dispatch_one(row)
            except (ConnectionError, asyncio.TimeoutError):
                # Reconnect reconciliation resolves any ambiguous delivery by clientId.
                if self.codex.ws and not self.codex.ws.closed:
                    await self.codex.ws.close()
                await asyncio.sleep(1)
            except RpcError as error:
                LOG.warning("Input rejected by Codex (%s)", type(error).__name__)
                with self.store.db:
                    self.store.db.execute("UPDATE inbox SET state='failed' WHERE client_id=? AND state IN ('pending','dispatching')",
                                          (row["client_id"],))
                self.store.enqueue(f"input:{row['id']}:failed", row["chat_id"], "Codex 未接受这条消息，请检查会话状态后重试。")

    async def event_loop(self):
        while True:
            data = await self.codex.events.get()
            async with self.dispatch_lock:
                params = data["params"]
                if data["method"] == "item/completed":
                    self.record_item(params["turnId"], params["item"], params.get("completedAtMs"))
                elif data["method"] == "turn/completed":
                    self.finish_turn(params["turn"])
                elif "id" in data:
                    # The original Codex client owns approvals/tool callbacks. Never auto-approve.
                    LOG.info("Codex client request: %s", data["method"])
                    if self.store.running():
                        self.store.enqueue(f"request:{data['id']}", self.owner,
                            "Codex 需要在原会话中处理一个交互请求，请返回 Codex 查看。")

    async def sender_loop(self):
        while True:
            row = self.store.db.execute(
                "SELECT * FROM outbox WHERE message_id IS NULL ORDER BY rowid LIMIT 1"
            ).fetchone()
            if row is None or row["retry_at"] > time.time() or not self.owner_allowed():
                await asyncio.sleep(0.5)
                continue
            try:
                sent = await send_output(self.telegram, chat_id=row['chat_id'], text=row['text'], rich_text=row['rich_text'])
                with self.store.db:
                    self.store.db.execute("UPDATE outbox SET message_id=? WHERE key=?", (sent["message_id"], row["key"]))
                LOG.info("Sent Telegram message %s (%s)", sent["message_id"], row["key"])
            except (TelegramError, ConnectionError) as error:
                if isinstance(error, TelegramError) and error.code in {400, 403}:
                    with self.store.db:
                        self.store.db.execute("UPDATE outbox SET message_id=-1 WHERE key=?", (row["key"],))
                    LOG.error("Telegram delivery failed: %s", error)
                else:
                    delay = min(60, 2 ** min(row["attempts"] + 1, 6))
                    if isinstance(error, TelegramError):
                        delay = max(delay, error.retry_after)
                    with self.store.db:
                        self.store.db.execute("UPDATE outbox SET attempts=attempts+1,retry_at=? WHERE key=?",
                                              (time.time() + delay, row["key"]))
                    LOG.warning("Telegram delivery retry: %s", type(error).__name__)
            await asyncio.sleep(0.4)

    async def maintenance_loop(self):
        tick = 0
        while True:
            if self.ready.is_set():
                try:
                    if tick % 3 == 0:
                        async with self.dispatch_lock:
                            await self.reconcile()
                    if self.store.running() and self.owner_allowed():
                        await self.telegram.call("sendChatAction", chat_id=self.owner, action="typing")
                except (ConnectionError, RpcError, TelegramError, asyncio.TimeoutError):
                    pass
            self.write_health()
            tick += 1
            await asyncio.sleep(4)

    def write_health(self):
        data = {"pid": os.getpid(), "updated_at": time.time(), "connected": self.ready.is_set(),
                "thread_id": self.thread_id, "bot_username": self.username,
                "last_poll_at": self.last_poll_at, "last_error": self.last_error, **self.store.counts()}
        tmp = self.state_dir / "health.json.tmp"
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
        tmp.replace(self.state_dir / "health.json")

    async def run(self, notify=False):
        if not self.owner_allowed():
            raise RuntimeError("Configured owner is not in the Telegram allowlist")
        async with aiohttp.ClientSession() as session:
            self.telegram = Telegram(self.token(), session)
            me = await self.telegram.call("getMe")
            self.username = me["username"]
            expected = self.config.get("expected_bot_username")
            if expected and self.username.lower() != expected.lstrip("@").lower():
                raise RuntimeError("Bot identity does not match expected_bot_username; no polling started")
            webhook = await self.telegram.call("getWebhookInfo")
            if webhook.get("url"):
                raise RuntimeError("Bot already has a webhook; leaving it unchanged")
            try:
                scope = {"type": "chat", "chat_id": int(self.owner)}
                registered = await self.telegram.call("getMyCommands", scope=scope)
                if registered != BOT_COMMANDS:
                    await self.telegram.call("setMyCommands", scope=scope, commands=BOT_COMMANDS)
                    LOG.info("Updated Telegram commands for the allowlisted private chat")
            except (TelegramError, ConnectionError):
                LOG.warning("Command menu registration failed; typed commands remain available")
            tasks = [asyncio.create_task(coro) for coro in (
                self.connection_loop(notify), self.poll_loop(), self.input_loop(),
                self.event_loop(), self.sender_loop(), self.maintenance_loop(),
            )]
            try:
                await asyncio.gather(*tasks)
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                await self.codex.close()
                self.ready.clear()
                self.write_health()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["run", "status"])
    parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE)
    parser.add_argument("--notify", action="store_true")
    args = parser.parse_args()
    if args.action == "status":
        print((args.state_dir / "health.json").read_text())
        return
    os.umask(0o077)
    args.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock = (args.state_dir / "bridge.lock").open("w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit("The Telegram bridge is already running")
    config = json.loads((args.state_dir / "config.json").read_text())
    if config.get("setup_required") or not config.get("thread_id"):
        raise SystemExit("Telegram setup is incomplete: bind a local Codex thread and clear setup_required before starting")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    bridge = Bridge(config, args.state_dir)

    async def serve():
        task = asyncio.create_task(bridge.run(args.notify))
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            loop.add_signal_handler(sig, task.cancel)
        with contextlib.suppress(asyncio.CancelledError):
            await task

    try:
        asyncio.run(serve())
    except (RuntimeError, TelegramError) as error:
        LOG.error("Bridge stopped: %s", error)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
