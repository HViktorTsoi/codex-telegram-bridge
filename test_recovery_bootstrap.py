import fcntl
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import recovery_bootstrap as bootstrap


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.state = self.root / 'state with spaces'
        self.repo = self.root / 'repo with spaces'
        self.proc = self.root / 'proc'
        self.repo.mkdir()
        self.proc.mkdir()
        (self.proc / 'locks').write_text('')
        (self.state / 'recovery').mkdir(parents=True)
        (self.state / 'config.json').write_text(json.dumps({'mode': 'topics'}))
        self.python = sys.executable
        self.handles = []
        self.proc_patch = patch.object(bootstrap, 'PROC', self.proc)
        self.proc_patch.start()

    def tearDown(self):
        for handle in self.handles:
            handle.close()
        self.proc_patch.stop()
        self.temporary.cleanup()

    def config(self, **changes):
        value = {**bootstrap.DEFAULT_CONFIGURATION, **changes}
        (self.state / 'recovery/config.json').write_text(json.dumps(value))
        return value

    def process(self, pid=321, *, supervisor=False, args=None, uid=None):
        directory = self.proc / str(pid)
        directory.mkdir(exist_ok=True)
        if args is None:
            script = 'recovery_bootstrap.py' if supervisor else 'recovery_service.py'
            action = 'supervise' if supervisor else 'run'
            args = [self.python, '-u', str(self.repo / script), action, '--state-dir', str(self.state)]
            if supervisor:
                args += ['--repo-dir', str(self.repo), '--python', self.python]
        (directory / 'cmdline').write_bytes(b'\0'.join(os.fsencode(arg) for arg in args) + b'\0')
        uid = os.getuid() if uid is None else uid
        (directory / 'status').write_text('Uid:\t' + '\t'.join([str(uid)] * 4) + '\n')
        (directory / 'stat').write_text(f'{pid} (python) S 0 0 0\n')
        return pid

    def locked(self, pid, *, supervisor=False):
        name = 'supervisor.lock' if supervisor else 'service.lock'
        path = self.state / 'recovery' / name
        handle = path.open('a')
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.handles.append(handle)
        info = path.stat()
        with (self.proc / 'locks').open('a') as output:
            output.write(f'1: FLOCK ADVISORY WRITE {pid} {os.major(info.st_dev):02x}:'
                         f'{os.minor(info.st_dev):02x}:{info.st_ino} 0 EOF\n')

    def health(self, pid=321, **changes):
        import time
        value = {'pid': pid, 'updated_at': time.time(), 'enabled': True,
                 'mode': 'alerts-only', 'alerts_only': True, 'last_error': None}
        value.update(changes)
        (self.state / 'recovery/health.json').write_text(json.dumps(value))

    def status(self):
        return bootstrap.monitor_status(self.state, self.repo, self.python)

    def ensure(self):
        return bootstrap.ensure_monitor(self.state, self.repo, self.python)

    def test_status_is_read_only_and_missing_configuration_is_not_enabled(self):
        before = sorted(str(path.relative_to(self.state)) for path in self.state.rglob('*'))
        result = self.status()
        self.assertEqual(result['status'], 'unconfigured')
        self.assertFalse(result['config_enabled'])
        self.assertFalse(result['running'])
        self.assertEqual(before, sorted(str(path.relative_to(self.state)) for path in self.state.rglob('*')))

    def test_default_configuration_and_separate_hashed_session_start_healthy_monitor(self):
        def launch(command, **options):
            if command[1] == 'has-session':
                return SimpleNamespace(returncode=1)
            self.assertEqual(command[:4], ['tmux', 'new-session', '-d', '-s'])
            self.assertEqual(command[4], bootstrap.session_name(self.state))
            self.assertNotEqual(command[4], 'codex-telegram-bridge')
            self.assertIn('recovery_bootstrap.py', command[-1])
            self.assertIn('supervise', command[-1])
            self.assertNotIn('topics_bridge.py', command[-1])
            self.process()
            self.locked(321)
            self.health()
            return SimpleNamespace(returncode=0)

        with patch.object(bootstrap.subprocess, 'run', side_effect=launch) as runner:
            result = self.ensure()
        self.assertEqual(runner.call_count, 2)
        self.assertEqual(result['status'], 'healthy')
        self.assertEqual(json.loads((self.state / 'recovery/config.json').read_text()),
                         bootstrap.DEFAULT_CONFIGURATION)
        self.assertEqual((self.state / 'recovery/config.json').stat().st_mode & 0o777, 0o600)
        self.assertEqual(bootstrap.session_name(self.state), bootstrap.session_name(self.state / '..' / self.state.name))
        self.assertNotEqual(bootstrap.session_name(self.state), bootstrap.session_name(self.root / 'other'))

    def test_explicit_disabled_or_other_modes_are_never_overwritten_or_started(self):
        for changes, expected in [({'enabled': False}, 'disabled'), ({'mode': 'web'}, 'unsupported')]:
            with self.subTest(changes=changes):
                self.config(**changes)
                path = self.state / 'recovery/config.json'
                before = path.read_bytes()
                with patch.object(bootstrap.subprocess, 'run') as runner:
                    result = self.ensure()
                self.assertEqual(result['status'], expected)
                self.assertEqual(path.read_bytes(), before)
                runner.assert_not_called()

    def test_existing_empty_configuration_does_not_enable_defaults(self):
        (self.state / 'recovery/config.json').write_text('{}')
        with patch.object(bootstrap.subprocess, 'run') as runner:
            self.assertEqual(self.ensure()['status'], 'disabled')
        runner.assert_not_called()
        self.assertEqual((self.state / 'recovery/config.json').read_text(), '{}')

    def test_invalid_intervals_do_not_launch_or_overwrite_configuration(self):
        for interval in [0, 61, True, '5', float('nan')]:
            with self.subTest(interval=interval):
                self.config(interval_seconds=interval)
                with patch.object(bootstrap.subprocess, 'run') as runner:
                    result = self.ensure()
                runner.assert_not_called()
                self.assertEqual(result['status'], 'failed')
                self.assertEqual(result['error'], 'invalid_monitor_interval')

    def test_single_mode_does_not_create_default_alerts_configuration(self):
        (self.state / 'config.json').write_text('{"mode":"single"}')
        with patch.object(bootstrap.subprocess, 'run') as runner:
            self.assertEqual(self.ensure()['status'], 'unsupported')
        runner.assert_not_called()
        self.assertFalse((self.state / 'recovery/config.json').exists())

    def test_verified_external_monitor_is_reused_without_tmux(self):
        self.config()
        self.process(args=[self.python, str(self.repo / 'recovery_service.py'), 'run', '--state-dir', str(self.state)])
        self.locked(321)
        self.health()
        with patch.object(bootstrap.subprocess, 'run') as runner:
            result = self.ensure()
        runner.assert_not_called()
        self.assertEqual(result['status'], 'healthy')
        self.assertEqual(result['source'], 'external')
        self.assertEqual(result['pid'], 321)

    def test_disabled_configuration_still_reports_an_existing_running_monitor(self):
        self.config(enabled=False)
        self.process()
        self.locked(321)
        self.health()
        with patch.object(bootstrap.subprocess, 'run') as runner:
            result = self.ensure()
        runner.assert_not_called()
        self.assertEqual(result['status'], 'disabled')
        self.assertFalse(result['config_enabled'])
        self.assertTrue(result['running'])
        self.assertTrue(result['healthy'])

    def test_live_exact_monitor_with_stale_health_or_no_lock_is_never_duplicated(self):
        self.config()
        self.process()
        self.health(updated_at=1)
        with patch.object(bootstrap.subprocess, 'run') as runner:
            result = self.ensure()
            self.locked(321)
            again = self.ensure()
        runner.assert_not_called()
        self.assertEqual(result['status'], 'starting')
        self.assertEqual(again['status'], 'starting')
        self.assertTrue(result['running'])
        self.assertFalse(result['healthy'])

    def test_foreign_uid_wrong_argv_or_unidentified_lock_is_failed_without_start(self):
        self.config()
        self.locked(321)
        self.health()
        for case in ['uid', 'script', 'state', 'python', 'extra', 'not-running']:
            with self.subTest(case=case):
                args = [self.python, '-u', str(self.repo / 'recovery_service.py'), 'run', '--state-dir', str(self.state)]
                uid = None
                if case == 'uid':
                    uid = os.getuid() + 1
                elif case == 'script':
                    args[2] = str(self.repo / 'topics_bridge.py')
                elif case == 'state':
                    args[-1] += '-other'
                elif case == 'python':
                    args[0] = '/bin/sh'
                elif case == 'extra':
                    args.append('--notify')
                elif case == 'not-running':
                    args = []
                self.process(args=args, uid=uid)
                with patch.object(bootstrap.subprocess, 'run') as runner:
                    result = self.ensure()
                runner.assert_not_called()
                self.assertEqual(result['status'], 'failed')
                self.assertEqual(result['error'], 'monitor_lock_identity_unverified')
                self.assertFalse(result['healthy'])

    def test_kernel_lock_owner_must_match_health_and_exact_process(self):
        self.config()
        self.process()
        self.process(pid=322)
        self.locked(322)
        self.health(pid=321)
        self.assertEqual(self.status()['status'], 'starting')
        self.health(pid=322)
        self.assertEqual(self.status()['status'], 'healthy')

    def test_stale_or_failed_health_cannot_be_called_healthy(self):
        self.config()
        self.process()
        self.locked(321)
        import time
        for changes in [{'updated_at': 1}, {'updated_at': time.time() + 60},
                        {'updated_at': True}, {'updated_at': float('nan')},
                        {'enabled': False}, {'alerts_only': False}, {'mode': 'web'},
                        {'last_error': 'ConnectionError'}]:
            with self.subTest(changes=changes):
                self.health(**changes)
                result = self.status()
                self.assertFalse(result['healthy'])
                self.assertNotEqual(result['status'], 'healthy')

    def test_existing_supervisor_is_starting_until_monitor_health_exists(self):
        self.config()
        self.process(pid=400, supervisor=True)
        self.locked(400, supervisor=True)
        with patch.object(bootstrap.subprocess, 'run') as runner:
            result = self.ensure()
        runner.assert_not_called()
        self.assertEqual(result['status'], 'starting')
        self.assertFalse(result['healthy'])
        self.assertFalse(result['running'])
        self.assertEqual(result['supervisor_pid'], 400)

    def test_new_supervisor_without_health_stays_starting_after_bounded_wait(self):
        self.config()

        def launch(command, **options):
            if command[1] == 'new-session':
                self.process(pid=400, supervisor=True)
                self.locked(400, supervisor=True)
            return SimpleNamespace(returncode=1 if command[1] == 'has-session' else 0)

        with patch.object(bootstrap.subprocess, 'run', side_effect=launch), \
                patch.object(bootstrap.time, 'monotonic', side_effect=[0, 0, 8]), \
                patch.object(bootstrap.time, 'sleep') as sleep:
            result = self.ensure()
        self.assertEqual(result['status'], 'starting')
        self.assertFalse(result['healthy'])
        sleep.assert_called_once_with(0.2)

    def test_tmux_success_without_verified_process_is_a_failure(self):
        self.config()
        with patch.object(bootstrap.subprocess, 'run', return_value=SimpleNamespace(returncode=1)), \
                patch.object(bootstrap.time, 'monotonic', side_effect=[0, 8]), \
                patch.object(bootstrap.time, 'sleep') as sleep:
            result = self.ensure()
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['error'], 'monitor_startup_unverified')
        sleep.assert_not_called()

    def test_launch_failure_and_occupied_unverified_session_are_visible(self):
        self.config()
        with patch.object(bootstrap.subprocess, 'run', side_effect=FileNotFoundError('tmux')):
            result = self.ensure()
        self.assertEqual(result['status'], 'failed')
        self.assertIn('FileNotFoundError', result['error'])
        with patch.object(bootstrap.subprocess, 'run', return_value=SimpleNamespace(returncode=0)) as runner:
            result = self.ensure()
        runner.assert_called_once()
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['error'], 'tmux_session_without_verified_monitor')

    def test_invalid_configuration_and_unavailable_process_inspection_do_not_start(self):
        (self.state / 'recovery/config.json').write_text('not json')
        with patch.object(bootstrap.subprocess, 'run') as runner:
            self.assertEqual(self.ensure()['status'], 'failed')
        runner.assert_not_called()
        self.config()
        with patch.object(bootstrap, '_processes', side_effect=PermissionError), \
                patch.object(bootstrap.subprocess, 'run') as runner:
            self.assertEqual(self.ensure()['status'], 'failed')
        runner.assert_not_called()

    def test_bootstrap_singleton_prevents_concurrent_launches(self):
        self.config()
        with (self.state / 'recovery/bootstrap.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with patch.object(bootstrap.subprocess, 'run') as runner:
                result = self.ensure()
        runner.assert_not_called()
        self.assertEqual(result['status'], 'starting')
        self.assertEqual(result['error'], 'monitor_bootstrap_in_progress')


class SupervisorTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.state = Path(self.temporary.name)
        self.repo = self.state / 'repo'
        self.repo.mkdir()
        (self.state / 'recovery').mkdir()
        self.config = self.state / 'recovery/config.json'
        self.config.write_text(json.dumps(bootstrap.DEFAULT_CONFIGURATION))
        self.handlers = {}
        self.signal_patch = patch.object(bootstrap.signal, 'signal', side_effect=self.register)
        self.signal_patch.start()
        self.status_patch = patch.object(bootstrap, 'monitor_status', return_value={
            'config_enabled': True, 'mode': 'alerts-only', 'running': False, 'error': None})
        self.status = self.status_patch.start()

    def tearDown(self):
        self.status_patch.stop()
        self.signal_patch.stop()
        self.temporary.cleanup()

    def register(self, sig, handler):
        self.handlers[sig] = handler
        return signal.SIG_DFL

    def supervise(self):
        return bootstrap.supervise(self.state, self.repo, sys.executable)

    def test_supervisor_restarts_only_monitor_with_five_second_backoff(self):
        first, second = Mock(), Mock()
        first.poll.return_value = 1
        second.poll.return_value = 1
        event = Mock()
        event.is_set.return_value = False
        event.wait.side_effect = [False, True]
        with patch.object(bootstrap.threading, 'Event', return_value=event), \
                patch.object(bootstrap.subprocess, 'Popen', side_effect=[first, second]) as launch:
            self.assertEqual(self.supervise(), 0)
        self.assertEqual(launch.call_count, 2)
        for call in launch.call_args_list:
            self.assertEqual(call.args[0], [sys.executable, '-u', str(self.repo / 'recovery_service.py'),
                                           'run', '--state-dir', str(self.state)])
            self.assertTrue(call.kwargs['close_fds'])
        self.assertEqual([call.args for call in event.wait.call_args_list], [(5,), (5,)])

    def test_all_stop_signals_terminate_and_wait_for_the_owned_child(self):
        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            with self.subTest(sig=sig):
                child = Mock()
                child.poll.return_value = None
                event = __import__('threading').Event()

                def stop(_):
                    self.handlers[sig](sig, None)
                    return event.is_set()

                with patch.object(bootstrap.threading, 'Event', return_value=event), \
                        patch.object(event, 'wait', side_effect=stop), \
                        patch.object(bootstrap.subprocess, 'Popen', return_value=child):
                    self.assertEqual(self.supervise(), 0)
                child.terminate.assert_called_once()
                child.wait.assert_called_once_with(timeout=10)

    def test_stubborn_child_is_killed_after_bounded_termination_wait(self):
        child = Mock()
        child.poll.return_value = None
        child.wait.side_effect = [subprocess.TimeoutExpired('monitor', 10), 0]
        bootstrap._stop_child(child)
        child.terminate.assert_called_once()
        child.kill.assert_called_once()
        self.assertEqual([call.kwargs for call in child.wait.call_args_list], [{'timeout': 10}, {'timeout': 5}])

    def test_explicit_disable_stops_own_child_without_restarting(self):
        child = Mock()
        child.poll.return_value = None

        def disable(_):
            self.config.write_text('{"enabled":false,"mode":"alerts-only"}')
            return False

        with patch.object(bootstrap.threading, 'Event') as event_type, \
                patch.object(bootstrap.subprocess, 'Popen', return_value=child) as launch:
            event_type.return_value.is_set.return_value = False
            event_type.return_value.wait.side_effect = disable
            self.assertEqual(self.supervise(), 0)
        launch.assert_called_once()
        child.terminate.assert_called_once()
        child.wait.assert_called_once_with(timeout=10)

    def test_disabled_external_running_or_unverified_lock_never_spawns_monitor(self):
        cases = [{'config_enabled': False}, {'running': True}, {'error': 'monitor_lock_identity_unverified'}]
        for changes in cases:
            with self.subTest(changes=changes):
                self.status.return_value = {'config_enabled': True, 'mode': 'alerts-only',
                                            'running': False, 'error': None, **changes}
                with patch.object(bootstrap.subprocess, 'Popen') as launch:
                    self.supervise()
                launch.assert_not_called()

    def test_supervisor_lock_allows_only_one_instance(self):
        with (self.state / 'recovery/supervisor.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with patch.object(bootstrap.subprocess, 'Popen') as launch:
                self.assertEqual(self.supervise(), 0)
        launch.assert_not_called()

    def test_supervise_cli_requires_canonical_identity_arguments(self):
        with patch.object(sys, 'argv', ['recovery_bootstrap.py', 'supervise', '--state-dir', str(self.state)]), \
                patch.object(bootstrap, 'supervise') as supervise, \
                patch.object(sys, 'stderr'):
            with self.assertRaises(SystemExit) as error:
                bootstrap.main()
        self.assertEqual(error.exception.code, 2)
        supervise.assert_not_called()

    def test_spawn_errors_retry_with_backoff_and_remain_stoppable(self):
        event = Mock()
        event.is_set.return_value = False
        event.wait.return_value = True
        with patch.object(bootstrap.threading, 'Event', return_value=event), \
                patch.object(bootstrap.subprocess, 'Popen', side_effect=OSError) as launch:
            self.assertEqual(self.supervise(), 0)
        launch.assert_called_once()
        event.wait.assert_called_once_with(5)


if __name__ == '__main__':
    unittest.main()
