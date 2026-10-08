"""Pure desired-versus-actual runtime diff."""
from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence

from .values import InstalledTrigger, Operation, ProcessRef, RenderedSubscription


def diff(
    desired: Sequence[RenderedSubscription],
    actual: Sequence[InstalledTrigger],
    processes: Sequence[ProcessRef] = (),
    protected_scopes: Collection[str] = (),
    protected_targets: Collection[str] = (),
    protected_process_keys: Collection[str] = (),
    *,
    preferred_generation: str = "",
    process_parents: Mapping[int, int] | None = None,
) -> tuple[Operation, ...]:
    """Operations that make the host match ``desired``.

    ``protected_scopes`` and ``protected_targets`` name what could not be
    computed: a repository that will not resolve, and a started definition
    that will not parse. Their artifacts are left alone rather than removed,
    because absent input is not an instruction to delete.
    """
    wanted = {item.key: item for item in desired}
    installed = {item.key: item for item in actual}
    candidates = {
        item.pid: item for item in processes if item.role == "watcher" and item.key
    }
    parents = process_parents or {}
    trees: dict[int, tuple[int, ProcessRef]] = {}
    # A console launcher and its Python child represent one watch loop.
    for process in candidates.values():
        root = process.pid
        depth = 0
        distance = 0
        parent = parents.get(process.pid, 0)
        visited = {process.pid}
        while parent > 0 and parent not in visited:
            visited.add(parent)
            distance += 1
            if parent in candidates and candidates[parent].key == process.key:
                root = parent
                depth = distance
            parent = parents.get(parent, 0)
        if root not in trees or depth > trees[root][0]:
            # Interpreter children identify the generation; launchers may use current.
            trees[root] = (depth, process)
    watchers: dict[str, list[ProcessRef]] = {}
    for pid in trees:
        process = candidates[pid]
        watchers.setdefault(process.key, []).append(process)
    protected_keys = set(protected_process_keys) | {
        key for key, item in installed.items()
        if item.scope in protected_scopes
        or (item.target and item.target in protected_targets)
    }
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
        owners = watchers.get(key, [])
        process = min(owners, key=lambda item: (
            not (preferred_generation
                 and trees[item.pid][1].generation == preferred_generation),
            trees[item.pid][1].fingerprint != target.fingerprint,
            trees[item.pid][1].created_at, item.pid,
        ), default=None)
        for extra in owners:
            if extra != process:
                operations.append(Operation(
                    "stop-watcher", key, "duplicate watcher", process=extra))
        if process is not None and process.fingerprint != target.fingerprint:
            operations.append(Operation(
                "stop-watcher", key, "watch expression changed", process=process))
            process = None
        if process is None:
            operations.append(Operation("start-watcher", key, "watcher is not alive", rendered=target))

    for key in sorted(watchers.keys() - wanted.keys() - protected_keys):
        operations.extend(Operation(
            "stop-watcher", key, "watcher is not desired", process=process)
            for process in watchers[key])
    return tuple(operations)
