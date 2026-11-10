"""User input echoes are quoted only in display, with literal raw fallbacks."""
import copy
from html.parser import HTMLParser
from pathlib import Path
import tempfile
import unittest

from markdown_it import MarkdownIt

from bridge import TelegramError
from message_format import send_output, utf16_size
from topics_bridge import RoutedStore
import test_persistent_status as fixtures


SEPARATOR = '\n\n────────────\n\n'


class DisplayHTML(HTMLParser):
    def __init__(self, source):
        super().__init__(convert_charrefs=True)
        self.stack, self.nodes, self.text, self.quoted = [], [], [], []
        self.feed(source)

    def handle_starttag(self, tag, attrs):
        self.nodes.append((tag, tuple(self.stack)))
        if tag not in ('br', 'hr', 'img'):
            self.stack.append(tag)

    def handle_endtag(self, tag):
        if tag in self.stack:
            del self.stack[len(self.stack) - 1 - self.stack[::-1].index(tag):]

    def handle_data(self, text):
        self.text.append(text)
        if 'blockquote' in self.stack:
            self.quoted.append(text)


def markdown_display(source):
    return DisplayHTML(MarkdownIt('commonmark', {'html': False}).render(source))


class EchoQueueTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = RoutedStore(Path(self.tmp.name) / 'queue.sqlite', 'native')

    async def asyncTearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    def rows(self):
        return self.store.db.execute('SELECT * FROM outbox ORDER BY rowid').fetchall()

    def telegram(self, reject=False):
        class Telegram:
            def __init__(self):
                self.calls = []

            async def call(self, method, **params):
                self.calls.append((method, copy.deepcopy(params)))
                if reject and method == 'sendRichMessage':
                    raise TelegramError(method, 400, "can't parse rich message")
                return {'message_id': len(self.calls)}

        return Telegram()

    async def test_all_skill_and_desktop_roles_quote_literal_text_without_media(self):
        body = '**literal** _value_ `code` <https://example.com> &copy;\n![图](/private/input.png)'
        for key, title in (('main-guard-input:draft:1', '📩 你在 All 说：'),
                           ('main-guard-input:skills:2', '📩 你在 All 说：'),
                           ('native-user:work:user', '🖥 你在 Codex 客户端说：')):
            self.store.enqueue_at(key, '123', title + '\n' + body, 42)
        tg = self.telegram()
        for row in self.rows():
            self.assertTrue(row['rich_text'])
            self.assertTrue(row['text'].endswith(body))
            self.assertIsNone(row['media_json'])
            parsed = markdown_display(row['rich_text'])
            self.assertIn('blockquote', [tag for tag, _ in parsed.nodes])
            self.assertIn(body.splitlines()[0], ''.join(parsed.quoted))
            self.assertIn(body.splitlines()[1], ''.join(parsed.quoted))
            self.assertFalse({'a', 'img', 'strong', 'em', 'code'} & {tag for tag, _ in parsed.nodes})
            await send_output(tg, chat_id=row['chat_id'], topic_id=row['message_thread_id'],
                              text=row['text'], rich_text=row['rich_text'], media_json=row['media_json'])
        self.assertEqual([method for method, _ in tg.calls], ['sendRichMessage'] * 3)
        self.assertTrue(all(params['message_thread_id'] == 42 for _, params in tg.calls))

    async def test_plain_preference_preserves_raw_echo_without_synthetic_markers(self):
        self.store.set('output_format', 'plain')
        raw = '📩 你在 All 说：\n**原文**\n> 已有引用\n\\ 保留反斜杠'
        self.store.enqueue_at('main-guard-input:draft:1', '123', raw, 42)
        row = self.rows()[0]
        self.assertEqual(row['text'], raw)
        self.assertIsNone(row['rich_text'])
        tg = self.telegram()
        await send_output(tg, chat_id=row['chat_id'], topic_id=42, text=row['text'], rich_text=row['rich_text'])
        self.assertEqual(tg.calls, [('sendMessage', {'chat_id': '123', 'message_thread_id': 42, 'text': raw})])

    async def test_rich_rejection_falls_back_to_original_raw_text(self):
        raw = '🖥 你在 Codex 客户端说：\n**原样** &copy; ![图](/private/input.png)'
        self.store.enqueue_at('native-user:work:user', '123', raw, 42)
        row, tg = self.rows()[0], self.telegram(reject=True)
        await send_output(tg, chat_id=row['chat_id'], topic_id=42, text=row['text'], rich_text=row['rich_text'])
        self.assertEqual([method for method, _ in tg.calls], ['sendRichMessage', 'sendMessage'])
        self.assertEqual(tg.calls[-1][1]['text'], raw)
        self.assertEqual(self.rows()[0]['text'], raw)

    async def test_long_unicode_echo_keeps_each_continuation_quoted_and_raw_intact(self):
        raw = '📩 你在 All 说：\n' + ('😀 **值** \\ <tag> &copy; ' * 800) + '\n最后一行\n'
        self.store.enqueue_at('main-guard-input:draft:1', '123', raw, 42)
        rows = self.rows()
        self.assertGreater(len(rows), 2)
        self.assertEqual(''.join(row['text'] for row in rows), raw)
        for row in rows:
            self.assertLessEqual(utf16_size(row['text']), 3800)
            self.assertLessEqual(utf16_size(row['rich_text']), 3800)
            self.assertTrue(row['rich_text'].startswith('>'))
            parsed = markdown_display(row['rich_text'])
            self.assertIn('blockquote', [tag for tag, _ in parsed.nodes])
            self.assertIsNone(row['media_json'])

    async def test_assistant_text_resembling_an_echo_label_is_not_reclassified(self):
        raw = '📩 你在 All 说：\n助手正在解释这个标签，**仍按助手排版**。'
        self.store.enqueue_at('turn:work:answer', '123', raw, 42)
        row = self.rows()[0]
        self.assertEqual(row['rich_text'], raw)
        self.assertNotIn('blockquote', [tag for tag, _ in markdown_display(row['rich_text']).nodes])


class EchoCompletionTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.PersistentStatusTests.asyncSetUp
    asyncTearDown = fixtures.PersistentStatusTests.asyncTearDown
    restart = fixtures.PersistentStatusTests.restart
    delivery = fixtures.PersistentStatusTests.delivery
    turn = fixtures.PersistentStatusTests.turn
    accept = fixtures.PersistentStatusTests.accept
    identity = fixtures.PersistentStatusTests.identity
    output = fixtures.PersistentStatusTests.output
    update = fixtures.PersistentStatusTests.update
    sends = fixtures.PersistentStatusTests.sends
    calls_for = fixtures.PersistentStatusTests.calls_for
    assert_status_keyboard = fixtures.PersistentStatusTests.assert_status_keyboard
    keyboard_text = fixtures.PersistentStatusTests.keyboard_text
    drain = fixtures.PersistentStatusTests.drain

    def echo(self, raw, role='main-guard-input:draft:7'):
        bridge = self.bridges['a']
        self.accept()
        bridge.store.mark(7, 'running', 'a-work')
        bridge.delivery.watch(self.turn())
        bridge.store.enqueue_at(role, '123', raw, 42)

    async def test_cumulative_preview_quotes_input_and_keeps_progress_outside_it(self):
        raw = '📩 你在 All 说：\n**字面要求**\n第二行要求'
        self.echo(raw)
        await self.update()
        mid = self.delivery().body_preview.load(self.identity())['message_id']
        self.output('普通进度 **正常加粗**')
        self.now += 15
        await self.update()
        self.assertEqual(self.delivery().body_preview.load(self.identity())['message_id'], mid)
        self.assertEqual(len(self.sends()), 1)
        message = self.messages[mid]
        parsed = markdown_display(message['rich_message']['markdown'])
        self.assertIn('**字面要求**', ''.join(parsed.quoted))
        self.assertNotIn('普通进度', ''.join(parsed.quoted))
        self.assertTrue(any(tag == 'strong' and 'blockquote' not in path for tag, path in parsed.nodes))
        self.assert_status_keyboard(message, '正在工作')
        source = self.bridges['a'].store.db.execute("SELECT text FROM outbox WHERE key LIKE 'main-guard-input:%'").fetchone()
        self.assertEqual(source['text'], raw)

    async def test_footer_preview_keeps_echo_as_normal_quote_and_dims_only_progress(self):
        self.bridges['a'].config['preview_body_style'] = 'footer'
        self.restart()
        self.echo('📩 你在 All 说：\n**输入原文**')
        self.output('中途进度段落。')
        await self.update()
        mid = self.delivery().body_preview.load(self.identity())['message_id']
        parsed = DisplayHTML(self.messages[mid]['rich_message']['html'])
        self.assertIn('**输入原文**', ''.join(parsed.quoted))
        self.assertTrue(any(tag == 'blockquote' and 'footer' not in path for tag, path in parsed.nodes))
        self.assertTrue(any(tag == 'footer' and 'blockquote' not in path for tag, path in parsed.nodes))
        self.assertFalse(any(tag == 'footer' and 'blockquote' in path for tag, path in parsed.nodes))

    async def test_completed_history_quotes_both_sources_and_final_stays_normal(self):
        all_raw = '📩 你在 All 说：\n**All原文** ![图](/private/input.png)'
        native_raw = '🖥 你在 Codex 客户端说：\n<https://example.com> &copy;'
        self.echo(all_raw)
        self.bridges['a'].store.enqueue_at('native-user:a-work:desktop', '123', native_raw, 42)
        self.output('已核对正文', iid='progress')
        final_raw = '# 最终结果\n\n**完成正文**'
        self.output(final_raw, iid='final')
        self.delivery().seal(self.turn(status='completed'))
        rows = await self.drain()
        self.assertEqual([(text, silent) for _, text, silent in rows],
                         [(all_raw + SEPARATOR + native_raw + SEPARATOR + '已核对正文', True), (final_raw, False)])
        calls = self.sends()
        self.assertEqual(len(calls), 2)
        history = markdown_display(calls[0][1]['rich_message']['markdown'])
        self.assertEqual(sum(tag == 'blockquote' for tag, _ in history.nodes), 2)
        self.assertIn('**All原文** ![图](/private/input.png)', ''.join(history.quoted))
        self.assertIn('<https://example.com> &copy;', ''.join(history.quoted))
        final = markdown_display(calls[-1][1]['rich_message']['markdown'])
        self.assertNotIn('blockquote', [tag for tag, _ in final.nodes])
        self.assertIn('h1', [tag for tag, _ in final.nodes])
        self.assert_status_keyboard(calls[-1][1], '已完成')
        self.assertEqual(self.calls_for('sendPhoto', 'sendDocument'), [])

    async def test_completed_history_plain_fallback_retains_original_transcript(self):
        raw = '📩 你在 All 说：\n**原文** &copy; \\ ![图](/private/input.png)'
        self.echo(raw)
        self.output('独立最终回复', iid='final')
        self.delivery().seal(self.turn(status='completed'))
        self.failures['sendRichMessage'] = [TelegramError('sendRichMessage', 400, "can't parse rich message")]
        rows = await self.drain()
        self.assertEqual([text for _, text, _ in rows], [raw, '独立最终回复'])
        plain = self.calls_for('sendMessage')
        self.assertEqual(len(plain), 1)
        self.assertEqual(plain[0][1]['text'], raw)
        self.assertEqual(plain[0][1]['message_thread_id'], 42)

    async def test_preview_rich_rejection_uses_unescaped_original_body(self):
        raw = '📩 你在 All 说：\n**原文** &copy; \\ ![图](/private/input.png)'
        self.echo(raw)
        self.failures['sendRichMessage'] = [TelegramError('sendRichMessage', 400, "can't parse rich message")]
        await self.update()
        mid = self.delivery().body_preview.load(self.identity())['message_id']
        self.assertEqual(self.messages[mid]['text'], raw)
        self.assertNotIn('rich_message', self.messages[mid])
        self.assert_status_keyboard(self.messages[mid], '正在工作')

    async def test_plain_completed_history_never_inserts_quote_or_escape_markers(self):
        self.bridges['a'].store.set('output_format', 'plain')
        raw = '🖥 你在 Codex 客户端说：\n**原文** &copy; \\ <https://example.com>'
        self.echo(raw, 'native-user:a-work:desktop')
        self.output('正常最终回复', iid='final')
        self.delivery().seal(self.turn(status='completed'))
        rows = await self.drain()
        self.assertEqual([text for _, text, _ in rows], [raw, '正常最终回复'])
        self.assertTrue(all(method == 'sendMessage' for method, _ in self.sends()))
        self.assertEqual(self.sends()[0][1]['text'], raw)

    async def test_long_completed_echo_never_loses_quotation_on_continuation(self):
        raw = '🖥 你在 Codex 客户端说：\n' + ('😀 **原文** \\ <x> ' * 900) + '\n尾部'
        self.echo(raw, 'native-user:a-work:desktop')
        self.output('独立最终回复', iid='final')
        self.delivery().seal(self.turn(status='completed'))
        rows = await self.drain()
        self.assertGreater(len(rows), 3)
        self.assertEqual(''.join(text for _, text, _ in rows[:-1]), raw)
        for method, params in self.sends()[:-1]:
            self.assertEqual(method, 'sendRichMessage')
            markdown = params['rich_message']['markdown']
            self.assertLessEqual(utf16_size(markdown), 3800)
            self.assertTrue(markdown.startswith('>'))
            self.assertIn('blockquote', [tag for tag, _ in markdown_display(markdown).nodes])
            self.assertTrue(params['disable_notification'])
        self.assertFalse(self.sends()[-1][1]['disable_notification'])

    async def test_long_preview_tail_stays_quoted_and_preserves_last_literal_symbols(self):
        raw = '📩 你在 All 说：\n' + ('长行😀 **原文** ' * 1000) + '保留结尾 **字面符号** &copy;'
        self.echo(raw)
        await self.update()
        mid = self.delivery().body_preview.load(self.identity())['message_id']
        markdown = self.messages[mid]['rich_message']['markdown']
        self.assertLessEqual(utf16_size(markdown), 3800)
        self.assertIn('保留结尾 **字面符号** &copy;', ''.join(markdown_display(markdown).quoted))
        self.assertEqual(''.join(row[0] for row in self.bridges['a'].store.db.execute(
            "SELECT text FROM outbox WHERE key LIKE 'main-guard-input:%' ORDER BY rowid")), raw)

    async def test_rate_limit_and_restart_preserve_frozen_quote_payload_and_route(self):
        raw = '📩 你在 All 说：\n**同一份原文**\n第二行'
        self.echo(raw)
        self.output('最终结果', iid='final')
        self.delivery().seal(self.turn(status='completed'))
        row = self.delivery().next_row()
        frozen = tuple(row[field] for field in ('key', 'text', 'rich_text', 'chat_id', 'message_thread_id'))
        self.delivery().begin(row)
        self.delivery().failed(row, TelegramError('sendRichMessage', 429, 'limited', retry_after=20))
        self.restart()
        self.assertIsNone(self.delivery().next_row())
        self.now += 20
        retry = self.delivery().next_row()
        self.assertEqual(tuple(retry[field] for field in ('key', 'text', 'rich_text', 'chat_id', 'message_thread_id')), frozen)
        self.assertIn('blockquote', [tag for tag, _ in markdown_display(retry['rich_text']).nodes])

