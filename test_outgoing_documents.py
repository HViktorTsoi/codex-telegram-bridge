"""Explicit local documents must use the originating bridge's frozen route."""
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest

import aiohttp

from bridge import Bridge, Telegram, TelegramError
from message_format import send_output
from outgoing_images import ImageError, MAX_BYTES
from topics_bridge import RoutedStore, TargetBridge


SCRIPT = b'@echo off\r\necho DOCUMENT_ROUTE_OK\r\n'


class DocumentQueueTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name)
        self.file = self.path / 'Enable-Zbox-SSH.cmd'
        self.file.write_bytes(SCRIPT)
        self.store = RoutedStore(self.path / 'bridge.sqlite', 'native-manager')

    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    def rows(self):
        return self.store.db.execute('SELECT * FROM outbox ORDER BY rowid').fetchall()

    def documents(self):
        return [r for r in self.rows() if r['media_json']]

    def queue(self, text=None, key='turn:task:file'):
        self.store.enqueue_at(key, '123', text or f'脚本已附上：[下载脚本](<{self.file}>)', 6305)

    def test_cmd_link_becomes_one_document_with_frozen_origin_route(self):
        self.queue()
        row = self.documents()[0]
        media = json.loads(row['media_json'])
        self.assertEqual(len(self.documents()), 1)
        self.assertEqual(media['kind'], 'document')
        self.assertEqual(media['method'], 'sendDocument')
        self.assertEqual(media['name'], self.file.name)
        self.assertEqual(media['sha256'], hashlib.sha256(SCRIPT).hexdigest())
        self.assertEqual(Path(media['path']).read_bytes(), SCRIPT)
        self.assertEqual((row['chat_id'], row['message_thread_id']), ('123', 6305))
        self.assertNotIn(str(self.file), ''.join(r['text'] for r in self.rows()))
        self.assertTrue({'token', 'bot_username', 'telegram_state_dir', 'chat_id', 'message_thread_id'}.isdisjoint(media))
        self.assertEqual(os.stat(media['path']).st_mode & 0o777, 0o600)
        self.assertEqual(os.stat(Path(media['path']).parent).st_mode & 0o777, 0o700)

    def test_common_documents_and_distinct_names_with_same_content_are_preserved(self):
        for extension in ('.pdf', '.docx', '.xlsx', '.pptx', '.zip', '.csv'):
            file = self.path / ('报告' + extension)
            file.write_bytes(b'example document bytes')
            self.queue(f'[下载](<{file}>)', 'turn:task:' + extension)
        files = [json.loads(r['media_json']) for r in self.documents()]
        self.assertEqual(len(files), 6)
        self.assertEqual({m['name'] for m in files}, {'报告' + x for x in ('.pdf', '.docx', '.xlsx', '.pptx', '.zip', '.csv')})
        self.assertTrue(all(m['kind'] == 'document' and m['method'] == 'sendDocument' and m['mime'] for m in files))
        other = self.path / '另一个名称.pdf'
        other.write_bytes(b'example document bytes')
        self.queue(f'[下载](<{other}>)', 'turn:task:another-name')
        self.assertEqual(len(self.documents()), 7)

    def test_unicode_spaces_parentheses_and_file_url_deduplicate_same_document(self):
        file = self.path / '最终 报告（中文）(1).pdf'
        file.write_bytes(b'%PDF-1.7\nUNICODE_DOCUMENT\n')
        self.queue(f'[报告](<{file}>)\n[下载同一份](<{file.as_uri()}>)')
        self.assertEqual(len(self.documents()), 1)
        media = json.loads(self.documents()[0]['media_json'])
        self.assertEqual(media['name'], file.name)
        self.assertEqual(Path(media['path']).read_bytes(), file.read_bytes())

    def test_snapshot_survives_source_rewrite_deletion_and_same_key_recovery(self):
        self.queue()
        media = json.loads(self.documents()[0]['media_json'])
        self.file.write_bytes(b'new content must not replace the queued artifact')
        self.queue()
        self.file.unlink()
        self.queue()
        self.assertEqual(len(self.documents()), 1)
        self.assertEqual(Path(media['path']).read_bytes(), SCRIPT)

    def test_missing_empty_fifo_and_oversized_documents_fail_visibly(self):
        empty = self.path / 'empty.txt'
        empty.touch()
        fifo = self.path / 'pipe.txt'
        os.mkfifo(fifo)
        large = self.path / 'huge.pdf'
        with large.open('wb') as stream:
            stream.truncate(MAX_BYTES + 1)
        for i,file in enumerate((self.path / 'missing.cmd', empty, fifo, large)):
            self.queue(f'[交付文件](<{file}>)', f'turn:bad:{i}')
        self.assertEqual(len(self.documents()), 0)
        self.assertEqual(len(self.rows()), 4)
        self.assertTrue(all('未发送' in r['text'] and '文件' in r['text'] for r in self.rows()))

    def test_code_remote_relative_citation_and_user_references_are_not_uploaded(self):
        samples = (
            f'```markdown\n[示例](<{self.file}>)\n```',
            f'`[示例](<{self.file}>)`',
            '[网络](https://example.com/document.pdf)',
            '[相对](report.pdf)',
            f'[来源](<{self.file}:12>)',
            f'![错误的图片引用](<{self.file}>)',
        )
        for i,text in enumerate(samples):
            self.queue(text, f'turn:excluded:{i}')
        self.queue(f'[用户引用](<{self.file}>)', 'native-user:task:input')
        self.queue(f'[控制回复](<{self.file}>)', 'input:9:reply')
        self.assertEqual(self.documents(), [])

    def test_credentials_private_keys_and_disguised_secret_symlink_are_not_uploaded(self):
        candidates = []
        for relative in ('.env', '.ssh/id_ed25519', '.codex/auth.json', 'credentials.json', 'private-key.pem'):
            file = self.path / relative
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_text('PRIVATE_CREDENTIAL_FIXTURE')
            candidates.append(file)
        alias = self.path / 'innocent-download.txt'
        alias.symlink_to(candidates[0])
        candidates.append(alias)
        for i,file in enumerate(candidates):
            self.queue(f'[下载](<{file}>)', f'turn:secrets:{i}')
        self.assertEqual(self.documents(), [])

    def test_plain_document_only_reply_is_queued_once_without_retroactive_history_upload(self):
        self.store.set('output_format', 'plain')
        self.queue(f'[下载](<{self.file}>)')
        self.queue(f'[下载](<{self.file}>)')
        self.assertEqual(len(self.rows()), 1)
        self.assertIsNotNone(self.rows()[0]['media_json'])
        key = 'turn:old:unchanged:topic:6305:0'
        with self.store.db:
            self.store.db.execute('INSERT INTO outbox(key,chat_id,text,message_thread_id) VALUES(?,?,?,?)',
                                  (key, '123', f'[旧记录](<{self.file}>)', 6305))
        self.queue(f'[新标签](<{self.file}>)', 'turn:old:unchanged')
        row = self.store.db.execute('SELECT * FROM outbox WHERE key=?', (key,)).fetchone()
        self.assertIsNone(row['media_json'])
        self.assertIn('旧记录', row['text'])

    def test_startup_after_inflight_document_marks_uncertain_without_duplicate_upload(self):
        self.queue()
        row = self.documents()[0]
        self.store.images.begin(row)
        probe = RoutedStore(self.path / 'bridge.sqlite', 'native-manager')
        try:
            self.assertEqual(json.loads(probe.db.execute('SELECT media_json FROM outbox WHERE media_json IS NOT NULL').fetchone()[0])['delivery'], 'sending')
        finally:
            probe.db.close()
        self.store.images.recover()
        self.store.images.recover()
        documents = self.documents()
        self.assertEqual(len(documents), 1)
        self.assertEqual(documents[0]['message_id'], -1)
        self.assertEqual(json.loads(documents[0]['media_json'])['delivery'], 'uncertain')
        errors = [r for r in self.rows() if not r['media_json']]
        self.assertEqual(len(errors), 2)  # Original explanatory text and one durable error notice.
        self.assertTrue(any('文件' in r['text'] and '未获确认' in r['text'] for r in errors))
        self.assertTrue(all(r['message_thread_id'] == 6305 for r in self.rows()))


class DocumentDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name)
        self.file = self.path / 'Enable-Zbox-SSH.cmd'
        self.file.write_bytes(SCRIPT)
        (self.path / 'access.json').write_text(json.dumps({'dmPolicy': 'allowlist', 'allowFrom': ['123']}))
        self.config = {'thread_id': 'same-native-manager', 'owner_id': '123', 'socket_path': '/unused',
                       'telegram_state_dir': str(self.path), 'expected_bot_username': 'default_bot'}
        (self.path / 'default').mkdir()
        self.bridge = TargetBridge(self.config, self.path / 'default',
                                   {'key': 'manager', 'name': 'Manager', 'thread_id': 'same-native-manager',
                                    'message_thread_id': 6305}, lambda: '')
        self.bridge.store.enqueue_at('turn:task:artifact', '123', f'[交付脚本](<{self.file}>)', 6305)

    async def asyncTearDown(self):
        self.bridge.store.db.close()
        self.tmp.cleanup()

    def document(self):
        return self.bridge.store.db.execute('SELECT * FROM outbox WHERE media_json IS NOT NULL').fetchone()

    async def test_real_multipart_uses_selected_client_saved_topic_and_original_filename(self):
        observed = {}

        class Response:
            status = 200

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

            async def json(self):
                return {'ok': True, 'result': {'message_id': 418, 'message_thread_id': 6305,
                                               'document': {'file_name': 'Enable-Zbox-SSH.cmd', 'file_size': len(SCRIPT)}}}

        class Session:
            def post(self, url, **kwargs):
                observed['url'] = url
                for options,headers,value in kwargs['data']._fields:
                    name = options['name']
                    observed[name] = value.read() if name == 'document' else value
                    if name == 'document':
                        observed['filename'] = options.get('filename')
                        observed['mime'] = headers.get('Content-Type')
                return Response()

        row = self.document()
        self.assertIsNotNone(row)
        telegram = Telegram('DEFAULT_BOT_TEST_TOKEN', Session())
        result = await send_output(telegram, chat_id=row['chat_id'], text='', topic_id=row['message_thread_id'], media_json=row['media_json'])
        self.assertTrue(observed['url'].endswith('/botDEFAULT_BOT_TEST_TOKEN/sendDocument'))
        self.assertEqual((observed['chat_id'], observed['message_thread_id']), ('123', '6305'))
        self.assertEqual(observed['document'], SCRIPT)
        self.assertEqual(observed['filename'], self.file.name)
        self.assertTrue(observed['mime'])
        self.assertNotIn('DEFAULT_BOT_TEST_TOKEN', row['media_json'])
        self.assertEqual(result['message_id'], 418)

    async def test_two_bots_with_same_native_session_only_origin_inbox_routes_document(self):
        (self.path / 'secondary').mkdir()
        secondary = Bridge({**self.config, 'expected_bot_username': 'secondary_bot'}, self.path / 'secondary')
        calls = []

        class Sender:
            async def call(self, method, **params):
                calls.append(('default', method, params))
                return {'message_id': 400}

            async def upload(self, method, field, path, **params):
                calls.append(('default', method, params))
                return {'message_id': 418}

        try:
            self.bridge.store.accept({'update_id': 1}, {'message_id': 1, 'chat': {'id': 123},
                                                       'message_thread_id': 6305, 'text': '把脚本文件发给我'})
            self.bridge.store.mark(1, 'running', 'owned-turn')
            item = {'id': 'final', 'type': 'agentMessage', 'phase': 'final_answer',
                    'text': f'[脚本](<{self.file}>)'}
            self.bridge.record_item('owned-turn', item)
            secondary.record_item('owned-turn', item)
            rows = self.bridge.store.db.execute("SELECT * FROM outbox WHERE key LIKE 'turn:owned-turn:%'").fetchall()
            self.assertEqual(sum(bool(r['media_json']) for r in rows), 1)
            self.assertTrue(all(r['message_thread_id'] == 6305 for r in rows))
            self.assertEqual(secondary.store.db.execute('SELECT COUNT(*) FROM outbox').fetchone()[0], 0)
            self.bridge.telegram = Sender()
            await self.bridge.send_one(next(r for r in rows if r['media_json']))
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0][1], 'sendDocument')
            self.assertEqual(calls[0][2]['message_thread_id'], 6305)
        finally:
            secondary.store.db.close()

    async def test_document_rate_limit_retries_original_snapshot_in_same_route(self):
        calls = []

        class Sender:
            async def upload(self, method, field, path, **params):
                calls.append((method, field, path.read_bytes(), params))
                if len(calls) == 1:
                    raise TelegramError(method, 429, 'retry later', 1)
                return {'message_id': 418}

        row = self.document()
        self.assertIsNotNone(row)
        self.bridge.store.images.begin(row)
        with self.assertRaises(TelegramError):
            await send_output(Sender(), chat_id='123', text='', topic_id=6305, media_json=row['media_json'])
        self.bridge.store.images.retry(row)
        self.file.write_bytes(b'the source was edited while waiting')
        retry = self.document()
        await send_output(Sender(), chat_id='123', text='', topic_id=6305, media_json=retry['media_json'])
        self.assertEqual([c[:3] for c in calls], [('sendDocument', 'document', SCRIPT)] * 2)
        self.assertTrue(all(c[3]['message_thread_id'] == 6305 for c in calls))

    async def test_document_unknown_ack_and_permanent_errors_never_send_another_kind(self):
        row = self.document()
        self.assertIsNotNone(row)
        for error in (ConnectionError('ack lost'), TelegramError('sendDocument', 400, 'message thread not found'),
                      TelegramError('sendDocument', 403, 'forbidden'), TelegramError('sendDocument', 500, 'error')):
            calls = []

            class Sender:
                async def upload(self, method, *args, **params):
                    calls.append(method)
                    raise error

            with self.assertRaises(type(error)):
                await send_output(Sender(), chat_id='123', text='', topic_id=6305, media_json=row['media_json'])
            self.assertEqual(calls, ['sendDocument'])

    async def test_changed_document_snapshot_is_rejected_before_upload(self):
        row = self.document()
        self.assertIsNotNone(row)
        media = json.loads(row['media_json'])
        Path(media['path']).write_bytes(b'tampered')
        with self.assertRaises(ImageError):
            await send_output(None, chat_id='123', text='', topic_id=6305, media_json=row['media_json'])

    async def test_completion_sends_documents_before_one_normal_final_with_persistent_bar(self):
        self.bridge.target['notification_mode'] = 'completion'
        self.bridge.target['preview_style'] = 'status-bar'
        from completion_delivery import CompletionDelivery
        self.bridge.delivery = CompletionDelivery(self.bridge, clock=lambda: 1000)
        turn = {'id': 'task', 'status': 'inProgress', 'startedAt': 1001}
        self.bridge.delivery.watch(turn)
        self.bridge.store.enqueue_at('turn:task:answer', '123', f'已完成：[脚本](<{self.file}>)', 6305)
        self.bridge.delivery.seal({**turn, 'status': 'completed'})
        calls = []

        class Sender:
            async def upload(self, method, field, path, **params):
                calls.append((method, params))
                return {'message_id': 400 + len(calls)}

            async def call(self, method, **params):
                self_result = {'message_id': 400 + len(calls), 'message_thread_id': params.get('message_thread_id')}
                calls.append((method, params))
                return self_result

        self.bridge.telegram = Sender()
        for _ in range(10):
            row = self.bridge.delivery.next_row()
            if row is None:
                break
            await self.bridge.send_one(row)
        else:
            self.fail('Completion did not settle')
        documents = [(m,p) for m,p in calls if m == 'sendDocument']
        self.assertTrue(documents)
        self.assertTrue(all(p['disable_notification'] is True for _,p in documents))
        self.assertEqual(calls[0][0], 'sendDocument')
        finals = [(m,p) for m,p in calls if m in ('sendMessage', 'sendRichMessage') and p.get('disable_notification') is not True]
        self.assertEqual(len(finals), 1)
        self.assertEqual(len(finals[0][1]['reply_markup']['inline_keyboard']), 2)
        self.assertIn('已完成', finals[0][1]['reply_markup']['inline_keyboard'][0][0]['text'])
        self.assertTrue(all(p.get('message_thread_id') == 6305 for m,p in calls if m != 'deleteMessage'))

    async def test_document_transport_error_does_not_expose_current_bot_token(self):
        class Session:
            def post(self, *args, **kwargs):
                raise aiohttp.ClientConnectionError('https://api.telegram.org/botDOCUMENT_TEST_SECRET/sendDocument')

        with self.assertRaises(ConnectionError) as caught:
            await Telegram('DOCUMENT_TEST_SECRET', Session()).upload('sendDocument', 'document', self.file, chat_id='123')
        self.assertNotIn('DOCUMENT_TEST_SECRET', str(caught.exception))


if __name__ == '__main__':
    unittest.main()
