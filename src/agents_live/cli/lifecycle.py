"""Lifecycle composition above the runtime and agent ports."""
from __future__ import annotations

from collections.abc import Collection
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
    protected_targets: tuple[str, ...] = ()
    unknown_metadata: tuple[tuple[Path, tuple[str, ...]], ...] = ()
    required_runtimes: tuple[tuple[Path, runtime.RuntimeTarget], ...] = ()


def collect(
    *,
    additions: dict[Path, set[str]] | None = None,
    removals: dict[Path, set[str]] | None = None,
    persist: bool = True,
    roots: Collection[Path] | None = None,
) -> Collected:
    additions = {
        root.resolve(): names for root, names in (additions or {}).items()}
    removals = {
        root.resolve(): names for root, names in (removals or {}).items()}
    try:
        registry = repos.load()
    except ValueError as exc:
        raise CollectionUnavailable(str(exc)) from exc
    selected_roots = (
        {root.resolve() for root in roots} if roots is not None else None)
    roots = [
        (Path(value).resolve(), Path(value).is_dir())
        for value in registry["repos"].values()
        if selected_roots is None or Path(value).resolve() in selected_roots
    ]
    host = runtime.current()
    try:
        installed = {item.key for item in host.trigger_store.list()}
    except (OSError, RuntimeError, ValueError) as exc:
        raise CollectionUnavailable(
            f"trigger store is unreadable: {exc}") from exc
    discovered: dict[Path, dict[str, tuple[runtime.Subscription, ...]]] = {}
    specs_by_root: dict[Path, dict[str, agent.AgentSpec]] = {}
    unavailable: list[str] = []
    protected: list[str] = []
    broken: list[tuple[Path, str]] = []
    broken_by_root: dict[Path, tuple[agent.BrokenDefinition, ...]] = {}
    for root, readable in roots:
        if not readable:
            unavailable.append(str(root))
            protected.append(f"repo:{root}")
            discovered[root] = {}
            continue
        try:
            root_specs, discovered[root], root_broken = _discover(root)
        except (OSError, agent.DefinitionError, ValueError) as exc:
            unavailable.append(f"{root}: {exc}")
            protected.append(f"repo:{root}")
            discovered[root] = {}
        else:
            specs_by_root[root] = {
                spec.identifier: spec for spec in root_specs}
            broken.extend((item.path, item.message) for item in root_broken)
            broken_by_root[root] = root_broken

    registry_roots: set[Path] = set()
    blocked_ownership_roots: set[Path] = set()
    for root, readable in roots:
        if not readable:
            continue
        try:
            if not ownership.local_only(root):
                registry_roots.add(root)
        except ownership.OwnershipUnavailableError as exc:
            unavailable.append(f"{root}: {exc}")
            protected.append(f"repo:{root}")
            blocked_ownership_roots.add(root)

    owners: dict[str, str] | None = None
    if registry_roots:
        try:
            owners = ownership.load_owners(root=next(iter(registry_roots)))
        except ownership.OwnershipUnavailableError as exc:
            for root in registry_roots:
                unavailable.append(f"{root}: {exc}")
                protected.append(f"repo:{root}")
            blocked_ownership_roots.update(registry_roots)

    owner_by_identifier: dict[str, str | None] = {}
    if owners is not None and not blocked_ownership_roots:
        try:
            for root in registry_roots:
                specs = specs_by_root.get(root, {}).values()
                owner_by_identifier.update(ownership.resolve_owners(
                    ((spec.identifier, spec.name) for spec in specs), owners))
        except ownership.OwnershipUnavailableError as exc:
            for root in registry_roots:
                unavailable.append(f"{root}: {exc}")
                protected.append(f"repo:{root}")
            blocked_ownership_roots.update(registry_roots)

    snapshots: dict[Path, frozenset[str]] = {}
    initialized: dict[Path, bool] = {}
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
        for identifier, spec in specs_by_root.get(root, {}).items():
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
            initialized[root] = state.load(root).initialized
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
            complete = (
                f"repo:{root}" not in protected
                and not broken_by_root.get(root))
            explicit = bool(additions.get(root) or removals.get(root))
            if (not agents and not initialized.get(root)
                    and not complete and not explicit):
                # An adoption that found nothing, in a repository that did not
                # read completely, is not a fact about what the user started.
                # Recording it would spend the one chance to adopt.
                continue
            state.replace(root, agents)

    desired: list[runtime.Subscription] = []
    required_runtimes: set[tuple[Path, runtime.RuntimeTarget]] = set()
    for root, definitions in discovered.items():
        if root in blocked_ownership_roots:
            continue
        for identifier in sorted(snapshots[root]):
            if root in registry_roots:
                owner = owner_by_identifier.get(identifier)
                if owner is not None and not ownership.owns(owner):
                    if definitions.get(identifier):
                        owner_host, _, owner_runtime = ownership.display_owner(
                            owner).partition(ownership.SEPARATOR)
                        required_runtimes.add((root, runtime.RuntimeTarget(
                            owner_runtime or "unknown",
                            bool(owner_runtime) and (
                                owner_host == ownership.current_host()),
                        )))
                    continue
            desired.extend(definitions.get(identifier, ()))
    desired.append(_maintenance())
    # A started definition that stopped parsing has an unknown desired state,
    # not an empty one, so its artifacts are held rather than withdrawn.
    protected_targets = [
        f"agent:{identifier}"
        for root, items in broken_by_root.items()
        for identifier in (item.identifier_in(root) for item in items)
        if identifier in snapshots.get(root, frozenset())
    ]
    return Collected(
        tuple(desired),
        tuple(unavailable),
        tuple(legacy),
        tuple(broken),
        tuple(protected),
        tuple(protected_targets),
        tuple(
            (spec.prompt_path, spec.unknown_metadata)
            for specs in specs_by_root.values()
            for spec in specs.values()
            if spec.unknown_metadata
        ),
        tuple(sorted(required_runtimes, key=lambda item: (
            str(item[0]), item[1].runtime, item[1].paired))),
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
        protected_targets=collected.protected_targets,
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
) -> tuple[
    tuple[agent.AgentSpec, ...],
    dict[str, tuple[runtime.Subscription, ...]],
    tuple[agent.BrokenDefinition, ...],
]:
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
    return discovery.specs, result, discovery.broken


def _maintenance() -> runtime.Subscription:
    identity = ownership.current_owner_id()
    installation = identity.rsplit(":", 1)[-1]
    return runtime.Subscription.create(
        scope=f"runtime:{installation}",
        target="runtime",
        kind="schedule",
        trigger="*/5 * * * *",
    )
