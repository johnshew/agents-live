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

import atexit
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import TypeVar

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


class ExecutableNotFound(RuntimeError):
    """No executable this host can launch answers to that name."""


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


def runtime_name() -> str:
    """The runtime part of an ownership identity.

    ``windows`` on native Windows, the distro name on WSL, and the
    runtime identifier elsewhere. WSL is the case that needs a name of
    its own: a distro's hostname defaults to the Windows computer name,
    so two distros on one machine are indistinguishable by hostname, and
    only the distro name tells a reader which row belongs to which.

    Display only. Whether a runtime owns an agent is decided by the uuid
    part of the identity (``ownership.owns``), so renaming a distro does
    not move its agents.
    """
    runtime = id()
    if runtime == WSL:
        distro = os.environ.get("WSL_DISTRO_NAME", "").strip()
        if distro:
            return distro.lower()
    return runtime


def user_state_base() -> Path:
    """Where this host puts per-user state that is not configuration.

    The XDG spelling on POSIX, the local application-data directory on
    Windows, where a roaming profile would otherwise carry machine-local
    runtime state to another machine. An explicit ``XDG_STATE_HOME``
    still wins on both, which is what lets a test point the whole state
    tree somewhere temporary.
    """
    if _IS_WINDOWS:
        local = os.environ.get("LOCALAPPDATA", "").strip()
        if local:
            return Path(local)
        return Path.home() / "AppData" / "Local"
    return Path.home() / ".local" / "state"


# ---------------------------------------------------------------------------
# Enumeration passes
# ---------------------------------------------------------------------------

# Some host reads answer for the whole machine at once - the process
# table, the folder of registered tasks - and the callers that ask are
# per-agent loops. Asking once per agent is what made the dashboard
# unusable on Windows, where a process-table read costs about two
# seconds and the views ask for it twice per agent, three times over.
#
# The scope is declared, not timed. A cache with a lifetime would also
# answer a read taken right after an action from before it, which is
# exactly the case where a stale answer is a wrong answer.
_T = TypeVar("_T")

_PASS: ContextVar[dict[str, object] | None] = ContextVar(
    "agents_live_enumeration_pass", default=None)


@contextmanager
def enumeration_pass() -> Iterator[None]:
    """Take each host-wide read once for everything inside this block.

    Re-entrant: an inner pass joins the outer one instead of starting a
    second, so a caller can declare a pass without knowing whether one
    of its callers already did.
    """
    if _PASS.get() is not None:
        yield
        return
    token = _PASS.set({})
    try:
        yield
    finally:
        _PASS.reset(token)


def pass_cached(key: str, read: Callable[[], _T]) -> _T:
    """What *read* answers, taken once per enclosing enumeration pass.

    Outside a pass every call reads the host, so nothing is ever
    answered from a snapshot the caller did not ask for.
    """
    cache = _PASS.get()
    if cache is None:
        return read()
    if key not in cache:
        cache[key] = read()
    return cache[key]  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Scheduling
# ---------------------------------------------------------------------------

# Which of the host's own schedulers persists a trigger. Named for the
# mechanism rather than the platform, because that is what the answer is
# used for: `schedules` maps it to an implementation, and nothing else
# asks the question at all.
CRONTAB = "crontab"
TASK_SCHEDULER = "task-scheduler"


def native_scheduler() -> str:
    """The scheduler this host dispatches with."""
    return TASK_SCHEDULER if _IS_WINDOWS else CRONTAB


# ---------------------------------------------------------------------------
# Environment, PATH, and executables
# ---------------------------------------------------------------------------

# Variables a Windows child needs in an explicitly built environment.
# `SystemRoot` is not optional: a native CLI launched without it dies
# with STATUS_STACK_BUFFER_OVERRUN (0xC0000409) inside the loader,
# before it runs a line of its own code, and reports nothing on either
# stream. The rest are what a program written for Windows expects to be
# able to read - temp directories, the profile, and the processor and
# shell facts anything it spawns will ask for.
_WINDOWS_ENV_PASSTHROUGH = (
    "SystemRoot", "SystemDrive", "windir", "COMSPEC", "PATHEXT",
    "TEMP", "TMP", "USERPROFILE", "USERNAME", "HOMEDRIVE", "HOMEPATH",
    "APPDATA", "LOCALAPPDATA", "ProgramData", "ProgramFiles",
    "ProgramFiles(x86)", "ProgramW6432", "PUBLIC",
    "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE", "OS",
)

