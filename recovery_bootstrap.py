#!/usr/bin/env python3
"""Bootstrap an independent alerts-only monitor without touching the receiver."""
import argparse
import contextlib
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import shlex
import signal
import subprocess
import sys
import tempfile
import threading
import time


DEFAULT_CONFIGURATION = {'enabled': True, 'mode': 'alerts-only', 'interval_seconds': 5}
STARTUP_WAIT = 8
RESTART_BACKOFF = 5
PROC = Path('/proc')


def session_name(state_dir):
    """Keep the sidecar separate from both receiver names and other states."""
    state = Path(state_dir).expanduser().resolve()
    return 'codex-telegram-alerts-' + hashlib.sha256(os.fsencode(state)).hexdigest()[:16]


def _read_json(path):
    data = json.loads(path.read_text())
    if not isinstance(data, dict):
        raise ValueError('Expected a JSON object')
    return data


def _processes():
    """Inspect only same-user processes; never trust a PID from a JSON file."""
    found = {}
    for directory in PROC.iterdir():
        if not directory.name.isdecimal():
            continue
        try:
            if directory.stat().st_uid != os.getuid():
                continue
            status = (directory / 'status').read_text().splitlines()
            uids = next(line.split()[1:] for line in status if line.startswith('Uid:'))
            if any(int(uid) != os.getuid() for uid in uids):
                continue
            stat = (directory / 'stat').read_text().rsplit(')', 1)[1].split()
            if stat[0] == 'Z':
                continue
            args = [os.fsdecode(arg) for arg in (directory / 'cmdline').read_bytes().rstrip(b'\0').split(b'\0')]
            found[int(directory.name)] = args
        except (OSError, ValueError, StopIteration, IndexError):
            continue
    return found


def _exact(args, script, action, state, python, extra=()):
    if not args:
        return False
    try:
        if Path(args[0]).resolve() != Path(python).resolve():
            return False
        tail = args[1:]
        if tail and tail[0] == '-u':
            tail = tail[1:]
        return tail == [str(script), action, '--state-dir', str(state), *extra]
    except (OSError, ValueError):
        return False


def _lock_owner(path):
    """Return None for a free lock, its kernel owner PID, or -1 if unknown.

    Opening read-only and briefly trying flock also detects occupied locks when
    /proc/locks cannot identify their owner. No status file is created or edited.
    """
    try:
        handle = path.open('rb')
    except FileNotFoundError:
        return None
    with handle:
        info = os.fstat(handle.fileno())
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            identity = (os.major(info.st_dev), os.minor(info.st_dev), info.st_ino)
            for line in (PROC / 'locks').read_text().splitlines():
                fields = line.split()
                if len(fields) != 8 or fields[1:4] != ['FLOCK', 'ADVISORY', 'WRITE']:
                    continue
                try:
                    major, minor, inode = fields[5].split(':')
                    match = (int(major, 16), int(minor, 16), int(inode))
                    if match == identity and fields[6:] == ['0', 'EOF']:
                        return int(fields[4])
                except ValueError:
                    continue
            return -1
        else:
            fcntl.flock(handle, fcntl.LOCK_UN)
            return None


