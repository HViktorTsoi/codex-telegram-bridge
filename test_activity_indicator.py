import html
import json
import re
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock

from activity_indicator import TopicActivity
from bridge import Store, TelegramError
from topics_bridge import TargetBridge


class ActivityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name)
        self.store = Store(self.path / 'queue.sqlite')
        self.now = 1000
        self.activity = TopicActivity(self.store, clock=lambda: self.now)
        self.telegram = AsyncMock()
        self.telegram.call.return_value = {'message_id': 321}

    async def asyncTearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    def turn(self, status='inProgress', tid='native-turn'):
        return {'id': tid, 'status': status, 'startedAt': 990,
                'completedAt': 1060 if status != 'inProgress' else None}

    async def update(self, turns=None, **extra):
        args = {'chat_id': '123', 'topic_id': 42, 'name': 'Manager',
                'turns': [self.turn()] if turns is None else turns}
        args.update(extra)
        await self.activity.update(self.telegram, **args)

    @staticmethod
    def rendered(params):
        if 'rich_message' in params:
            return html.unescape(re.sub('<[^>]+>', '', params['rich_message']['html'].replace('<br>', '\n')))
        return params['text']

    async def test_desktop_turn_without_telegram_input_creates_one_silent_card_and_edits_it(self):
        await self.update()
        method, = self.telegram.call.call_args.args
        params = self.telegram.call.call_args.kwargs
        self.assertEqual(method, 'sendRichMessage')
        self.assertEqual((params['chat_id'], params['message_thread_id']), ('123', 42))
        self.assertTrue(params['disable_notification'])
        self.assertIn('⏳', self.rendered(params))
        self.assertEqual(len(self.rendered(params).splitlines()), 2)
        self.assertIn('<b>⏳ 正在工作', params['rich_message']['html'])
        self.assertNotIn('<mark>', params['rich_message']['html'])
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM inbox').fetchone()[0], 0)
        self.now += 4
        await self.update()
        self.assertEqual(self.telegram.call.await_count, 1)
        self.now += 12
        await self.update()
        self.assertEqual(self.telegram.call.call_args.args, ('editMessageText',))
        self.assertEqual(self.telegram.call.call_args.kwargs['message_id'], 321)

    async def test_intermediate_output_is_not_completion_and_waiting_transitions_immediately(self):
        await self.update()
        await self.update(status={'type': 'active', 'activeFlags': ['waitingOnUserInput']})
        self.assertIn('需要你处理', self.rendered(self.telegram.call.call_args.kwargs))
        self.assertTrue(self.activity.open)
        await self.update(status={'type': 'active', 'activeFlags': []})
        self.assertIn('正在工作', self.rendered(self.telegram.call.call_args.kwargs))
        self.assertTrue(self.activity.open)

    async def test_completion_stop_and_failure_edit_the_original_card_and_stop_updates(self):
        for status, expected in [('completed', '已完成'), ('interrupted', '已停止'), ('failed', '执行失败')]:
            self.store.set(TopicActivity.KEY, '{}')
            await self.update()
            self.now += 60
            await self.update([self.turn(status)])
            self.assertIn(expected, self.rendered(self.telegram.call.call_args.kwargs))
            self.assertFalse(self.activity.open)
            count = self.telegram.call.await_count
            await self.update([self.turn(status)])
            self.assertEqual(self.telegram.call.await_count, count)

    async def test_idle_startup_does_not_announce_old_completed_turns(self):
        await self.update([self.turn('completed')])
        self.telegram.call.assert_not_called()

    async def test_disconnect_and_missing_history_are_unknown_and_reconnect_reuses_card(self):
        await self.update()
        self.now += 16
        self.activity = TopicActivity(self.store, clock=lambda: self.now)
        await self.update([], connected=False)
        self.assertIn('待确认', self.rendered(self.telegram.call.call_args.kwargs))
        self.assertTrue(self.activity.open)
        self.now += 16
        await self.update([])
        self.assertIn('待确认', self.rendered(self.telegram.call.call_args.kwargs))
        await self.update()
        self.assertIn('正在工作', self.rendered(self.telegram.call.call_args.kwargs))
        self.assertEqual(sum(c.args[0] in ('sendMessage', 'sendRichMessage') for c in self.telegram.call.call_args_list), 1)

    async def test_restart_after_completion_edits_existing_card_without_starting_any_native_turn(self):
        await self.update()
        self.activity = TopicActivity(self.store, clock=lambda: self.now)
        await self.update([self.turn('completed')])
        self.assertEqual(self.telegram.call.call_args.args[0], 'editMessageText')
        self.assertFalse(self.activity.open)

    async def test_following_turn_finishes_previous_card_before_creating_next(self):
        await self.update()
        await self.update([self.turn(tid='next'), self.turn('completed')])
        self.assertEqual([c.args[0] for c in self.telegram.call.call_args_list],
                         ['sendRichMessage', 'editMessageText', 'sendRichMessage'])
        self.assertIn('已完成', self.rendered(self.telegram.call.call_args_list[1].kwargs))

    async def test_repaired_topic_never_edits_old_message_in_another_topic(self):
        await self.update()
        await self.update(topic_id=99)
        self.assertEqual(self.telegram.call.call_args.args[0], 'sendRichMessage')
        self.assertEqual(self.telegram.call.call_args.kwargs['message_thread_id'], 99)

    async def test_ambiguous_send_is_not_repeated_even_after_restart(self):
        self.telegram.call.side_effect = ConnectionError('response lost')
        with self.assertRaises(ConnectionError):
            await self.update()
        self.now += 30
        self.activity = TopicActivity(self.store, clock=lambda: self.now)
        await self.update()
        self.assertEqual(self.telegram.call.await_count, 1)
        await self.update([self.turn('completed')])
        self.assertFalse(self.activity.open)

    async def test_rate_limit_retries_once_after_delay_without_wrong_route(self):
        self.telegram.call.side_effect = TelegramError('sendMessage', 429, 'Too Many Requests', retry_after=30)
        with self.assertRaises(TelegramError):
            await self.update()
        self.now += 16
        await self.update()
        self.assertEqual(self.telegram.call.await_count, 1)
        self.now += 15
        self.telegram.call.side_effect = None
        await self.update()
        self.assertEqual(self.telegram.call.await_count, 2)
        self.assertEqual(self.telegram.call.call_args.kwargs['message_thread_id'], 42)

    async def test_deleted_message_is_not_recreated_and_not_modified_is_success(self):
        await self.update()
        self.now += 20
        self.telegram.call.side_effect = TelegramError('editMessageText', 400, 'message is not modified')
        await self.update()
        self.now += 20
        self.telegram.call.side_effect = TelegramError('editMessageText', 400, 'message to edit not found')
        with self.assertRaises(TelegramError):
            await self.update()
        self.now += 30
        await self.update()
        self.assertEqual(sum(c.args[0] in ('sendMessage', 'sendRichMessage') for c in self.telegram.call.call_args_list), 1)

    async def test_old_plain_card_is_upgraded_in_place_without_waiting_for_interval(self):
        self.activity.save({'turn_id': 'native-turn', 'route': ['123', 42], 'open': True,
                            'started_at': 990, 'delivery': 'sent', 'message_id': 321,
                            'phase': 'working', 'updated_at': self.now})
        await self.update()
        self.assertEqual(self.telegram.call.call_args.args, ('editMessageText',))
        self.assertEqual(self.telegram.call.call_args.kwargs['message_id'], 321)
        self.assertIn('<b>', self.telegram.call.call_args.kwargs['rich_message']['html'])
        self.assertEqual(self.activity.load()['style'], 'bold')

    async def test_completed_marked_card_is_restyled_without_new_card_or_changed_status(self):
        self.activity.save({'turn_id': 'native-turn', 'route': ['123', 42], 'open': False,
                            'started_at': 900, 'finished_at': 960, 'delivery': 'sent',
                            'message_id': 321, 'phase': 'completed', 'updated_at': 960,
                            'style': 'marked', 'style_version': 1})
        await self.update([], connected=False)
        self.assertEqual(self.telegram.call.call_args.args, ('editMessageText',))
        self.assertEqual(self.telegram.call.call_args.kwargs['message_id'], 321)
        self.assertIn('已完成 · 1分0秒', self.rendered(self.telegram.call.call_args.kwargs))
        self.assertNotIn('<mark>', self.telegram.call.call_args.kwargs['rich_message']['html'])
        self.assertFalse(self.activity.open)
        self.assertEqual(self.activity.load()['phase'], 'completed')
        count = self.telegram.call.await_count
        await self.update([])
        self.assertEqual(self.telegram.call.await_count, count)

    async def test_format_rejection_falls_back_once_and_keeps_editing_same_plain_card(self):
        self.telegram.call.side_effect = [TelegramError('sendRichMessage', 400, 'invalid HTML'), {'message_id': 321}]
        await self.update()
        self.assertEqual([c.args[0] for c in self.telegram.call.call_args_list], ['sendRichMessage', 'sendMessage'])
        self.assertEqual(self.telegram.call.call_args.kwargs['message_thread_id'], 42)
        self.assertEqual(self.activity.load()['style'], 'plain')
        self.now += 16
        self.telegram.call.side_effect = None
        await self.update()
        self.assertEqual(self.telegram.call.call_args.args, ('editMessageText',))
        self.assertEqual(self.telegram.call.call_args.kwargs['message_id'], 321)
        self.assertIn('text', self.telegram.call.call_args.kwargs)

    async def test_old_card_format_rejection_never_creates_replacement(self):
        self.activity.save({'turn_id': 'native-turn', 'route': ['123', 42], 'open': True,
                            'started_at': 990, 'delivery': 'sent', 'message_id': 321})
        self.telegram.call.side_effect = [TelegramError('editMessageText', 400, 'rich_message unsupported'), {'message_id': 321}]
        await self.update()
        self.assertEqual([c.args[0] for c in self.telegram.call.call_args_list], ['editMessageText', 'editMessageText'])
        self.assertTrue(all(c.kwargs['message_id'] == 321 for c in self.telegram.call.call_args_list))

    async def test_missing_topic_is_not_retried_as_plain_text(self):
        self.telegram.call.side_effect = TelegramError('sendRichMessage', 400, 'message thread not found')
        with self.assertRaises(TelegramError):
            await self.update()
        self.assertEqual(self.telegram.call.await_count, 1)
        self.now += 30
        await self.update()
        self.assertEqual(self.telegram.call.await_count, 1)

    async def test_bridge_pilot_is_per_target_and_reads_native_state_without_generation(self):
        (self.path / 'access.json').write_text(json.dumps({'dmPolicy': 'allowlist', 'allowFrom': ['123']}))
        target = {'key': 'manager', 'name': 'Manager', 'thread_id': 'existing',
                  'message_thread_id': 42, 'activity_indicator': True}
        config = {'thread_id': 'existing', 'owner_id': '123', 'telegram_state_dir': str(self.path),
                  'socket_path': '/unused'}
        (self.path / 'manager').mkdir()
        bridge = TargetBridge(config, self.path / 'manager', target, lambda: '')
        try:
            bridge.telegram = self.telegram
            bridge.codex.call = AsyncMock(return_value={'thread': {'id': 'existing',
                                      'status': {'type': 'active', 'activeFlags': []}}})
            bridge.ready.set()
            await bridge.update_activity([self.turn()])
            bridge.codex.call.assert_awaited_once_with('thread/read', {'threadId': 'existing'})
            bridge.target['activity_indicator'] = False
            self.telegram.reset_mock()
            await bridge.update_activity([self.turn()])
            self.telegram.call.assert_not_called()
            bridge.target['activity_indicator'] = True
            (self.path / 'access.json').write_text(json.dumps({'dmPolicy': 'allowlist', 'allowFrom': []}))
            await bridge.update_activity([self.turn()])
            self.telegram.call.assert_not_called()
        finally:
            bridge.store.db.close()
