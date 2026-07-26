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
Registry owner values are ``"*"`` (run everywhere) or a
``hostname/runtime/uuid`` identity. The hostname and runtime are what a
table shows; the uuid is what a match reads. An owner value that yields
no uuid belongs to nobody here, which is how an incomplete or corrupted
entry stays safe without a repair path (see :func:`owns`).

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
* ``current_host()`` - ``hostname -s``, lowercased; the first part of
  this runtime's identity.
* ``current_owner_id()`` - this runtime's identity,
  ``hostname/runtime/uuid`` (see ``docs/windows-support.md``, Ownership
  generalization).
* ``owns(value)`` - whether an EXISTING owner value pins an agent here;
  an unmatchable value is never ours.
* ``owner_uuid(value)`` / ``display_owner(value)`` - the matching part
  and the readable part of an owner value.
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

import re
import socket
import subprocess
import uuid

from . import adminlog, hostruntime, paths


WILDCARD = "*"

# Separates the three parts of an ownership identity,
# ``hostname/runtime/uuid``.
SEPARATOR = "/"

# Where a runtime keeps the UUID it generated for itself, under the user
# state home.
RUNTIME_ID_FILE = "runtime-id"

_RUNTIME_UUID_RE = re.compile(r"[0-9a-f]{32}")

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
    """This machine's name (``hostname -s``, lowercased).

    The first part of this runtime's identity, and one of the two parts
    a table shows. It is not what an owner value is matched against:
    that is the uuid part, because a hostname does not distinguish the
    WSL distros on one machine, which default to sharing it.
    """
    try:
        out = subprocess.run(
            ["hostname", "-s"], capture_output=True, text=True, check=True, timeout=2,
        ).stdout.strip()
        if out:
            return out.lower()
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return socket.gethostname().split(".", 1)[0].lower()


def display_owner(value: str) -> str:
    """An owner value as a person reads it: ``hostname/runtime``.

    The uuid part is never shown. It answers "is this mine", which no
    reader can evaluate by eye, and showing it would spend the width of
    a 32-character hex string on the one part of the identity that
    carries no meaning for a human. A value with no runtime part still
    renders its separator (``hostname/``), so an incomplete entry reads
    as incomplete rather than as a machine named after the whole string.
    """
    if value == WILDCARD:
        return value
    hostname, _, rest = value.partition(SEPARATOR)
    runtime = rest.partition(SEPARATOR)[0]
    return f"{hostname}{SEPARATOR}{runtime}"


def owner_uuid(value: str) -> str:
    """The uuid part of an owner value, or ``""`` when it has none.

    Anything that is not a well-formed triple ending in a 32-character
    hex uuid returns ``""``: a bare hostname, a truncated write, a
    hand-edit, a badly merged value. None of those are a shape to
    repair - see :func:`owns`.
    """
    parts = value.split(SEPARATOR)
    if len(parts) != 3:
        return ""
    candidate = parts[2].strip().lower()
    return candidate if _RUNTIME_UUID_RE.fullmatch(candidate) else ""


def owns(value: str) -> bool:
    """Whether this runtime owns an agent pinned to ``value``.

    Matching reads the uuid part and nothing else, so two WSL distros on
    one machine - which default to the same hostname - are separate
    owners, and a renamed machine keeps its agents.

    An owner value that yields no uuid is NOT ours. That single rule is
    what makes an incomplete, hand-edited, truncated, or corrupted entry
    safe: it reads exactly like an agent owned by another machine, so
    this host neither runs it nor prunes its registry entry, and an
    operator resolves it by claiming the agent (``--transfer-here``).
    There is no repair path to maintain because nothing is repaired.

    Note that this is about a value that EXISTS. An agent absent from
    the registry is unclaimed, which is a different state that callers
    handle themselves; conflating the two would stop every agent in a
    local-mode project, where nothing is registered by definition.
    """
    if value == WILDCARD:
        return True
    identity = owner_uuid(value)
    return bool(identity) and identity == _runtime_uuid()


