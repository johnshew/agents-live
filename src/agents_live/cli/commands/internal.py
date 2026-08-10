"""Internal argv ingress for durable runtime artifacts."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from ... import agent, paths, runtime
from ...dispatch import Firing, dispatch
from ...runtime.grammars import parse_watch
from ...runtime.watchloop import run as run_watchloop
from .. import lifecycle


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
    install_liveness = commands.add_parser("install-liveness")
    install_liveness.add_argument("--distro")
    args = parser.parse_args(argv)
    if args.command == "liveness":
        from ...runtime.hosts.wsl_liveness import run_once
        return run_once()
    if args.command == "install-liveness":
        from ...runtime.hosts.wsl_liveness import install
        install(args.distro)
        return 0
    if args.command == "maintain":
        return _maintain(dry_run=args.dry_run)
    return _watch(args)


def _maintain(*, dry_run: bool) -> int:
    try:
        result = lifecycle.converge(dry_run=dry_run)
        collected = lifecycle.collect(persist=False)
    except lifecycle.CollectionUnavailable as exc:
        print(str(exc), file=sys.stderr)
        return 1
    if result.failed or not result.health.healthy:
        for operation, detail in result.failed:
            print(f"{operation.key}: {detail}", file=sys.stderr)
        return 1
    if dry_run:
        return 0
    watchers = {
        item.target for item in collected.subscriptions
        if item.kind == "watch" and item.target != "runtime"
    }
    clocks = {
        item.target for item in collected.subscriptions
        if item.kind == "schedule" and item.target != "runtime"
    }
    repositories = {
        item.scope.removeprefix("repo:")
        for item in collected.subscriptions
        if item.scope.startswith("repo:")
    }
    payload = {
        "status": "healthy",
        "ts": datetime.now(timezone.utc).isoformat(),
        "watchers": len(watchers),
        "cron": len(clocks),
        "repos": {root: {"status": "ok"} for root in sorted(repositories)},
    }
    previous = _health_beacon()
    if isinstance(previous.get("smoketest"), dict):
        payload["smoketest"] = previous["smoketest"]
    paths.atomic_write_text(
        paths.health_beacon_path(), json.dumps(payload, indent=2) + "\n")
    return 0


def _health_beacon() -> dict:
    try:
        payload = json.loads(paths.health_beacon_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


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
