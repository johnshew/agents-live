"""Administrative events for started-intent changes (#591).

``start`` and ``stop`` change whether an agent runs automatically. Each
affected agent gets one host admin event naming it, so ``logs --agent`` and
``logs timeline <agent>`` can answer who changed it, when, and with what
command. Recording never changes the command's outcome.
"""
from __future__ import annotations

import uuid
from pathlib import Path
from typing import Iterable

from .. import state
from ..obs import admin

STARTED = "started"
STOPPED = "stopped"
UNKNOWN = "unknown"


def correlation() -> str:
    """One identifier shared by every event a single command records."""
    return uuid.uuid4().hex


def started(root: Path) -> frozenset[str] | None:
    """Started identifiers for ``root``, or None when unreadable."""
    try:
        return state.load(root).agents
    except Exception:
        return None


def label(agents: frozenset[str] | None, identifier: str | None) -> str:
    if agents is None or not identifier:
        return UNKNOWN
    return STARTED if identifier in agents else STOPPED


def record(
    action: str,
    *,
    root: Path,
    name: str,
    identifier: str | None,
    previous: str,
    outcome: str = "ok",
    category: str | None = None,
    detail: str = "",
    correlation_id: str | None = None,
) -> None:
    """Append one ``agent-<action>`` admin event for one agent."""
    try:
        new = label(started(root), identifier) if identifier else UNKNOWN
        target = identifier or name
        message = f"{action} '{name}'"
        if identifier:
            message += f" ({identifier})"
        message += f": {previous} -> {new}"
        if outcome != "ok":
            message += f" [{outcome}]"
        if detail:
            message += f": {detail}"
        fields: dict[str, object] = {
            "status": "ok" if outcome == "ok" else "error",
            "message": message,
            "root": str(root),
            "target_agent": target,
            "target_name": name,
            "previous_state": previous,
            "new_state": new,
            "outcome": outcome,
        }
        if outcome != "ok":
            fields["level"] = "error"
            fields["error_category"] = category or outcome
        if correlation_id:
            fields["correlation_id"] = correlation_id
        admin.record(f"agent-{action}", **fields)
    except Exception:
        pass


def record_converged(
    action: str,
    *,
    root: Path,
    agents: Iterable[tuple[str, str]],
    previous: frozenset[str] | None,
    failures: Iterable[str],
    correlation_id: str,
) -> None:
    """One event per ``(name, identifier)`` after convergence returned."""
    failed = [str(item) for item in failures]
    for name, identifier in agents:
        record(
            action, root=root, name=name, identifier=identifier,
            previous=label(previous, identifier),
            outcome="failed" if failed else "ok",
            category="convergence_failed" if failed else None,
            detail="; ".join(failed)[:400],
            correlation_id=correlation_id)
