"""Windows file-change notification, through ``ReadDirectoryChangesW``.

The Windows leaf of the watcher track (docs/windows-support.md, File
change notification on Windows). One directory handle per watched root,
recursive, read on a thread that hands whole paths to a queue; the loop
that wants them never touches a handle, an overlapped structure, or a
byte offset.

Option A of the three the design weighed, chosen after a spike measured
the four behaviors that decide whether it can be relied on: a read
cancelled from another thread returns rather than hanging, a rename
arrives as an ordered pair, an overflowed buffer is reported as a
zero-length read rather than silently dropped records, and a deleted
root fails the read instead of spinning. Nothing here is a dependency:
``ctypes`` and the kernel are already present on every Windows host.

Overflow is not recoverable by re-reading, because the records that
would have said what changed are gone. It degrades to a bounded rescan
of the watched directories, which is a superset of what was missed and
is bounded so a storm cannot turn into unbounded work.
"""
from __future__ import annotations

import ctypes
import queue
import struct
import threading
from pathlib import Path

# Spelled out rather than imported from ``ctypes.wintypes``, which only
# exists on Windows. The record walking and the overflow recovery below
# are ordinary logic, and they stay testable on the hosts where the
# suite runs. ``c_uint32`` rather than ``c_ulong``: DWORD is four bytes
# everywhere, ``c_ulong`` is eight on 64-bit Linux.
_DWORD = ctypes.c_uint32
_HANDLE = ctypes.c_void_p

# Filter: names, directory names, sizes, and last-write times. Enough to
# see a file written, created, renamed, or removed; not so much that a
# metadata touch wakes an agent.
_NOTIFY_FILTER = 0x01 | 0x02 | 0x08 | 0x10

_FILE_LIST_DIRECTORY = 0x0001
_FILE_SHARE_ALL = 0x07
_OPEN_EXISTING = 3
_FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
_FILE_FLAG_OVERLAPPED = 0x40000000
_INVALID_HANDLE = ctypes.c_void_p(-1).value
_WAIT_OBJECT_0 = 0
_WAIT_TIMEOUT = 0x102

# A read that was cancelled is the normal way this watcher stops, so it
# is not an error; anything else that fails a read is terminal.
_ERROR_OPERATION_ABORTED = 995

# Big enough that ordinary editing never overflows it, small enough that
# one per watched directory is not worth worrying about. The kernel
# cannot use more than 64 KiB for a remote directory in any case.
_BUFFER_BYTES = 64 * 1024

# How much a rescan after overflow is allowed to cost. A storm large
# enough to overflow the buffer is exactly the situation where an
# unbounded directory walk would make things worse.
RESCAN_FILE_LIMIT = 2000

# How many events may wait to be read. The reader threads produce as
# fast as the kernel reports; the loop consumes only between debounce
# windows and agent runs. An unbounded queue turns that gap into
# unbounded memory, and a watcher that survives a storm by exhausting
# the machine has not survived it. Past the bound, events are dropped
# and the drop is recorded, which degrades to the same bounded rescan
# a kernel buffer overflow does.
QUEUE_LIMIT = 4096


class WatchFailed(RuntimeError):
    """The watch cannot continue: the root went away, or a read failed."""


class _Overlapped(ctypes.Structure):
    _fields_ = [("Internal", ctypes.c_void_p),
                ("InternalHigh", ctypes.c_void_p),
                ("Offset", _DWORD),
                ("OffsetHigh", _DWORD),
                ("hEvent", _HANDLE)]


def _kernel32():
    """kernel32 with the signatures this module relies on declared.

    Without the return types below, ctypes assumes a C ``int`` and a
    64-bit handle comes back truncated - the failure looks like an
    invalid handle on a directory that plainly exists.
    """
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateFileW.restype = ctypes.c_void_p
    k32.CreateEventW.restype = ctypes.c_void_p
    k32.ReadDirectoryChangesW.restype = ctypes.c_int
    k32.GetOverlappedResult.restype = ctypes.c_int
    k32.WaitForSingleObject.restype = _DWORD
    k32.WaitForSingleObject.argtypes = [ctypes.c_void_p, _DWORD]
    k32.CancelIoEx.restype = ctypes.c_int
    k32.CloseHandle.restype = ctypes.c_int
    k32.CloseHandle.argtypes = [ctypes.c_void_p]
    k32.ResetEvent.restype = ctypes.c_int
    k32.ResetEvent.argtypes = [ctypes.c_void_p]
    return k32


