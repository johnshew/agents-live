"""Fail-open durable dispatch budget, one counter per repository."""
from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class BudgetResult:
    allowed: bool
    count: int
    limit: int


def claim(path: Path, *, limit: int = 60, window_s: float = 60.0, now: float | None = None) -> BudgetResult:
    instant = time.time() if now is None else now
    lock = path.with_suffix(f"{path.suffix}.lock")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        _acquire(lock)
        try:
            return _claim(path, limit=limit, window_s=window_s, instant=instant)
        finally:
            lock.unlink(missing_ok=True)
    except (OSError, TimeoutError, TypeError, ValueError, json.JSONDecodeError):
        return BudgetResult(True, 0, limit)


def _claim(
    path: Path,
    *,
    limit: int,
    window_s: float,
    instant: float,
) -> BudgetResult:
    raw = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
    if not isinstance(raw, list):
        raw = []
    timestamps = [
        float(value) for value in raw
        if isinstance(value, (int, float)) and instant - window_s < float(value) <= instant
    ]
    allowed = len(timestamps) < limit
    if allowed:
        timestamps.append(instant)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(timestamps, stream, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return BudgetResult(allowed, len(timestamps), limit)


def _acquire(path: Path, *, timeout_s: float = 2.0) -> None:
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            descriptor = os.open(
                path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            try:
                stale = time.time() - path.stat().st_mtime > 30.0
            except OSError:
                stale = False
            if stale:
                path.unlink(missing_ok=True)
                continue
            if time.monotonic() >= deadline:
                raise TimeoutError("dispatch budget lock is busy")
            time.sleep(0.01)
            continue
        with os.fdopen(descriptor, "w", encoding="ascii") as stream:
            stream.write(str(os.getpid()))
        return
