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
import shutil
import subprocess
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

from . import system as hostruntime

if sys.platform == "win32":  # pragma: no cover - imported for its side effect
    from .windows_watch import WatchFailed, WindowsEventSource
else:
    class WatchFailed(RuntimeError):
        """The watch cannot continue and will not recover by retrying."""

    WindowsEventSource = None


# What a host is told about file changes with, named for the mechanism
# rather than the platform. `mechanism` decides which one this host has;
# everything that reports on watching takes the name from here.
INOTIFY = "inotifywait"
FSWATCH = "fswatch"
DIRECTORY_CHANGES = "ReadDirectoryChangesW"


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
            **hostruntime.CHILD_TEXT, bufsize=1)
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


class MacEventSource:
    """NUL-framed absolute paths from the fswatch FSEvents monitor."""

    EVENTS = ("Created", "Updated", "Removed", "Renamed")
    DIAGNOSTIC_LIMIT = 8192

    def __init__(self, directories: Sequence[str | Path], *, cwd: Path) -> None:
        self._cwd = cwd
        self.directories = [(cwd / directory).resolve() for directory in directories]
        self._process: subprocess.Popen[bytes] | None = None
        self._pending = b""
        self._diagnostic = b""
        self._stderr_open = False
        self._stdout_open = False

    def start(self) -> None:
        if self._process is not None:
            raise WatchFailed("fswatch source is already started")
        try:
            self._process = subprocess.Popen(
                [shutil.which(FSWATCH) or hostruntime.find_tool(FSWATCH) or FSWATCH,
                 "-0", "-r", "-m", "fsevents_monitor", "-l", "0.1",
                 *(f"--event={event}" for event in self.EVENTS),
                 "--", *(str(directory) for directory in self.directories)],
                cwd=self._cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                bufsize=0,
            )
            for stream in (self._process.stdout, self._process.stderr):
                if stream is None:
                    raise WatchFailed("fswatch output pipe was not available")
                os.set_blocking(stream.fileno(), False)
            self._stdout_open = self._stderr_open = True
        except OSError as exc:
            self.stop()
            raise WatchFailed(f"cannot start fswatch: {exc}") from exc

    def poll(self, timeout: float | None) -> list[str]:
        import select

        process = self._process
        if process is None or process.stdout is None or process.stderr is None:
            raise WatchFailed("fswatch source was not started")
        deadline = None if timeout is None else time.monotonic() + timeout
        paths: list[str] = []
        while True:
            streams = []
            if self._stdout_open:
                streams.append(process.stdout)
            if self._stderr_open:
                streams.append(process.stderr)
            if not self._stdout_open:
                raise WatchFailed(self._exit_reason())
            remaining = None if deadline is None else max(0, deadline - time.monotonic())
            ready, _, _ = select.select(streams, [], [], remaining)
            if not ready:
                if process.poll() is not None:
                    raise WatchFailed(self._exit_reason())
                return paths
            for stream in ready:
                try:
                    raw = os.read(stream.fileno(), 65536)
                except BlockingIOError:
                    continue
                if stream is process.stderr:
                    self._stderr_open = bool(raw)
                    self._diagnostic = (
                        self._diagnostic + raw)[-self.DIAGNOSTIC_LIMIT:]
                elif raw:
                    records = (self._pending + raw).split(b"\0")
                    self._pending = records.pop()
                    paths.extend(os.fsdecode(record) for record in records if record)
                else:
                    self._stdout_open = False
            if paths:
                return paths
            if deadline is not None and time.monotonic() >= deadline:
                return []

    def _exit_reason(self) -> str:
        process = self._process
        if process is not None and process.stderr is not None:
            try:
                raw = os.read(process.stderr.fileno(), self.DIAGNOSTIC_LIMIT)
            except BlockingIOError:
                raw = b""
            self._diagnostic = (self._diagnostic + raw)[-self.DIAGNOSTIC_LIMIT:]
        code = process.poll() if process is not None else None
        detail = self._diagnostic.decode("utf-8", errors="replace").strip()
        return f"fswatch exited (rc={code})" + (f": {detail}" if detail else "")

    def stop(self) -> None:
        process = self._process
        if process is None:
            return
        try:
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
        finally:
            for stream in (process.stdout, process.stderr):
                if stream is not None:
                    stream.close()
            self._process = None
            self._pending = self._diagnostic = b""
            self._stdout_open = self._stderr_open = False


def create_source(directories, *, cwd: Path) -> EventSource:
    """Construct this host's source without starting its lifetime."""
    if hostruntime.id() == hostruntime.WINDOWS:
        return WindowsEventSource(directories)
    if hostruntime.id() == hostruntime.MACOS:
        return MacEventSource(directories, cwd=cwd)
    return PosixEventSource(directories, cwd=cwd)


def open_source(directories, *, cwd: Path) -> EventSource:
    """The event source this host has, started and ready to poll."""
    source = create_source(directories, cwd=cwd)
    source.start()
    return source


def mechanism() -> str:
    """What this host watches files with, for the watcher's own log."""
    if hostruntime.id() == hostruntime.WINDOWS:
        return DIRECTORY_CHANGES
    return FSWATCH if hostruntime.id() == hostruntime.MACOS else INOTIFY
