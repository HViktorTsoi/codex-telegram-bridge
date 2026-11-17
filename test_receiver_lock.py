import asyncio
import json
import os
from pathlib import Path
import pwd
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from bridge import Bridge, TelegramError
from receiver_lock import BotReceiverLock, ReceiverAlreadyRunning, legacy_receivers
from topics_bridge import TopicRouter


class ReceiverLockTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.directory = self.root / 'pollers'
        self.proc = self.root / 'proc'
        self.proc.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def lock(self, bot_id=101, state='first', username='fixture_bot'):
        return BotReceiverLock(bot_id, username, self.root / state,
                               directory=self.directory, proc_root=self.proc)

    def process(self, pid, *, username='fixture_bot', entrypoint='topics_bridge.py',
                action='run', health=True, state_option='--state-dir'):
        state = self.root / ('state-' + str(pid))
        state.mkdir()
        (state / 'config.json').write_text(json.dumps({'expected_bot_username': username,
                                                     'telegram_state_dir': '/must/not/read/credentials'}))
        if health:
            (state / 'health.json').write_text(json.dumps({'pid': pid}))
        process = self.proc / str(pid)
        process.mkdir()
        args = [sys.executable, '-u', '/repo/' + entrypoint, action]
        args += ['--state-dir=' + str(state)] if state_option.endswith('=') else ['--state-dir', str(state)]
        (process / 'cmdline').write_bytes(b'\0'.join(os.fsencode(arg) for arg in args) + b'\0')
        return state

    def test_same_bot_cannot_poll_from_another_state_or_username_case(self):
        with self.lock():
            with self.assertRaises(ReceiverAlreadyRunning):
                with self.lock(state='copied-state', username='FIXTURE_BOT'):
                    self.fail('duplicate receiver acquired the Bot lock')
        data = json.loads((self.directory / '101.lock').read_text())
        self.assertEqual(data['state_dir'], str(self.root / 'first'))
        self.assertEqual(data['bot_id'], 101)
        self.assertEqual((self.directory.stat().st_mode & 0o777), 0o700)
        self.assertEqual(((self.directory / '101.lock').stat().st_mode & 0o777), 0o600)

    def test_distinct_bots_can_hold_independent_locks(self):
        with self.lock():
            with self.lock(bot_id=202, state='other', username='other_bot'):
                self.assertTrue((self.directory / '202.lock').exists())

    def test_default_directory_uses_unix_account_home_instead_of_process_home(self):
        with patch('receiver_lock.Path.home', return_value=self.root / 'different-home'):
            lock = BotReceiverLock(101, 'fixture_bot', self.root / 'state')
        expected = Path(pwd.getpwuid(os.getuid()).pw_dir) / '.local/state/codex-telegram-pollers'
        self.assertEqual(lock.directory, expected)

    def test_context_failure_releases_lock_and_keeps_the_same_inode(self):
        with self.assertRaisesRegex(ValueError, 'worker failed'):
            with self.lock():
                inode = (self.directory / '101.lock').stat().st_ino
                raise ValueError('worker failed')
        with self.lock(state='after-exit'):
            self.assertEqual((self.directory / '101.lock').stat().st_ino, inode)

    def test_process_exit_releases_kernel_lock(self):
        script = ('from receiver_lock import BotReceiverLock; import sys; '
                  'lock = BotReceiverLock(101, "fixture_bot", "child-state", '
                  'directory=sys.argv[1], proc_root=sys.argv[2]); '
                  'lock.__enter__(); print("ready", flush=True); sys.stdin.read()')
        child = subprocess.Popen([sys.executable, '-c', script, str(self.directory), str(self.proc)],
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            self.assertEqual(child.stdout.readline().strip(), 'ready')
            with self.assertRaises(ReceiverAlreadyRunning):
                with self.lock():
                    self.fail('child lock was bypassed')
            child.terminate()
            child.communicate(timeout=5)
            with self.lock():
                pass
        finally:
            if child.poll() is None:
                child.kill()
                child.communicate(timeout=5)

    def test_legacy_receiver_is_rejected_without_reading_credentials(self):
        self.process(900001, username='@FIXTURE_BOT', state_option='--state-dir=')
        self.assertEqual(legacy_receivers('fixture_bot', proc_root=self.proc), [900001])
        with self.assertRaisesRegex(ReceiverAlreadyRunning, 'PID 900001'):
            with self.lock():
                self.fail('legacy poller was bypassed')
        (self.proc / '900001' / 'cmdline').unlink()
        with self.lock():
            pass

    def test_legacy_scan_ignores_send_only_helpers_other_bots_and_current_process(self):
        self.process(900002, entrypoint='recovery_service.py')
        self.process(900003, entrypoint='recovery_guard.py')
        self.process(900004, username='other_bot')
        self.process(900005, action='status')
        self.process(900006, health=False)
        self.process(os.getpid())
        self.assertEqual(legacy_receivers('fixture_bot', proc_root=self.proc), [])

    def test_lock_directory_and_file_reject_symlinks_and_public_permissions(self):
        outside = self.root / 'outside'
        outside.mkdir()
        self.directory.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(RuntimeError):
            with self.lock():
                pass
        self.directory.unlink()
        self.directory.mkdir(mode=0o700)
        target = outside / 'target'
        target.write_text('do not overwrite')
        (self.directory / '101.lock').symlink_to(target)
        with self.assertRaises(RuntimeError):
            with self.lock():
                pass
        self.assertEqual(target.read_text(), 'do not overwrite')
        (self.directory / '101.lock').unlink()
        self.directory.chmod(0o755)
        with self.assertRaises(RuntimeError):
            with self.lock():
                pass

    def test_parent_symlink_and_hardlinked_lock_cannot_redirect_writes(self):
        outside = self.root / 'outside'
        outside.mkdir()
        parent = self.root / 'linked-parent'
        parent.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(RuntimeError):
            with BotReceiverLock(101, 'fixture_bot', self.root / 'state',
                                 directory=parent / 'pollers', proc_root=self.proc):
                pass
        self.assertFalse((outside / 'pollers').exists())
        self.directory.mkdir(mode=0o700)
        target = outside / 'target'
        target.write_text('do not overwrite')
        target.chmod(0o600)
        os.link(target, self.directory / '101.lock')
        with self.assertRaises(RuntimeError):
            with self.lock():
                pass
        self.assertEqual(target.read_text(), 'do not overwrite')


class ReceiverStartupTests(unittest.IsolatedAsyncioTestCase):
    async def test_both_entrypoints_refuse_duplicate_before_any_poll_or_setup_side_effect(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'proc').mkdir()
            telegram = root / 'telegram'
            telegram.mkdir()
            (telegram / '.env').write_text('TELEGRAM_BOT_TOKEN=fixture\n')
            calls = []

            class Telegram:
                async def call(self, method, **kwargs):
                    calls.append(method)
                    if method == 'getMe':
                        return {'id': 101, 'username': 'fixture_bot', 'has_topics_enabled': True}
                    raise AssertionError('duplicate reached ' + method)

            def lock(bot_id, username, state):
                return BotReceiverLock(bot_id, username, state, directory=root / 'pollers', proc_root=root / 'proc')

            async def startup_call(component, method, **kwargs):
                return await component.telegram.call(method, **kwargs)

            with lock(101, 'fixture_bot', root / 'existing'):
                for component_class, module in ((Bridge, 'bridge'), (TopicRouter, 'topics_bridge')):
                    with self.subTest(entrypoint=module):
                        calls.clear()
                        component = SimpleNamespace(owner_allowed=lambda: True, token=lambda: 'fixture',
                            session_scope=SimpleNamespace(enabled=False), state_dir=root / module,
                            config={'expected_bot_username': 'fixture_bot', 'telegram_state_dir': str(telegram)})
                        component.startup_call = lambda method, **kwargs: startup_call(component, method, **kwargs)
                        with patch(module + '.Telegram', return_value=Telegram()), \
                                patch(module + '.BotReceiverLock', side_effect=lock):
                            with self.assertRaises(ReceiverAlreadyRunning):
                                await component_class.run(component)
                        self.assertEqual(calls, ['getMe'])

    async def test_both_entrypoints_release_lock_when_startup_is_cancelled(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'proc').mkdir()
            telegram = root / 'telegram'
            telegram.mkdir()
            (telegram / '.env').write_text('TELEGRAM_BOT_TOKEN=fixture\n')

            def lock(bot_id, username, state):
                return BotReceiverLock(bot_id, username, state, directory=root / 'pollers', proc_root=root / 'proc')

            for component_class, module in ((Bridge, 'bridge'), (TopicRouter, 'topics_bridge')):
                with self.subTest(entrypoint=module):
                    entered = asyncio.Event()

                    class Telegram:
                        async def call(self, method, **kwargs):
                            if method == 'getMe':
                                return {'id': 101, 'username': 'fixture_bot', 'has_topics_enabled': True}
                            if method == 'getWebhookInfo':
                                entered.set()
                                await asyncio.Future()
                            raise AssertionError(method)

                    component = SimpleNamespace(owner_allowed=lambda: True, token=lambda: 'fixture',
                        session_scope=SimpleNamespace(enabled=False), state_dir=root / module,
                        config={'expected_bot_username': 'fixture_bot', 'telegram_state_dir': str(telegram)})
                    component.startup_call = lambda method, **kwargs: component.telegram.call(method, **kwargs)
                    with patch(module + '.Telegram', return_value=Telegram()), \
                            patch(module + '.BotReceiverLock', side_effect=lock):
                        task = asyncio.create_task(component_class.run(component))
                        try:
                            await asyncio.wait_for(entered.wait(), timeout=2)
                            with self.assertRaises(ReceiverAlreadyRunning):
                                with lock(101, 'fixture_bot', root / 'another'):
                                    pass
                        finally:
                            task.cancel()
                            with self.assertRaises(asyncio.CancelledError):
                                await task
                    with lock(101, 'fixture_bot', root / 'after-cancellation'):
                        pass

    async def test_fatal_poll_errors_are_still_raised_once_without_retry(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for component_class, module in ((Bridge, 'bridge'), (TopicRouter, 'topics_bridge')):
                for code in (401, 409):
                    with self.subTest(entrypoint=module, code=code):
                        call = AsyncMock(side_effect=TelegramError('getUpdates', code, 'fixture rejection'))
                        component = SimpleNamespace(telegram=SimpleNamespace(call=call), state_dir=root,
                            config={}, targets={}, last_error=None, last_poll_at=None, write_health=Mock(),
                            store=SimpleNamespace(offset=lambda: 0, get=lambda key, default: default))
                        with patch(module + '.asyncio.sleep', new_callable=AsyncMock) as sleep:
                            with self.assertRaises(TelegramError):
                                await component_class.poll_loop(component)
                        call.assert_awaited_once()
                        sleep.assert_not_awaited()
                        self.assertEqual(component.last_error, 'Telegram poll: ' + str(code))


if __name__ == '__main__':
    unittest.main()
