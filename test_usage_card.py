import asyncio
import copy
import json
import sqlite3
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from bridge import TelegramError
from usage_card import UsageCard, snapshot


def limits(used=20, reset=5000, minutes=10080):
    return {'rateLimitsByLimitId': {'codex': {'planType': 'prolite', 'primary': {
        'usedPercent': used, 'windowDurationMins': minutes, 'resetsAt': reset}, 'secondary': None}}}


class UsageCardTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
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
        self.assertIn('周额度', text)
        self.assertIn('已用 20%', text)
        self.assertIn('剩余 80%', text)
        self.assertNotIn('5 小时', text)

    async def test_push_updates_snapshot_and_defers_poll_without_rpc(self):
        await self.ui.tick()
        self.now += 20
        self.ui.on_account_event({'method': 'account/rateLimits/updated', 'params': limits(25), '_account_epoch': 'epoch'})
        await self.ui.tick()
        self.assertEqual(self.native.call.await_count, 1)
        self.assertIn('已用 25%', self.ui.text())
        self.assertIn('事件推送', self.ui.text())
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
        self.assertIn('1小时7分钟后', self.ui.text())
        mid = self.ui.state['message_id']
        self.now += 61
        await self.ui.tick()
        self.assertIn('1小时6分钟后', self.ui.text())
        self.assertEqual(self.native.call.await_count, 1)
        self.assertEqual(self.ui.state['message_id'], mid)
        self.assertEqual(len(self.methods('sendMessage')), 1)
        self.assertEqual(len(self.methods('pinChatMessage')), 1)

    async def test_root_rebinding_reuses_main_card_and_invalidates_cached_quota(self):
        await self.ui.tick()
        mid = self.ui.state['message_id']
        self.now += 61
        self.router.root_target['thread_id'] = self.bridge.thread_id = 'new-native'
        self.native.call.return_value = limits(35)
        await self.ui.tick()
        self.assertEqual(self.ui.state['identity']['thread'], 'new-native')
        self.assertIn('已用 35%', self.ui.text())
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
        self.assertIn('已用 19%', self.ui.text())
        self.assertEqual(self.native.call.await_count, 1)

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
        self.assertNotIn('99%', self.ui.text())
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
        self.assertIn('已用 42%', self.ui.text())
        self.ui.on_account_event({'method': 'account/rateLimits/updated', 'params': limits(88), '_account_epoch': 'old'})
        self.ui._drain()
        self.assertNotIn('88%', self.ui.text())

    async def test_reset_passage_does_not_invent_refilled_quota(self):
        self.native.call.return_value = limits(reset=1100)
        await self.ui.tick()
        self.now = 1101
        await self.ui.tick()
        self.assertIn('上次记录', self.ui.text())
        self.assertIn('等待同步', self.ui.text())
        self.assertIn('已用 20%', self.ui.text())
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


if __name__ == '__main__':
    unittest.main()
