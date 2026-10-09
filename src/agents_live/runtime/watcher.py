"""Detached watcher exit observation and actual change-loop progress."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

from .. import paths
from ..obs import events
from . import artifacts
from .hosts import system

STDERR_LIMIT = 8192
POLL_SECONDS = 15.0
PROGRESS_SECONDS = 45.0
STARTUP_SECONDS = 30.0
OBSERVER_STARTUP_SECONDS = 5.0
SESSION_ENV = "AGENTS_LIVE_WATCH_SESSION"


def _home() -> Path:
    return paths.host_logs_dir().parent / "watchers"


def _read(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _write(path: Path, value: dict) -> None:
    paths.atomic_write_text(path, json.dumps(value) + "\n", mode=0o600)


def prepare(argv, *, cwd=None, key="") -> Path:
    metadata = artifacts.from_argv(argv)
    repository = (
        metadata.scope.removeprefix("repo:")
        if metadata is not None and metadata.scope.startswith("repo:")
        else next((str(second) for first, second in zip(argv, argv[1:])
                   if first == "--repo"), str(cwd or ""))
    )
    agent = metadata.target.removeprefix("agent:") if metadata else "watcher"
    session = _home() / uuid.uuid4().hex
    _write(session / "request.json", {
        "argv": list(argv), "cwd": str(cwd) if cwd else None,
        "repository": repository, "agent": agent,
        "key": key or (metadata.id if metadata else ""), "run_id": session.name,
    })
    return session


def spawn(argv, *, cwd=None, key="", stdout=subprocess.DEVNULL):
    """Return the real watcher pid, not its independently detached observer."""
    session = prepare(argv, cwd=cwd, key=key)
    try:
        route = 3 if list(argv[1:2]) == ["--repo"] else 1
        observer_argv = (
            [*argv[:route], "internal", "watch-observe", str(session)]
            if list(argv[route:route + 2]) == ["internal", "watch-loop"] else
            [argv[0], "-m", "agents_live.runtime.watcher", str(session)])
        observer = system.spawn_detached(
            observer_argv,
            cwd=cwd, stdout=stdout,
            breakaway=bool(os.environ.get(SESSION_ENV)),
        )
    except BaseException:
        shutil.rmtree(session)
        raise
    deadline = time.monotonic() + OBSERVER_STARTUP_SECONDS
    while time.monotonic() < deadline:
        ready = _read(session / "child.json")
        if ready:
            return SimpleNamespace(pid=ready["pid"], created_at=ready["created_at"])
        if observer.poll() is not None:
            raise RuntimeError(_read(session / "exit.json").get(
                "stderr_tail", "watcher observer exited before launching its child"))
        time.sleep(0.02)
    # Tell a slow observer not to create an unowned late watcher.
    _write(session / "stop.json", {
        "stop_reason": "startup_timeout", "operation": "watcher-start"})
    raise RuntimeError("watcher observer startup timed out")


def _event(request: dict, status: str, **fields) -> None:
    repository = request.get("repository", "")
    directory = (paths.repo_state_dir(Path(repository)) / "logs"
                 if repository else paths.host_logs_dir())
    code = fields.pop("exit_code", None)
    unexpected = status == "exited" and fields.get("stop_reason") in {
        "unexpected_exit", "observer_error", "error", "watch_failed", "startup_timeout"}
    fields["level"] = "error" if unexpected else "info"
    agent = request.get("agent", "watcher")
    filename = agent if agent and not any(char in agent for char in "/\\") else "watchers"
    events.record(directory / f"{filename}.jsonl", events.create(
        "watcher", status, repository=repository, agent=agent,
        run_id=request.get("run_id", ""), origin="watch", exit_code=code,
        category="watcher_exit" if unexpected else None,
        message=f"watcher {status}: {fields.get('stop_reason', '')}",
        attributes=tuple(fields.items()),
    ))


def supervise(session: Path) -> int:
    """Drain stderr while waiting for the child, even after its launcher exits."""
    request = _read(session / "request.json")
    child = None
    tail = bytearray()
    byte_count = 0
    mutex = threading.Lock()
    reader = None
    cleanup = None
    capture_error = ""
    code = -1
    try:
        if _read(session / "stop.json"):
            raise RuntimeError("watcher startup was cancelled")
        child = system.spawn_detached(
            request["argv"], cwd=request.get("cwd"),
            env={**os.environ, SESSION_ENV: str(session)},
            stdout=None, stderr=subprocess.PIPE,
            suspended=True,
        )
        created = system.process_start_time(child.pid) or time.time()
        identity = {"pid": child.pid, "created_at": created, "session": str(session),
                    "started": time.monotonic(), "key": request.get("key", ""),
                    "birth": system.process_start_token(child.pid)}
        _write(_home() / "pids" / f"{child.pid}.json", identity)
        cleanup = system.supervise_child(child, allow_breakaway=True)
        _write(session / "child.json", identity)

        def drain():
            nonlocal byte_count, capture_error
            try:
                while chunk := child.stderr.read1(65536):
                    with mutex:
                        byte_count += len(chunk)
                        tail.extend(chunk)
                        del tail[:-STDERR_LIMIT]
            except (OSError, ValueError) as exc:
                capture_error = str(exc)

        reader = threading.Thread(target=drain, daemon=True)
        reader.start()
        last_snapshot = None
        while child.poll() is None:
            with mutex:
                snapshot = bytes(tail)
            if snapshot != last_snapshot:
                paths.atomic_write_text(
                    session / "stderr.txt", snapshot.decode("utf-8", errors="replace"),
                    mode=0o600)
                last_snapshot = snapshot
            stop = _read(session / "stop.json")
            if stop.get("stop_reason") == "startup_timeout":
                system.terminate(child.pid)
            time.sleep(0.2)
        code = child.wait()
    except Exception as exc:
        capture_error = str(exc)
        if child is not None and child.poll() is None:
            system.terminate(child.pid)
            code = child.wait(timeout=10)
        else:
            code = -1
    finally:
        if cleanup is not None:
            cleanup()
        if reader is not None:
            reader.join(timeout=1)
        with mutex:
            stderr_tail = bytes(tail).decode("utf-8", errors="replace")
        stop = _read(session / "stop.json") or _read(session / "result.json")
        request = _read(session / "request.json") or request
        fields = {
            "watcher_pid": child.pid if child is not None else None,
            "subscription_id": request.get("key", ""),
            "exit_code": code, "stderr_tail": stderr_tail,
            "stderr_bytes": byte_count, "stderr_truncated": byte_count > STDERR_LIMIT,
            "stop_reason": stop.get("stop_reason", "observer_error" if capture_error else "unexpected_exit"),
            "operation": stop.get("operation", "watcher"),
            "capture_error": capture_error,
            "stderr_complete": not capture_error and (reader is None or not reader.is_alive()),
        }
        _write(session / "exit.json", fields)
        _event(request, "exited", **fields)
        if child is not None:
            for pid in {child.pid, _read(session / "loop.json").get("pid")} - {None}:
                index = _home() / "pids" / f"{pid}.json"
                if _read(index).get("session") == str(session):
                    index.unlink(missing_ok=True)
        if reader is not None and not reader.is_alive() and child.stderr is not None:
            child.stderr.close()
    return code


def identify(agent: str) -> None:
    value = os.environ.get(SESSION_ENV)
    if value:
        request = Path(value) / "request.json"
        _write(request, {**_read(request), "agent": agent})


def progress(phase: str) -> None:
    """Only the executing loop calls this; an independent timer would lie."""
    value = os.environ.get(SESSION_ENV)
    if value:
        session = Path(value)
        if not (session / "loop.json").exists():
            deadline = time.monotonic() + OBSERVER_STARTUP_SECONDS
            child = _read(session / "child.json")
            while not child and time.monotonic() < deadline:
                time.sleep(0.02)
                child = _read(session / "child.json")
            if not child:
                raise RuntimeError("watcher observer did not publish its child identity")
            identity = {
                **child, "pid": os.getpid(),
                "created_at": system.process_start_time(os.getpid()) or time.time(),
                "birth": system.process_start_token(os.getpid()),
                "session": str(session),
            }
            _write(session / "loop.json", identity)
            _write(_home() / "pids" / f"{os.getpid()}.json", identity)
        _write(session / "progress.json", {
            "pid": os.getpid(), "phase": phase, "at": time.monotonic()})


def result(reason: str) -> None:
    value = os.environ.get(SESSION_ENV)
    if value:
        _write(Path(value) / "result.json", {
            "stop_reason": reason, "operation": "watch-loop"})


def _session(ref) -> tuple[Path | None, dict]:
    identity = _read(_home() / "pids" / f"{ref.pid}.json")
    if (
        not identity or identity.get("key") != ref.key
        or not isinstance(identity.get("created_at"), (int, float))
        or not isinstance(identity.get("started"), (int, float))
        or not isinstance(identity.get("session"), str)
        or abs(identity.get("created_at", 0) - ref.created_at) > 2
        or identity.get("birth") is not None
        and identity["birth"] != system.process_start_token(ref.pid)
    ):
        return None, {}
    session = Path(identity.get("session", ""))
    if session.parent != _home():
        return None, {}
    return session, identity


def health(ref) -> str:
    session, identity = _session(ref)
    if session is None:
        return "unverified"
    pulse = _read(session / "progress.json")
    now = time.monotonic()
    if not pulse:
        return "starting" if 0 <= now - identity["started"] < STARTUP_SECONDS else "not-watching"
    if not isinstance(pulse.get("at"), (int, float)):
        return "not-watching"
    age = now - pulse["at"]
    loop_pid = _read(session / "loop.json").get("pid", ref.pid)
    if pulse.get("pid") not in {ref.pid, loop_pid} or not 0 <= age <= PROGRESS_SECONDS:
        return "not-watching"
    return "watching" if pulse.get("phase") in {"poll", "dispatch"} else "not-watching"


def stop(ref, *, reason: str, operation: str, repository="", agent="", status="stopping") -> None:
    """Persist intent before termination so an independent observer can report it."""
    session, _identity = _session(ref)
    fields = {"stop_reason": reason, "operation": operation,
              "watcher_pid": ref.pid, "subscription_id": ref.key}
    request = (_read(session / "request.json") if session else {
        "repository": repository, "agent": agent, "run_id": uuid.uuid4().hex})
    if session is not None and status == "stopping":
        _write(session / "stop.json", fields)
    _event(request, status, **fields)


def retain(*, cutoff: float) -> int:
    """Only completed sessions are eligible; never erase active exit evidence."""
    if not _home().is_dir():
        return 0
    removed = 0
    for session in _home().iterdir():
        exit_path = session / "exit.json"
        if exit_path.is_file() and exit_path.stat().st_mtime < cutoff:
            shutil.rmtree(session)
            removed += 1
    return removed


if __name__ == "__main__":
    raise SystemExit(supervise(Path(sys.argv[1])))
