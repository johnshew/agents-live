"""Fresh processor dependency checks and reactive crash diagnosis."""
from __future__ import annotations

import ast
import re
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path

from ..runtime.spawn import find_uv

_PEP_723 = re.compile(
    r"^# /// script\s*$.*?^# ///\s*$",
    re.DOTALL | re.MULTILINE,
)


@dataclass(frozen=True)
class ProcessorCheck:
    ok: bool
    checked: int
    failures: tuple[str, ...] = ()

    @property
    def detail(self) -> str:
        if self.failures:
            return (
                f"{self.checked} PEP 723 processor(s) checked; "
                + "; ".join(self.failures)
            )
        return f"{self.checked} PEP 723 processor(s) resolved from a fresh cache"


def _expanded_paths(root: Path, pending: list[Path]) -> tuple[Path, ...]:
    found: set[Path] = set()
    visited: set[Path] = set()
    while pending:
        path = pending.pop()
        if path in visited:
            continue
        visited.add(path)
        text = path.read_text(encoding="utf-8")
        if _PEP_723.search(text):
            found.add(path)
        for reference in _script_references(text):
            candidate = Path(reference)
            candidate = candidate.resolve() if candidate.is_absolute() else (
                root / candidate).resolve()
            if candidate.is_relative_to(root) and candidate.is_file():
                pending.append(candidate)
    return tuple(sorted(found))


def _script_references(text: str) -> tuple[str, ...]:
    """Literal paths passed after ``uv run --script`` in Python source."""
    references: set[str] = set()
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return ()
    for node in ast.walk(tree):
        values: list[str] = []
        if isinstance(node, (ast.List, ast.Tuple)):
            values = [
                item.value for item in node.elts
                if isinstance(item, ast.Constant) and isinstance(item.value, str)
            ]
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            try:
                values = shlex.split(node.value)
            except ValueError:
                continue
        for index, value in enumerate(values[:-1]):
            if value == "--script":
                references.add(values[index + 1])
    return tuple(sorted(references))


def diagnose(
    root: Path,
    processor: Path,
    failure: str,
    *,
    command: tuple[str, ...] | None = None,
    timeout: float = 60,
) -> str | None:
    """Explain a processor crash without running the processor again."""
    root = root.resolve()
    processor = processor.resolve()
    if processor.suffix.lower() != ".py" or not processor.is_relative_to(root):
        return None
    try:
        paths = _expanded_paths(root, [processor])
        if not paths:
            return None
        prefix = command or (find_uv(),)
    except (OSError, UnicodeError):
        return None
    result = _check_paths(root, paths, prefix, timeout)
    if not result.ok:
        return f"dependency diagnosis: fresh resolution failed; {result.detail}"
    lowered = failure.casefold()
    if any(marker in lowered for marker in (
        "modulenotfounderror",
        "importerror",
        "cannot import name",
        "no module named",
    )):
        return (
            "dependency diagnosis: fresh resolution succeeded, but the "
            "processor failed while importing; a resolved dependency likely "
            "removed or moved that API. Add a compatible version bound to "
            "the script's PEP 723 dependencies"
        )
    return (
        "dependency diagnosis: fresh resolution succeeded; the processor "
        "failed after dependency setup"
    )


def _check_paths(
    root: Path,
    paths: tuple[Path, ...],
    prefix: tuple[str, ...],
    timeout: float,
) -> ProcessorCheck:
    failures: list[str] = []
    groups: dict[str, list[Path]] = {}
    for path in paths:
        text = path.read_text(encoding="utf-8")
        match = _PEP_723.search(text)
        if match is not None:
            groups.setdefault(match.group(0), []).append(path)
    for grouped in groups.values():
        path = grouped[0]
        relative = path.relative_to(root).as_posix()
        try:
            completed = subprocess.run(
                [
                    *prefix,
                    "lock",
                    "--script",
                    str(path),
                    "--dry-run",
                    "--refresh",
                    "--no-cache",
                ],
                cwd=root,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            failures.append(f"{relative}: {exc}")
            continue
        if completed.returncode:
            message = completed.stderr.strip().splitlines()
            detail = message[-1] if message else f"uv exited {completed.returncode}"
            affected = ", ".join(
                item.relative_to(root).as_posix() for item in grouped)
            failures.append(f"{affected}: {detail}")
    return ProcessorCheck(not failures, len(paths), tuple(failures))


__all__ = ["diagnose"]