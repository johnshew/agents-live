"""Durable started-or-stopped facts, scoped to one repository path."""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .. import paths

_VERSION = 1


class StartedStateUnavailable(RuntimeError):
    """Started state exists but cannot be trusted, so collection must abstain."""


@dataclass(frozen=True)
class StartedSnapshot:
    initialized: bool
    agents: frozenset[str]


def _path(root: Path) -> Path:
    return paths.repo_state_dir(root) / "started.json"


def load(root: Path) -> StartedSnapshot:
    location = _path(root)
    if not location.exists():
        return StartedSnapshot(False, frozenset())
    try:
        raw = json.loads(location.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise StartedStateUnavailable(
            f"started state is unreadable at {location}: {exc}") from exc
    if (
        not isinstance(raw, dict)
        or raw.get("version") != _VERSION
        or not isinstance(raw.get("agents"), list)
        or not all(isinstance(item, str) and item for item in raw["agents"])
    ):
        raise StartedStateUnavailable(
            f"started state has an invalid format at {location}")
    return StartedSnapshot(True, frozenset(raw["agents"]))


def load_or_adopt(
    root: Path,
    installed_agents: set[str],
    *,
    persist: bool = True,
) -> StartedSnapshot:
    snapshot = load(root)
    if snapshot.initialized:
        return snapshot
    adopted = StartedSnapshot(True, frozenset(installed_agents))
    if persist:
        _write(root, adopted.agents)
    return adopted


def record(root: Path, agent_id: str) -> None:
    if not agent_id:
        raise ValueError("agent id must not be empty")
    snapshot = load(root)
    _write(root, snapshot.agents | {agent_id})


def clear(root: Path, agent_id: str) -> None:
    snapshot = load(root)
    _write(root, snapshot.agents - {agent_id})


def is_started(root: Path, agent_id: str) -> bool:
    snapshot = load(root)
    if not snapshot.initialized:
        return False
    return agent_id in snapshot.agents


def replace(root: Path, agents: frozenset[str] | set[str]) -> None:
    _write(root, agents)


def _write(root: Path, agents: frozenset[str] | set[str]) -> None:
    location = _path(root)
    location.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        {"agents": sorted(agents), "version": _VERSION},
        sort_keys=True,
        separators=(",", ":"),
    )
    descriptor, temporary = tempfile.mkstemp(
        dir=location.parent, prefix=f".{location.name}.")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, location)
    except OSError as exc:
        raise StartedStateUnavailable(
            f"could not write started state at {location}: {exc}") from exc
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
