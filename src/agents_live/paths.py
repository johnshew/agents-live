"""Repository-root, config-home, and state-location resolution.

Single source of truth for "which repo/project am I operating on".
Resolution order:

1. Explicit argument (the ``agents-live --repo`` flag).
2. ``AGENTS_LIVE_REPO`` environment variable.
3. Walk up from CWD to the nearest directory containing a marker:
   ``.agents-live.toml``, or ``pyproject.toml`` with a
   ``[tool.agents-live]`` table.
4. The default repository in the user-level XDG registry.

The optional user-configured default is the only fallback: a script-location
anchor would resolve inside the installed package instead of the user's
project. With no explicit root, env var, marker, or default, resolution fails
loudly. All persisted
invocations (cron lines, watcher respawns, dispatches) pin CWD to the
repo, so the marker walk always succeeds for scheduled work. One
first-use exception lives at the CLI layer, not here: interactive
``run``/``start`` inside a markerless git repository auto-create the
minimal local-mode marker at the git root (``cli.AUTO_MARKER``), after
which resolution succeeds by the normal walk.

The markers ARE the config home (§3.2 decision, 2026-07-12): project
config lives at the repo root in ``.agents-live.toml``
(authoritative when both exist) or the ``[tool.agents-live]`` table
of ``pyproject.toml``. :func:`load_config` reads it; ``init`` writes it.
``Agents/data/`` holds runtime state only and is no longer a marker or a
config home. (Names renamed from triggered-tasks 2026-07-12, R1a of the
convergence plan - clean break, no legacy names read.)

stdlib-only on purpose: every sibling script (headless, ownership, qlog,
timeline, prereqs) imports this module flat from the same directory.
"""
from __future__ import annotations

import os
import tomllib
from pathlib import Path

ENV_VAR = "AGENTS_LIVE_REPO"
CONFIG_DOTFILE = ".agents-live.toml"
PYPROJECT = "pyproject.toml"
PYPROJECT_TABLE = "agents-live"  # [tool.agents-live]
# Human-readable marker descriptions for error messages; the actual probe
# is _is_project_root (the pyproject marker requires the table, not just
# the file).
MARKERS = (CONFIG_DOTFILE, f"{PYPROJECT} with [tool.{PYPROJECT_TABLE}]")

_cached_default_root: Path | None = None
_cached_default_source: str | None = None


def resolve_root(explicit: str | Path | None = None) -> Path:
    """Return the repository/project root per the resolution order above.

    ``explicit`` bypasses the cache; the default resolution is computed
    once per process (matching the previous ``repo_root()`` caching).
    Explicit and environment-supplied roots must exist and be
    directories - a typo must fail loudly here, not silently redirect
    logs and state to a location later code would create.
    """
    if explicit is not None:
        if isinstance(explicit, str) and _is_alias_candidate(explicit):
            candidate = Path(explicit).expanduser()
            if not candidate.is_dir():
                from . import repos
                return repos.resolve_alias(explicit)
        return _validated_root(explicit, source="explicit argument")

    global _cached_default_root, _cached_default_source
    if _cached_default_root is not None:
        return _cached_default_root

    env_value = os.environ.get(ENV_VAR, "").strip()
    if env_value:
        _cached_default_root = _validated_root(env_value, source=ENV_VAR)
        _cached_default_source = "environment"
        return _cached_default_root

    marked = _walk_for_marker(Path.cwd())
    if marked is not None:
        _cached_default_root = marked
        _cached_default_source = "marker"
        return _cached_default_root

    from . import repos
    default = repos.default_root()
    if default is not None:
        _cached_default_root = default
        _cached_default_source = "default"
        return _cached_default_root

    raise ValueError(
        f"no project root found: no {ENV_VAR} set, no --repo given, and no "
        f"marker ({' or '.join(MARKERS)}) in {Path.cwd()} or its parents, "
        "and no default repo configured"
    )


def clear_cache() -> None:
    """Reset the cached default resolution (tests only)."""
    global _cached_default_root, _cached_default_source
    _cached_default_root = None
    _cached_default_source = None


def resolution_source() -> str | None:
    """Source used by the cached implicit resolution."""
    return _cached_default_source


