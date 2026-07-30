"""Upgrade the runtime and refresh managed project skill payloads.

A package module (relative imports): runs via ``agents-live upgrade``,
never as a standalone ``uv run --script`` target.
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

from . import (__version__, adminlog, dashboards, hostruntime, init, paths,
               plugins, preflight, repos, triggers)
from .spawn import find_uv


def _targets() -> tuple[list[tuple[str, Path]], list[str]]:
    local = paths.local_root()
    if os.environ.get(paths.ENV_VAR, "").strip():
        return [("selected project", local)], []

    targets: dict[Path, str] = {}
    global_root = paths.global_root()
    if paths.config_source(global_root) is not None:
        targets[global_root] = "global workspace"
    if local is not None:
        targets[local] = "current project"

    errors = []
    for alias, value, error in repos.entries():
        if error:
            errors.append(f"{alias}: {error}")
            continue
        root = Path(value)
        targets.setdefault(root, alias)
    from . import health_check  # noqa: PLC0415
    for root in health_check.persisted_roots():
        targets.setdefault(root, f"active workspace {root.name}")
    return [(label, root) for root, label in targets.items()], errors


def _refresh_payload(root: Path) -> None:
    status = init.install_skill(root)
    if status == "installed":
        message = "installed current skill payload"
    elif status == "refreshed":
        message = "upgraded skill payload to match the installed package"
    else:
        message = "skill payload already matches the installed package"
    print(f"{root}: {message}")


def _migrate_triggers(root: Path) -> None:
    completed = subprocess.run(
        [sys.executable, "-m", "agents_live.cli", "--repo", str(root),
         "internal", "migrate"],
        check=False,
    )
    if completed.returncode != 0:
        raise OSError(
            f"trigger migration failed with exit {completed.returncode}")


def _install_command(uv: str, source: Path | None) -> list[str]:
    """The uv command that puts a new runtime in the tool environment.

    Without a source this is the published package. With one it is a
    local build - a project directory or a built artifact - which is the
    only way to exercise the installed-tool leg of the testing boundary
    without publishing first (#179). ``--force`` is required because the
    tool is already installed; that is the whole point.

    ``--force`` alone is not enough for a local source: uv will happily
    reuse a cached build of the same directory, so an install can report
    success having put the *previous* source on disk. Installing a stale
    build is the one outcome this flag exists to rule out, so the local
    path also asks for the package itself to be rebuilt. It is scoped to
    ``agents-live`` rather than a blanket ``--reinstall`` so dependencies
    stay cached and the install does not turn into a full re-download.
    """
    if source is None:
        return [uv, "tool", "upgrade", "agents-live"]
    return [uv, "tool", "install", "--force",
            "--reinstall-package", "agents-live", str(source)]


def _holders(environment: Path) -> list[str]:
    """The long-lived agents-live processes running out of *environment*.

    Watchers and dashboards outlive the command that started them and
    hold the environment's files open. uv discovers that only part way
    through rebuilding, which is how an upgrade removes a plugin and then
    fails on the launcher, leaving neither the old nor the new state
    (#231).

    This process is deliberately not counted. It runs from the same
    environment and cannot stop itself, and uv writes the launcher last,
    so the launcher it holds is already covered by
    :func:`plugins.only_the_launcher_failed`. Only processes an operator
    can act on are named.
    """
    from .headless import split_command_line, watchers_on_host  # noqa: PLC0415

    try:
        held = {
            pid for pid, command in hostruntime.process_command_lines()
            if any(triggers.within(arg, environment)
                   for arg in split_command_line(command))
        }
        watchers = watchers_on_host(under=environment)
    except OSError:
        # An unreadable process table must not fail an upgrade that would
        # otherwise work; uv still reports whatever it cannot replace.
        return []
    holders = [
        f"watcher '{name}'{f' in {project}' if project else ''} (pid {pid})"
        for pid, name, project in watchers
    ]
    holders += [
        f"dashboard on port {entry['port']} (pid {entry['pid']})"
        for entry in dashboards.running() if int(entry["pid"]) in held
    ]
    return sorted(holders)


def _refuse_while_held(end: dict) -> bool:
    """Whether processes hold the installation this upgrade would rewrite.

    Only where the host locks a running image: POSIX replaces one without
    complaint, so there is nothing to refuse, and stopping a watcher
    there would cost more than it saves.
    """
    if not hostruntime.locks_running_image():
        return False
    # With nothing long-lived running, nothing can be holding the
    # installation, and there is no reason to pay for `uv tool dir`.
    if not _running_watchers() and not dashboards.running():
        return False
    environment = plugins.tool_environment()
    if environment is None:
        return False
    holders = _holders(environment)
    if not holders:
        return False
    end["status"] = "error"
    end["level"] = "error"
    end["held_by"] = len(holders)
    end["message"] = "installation in use; nothing was changed"
    preflight.emit_failure(
        "upgrade",
        "this installation is in use, so nothing was changed: "
        f"{'; '.join(holders)}. Stop them and run upgrade again "
        "(`agents-live --repo <path> stop <name>`, "
        "`agents-live dashboard --stop`)")
    return True


def _upgrade_runtime(roots: list[Path] | None = None,
                     source: Path | None = None) -> int:
    try:
        uv = find_uv()
    except FileNotFoundError as exc:
        preflight.emit_failure("upgrade", str(exc))
        return 1
    with adminlog.operation("upgrade-runtime",
                            version_before=__version__,
                            source=str(source) if source else "pypi") as end:
        # Before uv is invoked at all: a rebuild it cannot finish leaves
        # the environment neither on the old version nor the new (#231).
        if _refuse_while_held(end):
            return 1
        # The upgrade rewrites this tool's own executables, and on
        # Windows one of them is the running process.
        launcher_before = plugins.launcher_stamp()
        # Read before the install, so every process named afterwards
        # demonstrably predates the runtime that replaced it (#188).
        watchers_before = _running_watchers()
        status = subprocess.run(
            _install_command(uv, source), check=False,
        ).returncode
        kept_launcher = False
        if status != 0:
            if not plugins.only_the_launcher_failed(launcher_before):
                end["status"] = "error"
                end["level"] = "error"
                end["message"] = f"uv install exited {status}"
                return status
            kept_launcher = True
            end["launcher_replaced"] = False
            end["message"] = (
                f"uv install exited {status} after upgrading the runtime; "
                f"the launcher was in use and was left in place")
        _report_stale_watchers(watchers_before, end)
        if kept_launcher:
            _warn_launcher_kept()
        try:
            plugins.converge(roots or [], trigger="upgrade", pin_primary=False)
        except (OSError, ValueError, plugins.PluginError) as exc:
            end["status"] = "error"
            end["level"] = "error"
            end["message"] = str(exc)
            preflight.emit_failure("upgrade", str(exc))
            return 1
        end["version_after"] = plugins.installed_version()
    return 0


def _warn_launcher_kept() -> None:
    """Say the runtime is upgraded but its launcher is the old file.

    Worth saying rather than passing over in silence, because two
    things outlive the upgrade. uv writes its receipt only after the
    launcher lands, so its record of this tool still describes the
    previous install. And processes already running keep the code they
    started with regardless (#188).

    The launcher itself does not carry the version. It selects an
    interpreter and a module, both of which now resolve to the new
    runtime, so keeping the old one costs nothing at the command line.
    """
    print("note: the runtime was upgraded, but its launcher was in use and "
          "could not be replaced; the launcher does not carry the version, "
          "so commands run the new runtime. uv's own record of this tool "
          "stays on the previous install until an upgrade runs with no "
          "agents-live process running",
          file=sys.stderr)


def _running_watchers() -> list[tuple[int, str, str | None]]:
    """Every watcher on this host, or nothing if the host will not say."""
    from .headless import watchers_on_host  # noqa: PLC0415

    try:
        return watchers_on_host()
    except OSError:
        # Enumeration is a courtesy here; an upgrade that worked must
        # not fail over an unreadable process table.
        return []


def _report_stale_watchers(
        before: list[tuple[int, str, str | None]], end: dict) -> None:
    """Name the watchers still running the version just replaced.

    Replacing the runtime does not stop the processes already running
    it, on any host. A running process has its code loaded and keeps
    executing it however the files underneath change: on POSIX the
    replaced file keeps its inode for as long as a process holds it, and
    on Windows the executable a process is running cannot be replaced at
    all while it runs, so uv rebuilds the environment around it. Either
    way the upgrade reports success while every watcher that was running
    carries on with the previous release, which is version skew with
    nothing to connect it to its cause (#188).

    Restarting them is deliberately not done here: it would interrupt a
    watcher mid-dispatch, which is a policy an upgrade should not decide
    on its own.

    Reported per watcher rather than per process: one watcher is more
    than one process on Windows (the shim executes an interpreter, which
    is what the process table shows alongside it), and a count of
    processes would overstate how many agents are affected.
    """
    stale: dict[tuple[str, str | None], list[int]] = {}
    for pid, name, project in before:
        if hostruntime.is_alive(pid):
            stale.setdefault((name, project), []).append(pid)
    end["stale_watchers"] = len(stale)
    if not stale:
        return
    end["stale_watcher_agents"] = ", ".join(
        sorted({name for name, _ in stale}))
    print(f"warning: {len(stale)} watcher(s) are still running the "
          f"previous version and will until restarted:", file=sys.stderr)
    for (name, project), pids in sorted(stale.items(),
                                        key=lambda row: (row[0][0],
                                                         min(row[1]))):
        where = project if project else "project not named on the command line"
        listed = ", ".join(str(pid) for pid in sorted(pids))
        print(f"  {name} (pid {listed}, {where})", file=sys.stderr)
    print("restart each one in its own project: `agents-live --repo <path> "
          "stop <name>` then `agents-live --repo <path> start <name>`",
          file=sys.stderr)


def _refresh_with_installed_cli(*, refresh_skills: bool) -> int:
    # cli_shim_path prefers the entry point beside the interpreter (the
    # uv tool env), so a freshly installed shim is found even when
    # ~/.local/bin is not on PATH yet.
    from .headless import AgentsLiveError, cli_shim_path  # noqa: PLC0415

    try:
        executable = str(cli_shim_path())
    except AgentsLiveError as exc:
        detail = f"agents-live executable not found after runtime upgrade: {exc}"
        if refresh_skills:
            preflight.emit_failure("upgrade", detail)
            return 1
        print(f"warning: could not update shell completions: {detail}",
              file=sys.stderr)
        return 0
    try:
        completion_status = subprocess.run(
            [executable, "completions", "--update"], check=False,
        ).returncode
    except OSError as exc:
        completion_status = None
        print(f"warning: could not update shell completions after runtime "
              f"upgrade: {exc}", file=sys.stderr)
    if completion_status not in (None, 0):
        print("warning: could not update shell completions after runtime "
              f"upgrade (exit {completion_status})", file=sys.stderr)
    if not refresh_skills:
        return 0
    try:
        return subprocess.run(
            [executable, "upgrade", "--skills-only"], check=False,
        ).returncode
    except OSError as exc:
        preflight.emit_failure("upgrade", f"skill refresh failed: {exc}")
        return 1


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Upgrade the runtime and managed project skill payloads")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--runtime-only", action="store_true",
        help="Upgrade the uv tool without refreshing project skill payloads",
    )
    mode.add_argument(
        "--skills-only", action="store_true",
        help="Refresh project skill payloads without upgrading the uv tool",
    )
    # Not in the mode group: --from selects where the runtime comes
    # from, not whether it is installed, so it composes with
    # --runtime-only. Only --skills-only contradicts it.
    parser.add_argument(
        "--from", dest="source", metavar="PATH",
        help="Install the runtime from a local project directory or built "
             "artifact instead of PyPI",
    )
    args = parser.parse_args()
    print(f"Installed agents-live version: {__version__}")

    source: Path | None = None
    if args.source is not None:
        if args.skills_only:
            preflight.emit_failure(
                "upgrade", "--from installs a runtime; it cannot be combined "
                "with --skills-only", code="invalid_arguments")
            return 1
        source = Path(args.source).expanduser()
        if not source.exists():
            preflight.emit_failure(
                "upgrade", f"no such path to install from: {source}",
                code="source_missing")
            return 1
        source = source.resolve()

    try:
        targets, errors = _targets()
        target_roots = [root for _, root in targets]
        if os.environ.get(paths.ENV_VAR, "").strip():
            # Explicit --repo and AGENTS_LIVE_REPO both set this environment
            # value. They narrow payload refresh, but plugins share one
            # host-global tool and still include every registered project.
            for alias, value, error in repos.entries():
                if error:
                    errors.append(f"{alias}: {error}")
                else:
                    target_roots.append(Path(value))
    except (OSError, ValueError) as exc:
        # The message already names its source (registry file vs an
        # invalid AGENTS_LIVE_REPO); no prefix that could mislabel it.
        preflight.emit_failure("upgrade", str(exc))
        return 1

    if not args.skills_only:
        runtime_status = _upgrade_runtime(
            list(dict.fromkeys(target_roots)), source=source)
        if runtime_status != 0:
            return runtime_status
        if args.runtime_only:
            return _refresh_with_installed_cli(refresh_skills=False)
        # After the runtime upgrade this process is still the old
        # version, so payload refresh must run in the freshly installed
        # CLI. One child covers every target: its own `_targets()`
        # resolves the current project and all registered repositories
        # (and honors AGENTS_LIVE_REPO), so per-repo children would only
        # multiply interpreter start-ups.
        return _refresh_with_installed_cli(refresh_skills=True)

    for error in errors:
        print(f"warning: skipping registered repo {error}", file=sys.stderr)

    # Converge the built-in automatic maintenance crontab entries: a
    # runtime upgrade can re-home the pinned shim path they carry. This
    # branch runs in the freshly installed CLI, so the canonical lines
    # are the new install's. Best-effort: no crontab is not fatal.
    try:
        from . import health_check  # noqa: PLC0415
        if health_check.ensure_health_cron_lines():
            print("Converged the automatic maintenance schedule")
    except Exception as exc:
        print(f"warning: could not converge health-check crontab entries: "
              f"{exc}", file=sys.stderr)

    if not targets:
        print("No initialized or registered projects to refresh")
        return 1 if errors else 0

    failed = bool(errors)
    for label, root in targets:
        print(f"Refreshing {label}: {root}")
        try:
            _migrate_triggers(root)
            _refresh_payload(root)
        except (OSError, ValueError) as exc:
            preflight.emit_failure(
                "upgrade", f"{label} ({root}): {exc}")
            failed = True
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())