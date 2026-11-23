import asyncio
import copy
import json
import sqlite3
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from bridge import TelegramError
from host_network import HostNetwork
from usage_card import UsageCard, snapshot, _remaining


def limits(used=20, reset=5000, minutes=10080):
    return {'rateLimitsByLimitId': {'codex': {'planType': 'prolite', 'primary': {
        'usedPercent': used, 'windowDurationMins': minutes, 'resetsAt': reset}, 'secondary': None}}}


class UsageCardTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.network = SimpleNamespace(label=lambda: '网络未知', poll=lambda: None, aclose=AsyncMock())
        self.network_patch = patch('usage_card.HostNetwork', return_value=self.network)
        self.network_patch.start()
        self.db = sqlite3.connect(':memory:')
        self.db.row_factory = sqlite3.Row
        self.db.execute('CREATE TABLE kv(key TEXT PRIMARY KEY,value TEXT)')
        self.db.execute("INSERT INTO kv VALUES('offset','0')")
        self.now, self.sequence, self.calls, self.failures = 1000, 100, [], {}
        self.native = SimpleNamespace(user_input_epoch='epoch', call=AsyncMock(return_value=limits()))
        self.bridge = SimpleNamespace(thread_id='native', codex=self.native, ready=asyncio.Event())
        self.bridge.ready.set()
        store = SimpleNamespace(db=self.db, offset=lambda: int(self.db.execute(
            "SELECT value FROM kv WHERE key='offset'").fetchone()[0]))
        self.router = SimpleNamespace(config={'root_target': 'manager', 'host_id': 'local', 'socket_path': '/unused',
            'expected_bot_username': 'fixture', 'usage_card': True}, root_target={'key': 'manager', 'thread_id': 'native'},
            owner='123', bot_id=444, username='fixture', store=store, bridges={'manager': self.bridge},
            owner_allowed=lambda: True,
            access=lambda: {'dmPolicy': 'allowlist', 'allowFrom': ['123']})

        async def telegram(method, **params):
            self.calls.append((method, copy.deepcopy(params)))
            if self.failures.get(method):
                raise self.failures[method].pop(0)
            if method in ('answerCallbackQuery', 'pinChatMessage'):
                return True
            if method == 'sendMessage':
                self.sequence += 1
                return {'message_id': self.sequence, 'chat': {'id': 123}, 'from': {'id': 444}}
            if method == 'editMessageText':
                return {'message_id': params['message_id']}
            raise AssertionError(method)

        self.router.telegram = SimpleNamespace(call=AsyncMock(side_effect=telegram))
        self.ui = UsageCard(self.router, clock=lambda: self.now)

    async def asyncTearDown(self):
        self.network_patch.stop()
        self.db.close()

    def methods(self, name):
        return [p for m, p in self.calls if m == name]

    def command(self, identity=201, **changes):
        return {'update_id': identity, 'message': {'message_id': identity, 'text': '/usage',
            'from': {'id': 123}, 'chat': {'id': 123, 'type': 'private'}, **changes}}

    def callback(self, identity=202, **changes):
        return {'update_id': identity, 'callback_query': {'id': str(identity), 'data': 'uc:refresh',
            'from': {'id': 123}, 'message': {'message_id': self.ui.state.get('message_id'),
            'from': {'id': 444}, 'chat': {'id': 123, 'type': 'private'}, 'date': 1, **changes}}}

    async def test_one_initial_read_and_quiet_main_card_without_model_turn(self):
        await self.ui.tick()
        for _ in range(4):
            await self.ui.tick()
        self.native.call.assert_awaited_once_with('account/rateLimits/read', {})
        self.assertEqual(len(self.methods('sendMessage')), 1)
        self.assertEqual(len(self.methods('pinChatMessage')), 1)
        self.assertTrue(self.methods('sendMessage')[0]['disable_notification'])
        self.assertTrue(self.methods('pinChatMessage')[0]['disable_notification'])
        self.assertNotIn('message_thread_id', self.methods('sendMessage')[0])
        text = self.methods('sendMessage')[0]['text']
        self.assertEqual(text.splitlines()[0], '████████░░ 剩余 80% · 网络未知')
        self.assertIn('████████░░ 剩余 80%', text)
        self.assertNotIn('已用', text)
        self.assertNotIn('Codex', text)
        self.assertIn('剩余 80%', text)
        self.assertNotIn('5 小时', text)

    async def test_push_updates_snapshot_and_defers_poll_without_rpc(self):
        await self.ui.tick()
        self.now += 20
        self.ui.on_account_event({'method': 'account/rateLimits/updated', 'params': limits(25), '_account_epoch': 'epoch'})
        await self.ui.tick()
        self.assertEqual(self.native.call.await_count, 1)
        self.assertIn('剩余 75%', self.ui.text())
        self.assertEqual(self.ui.state['source'], '事件推送')
        self.now += 60
        await self.ui.tick()
        self.assertEqual(self.native.call.await_count, 1)
        self.assertGreaterEqual(len(self.methods('editMessageText')), 2)
        self.now = 1020 + 900
        await self.ui.tick()
        self.assertEqual(self.native.call.await_count, 2)
        self.assertEqual(len(self.methods('sendMessage')), 1)
        self.assertEqual(len(self.methods('pinChatMessage')), 1)

    async def test_fresh_persistent_cache_survives_restart_without_read_or_new_card(self):
        await self.ui.tick()
        self.ui = UsageCard(self.router, clock=lambda: self.now)
        self.ui.recover()
        await self.ui.tick()
        self.assertEqual(self.native.call.await_count, 1)
        self.assertEqual(len(self.methods('sendMessage')), 1)
        self.assertEqual(len(self.methods('pinChatMessage')), 1)

    async def test_local_reset_countdown_edits_same_card_without_quota_request(self):
        await self.ui.tick()
        self.assertIn('重置 1h', self.ui.text())
        self.assertNotIn('香港', self.ui.text())
        self.assertNotRegex(self.ui.text(), r'\d{2}/\d{2}|\d{2}:\d{2}')
        quota_line = self.ui.text().splitlines()[0]
        mid = self.ui.state['message_id']
        self.now += 61
        await self.ui.tick()
        self.assertIn('重置 1h', self.ui.text())
        self.assertEqual(self.ui.text().splitlines()[0], quota_line)
        self.assertEqual(self.native.call.await_count, 1)
        self.assertEqual(self.ui.state['message_id'], mid)
        self.assertEqual(len(self.methods('sendMessage')), 1)
        self.assertEqual(len(self.methods('pinChatMessage')), 1)

    def test_remaining_bar_preserves_numeric_percentage_at_empty_full_and_fractional_levels(self):
        for used, bar, label in ((100, '░' * 10, '0%'), (0, '█' * 10, '100%'),
                                 (22.5, '█' * 8 + '░' * 2, '77.5%'),
                                 (99.9, '░' * 10, '0.1%'), (125, '░' * 10, '0%')):
            with self.subTest(used=used):
                self.ui.state['buckets'] = snapshot(limits(used))
                text = self.ui.text()
                self.assertEqual(text.splitlines()[0], bar + ' 剩余 ' + label + ' · 网络未知')
                self.assertNotIn('已用', text)
                self.assertNotIn('周额度', text)
        self.native.call.assert_not_awaited()

    async def test_multiple_windows_keep_short_duration_labels(self):
        data = limits(50, minutes=300)
        data['rateLimitsByLimitId']['codex']['secondary'] = {
            'usedPercent': 75, 'windowDurationMins': 10080, 'resetsAt': 6000}
        self.native.call.return_value = data
        await self.ui.tick()
        text = self.ui.text()
        self.assertEqual(text.splitlines()[0], '5h █████░░░░░ 剩余 50% · 网络未知')
        self.assertIn('5h █████░░░░░ 剩余 50%', text)
        self.assertRegex(text, r'7d [█░]{10} 剩余 25%')
        self.assertNotIn('已用', text)
        self.native.call.assert_awaited_once_with('account/rateLimits/read', {})

    async def test_root_rebinding_reuses_main_card_and_invalidates_cached_quota(self):
        await self.ui.tick()
        mid = self.ui.state['message_id']
        self.now += 61
        self.router.root_target['thread_id'] = self.bridge.thread_id = 'new-native'
        self.native.call.return_value = limits(35)
        await self.ui.tick()
        self.assertEqual(self.ui.state['identity']['thread'], 'new-native')
        self.assertIn('剩余 65%', self.ui.text())
        self.assertEqual(self.native.call.await_count, 2)
        self.assertEqual(self.ui.state['message_id'], mid)
        self.assertEqual(len(self.methods('sendMessage')), 1)
        self.assertEqual(len(self.methods('pinChatMessage')), 1)

    async def test_account_change_invalidates_old_buckets_even_when_push_queue_overflows(self):
        old = limits()
        old['rateLimitsByLimitId']['old-account-only'] = copy.deepcopy(old['rateLimitsByLimitId']['codex'])
        self.native.call.return_value = old
        await self.ui.tick()
        self.ui.on_account_event({'method': 'account/updated', 'params': {}, '_account_epoch': 'epoch'})
        for used in range(20):
            self.ui.on_account_event({'method': 'account/rateLimits/updated', 'params': limits(used),
                                      '_account_epoch': 'epoch'})
        await self.ui.tick()
        self.assertEqual(set(self.ui.state['buckets']), {'codex'})
        self.assertIn('剩余 81%', self.ui.text())
        self.assertEqual(self.native.call.await_count, 1)

    async def test_missing_setting_shows_and_pins_card_without_usage_command(self):
        self.router.config.pop('usage_card')
        self.ui = UsageCard(self.router, clock=lambda: self.now)
        await self.ui.tick()
        self.native.call.assert_awaited_once_with('account/rateLimits/read', {})
        self.assertEqual(len(self.methods('sendMessage')), 1)
        self.assertEqual(len(self.methods('pinChatMessage')), 1)
        self.assertNotIn('message_thread_id', self.methods('sendMessage')[0])
        self.assertTrue(self.methods('sendMessage')[0]['disable_notification'])
        self.assertTrue(self.methods('pinChatMessage')[0]['disable_notification'])

    async def test_saved_disabled_preference_prevents_default_or_config_enabling_card(self):
        with self.db:
            self.db.execute('INSERT INTO kv VALUES(?,?)', (UsageCard.ENABLED, 'false'))
        for configured in (None, True):
            with self.subTest(configured=configured):
                if configured is None:
                    self.router.config.pop('usage_card', None)
                else:
                    self.router.config['usage_card'] = configured
                self.ui = UsageCard(self.router, clock=lambda: self.now)
                self.ui.on_account_event({'method': 'account/rateLimits/updated', 'params': limits()})
                self.assertFalse(await self.ui.tick())
                self.native.call.assert_not_awaited()
                self.assertEqual(self.calls, [])

    async def test_usage_command_can_reenable_saved_disabled_preference(self):
        with self.db:
            self.db.execute('INSERT INTO kv VALUES(?,?)', (UsageCard.ENABLED, 'false'))
        self.ui = UsageCard(self.router, clock=lambda: self.now)
        self.assertFalse(await self.ui.tick())
        self.assertTrue(self.ui.ingest(self.command()))
        await self.ui.handle_one()
        self.native.call.assert_awaited_once_with('account/rateLimits/read', {})
        self.assertEqual(len(self.methods('sendMessage')), 1)
        self.assertEqual(len(self.methods('pinChatMessage')), 1)
        self.assertTrue(UsageCard(self.router).enabled)

    async def test_disabled_does_nothing_until_explicit_usage_command(self):
        self.router.config['usage_card'] = False
        self.ui = UsageCard(self.router, clock=lambda: self.now)
        self.assertFalse(await self.ui.tick())
        self.native.call.assert_not_awaited()
        self.assertTrue(self.ui.ingest(self.command()))
        await self.ui.handle_one()
        self.assertTrue(self.ui.enabled)
        self.assertEqual(self.native.call.await_count, 1)
        self.assertEqual(UsageCard(self.router).enabled, True)

    async def test_manual_refresh_coalesces_and_respects_sixty_second_floor(self):
        await self.ui.tick()
        self.now += 20
        for identity in (301, 302):
            self.ui.ingest(self.callback(identity))
            await self.ui.handle_one()
        self.assertEqual(self.native.call.await_count, 1)
        self.now += 41
        self.ui.ingest(self.callback(303))
        await self.ui.handle_one()
        self.assertEqual(self.native.call.await_count, 2)
        self.assertEqual(len(self.methods('sendMessage')), 1)

    async def test_refresh_notice_reports_remaining_cooldown_and_eligible_query(self):
        await self.ui.tick()
        self.now += 20.2
        self.ui.ingest(self.callback(304))
        await self.ui.handle_one()
        self.assertIn('40 秒', self.methods('answerCallbackQuery')[-1]['text'])
        self.assertEqual(self.native.call.await_count, 1)
        self.now += 40.8
        self.ui.ingest(self.callback(305))
        await self.ui.handle_one()
        self.assertEqual(self.methods('answerCallbackQuery')[-1]['text'], '正在刷新额度…')
        self.assertEqual(self.native.call.await_count, 2)

    async def test_refresh_notice_distinguishes_disconnected_busy_and_disabled(self):
        await self.ui.tick()
        self.now += 61
        self.bridge.ready.clear()
        self.ui.ingest(self.callback(306))
        await self.ui.handle_one()
        self.assertIn('尚未连接', self.methods('answerCallbackQuery')[-1]['text'])
        self.bridge.ready.set()
        async with self.ui.fetch_lock:
            self.ui.ingest(self.callback(307))
            await self.ui.handle_one()
        self.assertIn('正在刷新，请稍候', self.methods('answerCallbackQuery')[-1]['text'])
        self.ui.enabled = False
        self.ui.ingest(self.callback(308))
        await self.ui.handle_one()
        self.assertIn('已关闭', self.methods('answerCallbackQuery')[-1]['text'])
        self.assertEqual(self.native.call.await_count, 1)

    async def test_explicit_refresh_repairs_failed_known_card_from_cache_during_cooldown(self):
        await self.ui.tick()
        mid = self.ui.state['message_id']
        self.now += 20
        self.ui.on_account_event({'method': 'account/rateLimits/updated', 'params': limits(30),
                                  '_account_epoch': 'epoch'})
        self.ui._drain()
        self.ui.state.update(delivery='failed', retry_at=self.now + 900, edited_at=self.now)
        self.ui._save()
        self.ui.ingest(self.callback(309))
        await self.ui.handle_one()
        self.assertEqual(self.native.call.await_count, 1)
        self.assertIn('40 秒', self.methods('answerCallbackQuery')[-1]['text'])
        self.assertEqual(self.ui.state['delivery'], 'sent')
        self.assertEqual(self.methods('editMessageText')[-1]['message_id'], mid)
        self.assertIn('剩余 70%', self.methods('editMessageText')[-1]['text'])
        self.assertEqual(len(self.methods('sendMessage')), 1)
        self.assertEqual(len(self.methods('pinChatMessage')), 1)

    async def test_usage_command_repairs_failed_known_card_even_when_text_is_unchanged(self):
        await self.ui.tick()
        mid, sent = self.ui.state['message_id'], self.ui.state['sent_text']
        self.now += 1
        self.ui.state.update(delivery='failed', retry_at=self.now + 900)
        self.ui._save()
        self.assertEqual(self.ui.text(), sent)
        self.ui.ingest(self.command(310))
        await self.ui.handle_one()
        self.assertEqual(self.native.call.await_count, 1)
        self.assertEqual(self.ui.state['delivery'], 'sent')
        self.assertEqual(self.methods('editMessageText')[-1]['message_id'], mid)
        self.assertEqual(len(self.methods('sendMessage')), 1)

    async def test_explicit_actions_do_not_recreate_unknown_or_unidentified_failed_cards(self):
        await self.ui.tick()
        initial = copy.deepcopy(self.ui.state)
        for index, (status, mid) in enumerate((('uncertain', initial['message_id']),
                                              ('creating', initial['message_id']),
                                              ('failed', None), ('failed', True))):
            for action in ('command', 'callback'):
                with self.subTest(status=status, message_id=mid, action=action):
                    self.ui.state = {**copy.deepcopy(initial), 'delivery': status}
                    if mid is None:
                        self.ui.state.pop('message_id')
                    else:
                        self.ui.state['message_id'] = mid
                    identity = 320 + index * 2 + (action == 'callback')
                    update = (self.command(identity) if action == 'command' else
                              self.callback(identity, message_id=initial['message_id']))
                    self.ui.ingest(update)
                    await self.ui.handle_one()
                    self.assertEqual(self.ui.state['delivery'], status)
                    self.assertEqual(len(self.methods('sendMessage')), 1)
                    self.assertEqual(self.methods('editMessageText'), [])

    async def test_callback_ack_failure_does_not_block_query_or_known_card_repair(self):
        await self.ui.tick()
        mid = self.ui.state['message_id']
        for index, error in enumerate((TelegramError('answerCallbackQuery', 400, 'query is too old'),
                                       ConnectionError('popup unavailable'))):
            self.now += 61
            self.ui.state.update(delivery='failed')
            self.ui._save()
            self.failures['answerCallbackQuery'] = [error]
            identity = 340 + index
            self.ui.ingest(self.callback(identity))
            await self.ui.handle_one()
            self.assertEqual(self.native.call.await_count, index + 2)
            self.assertEqual(self.ui.state['sample_at'], self.now)
            self.assertEqual(self.ui.state['delivery'], 'sent')
            self.assertEqual(self.methods('editMessageText')[-1]['message_id'], mid)
            self.assertEqual(self.db.execute('SELECT state FROM usage_jobs WHERE id=?',
                                            (identity,)).fetchone()[0], 'done')
        self.assertEqual(len(self.methods('sendMessage')), 1)

    async def test_unusable_push_does_not_discard_valid_inflight_quota_read(self):
        await self.ui.tick()
        self.now += 61
        sequence = self.ui.event_sequence

        async def read(*args):
            for payload in ({}, {'rateLimits': {'primary': {'usedPercent': True,
                                                            'windowDurationMins': 10080}}}):
                self.ui.on_account_event({'method': 'account/rateLimits/updated', 'params': payload,
                                          '_account_epoch': 'epoch'})
            return limits(30)

        self.native.call.side_effect = read
        self.assertTrue(await self.ui._fetch(force=True))
        self.ui._drain()
        self.assertEqual(self.ui.event_sequence, sequence)
        self.assertEqual(self.ui.state['sample_at'], self.now)
        self.assertIn('剩余 70%', self.ui.text())

    async def test_simultaneous_refreshes_make_only_one_rpc(self):
        await self.ui.tick()
        self.now += 61
        entered, release = asyncio.Event(), asyncio.Event()

        async def read(*args):
            entered.set()
            await release.wait()
            return limits(30)

        self.native.call.side_effect = read
        first = asyncio.create_task(self.ui._fetch(force=True))
        await entered.wait()
        self.assertFalse(await self.ui._fetch(force=True))
        release.set()
        await first
        self.assertEqual(self.native.call.await_count, 2)

    async def test_account_change_discards_inflight_old_account_read(self):
        await self.ui.tick()
        self.now += 61
        entered, release = asyncio.Event(), asyncio.Event()

        async def read(*args):
            entered.set()
            await release.wait()
            return limits(99)

        self.native.call.side_effect = read
        work = asyncio.create_task(self.ui._fetch(force=True))
        await entered.wait()
        self.ui.on_account_event({'method': 'account/updated', 'params': {}, '_account_epoch': 'epoch'})
        release.set()
        self.assertFalse(await work)
        await self.ui.tick()
        self.assertIsNone(self.ui.state['buckets'])
        self.assertNotIn('剩余 1%', self.ui.text())
        self.assertEqual(self.native.call.await_count, 2)

    async def test_newer_push_wins_over_inflight_read_and_old_epoch_is_ignored(self):
        await self.ui.tick()
        self.now += 61

        async def read(*args):
            self.ui.on_account_event({'method': 'account/rateLimits/updated', 'params': limits(42), '_account_epoch': 'epoch'})
            return limits(99)

        self.native.call.side_effect = read
        await self.ui._fetch(force=True)
        self.ui._drain()
        self.assertIn('剩余 58%', self.ui.text())
        self.ui.on_account_event({'method': 'account/rateLimits/updated', 'params': limits(88), '_account_epoch': 'old'})
        self.ui._drain()
        self.assertNotIn('剩余 12%', self.ui.text())

    async def test_reset_passage_does_not_invent_refilled_quota(self):
        self.native.call.return_value = limits(reset=1100)
        await self.ui.tick()
        self.now = 1101
        await self.ui.tick()
        self.assertIn('上次记录', self.ui.text())
        self.assertIn('等待同步', self.ui.text())
        self.assertIn('剩余 80%', self.ui.text())
        self.assertEqual(self.native.call.await_count, 1)

    async def test_failed_automatic_reads_back_off_for_fifteen_minutes(self):
        self.native.call.side_effect = ConnectionError('unavailable')
        await self.ui.tick()
        self.now += 600
        await self.ui.tick()
        self.assertEqual(self.native.call.await_count, 1)
        self.now += 300
        await self.ui.tick()
        self.assertEqual(self.native.call.await_count, 2)

    async def test_unknown_send_and_pin_are_not_repeated_after_restart(self):
        self.failures['sendMessage'] = [ConnectionError('unknown acknowledgement')]
        await self.ui.tick()
        self.ui = UsageCard(self.router, clock=lambda: self.now)
        self.ui.recover()
        await self.ui.tick()
        self.assertEqual(len(self.methods('sendMessage')), 1)
        self.assertEqual(self.ui.state['delivery'], 'uncertain')
        self.now += 16
        self.ui.state.pop('delivery')
        self.ui._save()
        self.failures['pinChatMessage'] = [ConnectionError('unknown pin acknowledgement')]
        await self.ui.tick()
        self.ui = UsageCard(self.router, clock=lambda: self.now)
        self.ui.recover()
        await self.ui.tick()
        self.assertEqual(len(self.methods('pinChatMessage')), 1)
        self.assertEqual(self.ui.state['pin_state'], 'uncertain')

    async def test_telegram_rate_limit_waits_and_deleted_card_requires_explicit_repair(self):
        self.failures['sendMessage'] = [TelegramError('sendMessage', 429, 'Too Many Requests', retry_after=30)]
        await self.ui.tick()
        self.now += 29
        await self.ui.tick()
        self.assertEqual(len(self.methods('sendMessage')), 1)
        self.now += 1
        await self.ui.tick()
        self.assertEqual(len(self.methods('sendMessage')), 2)
        self.now += 60
        self.failures['editMessageText'] = [TelegramError('editMessageText', 400, 'message to edit not found')]
        await self.ui.tick()
        self.assertEqual(self.ui.state['delivery'], 'deleted')
        await self.ui.tick()
        self.assertEqual(len(self.methods('sendMessage')), 2)
        self.ui.ingest(self.command(401))
        await self.ui.handle_one()
        self.assertEqual(len(self.methods('sendMessage')), 3)
        self.assertEqual(self.native.call.await_count, 1)

    async def test_stale_foreign_and_thread_callbacks_cannot_refresh(self):
        await self.ui.tick()
        self.now += 61
        for identity, changes in [(501, {'message_id': 999}), (502, {'from': {'id': 555}}),
                                   (503, {'message_thread_id': 42})]:
            self.ui.ingest(self.callback(identity, **changes))
            await self.ui.handle_one()
        self.assertEqual(self.native.call.await_count, 1)

    def test_malformed_windows_are_unknown_and_snapshot_keeps_no_extra_fields(self):
        data = limits()
        data.update(email='private', token='secret')
        normalized = snapshot(data)
        self.assertNotIn('private', json.dumps(normalized))
        self.assertNotIn('secret', json.dumps(normalized))
        data['rateLimitsByLimitId']['codex']['primary']['usedPercent'] = True
        self.assertIsNone(snapshot(data))

    def test_reset_countdown_uses_only_days_and_hours(self):
        for seconds, expected in [(1, '<1h'), (3599, '<1h'), (3600, '1h'),
                                  (86399, '23h'), (86400, '1d 0h'),
                                  (4 * 86400 + 7 * 3600 + 1800, '4d 7h')]:
            with self.subTest(seconds=seconds):
                self.assertEqual(_remaining(seconds), expected)

    async def test_wifi_changes_edit_same_card_without_an_additional_quota_request(self):
        provider = AsyncMock(return_value={'type': 'wifi', 'ssid': 'CUHK1x'})
        network = self.ui.network = HostNetwork(provider=provider, clock=lambda: self.now)
        await network.refresh()
        await self.ui.tick()
        mid = self.ui.state['message_id']
        self.assertIn('剩余 80% · Wi-Fi CUHK1x', self.methods('sendMessage')[0]['text'].splitlines()[0])
        self.assertEqual(self.ui.text().count('CUHK1x'), 1)
        self.now += 61
        provider.return_value = {'type': 'wifi', 'ssid': 'New WiFi'}
        await network.refresh()
        await self.ui.tick()
        self.assertEqual(self.ui.state['message_id'], mid)
        self.assertIn('Wi-Fi New WiFi', self.methods('editMessageText')[-1]['text'].splitlines()[0])
        self.assertNotIn('CUHK1x', self.ui.text())
        self.assertEqual(len(self.methods('sendMessage')), 1)
        self.native.call.assert_awaited_once_with('account/rateLimits/read', {})
        self.assertEqual(provider.await_count, 2)

    async def test_slow_network_collection_does_not_delay_usage_or_quota_controls(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def slow():
            entered.set()
            await release.wait()
            return {'type': 'wifi', 'ssid': 'eventually'}
        network = self.ui.network = HostNetwork(provider=slow, timeout=30)
        try:
            await asyncio.wait_for(self.ui.tick(), .2)
            await entered.wait()
            self.assertEqual(len(self.methods('sendMessage')), 1)
            self.native.call.assert_awaited_once_with('account/rateLimits/read', {})
            self.ui.ingest(self.callback())
            await asyncio.wait_for(self.ui.handle_one(), .2)
            self.assertTrue(self.methods('answerCallbackQuery'))
            self.assertEqual(self.native.call.await_count, 1)
        finally:
            release.set()
            await network.aclose()

    def test_network_is_displayed_once_without_quota_data(self):
        network = self.ui.network = HostNetwork()
        network.value = {'type': 'wired'}
        self.assertEqual(self.ui.text(), '等待额度同步\n有线网络')


if __name__ == '__main__':
    unittest.main()
