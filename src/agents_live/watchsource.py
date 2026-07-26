"""Watch sources: where changed paths come from on this host.

The dispatch point of the watcher track (docs/windows-support.md). A
source is asked for the paths that changed under a set of directories,
and it answers in the only vocabulary the loop above needs: absolute
paths, in batches, with a timeout. What it does underneath - drive
``inotifywait`` and read its stdout, or hold directory handles and read
change records - stays here.

The policy that decides which of those paths matter, how they are
batched, and when they dispatch is ``watchpolicy``, and it never
learned about either mechanism. This module is the other half of that
split: the I/O, kept away from the rules.
"""
from __future__ import annotations

import contextlib
import os
import subprocess
import sys
from pathlib import Path
from typing import Protocol

from . import hostruntime

if sys.platform == "win32":  # pragma: no cover - imported for its side effect
    from .winwatch import WatchFailed, WindowsEventSource
else:
    class WatchFailed(RuntimeError):
        """The watch cannot continue and will not recover by retrying."""

    WindowsEventSource = None


class EventSource(Protocol):
    """A stream of changed paths, in batches, that can be stopped."""

    def start(self) -> None: ...

    def poll(self, timeout: float | None) -> list[str]: ...

    def stop(self) -> None: ...


class PosixEventSource:
    """Changed paths from a monitoring ``inotifywait`` child process.

    The long-standing Linux and WSL implementation, moved here whole:
    one process watching every directory, printing one absolute path
    per line, read without blocking so the loop above can time its own
    debounce window.
    """

    # close_write catches direct writes; moved_to catches atomic saves
    # and files arriving by temp-and-rename, which produce no
    # close_write at all; moved_from and delete catch files leaving.
    EVENTS = "close_write,moved_to,moved_from,delete"

    def __init__(self, directories, *, cwd: Path) -> None:
        self.directories = [Path(d) for d in directories]
        self._cwd = cwd
        self._process: subprocess.Popen | None = None
        self._pending = ""

    def start(self) -> None:
        import fcntl

        self._process = subprocess.Popen(
            ["inotifywait", "-m", "-r", "-e", self.EVENTS,
             *[str(d) for d in self.directories], "--format", "%w%f"],
            cwd=self._cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1)
        if self._process.stdout is None:
            raise WatchFailed("watcher stdout was not available")
        descriptor = self._process.stdout.fileno()
        # The raw descriptor, not the wrapper: TextIOWrapper.read can
        # buffer internally and leave select saying nothing is ready
        # while a batch of events sits in user space.
        flags = fcntl.fcntl(descriptor, fcntl.F_GETFL)
        fcntl.fcntl(descriptor, fcntl.F_SETFL, flags | os.O_NONBLOCK)

    def poll(self, timeout: float | None) -> list[str]:
        import select

        process = self._process
        if process is None or process.stdout is None:
            raise WatchFailed("the watcher was never started")
        descriptor = process.stdout.fileno()
        ready, _, _ = select.select([descriptor], [], [],
                                    *(() if timeout is None else (timeout,)))
        if not ready:
            return []
        paths: list[str] = []
        try:
            while True:
                raw = os.read(descriptor, 8192)
                if not raw:
                    raise WatchFailed(self._exit_reason())
                self._pending += raw.decode("utf-8", errors="replace")
                while "\n" in self._pending:
                    line, self._pending = self._pending.split("\n", 1)
                    if line.strip():
                        paths.append(line.strip())
        except (BlockingIOError, OSError) as exc:
            if isinstance(exc, BlockingIOError) or paths:
                return paths
            raise
        return paths

    def _exit_reason(self) -> str:
        process = self._process
        detail = ""
        if process is not None and process.stderr is not None:
            try:
                detail = process.stderr.read().strip()
            except OSError:
                detail = ""
        code = process.poll() if process is not None else None
        return f"inotifywait exited (rc={code})" + (f": {detail}" if detail else "")

    def stop(self) -> None:
        process = self._process
        if process is None:
            return
        if process.poll() is None:
            process.terminate()
        # Close the pipes we own: a watcher that returns without this
        # leaks both descriptors until the process itself exits.
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                with contextlib.suppress(OSError):
                    stream.close()


def open_source(directories, *, cwd: Path) -> EventSource:
    """The event source this host has, started and ready to poll."""
    if hostruntime.id() == hostruntime.WINDOWS:
        source: EventSource = WindowsEventSource(directories)
    else:
        source = PosixEventSource(directories, cwd=cwd)
    source.start()
    return source


def mechanism() -> str:
    """What this host watches files with, for the watcher's own log."""
    return ("ReadDirectoryChangesW" if hostruntime.id() == hostruntime.WINDOWS
            else "inotifywait")
