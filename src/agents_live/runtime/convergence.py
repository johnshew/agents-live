"""One idempotent convergence path over the host protocols."""
from __future__ import annotations

from collections.abc import Collection, Sequence
from dataclasses import dataclass, replace
from threading import RLock

from ..obs import timing

from .diff import diff
from .grammars import parse_schedule
from . import handoff, utc, watcher
from .protocols import HostAdapter
from .values import Converged, Health, InstalledTrigger, Operation, ProcessRef, Subscription

_adapter: HostAdapter | None = None
_lock = RLock()


def configure(adapter: HostAdapter) -> None:
    global _adapter
    with _lock:
        _adapter = adapter


def current() -> HostAdapter:
    global _adapter
    with _lock:
        if _adapter is None:
            from .hosts import current as current_host
            _adapter = current_host()
        return _adapter


def health() -> Health:
    return current().health()


@dataclass(frozen=True)
class Plan:
    host: HostAdapter
    operations: tuple[Operation, ...]
    addresses: tuple[tuple[str, str, str], ...] = ()
    utc_offsets: tuple[tuple[str, str], ...] = ()


def plan(
    subscriptions: Sequence[Subscription],
    *,
    installed: Sequence[InstalledTrigger] | None = None,
    watchers: Sequence[ProcessRef] | None = None,
    protected_scopes: Collection[str] = (),
    protected_targets: Collection[str] = (),
    protected_process_keys: Collection[str] = (),
    _host: HostAdapter | None = None,
) -> Plan:
    from ..deploy import pointer
    from .hosts import system

    host = _host or current()
    installed = tuple(host.trigger_store.list() if installed is None else installed)
    subscriptions = with_utc_offsets(subscriptions, installed)
    rendered = tuple(host.render(item) for item in subscriptions)
    processes = tuple(host.supervisor.owned(role="watcher") if watchers is None else watchers)
    running = handoff.running_watchers(processes) if processes else set()
    watcher_health = {
        item.pid: "busy" if item.pid in running else host.supervisor.watcher_health(item)
        for item in processes}
    keys = [item.key for item in processes if item.key]
    parents = system.process_parent_ids() if len(set(keys)) != len(keys) else {}
    try:
        preferred_generation = pointer.read().generation
    except (pointer.PointerError, OSError):
        preferred_generation = ""
    return Plan(host, tuple(diff(
        rendered,
        installed,
        processes,
        protected_scopes,
        protected_targets,
        protected_process_keys,
        preferred_generation=preferred_generation,
        process_parents=parents,
        watcher_health=watcher_health,
    )), tuple((item.key, item.scope.removeprefix("repo:"),
               item.target.removeprefix("agent:"))
              for item in (*installed, *rendered)),
        tuple((item.key, item.utc_offset) for item in subscriptions
              if item.utc_offset is not None))


def is_clock(subscription: Subscription) -> bool:
    """Whether *subscription* is an agent's clock wake (not boot or maintenance)."""
    return (subscription.kind == "schedule" and subscription.target != "runtime"
            and parse_schedule(subscription.trigger).canonical != "@reboot")


def with_utc_offsets(
    subscriptions: Sequence[Subscription],
    installed: Sequence[InstalledTrigger],
) -> tuple[Subscription, ...]:
    """Give each clock wake the UTC offset its agent already keeps.

    An agent's registered offset is kept, so a daylight saving change or a
    time zone switch never re-registers or moves it; an agent with no
    registered clock wake (new, restarted, or lost) takes the current
    local offset (#609).
    """
    kept: dict[tuple[str, str], str] = {}
    for item in sorted(installed, key=lambda entry: entry.key):
        offset = utc.offset_in(item.rendered) if item.kind == "schedule" else None
        if offset is not None and item.target:
            kept.setdefault((item.scope, item.target), offset)
    current_offset = utc.current_offset()
    return tuple(
        replace(item, utc_offset=kept.get((item.scope, item.target), current_offset))
        if is_clock(item) else item
        for item in subscriptions)


def _prepare(host: HostAdapter) -> Converged | None:
    try:
        prepare = getattr(host, "prepare", None)
        if prepare is not None:
            prepare()
    except Exception as exc:
        operation = Operation(
            "repair-liveness", "runtime-liveness",
            "converge WSL liveness before durable triggers")
        return Converged(
            False, (), ((operation, str(exc)),),
            Health(False, detail=(str(exc),)))
    return None


