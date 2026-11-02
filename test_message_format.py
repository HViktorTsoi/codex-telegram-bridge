import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from bridge import Store, TelegramError
from message_format import message_parts, normalize_local_links, send_output, utf16_size
from topics_bridge import RoutedStore


class ChunkTests(unittest.TestCase):
    def test_short_gfm_including_table_formula_and_links_is_preserved(self):
        source = '# Heading\n\n**bold** [link](https://example.com)\n\n| A | B |\n|---|---|\n| 1 | 2 |\n\n$x^2$'
        self.assertEqual(list(message_parts(source, rich=True)), [(source, source)])

    def test_fenced_code_is_balanced_across_long_messages_without_changing_source(self):
        source = '# Report\n\n```python\n' + 'print("中文😀")\n' * 80 + '```\n\nDone.'
        parts = list(message_parts(source, rich=True, limit=120, max_lines=8))
        self.assertGreater(len(parts), 2)
        self.assertEqual(''.join(p[0] for p in parts), source)
        self.assertTrue(all(utf16_size(p[0]) <= 120 for p in parts))
        self.assertTrue(all(p[1].count('```') == 2 for p in parts if 'print' in p[0]))

    def test_four_backticks_keep_literal_three_backticks_inside_code(self):
        source = '````markdown\n' + ('Literal ``` is part of a line\n' * 30) + '````\n'
        parts = list(message_parts(source, rich=True, limit=110))
        self.assertEqual(''.join(p[0] for p in parts), source)
        self.assertTrue(all(p[1].startswith('````markdown\n') and p[1].rstrip().endswith('````') for p in parts))

    def test_very_long_code_line_splits_without_losing_unicode(self):
        source = '```text\n' + '😀中文' * 200 + '\n```'
        parts = list(message_parts(source, rich=True, limit=100))
        self.assertEqual(''.join(p[0] for p in parts), source)
        self.assertTrue(all(utf16_size(p[0]) <= 100 for p in parts))
        self.assertTrue(all(p[1].count('```') == 2 for p in parts))

    def test_unclosed_fence_is_closed_only_in_rendered_payload(self):
        source = '```python\nprint(1)'
        self.assertEqual(list(message_parts(source, rich=True)), [(source, source + '\n```')])

    def test_plain_preference_and_unusual_fence_keep_raw_text(self):
        for source in ('😀' * 4000, '`' * 40 + '\nraw\n' + '`' * 40):
            parts = list(message_parts(source, rich=False))
            self.assertEqual(''.join(p[0] for p in parts), source)
            self.assertTrue(all(p[1] is None for p in parts))
        parts = list(message_parts('`' * 40 + '\nraw', rich=True))
        self.assertTrue(all(p[1] is None for p in parts))


