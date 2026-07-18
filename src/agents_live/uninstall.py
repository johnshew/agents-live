"""Remove host integrations before uninstalling the uv-managed tool."""
from __future__ import annotations

import argparse
import shutil
import subprocess

from . import heartbeat


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Uninstall agents-live safely")
    parser.add_argument("--distro")
    parser.add_argument("--retain-state", action="store_true")
    args = parser.parse_args(argv)
    try:
        heartbeat.uninstall(args.distro, retain_state=args.retain_state)
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
        selected = args.distro or "<name>"
        print(
            f"error: host cleanup failed; agents-live remains installed: {exc}\n"
            "recovery: "
            f"uvx agents-live heartbeat uninstall --distro {selected}",
            file=__import__("sys").stderr)
        return 1
    uv = shutil.which("uv")
    if not uv:
        print(
            "error: host cleanup succeeded, but uv was not found; run "
            "`uv tool uninstall agents-live`",
            file=__import__("sys").stderr)
        return 1
    completed = subprocess.run([uv, "tool", "uninstall", "agents-live"], check=False)
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
