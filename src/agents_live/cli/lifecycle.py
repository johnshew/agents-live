"""Lifecycle composition above the runtime and agent ports."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .. import agent, runtime, state
from ..state import ownership, registry as repos


class CollectionUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class Collected:
    subscriptions: tuple[runtime.Subscription, ...]
    unavailable_repositories: tuple[str, ...]
    legacy: tuple[tuple[Path, str], ...] = ()
    broken_definitions: tuple[tuple[Path, str], ...] = ()
    protected_scopes: tuple[str, ...] = ()


def collect(
    *,
    additions: dict[Path, set[str]] | None = None,
    removals: dict[Path, set[str]] | None = None,
    persist: bool = True,
) -> Collected:
    additions = {
        root.resolve(): names for root, names in (additions or {}).items()}
    removals = {
        root.resolve(): names for root, names in (removals or {}).items()}
    try:
        registry = repos.load()
    except ValueError as exc:
        raise CollectionUnavailable(str(exc)) from exc
    roots = [(Path(value).resolve(), Path(value).is_dir())
             for value in registry["repos"].values()]
    host = runtime.current()
    try:
        installed = {item.key for item in host.trigger_store.list()}
    except (OSError, RuntimeError, ValueError) as exc:
        raise CollectionUnavailable(
            f"trigger store is unreadable: {exc}") from exc
    discovered: dict[Path, dict[str, tuple[runtime.Subscription, ...]]] = {}
    unavailable: list[str] = []
    protected: list[str] = []
    broken: list[tuple[Path, str]] = []
    for root, readable in roots:
        if not readable:
            unavailable.append(str(root))
            protected.append(f"repo:{root}")
            discovered[root] = {}
            continue
        try:
            discovered[root], root_broken = _discover(root)
        except (OSError, agent.DefinitionError, ValueError) as exc:
            unavailable.append(f"{root}: {exc}")
            protected.append(f"repo:{root}")
            discovered[root] = {}
        else:
            broken.extend((item.path, item.message) for item in root_broken)

    owners: dict[str, str] | None = None
    if not ownership.local_only():
        try:
            owners = ownership.load_owners()
        except ownership.OwnershipUnavailableError as exc:
            raise CollectionUnavailable(str(exc)) from exc

    owner_by_identifier: dict[str, str | None] = {}
    if owners is not None:
        try:
            for root, definitions in discovered.items():
                specs = [agent.load(identifier, root=root)
                         for identifier in definitions]
                owner_by_identifier.update(ownership.resolve_owners(
                    ((spec.identifier, spec.name) for spec in specs), owners))
        except ownership.OwnershipUnavailableError as exc:
            raise CollectionUnavailable(str(exc)) from exc

    snapshots: dict[Path, frozenset[str]] = {}
    legacy: list[tuple[Path, str]] = []
    for root, definitions in discovered.items():
        adopted = {
            name for name, subscriptions in definitions.items()
            if any(item.key in installed for item in subscriptions)
        }
        try:
            legacy_names = host.legacy_agents(str(root))
        except (OSError, RuntimeError, ValueError) as exc:
            raise CollectionUnavailable(
                f"legacy trigger store is unreadable for {root}: {exc}") from exc
        identifiers_by_name: dict[str, list[str]] = {}
        for identifier in definitions:
            spec = agent.load(identifier, root=root)
            identifiers_by_name.setdefault(spec.name, []).append(identifier)
        for name in legacy_names:
            matches = identifiers_by_name.get(name, [])
            if len(matches) > 1:
                raise CollectionUnavailable(
                    f"legacy trigger {name!r} in {root} is ambiguous; "
                    "start the intended definitions by canonical identifier")
            if matches:
                adopted.add(matches[0])
        try:
            snapshot = state.load_or_adopt(root, adopted, persist=False)
        except state.StartedStateUnavailable as exc:
            raise CollectionUnavailable(str(exc)) from exc
        snapshots[root] = frozenset(
            (snapshot.agents | additions.get(root, set()))
            - removals.get(root, set()))
        legacy.extend(
            (root, name) for name in legacy_names
            if identifiers_by_name.get(name, [None])[0] in snapshots[root]
        )

    if persist:
        for root, agents in snapshots.items():
            state.replace(root, agents)

    desired: list[runtime.Subscription] = []
    for root, definitions in discovered.items():
        for identifier in sorted(snapshots[root]):
            if owners is not None:
                owner = owner_by_identifier.get(identifier)
                if owner is not None and not ownership.owns(owner):
                    continue
            desired.extend(definitions.get(identifier, ()))
    desired.append(_maintenance())
    return Collected(
        tuple(desired),
        tuple(unavailable),
        tuple(legacy),
        tuple(broken),
        tuple(protected),
    )


def converge(
    *,
    additions: dict[Path, set[str]] | None = None,
    removals: dict[Path, set[str]] | None = None,
    dry_run: bool = False,
) -> runtime.Converged:
    collected = collect(
        additions=additions,
        removals=removals,
        persist=not dry_run,
    )
    converged = runtime.converge(
        collected.subscriptions,
        dry_run=dry_run,
        protected_scopes=collected.protected_scopes,
    )
    if dry_run or converged.failed:
        return converged
    host = runtime.current()
    done = list(converged.done)
    failed = list(converged.failed)
    for root, name in collected.legacy:
        operation = runtime.Operation(
            "remove-legacy", f"{root}:{name}",
            "canonical subscriptions replaced legacy triggers")
        try:
            host.remove_legacy(str(root), name)
        except Exception as exc:
            failed.append((operation, str(exc)))
        else:
            done.append(operation)
    health = converged.health
    if failed and health.healthy:
        health = runtime.Health(
            False, health.liveness, health.budget_tripped,
            (*health.detail, "legacy trigger cleanup failed"))
    return runtime.Converged(
        False, tuple(done), tuple(failed), health)


def _discover(
    root: Path,
) -> tuple[dict[str, tuple[runtime.Subscription, ...]], tuple[agent.BrokenDefinition, ...]]:
    result: dict[str, tuple[runtime.Subscription, ...]] = {}
    discovery = agent.discover(root)
    for spec in discovery.specs:
        config = spec.execution
        if config is None:
            continue
        subscriptions: list[runtime.Subscription] = []
        scope = f"repo:{root}"
        target = f"agent:{spec.identifier}"
        for schedule in config.schedules:
            canonical = runtime.parse_schedule(schedule).canonical
            subscriptions.append(runtime.Subscription.create(
                scope=scope, target=target, kind="schedule", trigger=canonical))
        if config.watch:
            canonical_watch = runtime.parse_watch(config.watch).canonical
            subscriptions.append(runtime.Subscription.create(
                scope=scope, target=target, kind="watch", trigger=canonical_watch))
        result[spec.identifier] = tuple(subscriptions)
    return result, discovery.broken


def _maintenance() -> runtime.Subscription:
    identity = ownership.current_owner_id()
    installation = identity.rsplit(":", 1)[-1]
    return runtime.Subscription.create(
        scope=f"runtime:{installation}",
        target="runtime",
        kind="schedule",
        trigger="*/5 * * * *",
    )
