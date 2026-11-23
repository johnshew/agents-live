#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Run a command with no visible window on Windows.

Usage:
  uv run --script tools/run-hidden.py [--detach] [--log FILE] -- COMMAND [ARGS...]

Without --detach, waits for the command and returns its exit code. Its output
is appended to --log when given; otherwise it reaches the caller's own output:
the command shares the caller's console when there is one and stays windowless
when there is none (an agent's pipes, for example).

With --detach, starts the command in the background, prints its process ID and
returns at once. Its output is appended to --log, or discarded. Children of the
command inherit the hidden console, so tools it launches open no window either.

On other platforms the command runs unchanged; --detach starts it in a new
session so it outlives the caller's terminal.
"""

from __future__ import annotations

import argparse
import contextlib
import subprocess
import sys


def creation_flags(detach: bool, *, share_console: bool = False) -> int:
    """Windows process-creation flags that keep the command windowless.

    A command that relays output shares the caller's console when one exists:
    CREATE_NO_WINDOW would detach it from that console, and writes to the
    inherited console handles would then vanish without an error.
    """
    if sys.platform != "win32":
        return 0
    if share_console:
        import ctypes

        if ctypes.windll.kernel32.GetConsoleWindow():
            return 0
    flags = subprocess.CREATE_NO_WINDOW
    if detach:
        flags |= subprocess.CREATE_NEW_PROCESS_GROUP
    return flags


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--detach", action="store_true",
                        help="start in the background and print the process ID")
    parser.add_argument("--log", help="append combined stdout and stderr to this file")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("no command given")

    with contextlib.ExitStack() as stack:
        log = stack.enter_context(open(args.log, "ab")) if args.log else None
        try:
            if args.detach:
                process = subprocess.Popen(
                    command,
                    stdin=subprocess.DEVNULL,
                    stdout=log or subprocess.DEVNULL,
                    stderr=subprocess.STDOUT,
                    creationflags=creation_flags(True),
                    start_new_session=sys.platform != "win32",
                    close_fds=True,
                )
                print(process.pid, flush=True)
                return 0
            return subprocess.run(
                command,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT if log else None,
                creationflags=creation_flags(False, share_console=log is None),
                check=False,
            ).returncode
        except OSError as error:
            print(f"run-hidden: cannot start {command[0]}: {error}", file=sys.stderr)
            return 127


if __name__ == "__main__":
    sys.exit(main())
