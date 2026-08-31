"""Build complete generations before atomically making one active.

The builder owns filesystem state transitions, while callers provide the
package installer and smoke check. Keeping those effects behind callables lets
the same lifecycle install from PyPI or an exact local wheel without teaching
the deployment layer about a package manager.
"""
from __future__ import annotations

import json
import shutil
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .. import paths
from ..runtime.hosts import system as hostruntime
from . import layout, ownership, pointer

FORMAT = 1


class GenerationError(RuntimeError):
    """A generation could not reach a complete, validated state."""


@dataclass(frozen=True)
class Provenance:
    """The authenticated artifact from which a generation was built."""

    channel: str
    artifact: str
    sha256: str


@dataclass(frozen=True)
class Generation:
    """A complete generation carrying a persisted validation record."""

    name: str
    path: Path
    validated: str
    format: int = FORMAT
    provenance: Provenance | None = None


def _validate_provenance(provenance: Provenance) -> None:
    if (
        not provenance.channel
        or not provenance.artifact
        or len(provenance.sha256) != 64
        or any(character not in "0123456789abcdef"
               for character in provenance.sha256)
    ):
        raise GenerationError("artifact provenance is invalid")


def _discard_staging(path: Path) -> None:
    if path.is_symlink() or (path.exists() and not path.is_dir()):
        path.unlink()
    elif path.exists():
        shutil.rmtree(path)


def _record(generation: Generation) -> str:
    document: dict[str, object] = {
        "format": generation.format,
        "generation": generation.name,
        "validated": generation.validated,
    }
    if generation.provenance is not None:
        document["provenance"] = {
            "channel": generation.provenance.channel,
            "artifact": generation.provenance.artifact,
            "sha256": generation.provenance.sha256,
        }
    return json.dumps(document, indent=2, sort_keys=True) + "\n"


