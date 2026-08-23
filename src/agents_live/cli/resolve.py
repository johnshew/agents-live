"""Cross-repository agent resolution for name-addressed commands (#388).

Registration enrolls a repository in this host's managed set, and a name
should reach the one agent that answers to it wherever it lives. During
6.x that is additive: the existing routing decides first, and the
registered repositories are searched only when it finds nothing. A
command that succeeds today keeps selecting the same definition; a
command that fails today may now succeed, or report a repository-
qualified ambiguity instead of a bare "not found".

The strict combined candidate set - local paths and every registered
repository in one deduplicated ambiguity set - is the target contract
and lands at the major-version boundary, not here.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .. import agent, paths
from ..state import registry as repos


class AmbiguousAgent(agent.DefinitionError):
    """One name, several registered repositories, no safe choice."""


@dataclass(frozen=True)
class Resolution:
    """Where a named agent was found, and what the user should know."""

    root: Path
    spec: agent.AgentSpec
    fallback: bool = False
    warning: str | None = None

    @property
    def identifier(self) -> str:
        return self.spec.identifier


def repository_pinned() -> bool:
    """Whether this invocation named its repository.

    ``--repo`` and a positional project argument both export the
    repository environment variable, as does every persisted invocation
    (schedules, watchers, dispatches). All of them are explicit
    selections, and an explicit selection narrows discovery to that
    repository.
    """
    return bool(os.environ.get(paths.ENV_VAR, "").strip())


def registered_roots(exclude: Path | None = None) -> list[Path]:
    """Registered repository roots, ordered by alias.

    Ordering by alias rather than registry iteration keeps resolution
    independent of the order repositories were added.
    """
    excluded = exclude.resolve() if exclude is not None else None
    roots = []
    try:
        rows = repos.entries()
    except ValueError:
        # An unreadable registry is doctor's finding, not a reason to
        # fail a lookup the resolved repository can still answer.
        return []
    for _alias, value, error in rows:
        if error is not None:
            continue
        root = Path(value).resolve()
        if root != excluded:
            roots.append(root)
    return roots


def _alias_for(root: Path, registry: dict) -> str:
    resolved = str(Path(root).resolve())
    for alias, value in sorted(registry["repos"].items()):
        if value == resolved:
            return alias
    return Path(root).name


def _candidates(name: str, roots: list[Path]) -> list[tuple[Path, agent.AgentSpec]]:
    """Every registered definition matching *name*, deduplicated by path."""
    found: list[tuple[Path, agent.AgentSpec]] = []
    seen: set[Path] = set()
    for root in roots:
        try:
            discovery = agent.discover(root)
        except (agent.DefinitionError, OSError, ValueError):
            # A repository whose configuration or tree cannot be read is
            # reported by doctor; it must not take down a lookup that
            # another repository can answer.
            continue
        for spec in discovery.specs:
            if spec.name != name and spec.identifier != name:
                continue
            resolved = spec.prompt_path.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            found.append((root, spec))
    return found


def qualified_identifiers(
        candidates: list[tuple[Path, str]]) -> list[str]:
    """Repository-qualified identifiers for an ambiguity message."""
    try:
        registry = repos.load()
    except ValueError:
        registry = {"repos": {}, "default_repo": None}
    return [
        f"{_alias_for(root, registry)}/{identifier}"
        for root, identifier in candidates
    ]


def resolve(name: str, *, root: Path, action: str = "run") -> Resolution:
    """Resolve *name* against *root*, then the registered repositories.

    An explicit path is not a name and never reaches the fallback: it is
    loaded where it points. A name that the resolved repository answers
    keeps that answer, with a warning when another registered repository
    would also match it at the major boundary.
    """
    root = Path(root)
    narrowed = repository_pinned() or _is_explicit_path(name)
    try:
        spec = agent.load(name, root=root)
    except agent.DefinitionNotFound as exc:
        if narrowed:
            raise
        return _fallback(name, root=root, action=action, missing=exc)
    if narrowed:
        return Resolution(root, spec)
    here = spec.prompt_path.resolve()
    others = [
        item for item in _candidates(name, registered_roots(exclude=root))
        if item[1].prompt_path.resolve() != here
    ]
    if not others:
        return Resolution(root, spec)
    choices = ", ".join(qualified_identifiers([
        (candidate_root, candidate.identifier)
        for candidate_root, candidate in others
    ]))
    return Resolution(root, spec, warning=(
        f"warning: '{name}' also names a definition in another registered "
        f"repository ({choices}); qualify it with `--repo` before this "
        "becomes ambiguous"))


def _is_explicit_path(name: str) -> bool:
    candidate = Path(name)
    return candidate.is_absolute() or len(candidate.parts) > 1


def _fallback(name: str, *, root: Path, action: str,
              missing: agent.DefinitionNotFound) -> Resolution:
    roots = registered_roots(exclude=root)
    candidates = _candidates(name, roots)
    if len(candidates) == 1:
        selected_root, spec = candidates[0]
        return Resolution(selected_root, spec, fallback=True)
    if len(candidates) > 1:
        choices = "\n".join(
            f"  {item}" for item in qualified_identifiers([
                (candidate_root, candidate.identifier)
                for candidate_root, candidate in candidates
            ]))
        raise AmbiguousAgent(
            f"agent '{name}' is ambiguous across registered repositories\n"
            f"{choices}\n"
            f"use: agents-live --repo <repository> {action} <identifier>")
    searched = ", ".join(str(item) for item in [Path(root), *roots])
    raise agent.DefinitionNotFound(
        f"{missing}; searched {searched}")
