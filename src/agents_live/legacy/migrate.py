"""One-major-cycle conversion of pre-6.0 trigger artifacts."""
from __future__ import annotations

import argparse
import json
import shlex
import sys
from pathlib import Path

from .. import agent, paths, preflight
from ..cli import lifecycle
from ..obs import admin as adminlog
from ..runtime.hosts import crontab, system as hostruntime, task_scheduler


def _store():
    if hostruntime.native_scheduler() == hostruntime.TASK_SCHEDULER:
        return task_scheduler
    return crontab


def _store_call(action):
    try:
        return action()
    except task_scheduler.TaskError as exc:
        raise RuntimeError(str(exc)) from exc


def persisted_roots() -> list[Path]:
    """Existing roots named only by pre-6.0 trigger artifacts."""
    return _store_call(lambda: _store().persisted_roots())


def remove_under(environment: Path) -> int:
    """Remove pre-6.0 triggers tied to an installation being removed."""
    removed = _store_call(lambda: _store().remove_under(environment))
    if removed:
        adminlog.record(
            "schedule-sweep",
            count=removed,
            scheduler=hostruntime.native_scheduler(),
        )
    return removed


def _token_pair_value(line: str, flag: str) -> str | None:
    try:
        tokens = shlex.split(line)
    except ValueError:
        tokens = line.split()
    for first, second in zip(tokens, tokens[1:]):
        if first == flag:
            return second
    return None


def _line_belongs_to_root(line: str, root: Path) -> bool:
    try:
        tokens = shlex.split(line)
    except ValueError:
        tokens = line.split()
    roots = [
        second
        for first, second in zip(tokens, tokens[1:])
        if first in {"cd", "--repo"}
    ]
    return bool(roots) and all(Path(candidate) == root for candidate in roots)


def _legacy_watchers(root: Path) -> list[tuple[int, str]]:
    found: list[tuple[int, str]] = []
    for pid, command in hostruntime.process_command_lines():
        args = hostruntime.split_command_line(command)
        if "--runtime-role" in args:
            continue
        if not any(
            argument in {"watch-loop", "--watch-loop"}
            for argument in args
        ):
            continue
        explicit = next(
            (
                second
                for first, second in zip(args, args[1:])
                if first == "--repo"
            ),
            None,
        )
        name = next(
            (
                second
                for first, second in zip(args, args[1:])
                if first in {"watch-loop", "--watch-loop"}
            ),
            None,
        )
        if name is None:
            continue
        if explicit is not None:
            belongs = Path(explicit) == root
        else:
            belongs = any(
                bool(argument) and root in Path(argument).parents
                for argument in args
            )
        if belongs:
            found.append((pid, name))
    return found


def _result(
    result,
    *,
    dry_run: bool,
    rewrites: int | None = None,
    unmatched: list[str] | None = None,
) -> int:
    failed = [
        {"operation": operation.kind, "key": operation.key, "detail": detail}
        for operation, detail in result.failed
    ]
    payload = {
        "ok": not failed,
        "dry_run": dry_run,
        "rewrites": rewrites if rewrites is not None else sum(
            operation.kind == "remove-legacy" for operation in result.done
        ),
        "changes": len(result.done),
        "failures": failed,
    }
    if unmatched is not None:
        payload["unmatched"] = unmatched
    if preflight.json_mode():
        print(json.dumps(payload))
    elif failed:
        for item in failed:
            print(
                f"{item['operation']} {item['key']}: {item['detail']}",
                file=sys.stderr,
            )
    else:
        verb = "Would converge" if dry_run else "Converged"
        print(f"{verb} {payload['changes']} trigger operation(s).")
    return 1 if failed else 0


def _migrate(root: Path, *, dry_run: bool) -> int:
    old_watchers = [] if dry_run else _legacy_watchers(root)
    try:
        result = lifecycle.converge(
            selected_roots=(root,),
            dry_run=dry_run,
        )
    except lifecycle.CollectionUnavailable as exc:
        if preflight.json_mode():
            print(json.dumps({"ok": False, "error": str(exc)}))
        else:
            print(str(exc), file=sys.stderr)
        return 1
    code = _result(result, dry_run=dry_run)
    if code != 0 or dry_run:
        return code
    removed = {
        operation.key.removeprefix(f"{root}:")
        for operation in result.done
        if operation.kind == "remove-legacy"
        and operation.key.startswith(f"{root}:")
    }
    for pid, name in old_watchers:
        if name in removed:
            hostruntime.terminate(pid)
    return 0


