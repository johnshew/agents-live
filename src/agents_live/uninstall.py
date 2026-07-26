"""Remove host integrations before uninstalling the uv-managed tool."""
from __future__ import annotations

import argparse
import os
import shlex
import subprocess
import sys

from . import (adminlog, completions, health_check, heartbeat, hostruntime,
               preflight)
from .spawn import find_uv


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Uninstall agents-live safely")
    parser.add_argument("--distro")
    parser.add_argument("--retain-state", action="store_true")
    args = parser.parse_args(argv)
    adminlog.record("uninstall", status="start",
                    retain_state=args.retain_state)
    if hostruntime.id() == hostruntime.WSL:
        try:
            heartbeat.uninstall(args.distro, retain_state=args.retain_state)
        except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
            selected = (
                args.distro or os.environ.get("WSL_DISTRO_NAME")
                or "<your-distro-name>")
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
    completed = subprocess.run([uv, "tool", "uninstall", "agents-live"], check=False)
    adminlog.record("uninstall",
                    status="ok" if not completed.returncode else "error",
                    level=None if not completed.returncode else "error",
                    exit_code=completed.returncode)
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
