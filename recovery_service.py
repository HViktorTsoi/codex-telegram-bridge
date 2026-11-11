#!/usr/bin/env python3
"""Independently report receiver failures; this service does not receive updates."""
import argparse
import asyncio
import contextlib
import fcntl
import json
import logging
import os
from pathlib import Path
import signal
import time

from recovery_guard import Guardian


LOG = logging.getLogger('codex_telegram_recovery_service')
DEFAULT_STATE = Path.home() / '.local/state/codex-telegram'


def configuration(state_dir):
    data = json.loads((state_dir / 'recovery/config.json').read_text())
    if data.get('enabled') is not True or data.get('mode') != 'alerts-only':
        raise ValueError('This service currently requires enabled alerts-only mode')
    interval = data.get('interval_seconds', 5)
    if type(interval) not in (int, float) or not 1 <= interval <= 60:
        raise ValueError('interval_seconds must be between 1 and 60')
    return data, interval


def write_health(state_dir, guardian, *, error=None, running=True):
    try:
        receiver, _ = guardian.receiver_state()
    except Exception as failure:
        receiver, error = 'unknown', error or type(failure).__name__
    error = error or getattr(guardian, 'last_delivery_error', None)
    data = {'pid': os.getpid(), 'updated_at': time.time(), 'enabled': running,
            'mode': 'alerts-only', 'alerts_only': True, 'receiver_state': receiver,
            'last_error': error}
    path = state_dir / 'recovery/health.json'
    temporary = path.with_suffix('.json.tmp')
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n')
    temporary.chmod(0o600)
    temporary.replace(path)


async def serve(state_dir, repo_dir):
    _, interval = configuration(state_dir)
    guardian = Guardian(state_dir, repo_dir, None, alerts_only=True)
    try:
        while True:
            error = None
            try:
                await guardian.monitor_once()
            except Exception as failure:
                error = type(failure).__name__
                LOG.warning('Recovery monitor unavailable: %s', error)
            try:
                write_health(state_dir, guardian, error=error)
            except OSError as failure:
                LOG.warning('Recovery health unavailable: %s', type(failure).__name__)
            await asyncio.sleep(interval)
    finally:
        with contextlib.suppress(Exception):
            write_health(state_dir, guardian, running=False)
        await guardian.close()


async def probe(state_dir, repo_dir, target_key):
    """Display an explicitly labelled test in one current local topic."""
    configuration(state_dir)
    guardian = Guardian(state_dir, repo_dir, None, alerts_only=True)
    try:
        await guardian._ensure_telegram()
        config = guardian.config()
        target = next((target for target in config['targets'] if target['key'] == target_key), None)
        if not target or target.get('topic_missing'):
            raise ValueError('The test target is not a current bound topic')
        binding = (target['thread_id'], target['message_thread_id'])
        counts = guardian._queue_counts({'target_key': target_key})
        text = ('🧪 故障提示通道测试\n' + str(target.get('name') or '当前会话')
                + '\n这是测试消息，当前接收器未被停止。'
                + f"\n待处理消息：{counts['pending_messages']} · 附件：{counts['pending_files']}")
        sent = await guardian.telegram.call('sendMessage', chat_id=guardian.owner_id,
            message_thread_id=binding[1], text=text, disable_notification=True,
            protect_content=True, reply_markup={'inline_keyboard': []})
        if (not isinstance(sent, dict) or type(sent.get('message_id')) is not int
                or sent['message_id'] <= 0):
            raise ConnectionError('Test message acknowledgement unavailable; do not resend blindly')
        current = next((target for target in guardian.config()['targets'] if target['key'] == target_key), None)
        if (not current or current.get('topic_missing')
                or (current['thread_id'], current['message_thread_id']) != binding):
            raise ValueError('The test target binding changed')
        edited = await guardian.telegram.call('editMessageText', chat_id=guardian.owner_id,
            message_id=sent['message_id'], text=text + '\n\n✅ 测试完成：独立提示通道可用，已原地更新此消息。',
            reply_markup={'inline_keyboard': []})
        if edited is not True and (not isinstance(edited, dict) or edited.get('message_id') != sent['message_id']):
            raise ConnectionError('Test message edit acknowledgement unavailable')
        result = {'target_key': target_key, 'topic_id': binding[1], 'message_id': sent['message_id'],
                  'sent': True, 'edited_in_place': True, 'model_calls': 0, 'verified_at': time.time()}
        path = state_dir / 'recovery/probe.json'
        path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
        path.chmod(0o600)
        return result
    finally:
        await guardian.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('run', 'status', 'probe'))
    parser.add_argument('--state-dir', type=Path, default=DEFAULT_STATE)
    parser.add_argument('--target', help='Current local target key for the labelled notification test')
    args = parser.parse_args()
    state_dir = args.state_dir.expanduser().resolve()
    if args.action == 'status':
        print((state_dir / 'recovery/health.json').read_text())
        return
    os.umask(0o077)
    configuration(state_dir)
    if args.action == 'probe':
        if not args.target:
            parser.error('probe requires --target')
        print(json.dumps(asyncio.run(probe(state_dir, Path(__file__).parent, args.target)), ensure_ascii=False))
        return
    lock = (state_dir / 'recovery/service.lock').open('a')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        raise SystemExit('The recovery monitor is already running')
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')

    async def run():
        task = asyncio.create_task(serve(state_dir, Path(__file__).parent))
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            loop.add_signal_handler(sig, task.cancel)
        with contextlib.suppress(asyncio.CancelledError):
            await task

    try:
        asyncio.run(run())
    finally:
        lock.close()


if __name__ == '__main__':
    main()
