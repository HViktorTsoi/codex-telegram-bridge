"""Live transcript boundaries must match the completed transcript boundaries."""
import unittest

import test_persistent_status as harness


SEPARATOR = '\n\n────────────\n\n'


class LiveMessageSeparatorTests(unittest.IsolatedAsyncioTestCase):
    # Reuse the real TargetBridge fixture, without inheriting its test methods.
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
    body_text = staticmethod(harness.PersistentStatusTests.body_text)

    async def test_live_status_bar_and_status_draft_edit_separated_cumulative_items(self):
        first = '先检查普通正文。'
        second = '## 检查结果\n\n| 项目 | 状态 |\n| --- | --- |\n| 文件 | 正常 |'
        for name, style in (('a', 'status-bar'), ('b', 'status-draft')):
            self.bridges[name].config['preview_style'] = style
            delivery = self.restart(name)
            self.output(first, tid=name + '-work', name=name)
            await self.update(tid=name + '-work', name=name)
            identity = self.identity(tid=name + '-work', name=name)
            message_id = delivery.body_preview.load(identity)['message_id']
            self.assertEqual(self.body_text(self.messages[message_id]), first)

            self.output(second, tid=name + '-work', iid='second', name=name)
            self.now += 15
            await self.update(tid=name + '-work', name=name)
            self.assertEqual(delivery.body_preview.load(identity)['message_id'], message_id)
            self.assertEqual(self.body_text(self.messages[message_id]), first + SEPARATOR + second)
            self.assertEqual(delivery._group(name + '-work')['stage'], 'active')
            topic = self.bridges[name].target['message_thread_id']
            self.assertEqual(sum(p.get('message_thread_id') == topic for _, p in self.sends()), 1)
            if style == 'status-bar':
                self.assertEqual(len(self.messages[message_id]['reply_markup']['inline_keyboard']), 2)

    async def test_split_code_and_table_item_reassembles_without_internal_separators(self):
        first = ('```python\n' + 'print("保留每个换行")\n' * 420 + '```\n\n'
                 '| 数据 | 数量 |\n| --- | --- |\n| 示例 | 42 |')
        second = '代码及表格检查完成。'
        self.output(first)
        delivery = self.delivery()
        delivery.watch(self.turn())
        delivery._sync_rows()
        group = delivery._group('a-work')
        store = self.bridges['a'].store
        rows = store.db.execute('SELECT * FROM outbox ORDER BY rowid').fetchall()
        self.assertGreater(len(rows), 1)
        self.assertEqual(delivery._body(group), first)

        self.output(second, iid='second')
        delivery._sync_rows()
        expected = first + SEPARATOR + second
        self.assertEqual(delivery._body(group), expected)
        rows = store.db.execute('SELECT * FROM outbox ORDER BY rowid').fetchall()
        self.assertEqual(delivery.batches.body(rows), expected)
        self.assertEqual(''.join(r['text'] for r in rows[:-1]), first)
        self.assertEqual(expected.count(SEPARATOR), 1)

    async def test_preview_contains_only_original_output_and_separators(self):
        store, delivery = self.bridges['a'].store, self.delivery()
        store.accept({'update_id': 7}, {'message_id': 7, 'chat': {'id': 123},
                                      'message_thread_id': 42, 'text': '用户正文，不带引用'})
        # Input and routing metadata never become part of the output transcript.
        store.enqueue_at('input:7:reply', '123', '［Telegram 回传路由］\n桥接元信息', 42)
        texts = ['普通消息一，没有额外引用。', '> 这是输出本身的引用\n\n普通消息二。']
        for index, text in enumerate(texts):
            self.output(text, iid=str(index))
        delivery.watch(self.turn())
        delivery._sync_rows()
        body = delivery._body(delivery._group('a-work'))
        self.assertEqual(body, SEPARATOR.join(texts))
        self.assertNotIn('用户正文', body)
        self.assertNotIn('Telegram 回传路由', body)


if __name__ == '__main__':
    unittest.main()
