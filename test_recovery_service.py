import asyncio
import fcntl
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import recovery_service


class ServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / 'recovery').mkdir()
        self.config = {'enabled': True, 'mode': 'alerts-only', 'interval_seconds': 5}
        (self.root / 'recovery/config.json').write_text(json.dumps(self.config))

    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def test_service_only_monitors_and_writes_alerts_only_health(self):
        guardian = SimpleNamespace(monitor_once=AsyncMock(), close=AsyncMock(),
                                   receiver_state=lambda: ('healthy', {}), last_delivery_error=None)

        async def end(interval):
            data = json.loads((self.root / 'recovery/health.json').read_text())
            self.assertTrue(data['alerts_only'])
            self.assertTrue(data['enabled'])
            self.assertEqual(data['receiver_state'], 'healthy')
            self.assertEqual(interval, 5)
            raise asyncio.CancelledError()

        with patch('recovery_service.Guardian', return_value=guardian) as constructor, \
                patch('recovery_service.asyncio.sleep', side_effect=end):
            with self.assertRaises(asyncio.CancelledError):
                await recovery_service.serve(self.root, self.root)
        constructor.assert_called_once_with(self.root, self.root, None, alerts_only=True)
        guardian.monitor_once.assert_awaited_once()
        guardian.close.assert_awaited_once()
        self.assertFalse(json.loads((self.root / 'recovery/health.json').read_text())['enabled'])

    async def test_monitor_failure_is_visible_and_does_not_skip_health(self):
        guardian = SimpleNamespace(monitor_once=AsyncMock(side_effect=RuntimeError('sensitive detail')),
            close=AsyncMock(), receiver_state=lambda: ('stopped', {}), last_delivery_error=None)

        async def end(_):
            data = json.loads((self.root / 'recovery/health.json').read_text())
            self.assertEqual(data['last_error'], 'RuntimeError')
            self.assertNotIn('sensitive', json.dumps(data))
            raise asyncio.CancelledError()

        with patch('recovery_service.Guardian', return_value=guardian), \
                patch('recovery_service.asyncio.sleep', side_effect=end):
            with self.assertRaises(asyncio.CancelledError):
                await recovery_service.serve(self.root, self.root)

    async def test_no_web_mode_or_unbounded_intervals_can_be_enabled_accidentally(self):
        for change in [{'mode': 'web'}, {'enabled': False}, {'interval_seconds': 0}, {'interval_seconds': 1000}]:
            (self.root / 'recovery/config.json').write_text(json.dumps({**self.config, **change}))
            with self.assertRaises(ValueError):
                recovery_service.configuration(self.root)

    async def test_duplicate_monitor_process_is_rejected_before_starting(self):
        with (self.root / 'recovery/service.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with patch('sys.argv', ['recovery_service.py', 'run', '--state-dir', str(self.root)]), \
                    patch('recovery_service.asyncio.run') as runner:
                with self.assertRaisesRegex(SystemExit, 'already running'):
                    recovery_service.main()
                runner.assert_not_called()

    async def test_explicit_notification_probe_sends_and_edits_only_the_original_topic(self):
        calls = []

        async def call(method, **params):
            calls.append((method, params))
            return {'message_id': 77}

        guardian = SimpleNamespace(_ensure_telegram=AsyncMock(), close=AsyncMock(), owner_id='123',
            config=lambda: {'targets': [{'key': 'skyrim', 'name': 'Skyrim', 'thread_id': 'native', 'message_thread_id': 99}]},
            _queue_counts=lambda card: {'pending_messages': 3, 'pending_files': 2},
            telegram=SimpleNamespace(call=call))
        with patch('recovery_service.Guardian', return_value=guardian):
            result = await recovery_service.probe(self.root, self.root, 'skyrim')
        self.assertEqual([method for method, _ in calls], ['sendMessage', 'editMessageText'])
        self.assertEqual(calls[0][1]['message_thread_id'], 99)
        self.assertTrue(calls[0][1]['disable_notification'])
        self.assertIn('测试', calls[0][1]['text'])
        self.assertEqual(calls[1][1]['message_id'], 77)
        self.assertEqual(result['model_calls'], 0)
        self.assertTrue(result['edited_in_place'])

    async def test_unknown_probe_delivery_is_never_retried(self):
        transport = AsyncMock(side_effect=ConnectionError('unknown acknowledgement'))
        guardian = SimpleNamespace(_ensure_telegram=AsyncMock(), close=AsyncMock(), owner_id='123',
            config=lambda: {'targets': [{'key': 'skyrim', 'thread_id': 'native', 'message_thread_id': 99}]},
            _queue_counts=lambda card: {'pending_messages': 0, 'pending_files': 0},
            telegram=SimpleNamespace(call=transport))
        with patch('recovery_service.Guardian', return_value=guardian):
            with self.assertRaises(ConnectionError):
                await recovery_service.probe(self.root, self.root, 'skyrim')
        transport.assert_awaited_once()
        guardian.close.assert_awaited_once()


if __name__ == '__main__':
    unittest.main()
