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
    cli.py stop <name>               deactivate, keep config
    cli.py status [...]              list agents and state (status.py)
    cli.py smoketest [...]           end-to-end validation (smoketest.py)
    cli.py doctor [...]              environment/install checks
    cli.py logs [timeline] [...]     query logs (qlog.py / timeline.py)
    cli.py dashboard [...]           interactive control panel (dashboard.py)

Global flag: ``--repo <path-or-alias>`` (before the subcommand) pins the
project root. Without it, resolution follows paths.py.

``run``/``start``/``stop`` accept the agent name positionally
(``cli.py run foo`` == ``cli.py run --name foo``).

``logs`` and ``dashboard`` delegate via ``uv run --script`` so their
heavier dependencies (DuckDB, UI) stay on-demand (decision §6.4); the
other subcommands dispatch in-process to the module's ``main()``.
"""
from __future__ import annotations

import os
import contextlib
import io
import json
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
from .cli_spec import (
    COMMAND_BY_NAME,
    Cmd,
    command_help,
    render_usage,
    unknown_flag,
    validation_error,
)

# First-use adoption (§3.2 amendment, 2026-07-15): `run` and `start`
# inside a git repository that has no marker write the minimal local-mode
# marker at the git root instead of failing, so the simple local case
# needs no `init`. The guess happens once, is recorded in a file
# `git status` shows, and never applies to scheduled work (persisted
# invocations pin the root). Every other command - and any resolution
# failure caused by a set-but-invalid AGENTS_LIVE_REPO - keeps the
# fail-loudly contract.
_MARKER_TEMPLATE = """\
# agents-live project marker (auto-created by `agents-live {cmd}`).
# Marks the project root and holds project config; an empty config means
# all defaults (local ownership mode). Run `agents-live init` to install
# the skill, seed agent directories, or declare non-local configuration.
"""

DOCS_URL = "https://github.com/johnshew/agents-live"


def _usage() -> str:
    return render_usage(__version__, DOCS_URL)


def _apply_name_sugar(name_sugar: bool, rest: list[str]) -> list[str]:
    if name_sugar and rest and not rest[0].startswith("-"):
        return ["--name", rest[0], *rest[1:]]
    return rest


def _finish(code: int, command: Cmd | None, rest: list[str],
            *, json_mode: bool) -> int:
    if (
        (command is None or command.update_notice)
        and not json_mode
        and "--quiet" not in rest
        and update_check.interactive()
    ):
        notice = update_check.consume_notice(__version__)
        update_check.launch_if_stale()
        if notice:
            print(f"\n{notice}", file=sys.stderr)
    return code


def _emit_failure(code: str, operation: str, detail: str,
                  *, json_mode: bool) -> None:
    preflight.emit_error(
        preflight.CapabilityFailure(
            code, "command", operation, detail.strip() or "command failed"),
        json_mode=json_mode,
    )


def _captured_result(code: int, cmd: str, stdout: str, stderr: str,
                     shape: str = "object") -> int:
    """Emit one JSON value for a captured JSON-capable command."""
    text = stdout.strip()
    lines = []
    for line in text.splitlines():
        try:
            lines.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    if shape == "records" and code == 0:
        # One record per stdout line; the envelope always carries a
        # ``records`` list so consumers see one shape for 0, 1, or N
        # rows. Failures use the shared error handling below.
        print(json.dumps({"ok": True, "operation": cmd, "records": lines}))
        return code
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = lines[-1] if lines else None
        if code != 0:
            parsed = next(
                (item for item in reversed(lines)
                 if isinstance(item, dict) and "error" in item),
                parsed,
            )
    if code != 0:
        # A structured payload (an error envelope, or a result document
        # like doctor's {ok: false, checks: [...]}) passes through
        # untouched; only unstructured output becomes an envelope.
        if isinstance(parsed, dict):
            print(json.dumps(parsed))
        else:
            _emit_failure(
                "operation_failed", cmd, stderr.strip() or text,
                json_mode=True,
            )
    elif parsed is not None:
        print(json.dumps(parsed))
    else:
        payload = {"ok": True, "operation": cmd}
        if text:
            payload["detail"] = text
        print(json.dumps(payload))
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
                _emit_failure(
                    "usage_error", "--repo", "--repo requires a path or alias",
                    json_mode=json_mode)
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
        return _finish(0, None, [], json_mode=json_mode)

    cmd, rest = args[0], args[1:]
    command = COMMAND_BY_NAME.get(cmd)
    if command is None:
        _emit_failure(
            "unknown_command", cmd, f"unknown command '{cmd}'",
            json_mode=json_mode)
        return 2
    if "--json" in rest:
        json_mode = True
        os.environ[preflight.JSON_ENV_VAR] = "1"
        rest = [argument for argument in rest if argument != "--json"]
    # Commands without envelope support (command.json False) still accept
    # --json: the env var carries envelope mode to any typed errors, and
    # the command's own output passes through uncaptured.
    capture = json_mode and command.json
    if any(arg in ("-h", "--help") for arg in rest):
        print(command_help(command, cmd), end="")
        return _finish(0, command, rest, json_mode=json_mode)
    unknown = unknown_flag(command, rest)
    if unknown is not None:
        _emit_failure(
            "usage_error", cmd, f"unrecognized argument: {unknown}",
            json_mode=json_mode)
        return 2
    all_repos = "--all-repos" in rest
    if all_repos and not command.all_repos:
        _emit_failure(
            "usage_error", cmd,
            f"{cmd} does not support --all-repos; select one repository",
            json_mode=json_mode)
        return 2
    invalid = validation_error(command, rest)
    if invalid is not None:
        _emit_failure("usage_error", cmd, invalid, json_mode=json_mode)
        return 2

    # Resolve the project root ONCE before dispatch so a missing root is a
    # structured CLI error, never a traceback from an imported or
    # delegated module.
    if command.root != "none" and not all_repos:
        if (
            command.root == "auto-marker"
            and not os.environ.get(paths.ENV_VAR, "").strip()
            and paths._walk_for_marker(Path.cwd()) is None
        ):
            _adopt_git_root(cmd)
        try:
            paths.resolve_root()
        except ValueError as exc:
            allow_markerless_invocation = (
                command.root == "markerless"
                and not os.environ.get(paths.ENV_VAR, "").strip()
            )
            if not allow_markerless_invocation and (
                    command.root != "auto-marker"
                    or _adopt_git_root(cmd) is None):
                preflight.emit_error(preflight.CapabilityFailure(
                    "no_project_root", "project-root", cmd, str(exc)),
                    json_mode=json_mode)
                return 2
        if (
            paths.resolution_source() == "default"
            and command.default_notice
        ):
            print(f"agents-live: using default repo {paths.resolve_root()}",
                  file=sys.stderr)

    rest = _apply_name_sugar(command.name_sugar, rest)

    # Static capability preflight for host-mutating commands (§3.6).
    # Advisory layer 1 of 3: the operation itself still converts failures,
    # and post-verification confirms state. For a targeted `start` the
    # probe set derives from the agent's own triggers.
    if command.probes or command.dynamic_probes is not None:
        capabilities = (
            _start_capabilities(rest)
            if command.dynamic_probes == "start"
            else None
        )
        if capabilities is None:
            capabilities = frozenset(command.probes)
        failure = preflight.check(cmd, capabilities)
        if failure is not None:
            preflight.emit_error(failure, json_mode=json_mode)
            return 2

    if command.dispatch == "subprocess":
        active = command
        script = command.module
        if rest:
            subcommand = next(
                (sub for sub in command.subcommands if sub.name == rest[0]),
                None,
            )
            if subcommand is not None:
                active, script, rest = subcommand, subcommand.module, rest[1:]
        if capture and active.json_args:
            lead = active.json_args[0]
            if not any(token == lead or token.startswith(f"{lead}=")
                       for token in rest):
                rest.extend(active.json_args)
        uv = shutil.which("uv") or "uv"
        try:
            completed = subprocess.run(
                [uv, "run", "--script", str(SCRIPT_DIR / script), *rest],
                check=False,
                **({"capture_output": True, "text": True} if capture else {}),
            )
        except KeyboardInterrupt:
            # Ctrl-C reaches the child (same process group) which handles
            # its own shutdown; the waiting parent reports the
            # conventional interrupt status instead of a traceback.
            return 130
        code = completed.returncode
        if code < 0:
            code = 128 - code  # signal death -> conventional 128+signum
        if capture:
            return _captured_result(
                code, cmd, completed.stdout, completed.stderr,
                shape=active.json_shape)
        return _finish(code, command, rest, json_mode=json_mode)

    import importlib
    # Package-aware dispatch (Phase 4): as loose scripts the modules are
    # top-level; installed as a package they are agents_live.<name>.
    module_name = command.module
    if __package__:
        module_name = f"{__package__}.{module_name}"
    module = importlib.import_module(module_name)
    sys.argv = [f"agents-live {cmd}", *rest]
    try:
        if capture:
            stdout = io.StringIO()
            stderr = io.StringIO()
            try:
                with (
                    contextlib.redirect_stdout(stdout),
                    contextlib.redirect_stderr(stderr),
                ):
                    code = module.main()
            except SystemExit as exc:
                # A subcommand's own argparse exits inside the redirect;
                # surface its captured message as an envelope instead of
                # exiting with empty stdout and stderr.
                code = exc.code if isinstance(exc.code, int) else (
                    1 if exc.code else 0)
                if code != 0:
                    _emit_failure(
                        "usage_error", cmd,
                        stderr.getvalue().strip() or stdout.getvalue().strip(),
                        json_mode=True)
                    return code
            return _captured_result(
                code, cmd, stdout.getvalue(), stderr.getvalue(),
                shape=command.json_shape)
        code = module.main()
        return _finish(code, command, rest, json_mode=json_mode)
    except Exception as exc:
        # Layer-2 safety net: a typed error that escapes a subcommand's
        # own handling still leaves as the envelope, never a traceback.
        # Untyped exceptions are programming bugs; re-raise so the crash
        # site stays diagnosable.
        if getattr(exc, "category", None) is None:
            raise
        preflight.emit_typed_error(exc, cmd)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
