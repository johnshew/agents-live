"""Repository-root, config-home, and state-location resolution.

Single source of truth for "which repo/project am I operating on".
Resolution order:

1. Explicit argument (the ``agents-live --repo`` flag).
2. ``AGENTS_LIVE_REPO`` environment variable.
3. Walk up from CWD to the nearest directory containing a marker:
   ``.agents-live.toml``, or ``pyproject.toml`` with a
   ``[tool.agents-live]`` table.
4. The optional user-configured default workspace.
5. The sole registered repository, when the registry holds exactly one
   AND the caller opted in (``allow_sole_registered``).
6. The initialized host-global workspace.

Step 5 precedes the global workspace because ``init`` always initializes
that workspace, so it always resolves and would mask the single project
the host actually manages (issue #173: the dashboard rendered the empty
global workspace instead). One registered repository is unambiguous;
two or more without a default are not, and those still fall through to
the global workspace.

Step 5 is opt-in rather than automatic because this resolver answers for
every command, not only the read-only ones that needed it. "Exactly one
repository is registered" is the state of every host that has run
``repos add`` once, so left automatic it silently supplies a target to
``start``, ``stop``, ``delete`` and ``migrate`` as well - writing cron
entries or scheduled tasks into a project the invocation never named
(issue #192). Read-only callers pass ``allow_sole_registered=True``;
everything else keeps failing loudly, which is also what makes a command
added later inherit the safe answer by default.

No script-location fallback exists: it would resolve inside the installed
package instead of the user's project. With no explicit root, environment
variable, marker, configured default, sole registered repository, or
initialized global workspace, resolution fails loudly. All persisted
invocations (cron lines, watcher respawns, dispatches) pin CWD to the
repo, so the marker walk always succeeds for scheduled work. One
first-use exception lives at the CLI layer, not here: an explicit agent file
path does not require workspace resolution.

The markers ARE the config home (§3.2 decision, 2026-07-12): project
config lives at the repo root in ``.agents-live.toml``
(authoritative when both exist) or the ``[tool.agents-live]`` table
of ``pyproject.toml``. :func:`load_config` reads it; ``init`` writes it.
Runtime state (logs, beacons, watch hashes) lives OUTSIDE the project
tree in the user-level XDG state home (:func:`state_home`,
:func:`repo_state_dir`); ``Agents/`` holds git-tracked content plus the
git-synced shared ownership registry ``Agents/data/agent-owners.json``.
(Names renamed from triggered-tasks 2026-07-12, R1a of the convergence
plan; state moved out of the tree 2026-07-19 - clean break, no legacy
locations read.)

stdlib-only on purpose, apart from the host-runtime seam beside it. Scripts
dispatched with ``uv run --script`` (qlog, timeline, dashboard) put the
package root on ``sys.path`` and import this module as
``agents_live.paths``; nothing imports it flat.
"""
from __future__ import annotations

import hashlib
import os
import re
import tempfile
import time
import tomllib
from pathlib import Path

from .runtime.hosts import system as hostruntime

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
_SHA256 = re.compile(r"^[0-9a-fA-F]{64}$")


def _repos_module():
    """Imported lazily: the registry reads config, which reads this module."""
    from .state import registry
    return registry


def _no_root_error(allow_sole_registered: bool) -> ValueError:
    """The shared "nothing resolved" failure.

    The registry clause appears only when the caller opted in: telling a
    host-mutating command there was "no single registered repository to
    fall back to" would describe a fallback it is not allowed to use.
    """
    reasons = [
        f"no {ENV_VAR} set",
        "no --repo given",
        f"no marker ({' or '.join(MARKERS)}) in {Path.cwd()} or its parents",
    ]
    if allow_sole_registered:
        reasons.append("no single registered repository to fall back to")
    reasons.append("the global workspace is not initialized")
    return ValueError(
        "no project root found: " + ", ".join(reasons[:-1])
        + f", and {reasons[-1]}; run `agents-live init` here, or "
        "`agents-live repos default <path>` to select a registered project")