class DirectoryWatch:
    """One recursive watch on one directory, read on its own thread."""

    def __init__(self, directory: Path, sink: queue.Queue) -> None:
        self.directory = directory
        #: Set when the sink was full and an event was dropped. Read and
        #: cleared by the source, which then treats this watch as having
        #: overflowed.
        self.dropped = threading.Event()
        self._sink = sink
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name=f"watch:{directory}", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def join(self, timeout: float | None = None) -> None:
        self._thread.join(timeout)

    def _run(self) -> None:
        k32 = _kernel32()
        try:
            handle = self._open(k32)
        except OSError as exc:
            self._sink.put(("failed", str(exc)))
            return
        event = k32.CreateEventW(None, True, False, None)
        buffer = ctypes.create_string_buffer(_BUFFER_BYTES)
        overlapped = _Overlapped()
        overlapped.hEvent = event
        returned = _DWORD()
        try:
            while not self._stop.is_set():
                k32.ResetEvent(event)
                if not k32.ReadDirectoryChangesW(
                        handle, buffer, _BUFFER_BYTES, True, _NOTIFY_FILTER,
                        ctypes.byref(returned), ctypes.byref(overlapped), None):
                    self._fail(k32, "the directory could not be watched")
                    return
                if not self._await(k32, handle, event, overlapped):
                    return
                if not k32.GetOverlappedResult(handle, ctypes.byref(overlapped),
                                               ctypes.byref(returned), True):
                    if ctypes.get_last_error() == _ERROR_OPERATION_ABORTED:
                        return
                    self._fail(k32, "the watched directory is no longer readable")
                    return
                if returned.value == 0:
                    # The kernel had more changes than the buffer could
                    # hold. What they were is unrecoverable; say so.
                    self._offer("overflow", str(self.directory))
                    continue
                for name in _records(buffer):
                    self._offer("path", str(self.directory / name))
        finally:
            k32.CloseHandle(event)
            k32.CloseHandle(handle)

    def _await(self, k32, handle, event, overlapped) -> bool:
        """Wait for the read, cancelling it if a stop arrives first."""
        while True:
            status = k32.WaitForSingleObject(event, 200)
            if status == _WAIT_OBJECT_0:
                return True
            if status != _WAIT_TIMEOUT:
                self._fail(k32, "waiting on the directory failed")
                return False
            if self._stop.is_set():
                # CancelIoEx is the only way out of a pending read; the
                # spike confirmed the wait then returns rather than
                # leaving a thread parked on a handle forever.
                k32.CancelIoEx(handle, ctypes.byref(overlapped))
                k32.WaitForSingleObject(event, 1000)
                return False

    def _offer(self, kind: str, value: str) -> None:
        """Queue an event, or record that the queue could not take it.

        Never blocks. A blocked reader stops calling
        ``ReadDirectoryChangesW``, and records that arrive while no read
        is pending are lost in the kernel - the same loss the bound
        exists to survive, arrived at by stalling instead. Dropping and
        remembering is honest and recovers the same way.
        """
        try:
            self._sink.put_nowait((kind, value))
        except queue.Full:
            self.dropped.set()

    def _fail(self, k32, message: str) -> None:
        code = ctypes.get_last_error()
        # Blocking, unlike an ordinary event: a terminal failure the loop
        # never hears about is a watch that has silently stopped
        # working. The thread is a daemon and is leaving either way.
        self._sink.put(("failed", f"{message} ({self.directory}): error {code}"))

    def _open(self, k32):
        handle = k32.CreateFileW(
            str(self.directory), _FILE_LIST_DIRECTORY, _FILE_SHARE_ALL, None,
            _OPEN_EXISTING,
            _FILE_FLAG_BACKUP_SEMANTICS | _FILE_FLAG_OVERLAPPED, None)
        if handle == _INVALID_HANDLE:
            raise ctypes.WinError(ctypes.get_last_error())
        return _HANDLE(handle)


