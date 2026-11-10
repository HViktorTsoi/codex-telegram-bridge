"""A new topic installation must actually use cumulative persistent previews."""
import copy
import json
from pathlib import Path
from unittest.mock import AsyncMock
import unittest

from completion_delivery import CompletionDelivery
import test_persistent_status as fixtures
from topics_bridge import TargetBridge


class DeliveryDefaultsTests(unittest.IsolatedAsyncioTestCase):
    asyncTearDown = fixtures.PersistentStatusTests.asyncTearDown
    restart = fixtures.PersistentStatusTests.restart
    delivery = fixtures.PersistentStatusTests.delivery
    turn = fixtures.PersistentStatusTests.turn
    accept = fixtures.PersistentStatusTests.accept
    pending_identity = fixtures.PersistentStatusTests.pending_identity
    identity = fixtures.PersistentStatusTests.identity
    output = fixtures.PersistentStatusTests.output
    update = fixtures.PersistentStatusTests.update
    calls_for = fixtures.PersistentStatusTests.calls_for
    sends = fixtures.PersistentStatusTests.sends
    body_text = staticmethod(fixtures.PersistentStatusTests.body_text)
    keyboard_text = fixtures.PersistentStatusTests.keyboard_text
    assert_status_keyboard = fixtures.PersistentStatusTests.assert_status_keyboard
    assert_no_drafts = fixtures.PersistentStatusTests.assert_no_drafts
    drain = fixtures.PersistentStatusTests.drain

    async def asyncSetUp(self):
        await fixtures.PersistentStatusTests.asyncSetUp(self)
        # Reuse only the display harness. This store is new: no previous mode or
        # preview state can make the first omitted-setting installation pass.
        previous = self.bridges['a']
        config, target = copy.deepcopy(previous.config), copy.deepcopy(previous.target)
        previous.store.db.close()
        for field in ('notification_mode', 'preview_style', 'status_card_pin'):
            config.pop(field, None)
            target.pop(field, None)
        config['mode'] = 'topics'
        directory = self.path / 'fresh-default-installation'
        directory.mkdir()
        bridge = TargetBridge(config, directory, target, lambda: '')
        bridge.telegram = self.telegram
        bridge.ready.set()
        self.bridges['a'] = bridge
        self.restart()

    async def test_new_installation_edits_one_cumulative_body_with_inline_status(self):
        self.accept()
        delivery, bridge = self.delivery(), self.bridges['a']
        await delivery.receipt(self.telegram)
        preview_id = delivery.body_preview.load(self.pending_identity())['message_id']
        bridge.store.mark(7, 'running', 'a-work')
        self.output('第一段公开过程')
        await self.update()
        self.output('第二段公开过程', iid='second')
        self.now += 15
        await self.update()
        self.assertEqual(len(self.sends()), 1)
        self.assertTrue(self.sends()[0][1]['disable_notification'])
        self.assertEqual(delivery.body_preview.load(self.identity())['message_id'], preview_id)
        self.assertEqual(self.body_text(self.messages[preview_id]),
                         '第一段公开过程\n\n────────────\n\n第二段公开过程')
        self.assert_status_keyboard(self.messages[preview_id], '正在工作')
        self.assertIsNone(delivery.next_row(), 'active canonical text must stay buffered')
        self.assertEqual(delivery.status_card.load(), {})
        self.assertEqual(bridge.activity.load(), {})
        self.assert_no_drafts()

    async def test_explicit_all_still_sends_the_original_item_immediately(self):
        bridge = self.bridges['a']
        bridge.config['notification_mode'] = 'all'
        delivery = self.restart()
        self.output('保留逐条发送的显式选择')
        row = delivery.next_row()
        self.assertEqual(row['text'], '保留逐条发送的显式选择')
        self.assertFalse(delivery.silent(row))
        await bridge.send_one(row)
        self.assertEqual(len(self.sends()), 1)
        self.assertNotIn('reply_markup', self.sends()[0][1])
        self.assertIsNone(delivery.next_row())

    async def test_shipped_topic_example_produces_the_persistent_inline_receipt(self):
        example = json.loads(Path(__file__).with_name('config.topics.example.json').read_text())
        bridge = self.bridges['a']
        for field in ('notification_mode', 'preview_style', 'status_card_pin'):
            if field in example:
                bridge.config[field] = example[field]
            else:
                bridge.config.pop(field, None)
        delivery = self.restart()
        self.accept()
        await delivery.receipt(self.telegram)
        self.assertEqual(len(self.sends()), 1)
        self.assert_status_keyboard(self.sends()[0][1], '已收到')
        self.assertTrue(self.sends()[0][1]['disable_notification'])
        self.assert_no_drafts()

    async def test_target_preferences_override_the_global_profile(self):
        bridge = self.bridges['a']
        bridge.config.update(notification_mode='all', preview_style='plain')
        bridge.target.update(notification_mode='completion', preview_style='status-bar')
        self.restart()
        self.output('目标明确选择累计状态栏')
        await self.update()
        self.assertIsNone(self.delivery().next_row())
        self.assert_status_keyboard(self.sends()[0][1], '正在工作')
        self.assert_no_drafts()
        other = self.bridges['b']
        other.target.update(notification_mode='all', preview_style='plain')
        self.restart('b')
        self.output('另一个目标保留逐条模式', name='b', tid='b-work')
        row = self.delivery('b').next_row()
        self.assertEqual(row['text'], '另一个目标保留逐条模式')
        self.assertFalse(self.delivery('b').silent(row))

    async def test_default_profile_retires_only_its_known_legacy_activity_card(self):
        bridge = self.bridges['a']
        old_id = 700
        self.messages[old_id] = {'message_id': old_id, 'text': '旧状态卡'}
        bridge.activity.save({'route': ['123', 42], 'open': True, 'message_id': old_id})
        bridge.config['activity_indicator'] = True
        bridge.codex.call = AsyncMock(return_value={'thread': {'id': bridge.thread_id,
                                                              'status': {'type': 'active'}}})
        self.output('迁移后正文与状态在一起')
        await bridge.update_activity([self.turn()])
        self.assertEqual(self.calls_for('deleteMessage'),
                         [('deleteMessage', {'chat_id': '123', 'message_id': old_id})])
        self.assertNotIn(old_id, self.messages)
        self.assertEqual(len(self.sends()), 1)
        self.assert_status_keyboard(self.sends()[0][1], '正在工作')
        self.assertIn('迁移后正文与状态在一起', self.body_text(self.sends()[0][1]))
        self.assert_no_drafts()
        bridge.codex.call.assert_awaited_once_with('thread/read', {'threadId': bridge.thread_id})

    async def test_default_history_baseline_and_restart_never_reannounce_results(self):
        delivery, bridge = self.delivery(), self.bridges['a']
        historical = self.turn(tid='historical', status='completed', started=1)
        await delivery.update(self.telegram, turns=[historical])
        self.assertEqual(self.calls, [])
        self.assertIsNone(delivery.next_row())
        self.accept()
        bridge.store.mark(7, 'running', 'a-work')
        self.output('本轮过程')
        await self.update()
        self.output('本轮最终回复', iid='final')
        terminal = self.turn(status='completed')
        delivery.seal(terminal)
        rows = await self.drain()
        self.assertEqual([(text, silent) for _, text, silent in rows],
                         [('本轮过程', True), ('本轮最终回复', False)])
        final = max(self.messages.values(), key=lambda message: message['message_id'])
        self.assert_status_keyboard(final, '已完成')
        before = len(self.calls)
        self.restart()
        self.now += 15
        await self.delivery().update(self.telegram, turns=[terminal, historical])
        self.assertIsNone(self.delivery().next_row())
        self.assertEqual(len(self.calls), before)
        self.assert_no_drafts()


if __name__ == '__main__':
    unittest.main()
