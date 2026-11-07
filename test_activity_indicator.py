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
        self.assertIn('<b>━━━━ ⏳ 正在工作 ━━━━</b>', params['rich_message']['html'])
        self.assertIn('10秒 · 更新 ', self.rendered(params).splitlines()[1])
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
        self.assertIn('━━━━ ✅ 已完成 ━━━━', self.rendered(self.telegram.call.call_args.kwargs))
        self.assertIn('1分0秒 · 更新 ', self.rendered(self.telegram.call.call_args.kwargs).splitlines()[1])
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


class BottomActivityTests(unittest.IsolatedAsyncioTestCase):
    asyncTearDown = ActivityTests.asyncTearDown
    turn = ActivityTests.turn
    update = ActivityTests.update
    rendered = staticmethod(ActivityTests.rendered)

    async def asyncSetUp(self):
        await ActivityTests.asyncSetUp(self)
        self.activity.position = 'bottom'
        self.next_message = 321

        async def telegram(method, **params):
            if method in ('sendRichMessage', 'sendMessage'):
                mid = self.next_message
                self.next_message += 2
                return {'message_id': mid}
            if method == 'deleteMessage':
                return True
            return {'message_id': params['message_id']}

        self.telegram.call.side_effect = telegram
        self.send = telegram

    def content(self, mid=322, turn='native-turn', topic=42):
        with self.store.db:
            self.store.db.execute('INSERT INTO outbox(key,chat_id,text,message_id,message_thread_id) VALUES(?,?,?,?,?)',
                                  (f'turn:{turn}:content-{mid}', '123', 'actual reply', mid, topic))

    def sends(self):
        return [c for c in self.telegram.call.call_args_list if c.args[0] in ('sendRichMessage', 'sendMessage')]

    async def test_new_content_moves_card_then_deletes_old_after_confirmation_with_15_second_limit(self):
        await self.update()
        self.content()
        self.now += 4
        await self.update()
        self.assertEqual(len(self.sends()), 1)
        self.now += 12
        await self.update()
        self.assertEqual([c.args[0] for c in self.telegram.call.call_args_list],
                         ['sendRichMessage', 'sendRichMessage', 'deleteMessage'])
        self.assertEqual(self.activity.load()['message_id'], 323)
        self.assertEqual(self.telegram.call.call_args.kwargs, {'chat_id': '123', 'message_id': 321})
        self.assertTrue(self.sends()[-1].kwargs['disable_notification'])
        self.assertEqual(self.sends()[-1].kwargs['message_thread_id'], 42)
        self.assertEqual(len(self.rendered(self.sends()[-1].kwargs).splitlines()), 2)
        self.now += 16
        await self.update()
        self.assertEqual(self.telegram.call.call_args.args[0], 'editMessageText')
        self.assertEqual(self.telegram.call.call_args.kwargs['message_id'], 323)
        self.assertEqual(len(self.sends()), 2)

    async def test_late_final_output_moves_completed_card_but_unrelated_future_messages_do_not(self):
        await self.update()
        self.now += 60
        await self.update([self.turn('completed')])
        self.assertFalse(self.activity.open)
        self.content()
        self.now += 16
        await self.update([self.turn('completed')])
        self.assertEqual(self.activity.load()['message_id'], 323)
        self.assertIn('已完成', self.rendered(self.sends()[-1].kwargs))
        self.assertIn('1分10秒', self.rendered(self.sends()[-1].kwargs))
        self.assertFalse(self.activity.open)
        count = self.telegram.call.await_count
        self.content(mid=324, turn='unrelated')
        self.now += 30
        await self.update([self.turn('completed')])
        self.assertEqual(self.telegram.call.await_count, count)

    async def test_enable_does_not_repost_historical_completed_cards(self):
        self.activity.save({'turn_id': 'native-turn', 'route': ['123', 42], 'message_id': 321,
                            'open': False, 'delivery': 'sent', 'phase': 'completed', 'started_at': 900,
                            'finished_at': 960, 'updated_at': 960, 'style_version': TopicActivity.STYLE_VERSION})
        self.content()
        await self.update([self.turn('completed')])
        self.telegram.call.assert_not_called()

    async def test_ambiguous_replacement_keeps_old_card_and_is_not_sent_again_after_restart(self):
        await self.update()
        self.content()
        self.now += 16
        self.telegram.call.side_effect = ConnectionError('replacement response lost')
        with self.assertRaises(ConnectionError):
            await self.update()
        self.assertEqual(self.activity.load()['replaces']['message_id'], 321)
        self.assertEqual(self.activity.load()['delivery'], 'uncertain')
        self.assertFalse(any(c.args[0] == 'deleteMessage' for c in self.telegram.call.call_args_list))
        self.now += 30
        self.activity = TopicActivity(self.store, clock=lambda: self.now, position='bottom')
        self.telegram.call.side_effect = self.send
        await self.update()
        self.assertEqual(len(self.sends()), 2)

    async def test_replacement_rate_limit_retries_without_deleting_old_card_first(self):
        await self.update()
        self.content()
        self.now += 16
        self.telegram.call.side_effect = TelegramError('sendRichMessage', 429, 'rate limit', retry_after=30)
        with self.assertRaises(TelegramError):
            await self.update()
        self.assertFalse(any(c.args[0] == 'deleteMessage' for c in self.telegram.call.call_args_list))
        self.now += 16
        await self.update()
        self.assertEqual(len(self.sends()), 2)
        self.now += 15
        self.telegram.call.side_effect = self.send
        await self.update()
        self.assertEqual(self.activity.load()['message_id'], 323)
        self.assertEqual(self.telegram.call.call_args.args[0], 'deleteMessage')
        self.assertEqual(len(self.sends()), 3)

    async def test_cleanup_retry_survives_restart_without_another_replacement(self):
        await self.update()
        self.content()
        self.now += 16

        async def throttled_delete(method, **params):
            if method == 'deleteMessage':
                raise TelegramError(method, 429, 'rate limit', retry_after=30)
            return await self.send(method, **params)

        self.telegram.call.side_effect = throttled_delete
        await self.update()
        self.assertEqual(self.activity.load()['message_id'], 323)
        self.assertEqual(len(self.activity.load()['cleanup']), 1)
        self.now += 31
        self.activity = TopicActivity(self.store, clock=lambda: self.now, position='bottom')
        self.telegram.call.side_effect = self.send
        await self.update()
        self.assertEqual(self.activity.load()['cleanup'], [])
        self.assertEqual(len(self.sends()), 2)

    async def test_other_topics_cannot_move_this_topic_card(self):
        await self.update()
        self.content(topic=99)
        self.now += 16
        await self.update()
        self.assertEqual(len(self.sends()), 1)
        self.assertEqual(self.telegram.call.call_args.args[0], 'editMessageText')
        await self.update(topic_id=99)
        self.assertEqual(len(self.sends()), 2)
        self.assertEqual(self.sends()[-1].kwargs['message_thread_id'], 99)
        self.assertFalse(any(c.args[0] == 'deleteMessage' for c in self.telegram.call.call_args_list))

    async def test_incoming_topic_message_also_moves_active_card_without_dispatching_input(self):
        from topics_bridge import RoutedStore
        self.store.db.close()
        self.store = RoutedStore(self.path / 'queue.sqlite', 'native')
        self.activity = TopicActivity(self.store, clock=lambda: self.now, position='bottom')
        await self.update()
        self.store.accept({'update_id': 1}, {'message_id': 322, 'message_thread_id': 42,
                                           'chat': {'id': 123}, 'text': '新的用户消息'})
        self.now += 16
        await self.update()
        self.assertEqual(self.activity.load()['message_id'], 323)
        self.assertEqual(self.store.pending()['text'], '新的用户消息')

    async def test_multiple_content_chunks_trigger_one_move_and_photo_counts_as_content(self):
        await self.update()
        for mid in (322, 324, 326):
            self.content(mid=mid)
        self.next_message = 327
        self.now += 16
        await self.update()
        self.assertEqual(self.activity.load()['message_id'], 327)
        self.assertEqual(len(self.sends()), 2)
        self.content(mid=328)
        with self.store.db:
            self.store.db.execute('UPDATE outbox SET media_json=? WHERE message_id=328', ('{}',))
        self.next_message = 329
        self.now += 16
        await self.update()
        self.assertEqual(self.activity.load()['message_id'], 329)
        self.assertEqual(len(self.sends()), 3)

    async def test_following_turn_keeps_pending_cleanup_record(self):
        await self.update()
        self.content()
        self.now += 16

        async def throttled_delete(method, **params):
            if method == 'deleteMessage':
                raise TelegramError(method, 429, 'rate limit', retry_after=30)
            return await self.send(method, **params)

        self.telegram.call.side_effect = throttled_delete
        await self.update()
        await self.update([self.turn(tid='next'), self.turn('completed')])
        self.assertEqual(self.activity.load()['turn_id'], 'next')
        self.assertEqual(len(self.activity.load()['cleanup']), 1)
