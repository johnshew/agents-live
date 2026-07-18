#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.12"
# ///
"""Multi-machine agent ownership - public kernel facade.

Mode is declared by the ``ownership`` key in the project config
(root ``.agents-live.toml`` or ``[tool.agents-live]`` in
``pyproject.toml`` - see ``paths.load_config``): ``"registry"`` enables
multi-host ownership; no config or no key means ``"local"`` by
definition (every agent owned by this host, transfers unavailable).
Registry owner values are ``"*"`` (run everywhere) or a short hostname
matching ``hostname -s``.

The REGISTRY IMPLEMENTATION is not part of the public kernel (proposal
§3.9: the public default is local-only). Registry operations dispatch to
a backend resolved in this order:

1. the ``agents_live.ownership`` entry-point group, name ``registry``
   (the private plugin installed alongside the ``agents-live`` package
   via ``uv tool install agents-live --with <plugin>``);
2. flat sibling import of ``ownership_registry`` (this repository's
   pre-flip deployment, where scripts run from the checkout).

Registry mode declared but no backend resolvable = fail closed
(``OwnershipUnavailableError``): a multi-host deployment must abstain,
never silently run everything locally.

Public API (see ``__all__``):

* ``WILDCARD`` - the ``"*"`` value.
* ``OwnershipUnavailableError`` - registry mode declared but the
  registry (or its backend) is missing/malformed; callers must abstain,
  never assume local.
* ``mode()`` / ``local_only()`` - declared mode ("registry" | "local").
* ``registry_available()`` - whether a registry backend is installed
  (gate multi-host bootstrap on this before declaring registry mode).
* ``current_host()`` - ``hostname -s``, lowercased.
* ``load_owners(rate_limit_secs=60)`` - registry mode: the backend's
    pulled, strictly validated ``{agent_name: owner}`` mapping; local
  mode: ``{}`` (nothing is owned elsewhere by definition; no file read,
  no network).
* ``set_owner(name, owner)`` / ``remove_owner(name)`` - registry
  mutations via the backend; raise ``OwnershipUnavailableError`` when
  no backend is installed.
* ``registry_file_exists()`` - bootstrap check for the first
  ``--transfer-to`` (False when no backend is installed).

See ``.claude/skills/agents-live/docs/commands.md`` for the operator
contract.

Counterpart: this module READS the ownership declaration; it never
writes the project config. Config mutations (including
``declare_ownership``) live in ``init.py``, the single sanctioned
mutation point.

Deliberately no dependency on ``headless.py`` so any layer can import it.
"""
from __future__ import annotations

import socket
import subprocess

from . import paths


WILDCARD = "*"

_OWNERSHIP_ENTRY_POINT_GROUP = "agents_live.ownership"
_BACKEND_MODULE = "ownership_registry"

_backend_cache: object | None = None
_backend_resolved = False


class OwnershipUnavailableError(RuntimeError):
    """Registry mode is declared but the registry (or the backend that
    implements it) is missing or malformed.

    Callers must treat this as abstention (skip the run, refuse the
    activation) - NEVER as local ownership. A vanished registry must not
    silently flip a multi-host deployment to run-everything-here."""


def _declared_mode() -> str | None:
    """The optional ``ownership`` key in the project config
    (``paths.load_config`` - root dotfile or pyproject table).

    Absent config or absent key -> None (the project never opted into
    multi-host ownership; local is the definition of that state, not an
    inference). An EXISTING config that is unreadable, or a declaration
    with an unknown value, raises: a declared-registry host must never
    silently downgrade because its config got corrupted."""
    try:
        value = paths.load_config(paths.resolve_root()).get("ownership")
    except ValueError as exc:
        raise OwnershipUnavailableError(
            f"ownership declaration unreadable: {exc}") from exc
    if value is None:
        return None
    if value != "registry":
        # Exactly two states exist: local (no key - the default) and
        # multihost ("registry"). There is no explicit "local" spelling;
        # any other value is malformed and must abstain, not guess.
        raise OwnershipUnavailableError(
            f"ownership declaration invalid: "
            f"{paths.config_source(paths.resolve_root())}: {value!r} "
            f"(the only declared mode is 'registry'; local is the "
            f"absence of the key)")
    return value


def mode() -> str:
    """``"registry"`` or ``"local"``. Registry mode exists ONLY by
    declaration (``ownership = "registry"`` in the project config); an
    undeclared project is local by definition - so zero-init and
    greenfield repos work with no config at all. There is no
    file-presence inference (removed 2026-07-12; it let ambient
    filesystem state pick the security policy)."""
    return _declared_mode() or "local"