# Suffixes a pinned executable may not have on Windows. A `.ps1` is not
# executable at all: `CreateProcess` cannot launch one, and the ones
# that shadow agent CLIs on an interactive PATH are installer
# bootstrappers rather than the CLI. A `.bat` or `.cmd` is worse than
# useless: Windows runs it through `cmd.exe`, which re-parses the
# argument string, so a prompt body carrying `&` or `|` would run as a
# command. Both fail closed rather than launching something whose
# behavior is not the CLI's.
_WINDOWS_REFUSED_SUFFIXES = {
    ".ps1": "a PowerShell script, which Windows cannot launch directly",
    ".psm1": "a PowerShell module, which Windows cannot launch directly",
    ".bat": "a batch shim, which re-parses arguments through cmd.exe",
    ".cmd": "a batch shim, which re-parses arguments through cmd.exe",
}


def base_env() -> dict[str, str]:
    """The host variables an agent CLI needs, without the caller's.

    Agent invocations build their environment rather than inheriting
    one, so a run started from a shell and a run started from a
    scheduler see the same thing. This is the platform floor such an
    environment stands on; callers add PATH and the agent's own
    variables on top.
    """
    if _IS_WINDOWS:
        env = {key: os.environ[key] for key in _WINDOWS_ENV_PASSTHROUGH
               if key in os.environ}
        env.setdefault("HOME", os.environ.get("USERPROFILE", str(Path.home())))
        return env
    return {"HOME": os.environ.get("HOME", str(Path.home()))}


def inherits_path() -> bool:
    """Whether a constructed PATH should keep this process's PATH.

    POSIX says no: cron hands an agent a near-empty environment, so the
    PATH a run gets is built from known install locations and nothing
    else. Windows says yes: Task Scheduler runs a task with the owning
    user's environment block, so the inherited PATH is the same one an
    interactive run sees, and dropping it would lose every per-user
    install location (winget shims, npm's global bin, nvm4w) that a
    Windows CLI is actually installed into.
    """
    return _IS_WINDOWS


def system_path_dirs() -> list[str]:
    """Directories that belong on any constructed PATH for this host."""
    if _IS_WINDOWS:
        root = os.environ.get("SystemRoot", r"C:\Windows")
        system32 = Path(root) / "System32"
        return [str(system32), root, str(system32 / "Wbem"),
                str(system32 / "WindowsPowerShell" / "v1.0")]
    return ["/usr/local/bin", "/usr/bin", "/bin"]


def supports_pty() -> bool:
    """Whether an agent CLI here has to be driven through a terminal.

    The Linux Copilot CLI needs `script -qc` to produce output at all.
    The Windows build writes to plain pipes in every launch context,
    including a detached process with no console, so there is nothing
    for a pseudo-terminal to fix and no ConPTY dependency to take on.
    """
    return not _IS_WINDOWS


def find_tool(name: str) -> str | None:
    """Locate *name* where this host installs tools a PATH may not reach.

    The complement of :func:`inherits_path`. A POSIX process launched by
    cron gets a PATH of almost nothing, so the well-known install
    locations are searched directly, including the versioned layout nvm
    gives node. Windows inherits the launching PATH, so `shutil.which`
    has already looked everywhere this would, and there is nothing left
    to find.
    """
    if _IS_WINDOWS:
        return None
    home = Path.home()
    candidates = [home / ".local" / "bin" / name,
                  home / ".cargo" / "bin" / name,
                  Path("/usr/local/bin") / name]
    if name in ("node", "npx"):
        candidates.extend(
            sorted((home / ".nvm" / "versions" / "node").glob(f"*/bin/{name}"),
                   reverse=True))
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