def monitor_status(state_dir, repo_dir, python=sys.executable):
    """Read configuration and verify monitor identity without starting anything.

    ``config_enabled`` describes the saved preference; ``running`` describes the
    observed process, including an external monitor after configuration disable.
    A healthy result requires a current matching process, its service lock and
    fresh error-free service health. A tmux session is never health evidence.
    """
    state, repo = Path(state_dir).expanduser().resolve(), Path(repo_dir).expanduser().resolve()
    result = {'status': 'configured', 'config_enabled': False, 'enabled': False,
              'mode': None, 'running': False, 'healthy': False, 'pid': None,
              'supervisor_pid': None, 'source': None, 'session': session_name(state),
              'health_age_seconds': None, 'last_error': None, 'error': None}
    try:
        config = _read_json(state / 'recovery/config.json')
    except FileNotFoundError:
        config = {}
        result['status'] = 'unconfigured'
    except (OSError, ValueError):
        config = {}
        result.update(status='failed', error='invalid_monitor_configuration')
    result.update(config_enabled=config.get('enabled') is True,
                  enabled=config.get('enabled') is True, mode=config.get('mode'))
    try:
        processes = _processes()
        monitors = {pid for pid, args in processes.items()
                    if _exact(args, repo / 'recovery_service.py', 'run', state, python)}
        supervisors = {pid for pid, args in processes.items()
                       if _exact(args, repo / 'recovery_bootstrap.py', 'supervise', state, python,
                                 ['--repo-dir', str(repo), '--python', str(python)])}
        service_owner = _lock_owner(state / 'recovery/service.lock')
        supervisor_owner = _lock_owner(state / 'recovery/supervisor.lock')
    except (OSError, ValueError):
        result.update(status='failed', error='monitor_identity_unavailable')
        return result
    result['running'] = bool(monitors)
    result['pid'] = service_owner if service_owner in monitors else next(iter(sorted(monitors)), None)
    if supervisor_owner in supervisors:
        result.update(supervisor_pid=supervisor_owner, source='tmux')
    elif monitors:
        result['source'] = 'external'
    identity_error = None
    if service_owner is not None and service_owner not in monitors:
        identity_error = 'monitor_lock_identity_unverified'
    elif supervisor_owner is not None and supervisor_owner not in supervisors:
        identity_error = 'supervisor_lock_identity_unverified'
    try:
        health = _read_json(state / 'recovery/health.json')
    except (OSError, ValueError):
        health = {}
    if type(health.get('pid')) is int and health['pid'] == result['pid'] and result['running']:
        updated = health.get('updated_at')
        interval = config.get('interval_seconds', 5)
        if type(interval) not in (int, float) or not math.isfinite(interval) or not 1 <= interval <= 60:
            interval = 5
        now = time.time()
        fresh = (type(updated) in (int, float) and math.isfinite(updated)
                 and 0 < updated <= now + 5 and now - updated < max(15, interval * 3))
        if type(updated) in (int, float) and math.isfinite(updated):
            result['health_age_seconds'] = round(now - updated, 1)
        result['last_error'] = health.get('last_error')
        result['healthy'] = bool(service_owner == health['pid'] and fresh
                                 and health.get('enabled') is True
                                 and health.get('mode') == 'alerts-only'
                                 and health.get('alerts_only') is True
                                 and not health.get('last_error') and not identity_error)
    if result['error']:
        return result
    if result['status'] == 'unconfigured':
        return result
    if config.get('enabled') is not True:
        result['status'] = 'disabled'
    elif config.get('mode') != 'alerts-only':
        result['status'] = 'unsupported'
    elif (type(config.get('interval_seconds', 5)) not in (int, float)
          or not math.isfinite(config.get('interval_seconds', 5))
          or not 1 <= config.get('interval_seconds', 5) <= 60):
        result.update(status='failed', error='invalid_monitor_interval')
    elif identity_error:
        result.update(status='failed', error=identity_error)
    elif result['healthy']:
        result['status'] = 'healthy'
    elif result['running']:
        result['status'] = 'failed' if result['last_error'] else 'starting'
        result['error'] = 'monitor_health_error' if result['last_error'] else 'monitor_health_pending'
    elif result['supervisor_pid']:
        result['status'] = 'starting'
    return result


