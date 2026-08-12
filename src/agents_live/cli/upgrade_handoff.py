"""Durable coordination for deferred native Windows self-upgrades."""
from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from .. import paths
from ..obs import admin as adminlog
from ..runtime.hosts import system as hostruntime

_STALE_AFTER_S = 300


@dataclass(frozen=True)
class Claim:
    operation_id: str
    environment: Path
    pending_path: Path
    result_path: Path
    transcript_path: Path


def _directory() -> Path:
    return paths.state_home() / "upgrade-handoffs"


def _lock_path() -> Path:
    return _directory() / "handoffs.lock"


def _key(environment: Path) -> str:
    normalized = str(environment.resolve()).casefold().encode("utf-8")
    return hashlib.sha256(normalized).hexdigest()[:24]


def _read(path: Path) -> dict | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _write(path: Path, value: dict) -> None:
    paths.atomic_write_text(
        path, json.dumps(value, sort_keys=True, separators=(",", ":")),
        mode=0o600)


def _record_terminal(pending: dict, result: dict | None, *, stale: bool) -> None:
    operation_id = str(pending.get("operation_id", "unknown"))
    exit_code = result.get("exit_code") if result else None
    ok = not stale and exit_code == 0
    message = (
        "deferred Windows upgrade completed" if ok else
        "deferred Windows upgrade helper exited without a terminal result"
        if stale else
        f"deferred Windows upgrade failed with exit code {exit_code}"
    )
    adminlog.record(
        "upgrade-runtime", status="ok" if ok else "error",
        level=None if ok else "error", deferred=True,
        correlation_id=operation_id, exit_code=exit_code,
        transcript=pending.get("transcript_path", ""), message=message)


def _reconcile_locked() -> None:
    directory = _directory()
    if not directory.is_dir():
        return
    now = time.time()
    for pending_path in directory.glob("*.pending.json"):
        pending = _read(pending_path)
        if pending is None:
            pending_path.unlink(missing_ok=True)
            continue
        result_path = Path(str(pending.get("result_path", "")))
        result = _read(result_path) if result_path.is_file() else None
        if (result is not None
            and result.get("operation_id") == pending.get("operation_id")
            and result.get("status") == "terminal"):
            _record_terminal(pending, result, stale=False)
            pending_path.unlink(missing_ok=True)
            result_path.unlink(missing_ok=True)
            continue
        helper_pid = pending.get("helper_pid")
        if (helper_pid is None
                and result is not None
                and result.get("operation_id") == pending.get("operation_id")
                and result.get("status") == "started"
                and isinstance(result.get("helper_pid"), int)):
            helper_pid = result["helper_pid"]
            pending["helper_pid"] = helper_pid
            _remember_identity(pending, helper_pid)
            _write(pending_path, pending)
        created_at = float(pending.get("created_at", 0.0))
        alive = _helper_is_running(pending, helper_pid)
        if alive or (helper_pid is None and now - created_at < _STALE_AFTER_S):
            continue
        _record_terminal(pending, result, stale=True)
        pending_path.unlink(missing_ok=True)
        result_path.unlink(missing_ok=True)


def reconcile() -> None:
    """Promote finished helpers into admin events; never break a command."""
    try:
        with hostruntime.exclusive_lock(_lock_path(), blocking=True):
            _reconcile_locked()
    except Exception:
        pass


def claim(environment: Path, *, source: str, runtime_only: bool
          ) -> tuple[Claim | None, str | None]:
    """Claim the one deferred-upgrade slot for *environment*."""
    directory = _directory()
    directory.mkdir(parents=True, exist_ok=True)
    pending_path = directory / f"{_key(environment)}.pending.json"
    with hostruntime.exclusive_lock(_lock_path(), blocking=True):
        _reconcile_locked()
        existing = _read(pending_path)
        if existing is not None:
            return None, str(existing.get("operation_id", "unknown"))
        operation_id = uuid.uuid4().hex
        result_path = directory / f"{operation_id}.result.json"
        transcript_path = directory / f"{operation_id}.transcript.log"
        claim = Claim(
            operation_id, environment.resolve(), pending_path,
            result_path, transcript_path)
        _write(pending_path, {
            "schema": 1,
            "operation_id": operation_id,
            "environment": str(claim.environment),
            "created_at": time.time(),
            "helper_pid": None,
            "result_path": str(result_path),
            "transcript_path": str(transcript_path),
            "source": source,
            "runtime_only": runtime_only,
        })
        return claim, None


def spawned(claim: Claim, helper_pid: int) -> None:
    with hostruntime.exclusive_lock(_lock_path(), blocking=True):
        pending = _read(claim.pending_path)
        if pending is None or pending.get("operation_id") != claim.operation_id:
            return
        pending["helper_pid"] = helper_pid
        _remember_identity(pending, helper_pid)
        _write(claim.pending_path, pending)


def _remember_identity(pending: dict, helper_pid: int) -> None:
    """Pin the pid to the process that holds it right now.

    A pid outlives the process it named, and this record outlives both.
    Without the start time a reused pid reads as the upgrade still
    running, which refuses every later upgrade rather than reporting
    stale information.
    """
    started_at = hostruntime.process_start_time(helper_pid)
    if started_at is not None:
        pending["helper_started_at"] = started_at


def _helper_is_running(pending: dict, helper_pid: object) -> bool:
    if not isinstance(helper_pid, int) or not hostruntime.is_alive(helper_pid):
        return False
    recorded = pending.get("helper_started_at")
    if not isinstance(recorded, (int, float)):
        return True
    current = hostruntime.process_start_time(helper_pid)
    if current is None:
        return True
    return abs(current - float(recorded)) < 2.0


def abandon(claim: Claim) -> None:
    with hostruntime.exclusive_lock(_lock_path(), blocking=True):
        pending = _read(claim.pending_path)
        if pending is not None and pending.get("operation_id") == claim.operation_id:
            claim.pending_path.unlink(missing_ok=True)