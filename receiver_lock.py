"""Keep one local Telegram receiver per verified Bot identity, across states."""
from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import pwd
import re
import stat


class ReceiverAlreadyRunning(RuntimeError):
    """Another receiver owns this Bot on the current user's computer."""


def _user_home():
    # HOME/XDG overrides must not let the same Unix user get a second Bot lock.
    return Path(pwd.getpwuid(os.getuid()).pw_dir)


def _open_directory(directory):
    """Anchor every path component without following parent symlinks."""
    path = Path(os.path.abspath(directory))
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    descriptor = os.open(path.anchor, flags)
    try:
        for part in path.parts[1:]:
            try:
                child = os.open(part, flags, dir_fd=descriptor)
            except FileNotFoundError:
                try:
                    os.mkdir(part, mode=0o700, dir_fd=descriptor)
                except FileExistsError:
                    pass
                child = os.open(part, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _receiver_state(args, process, home):
    """Recognize actual receiver entrypoints, never send-only helpers."""
    if not args:
        return None
    index = 0
    if Path(args[0]).name not in {'bridge.py', 'topics_bridge.py'}:
        if not re.fullmatch(r'(?:python|pypy)[0-9.]*', Path(args[0]).name):
            return None
        index = 1
        while index < len(args) and args[index].startswith('-'):
            if args[index] in {'-c', '-m', '-W', '-X'}:
                return None
            index += 1
    if (index + 1 >= len(args) or Path(args[index]).name not in {'bridge.py', 'topics_bridge.py'}
            or args[index + 1] != 'run'):
        return None
    state = home / '.local/state/codex-telegram'
    rest = args[index + 2:]
    for offset, arg in enumerate(rest):
        if arg == '--state-dir' and offset + 1 < len(rest):
            state = Path(rest[offset + 1])
        elif arg.startswith('--state-dir='):
            state = Path(arg.split('=', 1)[1])
    if not state.is_absolute():
        state = (process / 'cwd').resolve(strict=True) / state
    return state


def legacy_receivers(username, *, proc_root=Path('/proc'), home=None):
    """Find running old receivers by public configuration and PID, without tokens.

    A matching health PID establishes that the process reached its receiver
    lifecycle. This excludes new concurrent starters still acquiring this lock.
    """
    home = _user_home() if home is None else Path(home)
    uid, own_pid = os.getuid(), os.getpid()
    wanted = username.lstrip('@').lower()
    found = []
    try:
        processes = list(Path(proc_root).iterdir())
    except OSError:
        raise RuntimeError('Local receiver processes could not be checked') from None
    for process in processes:
        if not process.name.isdecimal() or int(process.name) == own_pid:
            continue
        try:
            if process.stat().st_uid != uid:
                continue
            args = [os.fsdecode(arg) for arg in (process / 'cmdline').read_bytes().split(b'\0') if arg]
            state = _receiver_state(args, process, home)
            if state is None:
                continue
            config = json.loads((state / 'config.json').read_text())
            expected = config.get('expected_bot_username')
            if not isinstance(expected, str) or expected.lstrip('@').lower() != wanted:
                continue
            health = json.loads((state / 'health.json').read_text())
            if health.get('pid') == int(process.name):
                found.append(int(process.name))
        except (OSError, ValueError, TypeError, AttributeError):
            # Processes may exit mid-snapshot. Unverified candidates cannot
            # establish that a receiver is already polling this Bot.
            continue
    return sorted(found)


class BotReceiverLock:
    """Hold an advisory per-Bot flock until all receiver workers have stopped."""

    def __init__(self, bot_id, username, state_dir, *, directory=None, proc_root=Path('/proc')):
        if isinstance(bot_id, bool) or not re.fullmatch(r'[1-9][0-9]*', str(bot_id)):
            raise RuntimeError('Telegram returned an invalid Bot identity')
        if not isinstance(username, str) or not re.fullmatch(r'[A-Za-z0-9_]+', username.lstrip('@')):
            raise RuntimeError('Telegram returned an invalid Bot username')
        self.bot_id, self.username = int(bot_id), username.lstrip('@')
        self.state_dir = Path(state_dir).resolve()
        self.directory = (_user_home() / '.local/state/codex-telegram-pollers'
                          if directory is None else Path(directory))
        self.proc_root = Path(proc_root)
        self.fd = None

    def __enter__(self):
        if self.fd is not None:
            raise RuntimeError('Telegram receiver identity lock is already held')
        directory_fd = None
        descriptor = None
        try:
            directory_fd = _open_directory(self.directory)
            directory_stat = os.fstat(directory_fd)
            if directory_stat.st_uid != os.getuid() or stat.S_IMODE(directory_stat.st_mode) & 0o077:
                raise RuntimeError('Telegram receiver lock directory must be private to the current user')
            descriptor = os.open(str(self.bot_id) + '.lock',
                                 os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC,
                                 0o600, dir_fd=directory_fd)
            lock_stat = os.fstat(descriptor)
            if (not stat.S_ISREG(lock_stat.st_mode) or lock_stat.st_uid != os.getuid()
                    or lock_stat.st_nlink != 1 or stat.S_IMODE(lock_stat.st_mode) & 0o077):
                raise RuntimeError('Telegram receiver lock file must be private to the current user')
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ReceiverAlreadyRunning(
                    f'A local Telegram receiver is already running for @{self.username} (Bot {self.bot_id})') from None
            existing = legacy_receivers(self.username, proc_root=self.proc_root)
            if existing:
                raise ReceiverAlreadyRunning(
                    f'A local Telegram receiver is already running for @{self.username} (PID {existing[0]})')
            metadata = json.dumps({'pid': os.getpid(), 'bot_id': self.bot_id, 'bot_username': self.username,
                                   'state_dir': str(self.state_dir)}) + '\n'
            os.ftruncate(descriptor, 0)
            os.write(descriptor, metadata.encode())
            self.fd, descriptor = descriptor, None
            return self
        except OSError:
            raise RuntimeError('Telegram receiver identity lock could not be secured') from None
        finally:
            if descriptor is not None:
                os.close(descriptor)
            if directory_fd is not None:
                os.close(directory_fd)

    def __exit__(self, *exc):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
        # Never unlink: another process may already have opened this inode.
