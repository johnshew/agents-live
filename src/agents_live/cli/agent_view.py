"""Read model composed from agent definitions and machine-local state."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .. import agent, obs, paths, runtime, state
from ..state import ownership


@dataclass(frozen=True)
class AgentView:
    name: str
    identifier: str
    description: str | None
    state: str
    state_error: str | None
    definition_error: str | None
    owner: str | None
    is_owner: bool
    ownership_available: bool
    ownership_error: str | None
    runtime: str | None
    model: str | None
    mode: str | None
    schedules: tuple[str, ...]
    watch: str | None
    consecutive_failures: int | None
    watcher_liveness: str
    observations_available: bool
    observation_error: str | None


@dataclass(frozen=True)
class HealthVerdict:
    healthy: bool
    category: str
    detail: str
    remedy: str | None = None


def health_verdict(payload: dict) -> HealthVerdict:
    """Canonical interpretation of a fresh host health beacon."""
    smoketest = payload.get("smoketest")
    smoketest_status = (
        str(smoketest.get("status", "")).lower()
        if isinstance(smoketest, dict) else ""
    )
    if payload.get("status") == "healthy" and smoketest_status == "pass":
        return HealthVerdict(True, "healthy", "fresh")
    if smoketest_status == "fail":
        return HealthVerdict(
            False, "smoketest_failed",
            "current framework smoketest verdict is failed",
            "agents-live smoketest",
        )
    if smoketest_status != "pass":
        return HealthVerdict(
            False, "smoketest_unknown",
            "current framework smoketest verdict is missing or unknown",
            "agents-live smoketest",
        )
    failures = payload.get("agent_failures")
    if isinstance(failures, list) and failures:
        first = failures[0] if isinstance(failures[0], dict) else {}
        identifier = first.get("agent")
        count = first.get("consecutive_failures")
        if isinstance(identifier, str) and isinstance(count, int):
            detail = f"{identifier} has {count} consecutive failures"
            remedy = f"agents-live logs --agent {identifier} --errors"
        else:
            detail = str(first.get("detail") or "an agent has repeated failures")
            remedy = str(first.get("remedy") or "agents-live logs --errors")
        return HealthVerdict(
            False, "agent_repeated_failures",
            detail, remedy,
        )
    return HealthVerdict(
        False, "health_degraded", "current health record is degraded",
        "agents-live doctor",
    )


def runtime_observations(
    root: Path,
    specs: tuple[agent.AgentSpec, ...],
    started: frozenset[str],
    is_owner: dict[str, bool],
) -> dict[str, tuple[int | None, str, str | None]]:
    """Canonical failure and watcher state for status consumers."""
    observation_error = None
    try:
        failures = obs.consecutive_failures(
            obs.files(paths.repo_state_dir(root) / "logs"))
    except OSError as exc:
        failures = {}
        observation_error = str(exc)
    liveness = {spec.identifier: "not-required" for spec in specs}
    watched = [
        spec for spec in specs
        if spec.execution is not None
        and spec.execution.watch
        and spec.identifier in started
        and is_owner.get(spec.identifier, False)
    ]
    if watched:
        try:
            host = runtime.current()
            installed = {item.key: item for item in host.trigger_store.list()}
            processes = {
                item.key: item for item in host.supervisor.owned(role="watcher")}
            for spec in watched:
                execution = spec.execution
                assert execution is not None and execution.watch
                subscription = runtime.Subscription.create(
                    scope=f"repo:{root}", target=f"agent:{spec.identifier}",
                    kind="watch",
                    trigger=runtime.parse_watch(execution.watch).canonical,
                )
                expected = host.render(subscription)
                trigger = installed.get(expected.key)
                process = processes.get(expected.key)
                if process is None:
                    liveness[spec.identifier] = "missing"
                elif (
                    trigger is None
                    or trigger.fingerprint != expected.fingerprint
                    or process.fingerprint != expected.fingerprint
                ):
                    liveness[spec.identifier] = "degraded"
                else:
                    liveness[spec.identifier] = "alive"
        except Exception as exc:
            observation_error = str(exc)
            for spec in watched:
                liveness[spec.identifier] = "unavailable"
    return {
        spec.identifier: (
            failures.get(spec.identifier, 0),
            liveness[spec.identifier], observation_error)
        for spec in specs
    }


def repository_agents(
    root: Path,
    *,
    ownership_rate_limit_secs: int = 60,
) -> tuple[AgentView, ...]:
    """Return one fail-closed view of every loadable agent in *root*."""
    try:
        started = state.load(root)
        started_identifiers = started.agents
        state_error = None
    except state.StartedStateUnavailable as exc:
        started_identifiers = frozenset()
        state_error = str(exc)
    try:
        discovery = agent.discover(root)
        specs = discovery.specs
        broken = discovery.broken
    except agent.DefinitionError as exc:
        specs = ()
        broken = (agent.BrokenDefinition(root, str(exc)),)
    ownership_available = True
    ownership_error = None
    owner_by_identifier: dict[str, str | None] = {
        spec.identifier: None for spec in specs
    }
    is_owner_by_identifier = {
        spec.identifier: True for spec in specs
    }
    try:
        if not ownership.local_only(root):
            owners = ownership.load_owners(
                root=root, rate_limit_secs=ownership_rate_limit_secs)
            owner_by_identifier.update(ownership.resolve_owners(
                ((spec.identifier, spec.name) for spec in specs), owners))
            is_owner_by_identifier = {
                identifier: ownership.owns(owner) if owner is not None else True
                for identifier, owner in owner_by_identifier.items()
            }
    except ownership.OwnershipUnavailableError as exc:
        ownership_available = False
        ownership_error = str(exc)
        is_owner_by_identifier = {
            spec.identifier: False for spec in specs
        }

    observations = runtime_observations(
        root, specs, started_identifiers,
        is_owner_by_identifier,
    )
    rows = []
    for spec in specs:
        execution = spec.execution
        owner = owner_by_identifier[spec.identifier]
        rows.append(AgentView(
            name=spec.name,
            identifier=spec.identifier,
            description=spec.properties.description,
            state=("unavailable" if state_error else
                   "started" if spec.identifier in started_identifiers else
                   "stopped"),
            state_error=state_error,
            definition_error=None,
            owner=owner,
            is_owner=is_owner_by_identifier[spec.identifier],
            ownership_available=ownership_available,
            ownership_error=ownership_error,
            runtime=execution.selector.provider if execution else None,
            model=execution.selector.model if execution else None,
            mode=execution.mode if execution else None,
            schedules=execution.schedules if execution else (),
            watch=execution.watch if execution else None,
            consecutive_failures=observations[spec.identifier][0],
            watcher_liveness=observations[spec.identifier][1],
            observations_available=observations[spec.identifier][2] is None,
            observation_error=observations[spec.identifier][2],
        ))
    for item in broken:
        rows.append(AgentView(
            name=item.name,
            identifier="",
            description=None,
            state="unloadable",
            state_error=state_error,
            definition_error=item.message,
            owner=None,
            is_owner=False,
            ownership_available=ownership_available,
            ownership_error=ownership_error,
            runtime=None,
            model=None,
            mode=None,
            schedules=(),
            watch=None,
            consecutive_failures=None,
            watcher_liveness="unavailable",
            observations_available=False,
            observation_error="definition unavailable",
        ))
    return tuple(sorted(rows, key=lambda row: (row.name, row.identifier)))