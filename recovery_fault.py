"""Record a receiver failure without exposing user text or Bot credentials."""
import json
import os
from pathlib import Path
import secrets
import time


def _busy(bridge):
    try:
        db = bridge.store.db
        if db.execute("SELECT 1 FROM inbox WHERE state IN ('pending','dispatching','uncertain','running') LIMIT 1").fetchone():
            return True
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='completion_turns'").fetchone():
            return bool(db.execute("SELECT 1 FROM completion_turns WHERE stage IN ('active','closing') AND notify=1 LIMIT 1").fetchone())
    except Exception:
        pass
    return False


def record_poll_failure(component, code, *, clock=time.time):
    """Best effort only; a notification failure must never hide the poll error."""
    component.last_error = 'Telegram poll: ' + str(code)
    try:
        targets = getattr(component, 'targets', {})
        wanted = {key for key, bridge in getattr(component, 'bridges', {}).items() if _busy(bridge)}
        try:
            for key, thread, topic in component.store.db.execute(
                    "SELECT DISTINCT target_key,thread_id,topic_id FROM jobs WHERE state='pending' AND error IS NULL"):
                target = targets.get(key)
                if target and target.get('thread_id') == thread and target.get('message_thread_id') == topic:
                    wanted.add(key)
        except Exception:
            pass
        if not wanted and component.config.get('root_target') in targets:
            wanted.add(component.config['root_target'])
        selected = [{'target_key': key, 'thread_id': target['thread_id'], 'topic_id': target['message_thread_id']}
                    for key, target in targets.items() if key in wanted and not target.get('topic_missing')]
        data = {'id': secrets.token_hex(16), 'at': clock(), 'code': int(code),
                'reason': 'telegram_poll_conflict' if code == 409 else 'telegram_auth_failed',
                'host_id': component.config.get('host_id'), 'targets': selected}
        directory = Path(component.state_dir) / 'control'
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = directory / 'recovery-fault.json'
        temporary = path.with_name(path.name + '.' + secrets.token_hex(6) + '.tmp')
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'w') as output:
                json.dump(data, output, ensure_ascii=False)
                output.write('\n')
                output.flush()
                os.fsync(output.fileno())
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
        return True
    except Exception:
        return False


def mark_maintenance(state_dir, *, until=None, pid=None):
    """Suppress generic stopped-process alerts during an intentional stop."""
    directory = Path(state_dir) / 'recovery'
    if not (directory / 'config.json').exists():
        return
    path = directory / 'maintenance.json'
    if until is None:
        path.unlink(missing_ok=True)
        return
    temporary = path.with_name(path.name + '.' + secrets.token_hex(6) + '.tmp')
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as output:
            json.dump({'at': time.time(), 'until': until, 'pid': pid, 'reason': 'manual_stop'}, output)
            output.write('\n')
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
