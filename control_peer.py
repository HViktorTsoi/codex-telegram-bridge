#!/usr/bin/env python3
"""Private SSH transport for the control center; never collects Telegram updates."""
import argparse
import json
from pathlib import Path
import sys

from control_center import export_bundle, receive_bundle


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['snapshot', 'sync'])
    parser.add_argument('--state-dir', type=Path, required=True)
    args = parser.parse_args()
    if args.action == 'snapshot':
        result = export_bundle(args.state_dir)
    else:
        raw = sys.stdin.read(4 * 1024 * 1024 + 1)
        if len(raw) > 4 * 1024 * 1024:
            raise ValueError('Control payload exceeds limit')
        result = receive_bundle(args.state_dir, json.loads(raw))
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
