"""Coordinate runtime launches with installation activation."""
from __future__ import annotations

from .. import paths
from .hosts import system


def gate():
    return system.exclusive_lock(paths.state_home() / "activation.lock")


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