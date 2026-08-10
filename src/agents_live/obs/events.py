"""Versioned local event schema."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = 1


@dataclass(frozen=True)
class Event:
    timestamp: str
    event: str
    status: str
    repository: str
    agent: str
    run_id: str
    origin: str
    category: str | None = None
    message: str = ""
    transcript: str | None = None
    exit_code: int | None = None
    usage: tuple[tuple[str, str | None], ...] = ()
    attributes: tuple[tuple[str, object], ...] = ()
    spec: int = SCHEMA_VERSION


def create(
    event: str,
    status: str,
    *,
    repository: str,
    agent: str,
    run_id: str,
    origin: str,
    category: str | None = None,
    message: str = "",
    transcript: str | None = None,
    exit_code: int | None = None,
    usage: tuple[tuple[str, str | None], ...] = (),
    attributes: tuple[tuple[str, object], ...] = (),
) -> Event:
    return Event(
        timestamp=datetime.now(timezone.utc).isoformat(),
        event=event,
        status=status,
        repository=repository,
        agent=agent,
        run_id=run_id,
        origin=origin,
        category=category,
        message=message,
        transcript=transcript,
        exit_code=exit_code,
        usage=usage,
        attributes=attributes,
    )


def record(path: Path, event: Event) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(
            json.dumps(asdict(event), sort_keys=True, separators=(",", ":"))
            + "\n")
