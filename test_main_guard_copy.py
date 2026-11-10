import copy
import json
import unittest
from unittest.mock import patch

from message_format import utf16_size
from reply_context import render
import test_main_guard as fixtures
from topics_bridge import TopicRouter


class MainGuardCopyTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.MainGuardTests.asyncSetUp
    asyncTearDown = fixtures.MainGuardTests.asyncTearDown
    message = fixtures.MainGuardTests.message
    send = fixtures.MainGuardTests.send
    pump = fixtures.MainGuardTests.pump
    click = fixtures.MainGuardTests.click
    callback = fixtures.MainGuardTests.callback
    inbox = fixtures.MainGuardTests.inbox
    assert_held = fixtures.MainGuardTests.assert_held
    confirm = fixtures.MainGuardTests.confirm

    def copies(self, key='instagram'):
        return self.router.bridges[key].store.db.execute(
            "SELECT * FROM outbox WHERE key LIKE 'main-guard-input:%' ORDER BY rowid").fetchall()

    async def test_target_selection_copies_exact_user_text_only_to_the_selected_topic(self):
        body = '  请检查这个项目\n**保留原文** ![图](/private/user.png)\n'
        update, panel = await self.send(body)
        self.assertEqual(self.copies(), [])
        await self.click(panel, 'Instagram')
        rows = self.copies()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['text'], '📩 你在 All 说：\n' + body)
        self.assertEqual((rows[0]['chat_id'], rows[0]['message_thread_id']), ('123', 22))
        self.assertTrue(rows[0]['rich_text'].startswith('>'))
        self.assertIsNone(rows[0]['media_json'])
        self.assertEqual(self.copies('meeting'), [])
        self.assertEqual([(r['id'], r['text']) for r in self.inbox()], [(update['update_id'], body)])
        self.assertEqual(self.native_calls, [])

    async def test_cancel_and_stale_target_selection_never_copy_user_text(self):
        _, panel = await self.send('这条取消')
        stale = self.callback(panel, 'Instagram')
        await self.click(panel, '取消')
        self.router.ingest(stale)
        await self.pump()
        self.assertEqual(self.copies(), [])
        self.assertEqual(self.copies('meeting'), [])
        self.assert_held()

    async def test_binding_changed_before_selection_does_not_copy_to_either_topic(self):
        _, panel = await self.send('不能跟随改变后的目标')
        self.router.targets['instagram']['message_thread_id'] = 999
        await self.click(panel, 'Instagram')
        self.assertEqual(self.copies(), [])
        self.assertEqual(self.copies('meeting'), [])
        self.assert_held()

    async def test_duplicate_target_click_and_recovery_preserve_the_already_sent_copy(self):
        update, panel = await self.send('只出现一次')
        selection = self.callback(panel, 'Instagram')
        self.router.ingest(selection)
        await self.pump()
        store = self.router.bridges['instagram'].store
        with store.db:
            store.db.execute('UPDATE outbox SET message_id=777 WHERE key=?', (self.copies()[0]['key'],))
        self.sequence += 1
        duplicate = copy.deepcopy(selection)
        duplicate['update_id'] = self.sequence
        self.router.ingest(duplicate)
        self.router.ingest(copy.deepcopy(update))
        await self.pump()
        self.router.picker.guard.recover()
        self.router.picker.guard.recover()
        self.assertEqual(len(self.inbox()), 1)
        self.assertEqual(len(self.copies()), 1)
        self.assertEqual(self.copies()[0]['message_id'], 777)

    async def test_restart_after_child_accept_before_copy_recovers_one_copy_without_new_input(self):
        _, panel = await self.send('输入已落盘，显示尚未落盘')
        child = self.router.bridges['instagram']
        with patch.object(child.store, 'enqueue_at', side_effect=OSError('copy interrupted')):
            await self.click(panel, 'Instagram')
        self.assertEqual(len(self.inbox()), 1)
        self.assertEqual(self.copies(), [])
        self.assertEqual(self.router.store.db.execute('SELECT state FROM main_guard_drafts').fetchone()[0], 'releasing')
        original = self.router
        reloaded = TopicRouter(copy.deepcopy(self.config), self.path)
        reloaded.bot_id, reloaded.telegram = 444, self.telegram
        self.router = reloaded
        try:
            await reloaded.picker.recover()
            await reloaded.picker.recover()
            self.assertEqual(len(self.inbox()), 1)
            self.assertEqual(len(self.copies()), 1)
            self.assertEqual(self.copies()[0]['text'], '📩 你在 All 说：\n输入已落盘，显示尚未落盘')
            self.assertEqual(reloaded.store.db.execute('SELECT state FROM main_guard_drafts').fetchone()[0], 'queued')
        finally:
            for bridge in reloaded.bridges.values():
                bridge.store.db.close()
            reloaded.store.db.close()
            self.router = original

    async def test_failure_after_copy_commit_does_not_queue_it_again(self):
        _, panel = await self.send('显示已落盘，确认步骤中断')
        with patch.object(self.router.recent, 'record', side_effect=OSError('confirmation interrupted')):
            await self.click(panel, 'Instagram')
        self.assertEqual(len(self.copies()), 1)
        self.router.picker.guard.recover()
        self.router.picker.guard.recover()
        self.assertEqual(len(self.copies()), 1)
        self.assertEqual(len(self.inbox()), 1)

    async def test_copy_uses_saved_readable_quote_without_changing_native_input(self):
        update, panel = await self.send('翻译选中部分', reply_to_message={
            'message_id': 80, 'text': '包含选中部分的原始正文'}, quote={'text': '选中部分', 'position': 2})
        update['message']['reply_to_message']['text'] = '后来修改的调用方对象'
        await self.confirm(panel)
        row = self.inbox()[0]
        context = json.loads(row['reply_json'])
        expected = '📩 你在 All 说：\n翻译选中部分\n\n' + render(context)
        self.assertEqual(self.copies()[0]['text'], expected)
        self.assertIn('> 选中部分', expected)
        self.assertIn('包含选中部分的原始正文', expected)
        self.assertNotIn('后来修改', expected)
        self.assertNotIn('reply_to_message_id', expected)
        self.assertEqual(row['text'], '翻译选中部分')
        self.assertEqual(len(self.inbox()), 1)

    async def test_long_unicode_copy_preserves_raw_and_quotes_every_continuation(self):
        body = '第一行\n' + '😀 **原文** ' * 900 + '\n最后一行\n'
        _, panel = await self.send(body)
        await self.confirm(panel)
        rows = self.copies()
        self.assertGreater(len(rows), 1)
        self.assertEqual(''.join(r['text'] for r in rows), '📩 你在 All 说：\n' + body)
        self.assertTrue(all(utf16_size(r['text']) <= 3800 and r['rich_text'].startswith('>')
                            and utf16_size(r['rich_text']) <= 3800
                            and r['media_json'] is None and r['message_thread_id'] == 22 for r in rows))
        self.assertEqual(self.inbox()[0]['text'], body)

    async def test_album_copies_only_caption_text_and_keeps_all_native_attachments(self):
        await self.send('', caption='@instagram /stop 都只是说明', media_group_id='album',
                        photo=[{'file_id': 'one'}])
        await self.send('', media_group_id='album', photo=[{'file_id': 'two'}])
        self.assertEqual(self.copies(), [])
        self.now += 3
        await self.pump()
        panel = self.telegram.messages[self.telegram.next_id]
        await self.confirm(panel)
        self.assertEqual([r['text'] for r in self.copies()], ['📩 你在 All 说：\n@instagram /stop 都只是说明'])
        self.assertTrue(all(r['media_json'] is None for r in self.copies()))
        self.assertEqual([json.loads(r['message_json'])['photo'][0]['file_id'] for r in self.inbox()], ['one', 'two'])
        self.assertEqual(self.router.store.db.execute('SELECT COUNT(*) FROM cc_requests').fetchone()[0], 0)

    async def test_normal_topic_input_keeps_its_original_path_without_extra_copy(self):
        update = self.message('已经在目标话题的消息', topic=22)
        self.router.ingest(update)
        self.assertTrue(self.router.forward_one())
        self.assertEqual(self.copies(), [])
        self.assertEqual(self.copies('meeting'), [])
        self.assertEqual([(r['id'], r['text']) for r in self.inbox()],
                         [(update['update_id'], '已经在目标话题的消息')])
