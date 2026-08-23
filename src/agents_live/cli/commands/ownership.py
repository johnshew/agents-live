"""Explicit project ownership enablement and status."""
from __future__ import annotations

import argparse
import json
import os
import sys

from ... import paths
from ...state import ownership
from . import init


def _status(root) -> tuple[str, str, bool]:
    try:
        mode = ownership.mode(root)
        if mode == "local":
            return (
                "local",
                "local-only; cross-machine assignment is not enabled",
                True,
            )
        ownership.validate_registry(root, rate_limit_secs=10**9)
    except (ownership.OwnershipUnavailableError, ValueError) as exc:
        return ("registry-unavailable", f"registry declared but unavailable: {exc}", False)
    return ("registry", "registry enabled", True)


def _emit(root, *, enabled: bool | None = None) -> int:
    status, detail, ok = _status(root)
    payload = {
        "ok": ok,
        "ownership": status,
        "detail": detail,
        "repository": str(root),
    }
    if enabled is not None:
        payload["enabled"] = enabled
    if os.environ.get("AGENTS_LIVE_JSON") == "1":
        print(json.dumps(payload))
    else:
        print(detail)
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Manage optional cross-machine ownership")
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser(
        "status", help="Report local, registry enabled, or unavailable state")
    subcommands.add_parser(
        "enable", help="Explicitly enable registry ownership for this project")
    args = parser.parse_args(argv)
    root = paths.resolve_root()

    if args.command == "status":
        return _emit(root)

    try:
        ownership.mode(root)
        ownership.validate_registry(root)
        changed = init.declare_ownership(root, "registry")
    except (ownership.OwnershipUnavailableError, OSError, ValueError) as exc:
        print(f"ownership enable refused: {exc}", file=sys.stderr)
        return 1
    if os.environ.get("AGENTS_LIVE_JSON") == "1":
        return _emit(root, enabled=changed)
    print(
        "Registry ownership enabled."
        if changed else "Registry ownership already enabled."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
