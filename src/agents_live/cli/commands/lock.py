"""Run a command while holding a cross-platform advisory file lock."""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

from ...runtime.hosts import system as hostruntime


BUSY_EXIT = 75
POLL_SECONDS = 0.1


def main(argv: list[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser()
    parser.add_argument("path", type=Path)
    parser.add_argument("--timeout", type=float, default=0.0)
    if "--" not in raw:
        parser.error("a command is required after --")
    separator = raw.index("--")
    args = parser.parse_args(raw[:separator])
    command = raw[separator + 1:]
    if not command:
        parser.error("a command is required after --")
    if args.timeout < 0:
        parser.error("--timeout must be zero or greater")

    path = args.path.expanduser().resolve()
    deadline = time.monotonic() + args.timeout
    while True:
        try:
            with hostruntime.exclusive_lock(path, blocking=False):
                return subprocess.run(command, check=False).returncode
        except hostruntime.LockBusy:
            if time.monotonic() >= deadline:
                print(f"lock is busy: {path}", file=sys.stderr)
                return BUSY_EXIT
            time.sleep(min(POLL_SECONDS, max(0.0, deadline - time.monotonic())))


if __name__ == "__main__":
    raise SystemExit(main())
