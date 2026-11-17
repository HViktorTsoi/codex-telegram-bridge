#!/usr/bin/env python3
"""Start/stop the private Telegram bridge in a detached tmux session."""
import argparse
import fcntl
import json
import os
import re
from pathlib import Path
import shlex
import subprocess
import sys
import time

from recovery_bootstrap import ensure_monitor, monitor_status

STATE = Path.home() / ".local/state/codex-telegram"
SESSION = "codex-telegram-bridge"


def start_alerts(state, config):
    if config.get("mode") != "topics":
        return
    result = ensure_monitor(state, Path(__file__).parent, sys.executable)
    status = result.get("status")
    if status == "healthy":
        print("Independent Telegram failure alerts are running.")
    elif status == "disabled":
        print("Independent Telegram failure alerts are disabled in configuration.")
        if result.get("running"):
            print("An existing monitor is still running; its service was left unchanged.")
    elif status == "unsupported":
        print("Existing recovery mode was left unchanged; default alerts were not started.")
    else:
        print("Receiver startup was handled, but independent failure alerts are not verified: "
              + str(result.get("error") or status or "unknown") + ". Retry manage.py start or check status.",
              file=sys.stderr)
        raise SystemExit(2)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["start", "stop", "status", "logs"])
    parser.add_argument("--notify", action="store_true")
    parser.add_argument("--state-dir", type=Path, default=STATE)
    parser.add_argument("--session-name", default=SESSION)
    args = parser.parse_args()
    state = args.state_dir.expanduser().resolve()
    session = args.session_name
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", session):
        parser.error("Invalid tmux session name")
    os.umask(0o077)
    exists = subprocess.run(["tmux", "has-session", "-t", "=" + session], capture_output=True).returncode == 0
    if args.action == "start":
        from recovery_fault import mark_maintenance
        try:
            mark_maintenance(state)
        except OSError:
            print("Recovery maintenance marker could not be cleared.", file=sys.stderr)
        if not (state / "config.json").exists():
            raise SystemExit(f"Missing configuration: {state / 'config.json'}")
        config = json.loads((state / "config.json").read_text())
        if exists:
            print("Telegram bridge is already running.")
            start_alerts(state, config)
            return
        log_path = state / "bridge.log"
        log_path.touch(mode=0o600, exist_ok=True)
        log_path.chmod(0o600)
        entrypoint = "topics_bridge.py" if config.get("mode") == "topics" else "bridge.py"
        command = [sys.executable, "-u", str(Path(__file__).with_name(entrypoint)), "run", "--state-dir", str(state)]
        if args.notify:
            if config.get("mode") == "topics":
                raise SystemExit("Topic mode uses its bound topics; send /sessions to inspect the connection.")
            command.append("--notify")
        shell_command = "exec " + shlex.join(command) + " >> " + shlex.quote(str(state / "bridge.log")) + " 2>&1"
        subprocess.run(["tmux", "new-session", "-d", "-s", session, "-c", str(Path.home()), shell_command], check=True)
        print("Telegram bridge started in tmux: " + session)
        start_alerts(state, config)
    elif args.action == "stop":
        from recovery_fault import mark_maintenance
        try:
            health_path = state / "health.json"
            health = json.loads(health_path.read_text()) if health_path.exists() else {}
            mark_maintenance(state, until=time.time() + 300, pid=health.get("pid"))
        except (OSError, ValueError):
            print("Recovery maintenance marker could not be saved.", file=sys.stderr)
        if exists:
            subprocess.run(["tmux", "kill-session", "-t", "=" + session], check=True)
            # Allow signal cleanup to release the singleton lock before a restart.
            with (state / "bridge.lock").open("a") as lock:
                for _ in range(50):
                    try:
                        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        time.sleep(0.1)
                else:
                    raise SystemExit("Bridge is still shutting down; check status before restarting.")
        print("Telegram bridge stopped.")
    elif args.action == "logs":
        p = state / "bridge.log"
        print("\n".join(p.read_text().splitlines()[-30:]) if p.exists() else "No logs yet.")
    else:
        path = state / "health.json"
        data = json.loads(path.read_text()) if path.exists() else {}
        data["tmux_running"] = exists
        data["health_age_seconds"] = round(time.time() - data["updated_at"], 1) if data.get("updated_at") else None
        data["recovery"] = monitor_status(state, Path(__file__).parent, sys.executable)
        print(json.dumps(data, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