def _is_alias_candidate(value: str) -> bool:
    return bool(value) and not any(
        separator in value for separator in (os.sep, os.altsep) if separator
    ) and value not in (".", "..") and not value.startswith("~")


def _validated_root(value: str | Path, *, source: str) -> Path:
    # Reject blank strings BEFORE constructing Path: Path("") is "." and
    # would silently resolve to CWD, bypassing validation entirely.
    if isinstance(value, str) and not value.strip():
        raise ValueError(f"repo root from {source} is blank")
    root = Path(value).expanduser().resolve()
    if not root.is_dir():
        raise ValueError(
            f"repo root from {source} is not an existing directory: {root}"
        )
    return root


def _walk_for_marker(start: Path) -> Path | None:
    current = start.resolve()
    for candidate in (current, *current.parents):
        if _is_project_root(candidate):
            return candidate
    return None


def _is_project_root(candidate: Path) -> bool:
    if (candidate / CONFIG_DOTFILE).is_file():
        return True
    # A pyproject.toml marks the root only when it actually declares the
    # [tool.agents-live] table; an unreadable one cannot prove it.
    table = _pyproject_table(candidate / PYPROJECT, on_error="ignore")
    return table is not None


def _pyproject_table(pyproject: Path, *, on_error: str) -> dict | None:
    """The ``[tool.agents-live]`` table of *pyproject*, or None when
    the file or table is absent. ``on_error`` is ``"ignore"`` (walk
    probe: unreadable file is simply not a marker) or ``"raise"``
    (config read: an existing file that might hold config must never be
    silently dropped - ValueError)."""
    if not pyproject.is_file():
        return None
    try:
        with pyproject.open("rb") as fh:
            doc = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        if on_error == "ignore":
            return None
        raise ValueError(
            f"project config unreadable: {pyproject}: {exc}") from exc
    tool = doc.get("tool")
    table = tool.get(PYPROJECT_TABLE) if isinstance(tool, dict) else None
    return table if isinstance(table, dict) else None


def config_source(root: Path | None = None) -> Path | None:
    """The file supplying project config: the root dotfile when present
    (authoritative), else ``pyproject.toml`` when it declares the
    ``[tool.agents-live]`` table, else None."""
    base = resolve_root() if root is None else Path(root)
    dotfile = base / CONFIG_DOTFILE
    if dotfile.is_file():
        return dotfile
    if _pyproject_table(base / PYPROJECT, on_error="ignore") is not None:
        return base / PYPROJECT
    return None


def load_config(root: Path | None = None) -> dict:
    """The effective project config mapping (§3.2 config home).

    ``.agents-live.toml`` at the root is the whole config and wins
    outright when present; otherwise the ``[tool.agents-live]``
    table of ``pyproject.toml``; otherwise ``{}`` (a project that never
    opted into any setting). Raises ValueError when an existing file
    that would supply config cannot be read or parsed - callers decide
    whether that is fatal (ownership: fail closed) or ignorable
    (agent-directory extras: fall back to the default)."""
    base = resolve_root() if root is None else Path(root)
    dotfile = base / CONFIG_DOTFILE
    if dotfile.is_file():
        try:
            with dotfile.open("rb") as fh:
                return tomllib.load(fh)
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise ValueError(
                f"project config unreadable: {dotfile}: {exc}") from exc
    table = _pyproject_table(base / PYPROJECT, on_error="raise")
    return table if table is not None else {}


def validated_agent_directories(root: Path, values: object) -> list[Path]:
    """Validate configured directories remain within *root*."""
    if not isinstance(values, list):
        raise ValueError("agent_directories must be a list of repo-relative paths")
    base = root.resolve()
    result = []
    for value in values:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("agent_directories entries must be non-empty strings")
        relative = Path(value)
        if relative.is_absolute():
            raise ValueError(f"agent_directories entry must be repo-relative: {value}")
        resolved = (base / relative).resolve()
        try:
            resolved.relative_to(base)
        except ValueError as exc:
            raise ValueError(
                f"agent_directories entry escapes the repository: {value}") from exc
        result.append(resolved)
    return result
