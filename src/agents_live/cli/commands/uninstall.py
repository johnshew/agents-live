"""Remove host integrations before uninstalling the self-managed runtime."""
from __future__ import annotations

import argparse
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

from ... import deploy, preflight, runtime
from ...obs import admin as adminlog
from ...legacy import migrate as legacy_migration
from ...runtime.hosts.processes import watchers_on_host
from ...runtime.hosts import wsl_liveness
from ...runtime.hosts import system as hostruntime
from . import completions

# Long enough for a watcher to finish the dispatch it is in, short enough
# that an uninstall does not appear to hang.
_WATCHER_GRACE_S = 5


def _remove_command_exposure(root: Path) -> None:
    """Remove only PATH entries and symlinks that point at this install root."""
    hostruntime.remove_user_path_directory(deploy.layout.command_root(root))
    link_root = Path.home() / ".local" / "bin"
    for name in ("agents-live", "al"):
        link = link_root / name
        try:
            if link.is_symlink() and link.resolve() == deploy.layout.command_path(
                    name, root).resolve():
                link.unlink()
        except OSError:
            continue


def _remove_self_managed(root: Path) -> bool:
    """Remove the owned root now, or queue removal after Windows exits."""
    _remove_command_exposure(root)
    if hostruntime.id() != hostruntime.WINDOWS:
        shutil.rmtree(root)
        print(f"Removed self-managed installation: {root}")
        return True
    if not hostruntime.defer_remove_tree(root):
        return False
    print("Self-managed uninstall will complete after this command exits")
    return True


def _stop_own_watchers(environment: Path | None
                       ) -> list[tuple[int, str, str | None]]:
    """Stop the watchers running out of this installation; name survivors.

    A watcher holds the executables uv has to delete, so on Windows it
    fails the removal outright, and it would do so after the host cleanup
    below had already run (#219). Only processes running out of the tool
    environment are stopped: those are provably this installation's, and
    they are exactly the ones that block it. A watcher started from a
    checkout is somebody's working tree and is left alone.
    """
    if environment is None:
        return []
    try:
        watchers = watchers_on_host(under=environment)
    except OSError:
        # An unreadable process table is not a reason to refuse; the
        # removal below still reports whatever it cannot delete.
        return []
    for pid, name, project in watchers:
        hostruntime.terminate(pid, grace_s=_WATCHER_GRACE_S)
        where = f" in {project}" if project else ""
        print(f"Stopped watcher '{name}'{where} (pid {pid})")
    return [watcher for watcher in watchers
            if hostruntime.is_alive(watcher[0])]


def _sweep_triggers(environment: Path | None) -> None:
    """Withdraw every host trigger that fires out of this installation.

    Per-agent triggers outlive the command that made them and are not
    addressed to the project uninstall happens to be run from, so
    nothing else withdraws them: left behind, they keep firing at an
    executable that is gone, failing on schedule forever (#219).

    A failure here is reported and not fatal. The triggers are inert
    once the tool goes, and refusing to finish would leave the worse
    state: an installation half removed.
    """
    if environment is None:
        print("warning: could not locate this installation, so its scheduled "
              "triggers were left in place; remove them with `agents-live "
              "stop --all` before uninstalling", file=sys.stderr)
        return
    try:
        removed = legacy_migration.remove_under(environment)
    except Exception as exc:
        print(f"warning: could not withdraw scheduled triggers: {exc}",
              file=sys.stderr)
        return
    if removed:
        print(f"Withdrew {removed} scheduled trigger(s) from this host")


def _sweep_runtime() -> None:
    """Withdraw every structured 6.0 artifact owned by this runtime."""
    try:
        host = runtime.current()
        watchers = host.supervisor.owned(role="watcher")
        for watcher in watchers:
            host.supervisor.terminate(watcher)
        removed = host.trigger_store.clear()
    except Exception as exc:
        print(f"warning: could not withdraw runtime artifacts: {exc}",
              file=sys.stderr)
        return
    if watchers:
        print(f"Stopped {len(watchers)} runtime watcher(s)")
    if removed:
        print(f"Withdrew {removed} runtime trigger(s) from this host")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Uninstall agents-live safely")
    parser.add_argument("--distro")
    parser.add_argument("--retain-state", action="store_true")
    args = parser.parse_args(argv)
    adminlog.record("uninstall", status="start",
                    retain_state=args.retain_state)
    installation = deploy.ownership.describe()
    refusal = deploy.ownership.refusal(installation, action="uninstall")
    if refusal is not None:
        preflight.emit_failure(
            "uninstall",
            refusal)
        return 1
    environment = installation.root
    # Before any host cleanup: what this fails on has to leave a working
    # installation, not a stripped host and a half-removed tool (#219).
    survivors = _stop_own_watchers(environment)
    if survivors:
        named = ", ".join(f"{name} (pid {pid})" for pid, name, _ in survivors)
        preflight.emit_failure(
            "uninstall",
            f"watchers still running from this installation: {named}; "
            "runtime artifacts remain installed; stop them and run "
            "uninstall again")
        return 1
    runtime_id = hostruntime.id()
    if runtime_id == hostruntime.WSL:
        try:
            wsl_liveness.uninstall(args.distro, retain_state=args.retain_state)
        except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
            # The runtime name is the distro name on WSL, except where
            # the distro did not say what it is called.
            named = hostruntime.runtime_name()
            selected = args.distro or (
                "<your-distro-name>" if named == hostruntime.WSL else named)
            preflight.emit_failure(
                "uninstall",
                "host cleanup failed; agents-live remains installed: "
                f"{exc}; recovery: uvx agents-live heartbeat uninstall "
                f"--distro {shlex.quote(selected)}")
            return 1
    else:
        # Only WSL keeps a Windows-side heartbeat task; every other host
        # runs its own triggers, which the loop removal below withdraws.
        # A hard dependency here would make uninstall impossible off WSL.
        print("no cross-host integrations to remove; uninstalling the tool")
    _sweep_runtime()
    _sweep_triggers(environment)
    try:
        for path in completions.remove():
            print(f"Removed shell completions: {path}")
    except OSError as exc:
        print(f"warning: could not remove shell completions: {exc}",
              file=sys.stderr)
    if not _remove_self_managed(installation.root):
        preflight.emit_failure(
            "uninstall",
            "the Windows uninstall helper could not start; the "
            "self-managed installation remains installed")
        return 1
    adminlog.record(
        "uninstall", status="ok", deferred=runtime_id == hostruntime.WINDOWS)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
