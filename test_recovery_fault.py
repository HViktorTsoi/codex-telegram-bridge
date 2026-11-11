import asyncio
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from bridge import TelegramError
import manage
from recovery_fault import mark_maintenance, record_poll_failure
from topics_bridge import TopicRouter


class FaultTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.config = {'mode': 'topics', 'host_id': 'zbox', 'owner_id': '123',
                       'telegram_state_dir': str(self.root), 'socket_path': '/unused',
                       'thread_id': 'manager-native', 'root_target': 'manager',
                       'expected_bot_username': 'fixture_bot', 'targets': [
                           {'key': 'manager', 'name': 'Manager', 'thread_id': 'manager-native', 'message_thread_id': 42},
                           {'key': 'skyrim', 'name': 'Skyrim', 'thread_id': 'skyrim-native', 'message_thread_id': 99}]}
        (self.root / 'config.json').write_text(json.dumps(self.config))
        (self.root / 'access.json').write_text(json.dumps({'dmPolicy': 'allowlist', 'allowFrom': ['123']}))
        self.router = TopicRouter(self.config, self.root)

    async def asyncTearDown(self):
        for child in self.router.bridges.values():
            child.store.db.close()
        self.router.store.db.close()
        self.tmp.cleanup()

    def enqueue(self):
        self.router.ingest({'update_id': 7, 'message': {'message_id': 8,
            'chat': {'id': 123, 'type': 'private'}, 'from': {'id': 123}, 'message_thread_id': 99,
            'text': 'private request must not be copied', 'document': {'file_id': 'private-file-id',
            'file_name': 'private-filename.zip', 'file_size': 123}}})

    def fault(self):
        return json.loads((self.root / 'control/recovery-fault.json').read_text())

    async def test_unforwarded_file_is_reported_to_its_original_topic_without_private_content(self):
        self.enqueue()
        self.assertTrue(record_poll_failure(self.router, 409, clock=lambda: 1000))
        data = self.fault()
        self.assertEqual(data['targets'], [{'target_key': 'skyrim', 'thread_id': 'skyrim-native', 'topic_id': 99}])
        self.assertEqual(data['reason'], 'telegram_poll_conflict')
        self.assertEqual(data['at'], 1000)
        self.assertNotIn('private', json.dumps(data))

    async def test_child_queue_and_idle_root_fallback_are_preserved(self):
        self.enqueue()
        self.assertTrue(self.router.forward_one())
        record_poll_failure(self.router, 401)
        self.assertEqual(self.fault()['targets'][0]['target_key'], 'skyrim')
        with self.router.bridges['skyrim'].store.db:
            self.router.bridges['skyrim'].store.db.execute("UPDATE inbox SET state='done'")
        record_poll_failure(self.router, 409)
        self.assertEqual(self.fault()['targets'][0]['target_key'], 'manager')

    async def test_fatal_poll_still_raises_and_health_contains_the_reason(self):
        self.enqueue()
        self.router.telegram = SimpleNamespace(call=AsyncMock(side_effect=TelegramError('getUpdates', 409, 'Conflict')))
        with self.assertRaises(TelegramError):
            await self.router.poll_loop()
        self.assertEqual(self.router.last_error, 'Telegram poll: 409')
        self.assertEqual(json.loads((self.root / 'health.json').read_text())['last_error'], 'Telegram poll: 409')
        self.assertEqual(self.fault()['targets'][0]['topic_id'], 99)

    async def test_unwritable_fault_record_does_not_hide_the_original_error(self):
        (self.root / 'control').mkdir(exist_ok=True)
        with patch('recovery_fault.os.open', side_effect=OSError('unavailable')):
            self.assertFalse(record_poll_failure(self.router, 409))
        self.assertEqual(self.router.last_error, 'Telegram poll: 409')


class ManagementTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / 'recovery').mkdir()
        (self.root / 'recovery/config.json').write_text('{"enabled":true,"mode":"alerts-only"}')
        (self.root / 'config.json').write_text('{"mode":"topics"}')

    def tearDown(self):
        self.tmp.cleanup()

    def test_explicit_management_state_is_not_redirected_to_default(self):
        mark_maintenance(self.root, until=time.time() + 300, pid=123)
        calls = []

        def run(args, **kwargs):
            calls.append(args)
            return SimpleNamespace(returncode=1 if args[1] == 'has-session' else 0)

        with patch('sys.argv', ['manage.py', 'start', '--state-dir', str(self.root), '--session-name', 'fixture-bridge']), \
                patch('manage.subprocess.run', side_effect=run), redirect_stdout(io.StringIO()):
            manage.main()
        self.assertFalse((self.root / 'recovery/maintenance.json').exists())
        create = next(args for args in calls if args[1] == 'new-session')
        self.assertIn('fixture-bridge', create)
        self.assertIn(str(self.root), create[-1])

    def test_intentional_stop_marks_a_bounded_maintenance_window(self):
        (self.root / 'health.json').write_text('{"pid":321}')
        with patch('sys.argv', ['manage.py', 'stop', '--state-dir', str(self.root)]), \
                patch('manage.subprocess.run', return_value=SimpleNamespace(returncode=1)), redirect_stdout(io.StringIO()):
            manage.main()
        data = json.loads((self.root / 'recovery/maintenance.json').read_text())
        self.assertEqual(data['pid'], 321)
        self.assertGreater(data['until'], time.time())
        mark_maintenance(self.root)
        self.assertFalse((self.root / 'recovery/maintenance.json').exists())


if __name__ == '__main__':
    unittest.main()
