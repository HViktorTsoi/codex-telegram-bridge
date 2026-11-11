import asyncio
import json
from pathlib import Path
import tempfile
import unittest

from aiohttp import web

from bridge import Bridge, Codex, Store, chunks, now_ms, permitted


class AccessTests(unittest.TestCase):
    def setUp(self):
        self.access = {"dmPolicy": "allowlist", "allowFrom": ["123"]}
        self.message = {"from": {"id": 123}, "chat": {"id": 123, "type": "private"}}

    def test_only_the_existing_owner_private_chat_is_allowed(self):
        self.assertTrue(permitted(self.message, self.access, "123"))
        for message in [
            {"from": {"id": 999}, "chat": {"id": 999, "type": "private"}},
            {"from": {"id": 123}, "chat": {"id": -456, "type": "group"}},
            {"from": {"id": 123}, "chat": {"id": 999, "type": "private"}},
            {"from": {"id": 123, "is_bot": True}, "chat": {"id": 123, "type": "private"}},
        ]:
            self.assertFalse(permitted(message, self.access, "123"))

    def test_revoking_access_and_invalid_config_fail_closed(self):
        for access in [{}, {"dmPolicy": "pairing", "allowFrom": ["123"]},
                       {"dmPolicy": "allowlist", "allowFrom": []}]:
            self.assertFalse(permitted(self.message, access, "123"))

    def test_long_unicode_messages_fit_telegram_and_preserve_text(self):
        text = ("中文🙂\n" * 3000) + "尾"
        parts = list(chunks(text))
        self.assertGreater(len(parts), 1)
        self.assertEqual("".join(parts), text)
        self.assertTrue(all(len(p.encode("utf-16-le")) // 2 <= 3800 for p in parts))


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "state.sqlite")

    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    def test_updates_are_durable_deduplicated_and_offsets_never_regress(self):
        message = {"chat": {"id": 123}, "text": "hello"}
        self.store.accept({"update_id": 20}, message)
        self.store.accept({"update_id": 20}, message)
        self.store.accept({"update_id": 19})
        self.assertEqual(self.store.get("offset"), "21")
        self.assertEqual(self.store.counts()["pending_inputs"], 1)
        self.store.db.close()
        self.store = Store(Path(self.tmp.name) / "state.sqlite")
        self.assertEqual(self.store.pending()["text"], "hello")
        self.assertEqual(self.store.get("offset"), "21")


class RoutingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name)
        self.config = {"thread_id": "thread", "owner_id": "123",
                       "socket_path": str(self.path / "codex.sock"),
                       "telegram_state_dir": str(self.path)}
        (self.path / "access.json").write_text(json.dumps({"dmPolicy": "allowlist", "allowFrom": ["123"]}))
        self.bridge = Bridge(self.config, self.path)
        self.bridge.store.accept({"update_id": 1}, {"chat": {"id": 123}, "text": "hello"})
        self.since = self.bridge.store.pending()["received_ms"]

    async def asyncTearDown(self):
        self.bridge.store.db.close()
        self.tmp.cleanup()

    def test_only_output_after_input_from_its_own_turn_is_forwarded(self):
        self.bridge.store.mark(1, "running", "t1")
        item = {"type": "agentMessage", "id": "a1", "phase": "final_answer", "text": "reply"}
        self.bridge.record_item("another-thread-turn", item, self.since + 1)
        self.bridge.record_item("t1", item, self.since - 1)
        self.bridge.record_item("t1", {"type": "reasoning", "id": "secret", "text": "private"}, self.since + 1)
        self.assertEqual(self.bridge.store.counts()["unsent_messages"], 0)
        self.bridge.record_item("t1", item, self.since + 1)
        self.bridge.record_item("t1", item, self.since + 1)
        self.assertEqual(self.bridge.store.counts()["unsent_messages"], 1)
        self.bridge.finish_turn({"id": "t1", "status": "completed"})
        self.assertEqual(self.bridge.store.counts()["running_inputs"], 0)

    async def test_single_chat_default_still_sends_progress_before_the_turn_finishes(self):
        self.bridge.store.mark(1, 'running', 'active-turn')
        for index, text in enumerate(('第一条单会话进度', '第二条单会话进度')):
            self.bridge.record_item('active-turn', {'type': 'agentMessage', 'id': str(index),
                                    'phase': 'commentary', 'text': text}, self.since + 1)
        calls, sent = [], asyncio.Event()

        class Telegram:
            async def call(self, method, **params):
                calls.append((method, params))
                if len(calls) == 2:
                    sent.set()
                return {'message_id': 100 + len(calls)}

        self.bridge.telegram = Telegram()
        sender = asyncio.create_task(self.bridge.sender_loop())
        try:
            await asyncio.wait_for(sent.wait(), 2)
        finally:
            sender.cancel()
            await asyncio.gather(sender, return_exceptions=True)
        self.assertEqual([params.get('text') or params['rich_message']['markdown'] for _, params in calls],
                         ['第一条单会话进度', '第二条单会话进度'])
        self.assertTrue(all(method in ('sendMessage', 'sendRichMessage') for method, _ in calls))
        self.assertTrue(all('reply_markup' not in params for _, params in calls))
        self.assertEqual(self.bridge.store.counts()['running_inputs'], 1)
        self.assertEqual(self.bridge.store.counts()['unsent_messages'], 0)

    async def test_reconnect_recovers_accepted_input_and_reply_without_reexecution(self):
        self.bridge.store.mark(1, "dispatching")
        since = self.since

        class RecoveredCodex:
            async def turns(self, limit=10):
                return [{"id": "t1", "status": "completed"}]

            async def items(self, turn_id):
                yield {"turnId": turn_id, "item": {"type": "userMessage", "clientId": "telegram-1"}}
                yield {"turnId": turn_id, "completedAtMs": since + 100,
                       "item": {"type": "agentMessage", "id": "a1", "text": "recovered reply"}}

        self.bridge.codex = RecoveredCodex()
        await self.bridge.reconcile(replay=True)
        row = self.bridge.store.db.execute("SELECT * FROM inbox").fetchone()
        self.assertEqual((row["state"], row["turn_id"]), ("done", "t1"))
        self.assertEqual(self.bridge.store.counts()["unsent_messages"], 1)
        await self.bridge.reconcile(replay=True)
        self.assertEqual(self.bridge.store.counts()["unsent_messages"], 1)

    async def test_ambiguous_dispatch_is_not_repeated(self):
        self.bridge.store.mark(1, "dispatching")

        class EmptyCodex:
            async def turns(self, limit=10):
                return []

        self.bridge.codex = EmptyCodex()
        await self.bridge.reconcile(replay=True)
        self.assertIsNone(self.bridge.store.pending())
        row = self.bridge.store.db.execute("SELECT state FROM inbox").fetchone()
        self.assertEqual(row[0], "uncertain")

    async def test_dispatch_preserves_thread_settings(self):
        requests = []

        class StubCodex:
            async def call(self, method, params):
                requests.append((method, params))
                return {"turn": {"id": "active-turn"}}

        self.bridge.codex = StubCodex()
        await self.bridge.dispatch_one(self.bridge.store.pending())
        method, params = requests[0]
        self.assertEqual(method, "turn/start")
        self.assertEqual(set(params), {"threadId", "input", "clientUserMessageId"})
        self.assertEqual(params["threadId"], "thread")
        self.assertEqual(self.bridge.store.running()[0][0], "active-turn")


class SocketTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_unix_websocket_rpc_filters_unrelated_events(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "codex.sock")

            async def handler(request):
                ws = web.WebSocketResponse()
                await ws.prepare(request)
                async for message in ws:
                    data = json.loads(message.data)
                    if "id" not in data:
                        continue
                    if data["method"] == "initialize":
                        await ws.send_json({"id": data["id"], "result": {"userAgent": "test"}})
                    elif data["method"] == "thread/resume":
                        for thread_id in ["unrelated", "wanted"]:
                            await ws.send_json({"method": "item/completed", "params": {
                                "threadId": thread_id, "turnId": "t1", "completedAtMs": now_ms(),
                                "item": {"type": "agentMessage", "id": "a1", "text": "hello"}}})
                        await ws.send_json({"id": data["id"], "result": {"thread": {"id": "wanted"}}})
                return ws

            app = web.Application()
            app.router.add_get("/", handler)
            runner = web.AppRunner(app)
            await runner.setup()
            await web.UnixSite(runner, path).start()
            client = Codex(path, "wanted")
            try:
                response = await client.connect()
                self.assertEqual(response["thread"]["id"], "wanted")
                event = await asyncio.wait_for(client.events.get(), 2)
                self.assertEqual(event["params"]["threadId"], "wanted")
                self.assertTrue(client.events.empty())
            finally:
                await client.close()
                await runner.cleanup()


class SettingsCommandTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        path = Path(self.tmp.name)
        (path / "access.json").write_text(json.dumps({"dmPolicy": "allowlist", "allowFrom": ["123"]}))
        self.bridge = Bridge({"thread_id": "thread", "owner_id": "123",
                              "socket_path": str(path / "codex.sock"),
                              "telegram_state_dir": str(path)}, path)
        self.sequence = 0

        class SettingsCodex:
            def __init__(self):
                self.current = {"model": "model-a", "reasoningEffort": "max",
                                "thread": {"name": "Same conversation", "status": {"type": "active"}}}
                self.updates = []
                self.apply = True
                self.catalog = [
                    {"id": "model-a", "model": "model-a", "defaultReasoningEffort": "low",
                     "supportedReasoningEfforts": [{"reasoningEffort": x} for x in ["low", "high", "max"]]},
                    {"id": "alias-b", "model": "model-b", "defaultReasoningEffort": "medium",
                     "supportedReasoningEfforts": [{"reasoningEffort": x} for x in ["low", "medium"]]},
                ]

            async def settings(self):
                return dict(self.current)

            async def models(self):
                return self.catalog

            async def call(self, method, params):
                if method != "thread/settings/update":
                    raise AssertionError("Settings commands must not start generation: " + method)
                self.updates.append(params)
                if self.apply:
                    if "model" in params:
                        self.current["model"] = params["model"]
                    if "effort" in params:
                        self.current["reasoningEffort"] = params["effort"]
                return {}

        self.codex = SettingsCodex()
        self.bridge.codex = self.codex

    async def asyncTearDown(self):
        self.bridge.store.db.close()
        self.tmp.cleanup()

    async def command(self, text):
        self.sequence += 1
        self.bridge.store.accept({"update_id": self.sequence}, {"chat": {"id": 123}, "text": text})
        await self.bridge.dispatch_one(self.bridge.store.pending())
        self.assertIsNone(self.bridge.store.pending())
        rows = self.bridge.store.db.execute("SELECT text FROM outbox WHERE key LIKE ? ORDER BY rowid",
                                            (f"input:{self.sequence}:reply:%",)).fetchall()
        return "".join(row[0] for row in rows)

    async def test_queries_show_real_settings_and_catalog_without_starting_generation(self):
        for text in ["/model", "/effort", "/status"]:
            with self.subTest(command=text):
                reply = await self.command(text)
                self.assertIn("model-a", reply)
                self.assertIn("max", reply)
        self.assertEqual(self.codex.updates, [])
        self.assertEqual(self.bridge.store.counts()["running_inputs"], 0)

    async def test_model_change_uses_supported_default_when_current_effort_is_incompatible(self):
        reply = await self.command("/model alias-b")
        self.assertEqual(self.codex.updates, [{"threadId": "thread", "model": "model-b", "effort": "medium"}])
        self.assertIn("model-b", reply)
        self.assertIn("默认 effort：medium", reply)
        self.assertIn("下一轮", reply)

    async def test_model_change_preserves_a_compatible_effort(self):
        self.codex.current["reasoningEffort"] = "low"
        await self.command("/model model-b")
        self.assertEqual(self.codex.updates[0]["effort"], "low")

    async def test_effort_changes_only_the_requested_thread_setting(self):
        reply = await self.command("/effort@my_bot HIGH")
        self.assertEqual(self.codex.updates, [{"threadId": "thread", "effort": "high"}])
        self.assertIn("effort：high", reply)
        self.assertIn("model：model-a", reply)

    async def test_invalid_models_efforts_and_syntax_are_rejected_without_mutation(self):
        for text in ["/model missing-model", "/effort imaginary", "/effort high extra", "/model model-b extra"]:
            with self.subTest(command=text):
                reply = await self.command(text)
                self.assertTrue(any(word in reply for word in ["不可用", "不支持", "用法"]))
        self.assertEqual(self.codex.updates, [])
        self.assertEqual(self.codex.current["model"], "model-a")
        self.assertEqual(self.codex.current["reasoningEffort"], "max")

    async def test_effort_choices_come_from_catalog_rather_than_a_fixed_enum(self):
        self.codex.catalog[0]["supportedReasoningEfforts"].append({"reasoningEffort": "careful"})
        reply = await self.command("/effort careful")
        self.assertIn("effort：careful", reply)

    async def test_effort_preserves_catalog_case_and_accepts_a_unique_case_insensitive_match(self):
        self.codex.catalog[0]['supportedReasoningEfforts'].append({'reasoningEffort': 'CarefulV2'})
        await self.command('/effort carefulv2')
        self.assertEqual(self.codex.updates[-1], {'threadId': 'thread', 'effort': 'CarefulV2'})
        await self.command('/effort CarefulV2')
        self.assertEqual(self.codex.updates[-1]['effort'], 'CarefulV2')

    async def test_success_is_not_claimed_when_settings_readback_does_not_match(self):
        self.codex.apply = False
        reply = await self.command("/effort high")
        self.assertIn("与目标不同", reply)
        self.assertIn("effort：max", reply)
        self.assertNotIn("已更新此会话参数", reply)

    async def test_unknown_slash_command_is_handled_without_model_generation(self):
        reply = await self.command("/unknown")
        self.assertIn("/help", reply)
        self.assertEqual(self.codex.updates, [])

    async def test_reply_format_commands_persist_without_model_or_permission_changes(self):
        reply = await self.command('/format')
        self.assertIn('Rich Markdown', reply)
        reply = await self.command('/format plain')
        self.assertEqual(self.bridge.store.get('output_format'), 'plain')
        self.assertIn('纯文本', reply)
        await self.command('/format invalid')
        self.assertEqual(self.bridge.store.get('output_format'), 'plain')
        await self.command('/format RICH')
        self.assertEqual(self.bridge.store.get('output_format'), 'rich')
        self.assertEqual(self.codex.updates, [])

    async def test_revoked_owner_cannot_change_settings(self):
        path = Path(self.tmp.name) / "access.json"
        path.write_text(json.dumps({"dmPolicy": "allowlist", "allowFrom": []}))
        reply = await self.command("/effort high")
        self.assertEqual(reply, "")
        self.assertEqual(self.codex.updates, [])


if __name__ == "__main__":
    unittest.main()