def resolve_root(explicit: str | Path | None = None, *,
                 allow_sole_registered: bool = False) -> Path:
    """Return the repository/project root per the resolution order above.

    ``explicit`` bypasses the cache; the default resolution is computed
    once per process (matching the previous ``repo_root()`` caching).
    Explicit and environment-supplied roots must exist and be
    directories - a typo must fail loudly here, not silently redirect
    logs and state to a location later code would create.

    ``allow_sole_registered`` opts in to step 5. It is off by default so
    that a host-mutating command run from an unrelated directory fails
    loudly instead of acting on whichever project happens to be the only
    one registered (issue #192).
    """
    if explicit is not None:
        if isinstance(explicit, str) and _is_name_candidate(explicit):
            # The registry is consulted FIRST: a plain name that is a
            # registered repo always means that repository, never a
            # same-named directory that happens to exist under the
            # caller's CWD (which would make the target flip with CWD).
            repos = _repos_module()
            registry = repos.load()
            if explicit in registry["repos"]:
                return repos.resolve_name(explicit, registry)
            if not Path(explicit).expanduser().is_dir():
                raise ValueError(
                    f"repo {explicit!r} is not registered; run "
                    "`agents-live repos list`")
        return _validated_root(explicit, source="explicit argument")

    global _cached_default_root, _cached_default_source
    if _cached_default_root is not None:
        if (_cached_default_source == "sole-registered"
                and not allow_sole_registered):
            # A read-only caller resolved first and cached the registry
            # fallback; handing that root to a mutating caller would
            # reintroduce #192 through the cache.
            raise _no_root_error(allow_sole_registered)
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

    repos = _repos_module()
    default = repos.default_root()
    if default is not None:
        _cached_default_root = default
        _cached_default_source = "default"
        return _cached_default_root

    registry = repos.load()
    if allow_sole_registered and len(registry["repos"]) == 1:
        alias = next(iter(registry["repos"]))
        try:
            sole = repos.resolve_name(alias, registry)
        except ValueError:
            # The one registered repository has been moved or deleted.
            # This step is a convenience, so an unusable entry falls
            # through to the remaining resolution rather than turning a
            # missing root into a failure about an alias the caller
            # never mentioned.
            sole = None
        if sole is not None:
            _cached_default_root = sole
            _cached_default_source = "sole-registered"
            return _cached_default_root

    global_workspace = global_root()
    if config_source(global_workspace) is not None:
        _cached_default_root = global_workspace
        _cached_default_source = "global"
        return _cached_default_root

    raise _no_root_error(allow_sole_registered)


def local_root() -> Path | None:
    """The env-var or marker resolution WITHOUT the registry default.

    The project the caller is actually inside (or explicitly selected),
    if any. The one shared answer for callers that must not fall back to
    the configured default repository (e.g. upgrade's target discovery).
    Raises ValueError when the env var is set but invalid.
    """
    env_value = os.environ.get(ENV_VAR, "").strip()
    if env_value:
        return _validated_root(env_value, source=ENV_VAR)
    return _walk_for_marker(Path.cwd())


def clear_cache() -> None:
    """Reset the cached default resolution (tests only)."""
    global _cached_default_root, _cached_default_source
    _cached_default_root = None
    _cached_default_source = None


def resolution_source() -> str | None:
    """Source used by the cached implicit resolution."""
    return _cached_default_source


def state_home() -> Path:
    """The user-level runtime-state root (§ user-level state, 2026-07-19):
    ``$XDG_STATE_HOME/agents-live``, defaulting to the host's own
    per-user state directory (``~/.local/state`` on POSIX, the local
    application-data directory on Windows - see
    :func:`hostruntime.user_state_base`).

    Host-scoped runtime artifacts live directly here (health beacon, the
    health-check loop's own log, the Windows heartbeat beacon); per-repo
    runtime state lives under :func:`repo_state_dir`. Runtime state never
    lives inside a project tree: repositories sync between machines and
    export to archives, and machine-local logs must not travel with them.
    """
    root = os.environ.get("XDG_STATE_HOME", "").strip()
    base = Path(root).expanduser() if root else hostruntime.user_state_base()
    return base / "agents-live"