# Where a WSL runtime sees the other side's drives. A program found under
# it is a Windows build, reached through interop.
_INTEROP_PREFIX = "/mnt/"


def tool_is_native(name: str) -> bool:
    """Whether *name* resolves to a program belonging to this runtime.

    Only WSL can answer no. It sees the Windows side's programs on its
    own PATH, and a Windows build keeps its state on the Windows side,
    where the Linux processes that need it cannot reach it - a Windows
    ``node`` writes MSAL tokens to the Windows credential store, so a
    login performed there never populates the Linux cache an MCP reads.

    A PATH lookup that lands outside the interop mount counts, and so
    does an install this runtime keeps somewhere PATH may not reach
    (:func:`find_tool`), which is what makes the answer the same for a
    login shell and for cron.
    """
    if id() != WSL:
        return True
    found = shutil.which(name)
    if found is not None and not found.startswith(_INTEROP_PREFIX):
        return True
    return find_tool(name) is not None


def shell_interpreter() -> list[str] | None:
    """The argv prefix that runs a shell script, or None if there is none.

    POSIX has one by definition. Windows does not: `bash` there is an
    optional Git for Windows install that a scheduled task has no reason
    to see, and a handler that runs only where someone happened to
    install one is worse than a handler that refuses to start.
    """
    if _IS_WINDOWS:
        return None
    return ["bash"]


def use_utf8_io() -> None:
    """Make this process and its children speak UTF-8 on every stream.

    A Windows console defaults to a legacy code page, and Python's
    standard streams follow it, so printing a box-drawing character or
    an em dash out of a log line raises UnicodeEncodeError and takes the
    command down. Three things are needed to fix that: reconfiguring
    this process's streams, exporting `PYTHONUTF8` so the interpreters
    this process launches start the same way (the CLI reaches its own
    subcommands as subprocesses), and switching the console itself to
    UTF-8 so the bytes are rendered rather than mangled. The console
    code page belongs to the console, not to this process, so it is
    restored on the way out.
    """
    os.environ["PYTHONUTF8"] = "1"
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):  # a stream that cannot be re-encoded
            pass
    if _IS_WINDOWS:
        _use_utf8_console()


# The decoding half of `use_utf8_io`: how a captured child's bytes become
# text. `text=True` alone asks Python for the locale encoding, which is
# UTF-8 on POSIX and the ANSI code page on Windows, so the same call
# reads correctly on one host and mojibake on the other (#241). Every
# child this tool launches is one it also configures - its own
# subcommands, `uv`, `node`, `git` - and they all write UTF-8. A child
# that does not is not covered by this and states its own encoding:
# `schtasks` writes the console code page and `wintasks` decodes it as
# `oem`. Errors are replaced because a foreign byte is a bad log line,
# not a reason to fail the operation that read it.
CHILD_TEXT = {"text": True, "encoding": "utf-8", "errors": "replace"}


def split_command_line(text: str) -> list[str]:
    """A process's command line as the argument list it was built from.

    The inverse of what `process_command_lines` reports, and it lives
    here for that reason: on Windows the arguments were joined by
    quoting rules rather than by spaces, and a repo root routinely has a
    space in it, so only the parser that wrote the line can read it
    back. POSIX reports argv already separated.
    """
    if not _IS_WINDOWS:
        return text.split()
    from . import wintasks  # noqa: PLC0415 - Windows leaf

    try:
        return wintasks.parse_command_line(text)
    except wintasks.TaskError:
        return text.split()


def executable_filename(name: str) -> str:
    """The file name an installed console entry point has on this host.

    A packaged entry point is a script on POSIX and a launcher executable
    on Windows. Anything that has to find one by path, rather than by
    PATH lookup, needs the difference spelled out.
    """
    return f"{name}.exe" if _IS_WINDOWS else name


def executable_dir(prefix: Path | str | None = None) -> Path:
    """Where an environment keeps the entry points installed into it.

    ``Scripts`` on Windows and ``bin`` elsewhere. *prefix* defaults to
    the environment this process is running from.
    """
    base = Path(sys.prefix if prefix is None else prefix)
    return base / ("Scripts" if _IS_WINDOWS else "bin")


