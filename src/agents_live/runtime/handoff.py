"""Coordinate runtime launches with installation activation."""
from __future__ import annotations

import json
import os
import time
import uuid
from contextlib import ExitStack, contextmanager
from time import monotonic, sleep

from .. import paths
from .hosts import system
from .hosts.processes import pid_exists


@contextmanager
def gate(*, timeout: float = 0, operation: str = "convergence",
         run_id: str = "", agent: str = "", repository: str = ""):
    started = monotonic()
    deadline = started + timeout
    holder_path = paths.state_home() / "activation-holder.json"
    lock_path = paths.state_home() / "activation.lock"
    observed = {}
    with ExitStack() as acquired:
        while True:
            try:
                acquired.enter_context(system.exclusive_lock(
                    lock_path))
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
                    error.observation = {**observed, "waited_s": monotonic() - started}
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
            yield {**observed, "waited_s": monotonic() - started}
        finally:
            try:
                holder_path.unlink(missing_ok=True)
            except OSError:
                pass


def operation():
    return system.exclusive_lock(paths.state_home() / "activation-operation.lock")


def pause_watchers():
    return system.exclusive_lock(paths.state_home() / "activation-paused.lock")


def paused() -> bool:
    try:
        with system.exclusive_lock(paths.state_home() / "activation-paused.lock"):
            return False
    except system.LockBusy:
        return True