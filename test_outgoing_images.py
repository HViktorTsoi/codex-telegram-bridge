import asyncio
import base64
import contextlib
import json
import os
from pathlib import Path
import tempfile
import unittest

import aiohttp
from bridge import Bridge, Telegram, TelegramError
from message_format import send_output
from outgoing_images import ImageError, MAX_BYTES, local_image_path
from topics_bridge import RoutedStore, TargetBridge
import test_native_mirror as fixtures

PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aL1sAAAAASUVORK5CYII=')


class ImageQueueTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name)
        self.image = self.path / '中文 图片(1).png'
        self.image.write_bytes(PNG)
        self.store = RoutedStore(self.path / 'bridge.sqlite', 'native')

    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    def rows(self):
        return self.store.db.execute('SELECT * FROM outbox ORDER BY rowid').fetchall()

    def test_markdown_image_and_file_link_upload_once_with_saved_route(self):
        self.store.enqueue_at('turn:t:a', '123', f'图片如下\n\n![二维码](<{self.image}>)\n[下载]({self.image})', 42)
        rows = self.rows()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]['text'], '图片如下')
        media = json.loads(rows[1]['media_json'])
        self.assertEqual(Path(media['path']).read_bytes(), PNG)
        self.assertEqual(media['method'], 'sendPhoto')
        self.assertEqual(rows[1]['message_thread_id'], 42)
        self.assertEqual(os.stat(media['path']).st_mode & 0o777, 0o600)
        self.assertEqual(os.stat(Path(media['path']).parent).st_mode & 0o777, 0o700)
        self.image.write_bytes(PNG + b'changed')
        self.store.enqueue_at('turn:t:a', '123', f'![new](<{self.image}>)', 42)
        self.assertEqual(len(self.rows()), 2)
        self.assertEqual(Path(media['path']).read_bytes(), PNG)

    def test_plain_mode_still_uploads_and_image_only_reply_is_idempotent(self):
        self.store.set('output_format', 'plain')
        source = f'![图](<{self.image}>)'
        self.store.enqueue_at('turn:t:a', '123', source, 42)
        self.store.enqueue_at('turn:t:a', '123', source, 42)
        self.assertEqual(len(self.rows()), 1)
        self.assertIsNotNone(self.rows()[0]['media_json'])
        self.assertIsNone(self.rows()[0]['rich_text'])

    def test_code_examples_remote_relative_paths_and_user_input_do_not_upload(self):
        source = f'```md\n![图](<{self.image}>)\n```\n`[图](<{self.image}>)`\n![web](https://example.com/a.png)\n![relative](a.png)'
        self.store.enqueue_at('turn:t:a', '123', source, 42)
        self.assertEqual(self.rows()[0]['text'], source)
        self.assertTrue(all(r['media_json'] is None for r in self.rows()))
        self.store.enqueue_at('native-user:t:a', '123', f'![图](<{self.image}>)', 42)
        self.assertIsNone(self.rows()[-1]['media_json'])
        self.assertIsNone(local_image_path('file://other-host/private/a.png'))

    def test_missing_non_image_fifo_and_oversize_fail_visibly_without_blocking(self):
        bad = self.path / 'bad.png'
        bad.write_text('not a photo')
        fifo = self.path / 'pipe.png'
        os.mkfifo(fifo)
        huge = self.path / 'huge.png'
        with huge.open('wb') as f:
            f.truncate(MAX_BYTES + 1)
        for i, p in enumerate([bad, fifo, huge, self.path / 'missing.png']):
            self.store.enqueue_at(f'turn:t:a{i}', '123', f'![图片](<{p}>)', 42)
        self.assertEqual(len(self.rows()), 4)
        self.assertTrue(all('图片未发送' in r['text'] and r['media_json'] is None for r in self.rows()))

    def test_old_queued_source_is_not_rewritten_or_retroactively_uploaded(self):
        key = 'turn:t:a:topic:42:0'
        with self.store.db:
            self.store.db.execute('INSERT INTO outbox(key,chat_id,text,message_thread_id) VALUES(?,?,?,?)',
                                  (key, '123', f'![original](<{self.image}>)', 42))
        self.store.enqueue_at('turn:t:a', '123', f'![new](<{self.image}>)', 42)
        self.assertEqual(len(self.rows()), 1)
        self.assertIn('original', self.rows()[0]['text'])
        self.assertIsNone(self.rows()[0]['media_json'])

    def test_view_is_not_sent_until_final_explicit_delivery_and_never_from_another_turn(self):
        views = [self.path / 'internal.png', self.image]
        views[0].write_bytes(PNG + b'internal')
        for i, p in enumerate(views):
            self.store.images.observe('t', {'type': 'imageView', 'id': str(i), 'path': str(p), 'completedAtMs': i + 1})
        self.assertEqual(self.rows(), [])
        for phase, text in [('commentary', '已经附上图片'), ('final_answer', '我检查了内部截图'),
                            ('final_answer', '图片尚未发送，无法附上图片')]:
            self.assertEqual(self.store.images.agent_text('t', {'text': text, 'phase': phase}), text)
        final = {'text': '已重新附上二维码图片，请扫码。', 'phase': 'final_answer'}
        self.assertEqual(self.store.images.agent_text('other', final), final['text'])
        augmented = self.store.images.agent_text('t', final)
        self.store.enqueue_at('turn:t:final', '123', augmented, 42)
        media = json.loads(self.rows()[-1]['media_json'])
        self.assertEqual(media['name'], self.image.name)
        self.assertEqual(sum(bool(r['media_json']) for r in self.rows()), 1)

    def test_explicit_image_link_takes_precedence_over_last_internal_view(self):
        self.store.images.observe('t', {'type': 'imageView', 'id': 'view', 'path': '/other/internal.png'})
        text = f'已附上图片\n![图](<{self.image}>)'
        self.assertEqual(self.store.images.agent_text('t', {'phase': 'final_answer', 'text': text}), text)

    def test_crashed_upload_becomes_uncertain_without_retry_or_changing_destination(self):
        self.store.enqueue_at('turn:t:a', '123', f'![图](<{self.image}>)', 42)
        row = self.rows()[0]
        self.store.images.begin(row)
        # A read-only constructor must not alter an active sender's in-flight row.
        probe = RoutedStore(self.path / 'bridge.sqlite', 'native')
        self.assertEqual(json.loads(probe.db.execute('SELECT media_json FROM outbox').fetchone()[0])['delivery'], 'sending')
        probe.db.close()
        self.store.images.recover()
        rows = self.rows()
        self.assertEqual(rows[0]['message_id'], -1)
        self.assertEqual(json.loads(rows[0]['media_json'])['delivery'], 'uncertain')
        self.assertIn('未获确认', rows[1]['text'])
        self.assertTrue(all(r['message_thread_id'] == 42 for r in rows))
        self.store.images.recover()
        self.assertEqual(len(self.rows()), 2)


class ImageDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name)
        self.image = self.path / 'qr.png'
        self.image.write_bytes(PNG)
        (self.path / 'access.json').write_text(json.dumps({'dmPolicy': 'allowlist', 'allowFrom': ['123']}))
        self.config = {'thread_id': 'native', 'owner_id': '123', 'socket_path': '/unused',
                       'telegram_state_dir': str(self.path), 'notification_mode': 'all'}
        self.bridge = TargetBridge(self.config, self.path, {'thread_id': 'native', 'key': 'test', 'name': 'test', 'message_thread_id': 42}, lambda: '')
        self.bridge.store.enqueue_at('turn:t:a', '123', f'![QR](<{self.image}>)', 42)
        self.row = self.bridge.store.db.execute('SELECT * FROM outbox').fetchone()

    async def asyncTearDown(self):
        self.bridge.store.db.close()
        self.tmp.cleanup()

    async def test_real_multipart_upload_sends_binary_and_saved_topic(self):
        observed = {}

        class Response:
            status = 200

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

            async def json(self):
                return {'ok': True, 'result': {'message_id': 77, 'photo': [{'file_id': 'photo'}]}}

        class Session:
            def post(self, url, **kwargs):
                self.fields = kwargs['data']._fields
                for options, headers, value in self.fields:
                    observed[options['name']] = value.read() if options['name'] == 'photo' else value
                return Response()

        telegram = Telegram('PRIVATE_TOKEN', Session())
        result = await send_output(telegram, chat_id='123', text='', topic_id=42, media_json=self.row['media_json'])
        self.assertEqual(observed['photo'], PNG)
        self.assertEqual(observed['message_thread_id'], '42')
        self.assertEqual(observed['chat_id'], '123')
        self.assertEqual(result['message_id'], 77)

    async def test_single_bridge_records_view_and_final_delivery_without_model_calls(self):
        directory = self.path / 'single'
        directory.mkdir()
        single = Bridge(self.config, directory)
        try:
            single.store.accept({'update_id': 1}, {'chat': {'id': 123}, 'text': '发图片'})
            single.store.mark(1, 'running', 'turn')
            single.record_item('turn', {'id': 'view', 'type': 'imageView', 'path': str(self.image)})
            self.assertEqual(single.store.counts()['unsent_messages'], 0)
            single.record_item('turn', {'id': 'final', 'type': 'agentMessage',
                                        'phase': 'final_answer', 'text': '已附上二维码图片。'})
            rows = single.store.db.execute('SELECT * FROM outbox').fetchall()
            self.assertEqual(sum(bool(r['media_json']) for r in rows), 1)
            self.assertTrue(all(r['message_thread_id'] is None for r in rows))
        finally:
            single.store.db.close()

    async def test_photo_rejection_falls_back_to_document_in_same_topic(self):
        calls = []

        class Sender:
            async def upload(self, method, field, path, **params):
                calls.append((method, params))
                if method == 'sendPhoto':
                    raise TelegramError(method, 400, 'PHOTO_INVALID_DIMENSIONS')
                return {'message_id': 8}

        await send_output(Sender(), chat_id='123', text='', topic_id=42, media_json=self.row['media_json'])
        self.assertEqual([c[0] for c in calls], ['sendPhoto', 'sendDocument'])
        self.assertTrue(all(c[1]['message_thread_id'] == 42 for c in calls))

    async def test_unknown_delivery_missing_topic_and_throttling_never_fallback(self):
        for error in [ConnectionError('lost response'), TelegramError('sendPhoto', 400, 'message thread not found'),
                      TelegramError('sendPhoto', 403, 'Forbidden'), TelegramError('sendPhoto', 429, 'retry', 1),
                      TelegramError('sendPhoto', 500, 'server error')]:
            calls = []

            class Sender:
                async def upload(self, method, *args, **params):
                    calls.append(method)
                    raise error

            with self.assertRaises(type(error)):
                await send_output(Sender(), chat_id='123', text='', topic_id=42, media_json=self.row['media_json'])
            self.assertEqual(calls, ['sendPhoto'])

    async def test_tampered_snapshot_does_not_upload(self):
        media = json.loads(self.row['media_json'])
        Path(media['path']).write_bytes(b'changed')
        with self.assertRaises(ImageError):
            await send_output(None, chat_id='123', text='', topic_id=42, media_json=self.row['media_json'])

    async def test_sender_success_saves_photo_id_and_unknown_delivery_stops_retry(self):
        for error in [None, ConnectionError('lost response')]:
            if error:
                with self.bridge.store.db:
                    self.bridge.store.db.execute('UPDATE outbox SET message_id=NULL')
                self.bridge.store.images.retry(self.row)
            calls = []

            class Sender:
                async def upload(self, method, *args, **params):
                    calls.append((method, params))
                    if error:
                        raise error
                    return {'message_id': 90}

            self.bridge.telegram = Sender()
            task = asyncio.create_task(self.bridge.sender_loop())
            try:
                for _ in range(40):
                    row = self.bridge.store.db.execute('SELECT * FROM outbox WHERE key=?', (self.row['key'],)).fetchone()
                    if row['message_id'] is not None:
                        break
                    await asyncio.sleep(0.01)
                self.assertEqual(row['message_id'], -1 if error else 90)
                self.assertEqual(json.loads(row['media_json'])['delivery'], 'uncertain' if error else 'sent')
                self.assertEqual(len(calls), 1)
                self.assertEqual(calls[0][1]['message_thread_id'], 42)
            finally:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    async def test_upload_network_errors_do_not_leak_bot_token(self):
        class Session:
            def post(self, *args, **kwargs):
                raise aiohttp.ClientConnectionError('https://api.telegram.org/botPRIVATE_TOKEN/sendPhoto')

        with self.assertRaises(ConnectionError) as caught:
            await Telegram('PRIVATE_TOKEN', Session()).upload('sendPhoto', 'photo', self.image, chat_id='123')
        self.assertNotIn('PRIVATE_TOKEN', str(caught.exception))


class MirroredImagesTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.NativeMirrorTests.asyncSetUp
    asyncTearDown = fixtures.NativeMirrorTests.asyncTearDown
    enable = fixtures.NativeMirrorTests.enable
    output = fixtures.NativeMirrorTests.output
    turn = staticmethod(fixtures.NativeMirrorTests.turn)
    user = staticmethod(fixtures.NativeMirrorTests.user)
    agent = staticmethod(fixtures.NativeMirrorTests.agent)

    async def test_desktop_final_image_is_mirrored_once_and_internal_view_is_not(self):
        await self.enable()
        path = self.path / 'desktop.png'
        path.write_bytes(PNG)
        view = {'type': 'imageView', 'id': 'view', 'path': str(path)}
        self.items['new'] = [self.user(), view, self.agent('已重新附上二维码图片。')]
        await self.bridge.update_native_mirror([self.turn(status='completed')])
        self.assertEqual(sum(bool(r['media_json']) for r in self.output()), 1)
        await self.bridge.update_native_mirror([self.turn(status='completed')])
        self.assertEqual(sum(bool(r['media_json']) for r in self.output()), 1)
        self.items['internal'] = [view, self.agent('我查看了截图，接下来分析。')]
        await self.bridge.update_native_mirror([self.turn('internal', status='completed', started=1002)])
        self.assertEqual(sum(bool(r['media_json']) for r in self.output()), 1)


if __name__ == '__main__':
    unittest.main()
