"""Read model composed from agent definitions and machine-local state."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .. import agent, state
from ..state import ownership


@dataclass(frozen=True)
class AgentView:
    name: str
    identifier: str
    description: str | None
    state: str
    owner: str | None
    is_owner: bool
    ownership_available: bool
    runtime: str | None
    model: str | None
    mode: str | None
    schedules: tuple[str, ...]
    watch: str | None


def repository_agents(
    root: Path,
    *,
    ownership_rate_limit_secs: int = 60,
) -> tuple[AgentView, ...]:
    """Return one fail-closed view of every loadable agent in *root*."""
    started = state.load(root)
    specs = agent.discover(root).specs
    ownership_available = True
    owner_by_identifier: dict[str, str | None] = {
        spec.identifier: None for spec in specs
    }
    if not ownership.local_only(root):
        try:
            owners = ownership.load_owners(
                root=root, rate_limit_secs=ownership_rate_limit_secs)
            owner_by_identifier.update(ownership.resolve_owners(
                ((spec.identifier, spec.name) for spec in specs), owners))
        except ownership.OwnershipUnavailableError:
            ownership_available = False

    rows = []
    for spec in specs:
        execution = spec.execution
        owner = owner_by_identifier[spec.identifier]
        rows.append(AgentView(
            name=spec.name,
            identifier=spec.identifier,
            description=spec.properties.description,
            state="started" if spec.identifier in started.agents else "stopped",
            owner=owner,
            is_owner=ownership_available and (
                ownership.owns(owner) if owner is not None else True),
            ownership_available=ownership_available,
            runtime=execution.selector.provider if execution else None,
            model=execution.selector.model if execution else None,
            mode=execution.mode if execution else None,
            schedules=execution.schedules if execution else (),
            watch=execution.watch if execution else None,
        ))
    return tuple(sorted(rows, key=lambda row: (row.name, row.identifier)))