def _create_default_configuration(state):
    directory = state / 'recovery'
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = directory / 'config.json'
    # Publish a complete file without ever replacing an existing preference.
    with tempfile.NamedTemporaryFile(mode='w', dir=directory, prefix='.config-', delete=False) as handle:
        temporary = Path(handle.name)
        try:
            handle.write(json.dumps(DEFAULT_CONFIGURATION, indent=2) + '\n')
            handle.flush()
            os.fsync(handle.fileno())
            with contextlib.suppress(FileExistsError):
                os.link(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


def ensure_monitor(state_dir, repo_dir, python=sys.executable):
    """Configure topics defaults and start only the missing independent sidecar.

    Startup waits at most STARTUP_WAIT seconds for real service health. Callers
    must inspect ``status``; saving an enabled preference is not a health check.
    """
    state, repo = Path(state_dir).expanduser().resolve(), Path(repo_dir).expanduser().resolve()
    python = str(Path(python).expanduser().absolute())
    try:
        if _read_json(state / 'config.json').get('mode') != 'topics':
            return {**monitor_status(state, repo, python), 'status': 'unsupported',
                    'error': 'topics_mode_required'}
        _create_default_configuration(state)
        with (state / 'recovery/bootstrap.lock').open('a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                result = monitor_status(state, repo, python)
                if result['status'] == 'configured':
                    result.update(status='starting', error='monitor_bootstrap_in_progress')
                return result
            result = monitor_status(state, repo, python)
            if result['status'] != 'configured':
                return result
            session = session_name(state)
            existing = subprocess.run(['tmux', 'has-session', '-t', '=' + session],
                                      capture_output=True, timeout=3)
            if existing.returncode == 0:
                return {**result, 'status': 'failed', 'error': 'tmux_session_without_verified_monitor'}
            log = state / 'recovery/monitor.log'
            log.touch(mode=0o600, exist_ok=True)
            log.chmod(0o600)
            command = [python, '-u', str(repo / 'recovery_bootstrap.py'), 'supervise',
                       '--state-dir', str(state), '--repo-dir', str(repo), '--python', python]
            shell = 'exec ' + shlex.join(command) + ' >> ' + shlex.quote(str(log)) + ' 2>&1'
            subprocess.run(['tmux', 'new-session', '-d', '-s', session, '-c', str(repo), shell],
                           check=True, capture_output=True, timeout=5)
            deadline = time.monotonic() + STARTUP_WAIT
            while True:
                result = monitor_status(state, repo, python)
                if result['status'] in ('healthy', 'disabled', 'unsupported', 'failed'):
                    return result
                if time.monotonic() >= deadline:
                    if result['status'] == 'configured':
                        result.update(status='failed', error='monitor_startup_unverified')
                    return result
                time.sleep(0.2)
    except (OSError, ValueError, subprocess.SubprocessError) as failure:
        result = monitor_status(state, repo, python)
        if result['status'] == 'healthy':
            return result
        result.update(status='failed', error='monitor_bootstrap_' + type(failure).__name__)
        return result


def _stop_child(child):
    if child is None or child.poll() is not None:
        return
    child.terminate()
    try:
        child.wait(timeout=10)
    except subprocess.TimeoutExpired:
        child.kill()
        child.wait(timeout=5)


def supervise(state_dir, repo_dir, python=sys.executable):
    """Run only recovery_service.py, retrying failures independently of receiver."""
    state, repo = Path(state_dir).expanduser().resolve(), Path(repo_dir).expanduser().resolve()
    stopping = threading.Event()
    previous = {}
    child = None
    with (state / 'recovery/supervisor.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        try:
            for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
                previous[sig] = signal.signal(sig, lambda *_: stopping.set())
            while not stopping.is_set():
                result = monitor_status(state, repo, python)
                if not result['config_enabled'] or result['mode'] != 'alerts-only':
                    return 0
                # Existing external monitors, even stale ones, own their lifecycle.
                if result['running']:
                    return 0
                if result['error']:
                    return 1
                try:
                    child = subprocess.Popen([str(python), '-u', str(repo / 'recovery_service.py'),
                                              'run', '--state-dir', str(state)], cwd=repo,
                                             stdin=subprocess.DEVNULL, close_fds=True)
                except OSError:
                    if stopping.wait(RESTART_BACKOFF):
                        break
                    continue
                while child.poll() is None and not stopping.is_set():
                    try:
                        config = _read_json(state / 'recovery/config.json')
                    except (OSError, ValueError):
                        return 1
                    if config.get('enabled') is not True or config.get('mode') != 'alerts-only':
                        return 0
                    stopping.wait(0.5)
                _stop_child(child)
                child = None
                if stopping.wait(RESTART_BACKOFF):
                    break
            return 0
        finally:
            _stop_child(child)
            for sig, handler in previous.items():
                signal.signal(sig, handler)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('supervise', 'status'))
    parser.add_argument('--state-dir', type=Path, required=True)
    parser.add_argument('--repo-dir', type=Path)
    parser.add_argument('--python')
    args = parser.parse_args()
    os.umask(0o077)
    if args.action == 'status':
        print(json.dumps(monitor_status(args.state_dir, args.repo_dir or Path(__file__).resolve().parent,
                                        args.python or sys.executable), ensure_ascii=False, indent=2))
    else:
        if args.repo_dir is None or args.python is None:
            parser.error('supervise requires explicit --repo-dir and --python for process identity')
        raise SystemExit(supervise(args.state_dir, args.repo_dir, args.python))


if __name__ == '__main__':
    main()
