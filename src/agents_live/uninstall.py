"""Remove host integrations before uninstalling the uv-managed tool."""
from __future__ import annotations

import argparse
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

from . import (adminlog, completions, health_check, heartbeat, hostruntime,
               preflight)
from .spawn import find_uv

# Long enough for a watcher to finish the dispatch it is in, short enough
# that an uninstall does not appear to hang.
_WATCHER_GRACE_S = 5
_TOOL_DIR_TIMEOUT_S = 15


def _handoff_windows_uninstall(uv: str, environment: Path) -> bool:
    """Run uv after every process executing from *environment* exits.

    The console shim waits for this Python process, and both executables
    live inside the directory uv removes. A helper from outside that
    directory has to outlive both of them; waiting only for this PID races
    the shim's own exit.
    """
    powershell = (shutil.which("powershell.exe")
                  or shutil.which("pwsh.exe"))
    if powershell is None:
        return False
    escaped_environment = str(environment).replace("'", "''")
    escaped_uv = uv.replace("'", "''")
    script = (
        f"$root = '{escaped_environment}'; "
        "do { "
        "$running = @(Get-Process -ErrorAction SilentlyContinue | "
        "Where-Object { try { $_.Path -and "
        "$_.Path.StartsWith($root, "
        "[System.StringComparison]::OrdinalIgnoreCase) } "
        "catch { $false } }); "
        "if ($running.Count) { Start-Sleep -Milliseconds 100 } "
        "} while ($running.Count); "
        f"& '{escaped_uv}' tool uninstall agents-live; "
        "exit $LASTEXITCODE"
    )
    try:
        hostruntime.spawn_detached(
            [powershell, "-NoProfile", "-NonInteractive", "-Command", script],
            stdin=subprocess.DEVNULL, stdout=None, stderr=None)
    except OSError:
        return False
    print("Uninstall will complete after this command exits")
    return True


def _tool_environment() -> Path | None:
    """Where uv keeps this tool, or ``None`` if it will not say.

    Asked of uv rather than derived from ``sys.prefix``: uninstall can be
    run from an ephemeral ``uvx`` environment, which is not the
    installation being removed.
    """
    try:
        uv = find_uv()
        completed = subprocess.run(
            [uv, "tool", "dir"], capture_output=True, text=True,
            check=True, timeout=_TOOL_DIR_TIMEOUT_S)
    except (FileNotFoundError, OSError, subprocess.SubprocessError):
        return None
    environment = Path(completed.stdout.strip()) / "agents-live"
    return environment if environment.is_dir() else None


def _stop_own_watchers() -> list[tuple[int, str, str | None]]:
    """Stop the watchers running out of this installation; name survivors.

    A watcher holds the executables uv has to delete, so on Windows it
    fails the removal outright, and it would do so after the host cleanup
    below had already run (#219). Only processes running out of the tool
    environment are stopped: those are provably this installation's, and
    they are exactly the ones that block it. A watcher started from a
    checkout is somebody's working tree and is left alone.
    """
    from .headless import watchers_on_host  # noqa: PLC0415

    environment = _tool_environment()
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Uninstall agents-live safely")
    parser.add_argument("--distro")
    parser.add_argument("--retain-state", action="store_true")
    args = parser.parse_args(argv)
    adminlog.record("uninstall", status="start",
                    retain_state=args.retain_state)
    # Before any host cleanup: what this fails on has to leave a working
    # installation, not a stripped host and a half-removed tool (#219).
    survivors = _stop_own_watchers()
    if survivors:
        named = ", ".join(f"{name} (pid {pid})" for pid, name, _ in survivors)
        preflight.emit_failure(
            "uninstall",
            f"watchers still running from this installation: {named}; "
            "nothing was removed; stop them and run uninstall again")
        return 1
    if hostruntime.id() == hostruntime.WSL:
        try:
            heartbeat.uninstall(args.distro, retain_state=args.retain_state)
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
    # After host cleanup succeeded (never before: a failed uninstall must
    # not strand an installed tool without its check-and-repair loop).
    try:
        if health_check.remove_health_cron_lines():
            print("Removed the check-and-repair loop from this host")
    except Exception as exc:
        print(f"warning: could not remove the check-and-repair loop: "
              f"{exc}", file=sys.stderr)
    try:
        for path in completions.remove():
            print(f"Removed shell completions: {path}")
    except OSError as exc:
        print(f"warning: could not remove shell completions: {exc}",
              file=sys.stderr)
    try:
        uv = find_uv()
    except FileNotFoundError:
        preflight.emit_failure(
            "uninstall",
            "host cleanup succeeded, but uv was not found; restore or install "
            "uv, then run `uv tool uninstall agents-live`")
        return 1
    environment = (_tool_environment()
                   if hostruntime.id() == hostruntime.WINDOWS else None)
    if environment is not None:
        if not _handoff_windows_uninstall(uv, environment):
            preflight.emit_failure(
                "uninstall",
                "host cleanup succeeded, but the Windows uninstall helper "
                "could not start; run `uv tool uninstall agents-live` after "
                "this command exits")
            return 1
        adminlog.record("uninstall", status="ok", deferred=True)
        return 0
    completed = subprocess.run([uv, "tool", "uninstall", "agents-live"], check=False)
    adminlog.record("uninstall",
                    status="ok" if not completed.returncode else "error",
                    level=None if not completed.returncode else "error",
                    exit_code=completed.returncode)
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
