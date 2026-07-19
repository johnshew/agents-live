#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["PyYAML"]
# ///
"""agents-live migrate - converge persisted trigger entries to the
canonical invocation form (Phase 5 core; §5.2 F3).

Scope: crontab schedule lines (``--name <agent>``) and @reboot watcher
respawn lines (``ensure-watcher <agent>`` or its legacy flag form) that
reference THIS project
(the crontab is host-global; other projects' lines are never touched).
Every entry is compared against what activation would write today -
``activate.build_cron_lines`` / ``headless.build_reboot_watcher_line`` -
so migrate always converges entries to the running context's form: the
script-path form in the flat checkout, the pinned-shim + ``--repo`` form
once installed as a package (§3.4.2). This is what retires stale
``uv run .../scripts/*.py`` lines at the F7 flip.

A running watcher whose respawn line was rewritten is restarted so its
in-memory dispatch matches the new entry. Entries for agents that no
longer exist are reported and left alone - orphan pruning stays
``start --prune-orphans`` / the health check's job.

``--dry-run`` prints the plan without mutating anything.
"""
from __future__ import annotations

import argparse
import json
import shlex
import sys
from pathlib import Path

from . import headless, preflight
from .headless import (
    AgentsLiveError,
    build_reboot_watcher_line,
    cron_line_matches,
    find_watcher_pid,
    install_watcher_reboot_line,
    stop_watcher,
    agent_file_exists,
)


def _token_pair_value(line: str, flag: str) -> str | None:
    """The value following *flag* in a crontab line, token-exact."""
    try:
        tokens = shlex.split(line)
    except ValueError:
        tokens = line.split()
    for first, second in zip(tokens, tokens[1:]):
        if first == flag:
            return second
    return None


def plan_migration(lines: list[str]) -> dict:
    """Pure planning core: compare this project's entries against the
    canonical forms. Returns ``{"schedule": {name: (old, new)},
    "watcher": {name: (old, new)}, "missing": [name, ...]}`` where old/new
    are line lists (old == new entries are omitted)."""
    from . import activate

    schedule_names: set[str] = set()
    watcher_names: set[str] = set()
    for line in lines:
        if not headless.crontab_line_belongs_to_repo(line):
            continue
        name = _token_pair_value(line, "--name")
        if name:
            schedule_names.add(name)
        watcher = (
            _token_pair_value(line, "ensure-watcher")
            or _token_pair_value(line, "--ensure-watcher")
        )
        if watcher:
            watcher_names.add(watcher)

    plan: dict = {"schedule": {}, "watcher": {}, "missing": []}
    for name in sorted(schedule_names):
        if not agent_file_exists(name):
            plan["missing"].append(name)
            continue
        old = [l for l in lines if cron_line_matches(l, name)]
        try:
            new = activate.build_cron_lines(name)
        except AgentsLiveError:
            # Defined but currently unloadable/scheduleless: leave alone,
            # report as missing-from-migration rather than guessing.
            plan["missing"].append(name)
            continue
        if sorted(old) != sorted(new):
            plan["schedule"][name] = (old, new)
    for name in sorted(watcher_names):
        if not agent_file_exists(name):
            if name not in plan["missing"]:
                plan["missing"].append(name)
            continue
        old = [l for l in lines
               if headless.crontab_line_belongs_to_repo(l)
               and headless._reboot_watcher_line_agent_name(l) == name]
        new = [build_reboot_watcher_line(name)]
        if sorted(old) != sorted(new):
            plan["watcher"][name] = (old, new)
    return plan


def _line_belongs_to_root(line: str, root: Path) -> bool:
    """Whether *line* carries an exact ``cd`` or ``--repo`` root token."""
    try:
        tokens = shlex.split(line)
    except ValueError:
        tokens = line.split()
    value = str(root)
    roots = [
        second
        for first, second in zip(tokens, tokens[1:])
        if first in {"cd", "--repo"}
    ]
    return bool(roots) and all(candidate == value for candidate in roots)


def plan_adoption(lines: list[str], old_root: Path) -> dict:
    """Plan canonical replacements for trigger entries from *old_root*."""
    from . import activate

    candidates = [line for line in lines if _line_belongs_to_root(line, old_root)]
    schedule: dict[str, list[str]] = {}
    watcher: dict[str, list[str]] = {}
    unmatched: list[str] = []
    for line in candidates:
        name = _token_pair_value(line, "--name")
        watcher_name = (
            _token_pair_value(line, "ensure-watcher")
            or _token_pair_value(line, "--ensure-watcher")
        )
        if name:
            schedule.setdefault(name, []).append(line)
        elif watcher_name:
            watcher.setdefault(watcher_name, []).append(line)
        else:
            unmatched.append(line)

    plan: dict = {"schedule": {}, "watcher": {}, "unmatched": unmatched}
    for name, old in sorted(schedule.items()):
        if not agent_file_exists(name):
            plan["unmatched"].extend(old)
            continue
        try:
            new = activate.build_cron_lines(name)
        except AgentsLiveError:
            plan["unmatched"].extend(old)
            continue
        plan["schedule"][name] = (old, new)
    for name, old in sorted(watcher.items()):
        if not agent_file_exists(name):
            plan["unmatched"].extend(old)
            continue
        plan["watcher"][name] = (old, [build_reboot_watcher_line(name)])
    return plan


