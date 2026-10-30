#!/usr/bin/env python3
"""Manage the recurring KMB reminder without interrupting the chat bridge."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import time

STATE = Path.home() / ".local/state/kmb72-reminder"
SESSION = "kmb72-reminder"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["start", "stop", "status", "logs", "check", "test-voice"])
    args = parser.parse_args()
    os.umask(0o077)
    exists = subprocess.run(["tmux", "has-session", "-t", "=" + SESSION], capture_output=True).returncode == 0
    script = Path(__file__).with_name("bus_reminder.py")
    if args.action == "start":
        if exists:
            print("KMB reminder is already running.")
            return
        if not (STATE / "config.json").exists():
            raise SystemExit("Reminder configuration is missing")
        log = STATE / "monitor.log"
        log.touch(mode=0o600, exist_ok=True)
        log.chmod(0o600)
        command = "exec " + shlex.join([sys.executable, "-u", str(script), "run"])
        command += " >> " + shlex.quote(str(log)) + " 2>&1"
        subprocess.run(["tmux", "new-session", "-d", "-s", SESSION, "-c", str(Path.home()), command], check=True)
        print("KMB reminder started.")
    elif args.action == "stop":
        if exists:
            subprocess.run(["tmux", "kill-session", "-t", "=" + SESSION], check=True)
            with (STATE / "monitor.lock").open("a") as lock:
                for _ in range(50):
                    try:
                        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        time.sleep(0.1)
        print("KMB reminder stopped.")
    elif args.action == "logs":
        log = STATE / "monitor.log"
        print("\n".join(log.read_text().splitlines()[-25:]) if log.exists() else "No logs yet.")
    elif args.action == "status":
        path = STATE / "health.json"
        status = json.loads(path.read_text()) if path.exists() else {}
        status["tmux_running"] = exists
        print(json.dumps(status, ensure_ascii=False, indent=2))
    else:
        subprocess.run([sys.executable, str(script), args.action], check=True)


if __name__ == "__main__":
    main()
