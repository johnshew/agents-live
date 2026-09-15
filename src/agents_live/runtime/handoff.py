"""Coordinate runtime launches with installation activation."""
from __future__ import annotations

import time
from contextlib import ExitStack, contextmanager

from .. import paths
from .hosts import system


@contextmanager
def gate(*, timeout: float = 0):
    deadline = time.monotonic() + timeout
    with ExitStack() as acquired:
        while True:
            try:
                acquired.enter_context(system.exclusive_lock(
                    paths.state_home() / "activation.lock"))
                break
            except system.LockBusy:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise
                time.sleep(min(0.1, remaining))
        yield


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