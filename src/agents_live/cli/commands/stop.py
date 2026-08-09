"""Clear started intent, then converge the complete runtime."""
from __future__ import annotations

import argparse
import sys

from ... import agent, paths, state
from .. import lifecycle


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Stop automatic runs for an agent.")
    parser.add_argument("--name", required=True)
    parser.add_argument("--dry-run", "-n", action="store_true")
    args = parser.parse_args(argv)
    root = paths.resolve_root()
    try:
        snapshot = state.load(root)
        identifier = args.name if args.name in snapshot.agents else (
            agent.load(args.name, root=root).identifier)
        result = lifecycle.converge(
            removals={root: {identifier}}, dry_run=args.dry_run)
    except (agent.DefinitionError, lifecycle.CollectionUnavailable,
            state.StartedStateUnavailable, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    verb = "Would stop" if args.dry_run else "Stopped"
    print(f"{verb} '{args.name}' ({identifier}).")
    for operation, message in result.failed:
        print(f"{operation.key}: {message}", file=sys.stderr)
    return 1 if result.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
