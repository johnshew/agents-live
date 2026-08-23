"""Clear started intent, then converge the complete runtime."""
from __future__ import annotations

import argparse
import sys

from ... import agent, paths, state
from .. import lifecycle, resolve


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Stop automatic runs for an agent.")
    parser.add_argument("--name", required=True)
    parser.add_argument("--dry-run", "-n", action="store_true")
    args = parser.parse_args(argv)
    root = paths.resolve_root()
    try:
        root, identifier = _select(args.name, root)
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


def _select(name: str, root) -> tuple:
    """The repository and identifier this stop acts on.

    Stopping follows the same resolution as starting: a name the current
    repository does not answer may be started in another registered one,
    and refusing to stop it there would leave automation running with no
    way to withdraw it except by finding the repository by hand (#388).
    """
    snapshot = state.load(root)
    missing = None
    try:
        return root, _resolve(name, snapshot, root)
    except agent.DefinitionNotFound as exc:
        if resolve.repository_pinned():
            raise
        missing = exc
    matches = []
    for candidate in resolve.registered_roots(exclude=root):
        try:
            identifier = _resolve(name, state.load(candidate), candidate)
        except (agent.DefinitionNotFound, state.StartedStateUnavailable):
            continue
        matches.append((candidate, identifier))
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        choices = "\n".join(
            f"  {item}" for item in resolve.qualified_identifiers(matches))
        raise resolve.AmbiguousAgent(
            f"agent '{name}' is ambiguous across registered repositories\n"
            f"{choices}\n"
            "use: agents-live --repo <repository> stop <identifier>")
    if missing is not None:
        raise missing
    raise agent.DefinitionNotFound(f"definition not found: {name}")


def _resolve(name: str, snapshot: state.StartedSnapshot, root) -> str:
    """The started identifier for ``name``, even if its file is gone.

    Stopping is how a user withdraws automation that is misbehaving, so it
    cannot depend on the definition still loading.
    """
    if name in snapshot.agents:
        return name
    try:
        return agent.load(name, root=root).identifier
    except agent.DefinitionError:
        started = sorted(
            item for item in snapshot.agents
            if item.rsplit("-", 1)[0] == name)
        if len(started) == 1:
            return started[0]
        if len(started) > 1:
            raise agent.DefinitionError(
                f"'{name}' is ambiguous; stop one of: {', '.join(started)}"
            ) from None
        raise


if __name__ == "__main__":
    raise SystemExit(main())
