"""Isolated runtime fixtures and a guard against native test side effects."""
from __future__ import annotations

import atexit
import contextlib
import os
import re
import shutil
import sys
import tempfile
import threading
from pathlib import Path
from unittest import mock

from agents_live import deploy, paths, runtime
from agents_live.runtime.hosts import system
from agents_live.runtime.hosts.memory import MemoryHost

_lock = threading.RLock()
_guards = 0
_allowed = threading.local()

# Host-scoped state (the admin log, host logs, health beacon) resolves from the
# real user environment unless a test overrides it. Capture the real location
# once, then redirect the whole test process, and every child it starts, to a
# session-scoped temporary state home so no test can append to live history.
_REAL_STATE_ENV = "AGENTS_LIVE_TEST_REAL_STATE_HOME"
if _REAL_STATE_ENV not in os.environ:
    os.environ[_REAL_STATE_ENV] = str(paths.state_home())
    _session_root = tempfile.mkdtemp(prefix="agents-live-test-host-")
    atexit.register(shutil.rmtree, _session_root, True)
    os.environ["XDG_STATE_HOME"] = str(Path(_session_root) / "state")
REAL_STATE_HOME = Path(os.environ[_REAL_STATE_ENV])
_REAL_PREFIX = os.path.normcase(os.path.abspath(REAL_STATE_HOME))
_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_TRUNC
_TESTS = os.path.normcase(os.path.dirname(os.path.abspath(__file__)))
host_state_violations: list[str] = []


def _real_host_state(target) -> bool:
    if not isinstance(target, (str, bytes, os.PathLike)):
        return False
    candidate = os.path.normcase(os.path.abspath(os.fsdecode(target)))
    return candidate == _REAL_PREFIX or candidate.startswith(_REAL_PREFIX + os.sep)


def _caller() -> str:
    frame = sys._getframe(2)
    while frame is not None:
        filename = frame.f_code.co_filename
        if os.path.normcase(os.path.dirname(os.path.abspath(filename))) == _TESTS \
                and not filename.endswith("host_safety.py"):
            return f"{os.path.basename(filename)}:{frame.f_lineno} {frame.f_code.co_name}"
        frame = frame.f_back
    return "unknown caller"


def _guard_host_state(event: str, arguments: tuple) -> None:
    if event == "open":
        target, mode, flags = arguments
        writes = bool(mode and set(str(mode)) & set("wax+")) or bool(
            isinstance(flags, int) and flags & _WRITE_FLAGS)
    elif event == "os.rename":
        target, writes = arguments[1], True
    else:
        return
    if writes and _real_host_state(target):
        host_state_violations.append(f"{event} {os.fsdecode(target)} from {_caller()}")
        raise PermissionError(f"test wrote real host state blocked: {os.fsdecode(target)}")


def assert_real_host_state_untouched() -> None:
    """Module cleanup: fail when any test tried to write the real host state."""
    if host_state_violations:
        found = list(host_state_violations)
        host_state_violations.clear()
        raise AssertionError(
            "tests attempted to write the real host state home; isolate "
            "XDG_STATE_HOME:\n" + "\n".join(found))


def _audit(event: str, arguments: tuple) -> None:
    _guard_host_state(event, arguments)
    if event != "subprocess.Popen" or not _guards or getattr(_allowed, "depth", 0):
        return
    executable, argv, _cwd, _env = arguments
    command = " ".join(str(item) for item in argv) if not isinstance(argv, str) else argv
    parts = system.split_command_line(argv) if isinstance(argv, str) else argv
    executable = executable or (parts[0] if parts else "")
    name = Path(str(executable).strip('"')).name.lower().removesuffix(".exe")
    tokens = command.lower()
    scheduler = (
        name == "crontab" and not re.search(r"(?:^|\s)-l(?:\s|$)", tokens)
        or name == "schtasks" and not re.search(r"(?:^|\s)/query(?:\s|$)", tokens)
        or name in {"powershell", "pwsh", "cmd", "sh", "bash"}
        and re.search(r"schtasks|crontab|(?:register|unregister|set|start|stop|enable|disable)-scheduledtask", tokens)
    )
    watcher = (
        bool({"watch-loop", "watch-supervise", "watch-observe"}.intersection(parts))
        and ("agents-live" in tokens or "agents_live" in tokens)
        or "agents_live.runtime.watcher" in parts
    )
    if scheduler or watcher:
        raise RuntimeError(
            "native scheduler mutation or watcher launch blocked by test guard; "
            "use isolated_host or explicitly scoped allow_native_runtime")


sys.addaudithook(_audit)


@contextlib.contextmanager
def native_guard():
    global _guards
    with _lock:
        _guards += 1
    try:
        yield
    finally:
        with _lock:
            _guards -= 1


@contextlib.contextmanager
def allow_native_runtime():
    previous = getattr(_allowed, "depth", 0)
    _allowed.depth = previous + 1
    try:
        yield
    finally:
        _allowed.depth = previous


@contextlib.contextmanager
def isolated_host(root: Path | None = None):
    context = contextlib.nullcontext(str(root)) if root is not None else (
        tempfile.TemporaryDirectory(prefix="agents-live-test-"))
    with context as temporary:
        root = Path(temporary).resolve()
        host = MemoryHost()
        previous = runtime.current()
        cached = paths._cached_default_root, paths._cached_default_source
        with mock.patch.dict(os.environ, {
            "AGENTS_LIVE_REPO": str(root),
            "XDG_CONFIG_HOME": str(root / "config"),
            "XDG_STATE_HOME": str(root / "state"),
            "XDG_DATA_HOME": str(root / "data"),
            deploy.layout.ENV_INSTALL_ROOT: str(root / "installation"),
        }), native_guard():
            runtime.configure(host)
            paths.clear_cache()
            try:
                yield root, host
            finally:
                runtime.configure(previous)
                paths._cached_default_root, paths._cached_default_source = cached