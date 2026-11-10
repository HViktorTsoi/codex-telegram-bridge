"""A quoted live preview must not alter canonical output or task controls."""
import copy
import unittest

from bridge import TelegramError
from test_completion_delivery import ApiError
import test_persistent_status as harness


class QuotePreviewTests(unittest.IsolatedAsyncioTestCase):
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
    body_text = staticmethod(harness.PersistentStatusTests.body_text)

    def quote_mode(self, name='a', style='quote'):
        self.bridges[name].config['preview_body_style'] = style
        return self.restart(name)

    def preview_state(self, name='a', tid='a-work'):
        return self.delivery(name).body_preview.load(self.identity(tid=tid, name=name))

    def preview_message(self, name='a', tid='a-work'):
        return self.messages[self.preview_state(name, tid)['message_id']]

    @staticmethod
    def quote(text):
        return '\n'.join('> ' + line for line in text.split('\n'))

    @staticmethod
    def controls(message):
        return [button for row in message['reply_markup']['inline_keyboard']
                for button in row if 'callback_data' in button]

    async def test_quote_wraps_every_markdown_line_but_retains_raw_body_and_controls(self):
        self.quote_mode()
        body = ('## 进度\n\n已完成 **校验**。\n\n```python\nprint("保留缩进")\n```\n\n'
                '| 项目 | 结果 |\n| --- | --- |\n| 文件 | 正常 |\n\n> 原来的引用\n')
        self.output(body)
        await self.update()
        message = self.preview_message()
        self.assertEqual(message['rich_message']['markdown'], self.quote(body))
        self.assertEqual(self.preview_state()['desired_text'], body)
        self.assertEqual(self.preview_state()['sent_text'], body)
        self.assertEqual(self.delivery()._body(self.identity()), body)
        self.assertEqual(len(self.sends()), 1)
        self.assert_status_keyboard(message, '正在工作')
        callback = self.controls(message)[0]['callback_data']
        self.assertRegex(callback, r'^stop:[0-9a-f]{24}$')
        self.assertIs(self.sends()[0][1]['disable_notification'], True)

    async def test_final_message_stays_unquoted_and_cleans_the_quoted_preview(self):
        self.quote_mode()
        body = '结果 **保留 Markdown**。\n\n| 项目 | 数量 |\n| --- | --- |\n| 测试 | 2 |'
        self.output(body)
        await self.update()
        preview_id = self.preview_state()['message_id']
        self.assertEqual(self.body_text(self.preview_message()), self.quote(body))
        self.delivery().seal(self.turn(status='completed'))
        rows = await self.drain()
        self.assertEqual(len(rows), 1)
        self.assertEqual(sum(not silent for _, _, silent in rows), 1)
        self.assertNotIn(preview_id, self.messages)
        self.assertEqual(len(self.messages), 1)
        final = next(iter(self.messages.values()))
        self.assertEqual(self.body_text(final), body + '\n\n✅ 本轮任务已完成。')
        self.assert_status_keyboard(final, '已完成')
        self.assertEqual(self.controls(final), [])

    async def test_style_switch_edits_same_body_once_then_heartbeat_changes_only_markup(self):
        body = '正文不变，切换的只是预览排版。'
        self.output(body)
        await self.update()
        message_id = self.preview_state()['message_id']
        callback = self.controls(self.preview_message())[0]['callback_data']
        self.quote_mode()
        self.now += 15
        await self.update()
        self.assertEqual(self.preview_state()['message_id'], message_id)
        self.assertEqual(self.body_text(self.preview_message()), self.quote(body))
        self.assertEqual(len(self.sends()), 1)
        self.assertEqual(len(self.calls_for('editMessageText')), 1)
        self.assertEqual(self.controls(self.preview_message())[0]['callback_data'], callback)
        before = len(self.calls)
        self.now += 15
        await self.update()
        self.assertEqual([method for method, _ in self.calls[before:]], ['editMessageReplyMarkup'])
        self.assertEqual(self.preview_state()['sent_text'], body)
        self.quote_mode(style='normal')
        self.now += 15
        await self.update()
        self.assertEqual(self.preview_state()['message_id'], message_id)
        self.assertEqual(self.body_text(self.preview_message()), body)
        self.assertEqual(len(self.calls_for('editMessageText')), 2)
        self.assertEqual(len(self.sends()), 1)

    async def test_target_override_keeps_other_body_normal_and_invalid_style_is_rejected(self):
        self.quote_mode()
        self.bridges['b'].config['preview_body_style'] = 'quote'
        self.bridges['b'].target['preview_body_style'] = 'normal'
        self.restart('b')
        for name in ('a', 'b'):
            self.output('各自会话正文', tid=name + '-work', name=name)
            await self.update(tid=name + '-work', name=name)
        self.assertEqual(self.body_text(self.preview_message()), self.quote('各自会话正文'))
        self.assertEqual(self.body_text(self.preview_message('b', 'b-work')), '各自会话正文')
        self.bridges['a'].config['preview_body_style'] = 'invalid-style'
        with self.assertRaisesRegex(ValueError, 'preview_body_style'):
            self.restart()

    async def test_plain_output_and_unavailable_rich_support_keep_readable_raw_text(self):
        for name, setting in (('a', 'plain'), ('b', 'unsupported')):
            self.quote_mode(name)
            if setting == 'plain':
                self.bridges[name].store.set('output_format', 'plain')
            else:
                self.telegram.rich_supported = False
            body = '原文\n\n```text\n不添加大于号\n```'
            self.output(body, tid=name + '-work', name=name)
            await self.update(tid=name + '-work', name=name)
            message = self.preview_message(name, name + '-work')
            self.assertNotIn('rich_message', message)
            self.assertEqual(message['text'], body)
            self.assertEqual(self.preview_state(name, name + '-work')['sent_text'], body)
            self.assert_status_keyboard(message, '正在工作')

    async def test_known_rich_rejection_falls_back_to_raw_body_without_retrying_on_heartbeat(self):
        self.quote_mode()
        self.failures['sendRichMessage'] = [TelegramError('sendRichMessage', 400, "can't parse rich message")]
        body = '**原始正文**\n第二行'
        self.output(body)
        await self.update()
        self.assertEqual([method for method, _ in self.sends()], ['sendRichMessage', 'sendMessage'])
        message_id = self.preview_state()['message_id']
        self.assertEqual(self.preview_message()['text'], body)
        self.assertNotIn('rich_message', self.preview_message())
        self.assertEqual(self.preview_state()['sent_body_style'], 'normal')
        self.assert_status_keyboard(self.preview_message())
        before = len(self.calls)
        self.now += 15
        await self.update()
        self.assertEqual([method for method, _ in self.calls[before:]], ['editMessageReplyMarkup'])
        self.assertEqual(self.preview_state()['message_id'], message_id)
        self.assertEqual(len(self.messages), 1)

    async def test_fallback_not_modified_keeps_actual_style_normal_and_does_not_loop(self):
        body = '原来的普通正文保持不变。'
        self.output(body)
        await self.update()
        message_id = self.preview_state()['message_id']
        self.quote_mode()
        self.failures['editMessageText'] = [
            TelegramError('editMessageText', 400, "can't parse rich message"),
            TelegramError('editMessageText', 400, 'message is not modified'),
        ]
        self.now += 15
        await self.update()
        state = self.preview_state()
        self.assertEqual(state['message_id'], message_id)
        self.assertEqual(state['delivery'], 'sent')
        self.assertEqual(state['sent_body_style'], 'normal')
        self.assertEqual(self.body_text(self.preview_message()), body)
        before = len(self.calls)
        self.now += 15
        await self.update()
        self.assertEqual([method for method, _ in self.calls[before:]], ['editMessageReplyMarkup'])
        self.assertEqual(len(self.sends()), 1)

    async def test_plain_to_rich_switch_quotes_unchanged_body_on_the_existing_message(self):
        self.quote_mode()
        store = self.bridges['a'].store
        store.set('output_format', 'plain')
        body = '同一正文在恢复富文本后使用引用预览。'
        self.output(body)
        await self.update()
        message_id = self.preview_state()['message_id']
        self.assertEqual(self.preview_message()['text'], body)
        store.set('output_format', 'rich')
        self.now += 15
        await self.update()
        self.assertEqual(self.preview_state()['message_id'], message_id)
        self.assertEqual(self.body_text(self.preview_message()), self.quote(body))
        self.assertEqual(len(self.sends()), 1)
        self.assertEqual(len(self.calls_for('editMessageText')), 1)
        before = len(self.calls)
        self.now += 15
        await self.update()
        self.assertEqual([method for method, _ in self.calls[before:]], ['editMessageReplyMarkup'])

    async def test_status_draft_quotes_only_its_ordinary_body_not_the_live_status(self):
        self.bridges['a'].config['preview_style'] = 'status-draft'
        self.quote_mode()
        body = '正文区分于底部状态草稿。\n\n第二段。'
        self.output(body)
        await self.update()
        self.assertEqual(self.body_text(self.preview_message()), self.quote(body))
        drafts = self.calls_for('sendRichMessageDraft')
        self.assertEqual(len(drafts), 1)
        status = drafts[0][1]['rich_message']['blocks'][0]['text']
        self.assertNotIn(body, status)
        self.assertIn('正在工作', status)
        self.assertFalse(status.startswith('>'))

    async def test_rate_limit_retries_same_quote_without_creating_two_messages(self):
        self.quote_mode()
        self.failures['sendRichMessage'] = [ApiError(429, retry_after=30)]
        self.output('限流期间保留同一份正文')
        await self.update()
        first = copy.deepcopy(self.sends()[0][1])
        self.assertEqual(len(self.messages), 0)
        self.now += 15
        await self.update()
        self.assertEqual(len(self.sends()), 1)
        self.now += 15
        await self.update()
        second = self.sends()[-1][1]
        self.assertEqual(first['rich_message'], second['rich_message'])
        self.assertEqual(first['chat_id'], second['chat_id'])
        self.assertEqual(first['message_thread_id'], second['message_thread_id'])
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(len(self.sends()), 2)
        self.assert_status_keyboard(self.preview_message())

    async def test_unknown_create_ack_never_falls_back_or_recreates_quote(self):
        self.quote_mode()
        self.failures['sendRichMessage'] = [ConnectionError('Create acknowledgement was lost')]
        self.output('结果不明时不重复创建')
        await self.update()
        self.assertEqual(self.preview_state()['delivery'], 'uncertain')
        self.assertEqual([method for method, _ in self.sends()], ['sendRichMessage'])
        self.now += 30
        self.restart()
        await self.update()
        self.assertEqual([method for method, _ in self.sends()], ['sendRichMessage'])
        self.assertEqual(self.messages, {})


if __name__ == '__main__':
    unittest.main()
