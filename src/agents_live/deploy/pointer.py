"""The generation pointer: the one file an activation writes.

Activation is a single write of ``current.json``. That is the whole
reason the side-by-side model works on Windows: no running image is
replaced, so nothing can be locked part way through (#231). It also
means the pointer is the only source of truth about what is active, and
a reader that cannot understand it must say so rather than guess.

Every failure this file can present is classified, because #369 asks for
the failure semantics rather than a best effort:

======================  ==================================================
reason                  what it means, and what a caller must do
======================  ==================================================
``missing``             no self-managed installation is active here; the
                        caller falls back to how it is installed today
``unreadable``          the file exists but the host would not read it;
                        report the error, change nothing
``malformed``           not JSON, or not a pointer document; refuse and
                        repair, never guess a generation
``unsupported``         written by a newer runtime than this launcher
                        understands; refuse and say the launcher is old
======================  ==================================================

A refusal never falls back to "the newest directory in ``versions/``".
Guessing is how an installation ends up running a generation nobody
activated, which is the failure this model exists to remove.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .. import paths
from . import layout

#: The pointer document version. A launcher refuses a document numbered
#: above the one it was built for, so a format change is reported as an
#: old launcher rather than silently misread.
FORMAT = 1

ACTIVE = "active"
MISSING = "missing"
UNREADABLE = "unreadable"
MALFORMED = "malformed"
UNSUPPORTED = "unsupported"


class PointerError(RuntimeError):
    """The pointer did not answer. ``reason`` says which way it failed."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True)
class Pointer:
    """The active generation, and who put it there."""

    generation: str
    owner: str
    updated: str
    format: int = FORMAT

    def document(self) -> str:
        """The canonical text of this pointer."""
        return json.dumps(
            {
                "format": self.format,
                "generation": self.generation,
                "owner": self.owner,
                "updated": self.updated,
            },
            indent=2,
            sort_keys=True,
        ) + "\n"


def parse(text: str) -> Pointer:
    """A pointer from its document text, or a classified refusal."""
    try:
        document = json.loads(text)
    except json.JSONDecodeError as exc:
        raise PointerError(
            MALFORMED,
            f"the generation pointer is not readable JSON: {exc}") from exc
    if not isinstance(document, dict):
        raise PointerError(
            MALFORMED, "the generation pointer is not a pointer document")
    version = document.get("format")
    if not isinstance(version, int):
        raise PointerError(
            MALFORMED, "the generation pointer declares no format version")
    if version < 1:
        raise PointerError(
            MALFORMED,
            f"the generation pointer declares invalid format {version}")
    if version > FORMAT:
        raise PointerError(
            UNSUPPORTED,
            f"the generation pointer is format {version} and this runtime "
            f"understands format {FORMAT}; the launcher predates the "
            "installation it points at and has to be replaced")
    generation = document.get("generation")
    if not isinstance(generation, str) or not generation.strip():
        raise PointerError(
            MALFORMED, "the generation pointer names no generation")
    try:
        generation = layout.generation_name(generation)
    except layout.LayoutError as exc:
        raise PointerError(MALFORMED, str(exc)) from exc
    owner = document.get("owner")
    updated = document.get("updated")
    return Pointer(
        generation=generation,
        owner=owner if isinstance(owner, str) and owner else "unknown",
        updated=updated if isinstance(updated, str) else "",
        format=version,
    )


def read(path: Path | None = None) -> Pointer:
    """The active generation, or a :class:`PointerError` saying why not."""
    target = path or layout.pointer_path()
    try:
        text = target.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise PointerError(
            MISSING, "no generation pointer: this installation does not use "
            "the generation layout") from exc
    except OSError as exc:
        raise PointerError(
            UNREADABLE, f"the generation pointer could not be read: {exc}"
        ) from exc
    return parse(text)


def status(path: Path | None = None) -> tuple[Pointer | None, str, str]:
    """``(pointer, state, detail)`` without raising.

    The reporting form, for callers such as ``doctor`` that have to
    describe an installation they are not changing.
    """
    try:
        pointer = read(path)
    except PointerError as exc:
        return None, exc.reason, str(exc)
    return pointer, ACTIVE, f"generation {pointer.generation}"


def write(generation: str, *, owner: str, path: Path | None = None,
          updated: str | None = None) -> Pointer:
    """Make *generation* active, atomically.

    The write is temp-then-rename, so a reader observes either the old
    pointer or the new one and never a partial file. That is what makes
    activation a single reversible step: writing the previous
    generation's name back is a complete rollback.

    Writing the pointer does not verify that the generation is complete;
    staging and validation are separate steps by design (#369), and a
    caller that skips them is the bug this refuses to hide.
    """
    pointer = Pointer(
        generation=layout.generation_name(generation),
        owner=owner,
        updated=updated or datetime.now(timezone.utc).isoformat(
            timespec="seconds"),
    )
    paths.atomic_write_text(path or layout.pointer_path(), pointer.document())
    return pointer
