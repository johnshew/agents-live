"""Lifecycle composition above the runtime and agent ports."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from . import agent, ownership, repos, runtime, state


class CollectionUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class Collected:
    subscriptions: tuple[runtime.Subscription, ...]
    unavailable_repositories: tuple[str, ...]


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
    for root, readable in roots:
        if not readable:
            unavailable.append(str(root))
            discovered[root] = {}
            continue
        try:
            discovered[root] = _discover(root)
        except (OSError, agent.DefinitionError, ValueError) as exc:
            unavailable.append(f"{root}: {exc}")
            discovered[root] = {}

    owners: dict[str, str] | None = None
    if not ownership.local_only():
        try:
            owners = ownership.load_owners()
        except ownership.OwnershipUnavailableError as exc:
            raise CollectionUnavailable(str(exc)) from exc

    snapshots: dict[Path, frozenset[str]] = {}
    for root, definitions in discovered.items():
        adopted = {
            name for name, subscriptions in definitions.items()
            if any(item.key in installed for item in subscriptions)
        }
        try:
            snapshot = state.load_or_adopt(root, adopted, persist=False)
        except state.StartedStateUnavailable as exc:
            raise CollectionUnavailable(str(exc)) from exc
        snapshots[root] = frozenset(
            (snapshot.agents | additions.get(root, set()))
            - removals.get(root, set()))

    if persist:
        for root, agents in snapshots.items():
            state.replace(root, agents)

    desired: list[runtime.Subscription] = []
    for root, definitions in discovered.items():
        for name in sorted(snapshots[root]):
            if owners is not None:
                owner = owners.get(name)
                if owner is not None and not ownership.owns(owner):
                    continue
            desired.extend(definitions.get(name, ()))
    desired.append(_maintenance())
    return Collected(tuple(desired), tuple(unavailable))


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
    return runtime.converge(collected.subscriptions, dry_run=dry_run)


def _discover(root: Path) -> dict[str, tuple[runtime.Subscription, ...]]:
    agents = root / "Agents"
    if not agents.is_dir():
        return {}
    legacy = sorted(agents.glob("*.md"))
    if legacy:
        raise agent.DefinitionError(
            "5.x flat definitions require migration: "
            + ", ".join(str(item) for item in legacy))
    result: dict[str, tuple[runtime.Subscription, ...]] = {}
    for skill in sorted(agents.iterdir()):
        if not skill.is_dir() or not (skill / "SKILL.md").is_file():
            continue
        spec = agent.load(skill.name, root=root)
        config = spec.execution
        if config is None:
            continue
        subscriptions: list[runtime.Subscription] = []
        scope = f"repo:{root}"
        target = f"agent:{spec.name}"
        for schedule in config.schedules:
            canonical = runtime.parse_schedule(schedule).canonical
            subscriptions.append(runtime.Subscription.create(
                scope=scope, target=target, kind="schedule", trigger=canonical))
        if config.watch:
            canonical_watch = runtime.parse_watch(config.watch).canonical
            subscriptions.append(runtime.Subscription.create(
                scope=scope, target=target, kind="watch", trigger=canonical_watch))
        result[spec.name] = tuple(subscriptions)
    return result


def _maintenance() -> runtime.Subscription:
    identity = ownership.current_owner_id()
    installation = identity.rsplit(":", 1)[-1]
    return runtime.Subscription.create(
        scope=f"runtime:{installation}",
        target="runtime",
        kind="schedule",
        trigger="*/5 * * * *",
    )