def local_only() -> bool:
    """True when this project runs without an ownership registry: every
    agent is owned by the local host and transfer/registry operations are
    unavailable."""
    return mode() == "local"


# ---------------------------------------------------------------------------
# Backend resolution
# ---------------------------------------------------------------------------

def _backend():
    """The registry backend, or None when none is installed.

    Entry point first (installed plugin), then flat sibling import (this
    repo pre-flip). A broken INSTALLED plugin raises - a deployment that
    installed multi-host support must never silently fall back. Only a
    genuinely absent backend resolves to None."""
    global _backend_cache, _backend_resolved
    if _backend_resolved:
        return _backend_cache
    backend = None
    from importlib.metadata import entry_points
    for ep in entry_points(group=_OWNERSHIP_ENTRY_POINT_GROUP):
        if ep.name == "registry":
            backend = ep.load()
            break
    if backend is None:
        try:
            import importlib
            backend = importlib.import_module(_BACKEND_MODULE)
        except ModuleNotFoundError as exc:
            if exc.name != _BACKEND_MODULE:
                raise
            backend = None
    _backend_cache = backend
    _backend_resolved = True
    return backend


def _require_backend():
    backend = _backend()
    if backend is None:
        raise OwnershipUnavailableError(
            "no ownership registry backend installed (multi-host ownership "
            f"is a private plugin exposing the '{_OWNERSHIP_ENTRY_POINT_GROUP}' "
            "entry point; the public kernel is local-only)")
    return backend


def registry_available() -> bool:
    """Whether a registry backend is installed. Gate multi-host bootstrap
    (the first ``--transfer-to``) on this BEFORE declaring registry mode,
    so a kernel-only install can never write a declaration it cannot
    honor."""
    return _backend() is not None


def registry_file_exists() -> bool:
    """Whether the owners document exists on disk (bootstrap check for
    the first --transfer-to; validity is load_owners' job). False when
    no backend is installed."""
    backend = _backend()
    return bool(backend is not None and backend.registry_file_exists())


# ---------------------------------------------------------------------------
# Host identity
# ---------------------------------------------------------------------------

def current_host() -> str:
    """This machine's identifier (``hostname -s``, lowercased)."""
    try:
        out = subprocess.run(
            ["hostname", "-s"], capture_output=True, text=True, check=True, timeout=2,
        ).stdout.strip()
        if out:
            return out.lower()
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return socket.gethostname().split(".", 1)[0].lower()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def load_owners(*, rate_limit_secs: int = 60) -> dict[str, str]:
    """Return the ``{agent_name: owner}`` mapping.

    Registry mode (see :func:`mode`): the backend pulls the registry
    from origin if more than ``rate_limit_secs`` have elapsed since the
    last pull (default 60s, keyword-only; the pull is lock-coordinated
    with git-sync and fail-open), then strictly validates the on-disk
    document. A missing or malformed registry - or a missing backend -
    raises :class:`OwnershipUnavailableError`: enforcement must abstain,
    never assume local ownership. Pass ``rate_limit_secs=10**9`` to skip
    the network entirely (e.g. for read-only status views).

    Local mode: ``{}`` - nothing is owned elsewhere by definition. No
    file is read and no network is touched (an ambient owners file must
    not leak policy into an undeclared project).
    """
    if mode() == "registry":
        return _require_backend().load_owners(rate_limit_secs=rate_limit_secs)
    return {}


def set_owner(name: str, owner: str) -> None:
    """Assign ``name`` to ``owner`` via the registry backend (atomic
    write + git commit + detached background push; no-op if unchanged).
    Raises :class:`OwnershipUnavailableError` when no backend is
    installed."""
    _require_backend().set_owner(name, owner)


def remove_owner(name: str) -> bool:
    """Remove ``name`` from the registry via the backend (atomic delete
    + git commit + detached background push). Returns True if an entry
    was removed. Raises :class:`OwnershipUnavailableError` when no
    backend is installed."""
    return _require_backend().remove_owner(name)


__all__ = [
    "WILDCARD",
    "OwnershipUnavailableError",
    "mode",
    "local_only",
    "registry_available",
    "registry_file_exists",
    "current_host",
    "load_owners",
    "set_owner",
    "remove_owner",
]
