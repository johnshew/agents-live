"""Internal argv ingress for durable runtime artifacts."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import agent, lifecycle, paths, runtime
from .dispatch import Firing, dispatch
from .runtime.grammars import parse_watch
from .runtime.watchloop import run as run_watchloop


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    watch = commands.add_parser("watch-loop")
    watch.add_argument("name")
    watch.add_argument("--watch-expression")
    watch.add_argument("--runtime-role")
    watch.add_argument("--subscription-key", default="")
    watch.add_argument("--subscription-fingerprint")
    watch.add_argument("--artifact-marker")
    maintain = commands.add_parser("maintain")
    maintain.add_argument("--quiet", action="store_true")
    maintain.add_argument("--dry-run", action="store_true")
    commands.add_parser("liveness")
    args = parser.parse_args(argv)
    if args.command == "liveness":
        from .heartbeat import run_once
        return run_once()
    if args.command == "maintain":
        try:
            result = lifecycle.converge(dry_run=args.dry_run)
        except lifecycle.CollectionUnavailable as exc:
            print(str(exc), file=sys.stderr)
            return 1
        return 1 if result.failed else 0
    return _watch(args)


def _watch(args) -> int:
    root = paths.resolve_root()
    try:
        spec = agent.load(args.name, root=root)
        if spec.execution is None or not spec.execution.watch:
            raise agent.DefinitionError(f"'{args.name}' has no watch expression")
        expression = args.watch_expression or spec.execution.watch
        watch = parse_watch(expression)
        roots = _roots(root, watch.includes)
        source = runtime.current().change_source(
            [str(item) for item in roots])
        if source is None:
            raise RuntimeError("this host does not support file watching")
        run_watchloop(
            source,
            watch,
            root=root,
            fire=lambda changed: dispatch(Firing(
                args.name,
                str(root),
                "watch",
                args.subscription_key,
                changed,
            )),
        )
    except (agent.DefinitionError, RuntimeError, OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


def _roots(root: Path, includes: tuple[str, ...]) -> tuple[Path, ...]:
    found: set[Path] = set()
    for pattern in includes:
        parts = []
        for part in Path(pattern).parts:
            if any(char in part for char in "*?["):
                break
            parts.append(part)
        candidate = root.joinpath(*parts)
        found.add(candidate if candidate.is_dir() else candidate.parent)
    return tuple(sorted(found))


if __name__ == "__main__":
    raise SystemExit(main())
