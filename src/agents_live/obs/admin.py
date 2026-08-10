"""Host-scoped administrative events."""
from __future__ import annotations

import os
import re
import sys
import time
import uuid
from contextlib import contextmanager
from typing import Any, Iterator

from .. import paths
from .events import create, record as record_event

AGENT_NAME = "admin"
MAX_COMMAND_LENGTH = 512
REDACTED = "***"

_SECRET_FLAG = re.compile(
    r"^--?(?:[\w-]+-)?(?:token|key|secret|password|passwd|api[-_]?key)$",
    re.IGNORECASE)
_SECRET_ASSIGNMENT = re.compile(
    r"^(--?(?:[\w-]+-)?(?:token|key|secret|password|passwd|api[-_]?key))=.*$",
    re.IGNORECASE)


def log_path():
    return paths.host_logs_dir() / "admin.log"


def _redact(argv: list[str]) -> list[str]:
    safe: list[str] = []
    take_next = False
    for arg in argv:
        if take_next:
            safe.append(REDACTED)
            take_next = False
            continue
        assignment = _SECRET_ASSIGNMENT.match(arg)
        if assignment:
            safe.append(f"{assignment.group(1)}={REDACTED}")
            continue
        safe.append(arg)
        take_next = bool(_SECRET_FLAG.match(arg))
    return safe


def _command() -> str:
    argv = list(sys.argv) or [""]
    argv[0] = os.path.basename(argv[0]) or argv[0]
    return " ".join(_redact(argv))[:MAX_COMMAND_LENGTH]


def _interactive() -> bool:
    try:
        return sys.stdin.isatty() and sys.stdout.isatty()
    except (AttributeError, ValueError):
        return False


def record(name: str, **fields: Any) -> None:
    """Append one administrative event. Logging never breaks the operation."""
    try:
        status = str(fields.pop("status", "ok"))
        message = str(fields.pop("message", name))
        category = fields.pop("error_category", None)
        correlation_id = fields.pop("correlation_id", None)
        transcript = fields.pop("transcript", None)
        exit_code = fields.pop("exit_code", None)
        repository = str(fields.get("root", ""))
        attributes = {
            "scope": "host",
            "operation": name,
            "command": _command(),
            "interactive": _interactive(),
            **fields,
        }
        event = create(
            "admin",
            status,
            repository=repository,
            agent=AGENT_NAME,
                run_id=(str(correlation_id) if correlation_id is not None
                    else uuid.uuid4().hex),
            origin="cli",
            category=str(category) if category is not None else None,
            message=message,
                transcript=(str(transcript) if transcript is not None else None),
                exit_code=(int(exit_code) if exit_code is not None else None),
            attributes=tuple(attributes.items()),
        )
        record_event(log_path(), event)
    except Exception:
        pass


@contextmanager
def operation(name: str, **fields: Any) -> Iterator[dict[str, Any]]:
    """Record ``name`` as a start/end pair around the wrapped work."""
    record(name, status="start", **fields)
    started = time.time()
    end_fields: dict[str, Any] = {}
    try:
        yield end_fields
    except BaseException as exc:
        payload = {**fields, **end_fields}
        payload.update(status="error", level="error",
                       error_category=type(exc).__name__, message=str(exc))
        record(name, duration_s=round(time.time() - started, 3), **payload)
        raise
    payload = {**fields, **end_fields}
    payload.setdefault("status", "ok")
    record(name, duration_s=round(time.time() - started, 3), **payload)