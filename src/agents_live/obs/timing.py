"""Successful lock acquisitions, measured independently of wall-clock changes."""
from __future__ import annotations

import hashlib
import os
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .. import paths
from .events import create, record


@dataclass(frozen=True)
class Identity:
    operation: str
    run_id: str
    agent: str
    repository: str
    origin: str


_identity: ContextVar[Identity | None] = ContextVar("lock_identity", default=None)


def current(operation: str = "file-lock") -> Identity:
    return _identity.get() or Identity(
        operation, os.environ.get("AGENTS_LIVE_RUN_ID") or uuid.uuid4().hex,
        os.environ.get("AGENTS_LIVE_AGENT_ID", "admin"),
        os.environ.get("AGENTS_LIVE_REPO", ""),
        os.environ.get("AGENTS_LIVE_ORIGIN", "cli"),
    )


@contextmanager
def context(operation: str, *, run_id: str = "", agent: str = "",
            repository: str = "", origin: str = ""):
    previous = current(operation)
    token = _identity.set(Identity(
        operation, run_id or previous.run_id, agent or previous.agent,
        repository or previous.repository, origin or previous.origin))
    try:
        yield
    finally:
        _identity.reset(token)


@dataclass(frozen=True)
class Hold:
    identity: Identity
    kind: str
    lock_id: str
    acquisition_id: str
    acquired: float
    acquired_at: str
    wait_s: float

    def finish(self) -> None:
        try:
            self.emit("released", max(0.0, time.monotonic() - self.acquired))
        except Exception:
            pass

    def emit(self, status: str, held_s: float | None = None) -> None:
        try:
            record(paths.host_logs_dir() / "locks.jsonl", create(
                "lock", status, repository=self.identity.repository,
                agent=self.identity.agent, run_id=self.identity.run_id,
                origin=self.identity.origin,
                attributes=(
                    ("operation", self.identity.operation),
                    ("lock_kind", self.kind), ("lock_id", self.lock_id),
                    ("acquisition_id", self.acquisition_id),
                    ("lock_acquired_at", self.acquired_at),
                    ("lock_wait_s", self.wait_s), ("lock_hold_s", held_s),
                ),
            ))
        except Exception:
            # Telemetry must not prevent release or change the wrapped outcome.
            pass


def acquired(path: Path, kind: str, started: float, *,
             operation: str = "") -> Hold | None:
    try:
        moment = time.monotonic()
        identity = current(operation or kind)
        if operation:
            identity = Identity(operation, identity.run_id, identity.agent,
                                identity.repository, identity.origin)
        resource = os.path.normcase(str(path.absolute())).encode("utf-8")
        held = Hold(
            identity, kind, hashlib.sha256(resource).hexdigest()[:24],
            uuid.uuid4().hex, moment, datetime.now(timezone.utc).isoformat(),
            max(0.0, moment - started),
        )
        held.emit("acquired")
        return held
    except Exception:
        return None