class QueueTests(unittest.TestCase):
    def test_legacy_outbox_migration_and_recovery_keep_original_chunk_boundaries(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'queue.sqlite'
            with sqlite3.connect(path) as db:
                db.execute('CREATE TABLE outbox(key TEXT PRIMARY KEY,chat_id TEXT,text TEXT,message_id INTEGER,attempts INTEGER DEFAULT 0,retry_at REAL DEFAULT 0)')
                db.execute('INSERT INTO outbox(key,chat_id,text) VALUES(?,?,?)', ('turn:t:a:0', '123', 'old first'))
                db.execute('INSERT INTO outbox(key,chat_id,text) VALUES(?,?,?)', ('turn:t:a:1', '123', 'old second'))
            store = Store(path)
            try:
                store.enqueue('turn:t:a', '123', 'a completely different chunk layout\n' * 1000)
                rows = store.db.execute('SELECT text,rich_text FROM outbox ORDER BY key').fetchall()
                self.assertEqual([tuple(r) for r in rows], [('old first', None), ('old second', None)])
            finally:
                store.db.close()

    def test_model_reply_uses_rich_but_commands_and_plain_preference_do_not(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / 'queue.sqlite')
            try:
                store.enqueue('turn:t:a', '123', '**rich**')
                store.enqueue('input:1:reply', '123', '**literal control text**')
                store.set('output_format', 'plain')
                store.enqueue('turn:t:b', '123', '**raw**')
                rows = store.db.execute('SELECT rich_text FROM outbox ORDER BY rowid').fetchall()
                self.assertEqual([r[0] for r in rows], ['**rich**', None, None])
            finally:
                store.db.close()

    def test_routed_replay_keeps_queued_topic_and_render_payload(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RoutedStore(Path(tmp) / 'queue.sqlite', 'native-id')
            try:
                store.enqueue_at('turn:t:a', '123', '```python\nprint(1)', 42)
                store.set('output_format', 'plain')
                store.enqueue_at('turn:t:a', '123', 'changed content\n' * 500, 42)
                row = store.db.execute('SELECT text,rich_text,message_thread_id FROM outbox').fetchone()
                self.assertEqual(tuple(row), ('```python\nprint(1)', '```python\nprint(1)\n```', 42))
                self.assertEqual(store.db.execute('SELECT COUNT(*) FROM outbox').fetchone()[0], 1)
            finally:
                store.db.close()


class LocalLinkTests(unittest.TestCase):
    def test_local_image_and_download_link_preserve_surrounding_formatting(self):
        source = '**18 人、6,219 窗**\n\n![训练图](/data/研究/训练图.png)\n\n[下载高清图片](/data/研究/训练图.png)\n\n| A | B |\n|---|---|\n| 1 | 2 |'
        rendered = normalize_local_links(source)
        self.assertIn('**18 人、6,219 窗**', rendered)
        self.assertIn('训练图（本机图片，尚未上传：` /data/研究/训练图.png `）', rendered)
        self.assertIn('下载高清图片（本机文件：` /data/研究/训练图.png `）', rendered)
        self.assertTrue(rendered.endswith('| 1 | 2 |'))

    def test_balanced_parentheses_spaces_angle_brackets_and_titles(self):
        source = '[图](</data/a b/图(1).png> "标题") [文件](../result(2).csv)'
        rendered = normalize_local_links(source)
        self.assertIn('` /data/a b/图(1).png `', rendered)
        self.assertIn('` ../result(2).csv `', rendered)
        self.assertNotIn('标题', rendered)

    def test_file_sandbox_relative_and_windows_paths_are_literal(self):
        for target in ('file:///data/image.png', 'sandbox:/mnt/data/image.png',
                       './report.md', 'README.md', '~/a.png', r'C:\Users\me\a.png'):
            with self.subTest(target=target):
                self.assertIn('本机文件', normalize_local_links('[文件](' + target + ')'))

    def test_public_urls_anchors_and_code_examples_are_not_rewritten(self):
        source = ('[web](https://example.com/a_(b)) ![remote](https://example.com/a.png)\n'
                  '[user](tg://user?id=123) [email](mailto:a@example.com) [anchor](#here)\n'
                  '`![literal](/tmp/a.png)`\n``[example](/tmp/a`b.md)``\n'
                  '````markdown\n![example](/tmp/a.png)\n```\n````\n'
                  '~~~text\n[file](/tmp/example.md)\n~~~\n')
        self.assertEqual(normalize_local_links(source), source)

    def test_backtick_filename_remains_literal_and_normalizing_twice_is_safe(self):
        rendered = normalize_local_links('![图](/data/a`b.png)')
        self.assertIn('`` /data/a`b.png ``', rendered)
        self.assertEqual(normalize_local_links(rendered), rendered)


class DeliveryTests(unittest.IsolatedAsyncioTestCase):
    def telegram(self, error=None):
        class StubTelegram:
            def __init__(self):
                self.calls = []

            async def call(self, method, **params):
                self.calls.append((method, params))
                if method == 'sendRichMessage' and error:
                    raise error
                return {'message_id': 1}
        return StubTelegram()

    async def test_rich_delivery_keeps_topic_and_body_without_legacy_title(self):
        tg = self.telegram()
        await send_output(tg, chat_id='123', topic_id=42, text='**body**', rich_text='**body**', label='[A]_B')
        method, params = tg.calls[0]
        self.assertEqual(method, 'sendRichMessage')
        self.assertEqual((params['chat_id'], params['message_thread_id']), ('123', 42))
        self.assertEqual(params['rich_message']['markdown'], '**body**')

    async def test_parse_error_falls_back_to_verbatim_source_in_same_topic(self):
        tg = self.telegram(TelegramError('sendRichMessage', 400, "Bad Request: can't parse markdown"))
        with self.assertLogs('codex_telegram_format', level='WARNING'):
            await send_output(tg, chat_id='123', topic_id=42, text='raw **broken', rich_text='raw **broken', label='旧会话标题')
        self.assertEqual([m for m, _ in tg.calls], ['sendRichMessage', 'sendMessage'])
        self.assertEqual(tg.calls[1][1], {'chat_id': '123', 'message_thread_id': 42, 'text': 'raw **broken'})

    async def test_local_image_is_normalized_before_send_without_disabling_rich(self):
        tg = self.telegram()
        source = '**bold**\n\n![plot](/data/plot.png)'
        await send_output(tg, chat_id='123', topic_id=42, text=source, rich_text=source)
        self.assertEqual([m for m, _ in tg.calls], ['sendRichMessage'])
        params = tg.calls[0][1]
        self.assertEqual(params['message_thread_id'], 42)
        self.assertIn('**bold**', params['rich_message']['markdown'])
        self.assertNotIn('](/data/', params['rich_message']['markdown'])

    async def test_failed_normalized_payload_still_falls_back_to_original_source(self):
        tg = self.telegram(TelegramError('sendRichMessage', 400, 'Bad Request: RICH_MESSAGE_URL_INVALID'))
        source = '**bold** ![plot](/data/plot.png)'
        with self.assertLogs('codex_telegram_format', level='WARNING') as logs:
            await send_output(tg, chat_id='123', topic_id=42, text=source, rich_text=source)
        self.assertIn('invalid_url', logs.output[0])
        self.assertNotIn('/data/plot', logs.output[0])
        self.assertEqual(tg.calls[-1][1]['text'], source)

    async def test_missing_method_is_cached_but_other_sends_keep_working(self):
        tg = self.telegram(TelegramError('sendRichMessage', 404, 'Not Found'))
        with self.assertLogs('codex_telegram_format', level='WARNING'):
            await send_output(tg, chat_id='123', text='first', rich_text='first')
        await send_output(tg, chat_id='123', text='second', rich_text='second')
        self.assertEqual([m for m, _ in tg.calls], ['sendRichMessage', 'sendMessage', 'sendMessage'])

    async def test_missing_topic_throttling_permissions_and_unknown_delivery_never_trigger_fallback(self):
        for error in [TelegramError('sendRichMessage', 400, 'message thread not found'),
                      TelegramError('sendRichMessage', 400, 'TOPIC_CLOSED'),
                      TelegramError('sendRichMessage', 403, 'Forbidden'),
                      TelegramError('sendRichMessage', 429, 'Too Many Requests'),
                      TelegramError('sendRichMessage', 500, 'Internal Server Error'),
                      ConnectionError('response unknown')]:
            with self.subTest(error=type(error).__name__, code=getattr(error, 'code', None)):
                tg = self.telegram(error)
                with self.assertRaises(type(error)):
                    await send_output(tg, chat_id='123', topic_id=42, text='source', rich_text='source')
                self.assertEqual(len(tg.calls), 1)

    async def test_plain_delivery_keeps_body_verbatim_without_legacy_title(self):
        tg = self.telegram()
        await send_output(tg, chat_id='123', text='😀' * 1900, label='😀' * 300)
        self.assertEqual(tg.calls[0][0], 'sendMessage')
        self.assertEqual(tg.calls[0][1]['text'], '😀' * 1900)
        self.assertLessEqual(utf16_size(tg.calls[0][1]['text']), 4096)