def xdg_data_home() -> Path:
    """User-level XDG data directory."""
    root = os.environ.get("XDG_DATA_HOME", "").strip()
    return Path(root).expanduser() if root else Path.home() / ".local" / "share"


def data_home() -> Path:
    """User-level durable data root for host-global agent content."""
    return xdg_data_home() / "agents-live"


def global_root() -> Path:
    """Host-global workspace used when no initialized repository is selected."""
    return data_home() / "workspace"


def host_logs_dir() -> Path:
    """Host-level log directory (health-check loop and other host-scoped
    operations that run with no project selected)."""
    return state_home() / "logs"


def health_beacon_path() -> Path:
    """The host health beacon written by automatic maintenance."""
    return state_home() / "health.ok"


def repo_state_key(root: Path) -> str:
    """Stable per-repo state-directory name: ``<basename>-<hash8>``.

    The basename keeps the directory browsable; the hash of the resolved
    absolute path keeps same-named repos in different locations distinct.
    A moved repository gets a fresh state directory (old logs are
    abandoned, not corrupted) - acceptable for machine-local state.
    """
    resolved = Path(root).resolve()
    digest = hashlib.sha256(str(resolved).encode("utf-8")).hexdigest()[:8]
    return f"{resolved.name}-{digest}"


def repo_state_dir(root: Path) -> Path:
    """Per-repo runtime-state directory under the user-level state home."""
    return state_home() / "repos" / repo_state_key(root)