def current_label() -> str:
    """This runtime's readable label, ``hostname/runtime``.

    The display half of :func:`current_owner_id`, built without reading
    the identity file, so a dashboard header or a log line never fails
    on an unreadable uuid. Never use it to decide ownership: two WSL
    distros on one machine can produce the same label only if one is
    renamed to the other, but a label was never the thing that made an
    identity exact.
    """
    return (f"{current_host().replace(SEPARATOR, '-')}{SEPARATOR}"
            f"{hostruntime.runtime_name().replace(SEPARATOR, '-')}")


def current_owner_id() -> str:
    """This runtime's ownership identity, ``hostname/runtime/uuid``.

    Each part has exactly one job: the hostname and the runtime are what
    a table shows (:func:`display_owner`), and the uuid is what a match
    reads (:func:`owns`). Splitting them that way is what lets the
    identity be both readable and exact, which no single value managed -
    a hostname is not unique across the WSL distros on one machine, and
    a uuid on its own names nothing a person recognises.

    The uuid is generated once into this user's state home and is stable
    from then on across repository moves, machine renames, and package
    upgrades.

    Raises :class:`OwnershipUnavailableError` when an identity file
    exists but does not hold a uuid. An identity that cannot be read is
    abstention, exactly like an unreadable registry: a runtime that
    cannot say who it is cannot claim an agent.
    """
    return f"{current_label()}{SEPARATOR}{_runtime_uuid()}"


def _runtime_uuid() -> str:
    """Read this runtime's installation UUID, generating it once.

    The file is created exclusively, so two commands racing on first use
    settle on whichever one wins rather than on two identities.
    """
    path = paths.state_home() / RUNTIME_ID_FILE
    try:
        existing = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        existing = ""
    except OSError as exc:
        raise OwnershipUnavailableError(
            f"runtime identity unreadable: {path}: {exc}") from exc
    if existing:
        if not _RUNTIME_UUID_RE.fullmatch(existing):
            raise OwnershipUnavailableError(
                f"runtime identity malformed: {path}: {existing!r} "
                f"(expected a 32-character hex UUID; delete the file to "
                f"generate a new identity, which unclaims agents owned by "
                f"the old one)")
        return existing
    generated = uuid.uuid4().hex
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("x", encoding="utf-8") as handle:
            handle.write(generated)
    except FileExistsError:
        return _runtime_uuid()
    except OSError as exc:
        raise OwnershipUnavailableError(
            f"runtime identity not writable: {path}: {exc}") from exc
    return generated


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
    backend = _require_backend()
    previous = _recorded_owner(name)
    backend.set_owner(name, owner)
    if previous == owner:
        return
    # Transfers move execution between hosts and are the mechanism behind
    # duplicate or silently stopped agents, so who moved what, when, and
    # from where has to survive the command that did it.
    adminlog.record(
        "ownership-set", agent=name, owner_from=previous, owner_to=owner,
        claimed=_safely(lambda: owns(owner)),
        runtime=_safely(current_label))


def remove_owner(name: str) -> bool:
    """Remove ``name`` from the registry via the backend (atomic delete
    + git commit + detached background push). Returns True if an entry
    was removed. Raises :class:`OwnershipUnavailableError` when no
    backend is installed."""
    previous = _recorded_owner(name)
    removed = _require_backend().remove_owner(name)
    if removed:
        adminlog.record("ownership-remove", agent=name, owner_from=previous,
                        runtime=_safely(current_label))
    return removed


def _safely(read):
    """A field for the audit record, or None when it cannot be read.

    The write has already happened by the time these are gathered, so an
    unreadable identity must cost the record a field, never turn a
    completed transfer into a raised error.
    """
    try:
        return read()
    except Exception:
        return None


def _recorded_owner(name: str) -> str | None:
    """The owner currently recorded for *name*, for the log's ``from``.

    Read without a pull: the caller is about to write, so the local
    document is the state it is writing over, and an audit field must
    never add a network round trip to a mutation. Nor may it stop one:
    a read that fails costs the record a field, not the write.
    """
    return _safely(lambda: load_owners(rate_limit_secs=10**9).get(name))


__all__ = [
    "WILDCARD",
    "SEPARATOR",
    "OwnershipUnavailableError",
    "mode",
    "local_only",
    "registry_available",
    "registry_file_exists",
    "current_host",
    "current_label",
    "current_owner_id",
    "display_owner",
    "owner_uuid",
    "owns",
    "load_owners",
    "set_owner",
    "remove_owner",
]
