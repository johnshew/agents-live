"""Native macOS process identity without lossy ps argument parsing."""
from __future__ import annotations

import os
from collections.abc import Iterator

import psutil


def start_time(pid: int) -> float | None:
    """Return the kernel creation time, or None when it is unavailable."""
    try:
        return psutil.Process(pid).create_time()
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return None


def snapshot() -> Iterator[tuple[int, list[str], float]]:
    """Yield PID, argv and creation time for the same live runtime process."""
    try:
        for process in psutil.process_iter():
            try:
                if process.uids().real != os.getuid():
                    continue
                name = process.name().lower()
                # Protected macOS applications can deny argv even to their owner.
                # Agents Live entry points execute in a Python interpreter.
                if not name.startswith(("python", "pypy")) and name not in ("agents-live", "al"):
                    continue
                started = process.create_time()
                argv = process.cmdline()
                if argv and process.is_running():
                    yield process.pid, argv, started
            except (psutil.NoSuchProcess, psutil.ZombieProcess):
                continue
            except psutil.AccessDenied as exc:
                raise RuntimeError(
                    f"cannot inspect macOS process {process.pid}; "
                    "process inventory is incomplete") from exc
    except psutil.Error as exc:
        raise RuntimeError("cannot enumerate macOS processes") from exc
