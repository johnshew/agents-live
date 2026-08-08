"""Clear started intent, then converge the complete runtime."""
from __future__ import annotations

import argparse
import sys

from . import lifecycle, paths, state


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Stop automatic runs for an agent.")
    parser.add_argument("--name", required=True)
    parser.add_argument("--dry-run", "-n", action="store_true")
    args = parser.parse_args(argv)
    root = paths.resolve_root()
    try:
        result = lifecycle.converge(
            removals={root: {args.name}}, dry_run=args.dry_run)
    except (lifecycle.CollectionUnavailable,
            state.StartedStateUnavailable, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    verb = "Would stop" if args.dry_run else "Stopped"
    print(f"{verb} '{args.name}'.")
    for operation, message in result.failed:
        print(f"{operation.key}: {message}", file=sys.stderr)
    return 1 if result.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
