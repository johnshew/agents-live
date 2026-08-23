"""Framework-owned retention for logs and per-run diagnostics."""
from __future__ import annotations

import contextlib
import json
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .. import paths
from ..runtime.hosts.processes import pid_exists


DEFAULT_RETENTION_DAYS = 30
ACTIVE_MARKER = ".active"
_TIMESTAMP = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}"
    r"(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})")


@dataclass(frozen=True)
class Result:
    rotated_logs: int = 0
    removed_archives: int = 0
    removed_run_artifacts: int = 0

    def __add__(self, other: "Result") -> "Result":
        return Result(
            self.rotated_logs + other.rotated_logs,
            self.removed_archives + other.removed_archives,
            self.removed_run_artifacts + other.removed_run_artifacts,
        )


def retention_days(root: Path) -> int:
    value = paths.load_config(root).get(
        "retention_days", DEFAULT_RETENTION_DAYS)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError("retention_days must be a positive integer")
    return value


def maintain(root: Path, *, now: datetime | None = None) -> Result:
    """Rotate and retain one repository's framework-owned artifacts."""
    return maintain_state(
        paths.repo_state_dir(root),
        days=retention_days(root),
        now=now,
    )


def maintain_host(
    *,
    days: int = DEFAULT_RETENTION_DAYS,
    now: datetime | None = None,
) -> Result:
    """Rotate host-scoped logs using the host's effective policy."""
    return _maintain_logs(
        paths.host_logs_dir(),
        cutoff=_cutoff(days, now),
        now=now,
    )


def maintain_state(
    state: Path,
    *,
    days: int,
    now: datetime | None = None,
) -> Result:
    cutoff = _cutoff(days, now)
    logs = _maintain_logs(state / "logs", cutoff=cutoff, now=now)
    removed = _maintain_runs(state / "runs", cutoff=cutoff)
    return logs + Result(removed_run_artifacts=removed)


def mark_active(directory: Path) -> Path:
    """Mark a run directory before any child can create retained output."""
    marker = directory / ACTIVE_MARKER
    paths.atomic_write_text(
        marker,
        json.dumps({"pid": os.getpid(), "created": time.time()}) + "\n",
        mode=0o600,
    )
    return marker


def _cutoff(days: int, now: datetime | None) -> datetime:
    instant = now or datetime.now(timezone.utc)
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=timezone.utc)
    return instant.astimezone(timezone.utc) - timedelta(days=days)


def _maintain_logs(
    directory: Path,
    *,
    cutoff: datetime,
    now: datetime | None,
) -> Result:
    if not directory.is_dir():
        return Result()
    archive = directory / "archive"
    removed = _remove_expired_archives(archive, cutoff)
    rotated = 0
    for log in sorted({
        *directory.glob("*.jsonl"),
        *directory.glob("*.log"),
    }):
        if not _older_than(log, cutoff):
            continue
        archive.mkdir(parents=True, exist_ok=True)
        instant = now or datetime.now(timezone.utc)
        if instant.tzinfo is None:
            instant = instant.replace(tzinfo=timezone.utc)
        stamp = instant.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        destination = archive / f"{stamp}-{log.name}"
        try:
            os.replace(log, destination)
        except (FileNotFoundError, PermissionError):
            continue
        rotated += 1
    return Result(rotated_logs=rotated, removed_archives=removed)


def _remove_expired_archives(archive: Path, cutoff: datetime) -> int:
    if not archive.is_dir():
        return 0
    cutoff_epoch = cutoff.timestamp()
    removed = 0
    for item in sorted(archive.iterdir()):
        if (
            not item.is_file()
            or item.suffix not in {".jsonl", ".log", ".parquet"}
        ):
            continue
        try:
            if item.stat().st_mtime >= cutoff_epoch:
                continue
            item.unlink()
        except (FileNotFoundError, PermissionError):
            continue
        removed += 1
    return removed


def _older_than(path: Path, cutoff: datetime) -> bool:
    """Whether the oldest timestamp in an append-only log crossed cutoff."""
    try:
        with path.open(encoding="utf-8", errors="replace") as stream:
            for line in stream:
                timestamp = _line_timestamp(line)
                if timestamp is not None:
                    return timestamp < cutoff
        return datetime.fromtimestamp(
            path.stat().st_mtime, timezone.utc) < cutoff
    except (OSError, UnicodeError):
        return False


def _line_timestamp(line: str) -> datetime | None:
    try:
        raw = json.loads(line)
    except ValueError:
        raw = None
    value = None
    if isinstance(raw, dict):
        value = raw.get("ts", raw.get("timestamp"))
    if not isinstance(value, str):
        match = _TIMESTAMP.search(line)
        value = match.group(0) if match else None
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _maintain_runs(directory: Path, *, cutoff: datetime) -> int:
    if not directory.is_dir():
        return 0
    active = _active_run_ids(directory)
    cutoff_epoch = cutoff.timestamp()
    removed = 0
    for artifact in sorted(directory.rglob("*"), reverse=True):
        if artifact.is_dir() or artifact.name == ACTIVE_MARKER:
            continue
        if _run_id(artifact, directory) in active:
            continue
        try:
            if artifact.stat().st_mtime >= cutoff_epoch:
                continue
            artifact.unlink()
        except (FileNotFoundError, PermissionError):
            continue
        removed += 1
    for child in sorted(
        directory.rglob("*"), key=lambda item: len(item.parts), reverse=True,
    ):
        if child.is_dir():
            with contextlib.suppress(OSError):
                child.rmdir()
    return removed


def _active_run_ids(directory: Path) -> set[str]:
    active: set[str] = set()
    for marker in directory.glob(f"*/*/{ACTIVE_MARKER}"):
        try:
            payload = json.loads(marker.read_text(encoding="ascii"))
            pid = int(payload["pid"])
        except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
            pid = 0
        if pid > 0 and pid_exists(pid):
            active.add(marker.parent.name)
        else:
            with contextlib.suppress(OSError):
                marker.unlink(missing_ok=True)
    return active


def _run_id(path: Path, runs: Path) -> str | None:
    relative = path.relative_to(runs)
    if len(relative.parts) >= 3:
        return relative.parts[1]
    name = path.name
    for suffix in ("-pipeline.jsonl", "-agent-"):
        if suffix in name:
            return name.split(suffix, 1)[0]
    return None
