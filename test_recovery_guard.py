"""Recovery is authenticated elsewhere and can only start one stopped receiver."""
import asyncio
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from bridge import TelegramError
from recovery_guard import Guardian, GuardianError


class GuardianTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        self.now, self.running, self.exact = 1000, False, True
        self.calls, self.errors = [], []
        self.sequence = 100
        self.config = {'mode': 'topics', 'owner_id': '123', 'telegram_state_dir': str(self.root),
                       'host_id': 'zbox', 'expected_bot_username': 'fixture_bot', 'root_target': 'manager',
                       'targets': [{'key': 'manager', 'name': 'Manager', 'thread_id': 'manager-native', 'message_thread_id': 42},
                                   {'key': 'skyrim', 'name': 'Skyrim', 'thread_id': 'skyrim-native', 'message_thread_id': 99}]}
        self.write_config()
        (self.root / '.env').write_text('TELEGRAM_BOT_TOKEN=fixture-secret\n')
        (self.root / 'access.json').write_text(json.dumps({'dmPolicy': 'allowlist', 'allowFrom': ['123']}))
        self.write_health()
        (self.root / 'control').mkdir()
        with sqlite3.connect(self.root / 'topics.sqlite') as db:
            db.execute('CREATE TABLE jobs(id INTEGER PRIMARY KEY,target_key TEXT,message_json TEXT,state TEXT,error TEXT)')
        for target in self.config['targets']:
            directory = self.root / 'targets' / target['key']
            directory.mkdir(parents=True)
            with sqlite3.connect(directory / 'bridge.sqlite') as db:
                db.execute('CREATE TABLE inbox(id INTEGER PRIMARY KEY,state TEXT,message_json TEXT)')
        test = self

        class Telegram:
            async def call(self, method, **params):
                test.calls.append((method, params))
                if method == 'getMe':
                    return {'id': 444, 'username': 'fixture_bot'}
                if method not in ('sendMessage', 'editMessageText'):
                    raise AssertionError('The guardian must never receive updates: ' + method)
                if test.errors:
                    raise test.errors.pop(0)
                test.sequence += 1
                return {'message_id': params.get('message_id', test.sequence),
                        'message_thread_id': params.get('message_thread_id'), 'chat': {'id': 123}}

        self.telegram = Telegram()

        async def start():
            self.running = True
            self.write_health(pid=101, connected=True, last_poll=self.now)

        self.start = AsyncMock(side_effect=start)
        self.wait = AsyncMock(return_value=True)
        self.guard = self.create_guard()

    async def asyncTearDown(self):
        await self.guard.close()
        self.tmp.cleanup()

    def create_guard(self, url='https://recovery.example/codex-recovery'):
        return Guardian(self.root, self.repo, url, telegram=self.telegram, clock=lambda: self.now,
                        process_probe=lambda pid: {'exists': self.running, 'exact': self.exact},
                        start_bridge=self.start, wait_healthy=self.wait)

    def write_config(self):
        (self.root / 'config.json').write_text(json.dumps(self.config))

    def write_health(self, pid=100, connected=False, last_poll=None):
        (self.root / 'health.json').write_text(json.dumps({'pid': pid, 'connected': connected,
            'updated_at': self.now, 'last_poll_at': self.now - 1 if last_poll is None else last_poll}))

    def fault(self, code=409, identity='fixture-fault', targets=None):
        data = {'id': identity, 'at': self.now, 'code': code,
                'reason': 'telegram_auth_failed' if code == 401 else 'telegram_poll_conflict', 'host_id': 'zbox',
                'targets': [{'target_key': 'skyrim', 'thread_id': 'skyrim-native', 'topic_id': 99}]
                           if targets is None else targets}
        (self.root / 'control/recovery-fault.json').write_text(json.dumps(data))

    async def card(self, code=409):
        self.fault(code)
        await self.guard.monitor_once()
        row = self.guard.db.execute('SELECT * FROM cards ORDER BY rowid LIMIT 1').fetchone()
        return self.guard._card_token(row['id']), row

    async def test_fault_card_is_original_topic_only_and_never_polls_updates(self):
        token, row = await self.card()
        sends = [(method, params) for method, params in self.calls if method == 'sendMessage']
        self.assertEqual(len(sends), 1)
        params = sends[0][1]
        self.assertEqual((params['chat_id'], params['message_thread_id']), ('123', 99))
        self.assertTrue(params['protect_content'])
        url = params['reply_markup']['inline_keyboard'][0][0]['web_app']['url']
        self.assertEqual(url, 'https://recovery.example/codex-recovery/r/' + token)
        self.assertNotIn(token, dict(row).values())
        self.now += 3
        await self.guard.monitor_once()
        self.assertEqual(sum(method == 'sendMessage' for method, _ in self.calls), 1)
        self.start.assert_not_awaited()

    async def test_parent_and_child_queue_counts_deduplicate_accepted_files(self):
        with sqlite3.connect(self.root / 'topics.sqlite') as db:
            db.execute('INSERT INTO jobs VALUES(?,?,?,?,NULL)', (7, 'skyrim', json.dumps({'document': {'file_id': 'file'}}), 'pending'))
        with sqlite3.connect(self.root / 'targets/skyrim/bridge.sqlite') as db:
            db.executemany('INSERT INTO inbox VALUES(?,?,?)', [(7, 'pending', json.dumps({'document': {}})),
                                                               (8, 'uncertain', '{}'), (9, 'running', '{}')])
        token, _ = await self.card()
        status = await self.guard.status(token)
        self.assertEqual((status['pending_messages'], status['pending_files'], status['uncertain_inputs'], status['running_inputs']), (2, 1, 1, 1))
        self.assertTrue(status['can_recover'])

    async def test_recovery_claim_starts_once_and_returns_result_to_original_topic(self):
        token, row = await self.card()
        result = await self.guard.recover(token, row['action_nonce'])
        self.assertTrue(result['ok'])
        self.assertEqual(await self.guard.recover(token, row['action_nonce']), result)
        self.start.assert_awaited_once()
        self.wait.assert_awaited_once_with(self.now, 100)
        edits = [params for method, params in self.calls if method == 'editMessageText']
        self.assertEqual(edits[-1]['message_id'], row['message_id'])
        self.assertIn('已恢复', edits[-1]['text'])
        with sqlite3.connect(self.root / 'targets/skyrim/bridge.sqlite') as db:
            self.assertEqual(db.execute('SELECT count(*) FROM inbox').fetchone()[0], 0)

    async def test_live_or_unknown_process_never_stops_or_restarts(self):
        token, row = await self.card()
        self.running = True
        self.write_health(connected=True)
        self.assertTrue((await self.guard.recover(token, row['action_nonce']))['ok'])
        self.exact = False
        with self.assertRaises(GuardianError):
            await self.guard.recover(token, row['action_nonce'])
        self.start.assert_not_awaited()

    async def test_lock_held_and_maintenance_refuse_start(self):
        token, row = await self.card()
        with patch.object(self.guard, '_lock_free', return_value=False):
            with self.assertRaises(GuardianError):
                await self.guard.recover(token, row['action_nonce'])
        (self.guard.directory / 'maintenance.json').write_text(json.dumps({'until': self.now + 300}))
        with self.assertRaises(GuardianError):
            await self.guard.recover(token, row['action_nonce'])
        self.start.assert_not_awaited()

    async def test_nonce_expiry_owner_and_binding_are_revalidated(self):
        token, row = await self.card()
        with self.assertRaises(GuardianError) as invalid:
            await self.guard.recover(token, 'wrong')
        self.assertEqual(invalid.exception.status, 403)
        self.config['targets'][1]['message_thread_id'] = 100
        self.write_config()
        with self.assertRaises(GuardianError) as moved:
            await self.guard.status(token)
        self.assertEqual(moved.exception.status, 403)
        self.config['targets'][1]['message_thread_id'] = 99
        self.write_config()
        self.now += self.guard.TTL + 1
        with self.assertRaises(GuardianError) as expired:
            await self.guard.status(token)
        self.assertEqual(expired.exception.status, 410)
        self.start.assert_not_awaited()

    async def test_auth_failure_does_not_attempt_a_restart(self):
        token, row = await self.card(401)
        self.assertFalse((await self.guard.status(token))['can_recover'])
        with self.assertRaises(GuardianError):
            await self.guard.recover(token, row['action_nonce'])
        self.start.assert_not_awaited()

    async def test_unknown_start_result_is_not_replayed_even_after_reopen(self):
        token, row = await self.card()
        self.start.side_effect = ConnectionError('private source detail')
        result = await self.guard.recover(token, row['action_nonce'])
        self.assertFalse(result['ok'])
        self.assertNotIn('private', result['message'])
        await self.guard.close()
        self.guard = self.create_guard()
        self.assertEqual(await self.guard.recover(token, row['action_nonce']), result)
        self.start.assert_awaited_once()

    async def test_uncertain_card_send_is_never_sent_again(self):
        self.errors = [ConnectionError('unknown send')]
        token, row = await self.card()
        self.assertEqual(row['state'], 'uncertain')
        self.assertEqual(self.guard.last_delivery_error, 'ConnectionError')
        self.now += 10
        await self.guard.monitor_once()
        self.assertEqual(sum(method == 'sendMessage' for method, _ in self.calls), 1)
        self.assertIsNone(self.guard.resolve_card(token)['message_id'])
        self.assertEqual(self.guard.last_delivery_error, 'ConnectionError')

    async def test_known_rate_limit_retries_after_server_cooldown(self):
        self.errors = [TelegramError('sendMessage', 429, 'wait', retry_after=20)]
        token, row = await self.card()
        self.assertEqual(row['state'], 'queued')
        self.now += 10
        await self.guard.monitor_once()
        self.assertEqual(sum(method == 'sendMessage' for method, _ in self.calls), 1)
        self.now += 11
        await self.guard.monitor_once()
        self.assertEqual(sum(method == 'sendMessage' for method, _ in self.calls), 2)
        self.assertEqual(self.guard.resolve_card(token)['state'], 'sent')

    async def test_recovered_old_fault_is_not_announced_when_healthy(self):
        self.fault()
        self.running = True
        self.write_health(connected=True, last_poll=self.now + 1)
        await self.guard.monitor_once()
        self.running = False
        self.now += 1
        self.write_health(last_poll=self.now)
        await self.guard.monitor_once()
        self.assertEqual(self.guard.db.execute('SELECT count(*) FROM incidents').fetchone()[0], 0)
        self.now += self.guard.GRACE + 1
        await self.guard.monitor_once()
        self.assertEqual(self.guard.db.execute('SELECT reason FROM incidents').fetchone()[0], 'receiver_stopped')

    async def test_manual_stop_is_quiet_but_explicit_new_fault_is_reported(self):
        (self.guard.directory / 'maintenance.json').write_text(json.dumps({'until': self.now + 300}))
        await self.guard.monitor_once()
        self.assertEqual(self.calls, [])
        self.fault()
        await self.guard.monitor_once()
        self.assertEqual(sum(method == 'sendMessage' for method, _ in self.calls), 1)

    async def test_url_rotation_edits_the_existing_card_and_preserves_nonce(self):
        token, row = await self.card()
        await self.guard.close()
        self.guard = self.create_guard('https://new.example/maintenance')
        await self.guard.monitor_once()
        latest = self.guard.resolve_card(token)
        self.assertEqual((latest['message_id'], latest['action_nonce']), (row['message_id'], row['action_nonce']))
        self.assertEqual(sum(method == 'sendMessage' for method, _ in self.calls), 1)
        edits = [params for method, params in self.calls if method == 'editMessageText']
        self.assertEqual(edits[-1]['reply_markup']['inline_keyboard'][0][0]['web_app']['url'], 'https://new.example/maintenance/r/' + token)

    async def test_cooldown_prevents_another_card_from_restarting_the_same_bot(self):
        token, row = await self.card()
        self.wait.return_value = False
        self.start.side_effect = None
        await self.guard.recover(token, row['action_nonce'])
        self.now += 1
        self.fault(identity='another-fault')
        await self.guard.monitor_once()
        second = self.guard.db.execute('SELECT * FROM cards WHERE incident_id<>?', (row['incident_id'],)).fetchone()
        second_token = self.guard._card_token(second['id'])
        self.assertFalse((await self.guard.status(second_token))['can_recover'])
        with self.assertRaises(GuardianError) as rate:
            await self.guard.recover(second_token, second['action_nonce'])
        self.assertEqual(rate.exception.status, 429)
        self.start.assert_awaited_once()

    async def test_alerts_only_needs_no_public_url_and_exposes_no_recovery_button(self):
        await self.guard.close()
        self.guard = Guardian(self.root, self.repo, alerts_only=True, telegram=self.telegram,
            clock=lambda: self.now, process_probe=lambda pid: {'exists': self.running, 'exact': self.exact},
            start_bridge=self.start, wait_healthy=self.wait)
        token, row = await self.card()
        params = next(params for method, params in self.calls if method == 'sendMessage')
        self.assertEqual(params['message_thread_id'], 99)
        self.assertEqual(params['reply_markup'], {'inline_keyboard': []})
        self.assertNotIn('https://', json.dumps(params))
        self.assertNotIn(token, json.dumps(params))
        for action in (lambda: self.guard.status(token),
                       lambda: self.guard.recover(token, row['action_nonce'])):
            with self.assertRaises(GuardianError) as denied:
                await action()
            self.assertEqual(denied.exception.status, 403)
        self.start.assert_not_awaited()

    async def test_alerts_only_edits_the_same_fault_card_after_external_recovery(self):
        await self.guard.close()
        self.guard = Guardian(self.root, self.repo, alerts_only=True, telegram=self.telegram,
            clock=lambda: self.now, process_probe=lambda pid: {'exists': self.running, 'exact': self.exact},
            start_bridge=self.start, wait_healthy=self.wait)
        _, row = await self.card()
        self.now += 5
        self.running = True
        self.write_health(pid=101, connected=True, last_poll=self.now)
        await self.guard.monitor_once()
        edits = [params for method, params in self.calls if method == 'editMessageText']
        self.assertEqual(edits[-1]['message_id'], row['message_id'])
        self.assertIn('消息接收已恢复', edits[-1]['text'])
        self.assertNotIn('请联系维护者', edits[-1]['text'])
        self.assertEqual(edits[-1]['reply_markup'], {'inline_keyboard': []})
        count = len(edits)
        self.now += 3
        await self.guard.monitor_once()
        self.assertEqual(sum(method == 'editMessageText' for method, _ in self.calls), count)
        self.start.assert_not_awaited()

    async def test_alerts_only_recovery_edit_survives_the_web_card_expiry_window(self):
        await self.guard.close()
        self.guard = Guardian(self.root, self.repo, alerts_only=True, telegram=self.telegram,
            clock=lambda: self.now, process_probe=lambda pid: {'exists': self.running, 'exact': self.exact},
            start_bridge=self.start, wait_healthy=self.wait)
        _, row = await self.card()
        self.now += self.guard.TTL + 3600
        self.running = True
        self.write_health(pid=101, connected=True, last_poll=self.now)
        await self.guard.monitor_once()
        edits = [params for method, params in self.calls if method == 'editMessageText']
        self.assertEqual(edits[-1]['message_id'], row['message_id'])
        self.assertIn('消息接收已恢复', edits[-1]['text'])
        self.assertEqual(edits[-1]['reply_markup'], {'inline_keyboard': []})
        self.assertIsNone(self.guard.last_delivery_error)
        self.start.assert_not_awaited()

    async def test_alert_recovery_waits_for_a_successful_telegram_poll(self):
        await self.guard.close()
        self.guard = Guardian(self.root, self.repo, alerts_only=True, telegram=self.telegram,
            clock=lambda: self.now, process_probe=lambda pid: {'exists': self.running, 'exact': self.exact},
            start_bridge=self.start, wait_healthy=self.wait)
        _, row = await self.card()
        self.now += 5
        self.running = True
        self.write_health(pid=101, connected=True, last_poll=self.now)
        path = self.root / 'health.json'
        health = json.loads(path.read_text())
        health['last_poll_at'] = None
        path.write_text(json.dumps(health))
        self.assertEqual(self.guard.receiver_state()[0], 'running')
        await self.guard.monitor_once()
        self.assertFalse(any(method == 'editMessageText' for method, _ in self.calls))
        for invalid in (False, -1, float('nan'), self.now + 10):
            health['last_poll_at'] = invalid
            path.write_text(json.dumps(health))
            self.assertEqual(self.guard.receiver_state()[0], 'running')
        self.write_health(pid=101, connected=True, last_poll=self.now)
        await self.guard.monitor_once()
        edits = [params for method, params in self.calls if method == 'editMessageText']
        self.assertEqual(edits[-1]['message_id'], row['message_id'])
        self.assertIn('消息接收已恢复', edits[-1]['text'])

    async def test_default_start_uses_fixed_argv_without_shell_or_client_inputs(self):
        process = type('Process', (), {'returncode': 0, 'communicate': AsyncMock(return_value=(b'', b''))})()
        with patch('recovery_guard.Path.home', return_value=self.root), \
                patch('recovery_guard.asyncio.create_subprocess_exec', new=AsyncMock(return_value=process)) as spawn:
            self.guard.state_dir = (self.root / '.local/state/codex-telegram').resolve()
            await self.guard._start_default()
        args, kwargs = spawn.call_args
        self.assertEqual(args[1:], (str(self.repo / 'manage.py'), 'start',
                                    '--state-dir', str(self.guard.state_dir),
                                    '--session-name', 'codex-telegram-bridge'))
        self.assertNotIn('shell', kwargs)
        self.assertEqual(kwargs['cwd'], str(self.repo))

    async def test_no_https_or_invalid_card_can_expose_receiver_status(self):
        with self.assertRaises(ValueError):
            self.create_guard('http://recovery.example')
        with self.assertRaises(GuardianError) as missing:
            await self.guard.status('invalid')
        self.assertEqual(missing.exception.status, 404)


if __name__ == '__main__':
    unittest.main()
