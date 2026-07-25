"""Host runtime: which environment this process is in, and the process
and locking primitives that differ between environments.

The host-runtime seam described in docs/windows-support.md. Dispatch and
file change notification still live in their current modules; identity,
locking, detached spawning, liveness, and termination are extracted
here, because those are the operations whose POSIX spelling does not
survive the move to Windows.

The value returned by :func:`id` answers "which runtime environment is
this", not "which operating system". Linux and WSL are separate
environments on purpose: one physical machine can host both, they own
their agents independently, and only WSL carries the Windows-side
heartbeat integration.

The locking and process members were written after the Windows spike in
issue #126, not derived from the POSIX shape:

- A lock is a file lock on both platforms. Windows named mutexes were
  measured and rejected: ``Local\\`` names resolve per logon session, so
  an interactive process and a session 0 process would each believe they
  held the lock, and a crashed holder leaves ``WAIT_ABANDONED`` for the
  next waiter to interpret rather than simply releasing.
- Termination is tree-shaped on Windows and group-shaped on POSIX. Every
  Python launch on Windows brings a ``conhost.exe`` with it, so even a
  single spawn is already a small tree there.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path

LINUX = "linux"
WSL = "wsl"
WINDOWS = "windows"
MACOS = "macos"

PROC_VERSION = Path("/proc/version")

_IS_WINDOWS = sys.platform == "win32"

# Windows file locks are mandatory: a locked byte range cannot be read by
# an unrelated process. Locking a byte past any real content keeps a
# process that loses the race able to read the owner metadata in the file.
_LOCK_OFFSET_HIGH = 0x40000000  # byte 2**62

# How long termination waits for a stopping process before forcing it.
TERMINATE_GRACE_S = 10.0


class LockBusy(RuntimeError):
    """Another process holds the lock and the caller asked not to wait."""


def id() -> str:  # noqa: A001 - the seam member is named `id` by design
    """Return the runtime environment identifier for this process."""
    if sys.platform == "win32":
        return WINDOWS
    if sys.platform == "darwin":
        return MACOS
    try:
        if "microsoft" in PROC_VERSION.read_text().lower():
            return WSL
    except OSError:
        pass
    return LINUX


# ---------------------------------------------------------------------------
# Locking
# ---------------------------------------------------------------------------

@contextmanager
def exclusive_lock(path: Path, *, blocking: bool = False) -> Iterator[None]:
    """Hold an exclusive inter-process lock on *path* for the block.

    The lock file is opened in append mode and never truncated, so the
    inode another process may have locked is preserved and any owner
    metadata a caller writes into the file survives.

    Raises :class:`LockBusy` when another process holds the lock and
    *blocking* is false. Locks are not re-entrant on either platform: a
    second acquisition from the same process blocks or raises, exactly
    as one from a different process would.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as lock_file:
        _lock_acquire(lock_file, blocking=blocking)
        try:
            yield
        finally:
            _lock_release(lock_file)


