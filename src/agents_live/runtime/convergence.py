"""One idempotent convergence path over the host protocols."""
from __future__ import annotations

from collections.abc import Collection, Sequence
from dataclasses import dataclass
from threading import RLock

from .diff import diff
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
    host = _host or current()
    rendered = tuple(host.render(item) for item in subscriptions)
    return Plan(host, tuple(diff(
        rendered,
        host.trigger_store.list() if installed is None else installed,
        host.supervisor.owned(role="watcher") if watchers is None else watchers,
        protected_scopes,
        protected_targets,
        protected_process_keys,
    )))


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
    """Apply prepared operations without inventory, rendering, diff or health."""
    host = prepared.host
    with _lock:
        failure = _prepare(host) if prepare else None
        if failure is not None:
            return failure
        done: list[Operation] = []
        failed: list[tuple[Operation, str]] = []
        for operation in prepared.operations:
            try:
                if operation.kind == "install-trigger":
                    assert operation.rendered is not None
                    host.trigger_store.install(operation.rendered)
                elif operation.kind == "remove-trigger":
                    host.trigger_store.remove(operation.key)
                elif operation.kind == "start-watcher":
                    assert operation.rendered is not None
                    host.supervisor.spawn_detached(
                        operation.rendered.watcher_argv,
                        role="watcher",
                        key=operation.key,
                        fingerprint=operation.rendered.fingerprint,
                    )
                elif operation.kind == "stop-watcher":
                    assert operation.process is not None
                    host.supervisor.terminate(operation.process)
                else:
                    raise ValueError(f"unknown convergence operation: {operation.kind}")
            except Exception as exc:
                failed.append((operation, str(exc)))
            else:
                done.append(operation)
        return Converged(False, tuple(done), tuple(failed), Health(True))


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
    return Converged(result.dry_run, result.done, result.failed, current_health)


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
            return Converged(True, prepared.operations, (), _health(host))
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
