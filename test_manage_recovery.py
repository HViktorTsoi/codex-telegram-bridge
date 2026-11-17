import io
import json
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import manage


class ManagementRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.root.joinpath('config.json').write_text(json.dumps({'mode': 'topics'}))

    def tearDown(self):
        self.tmp.cleanup()

    def invoke(self, action='start', *, exists=False, result=None):
        operations = []

        def command(args, **kwargs):
            operations.append(('command', args))
            return SimpleNamespace(returncode=(0 if exists else 1) if args[1] == 'has-session' else 0)

        def ensure(*args):
            operations.append(('monitor', args))
            return result or {'status': 'healthy'}

        with patch('sys.argv', ['manage.py', action, '--state-dir', str(self.root),
                                '--session-name', 'fixture-receiver']), \
                patch('manage.subprocess.run', side_effect=command), \
                patch('manage.ensure_monitor', side_effect=ensure) as monitor, \
                redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()) as error:
            manage.main()
        return operations, monitor, output.getvalue(), error.getvalue()

    def test_new_topic_receiver_gets_independent_alerts_after_launch(self):
        operations, monitor, output, _ = self.invoke()
        create_index = next(i for i, (kind, args) in enumerate(operations)
                            if kind == 'command' and args[1] == 'new-session')
        monitor_index = next(i for i, (kind, _) in enumerate(operations) if kind == 'monitor')
        self.assertLess(create_index, monitor_index)
        monitor.assert_called_once_with(self.root, Path(manage.__file__).parent, manage.sys.executable)
        self.assertIn('alerts are running', output)

    def test_existing_receiver_also_gets_alerts_without_restart(self):
        operations, monitor, _, _ = self.invoke(exists=True)
        monitor.assert_called_once()
        self.assertFalse(any(kind == 'command' and args[1] in ('new-session', 'kill-session')
                             for kind, args in operations))

    def test_single_conversation_mode_does_not_start_unsupported_monitor(self):
        self.root.joinpath('config.json').write_text(json.dumps({'mode': 'single'}))
        _, monitor, _, _ = self.invoke()
        monitor.assert_not_called()

    def test_explicit_disable_and_external_modes_are_not_overridden(self):
        for status in ('disabled', 'unsupported'):
            with self.subTest(status=status):
                _, _, output, _ = self.invoke(result={'status': status})
                self.assertNotIn('alerts are running', output)

    def test_disabled_configuration_reports_existing_monitor_still_running(self):
        _, _, output, _ = self.invoke(result={'status': 'disabled', 'running': True})
        self.assertIn('still running', output)

    def test_unverified_or_failed_alert_launch_is_an_explicit_partial_failure(self):
        for status in ('starting', 'failed'):
            with self.subTest(status=status), self.assertRaises(SystemExit) as outcome:
                self.invoke(result={'status': status, 'error': 'monitor_not_ready'})
            self.assertEqual(outcome.exception.code, 2)

    def test_stopping_receiver_keeps_independent_monitor_untouched(self):
        operations, monitor, _, _ = self.invoke(action='stop', exists=True)
        monitor.assert_not_called()
        killed = [args for kind, args in operations if kind == 'command' and args[1] == 'kill-session']
        self.assertEqual(killed, [['tmux', 'kill-session', '-t', '=fixture-receiver']])

    def test_status_includes_real_read_only_monitor_state(self):
        recovery = {'status': 'healthy', 'running': True, 'config_enabled': True}
        with patch('sys.argv', ['manage.py', 'status', '--state-dir', str(self.root)]), \
                patch('manage.subprocess.run', return_value=SimpleNamespace(returncode=0)), \
                patch('manage.monitor_status', return_value=recovery) as query, \
                patch('manage.ensure_monitor') as starter, redirect_stdout(io.StringIO()) as output:
            manage.main()
        self.assertEqual(json.loads(output.getvalue())['recovery'], recovery)
        query.assert_called_once()
        starter.assert_not_called()


if __name__ == '__main__':
    unittest.main()
