"""Dim ordinary progress without rewriting canonical text or task controls."""
import copy
from html.parser import HTMLParser
import unittest
from unittest.mock import patch

from bridge import TelegramError
from preview_footer import render_footer_markdown
from test_completion_delivery import ApiError
import test_persistent_status as harness


class ParsedHTML(HTMLParser):
    """Inspect rendered structure without relying on HTML whitespace choices."""
    VOID = {'br', 'hr', 'img', 'input', 'wbr', 'source'}

    def __init__(self, source):
        super().__init__(convert_charrefs=True)
        self.nodes, self.stack, self.text = [], [], []
        self.feed(source)
        self.close()

    def handle_starttag(self, tag, attrs):
        self.nodes.append((tag, dict(attrs), tuple(self.stack)))
        if tag not in self.VOID:
            self.stack.append(tag)

    def handle_startendtag(self, tag, attrs):
        self.nodes.append((tag, dict(attrs), tuple(self.stack)))

    def handle_endtag(self, tag):
        if tag in self.stack:
            del self.stack[len(self.stack) - 1 - self.stack[::-1].index(tag):]

    def handle_data(self, data):
        self.text.append(data)

    def tags(self):
        return [tag for tag, _, _ in self.nodes]


class FooterRendererTests(unittest.TestCase):
    def test_ordinary_paragraphs_are_small_while_inline_markup_and_text_survive(self):
        html = render_footer_markdown('第一段 **粗体**、*斜体*、`变量`。\n\n第二段 [链接](https://example.com/a)。')
        parsed = ParsedHTML(html)
        self.assertEqual(parsed.tags().count('footer'), 2)
        self.assertTrue({'strong', 'em', 'code', 'a'} <= set(parsed.tags()))
        self.assertTrue(all(not path for tag, _, path in parsed.nodes if tag == 'footer'))
        self.assertIn('第一段 粗体、斜体、变量。', ''.join(parsed.text))
        self.assertIn('第二段 链接。', ''.join(parsed.text))

    def test_code_and_aligned_table_keep_normal_structured_blocks(self):
        source = ('普通进度。\n\n## 结构\n\n```python\nprint("<x>")\n```\n\n'
                  '| 左 | 中 | 右 |\n| :--- | :---: | ---: |\n| **甲** | `乙` | 丙 |')
        parsed = ParsedHTML(render_footer_markdown(source))
        self.assertEqual(parsed.tags().count('footer'), 1)
        self.assertTrue({'h2', 'pre', 'code', 'table', 'tr', 'th', 'td'} <= set(parsed.tags()))
        self.assertFalse({'thead', 'tbody'} & set(parsed.tags()))
        self.assertTrue(all('footer' not in path for tag, _, path in parsed.nodes if tag in ('pre', 'table')))
        cells = [attrs for tag, attrs, _ in parsed.nodes if tag in ('th', 'td')]
        self.assertEqual({attrs.get('align') for attrs in cells}, {'left', 'center', 'right'})
        self.assertTrue(all('style' not in attrs for attrs in cells))
        self.assertIn('print("<x>")', ''.join(parsed.text))

    def test_lists_and_nested_quotes_keep_their_own_structure(self):
        source = '开头段落。\n\n- 一项\n- 二项\n\n> 引用正文\n>\n> > 嵌套引用\n\n结尾段落。'
        parsed = ParsedHTML(render_footer_markdown(source))
        self.assertEqual(parsed.tags().count('footer'), 2)
        self.assertEqual(parsed.tags().count('li'), 2)
        self.assertEqual(parsed.tags().count('blockquote'), 2)
        self.assertTrue(all(not path for tag, _, path in parsed.nodes if tag == 'footer'))
        self.assertIn('一项', ''.join(parsed.text))
        self.assertIn('嵌套引用', ''.join(parsed.text))

    def test_single_newlines_stay_visible_in_a_small_text_paragraph(self):
        parsed = ParsedHTML(render_footer_markdown('第一行\n第二行  \n第三行'))
        self.assertEqual(parsed.tags().count('footer'), 1)
        self.assertEqual(parsed.tags().count('br'), 2)
        self.assertIn('第一行', ''.join(parsed.text))
        self.assertIn('第三行', ''.join(parsed.text))

    def test_raw_html_is_text_and_cannot_inject_media_or_controls(self):
        source = '</footer><script>alert(1)</script>\n\n<tg-button type="callback_data" data="x">假的按钮</tg-button>'
        rendered = render_footer_markdown(source)
        parsed = ParsedHTML(rendered)
        self.assertFalse({'script', 'tg-button', 'img'} & set(parsed.tags()))
        self.assertIn('<script>alert(1)</script>', ''.join(parsed.text))
        self.assertIn('假的按钮', ''.join(parsed.text))

    def test_unsafe_urls_do_not_become_links_but_normal_links_remain(self):
        source = ('[坏链接](javascript:alert%281%29) [另一个](vbscript:msgbox%281%29) '
                  '[本地](file:///etc/passwd) [数据](data:text/html;base64,ZXhhbXBsZQ==) '
                  '[网页](https://example.com) [邮箱](mailto:reader@example.com)')
        parsed = ParsedHTML(render_footer_markdown(source))
        links = [attrs['href'] for tag, attrs, _ in parsed.nodes if tag == 'a']
        self.assertEqual(set(links), {'https://example.com', 'mailto:reader@example.com'})
        self.assertIn('坏链接', ''.join(parsed.text))

    def test_image_references_are_readable_text_not_preview_media(self):
        parsed = ParsedHTML(render_footer_markdown('说明 ![图表 ALT](https://example.com/chart.png) 仍然可读。'))
        self.assertFalse({'img', 'video', 'audio', 'tg-document'} & set(parsed.tags()))
        self.assertIn('图表 ALT', ''.join(parsed.text))
        self.assertIn('说明', ''.join(parsed.text))


class FooterPreviewTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = harness.PersistentStatusTests.asyncSetUp
    asyncTearDown = harness.PersistentStatusTests.asyncTearDown
    restart = harness.PersistentStatusTests.restart
    delivery = harness.PersistentStatusTests.delivery
    turn = harness.PersistentStatusTests.turn
    identity = harness.PersistentStatusTests.identity
    output = harness.PersistentStatusTests.output
    update = harness.PersistentStatusTests.update
    calls_for = harness.PersistentStatusTests.calls_for
    sends = harness.PersistentStatusTests.sends
    drain = harness.PersistentStatusTests.drain
    assert_status_keyboard = harness.PersistentStatusTests.assert_status_keyboard
    keyboard_text = harness.PersistentStatusTests.keyboard_text

    def mode(self, name='a', style='footer'):
        self.bridges[name].config['preview_body_style'] = style
        return self.restart(name)

    def state(self, name='a', tid='a-work'):
        return self.delivery(name).body_preview.load(self.identity(tid=tid, name=name))

    def message(self, name='a', tid='a-work'):
        return self.messages[self.state(name, tid)['message_id']]

    def preview_html(self, name='a', tid='a-work'):
        return self.message(name, tid)['rich_message']['html']

    @staticmethod
    def controls(message):
        return [button for row in message['reply_markup']['inline_keyboard']
                for button in row if 'callback_data' in button]

    async def test_footer_changes_only_preview_rendering_and_preserves_raw_body_and_stop(self):
        self.mode()
        body = ('已完成 **读取**。\n\n```text\n原代码不变\n```\n\n'
                '| 项目 | 状态 |\n| --- | --- |\n| 文件 | 正常 |')
        self.output(body)
        await self.update()
        parsed = ParsedHTML(self.preview_html())
        self.assertTrue({'footer', 'pre', 'table'} <= set(parsed.tags()))
        self.assertEqual(self.state()['desired_text'], body)
        self.assertEqual(self.state()['sent_text'], body)
        self.assertEqual(self.delivery()._body(self.identity()), body)
        self.assertEqual(self.state()['sent_body_style'], 'footer')
        self.assertEqual(self.state()['sent_body_style_requested'], 'footer')
        self.assertEqual(self.state()['sent_body_format_requested'], 'rich')
        self.assertEqual(len(self.sends()), 1)
        self.assertIs(self.sends()[0][1]['disable_notification'], True)
        self.assert_status_keyboard(self.message(), '正在工作')
        self.assertRegex(self.controls(self.message())[0]['callback_data'], r'^stop:[0-9a-f]{24}$')

    async def test_final_body_is_original_markdown_and_retains_one_completion_notification(self):
        self.mode()
        body = '正式结果 **正常显示**。\n\n| 项目 | 数量 |\n| --- | --- |\n| 测试 | 2 |'
        self.output(body)
        await self.update()
        preview_id = self.state()['message_id']
        self.delivery().seal(self.turn(status='completed'))
        rows = await self.drain()
        self.assertEqual(len(rows), 1)
        self.assertEqual(sum(not silent for _, _, silent in rows), 1)
        self.assertNotIn(preview_id, self.messages)
        final = next(iter(self.messages.values()))
        self.assertEqual(final['rich_message'], {'markdown': body + '\n\n✅ 本轮任务已完成。'})
        self.assert_status_keyboard(final, '已完成')
        self.assertEqual(self.controls(final), [])

    async def test_quote_to_footer_switch_edits_same_message_then_only_status_markup(self):
        body = '正文不变，使用另一种中途样式。'
        self.mode(style='quote')
        self.output(body)
        await self.update()
        message_id = self.state()['message_id']
        callback = self.controls(self.message())[0]['callback_data']
        self.mode()
        self.now += 15
        await self.update()
        self.assertEqual(self.state()['message_id'], message_id)
        self.assertIn('footer', ParsedHTML(self.preview_html()).tags())
        self.assertEqual(self.controls(self.message())[0]['callback_data'], callback)
        self.assertEqual(len(self.sends()), 1)
        self.assertEqual(len(self.calls_for('editMessageText')), 1)
        before = len(self.calls)
        self.now += 15
        await self.update()
        self.assertEqual([method for method, _ in self.calls[before:]], ['editMessageReplyMarkup'])
        self.mode(style='normal')
        self.now += 15
        await self.update()
        self.assertEqual(self.state()['message_id'], message_id)
        self.assertEqual(self.message()['rich_message'], {'markdown': body})
        self.assertEqual(len(self.sends()), 1)

    async def test_target_override_keeps_another_topic_normal(self):
        self.mode()
        self.bridges['b'].config['preview_body_style'] = 'footer'
        self.bridges['b'].target['preview_body_style'] = 'normal'
        self.restart('b')
        for name in ('a', 'b'):
            self.output('各话题独立排版', tid=name + '-work', name=name)
            await self.update(tid=name + '-work', name=name)
        self.assertIn('footer', ParsedHTML(self.preview_html()).tags())
        self.assertEqual(self.message('b', 'b-work')['rich_message'], {'markdown': '各话题独立排版'})

    async def test_plain_or_unsupported_rich_output_keeps_the_original_readable_body(self):
        for name, setting in (('a', 'plain'), ('b', 'unsupported')):
            self.mode(name)
            if setting == 'plain':
                self.bridges[name].store.set('output_format', 'plain')
            else:
                self.telegram.rich_supported = False
            body = '原文 **保留**\n\n```text\n不是 HTML 文本\n```'
            self.output(body, tid=name + '-work', name=name)
            await self.update(tid=name + '-work', name=name)
            message = self.message(name, name + '-work')
            self.assertNotIn('rich_message', message)
            self.assertEqual(message['text'], body)
            self.assertEqual(self.state(name, name + '-work')['sent_body_style'], 'normal')
            self.assert_status_keyboard(message)

    async def test_known_html_format_rejection_falls_back_once_to_raw_text(self):
        self.mode()
        self.failures['sendRichMessage'] = [TelegramError('sendRichMessage', 400, "can't parse rich message")]
        body = '普通 **进度**。'
        self.output(body)
        await self.update()
        self.assertEqual([method for method, _ in self.sends()], ['sendRichMessage', 'sendMessage'])
        self.assertEqual(self.message()['text'], body)
        self.assertEqual(self.state()['sent_body_style'], 'normal')
        self.assertEqual(self.state()['sent_body_style_requested'], 'footer')
        before = len(self.calls)
        self.now += 15
        await self.update()
        self.assertEqual([method for method, _ in self.calls[before:]], ['editMessageReplyMarkup'])
        self.assertEqual(len(self.messages), 1)

    async def test_local_renderer_errors_keep_normal_rich_output_without_heartbeat_retry(self):
        for name, error in (('a', ValueError('Invalid local format')), ('b', RuntimeError('Renderer unavailable'))):
            self.mode(name)
            body = '普通 **原文**，本地排版失败仍可阅读。'
            before_sends = len(self.sends())
            with patch('preview_footer.render_footer_markdown', side_effect=error) as renderer:
                self.output(body, tid=name + '-work', name=name)
                await self.update(tid=name + '-work', name=name)
                message = self.message(name, name + '-work')
                self.assertEqual(message['rich_message'], {'markdown': body})
                state = self.state(name, name + '-work')
                self.assertEqual(state['delivery'], 'sent')
                self.assertEqual(state['sent_body_style'], 'normal')
                self.assertEqual(state['sent_body_style_requested'], 'footer')
                self.assertEqual(state['sent_text'], body)
                self.assertEqual(len(self.sends()), before_sends + 1)
                before = len(self.calls)
                self.now += 15
                await self.update(tid=name + '-work', name=name)
                self.assertEqual([method for method, _ in self.calls[before:]], ['editMessageReplyMarkup'])
                self.assertEqual(renderer.call_count, 1)
                self.assertEqual(len(self.sends()), before_sends + 1)

    async def test_plain_fallback_not_modified_does_not_claim_footer_or_retry_each_heartbeat(self):
        body = '原来已经发送的正文。'
        self.output(body)
        await self.update()
        message_id = self.state()['message_id']
        self.mode()
        self.failures['editMessageText'] = [
            TelegramError('editMessageText', 400, "can't parse rich message"),
            TelegramError('editMessageText', 400, 'message is not modified'),
        ]
        self.now += 15
        await self.update()
        self.assertEqual(self.state()['message_id'], message_id)
        self.assertEqual(self.state()['delivery'], 'sent')
        self.assertEqual(self.state()['sent_body_style'], 'normal')
        self.assertEqual(self.state()['sent_body_style_requested'], 'footer')
        before = len(self.calls)
        self.now += 15
        await self.update()
        self.assertEqual([method for method, _ in self.calls[before:]], ['editMessageReplyMarkup'])
        self.assertEqual(len(self.sends()), 1)

    async def test_rate_limit_retries_identical_footer_payload_at_the_original_route(self):
        self.mode()
        self.failures['sendRichMessage'] = [ApiError(429, retry_after=30)]
        self.output('限流期间保留同一份淡色正文。')
        await self.update()
        first = copy.deepcopy(self.sends()[0][1])
        self.now += 15
        await self.update()
        self.assertEqual(len(self.sends()), 1)
        self.now += 15
        await self.update()
        second = self.sends()[-1][1]
        self.assertEqual(first['rich_message'], second['rich_message'])
        self.assertEqual((first['chat_id'], first['message_thread_id']),
                         (second['chat_id'], second['message_thread_id']))
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(len(self.sends()), 2)
        self.assert_status_keyboard(self.message())

    async def test_unknown_creation_ack_never_falls_back_or_creates_a_second_footer_message(self):
        self.mode()
        self.failures['sendRichMessage'] = [ConnectionError('Create acknowledgement unavailable')]
        self.output('不明结果保持待核对。')
        await self.update()
        self.assertEqual(self.state()['delivery'], 'uncertain')
        self.now += 30
        self.restart()
        await self.update()
        self.assertEqual([method for method, _ in self.sends()], ['sendRichMessage'])
        self.assertEqual(self.messages, {})


if __name__ == '__main__':
    unittest.main()
