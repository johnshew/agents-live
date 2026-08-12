"""Read durable event records through one versioned decoder."""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

_RELATIVE_COMPACT = re.compile(r"^(\d+)\s*([mhd])$")
_RELATIVE_WORDS = re.compile(
    r"^(\d+)\s*(min|mins|minute|minutes|h|hr|hrs|hour|hours|d|day|days)"
    r"(?:\s+ago)?$",
    re.IGNORECASE,
)
_UNIT_TO_DELTA = {
    "m": "m", "min": "m", "mins": "m", "minute": "m", "minutes": "m",
    "h": "h", "hr": "h", "hrs": "h", "hour": "h", "hours": "h",
    "d": "d", "day": "d", "days": "d",
}


def resolve_since(value: str | None) -> str | None:
    """Normalize a relative or ISO-8601 bound to an aware UTC timestamp.

    Every reader shares this because the bound is compared as a string.
    An unresolved one does not fail: ``"2026-..." < "30m"`` is true and
    discards every record, while ``< "1h"`` is false and discards none,
    so the same window silently answered "nothing happened" or "here is
    everything" depending on which word the operator typed.
    """
    if value is None:
        return None
    text = value.strip()
    match = _RELATIVE_COMPACT.match(text) or _RELATIVE_WORDS.match(text)
    if match:
        unit = _UNIT_TO_DELTA[match.group(2).lower()]
        count = int(match.group(1))
        delta = {
            "m": timedelta(minutes=count),
            "h": timedelta(hours=count),
            "d": timedelta(days=count),
        }[unit]
        parsed = datetime.now(timezone.utc) - delta
    else:
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(
                f"invalid timestamp {value!r}; expected ISO-8601 or a relative "
                "duration such as 30m, 2h, or '1 day ago'"
            ) from exc
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def files(directory: Path) -> tuple[Path, ...]:
    if not directory.is_dir():
        return ()
    return tuple(sorted({
        *directory.glob("*.jsonl"),
        *directory.glob("*.log"),
    }))


def load(
    paths: Iterable[Path],
    *,
    text_filter: str | None = None,
    since: str | None = None,
) -> tuple[dict[str, object], ...]:
    since = resolve_since(since)
    records: list[dict[str, object]] = []
    for path in paths:
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                raw = json.loads(line)
            except ValueError:
                # ValueError, not json.JSONDecodeError: the flat script
                # dispatches put this module on sys.path twice, so the
                # attribute the handler resolves is not always the class
                # the raising copy of json produced. A record torn by two
                # appenders must never end a reader (#284).
                continue
            record = normalize(raw)
            if record is None:
                continue
            if since and str(record["ts"]) < since:
                continue
            if text_filter and text_filter.casefold() not in json.dumps(
                    record, sort_keys=True).casefold():
                continue
            records.append(record)
    return tuple(records)


def damaged(paths: Iterable[Path]) -> int:
    """How many lines no reader can decode.

    Skipping them silently is what let 11,577 torn records accumulate
    unnoticed: a dropped line looks exactly like one that was never
    written, so the loss has to be counted somewhere an operator reads.
    """
    total = 0
    for path in paths:
        try:
            lines = path.read_text(
                encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            if not line.strip():
                continue
            try:
                json.loads(line)
            except ValueError:
                total += 1
    return total


def normalize(raw: object) -> dict[str, object] | None:
    if not isinstance(raw, dict):
        return None
    if raw.get("log_schema") == 5:
        timestamp = raw.get("ts")
        if not isinstance(timestamp, str) or not timestamp:
            return None
        try:
            parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            return None
        return dict(raw)
    if raw.get("spec") != 1:
        return None
    required = ("timestamp", "event", "status", "agent", "run_id", "origin")
    if not all(isinstance(raw.get(field), str) for field in required):
        return None
    event = str(raw["event"])
    status = str(raw["status"])
    record = {
        "ts": raw["timestamp"],
        "log_schema": 1,
        "agent_name": raw["agent"],
        "phase": "done" if event == "run" else event,
        "status": {"success": "ok", "failed": "error"}.get(status, status),
        "message": raw.get("message", ""),
        "run_id": raw["run_id"],
        "trigger": raw["origin"],
        "error_category": raw.get("category"),
        "transcript": raw.get("transcript"),
        "exit_code": raw.get("exit_code"),
        "usage": raw.get("usage", []),
        "repository": raw.get("repository", ""),
        "changed_files": [],
    }
    attributes = raw.get("attributes", [])
    if isinstance(attributes, list):
        for item in attributes:
            if (
                isinstance(item, list)
                and len(item) == 2
                and isinstance(item[0], str)
                and item[0] not in record
            ):
                record[item[0]] = item[1]
    return record