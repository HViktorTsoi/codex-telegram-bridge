#!/usr/bin/env python3
"""Start/stop the private Telegram bridge in a detached tmux session."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import time

STATE = Path.home() / ".local/state/codex-telegram"
SESSION = "codex-telegram-bridge"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["start", "stop", "status", "logs"])
    parser.add_argument("--notify", action="store_true")
    args = parser.parse_args()
    os.umask(0o077)
    exists = subprocess.run(["tmux", "has-session", "-t", "=" + SESSION], capture_output=True).returncode == 0
    if args.action == "start":
        if exists:
            print("Telegram bridge is already running.")
            return
        if not (STATE / "config.json").exists():
            raise SystemExit(f"Missing configuration: {STATE / 'config.json'}")
        log_path = STATE / "bridge.log"
        log_path.touch(mode=0o600, exist_ok=True)
        log_path.chmod(0o600)
        config = json.loads((STATE / "config.json").read_text())
        entrypoint = "topics_bridge.py" if config.get("mode") == "topics" else "bridge.py"
        command = [sys.executable, "-u", str(Path(__file__).with_name(entrypoint)), "run", "--state-dir", str(STATE)]
        if args.notify:
            if config.get("mode") == "topics":
                raise SystemExit("Topic mode uses its bound topics; send /sessions to inspect the connection.")
            command.append("--notify")
        shell_command = "exec " + shlex.join(command) + " >> " + shlex.quote(str(STATE / "bridge.log")) + " 2>&1"
        subprocess.run(["tmux", "new-session", "-d", "-s", SESSION, "-c", str(Path.home()), shell_command], check=True)
        print("Telegram bridge started in tmux: " + SESSION)
    elif args.action == "stop":
        if exists:
            subprocess.run(["tmux", "kill-session", "-t", "=" + SESSION], check=True)
            # Allow signal cleanup to release the singleton lock before a restart.
            with (STATE / "bridge.lock").open("a") as lock:
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
        p = STATE / "bridge.log"
        print("\n".join(p.read_text().splitlines()[-30:]) if p.exists() else "No logs yet.")
    else:
        path = STATE / "health.json"
        data = json.loads(path.read_text()) if path.exists() else {}
        data["tmux_running"] = exists
        data["health_age_seconds"] = round(time.time() - data["updated_at"], 1) if data.get("updated_at") else None
        print(json.dumps(data, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
