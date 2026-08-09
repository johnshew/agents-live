#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["PyYAML"]
# ///
"""Internal convergence of persisted trigger entries to canonical form.

Scope: this project's schedule entries (``--name <agent>``) and watcher
respawn entries (``ensure-watcher <agent>`` or its legacy flag form).
The store is host-global, so other projects' entries are never touched.
Every entry is compared against the trigger spec activation would
install today - ``headless.schedule_spec`` / ``headless.watcher_spec``
- so migrate always converges entries to the running context's form:
the script-path form in the flat checkout, the pinned-shim + ``--repo``
form once installed as a package (§3.4.2). This is what retires stale
``uv run .../scripts/*.py`` lines at the F7 flip.

Which store holds the entries is :mod:`schedules`' question, not this
one: on a crontab host the comparison is between lines, on a Task
Scheduler host between registrations, and the plan, the printing, and
the rewrite are the same either way.

A running watcher whose respawn entry was rewritten is restarted so its
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

from .. import preflight
from ..obs import admin as adminlog
from ..runtime.hosts import crontab as crontasks
from ..runtime.hosts import system as hostruntime
from . import triggers
from .headless import (
    AgentsLiveError,
    find_watcher_pid,
    repo_root,
    schedule_spec,
    stop_watcher,
    watcher_spec,
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
    trigger specs activation would install. Returns ``{"schedule":
    {name: (old, new)}, "watcher": {name: (old, new)}, "missing":
    [name, ...]}`` where old/new are line lists (already-canonical
    entries are omitted)."""
    schedule_names: set[str] = set()
    watcher_names: set[str] = set()
    for line in lines:
        if not crontasks.belongs_to_root(line, repo_root()):
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
        old = [l for l in lines
               if crontasks.matches(l, repo_root(), name)]
        try:
            spec = schedule_spec(name)
        except AgentsLiveError:
            # Defined but currently unloadable/scheduleless: leave alone,
            # report as missing-from-migration rather than guessing.
            plan["missing"].append(name)
            continue
        if not triggers.is_canonical(old, spec):
            plan["schedule"][name] = (old, triggers.render(spec))
    for name in sorted(watcher_names):
        if not agent_file_exists(name):
            if name not in plan["missing"]:
                plan["missing"].append(name)
            continue
        old = [l for l in lines
               if crontasks.agent_of_line(l, repo_root(),
                                          kind=crontasks.WATCH) == name]
        spec = watcher_spec(name)
        if not triggers.is_canonical(old, spec):
            plan["watcher"][name] = (old, triggers.render(spec))
    return plan


def plan_task_migration() -> dict:
    """The same comparison against a task store instead of a crontab.

    Same question, different reader: what is registered for each agent
    this repository has on this host, against what activation would
    register today. Answered through the dispatch point, so this module
    still never names a store.
    """
    from . import schedules  # noqa: PLC0415

    plan: dict = {"schedule": {}, "watcher": {}, "missing": []}
    for key, names, spec_of in (
            ("schedule", schedules.installed_names(), schedule_spec),
            ("watcher", schedules.watcher_respawn_names(), watcher_spec)):
        for name in sorted(names):
            if not agent_file_exists(name):
                if name not in plan["missing"]:
                    plan["missing"].append(name)
                continue
            try:
                spec = spec_of(name)
            except AgentsLiveError:
                # Defined but currently unloadable/scheduleless: leave
                # alone, report rather than guess.
                if name not in plan["missing"]:
                    plan["missing"].append(name)
                continue
            old, new = schedules.current_form(spec)
            if old != new:
                plan[key][name] = (old, new)
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
            new = triggers.render(schedule_spec(name))
        except AgentsLiveError:
            plan["unmatched"].extend(old)
            continue
        plan["schedule"][name] = (old, new)
    for name, old in sorted(watcher.items()):
        if not agent_file_exists(name):
            plan["unmatched"].extend(old)
            continue
        plan["watcher"][name] = (old, triggers.render(watcher_spec(name)))
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


