"""One idempotent convergence path over the host protocols."""
from __future__ import annotations

from collections.abc import Sequence
from threading import RLock

from .diff import diff
from .protocols import HostAdapter
from .values import Converged, Health, Operation, Subscription

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


def converge(
    subscriptions: Sequence[Subscription],
    *,
    dry_run: bool = False,
    _host: HostAdapter | None = None,
) -> Converged:
    host = _host or current()
    with _lock:
        if not dry_run:
            try:
                prepare = getattr(host, "prepare", None)
                if prepare is not None:
                    prepare()
            except Exception as exc:
                operation = Operation(
                    "repair-liveness", "runtime-liveness",
                    "converge WSL liveness before durable triggers")
                return Converged(
                    False,
                    (),
                    ((operation, str(exc)),),
                    _health(host, str(exc)),
                )
        rendered = tuple(host.render(item) for item in subscriptions)
        operations = diff(
            rendered,
            host.trigger_store.list(),
            host.supervisor.owned(role="watcher"),
        )
        if dry_run:
            return Converged(True, operations, (), _health(host))

        done: list[Operation] = []
        failed: list[tuple[Operation, str]] = []
        for operation in operations:
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
        current_health = _health(host)
        if failed and current_health.healthy:
            current_health = Health(
                False,
                current_health.liveness,
                current_health.budget_tripped,
                (*current_health.detail, "convergence operation failed"),
            )
        return Converged(False, tuple(done), tuple(failed), current_health)


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
