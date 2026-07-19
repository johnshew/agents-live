#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["PyYAML", "mcp[cli]", "jsonschema"]
# ///
# The dependency list is the UNION of every in-process subcommand's PEP 723
# set (run.py needs mcp[cli] for pipeline mode; smoketest.py needs mcp +
# jsonschema) because in-process modules and their sys.executable children
# run in THIS env. This mirrors the packaged world, where these become the
# package's core dependencies. logs/dashboard stay out: they delegate via
# `uv run --script` to keep DuckDB/UI deps on-demand (decision 6.4).
"""agents-live - single-command entry point (proposal §3.1, Phase 1).

One dispatcher over the existing modules; the logic stays in them. Every
lifecycle operation is a subcommand:

    cli.py run <name> [...]          execute an agent once (run.py)
    cli.py start <name>|--all [...]  activate cron/watcher (activate.py)
    cli.py stop <name>               deactivate, keep config (teardown.py)
    cli.py teardown <name>           same as stop (teardown.py)
    cli.py status [...]              list agents and state (status.py)
    cli.py smoketest [...]           end-to-end validation (smoketest.py)
    cli.py doctor [...]              environment/install checks (prereqs.py)
    cli.py logs [timeline] [...]     query logs (qlog.py / timeline.py)
    cli.py dashboard [...]           interactive control panel (dashboard.py)

Global flag: ``--repo <path-or-alias>`` (before the subcommand) pins the
project root. Without it, resolution follows paths.py.

``run``/``start``/``stop``/``teardown`` accept the agent name positionally
(``cli.py run foo`` == ``cli.py run --name foo``).

``logs`` and ``dashboard`` delegate via ``uv run --script`` so their
heavier dependencies (DuckDB, UI) stay on-demand (decision §6.4); the
other subcommands dispatch in-process to the module's ``main()``.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from . import paths
from . import preflight
from . import update_check
from . import __version__

# Host-mutating subcommands run the static capability preflight first
# (proposal §3.6). Read-only commands never preflight - they must work
# sandboxed.
HOST_MUTATING = frozenset(preflight._COMMAND_PROBES)

# subcommand -> module name (in-process dispatch to module.main())
IN_PROCESS = {
    "run": "run",
    "start": "activate",
    "stop": "teardown",
    "teardown": "teardown",
    "status": "status",
    "smoketest": "smoketest",
    "doctor": "prereqs",
    "prereqs": "prereqs",  # alias
    "init": "init",
    "upgrade": "upgrade",
    "migrate": "migrate",
    "heartbeat": "heartbeat",
    "uninstall": "uninstall",
    "repos": "repos",
}

# init DEFINES the project root (creates the marker); it must not be
# gated on resolving one.
NO_ROOT_REQUIRED = frozenset({"init", "upgrade", "heartbeat", "uninstall", "repos"})
MARKERLESS_ALLOWED = frozenset({"doctor", "prereqs"})

# First-use adoption (§3.2 amendment, 2026-07-15): `run` and `start`
# inside a git repository that has no marker write the minimal local-mode
# marker at the git root instead of failing, so the simple local case
# needs no `init`. The guess happens once, is recorded in a file
# `git status` shows, and never applies to scheduled work (persisted
# invocations pin the root). Every other command - and any resolution
# failure caused by a set-but-invalid AGENTS_LIVE_REPO - keeps the
# fail-loudly contract.
AUTO_MARKER = frozenset({"run", "start"})

_MARKER_TEMPLATE = """\
# agents-live project marker (auto-created by `agents-live {cmd}`).
# Marks the project root and holds project config; an empty config means
# all defaults (local ownership mode). Run `agents-live init` to install
# the skill, seed agent directories, or declare non-local configuration.
"""

# subcommand -> script file (subprocess via uv run --script; on-demand deps)
SUBPROCESS = {
    "logs": "qlog.py",
    "dashboard": "dashboard.py",
}

# subcommands whose first bare argument is agent-name sugar for --name
NAME_SUGAR = {"run", "start", "stop", "teardown"}

DOCS_URL = "https://github.com/johnshew/agents-live"
ALL_REPOS_COMMANDS = frozenset({"status", "doctor", "prereqs", "dashboard"})
# `run` executes an agent, so silently targeting the registry default
# would be invisible mutation; `upgrade` is NO_ROOT_REQUIRED and never
# reaches the notice block, so listing it here would be dead weight.
DEFAULT_NOTICE_COMMANDS = HOST_MUTATING | frozenset({"run", "migrate"})


def _usage() -> str:
    # Doc links pinned per §3.5 (repin from main to the release tag at
    # packaging time, Phase 4).
    blob = f"{DOCS_URL}/blob/v0.3.1/src/agents_live/skill/docs"
    return (
        "usage: agents-live [--json] [--repo PATH] <command> [args]\n"
        "       agents-live --version\n\n"
        "commands:\n"
        "  run <name>          execute an agent once (verbose)\n"
        "  start <name>|--all  activate cron/watcher triggers\n"
        "  stop <name>         deactivate triggers, keep config\n"
        "  teardown <name>     same as stop\n"
        "  status [name]       list agents and runtime state\n"
        "  logs [timeline]     query logs / correlated event timeline\n"
        "  smoketest           end-to-end validation\n"
        "  doctor              environment and install checks\n"
        "  init                initialize the project layout\n"
        "  upgrade             upgrade runtime and project skill payloads\n"
        "  migrate             converge cron/watcher entries to the\n"
        "                      canonical invocation form\n"
        "  heartbeat           run or manage the WSL host heartbeat\n"
        "  uninstall           remove host integrations, then the uv tool\n"
        "  repos               manage registered repositories\n"
        "  dashboard           interactive control panel\n\n"
        "global flags:\n"
        "  --json              machine-readable output and error envelopes\n"
        "  --repo PATH|ALIAS   pin a path or registered repository (else\n"
        "                      AGENTS_LIVE_REPO, local marker, then default)\n"
        "  --version           show the installed version and exit\n\n"
        f"docs: {DOCS_URL}\n"
        f"  commands reference  {blob}/commands.md\n"
        f"  architecture        {blob}/approach.md\n"
        f"  diagnostics         {blob}/diagnostics.md\n"
    )


def _apply_name_sugar(cmd: str, rest: list[str]) -> list[str]:
    if cmd in NAME_SUGAR and rest and not rest[0].startswith("-"):
        return ["--name", rest[0], *rest[1:]]
    return rest


def _finish(code: int, cmd: str, rest: list[str], *, json_mode: bool) -> int:
    if (
        cmd not in ("doctor", "prereqs", "upgrade")
        and not json_mode
        and "--json" not in rest
        and "--quiet" not in rest
        and update_check.interactive()
    ):
        notice = update_check.consume_notice(__version__)
        update_check.launch_if_stale()
        if notice:
            print(f"\n{notice}", file=sys.stderr)
    return code


def _git_root(start: Path) -> Path | None:
    """Nearest ancestor (or *start*) containing ``.git`` - a directory
    for a normal checkout, a file for a worktree."""
    current = start.resolve()
    for candidate in (current, *current.parents):
        if (candidate / ".git").exists():
            return candidate
    return None


def _adopt_git_root(cmd: str) -> Path | None:
    """Write the minimal local-mode marker at the enclosing git root and
    re-resolve. None (the caller falls through to the structured
    ``no_project_root`` error) when AGENTS_LIVE_REPO is set (a typo'd
    env root must fail loudly, not be papered over), there is no git
    root, the marker already exists, or the write fails."""
    if os.environ.get(paths.ENV_VAR, "").strip():
        return None
    git_root = _git_root(Path.cwd())
    if git_root is None:
        return None
    marker = git_root / paths.CONFIG_DOTFILE
    if marker.exists():
        return None
    try:
        marker.write_text(_MARKER_TEMPLATE.format(cmd=cmd), encoding="utf-8")
    except OSError:
        return None
    paths.clear_cache()
    try:
        root = paths.resolve_root()
    except ValueError:
        return None
    print(
        f"agents-live: no project marker found; created {marker} "
        "(local mode; run `agents-live init` for more)",
        file=sys.stderr,
    )
    return root


def _start_capabilities(rest: list[str]) -> frozenset[str] | None:
    """Trigger-derived capability set for ``start`` (2026-07-12 finding:
    a cron-only agent must not require inotify). None = the default probe
    set (``--all`` or no name to derive from); an empty set skips the
    preflight so a nonexistent agent reports ``agent_invalid`` from the
    operation itself, not ``dependency_missing`` from the gate."""
    if "--all" in rest:
        return None
    name: str | None = None
    for index, token in enumerate(rest):
        if token == "--name" and index + 1 < len(rest):
            name = rest[index + 1]
            break
    if not name:
        return None
    from . import headless
    try:
        config = headless.load_agent_config(name)
    except Exception:
        return frozenset()
    capabilities = set()
    if config.schedule:
        capabilities.add("crontab")
    if config.watch_path:
        capabilities.add("inotify")
    return frozenset(capabilities)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)

    # Global flags, accepted in any order before the subcommand.
    json_mode = False
    while args:
        if args[0] == "--version":
            # __version__ is the same source every other consumer reads
            # (update checks, doctor), so the numbers can never disagree.
            print(f"agents-live {__version__}")
            return 0
        if args[0] == "--json":
            json_mode = True
            # Layer 2 (§3.6): carry json mode into in-process subcommands
            # and their children so typed errors downstream of the
            # preflight are serialized as envelopes too.
            os.environ[preflight.JSON_ENV_VAR] = "1"
            args = args[1:]
            continue
        if args[0] == "--repo":
            if len(args) < 2:
                print("error: --repo requires a path or alias", file=sys.stderr)
                return 2
            try:
                root = paths.resolve_root(args[1])
            except ValueError as exc:
                preflight.emit_error(preflight.CapabilityFailure(
                    "no_project_root", "project-root", "--repo", str(exc)),
                    json_mode=json_mode)
                return 2
            # Env var carries the choice into in-process resolution and any
            # child processes (watchers, handlers, subprocess subcommands).
            os.environ[paths.ENV_VAR] = str(root)
            paths.clear_cache()
            args = args[2:]
            continue
        break

    if not args or args[0] in ("-h", "--help", "help"):
        print(_usage())
        return _finish(0, "help", [], json_mode=json_mode)

    cmd, rest = args[0], args[1:]
    all_repos = "--all-repos" in rest
    if all_repos and cmd not in ALL_REPOS_COMMANDS:
        print(f"error: {cmd} does not support --all-repos; select one repository",
              file=sys.stderr)
        return 2

    # Resolve the project root ONCE before dispatch so a missing root is a
    # structured CLI error, never a traceback from an imported or
    # delegated module.
    if cmd not in NO_ROOT_REQUIRED and not all_repos:
        if (
            cmd in AUTO_MARKER
            and not os.environ.get(paths.ENV_VAR, "").strip()
            and paths._walk_for_marker(Path.cwd()) is None
        ):
            _adopt_git_root(cmd)
        try:
            paths.resolve_root()
        except ValueError as exc:
            allow_markerless_invocation = (
                cmd in MARKERLESS_ALLOWED
                and not os.environ.get(paths.ENV_VAR, "").strip()
            )
            if not allow_markerless_invocation and (
                    cmd not in AUTO_MARKER or _adopt_git_root(cmd) is None):
                preflight.emit_error(preflight.CapabilityFailure(
                    "no_project_root", "project-root", cmd, str(exc)),
                    json_mode=json_mode)
                return 2
        if (
            paths.resolution_source() == "default"
            and cmd in DEFAULT_NOTICE_COMMANDS
        ):
            print(f"agents-live: using default repo {paths.resolve_root()}",
                  file=sys.stderr)

    rest = _apply_name_sugar(cmd, rest)

    # Static capability preflight for host-mutating commands (§3.6).
    # Advisory layer 1 of 3: the operation itself still converts failures,
    # and post-verification confirms state. For a targeted `start` the
    # probe set derives from the agent's own triggers.
    if cmd in HOST_MUTATING:
        capabilities = _start_capabilities(rest) if cmd == "start" else None
        failure = preflight.check(cmd, capabilities)
        if failure is not None:
            preflight.emit_error(failure, json_mode=json_mode)
            return 2

    if cmd in SUBPROCESS:
        script = SUBPROCESS[cmd]
        if cmd == "logs" and rest and rest[0] == "timeline":
            script, rest = "timeline.py", rest[1:]
        uv = shutil.which("uv") or "uv"
        try:
            completed = subprocess.run(
                [uv, "run", "--script", str(SCRIPT_DIR / script), *rest],
                check=False,
            )
        except KeyboardInterrupt:
            # Ctrl-C reaches the child (same process group) which handles
            # its own shutdown; the waiting parent reports the
            # conventional interrupt status instead of a traceback.
            return 130
        code = completed.returncode
        if code < 0:
            code = 128 - code  # signal death -> conventional 128+signum
        return _finish(code, cmd, rest, json_mode=json_mode)

    if cmd not in IN_PROCESS:
        print(f"error: unknown command '{cmd}'\n\n{_usage()}", file=sys.stderr)
        return 2

    import importlib
    # Package-aware dispatch (Phase 4): as loose scripts the modules are
    # top-level; installed as a package they are agents_live.<name>.
    module_name = IN_PROCESS[cmd]
    if __package__:
        module_name = f"{__package__}.{module_name}"
    module = importlib.import_module(module_name)
    sys.argv = [f"agents-live {cmd}", *rest]
    try:
        code = module.main()
        return _finish(code, cmd, rest, json_mode=json_mode)
    except Exception as exc:
        # Layer-2 safety net: a typed error that escapes a subcommand's
        # own handling still leaves as the envelope, never a traceback.
        if getattr(exc, "category", None) is None:
            raise
        preflight.emit_typed_error(exc, cmd)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
