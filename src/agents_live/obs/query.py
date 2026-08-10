"""Read durable event records through one versioned decoder."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable


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
    records: list[dict[str, object]] = []
    for path in paths:
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                raw = json.loads(line)
            except json.JSONDecodeError:
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


def normalize(raw: object) -> dict[str, object] | None:
    if not isinstance(raw, dict):
        return None
    if raw.get("log_schema") == 5:
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