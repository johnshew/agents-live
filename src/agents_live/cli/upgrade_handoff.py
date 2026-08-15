"""Durable coordination for deferred native Windows self-upgrades."""
from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path

from .. import paths, runtime
from ..obs import admin as adminlog
from ..runtime import ProcessRef
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
        helper = _process_ref(pending.get("helper"))
        if (helper is None
                and result is not None
                and result.get("operation_id") == pending.get("operation_id")
                and result.get("status") == "started"
                and isinstance(result.get("helper_pid"), int)
                and isinstance(result.get("helper_started_at"), (int, float))):
            helper = ProcessRef(
                result["helper_pid"],
                float(result["helper_started_at"]),
                "powershell.exe",
                "upgrade",
                str(pending.get("operation_id", "")),
            )
            pending["helper"] = asdict(helper)
            _write(pending_path, pending)
        created_at = float(pending.get("created_at", 0.0))
        alive = (
            helper is not None
            and runtime.current().supervisor.alive(helper)
        )
        if alive or (helper is None and now - created_at < _STALE_AFTER_S):
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
            "helper": None,
            "result_path": str(result_path),
            "transcript_path": str(transcript_path),
            "source": source,
            "runtime_only": runtime_only,
        })
        return claim, None


def spawned(claim: Claim, helper: ProcessRef) -> None:
    with hostruntime.exclusive_lock(_lock_path(), blocking=True):
        pending = _read(claim.pending_path)
        if pending is None or pending.get("operation_id") != claim.operation_id:
            return
        pending["helper"] = asdict(helper)
        _write(claim.pending_path, pending)


def request_quiescence(
    claim: Claim,
    watchers: list[tuple[int, str, str | None]],
) -> tuple[tuple[str, str | None], ...]:
    """Ask installed-tool watchers to exit at their next idle boundary."""
    identities = tuple(sorted({
        (name, project) for _pid, name, project in watchers
    }, key=lambda item: (item[0], item[1] or "")))
    with hostruntime.exclusive_lock(_lock_path(), blocking=True):
        pending = _read(claim.pending_path)
        if pending is None or pending.get("operation_id") != claim.operation_id:
            raise RuntimeError("Windows upgrade handoff claim was lost")
        pending["quiesce_watchers"] = [
            {"name": name, "project": project}
            for name, project in identities
        ]
        pending["quiesce_active"] = bool(identities)
        _write(claim.pending_path, pending)
    return identities


def quiesce_operation(executable: Path | str) -> str | None:
    """Operation asking a watcher in this executable's environment to exit."""
    candidate = Path(executable).resolve()
    try:
        with hostruntime.exclusive_lock(_lock_path(), blocking=True):
            _reconcile_locked()
            for pending_path in _directory().glob("*.pending.json"):
                pending = _read(pending_path)
                if pending is None or not pending.get("quiesce_active"):
                    continue
                environment = Path(str(pending.get("environment", ""))).resolve()
                if candidate == environment or environment in candidate.parents:
                    return str(pending.get("operation_id", "")) or None
    except Exception:
        return None
    return None


def begin_restoration(
    operation_id: str,
) -> tuple[tuple[str, str | None], ...]:
    """Deactivate quiescence and return identities for restoration."""
    try:
        with hostruntime.exclusive_lock(_lock_path(), blocking=True):
            for pending_path in _directory().glob("*.pending.json"):
                pending = _read(pending_path)
                if pending is None or pending.get("operation_id") != operation_id:
                    continue
                values = pending.get("quiesce_watchers", [])
                if not isinstance(values, list):
                    raise RuntimeError("upgrade quiesce identities are unreadable")
                identities = tuple(
                    (str(item["name"]), item.get("project"))
                    for item in values
                    if isinstance(item, dict)
                    and isinstance(item.get("name"), str)
                    and item["name"]
                    and (item.get("project") is None
                         or isinstance(item.get("project"), str))
                )
                pending["quiesce_active"] = False
                _write(pending_path, pending)
                return identities
    except (OSError, RuntimeError, ValueError) as exc:
        raise RuntimeError(
            f"could not read Windows upgrade quiescence: {exc}") from exc
    return ()


def _process_ref(value: object) -> ProcessRef | None:
    if not isinstance(value, dict):
        return None
    try:
        return ProcessRef(**value)
    except (TypeError, ValueError):
        return None


def abandon(claim: Claim) -> None:
    with hostruntime.exclusive_lock(_lock_path(), blocking=True):
        pending = _read(claim.pending_path)
        if pending is not None and pending.get("operation_id") == claim.operation_id:
            claim.pending_path.unlink(missing_ok=True)