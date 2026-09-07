"""Isolated runtime fixtures and a guard against native test side effects."""
from __future__ import annotations

import contextlib
import os
import re
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


def _audit(event: str, arguments: tuple) -> None:
    if event != "subprocess.Popen" or not _guards or getattr(_allowed, "depth", 0):
        return
    executable, argv, _cwd, _env = arguments
    command = " ".join(str(item) for item in argv) if not isinstance(argv, str) else argv
    parts = system.split_command_line(argv) if isinstance(argv, str) else argv
    executable = executable or (parts[0] if parts else "")
    name = Path(str(executable)).name.lower().removesuffix(".exe")
    tokens = command.lower()
    scheduler = (
        name == "crontab" and not re.search(r"(?:^|\s)-l(?:\s|$)", tokens)
        or name == "schtasks" and not re.search(r"(?:^|\s)/query(?:\s|$)", tokens)
        or name in {"powershell", "pwsh", "cmd", "sh", "bash"}
        and re.search(r"schtasks|crontab|(?:register|unregister|set|start|stop|enable|disable)-scheduledtask", tokens)
    )
    watcher = "watch-loop" in parts and (
        "agents-live" in tokens or "agents_live" in tokens)
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