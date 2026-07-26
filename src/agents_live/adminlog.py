"""Host-scoped administrative event log.

Agent runs are logged per repository under ``<repo>/Agents/logs/<agent>.log``.
Administrative operations - the ones that change the host rather than run an
agent - have no agent and often no repository, so they land in a single
host-scoped stream at ``paths.host_logs_dir() / "admin.log"`` alongside the
health-check loop's own log. ``agents-live logs`` and ``logs timeline``
already union the host log directory, so these events are readable with no
reader change.

Identity convention: administrative events are not agents. They carry
``scope="host"`` and the pseudo-agent name ``admin`` so the existing readers,
which group and display by ``agent_name``, keep working while a query can
still separate administration from agent activity::

    agents-live logs --all --sql "select * from logs where scope = 'host'"

Every event also records ``operation`` (the administrative verb),
``command`` (the invoking argv), and ``interactive`` (whether a terminal was
attached), so a state change traces back to a cron entry, a CLI invocation,
or an agent.

Writing is best-effort: an unwritable state directory must never fail the
operation being recorded.
"""
from __future__ import annotations

import os
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

try:
    from . import paths
except ImportError:  # flat import under `uv run --script` dispatches
    import paths

SCOPE = "host"
AGENT_NAME = "admin"
PHASE = "admin"
MAX_COMMAND_LENGTH = 512


def log_path() -> Path:
    return paths.host_logs_dir() / "admin.log"


def _command() -> str:
    argv = list(sys.argv) or [""]
    argv[0] = os.path.basename(argv[0]) or argv[0]
    return " ".join(argv)[:MAX_COMMAND_LENGTH]


def _interactive() -> bool:
    try:
        return sys.stdin.isatty() and sys.stdout.isatty()
    except (AttributeError, ValueError):
        return False


def record(name: str, **fields: Any) -> None:
    """Append one administrative event named *name*. Never raises."""
    try:
        try:
            from .headless import log_event
        except ImportError:  # flat import, as above
            from headless import log_event
        # `timeline` renders by phase and message; one shared phase keeps
        # administration legible as a single track, with the operation
        # naming the verb.
        fields.setdefault("phase", PHASE)
        fields.setdefault("message", name)
        log_event(
            log_path(),
            scope=SCOPE,
            agent_name=AGENT_NAME,
            operation=name,
            command=_command(),
            interactive=_interactive(),
            **fields,
        )
    except Exception:  # logging must never break the operation it records
        pass


@contextmanager
def operation(name: str, **fields: Any) -> Iterator[dict[str, Any]]:
    """Record ``name`` as a start/end pair around the wrapped work.

    The yielded dict collects fields to attach to the end event, so a caller
    can record what it learned while doing the work (a resolved version, a
    count) without repeating the fields it already passed to the start.
    """
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
