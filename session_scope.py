"""Fail-closed native-thread ownership shared by independent Telegram Bots."""
from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import stat
import tempfile


SCOPES = frozenset({'general', 'research', 'development'})


class SessionScopeError(ValueError):
    """A safe, user-readable ownership or registry failure."""


def _thread_id(value):
    return (isinstance(value, str) and bool(value) and value == value.strip()
            and not any(ord(character) < 32 for character in value))


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise SessionScopeError('会话归属文件包含重复字段，已拒绝使用。')
        result[key] = value
    return result


def _signature(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


class SessionScope:
    def __init__(self, config):
        if not isinstance(config, dict):
            raise SessionScopeError('会话归属配置格式无效。')
        configured = ('session_scope_file' in config, 'session_scope' in config)
        self.enabled = any(configured)
        self.path = self.scope = None
        self._cached_signature = self._cached_registry = None
        if not self.enabled:
            return
        filename, scope = config.get('session_scope_file'), config.get('session_scope')
        if (not all(configured) or not isinstance(filename, str) or not filename.strip()
                or not Path(filename).is_absolute() or not isinstance(scope, str) or scope not in SCOPES):
            raise SessionScopeError('请同时配置绝对路径的会话归属文件和有效的 Bot 分组。')
        self.path, self.scope = Path(filename), scope
        self._load()

    @staticmethod
    def _validate(registry):
        if (not isinstance(registry, dict) or type(registry.get('version')) is not int
                or registry['version'] != 1):
            raise SessionScopeError('会话归属文件版本或格式无效。')
        scopes = registry.get('scopes')
        if (not isinstance(scopes, list) or len(scopes) != len(SCOPES)
                or not all(isinstance(scope, str) for scope in scopes)
                or set(scopes) != SCOPES):
            raise SessionScopeError('会话归属文件的 Bot 分组列表无效。')
        if registry.get('default_scope') not in SCOPES:
            raise SessionScopeError('会话归属文件的默认 Bot 分组无效。')
        assignments = registry.get('assignments')
        if (not isinstance(assignments, dict)
                or any(not _thread_id(thread) or not isinstance(scope, str) or scope not in SCOPES
                       for thread, scope in assignments.items())):
            raise SessionScopeError('会话归属文件的会话分配记录无效。')
        return registry

    def _load(self, *, force=False):
        try:
            info = self.path.lstat()
            if not stat.S_ISREG(info.st_mode):
                raise SessionScopeError('会话归属文件必须是普通文件，不能使用符号链接。')
            signature = _signature(info)
            if not force and signature == self._cached_signature:
                return self._cached_registry
            # Replacement is the supported writer protocol. Retry if it races a read.
            for _ in range(3):
                descriptor = os.open(self.path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
                with os.fdopen(descriptor, 'r', encoding='utf-8') as source:
                    before = os.fstat(source.fileno())
                    if not stat.S_ISREG(before.st_mode):
                        raise SessionScopeError('会话归属文件必须是普通文件。')
                    registry = json.load(source, object_pairs_hook=_unique_object)
                    after = os.fstat(source.fileno())
                current = self.path.lstat()
                if (_signature(before) == _signature(after) == _signature(current)
                        and stat.S_ISREG(current.st_mode)):
                    registry = self._validate(registry)
                    self._cached_signature, self._cached_registry = _signature(current), registry
                    return registry
            raise SessionScopeError('会话归属文件正在变化，请稍后重试。')
        except SessionScopeError:
            raise
        except (OSError, UnicodeError, ValueError, TypeError):
            # Never fall back to an older cached allocation after a read failure.
            raise SessionScopeError('无法读取有效的会话归属文件，已停止此 Bot 的会话操作。') from None

    def allows(self, thread_id):
        if not self.enabled:
            return True
        registry = self._load()
        return (_thread_id(thread_id)
                and registry['assignments'].get(thread_id, registry['default_scope']) == self.scope)

    def require(self, thread_id):
        if not self.allows(thread_id):
            raise SessionScopeError('该会话不属于当前 Bot，不能绑定或操作。')

    def filter_threads(self, threads):
        if not self.enabled:
            return list(threads)
        registry = self._load()
        return [thread for thread in threads if isinstance(thread, dict) and _thread_id(thread.get('id'))
                and registry['assignments'].get(thread['id'], registry['default_scope']) == self.scope]

    def claim_created(self, thread_id):
        """Assign a just-created native ID; callers must not pass existing sessions."""
        if not self.enabled:
            return
        if not _thread_id(thread_id):
            raise SessionScopeError('新建会话的原生 ID 无效，不能登记 Bot 归属。')
        descriptor, temporary = None, None
        try:
            # Lock a separate inode: replacing the JSON must not replace the lock.
            lock_path = self.path.with_name(self.path.name + '.lock')
            descriptor = os.open(lock_path, os.O_WRONLY | os.O_CREAT | os.O_NONBLOCK | os.O_NOFOLLOW, 0o600)
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise SessionScopeError('会话归属锁文件无效，不能登记新建会话。')
            os.fchmod(descriptor, 0o600)
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            registry = self._load(force=True)
            previous = registry['assignments'].get(thread_id)
            if previous is not None:
                if previous != self.scope:
                    raise SessionScopeError('该会话已有其他 Bot 归属，不能覆盖原有分配。')
                return
            # Copy rather than mutate a validated snapshot still held by readers.
            updated = {**registry, 'assignments': {**registry['assignments'], thread_id: self.scope}}
            temporary_descriptor, temporary = tempfile.mkstemp(
                prefix='.' + self.path.name + '.', suffix='.tmp', dir=self.path.parent)
            with os.fdopen(temporary_descriptor, 'w', encoding='utf-8') as output:
                os.fchmod(output.fileno(), 0o600)
                json.dump(updated, output, ensure_ascii=False, indent=2)
                output.write('\n')
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.path)
            temporary = None
            directory = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
            self._load(force=True)
        except SessionScopeError:
            raise
        except (OSError, UnicodeError, ValueError, TypeError):
            raise SessionScopeError('新建会话的 Bot 归属未能确认保存，请核对后再继续。') from None
        finally:
            if temporary is not None:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass
            if descriptor is not None:
                os.close(descriptor)
