"""Coordinate runtime launches with installation activation."""
from __future__ import annotations

import json
import os
import time
import uuid
from collections.abc import Sequence
from contextlib import ExitStack, contextmanager
from time import monotonic, sleep

from .. import paths
from ..obs import timing
from .hosts import system
from .hosts.processes import pid_exists
from .values import ProcessRef


@contextmanager
def gate(*, timeout: float = 0, operation: str = "convergence",
         run_id: str = "", agent: str = "", repository: str = ""):
    started = monotonic()
    deadline = started + timeout
    holder_path = paths.state_home() / "activation-holder.json"
    lock_path = paths.state_home() / "activation.lock"
    observed = {}
    wait_started = time.monotonic()
    with timing.context(operation, run_id=run_id, agent=agent,
                        repository=repository), ExitStack() as acquired:
        while True:
            try:
                held = acquired.enter_context(system.exclusive_lock(
                    lock_path, kind="runtime-launch-gate",
                    wait_started=wait_started))
                break
            except system.LockBusy:
                try:
                    holder = json.loads(holder_path.read_text(encoding="utf-8"))
                    acquisition = json.loads(lock_path.read_text(encoding="utf-8"))
                    valid = (
                        isinstance(holder, dict) and isinstance(acquisition, dict)
                        and isinstance(holder.get("pid"), int) and holder["pid"] > 0
                        and isinstance(holder.get("acquired_at"), (int, float))
                        and 0 < holder["acquired_at"] <= time.time()
                        and bool(holder.get("acquisition"))
                        and all(holder.get(key) == acquisition.get(key)
                                for key in ("pid", "acquired_at", "acquisition"))
                        and pid_exists(holder["pid"])
                    )
                    observed = ({f"holder_{key}": holder.get(key, "") for key in
                                 ("operation", "run_id", "agent", "repository", "pid", "acquired_at")}
                                if valid else {"holder_operation": "unknown"})
                except (OSError, ValueError, TypeError):
                    observed = {"holder_operation": "unknown"}
                remaining = deadline - monotonic()
                if remaining <= 0:
                    error = system.LockBusy("runtime launch gate wait expired")
                    error.observation = {
                        **observed, "waited_s": monotonic() - started,
                        "gate_wait_s": time.monotonic() - wait_started,
                    }
                    raise error
                sleep(min(0.1, remaining))
        holder = {
            "operation": operation, "run_id": run_id, "agent": agent,
            "repository": repository, "pid": os.getpid(),
            "acquired_at": time.time(), "acquisition": uuid.uuid4().hex,
        }
        try:
            with lock_path.open("w", encoding="utf-8") as stream:
                json.dump(holder, stream)
            paths.atomic_write_text(holder_path, json.dumps(holder))
        except OSError:
            pass
        try:
            yield {**observed, "waited_s": monotonic() - started,
                   "gate_wait_s": held.wait_s if held is not None
                   else time.monotonic() - wait_started}
        finally:
            try:
                holder_path.unlink(missing_ok=True)
            except OSError:
                pass


def operation():
    return system.exclusive_lock(paths.state_home() / "activation-operation.lock",
                                 kind="activation-operation")


def running_watchers(watchers: Sequence[ProcessRef]) -> set[int]:
    """Protect watcher trees with active runs, including old-generation owners."""
    candidates = {watcher.pid for watcher in watchers}
    if not candidates:
        return set()
    owners = set()
    try:
        for lock in (paths.state_home() / "repos").glob("*/locks/*.lock"):
            try:
                document = json.loads(lock.read_text(encoding="ascii"))
                pid = int(document["pid"])
            except FileNotFoundError:
                continue
            if pid > 0 and pid_exists(pid):
                owners.add(pid)
    except (OSError, ValueError, TypeError, KeyError):
        # Unknown lock ownership is not permission to kill a watcher tree.
        return candidates
    protected = candidates & owners
    if not owners or protected == candidates:
        return protected
    try:
        parents = system.process_parent_ids()
    except OSError:
        return candidates
    for owner in owners:
        visited = set()
        while owner > 0 and owner not in visited:
            visited.add(owner)
            if owner in candidates:
                protected.add(owner)
            owner = parents.get(owner, 0)
    return protected


def commit_epoch() -> int:
    """Read the cooperating runtime writers' sequence; odd means in progress."""
    try:
        value = int((paths.state_home() / "runtime-commit-epoch").read_text(
            encoding="ascii"))
    except FileNotFoundError:
        return 0
    if value < 0:
        raise ValueError("runtime commit epoch is invalid")
    return value


@contextmanager
def commit():
    """Mark a possibly partial runtime mutation while the caller holds gate()."""
    location = paths.state_home() / "runtime-commit-epoch"
    started = (commit_epoch() // 2 + 1) * 2 - 1
    paths.atomic_write_text(location, f"{started}\n")
    try:
        yield
    finally:
        paths.atomic_write_text(location, f"{started + 1}\n")


@contextmanager
def pause_watchers():
    marker = paths.state_home() / "activation-paused.json"
    with system.exclusive_lock(paths.state_home() / "activation-paused.lock",
                               kind="watcher-pause"):
        birth = system.process_start_token(os.getpid())
        if birth is None:
            raise OSError("cannot identify watcher-pause owner")
        paths.atomic_write_text(marker, json.dumps({"pid": os.getpid(), "birth": birth}))
        try:
            yield
        finally:
            deadline = monotonic() + 2.0
            delay = 0.005
            while True:
                try:
                    marker.unlink(missing_ok=True)
                    break
                except PermissionError:
                    if monotonic() >= deadline:
                        raise
                    sleep(delay)
                    delay = min(delay * 2, 0.05)


def paused() -> bool:
    """Read atomically published intent, without taking a lock on every poll."""
    try:
        holder = json.loads((paths.state_home() / "activation-paused.json").read_text(
            encoding="utf-8"))
    except FileNotFoundError:
        return False
    if (not isinstance(holder, dict) or not isinstance(holder.get("pid"), int)
            or holder["pid"] <= 0 or not isinstance(holder.get("birth"), (int, float))):
        raise ValueError("invalid watcher-pause owner")
    if not system.is_alive(holder["pid"]):
        return False
    birth = system.process_start_token(holder["pid"])
    if birth is None:
        raise OSError("cannot verify watcher-pause owner")
    return birth == holder["birth"]