def _adoption_candidates(
    lines: list[str], old_root: Path, root: Path,
) -> tuple[set[str], set[str], list[str]]:
    discovery = agent.discover(root)
    identities: dict[str, list[agent.AgentSpec]] = {}
    for spec in discovery.specs:
        identities.setdefault(spec.name, []).append(spec)
        identities.setdefault(spec.identifier, []).append(spec)
    identifiers: set[str] = set()
    matched: set[str] = set()
    unmatched: list[str] = []
    for line in lines:
        if not _line_belongs_to_root(line, old_root):
            continue
        schedule_name = _token_pair_value(line, "--name")
        watcher_name = (
            _token_pair_value(line, "ensure-watcher")
            or _token_pair_value(line, "--ensure-watcher")
        )
        name = schedule_name or watcher_name
        candidates = identities.get(name or "", [])
        candidates = list({spec.identifier: spec for spec in candidates}.values())
        if len(candidates) != 1:
            unmatched.append(line)
            continue
        spec = candidates[0]
        config = spec.execution
        has_trigger = bool(
            config
            and (
                (schedule_name and config.schedules)
                or (watcher_name and config.watch)
            )
        )
        if not has_trigger:
            unmatched.append(line)
            continue
        identifiers.add(spec.identifier)
        matched.add(line)
    return identifiers, matched, unmatched


def _adopt(root: Path, old_root: Path, *, dry_run: bool) -> int:
    if hostruntime.native_scheduler() == hostruntime.TASK_SCHEDULER:
        print(
            "--adopt is unavailable for Task Scheduler artifacts; run "
            "`agents-live start --all` from the new location",
            file=sys.stderr,
        )
        return 1
    if old_root.exists():
        print(
            f"cannot adopt {old_root}: the old project root still exists",
            file=sys.stderr,
        )
        return 1
    lines = crontab.lines()
    if lines is None:
        print("crontab is not accessible", file=sys.stderr)
        return 1
    identifiers, matched, unmatched = _adoption_candidates(
        lines, old_root, root)
    if not preflight.json_mode():
        for line in unmatched:
            print(f"Unmatched old-root entry left unchanged:\n  {line}")
    if not identifiers:
        if preflight.json_mode():
            print(json.dumps({
                "ok": True,
                "dry_run": dry_run,
                "rewrites": 0,
                "unmatched": unmatched,
            }))
        elif not unmatched:
            print("No matching old-root entries to adopt.")
        return 0
    old_watchers = [] if dry_run else _legacy_watchers(old_root)
    try:
        result = lifecycle.converge(
            additions={root: identifiers},
            selected_roots=(root,),
            dry_run=dry_run,
        )
    except lifecycle.CollectionUnavailable as exc:
        print(str(exc), file=sys.stderr)
        return 1
    code = _result(
        result,
        dry_run=dry_run,
        rewrites=len(matched),
        unmatched=unmatched,
    )
    if code != 0 or dry_run:
        return code
    with crontab.lock():
        current = crontab.lines()
        if current is None:
            print("crontab became inaccessible during adoption", file=sys.stderr)
            return 1
        crontab.write([line for line in current if line not in matched])
    for pid, name in old_watchers:
        if name in identifiers or any(
                spec.name == name and spec.identifier in identifiers
                for spec in agent.discover(root).specs):
            hostruntime.terminate(pid)
    adminlog.record(
        "trigger-adopt",
        old_root=str(old_root),
        rewrites=len(matched),
        agents=sorted(identifiers),
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Converge pre-6.0 triggers into current subscriptions.")
    parser.add_argument("--dry-run", "-n", action="store_true")
    parser.add_argument("--adopt", metavar="OLD_ROOT")
    args = parser.parse_args(argv)
    try:
        root = paths.resolve_root()
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    if args.adopt:
        old_root = Path(args.adopt).expanduser().resolve()
        return _adopt(root, old_root, dry_run=args.dry_run)
    return _migrate(root, dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())