def _records(buffer) -> list[str]:
    """The file names in one filled ``FILE_NOTIFY_INFORMATION`` chain.

    Read with ``struct`` and decoded as UTF-16LE rather than through
    ``wstring_at``, whose ``wchar_t`` is four bytes off Windows: the
    layout is the kernel's, not the host compiler's, so reading it
    should not depend on the host either.

    Offsets come from the kernel and are walked defensively: a record
    that would read past the buffer ends the chain rather than reading
    memory that is not ours.
    """
    names: list[str] = []
    offset = 0
    size = len(buffer)
    while True:
        if offset + 12 > size:
            break
        next_entry, _action, length = struct.unpack_from("<III", buffer, offset)
        if offset + 12 + length > size:
            break
        names.append(bytes(buffer[offset + 12:offset + 12 + length])
                     .decode("utf-16-le", errors="replace"))
        if not next_entry or offset + next_entry <= offset:
            break
        offset += next_entry
    return names


def rescan(directories) -> list[str]:
    """Every file under *directories*, up to the rescan limit.

    What an overflow degrades to. A superset of the changes that were
    lost, bounded so that the response to a storm cannot itself be a
    storm.
    """
    found: list[str] = []
    for directory in directories:
        for path in Path(directory).rglob("*"):
            if len(found) >= RESCAN_FILE_LIMIT:
                return found
            if path.is_file():
                found.append(str(path))
    return found


class WindowsEventSource:
    """Paths that changed under a set of directories, as they change."""

    def __init__(self, directories) -> None:
        #: What the directory threads put their findings on, as
        #: ``(kind, value)``: a path, an overflowed directory, a
        #: terminal failure, or the sentinel that ends a wait. Bounded:
        #: see ``QUEUE_LIMIT``.
        self.events: queue.Queue = queue.Queue(maxsize=QUEUE_LIMIT)
        self._watches = [DirectoryWatch(Path(d), self.events)
                         for d in directories]
        self._overflowed: set[str] = set()

    def start(self) -> None:
        for watch in self._watches:
            watch.start()

    def poll(self, timeout: float | None) -> list[str]:
        """Absolute paths seen within *timeout*; empty if none were.

        Blocks for at most *timeout* seconds, or until the first path
        arrives, then drains whatever else is already queued: the loop
        above wants batches, not one path at a time.
        """
        paths: list[str] = []
        try:
            paths.append(self._take(self.events.get(timeout=timeout)))
        except queue.Empty:
            pass
        else:
            while True:
                try:
                    paths.append(self._take(self.events.get_nowait()))
                except queue.Empty:
                    break
        # Checked even when nothing was waiting: a drop recorded just
        # after the last drain would otherwise sit unanswered until some
        # unrelated change happened to arrive.
        for watch in self._watches:
            if watch.dropped.is_set():
                watch.dropped.clear()
                self._overflowed.add(str(watch.directory))
        if self._overflowed:
            paths.extend(rescan(sorted(self._overflowed)))
            self._overflowed.clear()
        return [path for path in paths if path]

    def _take(self, item) -> str:
        kind, value = item
        if kind == "failed":
            raise WatchFailed(value)
        if kind == "overflow":
            self._overflowed.add(value)
            return ""
        if kind == "stopped":
            return ""
        return value

    def stop(self) -> None:
        for watch in self._watches:
            watch.stop()
        # A poll with no debounce window pending waits without a
        # timeout; the sentinel is what lets a stop reach it. A full
        # queue needs no sentinel - the poll it would wake is already
        # returning events - and waiting for room would hang the stop.
        try:
            self.events.put_nowait(("stopped", ""))
        except queue.Full:
            pass
        for watch in self._watches:
            watch.join(2)