def _print_adoption(plan: dict, *, dry_run: bool, say=print) -> int:
    verb = "Would adopt" if dry_run else "Adopting"
    for line in plan["unmatched"]:
        say(f"Unmatched old-root entry left unchanged:\n  {line}")
    for kind, label in (("schedule", "schedule"), ("watcher", "@reboot watcher")):
        for name, (old, new) in plan[kind].items():
            say(f"{verb} {label} entries for '{name}':")
            for line in old:
                say(f"  - {line}")
            for line in new:
                say(f"  + {line}")
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

    # In JSON mode the whole of stdout is the document: a caller parses
    # it, and a narration printed alongside it is a parse error rather
    # than a message. The plan carries everything the narration says.
    say = (lambda *_a, **_k: None) if preflight.json_mode() else print

    tasks = hostruntime.native_scheduler() == hostruntime.TASK_SCHEDULER

    if args.adopt and tasks:
        # Adoption rewrites entries that name a root which no longer
        # exists. A task is found by a name carrying a digest of its
        # root, and a root nobody can name cannot be digested, so there
        # is nothing here to look up. Re-running `start` from the new
        # location registers the agents under their new names.
        raise AgentsLiveError(
            "--adopt is not available on this host: tasks are named after "
            "the project they belong to, so entries from a moved project "
            "cannot be found by name; run `agents-live start --all` from "
            "the new location instead")

    if args.adopt:
        old_root = Path(args.adopt).expanduser().resolve()
        if old_root.exists():
            raise AgentsLiveError(
                f"cannot adopt {old_root}: the old project root still exists; "
                "move or remove it before adopting its triggers")
        if args.dry_run:
            lines = crontasks.lines()
            if lines is None:
                raise AgentsLiveError("crontab is not accessible")
            plan = plan_adoption(lines, old_root)
        else:
            with crontasks.lock():
                lines = crontasks.lines()
                if lines is None:
                    raise AgentsLiveError("crontab is not accessible")
                plan = plan_adoption(lines, old_root)
                rewritten = _apply_adoption(lines, plan)
                if rewritten != lines:
                    crontasks.write(rewritten)
        rewrites = _print_adoption(plan, dry_run=args.dry_run, say=say)
        if not args.dry_run and rewrites:
            adminlog.record("trigger-adopt", old_root=str(old_root),
                            rewrites=rewrites,
                            agents=sorted(plan["schedule"]) + sorted(plan["watcher"]))
        if rewrites == 0:
            say("No matching old-root entries to adopt.")
        else:
            done = "planned" if args.dry_run else "adopted"
            say(f"\n{rewrites} entr{'y' if rewrites == 1 else 'ies'} {done}.")
        if preflight.json_mode():
            print(json.dumps({
                "ok": True, "dry_run": args.dry_run,
                "rewrites": rewrites, "plan": plan,
            }))
        return 0

    if tasks:
        plan = plan_task_migration()
    else:
        lines = crontasks.lines()
        if lines is None:
            raise AgentsLiveError("crontab is not accessible")
        plan = plan_migration(lines)
    rewrites = len(plan["schedule"]) + len(plan["watcher"])

    for name in plan["missing"]:
        say(f"'{name}': entry references an agent with no definition file; "
            f"left alone (prune via `start --prune-orphans`)")

    if rewrites == 0:
        say("All entries already canonical; nothing to migrate.")
        if preflight.json_mode():
            print(json.dumps({
                "ok": True, "dry_run": args.dry_run,
                "rewrites": 0, "plan": plan,
            }))
        return 0

    from . import activate, schedules

    verb = "Would rewrite" if args.dry_run else "Rewriting"
    for name, (old, new) in plan["schedule"].items():
        say(f"{verb} schedule entr{'y' if len(new) == 1 else 'ies'} "
            f"for '{name}':")
        for l in old:
            say(f"  - {l}")
        for l in new:
            say(f"  + {l}")
        if not args.dry_run:
            activate.install_cron_agent(name)
    for name, (old, new) in plan["watcher"].items():
        say(f"{verb} respawn entry for '{name}':")
        for l in old:
            say(f"  - {l}")
        for l in new:
            say(f"  + {l}")
        if not args.dry_run:
            schedules.install_watcher_respawn(name)
            # The running watcher (if any) still dispatches through the
            # old invocation; cycle it onto the new one.
            if find_watcher_pid(name):
                stop_watcher(name)
                pid = activate.activate_watcher(name)
                say(f"  restarted watcher for '{name}' (pid {pid})")

    done = "planned" if args.dry_run else "migrated"
    say(f"\n{rewrites} entr{'y' if rewrites == 1 else 'ies'} {done}.")
    if not args.dry_run:
        adminlog.record("trigger-migrate", rewrites=rewrites,
                        agents=sorted(plan["schedule"]) + sorted(plan["watcher"]))
    if preflight.json_mode():
        print(json.dumps({
            "ok": True, "dry_run": args.dry_run,
            "rewrites": rewrites, "plan": plan,
        }))
    return 0


if __name__ == "__main__":
    sys.exit(main())
