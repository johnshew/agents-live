"""Where a self-managed installation keeps its generations.

The layout is the one #334 describes:

    <installation root>/
        versions/<generation>/   a complete environment, never rewritten
        current/                 link to the active generation
        owner.json               which channel owns this installation

Two properties decide whether the model works, and both live here.

The pointer is a directory link, not an executable. Flipping it never
rewrites a running image, so Windows has nothing to lock (#231).

A generation directory is named, never derived from unvalidated input. A
name that could climb out of ``versions/`` would let a build write
anywhere the installing user can, so names are validated before they
reach the filesystem rather than after.

A directory under ``versions/`` is not a generation until it holds a
validation record. The record is written last, so an interrupted build
leaves a directory that every listing here ignores.

Nothing in this module creates or mutates a directory: it computes paths
and refuses bad names.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

from ..runtime.hosts import system as hostruntime

#: Points the whole installation tree somewhere else. Tests use it; an
#: operator with a machine that keeps applications off the system drive
#: is the reason it is not test-only.
ENV_INSTALL_ROOT = "AGENTS_LIVE_INSTALL_ROOT"

GENERATIONS = "versions"
CURRENT = "current"
OWNERSHIP = "owner.json"
GENERATION_RECORD = "generation.json"
DEPLOYMENT_LOCK = ".deployment.lock"

# A generation name reaches the filesystem, so it is validated against
# what a version may contain rather than against what a path may not.
# PEP 440 versions, Git describes, and local build tags all fit.
_GENERATION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]*\Z")


class LayoutError(ValueError):
    """A name that may not become part of the installation root."""


def installation_root() -> Path:
    """The root a self-managed installation owns, entirely.

    ``%LOCALAPPDATA%\\agents-live`` on Windows and
    ``$XDG_DATA_HOME/agents-live`` on POSIX, unless
    :data:`ENV_INSTALL_ROOT` names somewhere else. The directory is
    machine-local on purpose: it holds executables built for this
    machine, which must not follow a roaming profile to another.
    """
    explicit = os.environ.get(ENV_INSTALL_ROOT, "").strip()
    if explicit:
        return Path(explicit).expanduser()
    return hostruntime.user_data_base() / "agents-live"


def generations_root(root: Path | None = None) -> Path:
    """Where every installed generation lives, active or not."""
    return (root or installation_root()) / GENERATIONS


def generation_name(version: str) -> str:
    """*version* as a directory name, or a refusal.

    Raises :class:`LayoutError` for anything that is not a plain name:
    empty or blank, a separator, a drive, ``.``/``..``, a leading ``~``,
    or a character outside the version alphabet. The check runs before
    the name is joined to a path, because a name that escapes is only
    observable afterwards, as a write outside the installation root.
    """
    candidate = (version or "").strip()
    if not candidate:
        raise LayoutError("a generation needs a name")
    if not _GENERATION.fullmatch(candidate):
        raise LayoutError(
            f"'{version}' is not a usable generation name; a generation is "
            "named with letters, digits, and '.', '_', '+', or '-'")
    return candidate


def generation_dir(version: str, root: Path | None = None) -> Path:
    """The directory a complete generation occupies."""
    return generations_root(root) / generation_name(version)


def current_path(root: Path | None = None) -> Path:
    """The stable directory link through which commands enter the runtime."""
    return (root or installation_root()) / CURRENT


def command_root(root: Path | None = None) -> Path:
    """The stable directory placed on PATH for generated console scripts."""
    return hostruntime.executable_dir(current_path(root))


def public_command_root(root: Path | None = None) -> Path:
    """The user-facing command directory persisted on PATH."""
    if hostruntime.id() == hostruntime.WINDOWS:
        return command_root(root)
    return Path.home() / ".local" / "bin"


def ownership_path(root: Path | None = None) -> Path:
    """Where this installation records which channel owns it.

    Beside the selection, not in machine-local state: an installation that
    is deleted, copied, or restored takes the answer with it, and a
    command can read both facts from the tree it is about to change.
    """
    return (root or installation_root()) / OWNERSHIP


def deployment_lock_path(root: Path | None = None) -> Path:
    """The inter-process lock shared by builders, activators, and collectors."""
    return (root or installation_root()) / DEPLOYMENT_LOCK


def generation_record_path(version: str, root: Path | None = None) -> Path:
    """The validation record inside a complete generation."""
    return generation_dir(version, root) / GENERATION_RECORD


def command_path(name: str = "agents-live", root: Path | None = None) -> Path:
    """The stable path to a generated console entry point."""
    if name not in ("agents-live", "al"):
        raise LayoutError(
            f"'{name}' is not an Agents Live command; expected agents-live or al")
    return command_root(root) / hostruntime.executable_filename(name)


def is_sealed(path: Path) -> bool:
    """Whether *path* holds a generation's validation record.

    Presence only. Whether the record is well formed is
    :func:`generation.load`'s question; this answers the cheaper one that
    every listing and every rebuild decision needs first.
    """
    try:
        return (path / GENERATION_RECORD).is_file()
    except OSError:
        return False


def installed_generations(root: Path | None = None) -> tuple[str, ...]:
    """Complete generations on disk, sorted, unsealed directories excluded.

    An unreadable or absent ``versions/`` answers with nothing rather
    than raising: a host that has never used this model is the normal
    case, not a fault.
    """
    try:
        entries = sorted(
            entry.name for entry in generations_root(root).iterdir()
            if entry.is_dir() and is_sealed(entry))
    except OSError:
        return ()
    return tuple(entries)


def generation_of(path: Path | str, root: Path | None = None) -> str | None:
    """The generation *path* runs from, or ``None`` if it is outside one.

    This is how a process answers what it is executing without asking a
    package manager: the answer is the directory the running image sits
    in, which stays true for the life of the process even after the
    pointer moves on.
    """
    try:
        supplied = Path(path)
        candidate = supplied.parent.resolve() / supplied.name
        generations = generations_root(root).resolve()
    except OSError:
        return None
    for parent in (candidate, *candidate.parents):
        if parent.parent == generations:
            return parent.name
    return None
