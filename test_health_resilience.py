"""Diagnostic snapshot failures must not bring down the transport workers."""
import asyncio
import errno
import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from bridge import Bridge, refresh_health_snapshot
from topics_bridge import TopicRouter


class HealthResilienceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name)
        telegram_dir = self.path / 'telegram'
        telegram_dir.mkdir()
        (telegram_dir / 'access.json').write_text(json.dumps({'dmPolicy': 'allowlist', 'allowFrom': ['123']}))
        (telegram_dir / '.env').write_text('TELEGRAM_BOT_TOKEN=fixture\n')
        config = {'owner_id': '123', 'thread_id': 'fixture-native', 'socket_path': '/unused',
                  'telegram_state_dir': str(telegram_dir), 'expected_bot_username': 'fixture_bot'}
        single_path = self.path / 'single'
        single_path.mkdir()
        self.single = Bridge(config, single_path)
        topics_path = self.path / 'topics'
        topics_path.mkdir()
        self.router = TopicRouter({**config, 'root_target': 'fixture', 'targets': [
            {'key': 'fixture', 'name': 'Fixture', 'thread_id': 'fixture-native', 'message_thread_id': 42}
        ]}, topics_path)

    async def asyncTearDown(self):
        self.single.store.db.close()
        for child in self.router.bridges.values():
            child.store.db.close()
        self.router.store.db.close()
        self.tmp.cleanup()

    async def assert_snapshot_loop_recovers(self, component, loop):
        for failure_errno in (errno.ENOSPC, errno.EACCES):
            with self.subTest(errno=failure_errno):
                attempts = []
                original_write = Path.write_text

                def flaky_write(path, *args, **kwargs):
                    if path == component.state_dir / 'health.json.tmp':
                        attempts.append(path)
                        if len(attempts) == 1:
                            raise OSError(failure_errno, 'fixture failure')
                    return original_write(path, *args, **kwargs)

                sleep = AsyncMock(side_effect=[None, asyncio.CancelledError()])
                with patch.object(Path, 'write_text', new=flaky_write), \
                        patch('bridge.asyncio.sleep', new=sleep), \
                        self.assertLogs('codex_telegram', level='INFO') as logs:
                    with self.assertRaises(asyncio.CancelledError):
                        await loop()
                self.assertEqual(len(attempts), 2)
                self.assertEqual(sleep.await_count, 2)
                self.assertEqual([call.args for call in sleep.await_args_list], [(4,), (4,)])
                health = json.loads((component.state_dir / 'health.json').read_text())
                self.assertFalse(health['connected'])
                self.assertFalse(component._health_snapshot_unavailable)
                self.assertEqual(len(logs.records), 2)
                self.assertIn('Health snapshot unavailable', logs.records[0].message)
                self.assertIn('Health snapshot writes recovered', logs.records[1].message)

    async def test_single_maintenance_survives_disk_full_and_permission_failure(self):
        await self.assert_snapshot_loop_recovers(self.single, self.single.maintenance_loop)

    async def test_topic_health_loop_survives_disk_full_and_permission_failure(self):
        await self.assert_snapshot_loop_recovers(self.router, self.router.health_loop)

    def test_repeated_snapshot_failures_log_once_until_recovery(self):
        component = SimpleNamespace(write_health=Mock(side_effect=[
            OSError(errno.ENOSPC, 'private source text'),
            OSError(errno.ENOSPC, 'private source text'),
            OSError(errno.ENOSPC, 'private source text'), None,
            OSError(errno.EACCES, 'private source text'),
        ]))
        with self.assertLogs('codex_telegram', level='INFO') as logs:
            result = [refresh_health_snapshot(component) for _ in range(5)]
        self.assertEqual(result, [False, False, False, True, False])
        self.assertEqual([r.levelname for r in logs.records], ['WARNING', 'INFO', 'WARNING'])
        self.assertNotIn('private source text', '\n'.join(logs.output))

    def test_snapshot_does_not_hide_database_or_programming_errors(self):
        for error in (sqlite3.OperationalError('database failure'), ValueError('invalid health data')):
            with self.subTest(error=type(error).__name__):
                component = SimpleNamespace(write_health=Mock(side_effect=error))
                with self.assertRaises(type(error)) as raised:
                    refresh_health_snapshot(component)
                self.assertIs(raised.exception, error)

    async def assert_shutdown_preserves_failure(self, component, module):
        async def idle(*args, **kwargs):
            await asyncio.Future()

        failure = RuntimeError('primary worker failure')

        async def failed_worker(*args, **kwargs):
            raise failure

        class Telegram:
            async def call(self, method, **kwargs):
                if method == 'getMe':
                    return {'username': 'fixture_bot', 'id': 1, 'has_topics_enabled': True}
                if method == 'getWebhookInfo':
                    return {}
                if method == 'getMyCommands':
                    return []
                if method == 'setMyCommands':
                    return True
                raise AssertionError(method)

        snapshot = Mock(side_effect=OSError(errno.ENOSPC, 'fixture cleanup write failure'))
        component.poll_loop = failed_worker
        if isinstance(component, TopicRouter):
            component.start_target = Mock()
            component.picker.recover = AsyncMock()
            component.picker.loop = idle
            for name in ('forwarding_loop', 'notices_loop', 'health_loop'):
                setattr(component, name, idle)
            for name in ('jobs_loop', 'delivery_loop', 'refresh_loop', 'sync_loop'):
                setattr(component.control, name, idle)
            for child in component.bridges.values():
                child.codex.close = AsyncMock()
        else:
            for name in ('connection_loop', 'input_loop', 'event_loop', 'sender_loop', 'maintenance_loop'):
                setattr(component, name, idle)
            component.codex.close = AsyncMock()
        with patch(module + '.Telegram', return_value=Telegram()), \
                patch.object(component, 'write_health', new=snapshot), \
                self.assertLogs('codex_telegram', level='WARNING'):
            with self.assertRaises(RuntimeError) as raised:
                await component.run()
        self.assertIs(raised.exception, failure)
        snapshot.assert_called_once()

    async def test_single_shutdown_snapshot_does_not_mask_worker_failure(self):
        await self.assert_shutdown_preserves_failure(self.single, 'bridge')

    async def test_topic_shutdown_snapshot_does_not_mask_worker_failure(self):
        await self.assert_shutdown_preserves_failure(self.router, 'topics_bridge')


if __name__ == '__main__':
    unittest.main()
