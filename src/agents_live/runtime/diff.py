"""Pure desired-versus-actual runtime diff."""
from __future__ import annotations

from collections.abc import Collection, Sequence

from .values import InstalledTrigger, Operation, ProcessRef, RenderedSubscription


def diff(
    desired: Sequence[RenderedSubscription],
    actual: Sequence[InstalledTrigger],
    processes: Sequence[ProcessRef] = (),
    protected_scopes: Collection[str] = (),
) -> tuple[Operation, ...]:
    """Operations that make the host match ``desired``.

    ``protected_scopes`` names scopes whose desired set could not be
    computed. Their artifacts are left alone rather than removed, because
    absent input is not an instruction to delete.
    """
    wanted = {item.key: item for item in desired}
    installed = {item.key: item for item in actual}
    watchers = {item.key: item for item in processes if item.role == "watcher" and item.key}
    protected_keys = {
        key for key, item in installed.items() if item.scope in protected_scopes}
    operations: list[Operation] = []

    for key in sorted(installed.keys() - wanted.keys() - protected_keys):
        operations.append(Operation("remove-trigger", key, "trigger is not desired"))
    for key in sorted(wanted):
        target = wanted[key]
        current = installed.get(key)
        if current is None or current.fingerprint != target.fingerprint:
            if current is not None:
                operations.append(Operation("remove-trigger", key, "trigger fingerprint changed"))
            operations.append(Operation("install-trigger", key, "trigger is missing", rendered=target))
        if target.kind != "watch":
            continue
        process = watchers.get(key)
        if process is not None and process.fingerprint != target.fingerprint:
            operations.append(Operation(
                "stop-watcher", key, "watch expression changed", process=process))
            process = None
        if process is None:
            operations.append(Operation("start-watcher", key, "watcher is not alive", rendered=target))

    for key in sorted(watchers.keys() - wanted.keys() - protected_keys):
        operations.append(Operation(
            "stop-watcher", key, "watcher is not desired", process=watchers[key]))
    return tuple(operations)
