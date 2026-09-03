"""Read the active generation from the stable ``current`` directory link."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from . import layout

ACTIVE = "active"
MISSING = "missing"
INVALID = "invalid"


class PointerError(RuntimeError):
    """The current selection did not name one installed generation."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True)
class Pointer:
    """The generation selected by the stable directory link."""

    generation: str


def read(path: Path | None = None) -> Pointer:
    """Return the active generation, deriving it only from ``current``."""
    current = path or layout.current_path()
    if not current.exists() and not current.is_symlink():
        raise PointerError(
            MISSING, "no current generation is selected by this installation")
    try:
        target = current.resolve(strict=True)
        generations = layout.generations_root(current.parent).resolve(strict=True)
    except OSError as exc:
        raise PointerError(
            INVALID, f"the current generation link could not be resolved: {exc}"
        ) from exc
    if target.parent != generations:
        raise PointerError(
            INVALID, "the current generation link points outside versions")
    try:
        generation = layout.generation_name(target.name)
    except layout.LayoutError as exc:
        raise PointerError(INVALID, str(exc)) from exc
    return Pointer(generation)


def status(path: Path | None = None) -> tuple[Pointer | None, str, str]:
    """Return the current selection and a classified status without raising."""
    try:
        selected = read(path)
    except PointerError as exc:
        return None, exc.reason, str(exc)
    return selected, ACTIVE, f"generation {selected.generation}"