def commit(prepared: Plan, *, prepare: bool = True) -> Converged:
    """Apply under the caller's gate, rechecking ownership before each spawn."""
    host = prepared.host
    with _lock:
        failure = _prepare(host) if prepare else None
        if failure is not None:
            return failure
        done: list[Operation] = []
        failed: list[tuple[Operation, str]] = []
        running = handoff.running_watchers(tuple(
            operation.process for operation in prepared.operations
            if operation.kind == "stop-watcher" and operation.process is not None))
        deferred: set[str] = set()
        addresses = {key: (repository, agent) for key, repository, agent in prepared.addresses}
        for operation in prepared.operations:
            try:
                if operation.kind == "install-trigger":
                    assert operation.rendered is not None
                    host.trigger_store.install(operation.rendered)
                elif operation.kind == "remove-trigger":
                    host.trigger_store.remove(operation.key)
                elif operation.kind == "start-watcher":
                    assert operation.rendered is not None
                    if operation.key in deferred:
                        done.append(replace(
                            operation, detail="start deferred: watcher has an active run"))
                        continue
                    owners = [
                        item for item in host.supervisor.owned(role="watcher")
                        if item.key == operation.key]
                    if owners:
                        if any(item.fingerprint != operation.rendered.fingerprint
                               for item in owners):
                            raise RuntimeError("replacement deferred: stale watcher is still alive")
                        busy_owners = handoff.running_watchers(owners)
                        if any(host.supervisor.watcher_health(item) not in {"watching", "starting"}
                               for item in owners
                               if item.pid not in busy_owners):
                            raise RuntimeError("replacement deferred: non-watching owner is still alive")
                        done.append(replace(
                            operation, detail="start not needed: watcher is already alive"))
                        continue
                    host.supervisor.spawn_detached(
                        operation.rendered.watcher_argv,
                        role="watcher",
                        key=operation.key,
                        fingerprint=operation.rendered.fingerprint,
                    )
                elif operation.kind == "stop-watcher":
                    assert operation.process is not None
                    if operation.process.pid in running:
                        repository, agent = addresses.get(operation.key, ("", "watcher"))
                        watcher.stop(
                            operation.process, reason=operation.detail,
                            operation=timing.current().operation,
                            repository=repository, agent=agent, status="deferred")
                        deferred.add(operation.key)
                        done.append(replace(
                            operation, detail="stop deferred: watcher has an active run"))
                        continue
                    if operation.detail == "watcher is not watching" and (
                        host.supervisor.watcher_health(operation.process) in {"watching", "starting"}
                    ):
                        done.append(replace(operation, detail="stop not needed: watcher recovered"))
                        continue
                    repository, agent = addresses.get(operation.key, ("", "watcher"))
                    watcher.stop(
                        operation.process, reason=operation.detail,
                        operation=timing.current().operation,
                        repository=repository, agent=agent)
                    host.supervisor.terminate(operation.process)
                    if host.supervisor.alive(operation.process):
                        raise RuntimeError("watcher did not stop")
                else:
                    raise ValueError(f"unknown convergence operation: {operation.kind}")
            except Exception as exc:
                failed.append((operation, str(exc)))
            else:
                done.append(operation)
        return Converged(False, tuple(done), tuple(failed), Health(True),
                         prepared.utc_offsets)


def assess(result: Converged, *, _host: HostAdapter | None = None) -> Converged:
    current_health = _health(_host or current())
    if result.failed and current_health.healthy:
        current_health = Health(
            False,
            current_health.liveness,
            current_health.budget_tripped,
            (*current_health.detail, *result.health.detail,
             "convergence operation failed"),
        )
    return Converged(result.dry_run, result.done, result.failed, current_health,
                     result.utc_offsets)


def converge(
    subscriptions: Sequence[Subscription],
    *,
    dry_run: bool = False,
    protected_scopes: Collection[str] = (),
    protected_targets: Collection[str] = (),
    protected_process_keys: Collection[str] = (),
    _host: HostAdapter | None = None,
) -> Converged:
    host = _host or current()
    with _lock:
        if not dry_run:
            failure = _prepare(host)
            if failure is not None:
                return assess(failure, _host=host)
        prepared = plan(
            subscriptions,
            protected_scopes=protected_scopes,
            protected_targets=protected_targets,
            protected_process_keys=protected_process_keys,
            _host=host,
        )
        if dry_run:
            return Converged(True, prepared.operations, (), _health(host),
                             prepared.utc_offsets)
        return assess(commit(prepared, prepare=False), _host=host)


def _health(host: HostAdapter, failure: str | None = None) -> Health:
    try:
        result = host.health()
    except Exception as exc:
        return Health(False, detail=(failure or str(exc),))
    if failure and result.healthy:
        return Health(
            False,
            result.liveness,
            result.budget_tripped,
            (*result.detail, failure),
        )
    return result