if _IS_WINDOWS:
    import ctypes
    from ctypes import wintypes

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    LOCKFILE_FAIL_IMMEDIATELY = 0x00000001
    LOCKFILE_EXCLUSIVE_LOCK = 0x00000002
    ERROR_LOCK_VIOLATION = 33
    TH32CS_SNAPPROCESS = 0x00000002
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    PROCESS_TERMINATE = 0x0001
    STILL_ACTIVE = 259
    DETACHED_PROCESS = 0x00000008
    CREATE_NEW_PROCESS_GROUP = 0x00000200
    CREATE_NO_WINDOW = 0x08000000

    class _OVERLAPPED(ctypes.Structure):
        _fields_ = [
            ("Internal", ctypes.c_void_p),
            ("InternalHigh", ctypes.c_void_p),
            ("Offset", wintypes.DWORD),
            ("OffsetHigh", wintypes.DWORD),
            ("hEvent", wintypes.HANDLE),
        ]

    class _PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        ]

    _kernel32.LockFileEx.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
        wintypes.DWORD, ctypes.POINTER(_OVERLAPPED)]
    _kernel32.LockFileEx.restype = wintypes.BOOL
    _kernel32.UnlockFileEx.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
        ctypes.POINTER(_OVERLAPPED)]
    _kernel32.UnlockFileEx.restype = wintypes.BOOL
    _kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL,
                                      wintypes.DWORD]
    _kernel32.OpenProcess.restype = wintypes.HANDLE
    _kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE,
                                             ctypes.POINTER(wintypes.DWORD)]
    _kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    _kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    _kernel32.TerminateProcess.restype = wintypes.BOOL
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.CloseHandle.restype = wintypes.BOOL
    _kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD,
                                                   wintypes.DWORD]
    _kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    _kernel32.Process32FirstW.argtypes = [wintypes.HANDLE,
                                          ctypes.POINTER(_PROCESSENTRY32W)]
    _kernel32.Process32FirstW.restype = wintypes.BOOL
    _kernel32.Process32NextW.argtypes = [wintypes.HANDLE,
                                         ctypes.POINTER(_PROCESSENTRY32W)]
    _kernel32.Process32NextW.restype = wintypes.BOOL

    def _overlapped() -> _OVERLAPPED:
        overlapped = _OVERLAPPED()
        overlapped.Offset = 0
        overlapped.OffsetHigh = _LOCK_OFFSET_HIGH
        return overlapped

    def _os_handle(lock_file) -> int:
        import msvcrt  # noqa: PLC0415 - Windows-only
        return msvcrt.get_osfhandle(lock_file.fileno())

    def _lock_acquire(lock_file, *, blocking: bool) -> None:
        flags = LOCKFILE_EXCLUSIVE_LOCK
        if not blocking:
            flags |= LOCKFILE_FAIL_IMMEDIATELY
        ctypes.set_last_error(0)
        acquired = _kernel32.LockFileEx(_os_handle(lock_file), flags, 0, 1, 0,
                                        ctypes.byref(_overlapped()))
        if not acquired:
            error = ctypes.get_last_error()
            if error == ERROR_LOCK_VIOLATION:
                raise LockBusy(str(lock_file.name))
            raise OSError(0, "LockFileEx failed", str(lock_file.name), error)

    def _lock_release(lock_file) -> None:
        _kernel32.UnlockFileEx(_os_handle(lock_file), 0, 1, 0,
                               ctypes.byref(_overlapped()))

    def is_alive(pid: int) -> bool:
        """Return whether *pid* names a process that has not exited."""
        handle = _kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION,
                                       False, pid)
        if not handle:
            return False
        try:
            code = wintypes.DWORD()
            if not _kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == STILL_ACTIVE
        finally:
            _kernel32.CloseHandle(handle)

    def _process_table() -> list[tuple[int, int]]:
        """Every (pid, parent pid) pair currently on the host."""
        snapshot = _kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if snapshot == wintypes.HANDLE(-1).value:
            return []
        try:
            entry = _PROCESSENTRY32W()
            entry.dwSize = ctypes.sizeof(entry)
            pairs: list[tuple[int, int]] = []
            more = _kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
            while more:
                pairs.append((entry.th32ProcessID, entry.th32ParentProcessID))
                more = _kernel32.Process32NextW(snapshot, ctypes.byref(entry))
            return pairs
        finally:
            _kernel32.CloseHandle(snapshot)

    def _descendants(pid: int) -> list[int]:
        """Every descendant of *pid*, nearest first.

        Windows has no process group, so the tree is reconstructed from
        the parent links in a process snapshot.
        """
        children: dict[int, list[int]] = {}
        for child, parent in _process_table():
            children.setdefault(parent, []).append(child)
        found: list[int] = []
        queue = [pid]
        while queue:
            current = queue.pop(0)
            for child in children.get(current, []):
                if child != current and child not in found:
                    found.append(child)
                    queue.append(child)
        return found

    def _terminate_one(pid: int) -> None:
        handle = _kernel32.OpenProcess(PROCESS_TERMINATE, False, pid)
        if not handle:
            return
        try:
            _kernel32.TerminateProcess(handle, 1)
        finally:
            _kernel32.CloseHandle(handle)

    def terminate(pid: int, *, grace_s: float = TERMINATE_GRACE_S) -> None:
        """Terminate *pid* and every process descended from it.

        There is no graceful stage: a console-less Windows process is
        under no obligation to handle any signal, so *grace_s* bounds
        only the wait for the tree to disappear. The tree is enumerated
        before the root is terminated, because terminating the root
        orphans its children and loses the parent links that identify
        them.
        """
        tree = [pid, *_descendants(pid)]
        for target in reversed(tree):
            _terminate_one(target)
        deadline = time.monotonic() + grace_s
        while time.monotonic() < deadline:
            if not any(is_alive(target) for target in tree):
                return
            time.sleep(0.05)

    def _detached_popen_kwargs() -> dict:
        return {"creationflags": (DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
                                  | CREATE_NO_WINDOW)}

else:
    import fcntl
    import signal

    def _lock_acquire(lock_file, *, blocking: bool) -> None:
        flags = fcntl.LOCK_EX if blocking else fcntl.LOCK_EX | fcntl.LOCK_NB
        try:
            fcntl.flock(lock_file.fileno(), flags)
        except BlockingIOError as exc:
            raise LockBusy(str(lock_file.name)) from exc

    def _lock_release(lock_file) -> None:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def is_alive(pid: int) -> bool:
        """Return whether *pid* names a process that has not exited."""
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    def terminate(pid: int, *, grace_s: float = TERMINATE_GRACE_S) -> None:
        """Signal *pid*'s process group to stop, then force it.

        Processes started through :func:`spawn_detached` lead their own
        group, so signalling the group reaches whatever they spawned in
        turn. A process that is not a group leader is signalled directly
        rather than taking its caller's group down with it.
        """
        for sig in (signal.SIGTERM, signal.SIGKILL):
            if not is_alive(pid):
                return
            try:
                if os.getpgid(pid) == pid:
                    os.killpg(pid, sig)
                else:
                    os.kill(pid, sig)
            except (ProcessLookupError, PermissionError):
                return
            if sig is signal.SIGTERM:
                deadline = time.monotonic() + grace_s
                while time.monotonic() < deadline and is_alive(pid):
                    time.sleep(0.05)

    def _detached_popen_kwargs() -> dict:
        return {"start_new_session": True}


# ---------------------------------------------------------------------------
# Spawning
# ---------------------------------------------------------------------------

def spawn_detached(
    argv: Sequence[str],
    *,
    cwd: Path | str | None = None,
    env: Mapping[str, str] | None = None,
    stdin=subprocess.DEVNULL,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
    text: bool = False,
) -> subprocess.Popen:
    """Start *argv* as a process that outlives this one.

    Detachment means two things on both platforms: the child does not
    die with its parent, and it leads its own group or tree, so
    :func:`terminate` can take it down with everything it spawned.
    """
    return subprocess.Popen(
        list(argv),
        cwd=str(cwd) if cwd is not None else None,
        env=dict(env) if env is not None else None,
        stdin=stdin,
        stdout=stdout,
        stderr=stderr,
        text=text,
        **_detached_popen_kwargs(),
    )