def interpreter_name() -> str:
    """The name that reaches a Python interpreter on this host.

    ``python3`` is the POSIX spelling and does not exist on Windows,
    where asking for it reaches the Microsoft Store alias instead of an
    interpreter.
    """
    return "python" if _IS_WINDOWS else "python3"


def locks_running_image() -> bool:
    """Whether this host refuses to overwrite a running executable.

    Windows holds a mandatory lock on the file backing a running image,
    so replacing one means moving it out of the way first. POSIX
    replaces the directory entry and leaves the running process on the
    old inode, so nothing has to move.
    """
    return _IS_WINDOWS


def pin_executable(name: str, *, path: str | None = None) -> str:
    """The argv[0] that launches *name* on this host.

    POSIX returns the name unchanged: `execvp` searches the child's own
    PATH, so handing the child a constructed PATH already pins what runs.
    Windows resolves an executable name against the PATH of the process
    doing the launching, not the environment handed to the child, so the
    name alone pins nothing there and the absolute path is resolved up
    front instead.

    The Windows search walks PATH and PATHEXT in the order Windows
    itself would, skipping the shims in ``_WINDOWS_REFUSED_SUFFIXES``
    rather than stopping at the first one: the mainstream Copilot layout
    puts VS Code's shims ahead of the installed executable, so refusing
    the first answer would refuse a host that has the CLI (#238). Unlike
    `shutil.which` it never searches the current directory, which is not
    a place a launched executable should come from.

    Raises :class:`ExecutableNotFound` when nothing on *path* answers to
    the name, or when every answer is a refused shim.
    """
    if not _IS_WINDOWS:
        return name
    search_path = os.environ.get("PATH", "") if path is None else path
    path_ext = os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD").split(os.pathsep)
    extensions = ([""] if Path(name).suffix else
                  [suffix for suffix in path_ext if suffix])
    directories = ([""] if os.path.dirname(name) else
                   [directory for directory in search_path.split(os.pathsep)
                    if directory])
    refused: list[tuple[str, str]] = []
    seen: set[str] = set()
    for directory in directories:
        for extension in extensions:
            candidate = os.path.join(directory, name + extension)
            normalized = os.path.normcase(os.path.abspath(candidate))
            if normalized in seen or not os.path.isfile(candidate):
                continue
            seen.add(normalized)
            resolved = str(Path(candidate).resolve())
            reason = _WINDOWS_REFUSED_SUFFIXES.get(
                Path(resolved).suffix.lower())
            if reason is not None:
                refused.append((resolved, reason))
                continue
            return resolved
    if refused:
        resolved, reason = refused[0]
        raise ExecutableNotFound(
            f"only shims answer to '{name}' on this host's PATH; "
            f"{resolved} is {reason}; install the CLI itself, or point "
            f"the runtime at its executable")
    raise ExecutableNotFound(
        f"no executable named '{name}' on this host's PATH")


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
    PROCESS_COMMAND_LINE_INFORMATION = 60  # ProcessCommandLineInformation
    STATUS_INFO_LENGTH_MISMATCH = 0xC0000004
    STILL_ACTIVE = 259
    CREATE_NEW_PROCESS_GROUP = 0x00000200
    CREATE_NO_WINDOW = 0x08000000
    CP_UTF8 = 65001

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

    class _UNICODE_STRING(ctypes.Structure):
        _fields_ = [
            ("Length", wintypes.USHORT),
            ("MaximumLength", wintypes.USHORT),
            ("Buffer", ctypes.c_void_p),
        ]

    def _bind_ntdll():
        """Bind ``NtQueryInformationProcess``, or ``None`` if unavailable.

        This is the one undocumented interface in the module, so it is
        bound defensively and every caller has a supported fallback.
        """
        try:
            ntdll = ctypes.WinDLL("ntdll", use_last_error=True)
            query = ntdll.NtQueryInformationProcess
        except (OSError, AttributeError):  # pragma: no cover - always present
            return None
        query.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                          wintypes.ULONG, ctypes.POINTER(wintypes.ULONG)]
        query.restype = ctypes.c_long
        return query

    _nt_query_process = _bind_ntdll()

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
    _kernel32.GetConsoleOutputCP.argtypes = []
    _kernel32.GetConsoleOutputCP.restype = wintypes.UINT
    _kernel32.SetConsoleOutputCP.argtypes = [wintypes.UINT]
    _kernel32.SetConsoleOutputCP.restype = wintypes.BOOL

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

    def _command_line(pid: int) -> str | None:
        """*pid*'s command line, or ``None`` if it cannot be read.

        ``PROCESS_QUERY_LIMITED_INFORMATION`` needs no elevation and is
        simply refused for processes owned by someone else, which is the
        same answer CIM gives, so a refusal here is ordinary rather than
        an error.
        """
        if _nt_query_process is None:
            return None
        handle = _kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION,
                                       False, pid)
        if not handle:
            return None
        try:
            needed = wintypes.ULONG(0)
            # Sizing call: the expected answer is the length mismatch.
            status = _nt_query_process(handle, PROCESS_COMMAND_LINE_INFORMATION,
                                       None, 0, ctypes.byref(needed))
            if status & 0xFFFFFFFF != STATUS_INFO_LENGTH_MISMATCH:
                return None
            if not needed.value:
                return None
            buffer = ctypes.create_string_buffer(needed.value)
            status = _nt_query_process(handle, PROCESS_COMMAND_LINE_INFORMATION,
                                       buffer, needed.value,
                                       ctypes.byref(needed))
            if status:  # anything but STATUS_SUCCESS
                return None
            text = ctypes.cast(buffer,
                               ctypes.POINTER(_UNICODE_STRING)).contents
            if not text.Buffer or not text.Length:
                return None
            return ctypes.wstring_at(text.Buffer, text.Length // 2)
        except OSError:  # pragma: no cover - the process died mid-read
            return None
        finally:
            _kernel32.CloseHandle(handle)

    def _command_lines_in_process() -> list[tuple[int, str]]:
        """Every readable process as ``(pid, command line)``, read directly.

        A process snapshot supplies the pids; ``ntdll`` supplies the
        arguments, which is what a snapshot omits and what says which
        agent a watcher belongs to. Empty means the mechanism did not
        work at all - this process is always readable by itself - so the
        caller can tell "nothing to report" apart from "ask another way".
        """
        found: list[tuple[int, str]] = []
        for pid, _parent in _process_table():
            text = _command_line(pid)
            if text:
                found.append((pid, text))
        return found

    def _command_lines_via_cim() -> list[tuple[int, str]]:
        """Every visible process as ``(pid, command line)``, via CIM.

        The supported fallback for hosts where the direct read is
        unavailable: ``ProcessCommandLineInformation`` is Windows 8.1 and
        later, and ``ntdll`` is not a contract.
        """
        shell = shutil.which("powershell.exe") or shutil.which("pwsh.exe")
        if shell is None:
            return []
        script = ("Get-CimInstance Win32_Process | "
                  "ForEach-Object { '{0} {1}' -f $_.ProcessId, $_.CommandLine }")
        try:
            completed = subprocess.run(
                [shell, "-NoProfile", "-NonInteractive", "-Command", script],
                capture_output=True, **CHILD_TEXT, timeout=30,
                creationflags=CREATE_NO_WINDOW)
        except (OSError, subprocess.TimeoutExpired):
            return []
        if completed.returncode != 0:
            return []
        found: list[tuple[int, str]] = []
        for line in completed.stdout.splitlines():
            pid_text, _, command = line.strip().partition(" ")
            if pid_text.isdigit() and command:
                found.append((int(pid_text), command))
        return found

    def process_command_lines() -> list[tuple[int, str]]:
        """Every visible process as ``(pid, command line)``.

        A process snapshot carries the executable name but not the
        arguments, and the arguments are what say which agent a watcher
        belongs to. Reading them directly costs milliseconds where
        starting PowerShell to ask CIM costs seconds, and this read is
        behind every question about whether a watcher is running -
        ``status``, the dashboard, the health loop, ``stop``, the orphan
        sweep, and an upgrade naming what it left behind. CIM stands as
        the fallback; when neither can be reached the answer is "nothing
        found", which reads as "no watcher is running" - visibly wrong
        rather than silently stopping the wrong process.
        """
        return _command_lines_in_process() or _command_lines_via_cim()

    def _detached_popen_kwargs() -> dict:
        """Flags for a child that outlives us and stays out of sight.

        ``CREATE_NO_WINDOW`` is deliberately not paired with
        ``DETACHED_PROCESS``: Windows ignores it whenever
        ``DETACHED_PROCESS`` or ``CREATE_NEW_CONSOLE`` is also set.
        Measured with both flags, the child came up owning a fresh
        console with a real window handle, which the default console host
        draws on the desktop - a window flashing open and shut on every
        spawn. ``CREATE_NO_WINDOW`` alone still gives the child its own
        console, so descendants that want one inherit it rather than
        allocating another, but its window handle is zero and nothing is
        ever drawn. ``CREATE_NEW_PROCESS_GROUP`` still keeps our
        console's Ctrl+C away from it.
        """
        return {"creationflags": CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW}

    def _use_utf8_console() -> None:
        """Put the attached console into UTF-8 for the life of this run."""
        current = _kernel32.GetConsoleOutputCP()
        if current in (0, CP_UTF8):  # 0: no console attached
            return
        if not _kernel32.SetConsoleOutputCP(CP_UTF8):
            return
        atexit.register(_kernel32.SetConsoleOutputCP, current)

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

    def process_command_lines() -> list[tuple[int, str]]:
        """Every visible process as ``(pid, command line)``."""
        try:
            completed = subprocess.run(
                ["ps", "-eo", "pid=,args="], capture_output=True, **CHILD_TEXT,
                check=True)
        except (OSError, subprocess.CalledProcessError):
            return []
        found: list[tuple[int, str]] = []
        for line in completed.stdout.splitlines():
            pid_text, _, command = line.strip().partition(" ")
            if pid_text.isdigit() and command:
                found.append((int(pid_text), command))
        return found

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

    ``text`` decodes the child's streams through :data:`CHILD_TEXT`
    rather than the locale, for the same reason every other capture in
    the tool does.
    """
    return subprocess.Popen(
        list(argv),
        cwd=str(cwd) if cwd is not None else None,
        env=dict(env) if env is not None else None,
        stdin=stdin,
        stdout=stdout,
        stderr=stderr,
        **(CHILD_TEXT if text else {}),
        **_detached_popen_kwargs(),
    )


def defer_until_environment_exits(
        argv: Sequence[str], environment: Path | str) -> bool:
    """Start *argv* after no process executes from *environment*.

    Windows will not remove an executable while it is running. The helper
    itself must therefore live outside the environment being removed. Other
    hosts do not need this handoff and return ``False``.
    """
    if not _IS_WINDOWS:
        return False
    powershell = (shutil.which("powershell.exe")
                  or shutil.which("pwsh.exe"))
    if powershell is None:
        return False
    escaped_environment = str(environment).replace("'", "''")
    command = subprocess.list2cmdline(list(argv)).replace("'", "''")
    script = (
        f"$root = '{escaped_environment}'; "
        "do { "
        "$running = @(Get-Process -ErrorAction SilentlyContinue | "
        "Where-Object { try { $_.Path -and "
        "$_.Path.StartsWith($root, "
        "[System.StringComparison]::OrdinalIgnoreCase) } "
        "catch { $false } }); "
        "if ($running.Count) { Start-Sleep -Milliseconds 100 } "
        "} while ($running.Count); "
        f"& ([scriptblock]::Create('{command}')); "
        "exit $LASTEXITCODE"
    )
    try:
        spawn_detached(
            [powershell, "-NoProfile", "-NonInteractive", "-Command", script],
            stdin=subprocess.DEVNULL, stdout=None, stderr=None)
    except OSError:
        return False
    return True