def _apply_adoption(lines: list[str], plan: dict) -> list[str]:
    replaced = {
        line
        for kind in ("schedule", "watcher")
        for old, _new in plan[kind].values()
        for line in old
    }
    canonical = [
        line
        for kind in ("schedule", "watcher")
        for _old, new in plan[kind].values()
        for line in new
    ]
    return [line for line in lines if line not in replaced] + canonical


def _print_adoption(plan: dict, *, dry_run: bool) -> int:
    verb = "Would adopt" if dry_run else "Adopting"
    for line in plan["unmatched"]:
        print(f"Unmatched old-root entry left unchanged:\n  {line}")
    for kind, label in (("schedule", "schedule"), ("watcher", "@reboot watcher")):
        for name, (old, new) in plan[kind].items():
            print(f"{verb} {label} entries for '{name}':")
            for line in old:
                print(f"  - {line}")
            for line in new:
                print(f"  + {line}")
    return len(plan["schedule"]) + len(plan["watcher"])


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Converge persisted cron/watcher entries to the "
                    "canonical invocation form.")
    parser.add_argument("--dry-run", "-n", action="store_true",
                        help="Print the plan without mutating anything.")
    parser.add_argument(
        "--adopt", metavar="OLD_ROOT",
        help="Adopt trigger entries from a moved, nonexistent project root.")
    args = parser.parse_args()

    if args.adopt:
        old_root = Path(args.adopt).expanduser().resolve()
        if old_root.exists():
            raise AgentsLiveError(
                f"cannot adopt {old_root}: the old project root still exists")
        if args.dry_run:
            lines = headless.current_crontab_lines()
            if lines is None:
                raise AgentsLiveError("crontab is not accessible")
            plan = plan_adoption(lines, old_root)
        else:
            with headless.crontab_lock():
                lines = headless.current_crontab_lines()
                if lines is None:
                    raise AgentsLiveError("crontab is not accessible")
                plan = plan_adoption(lines, old_root)
                rewritten = _apply_adoption(lines, plan)
                if rewritten != lines:
                    headless.install_crontab(rewritten)
        rewrites = _print_adoption(plan, dry_run=args.dry_run)
        if rewrites == 0:
            print("No matching old-root entries to adopt.")
        else:
            done = "planned" if args.dry_run else "adopted"
            print(f"\n{rewrites} entr{'y' if rewrites == 1 else 'ies'} {done}.")
        if preflight.json_mode():
            print(json.dumps({
                "ok": True, "dry_run": args.dry_run,
                "rewrites": rewrites, "plan": plan,
            }))
        return 0

    lines = headless.current_crontab_lines()
    if lines is None:
        raise AgentsLiveError("crontab is not accessible")

    plan = plan_migration(lines)
    rewrites = len(plan["schedule"]) + len(plan["watcher"])

    for name in plan["missing"]:
        print(f"'{name}': entry references an agent with no definition file; "
              f"left alone (prune via `start --prune-orphans`)")

    if rewrites == 0:
        print("All entries already canonical; nothing to migrate.")
        if preflight.json_mode():
            print(json.dumps({
                "ok": True, "dry_run": args.dry_run,
                "rewrites": 0, "plan": plan,
            }))
        return 0

    from . import activate

    verb = "Would rewrite" if args.dry_run else "Rewriting"
    for name, (old, new) in plan["schedule"].items():
        print(f"{verb} schedule entr{'y' if len(new) == 1 else 'ies'} "
              f"for '{name}':")
        for l in old:
            print(f"  - {l}")
        for l in new:
            print(f"  + {l}")
        if not args.dry_run:
            activate.install_cron_agent(name)
    for name, (old, new) in plan["watcher"].items():
        print(f"{verb} @reboot respawn line for '{name}':")
        for l in old:
            print(f"  - {l}")
        for l in new:
            print(f"  + {l}")
        if not args.dry_run:
            install_watcher_reboot_line(name)
            # The running watcher (if any) still dispatches through the
            # old invocation; cycle it onto the new one.
            if find_watcher_pid(name):
                stop_watcher(name)
                pid = activate.activate_watcher(name)
                print(f"  restarted watcher for '{name}' (pid {pid})")

    done = "planned" if args.dry_run else "migrated"
    print(f"\n{rewrites} entr{'y' if rewrites == 1 else 'ies'} {done}.")
    if preflight.json_mode():
        print(json.dumps({
            "ok": True, "dry_run": args.dry_run,
            "rewrites": rewrites, "plan": plan,
        }))
    return 0


if __name__ == "__main__":
    sys.exit(main())
