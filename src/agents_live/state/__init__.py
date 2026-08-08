"""Machine-local desired state.

Repository registration answers where to look. Started state answers which
definitions should be automated here. Runtime artifacts never live here.
"""
from .started import (
    StartedSnapshot,
    StartedStateUnavailable,
    clear,
    is_started,
    load,
    load_or_adopt,
    record,
    replace,
)
from .. import paths as _paths

ENV_VAR = _paths.ENV_VAR


def resolve_root(value: str | None = None, *, allow_sole_registered: bool = False):
    return _paths.resolve_root(value, allow_sole_registered=allow_sole_registered)


def clear_root_cache() -> None:
    _paths.clear_cache()

__all__ = [
    "StartedSnapshot",
    "StartedStateUnavailable",
    "ENV_VAR",
    "clear",
    "clear_root_cache",
    "is_started",
    "load",
    "load_or_adopt",
    "record",
    "replace",
    "resolve_root",
]