def _is_name_candidate(value: str) -> bool:
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
    opted into any setting). Plugin declarations are validated here, including
    their repository-relative wheel paths and optional SHA-256 syntax. Raises
    ValueError when an existing file that would supply config cannot be read,
    parsed, or validated - callers decide
    whether that is fatal (ownership: fail closed) or ignorable
    (agent-directory extras: fall back to the default)."""
    base = resolve_root() if root is None else Path(root)
    dotfile = base / CONFIG_DOTFILE
    if dotfile.is_file():
        try:
            with dotfile.open("rb") as fh:
                config = tomllib.load(fh)
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise ValueError(
                f"project config unreadable: {dotfile}: {exc}") from exc
    else:
        table = _pyproject_table(base / PYPROJECT, on_error="raise")
        config = table if table is not None else {}
    # Shape-only validation: a malformed plugins table is a config error
    # every command should fail on, but a declared wheel missing from
    # disk (a gitignored dist/ on a fresh clone) must not break agent
    # discovery - existence is enforced by the plugin operations that
    # consume the artifact.
    validated_plugins(base, config.get("plugins", {}), require_exists=False)
    return config


def validated_plugins(root: Path, values: object, *,
                      require_exists: bool = True) -> dict[str, dict[str, object]]:
    """Validate and resolve project-declared plugin wheels."""
    if not isinstance(values, dict):
        raise ValueError("plugins must be a table")
    base = root.resolve()
    result = {}
    for name, declaration in values.items():
        if not isinstance(name, str) or not name.strip():
            raise ValueError("plugin names must be non-empty strings")
        if not isinstance(declaration, dict):
            raise ValueError(f"plugin {name!r} must be an inline table")
        unknown = set(declaration) - {"path", "sha256"}
        if unknown:
            raise ValueError(
                f"plugin {name!r} has unknown field(s): {', '.join(sorted(unknown))}")
        value = declaration.get("path")
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"plugin {name!r} path must be a non-empty string")
        relative = Path(value)
        if relative.is_absolute():
            raise ValueError(f"plugin {name!r} path must be repo-relative: {value}")
        resolved = (base / relative).resolve()
        try:
            resolved.relative_to(base)
        except ValueError as exc:
            raise ValueError(
                f"plugin {name!r} path escapes the repository: {value}") from exc
        if require_exists and not resolved.is_file():
            raise ValueError(f"plugin {name!r} wheel does not exist: {value}")
        if resolved.suffix != ".whl":
            raise ValueError(f"plugin {name!r} path must name a .whl file: {value}")
        digest = declaration.get("sha256")
        if digest is not None and (
                not isinstance(digest, str) or not _SHA256.fullmatch(digest)):
            raise ValueError(
                f"plugin {name!r} sha256 must be exactly 64 hexadecimal characters")
        result[name] = {"path": resolved, "sha256": digest}
    return result


_REPLACE_DEADLINE_SECONDS = 2.0


def _replace_when_windows_lets_go(temporary: str, path: Path) -> None:
    """``os.replace``, waiting out a Windows hold on the destination.

    POSIX ``rename`` succeeds no matter who holds the target open, so
    this loop is inert there: the first attempt either works or fails
    for a reason waiting cannot cure. Windows instead refuses with
    ``ERROR_ACCESS_DENIED`` for as long as any process holds the
    destination open without ``FILE_SHARE_DELETE`` -- which every plain
    ``open()`` omits, so one process merely *reading* a state file
    defeats another's write. Antivirus and search indexers take the same
    kind of hold on a freshly written file.

    Every such holder releases in milliseconds, so a bounded retry turns
    a spurious hard failure into a brief wait. A caller that still times
    out sees the original error, because at that point the destination
    is genuinely unavailable rather than momentarily busy.
    """
    deadline = time.monotonic() + _REPLACE_DEADLINE_SECONDS
    delay = 0.005
    while True:
        try:
            os.replace(temporary, path)
            return
        except PermissionError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(delay)
            delay = min(delay * 2, 0.05)


def atomic_write_text(path: Path, content: str, *,
                      mode: int | None = None) -> None:
    """Write-temp-then-rename so readers never observe a partial file.

    The temp file lives in the target directory (same filesystem, so
    ``os.replace`` is atomic), is fsynced before the rename, and is
    removed on any failure. ``mode`` restricts permissions before any
    content is written. The rename waits out a momentary Windows hold on
    the destination rather than failing the write."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=path.parent, text=True)
    handle = None
    try:
        if mode is not None:
            # By descriptor where the host has it, so the file being
            # narrowed is certainly the one just created. Windows grew
            # os.fchmod only in 3.13, and has no POSIX mode bits to
            # narrow in any version: chmod there sets the read-only
            # flag and nothing else, which is all it can promise.
            if hasattr(os, "fchmod"):
                os.fchmod(fd, mode)
            else:
                os.chmod(temporary, mode)
        handle = os.fdopen(fd, "w", encoding="utf-8")
        fd = -1
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
        handle.close()
        handle = None
        _replace_when_windows_lets_go(temporary, path)
    except BaseException:
        # Close before unlinking: Windows refuses to remove a file that
        # anything still holds open, and a failure before fdopen leaves
        # the descriptor from mkstemp with no owner to close it.
        if handle is not None:
            handle.close()
        elif fd >= 0:
            os.close(fd)
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


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
        # Not is_absolute(): on Windows that is False for a rooted path
        # with no drive ("/tmp/agents", "\\evil") and for a
        # drive-relative one ("C:agents"), both of which are anchored
        # somewhere other than this repository. anchor covers all three
        # spellings on both platforms.
        if relative.anchor:
            raise ValueError(f"agent_directories entry must be repo-relative: {value}")
        resolved = (base / relative).resolve()
        try:
            resolved.relative_to(base)
        except ValueError as exc:
            raise ValueError(
                f"agent_directories entry escapes the repository: {value}") from exc
        result.append(resolved)
    return result
