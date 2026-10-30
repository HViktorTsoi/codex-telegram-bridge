import asyncio
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import aiohttp
from aiohttp import web

from attachments import AttachmentError, MAX_BYTES, download, inputs_for, media_items
from bridge import Bridge, Store, TelegramError


class PayloadTests(unittest.TestCase):
    def test_best_photo_is_selected(self):
        items = media_items({"photo": [
            {"file_id": "small", "width": 90, "height": 90},
            {"file_id": "large", "width": 1200, "height": 900}]})
        self.assertEqual(items[0]["file_id"], "large")

    def test_existing_database_migrates_without_losing_pending_text(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "old.sqlite"
            db = sqlite3.connect(p)
            db.execute("CREATE TABLE inbox(id INTEGER PRIMARY KEY, chat_id TEXT, text TEXT, received_ms INTEGER, state TEXT, turn_id TEXT, client_id TEXT)")
            db.execute("INSERT INTO inbox VALUES(1,'123','old message',1,'pending',NULL,'telegram-1')")
            db.commit()
            db.close()
            store = Store(p)
            self.assertEqual(store.pending()["text"], "old message")
            self.assertIsNone(store.pending()["message_json"])
            store.db.close()

    def test_album_is_batched_and_caption_persists_across_restart(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "state.sqlite"
            store = Store(p)
            for i in [1, 2]:
                store.accept({"update_id": i}, {"chat": {"id": 123}, "media_group_id": "album",
                    "caption": "compare these" if i == 1 else "", "photo": [{"file_id": str(i), "width": 10, "height": 10}]})
            self.assertIsNone(store.pending())
            store.db.execute("UPDATE inbox SET received_ms=received_ms-3000")
            store.db.commit()
            store.db.close()
            store = Store(p)
            rows = store.batch(store.pending())
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[0]["text"], "compare these")
            self.assertEqual(json.loads(rows[1]["message_json"])["photo"][0]["file_id"], "2")
            store.mark_batch(rows, "dispatching")
            self.assertEqual(len({r[0] for r in store.db.execute("SELECT client_id FROM inbox")}), 1)
            store.db.close()


class DownloadTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.directory = Path(self.tmp.name) / "inbox"
        self.body = b"document content"
        self.status = 200
        self.chunked = False
        self.requests = []

        async def file_handler(request):
            self.requests.append(request.path)
            if self.chunked:
                response = web.StreamResponse(status=self.status)
                await response.prepare(request)
                await response.write(self.body)
                await response.write_eof()
                return response
            return web.Response(status=self.status, body=self.body)

        app = web.Application()
        app.router.add_get("/file", file_handler)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        self.session = aiohttp.ClientSession()
        session = self.session

        class LocalSession:
            def get(self, url, **kwargs):
                assert kwargs["allow_redirects"] is False
                return session.get(f"http://127.0.0.1:{port}/file", **kwargs)

        class FakeTelegram:
            token = "TEST_SECRET"
            session = LocalSession()

            async def call(self, method, **params):
                return {"file_path": "documents/file_1.txt"}

        self.telegram = FakeTelegram()
        self.item = {"file_id": "a", "file_name": "../../outside.txt", "kind": "document"}

    async def asyncTearDown(self):
        await self.session.close()
        await self.runner.cleanup()
        self.tmp.cleanup()

    async def test_filename_cannot_escape_private_directory_and_download_is_cached(self):
        path = await download(self.telegram, self.item, self.directory)
        self.assertEqual(path.parent, self.directory)
        self.assertEqual(path.read_bytes(), self.body)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertFalse((Path(self.tmp.name) / "outside.txt").exists())
        self.assertEqual(await download(self.telegram, self.item, self.directory), path)
        self.assertEqual(len(self.requests), 1)

    async def test_declared_oversized_file_is_rejected_before_download(self):
        with self.assertRaises(AttachmentError):
            await download(self.telegram, {**self.item, "file_size": MAX_BYTES + 1}, self.directory)
        self.assertEqual(self.requests, [])

    async def test_streaming_limit_is_enforced_even_without_content_length(self):
        self.body, self.chunked = b"x" * 128, True
        with self.assertRaises(AttachmentError):
            await download(self.telegram, self.item, self.directory, maximum=64)
        self.assertEqual(list(self.directory.glob("*.part")), [])
        self.assertEqual(list(self.directory.glob("*.txt")), [])

    async def test_http_errors_do_not_disclose_token(self):
        self.status = 500
        with self.assertRaises(ConnectionError) as caught:
            await download(self.telegram, self.item, self.directory)
        self.assertNotIn("TEST_SECRET", str(caught.exception))

    async def test_getfile_permanent_error_is_reported_without_stopping_bridge(self):
        self.telegram.call = AsyncMock(side_effect=TelegramError("getFile", 400, "file is too big"))
        with self.assertRaises(AttachmentError):
            await download(self.telegram, self.item, self.directory)


class InputRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name)
        (self.path / "access.json").write_text(json.dumps({"dmPolicy": "allowlist", "allowFrom": ["123"]}))
        self.bridge = Bridge({"thread_id": "thread", "owner_id": "123", "socket_path": "unused",
                              "telegram_state_dir": str(self.path)}, self.path)
        self.requests = []
        requests = self.requests

        class FakeCodex:
            async def call(self, method, params):
                requests.append((method, params))
                return {"turn": {"id": "t1"}}

        self.bridge.codex = FakeCodex()
        self.photo = self.path / "photo.png"
        self.photo.write_bytes(b"\x89PNG\r\n\x1a\n" + b"test payload")

    async def asyncTearDown(self):
        self.bridge.store.db.close()
        self.tmp.cleanup()

    def accept(self, message, i=1):
        self.bridge.store.accept({"update_id": i}, {"chat": {"id": 123}, **message})

    async def test_photo_and_caption_are_delivered_as_visual_input(self):
        self.accept({"caption": "what is this", "photo": [{"file_id": "image", "width": 100, "height": 100}]})
        with patch("attachments.download", AsyncMock(return_value=self.photo)):
            await self.bridge.dispatch_one(self.bridge.store.pending())
        inputs = self.requests[0][1]["input"]
        self.assertIn("what is this", inputs[0]["text"])
        self.assertEqual(inputs[1], {"type": "localImage", "path": str(self.photo)})
        self.assertNotIn("api.telegram.org", inputs[0]["text"])

    async def test_document_caption_starting_with_command_does_not_run_a_command(self):
        path = self.path / "sample.pdf"
        path.write_bytes(b"%PDF-1.7 test")
        self.accept({"caption": "/stop is the text in this document", "document": {"file_id": "doc", "file_name": "sample.pdf"}})
        with patch("attachments.download", AsyncMock(return_value=path)):
            await self.bridge.dispatch_one(self.bridge.store.pending())
        self.assertEqual(self.requests[0][0], "turn/start")
        self.assertIn(str(path), self.requests[0][1]["input"][0]["text"])
        self.assertIn("sample.pdf", self.requests[0][1]["input"][0]["text"])

    async def test_album_becomes_one_model_turn_and_all_messages_are_correlated(self):
        for i in [1, 2]:
            self.accept({"media_group_id": "album", "photo": [{"file_id": str(i), "width": 100, "height": 100}]}, i)
        self.bridge.store.db.execute("UPDATE inbox SET received_ms=received_ms-3000")
        self.bridge.store.db.commit()
        with patch("attachments.download", AsyncMock(return_value=self.photo)):
            await self.bridge.dispatch_one(self.bridge.store.pending())
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(len(self.requests[0][1]["input"]), 3)
        self.assertEqual(self.bridge.store.counts()["running_inputs"], 2)
        self.assertEqual(len({r[0] for r in self.bridge.store.db.execute("SELECT client_id FROM inbox")}), 1)

    async def test_unsupported_audio_is_received_as_a_file_without_claiming_transcription(self):
        path = self.path / "voice.ogg"
        path.write_bytes(b"OggS test")
        self.accept({"voice": {"file_id": "voice", "mime_type": "audio/ogg"}})
        with patch("attachments.download", AsyncMock(return_value=path)):
            await self.bridge.dispatch_one(self.bridge.store.pending())
        text = self.requests[0][1]["input"][0]["text"]
        self.assertIn(str(path), text)
        self.assertIn("尚未自动转写", text)

    async def test_revoked_owner_never_downloads_an_attachment(self):
        self.accept({"photo": [{"file_id": "secret"}]})
        (self.path / "access.json").write_text(json.dumps({"dmPolicy": "allowlist", "allowFrom": []}))
        with patch("attachments.download", AsyncMock()) as download_mock:
            await self.bridge.dispatch_one(self.bridge.store.pending())
        download_mock.assert_not_called()
        self.assertEqual(self.requests, [])
