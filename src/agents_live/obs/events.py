"""Versioned local event schema."""
from __future__ import annotations

import json
import os
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
    payload = (
        json.dumps(asdict(event), sort_keys=True, separators=(",", ":"))
        + "\n").encode("utf-8")
    append(path, payload)


def append(path: Path, payload: bytes) -> None:
    """Add one record with a single append write.

    Watchers, scheduled runs, and the maintenance loop all append to the
    same files. Text-mode buffering flushes on its own boundaries rather
    than on record boundaries, so a record can leave in two pieces and
    another writer lands between them; a live deployment accumulated
    11,577 records spliced into each other (#290). Readers skip what they
    cannot decode, so the loss is silent.

    ``O_APPEND`` makes the kernel position each write at the end, so one
    write per record is what keeps a record whole.
    """
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_BINARY", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        while payload:
            written = os.write(descriptor, payload)
            if not written:
                break
            payload = payload[written:]
    finally:
        os.close(descriptor)