def load(name: str, *, root: Path | None = None) -> Generation:
    """Load a complete, validated generation or refuse incomplete state."""
    generation_name = layout.generation_name(name)
    target = layout.generation_dir(generation_name, root)
    record_path = layout.generation_record_path(generation_name, root)
    if target.is_symlink() or not target.is_dir():
        raise GenerationError(f"generation {generation_name} is not installed")
    try:
        document = json.loads(record_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise GenerationError(
            f"generation {generation_name} has no validation record") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise GenerationError(
            f"generation {generation_name} has an unreadable validation "
            f"record: {exc}") from exc
    if not isinstance(document, dict):
        raise GenerationError(
            f"generation {generation_name} has an invalid validation record")
    version = document.get("format")
    if not isinstance(version, int) or version < 1:
        raise GenerationError(
            f"generation {generation_name} has an invalid validation format")
    if version > FORMAT:
        raise GenerationError(
            f"generation {generation_name} uses validation format {version}; "
            f"this runtime understands format {FORMAT}")
    recorded_name = document.get("generation")
    validated = document.get("validated")
    if recorded_name != generation_name or not isinstance(validated, str) or not validated:
        raise GenerationError(
            f"generation {generation_name} has a mismatched validation record")
    raw_provenance = document.get("provenance")
    provenance = None
    if raw_provenance is not None:
        if not isinstance(raw_provenance, dict):
            raise GenerationError(
                f"generation {generation_name} has invalid artifact provenance")
        channel = raw_provenance.get("channel")
        artifact = raw_provenance.get("artifact")
        sha256 = raw_provenance.get("sha256")
        if not all(isinstance(value, str)
                   for value in (channel, artifact, sha256)):
            raise GenerationError(
                f"generation {generation_name} has invalid artifact provenance")
        provenance = Provenance(channel, artifact, sha256)
        try:
            _validate_provenance(provenance)
        except GenerationError as exc:
            raise GenerationError(
                f"generation {generation_name} has invalid artifact provenance"
            ) from exc
    return Generation(generation_name, target, validated, version, provenance)


def build(
    name: str,
    *,
    populate: Callable[[Path], None],
    validate: Callable[[Path], None],
    root: Path | None = None,
    provenance: Provenance | None = None,
) -> Generation:
    """Populate, validate, and promote an immutable generation.

    A failed populate or validation leaves only the recognizable staging
    directory. The next attempt discards that directory before starting.
    Neither path reads or writes the active pointer.
    """
    generation_name = layout.generation_name(name)
    if provenance is not None:
        _validate_provenance(provenance)
    install_root = root or layout.installation_root()
    staging = layout.staging_dir(generation_name, install_root)
    target = layout.generation_dir(generation_name, install_root)
    install_root.mkdir(parents=True, exist_ok=True)
    try:
        with hostruntime.exclusive_lock(
                layout.deployment_lock_path(install_root), blocking=False):
            if target.exists():
                raise GenerationError(
                    f"generation {generation_name} is already installed and "
                    "will not be rewritten")
            _discard_staging(staging)
            try:
                populate(staging)
            except Exception as exc:
                raise GenerationError(
                    f"could not stage generation {generation_name}: {exc}") from exc
            if not staging.is_dir() or staging.is_symlink():
                raise GenerationError(
                    f"staging generation {generation_name} did not create a "
                    "generation directory")
            try:
                validate(staging)
            except Exception as exc:
                raise GenerationError(
                    f"generation {generation_name} failed validation: {exc}") from exc
            generation = Generation(
                generation_name,
                target,
                datetime.now(timezone.utc).isoformat(timespec="seconds"),
                provenance=provenance,
            )
            paths.atomic_write_text(
                staging / layout.GENERATION_RECORD, _record(generation))
            try:
                paths.replace_when_windows_lets_go(staging, target)
            except OSError as exc:
                raise GenerationError(
                    f"could not complete generation {generation_name}: {exc}") from exc
            return generation
    except hostruntime.LockBusy as exc:
        raise GenerationError(
            "another generation operation owns the installation lock") from exc


def adopt(
    name: str,
    *,
    validate: Callable[[Path], None],
    root: Path | None = None,
    provenance: Provenance | None = None,
) -> Generation:
    """Validate and seal a bootstrap-created dedicated environment.

    The bootstrap promotes authenticated bytes before invoking the generated
    command at its final absolute path. That command is the only process
    allowed to turn the directory into a complete immutable generation.
    """
    generation_name = layout.generation_name(name)
    if provenance is not None:
        _validate_provenance(provenance)
    install_root = root or layout.installation_root()
    target = layout.generation_dir(generation_name, install_root)
    if layout.generation_of(Path(sys.executable), install_root) != generation_name:
        raise GenerationError(
            f"generation {generation_name} can only be finalized by its own "
            "dedicated runtime")
    try:
        with hostruntime.exclusive_lock(
                layout.deployment_lock_path(install_root), blocking=False):
            if not target.is_dir() or target.is_symlink():
                raise GenerationError(
                    f"generation {generation_name} is not installed")
            if (target / layout.GENERATION_RECORD).exists():
                return load(generation_name, root=install_root)
            try:
                validate(target)
            except Exception as exc:
                raise GenerationError(
                    f"generation {generation_name} failed validation: {exc}") from exc
            generation = Generation(
                generation_name,
                target,
                datetime.now(timezone.utc).isoformat(timespec="seconds"),
                provenance=provenance,
            )
            paths.atomic_write_text(
                target / layout.GENERATION_RECORD, _record(generation))
            return generation
    except hostruntime.LockBusy as exc:
        raise GenerationError(
            "another generation operation owns the installation lock") from exc


def activate(generation: Generation, *, root: Path | None = None) -> pointer.Pointer:
    """Select a validated generation through the stable ``current`` path."""
    install_root = root or layout.installation_root()
    try:
        with hostruntime.exclusive_lock(
                layout.deployment_lock_path(install_root), blocking=False):
            installed = load(generation.name, root=install_root)
            if installed != generation:
                raise GenerationError(
                    f"generation {generation.name} changed after validation")
            previous, state, detail = pointer.status(
                layout.current_path(install_root))
            if state not in (pointer.ACTIVE, pointer.MISSING):
                raise GenerationError(
                    f"activation refused because {detail}")
            try:
                _replace_current(installed.path, root=install_root)
            except OSError as exc:
                if previous is not None:
                    _replace_current(
                        layout.generation_dir(previous.generation, install_root),
                        root=install_root,
                    )
                raise GenerationError(
                    f"could not activate generation {generation.name}: {exc}"
                ) from exc
            return pointer.read(layout.current_path(install_root))
    except hostruntime.LockBusy as exc:
        raise GenerationError(
            "another generation operation owns the installation lock") from exc


def _replace_current(target: Path, *, root: Path) -> None:
    """Point ``current`` at *target* using the host's directory-link primitive."""
    hostruntime.replace_directory_link(
        layout.current_path(root), target, root=root)


def clear_activation(*, root: Path | None = None) -> None:
    """Remove the stable selection and its metadata without touching versions."""
    install_root = root or layout.installation_root()
    hostruntime.remove_directory_link(layout.current_path(install_root))
