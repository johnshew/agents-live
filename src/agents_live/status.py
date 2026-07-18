#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["PyYAML"]
# ///
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import subprocess

from .headless import AgentsLiveError, agent_details, list_agents, load_agent_config

LOGS_DIR = Path("Agents/logs")


def _last_run_times(name: str) -> tuple[str, str]:
    """Return (last_ok, last_error) as '-Xd HH:MM' relative strings.

    Scans the agent's log file for phase=done entries.
    Returns '-' if no matching entry found.
    """
    log_file = LOGS_DIR / f"{name}.log"
    if not log_file.is_file():
        return ("—", "—")

    last_ok_ts: str | None = None
    last_err_ts: str | None = None

    for line in log_file.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if entry.get("phase") != "done":
            continue
        ts = entry.get("ts", "")
        status = entry.get("status", "")
        if status == "ok":
            last_ok_ts = ts
        elif status == "error":
            last_err_ts = ts

    now = datetime.now(timezone.utc)
    return (_format_ago(last_ok_ts, now), _format_ago(last_err_ts, now))


def _format_ago(ts: str | None, now: datetime) -> str:
    """Format an ISO timestamp as a human-friendly relative string.

    Examples: -1s, -4m, -1h, -59m, -2d 16h, -5d 1h
    Uses at most two significant units. Returns '—' if no timestamp.
    """
    if not ts:
        return "—"
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        total_seconds = int((now - dt).total_seconds())
        if total_seconds < 0:
            total_seconds = 0
        days, rem = divmod(total_seconds, 86400)
        hours, rem = divmod(rem, 3600)
        mins, secs = divmod(rem, 60)
        if days > 0:
            return f"-{days}d {hours}h" if hours else f"-{days}d"
        if hours > 0:
            return f"-{hours}h {mins}m" if mins else f"-{hours}h"
        if mins > 0:
            return f"-{mins}m"
        return f"-{secs}s"
    except (ValueError, TypeError):
        return "?"


def format_table(agents: list[dict[str, Any]]) -> str:
    headers = ["NAME", "TYPE", "SCHEDULE/WATCH", "RUNTIME", "MODE", "STATE", "OWNER", "LAST OK", "LAST ERR"]
    rows: list[list[str]] = []
    # Pre-compute last run times per agent name (only once per name)
    run_times: dict[str, tuple[str, str]] = {}
    for agent in agents:
        name = agent["name"]
        if name not in run_times:
            run_times[name] = _last_run_times(name)
    for agent in agents:
        trigger_states = agent.get("triggerStates", {})
        last_ok, last_err = run_times[agent["name"]]
        owner_val = agent.get("owner") or "—"
        is_owner = agent.get("isOwner")
        owner_cell = owner_val if is_owner is None else (
            f"{owner_val} *" if is_owner and owner_val not in ("—", "*") else owner_val
        )
        first_row = True
        for ttype, tvalue in _trigger_lines(agent):
            tstate = trigger_states.get(ttype, agent["state"])
            rows.append([
                agent["name"],
                ttype,
                tvalue,
                agent["runtime"],
                agent["mode"],
                tstate,
                owner_cell if first_row else "",
                last_ok if first_row else "",
                last_err if first_row else "",
            ])
            first_row = False
    table = [headers, *rows]
    widths = [max(len(str(row[i])) for row in table) for i in range(len(headers))]
    return "\n".join(
        "  ".join(str(row[i]).ljust(widths[i]) for i in range(len(headers))).rstrip()
        for row in table
    )


def _trigger_lines(agent: dict[str, Any]) -> list[tuple[str, str]]:
    """Return (type_label, trigger_value) tuples — one per trigger."""
    lines: list[tuple[str, str]] = []
    sched = agent.get("schedule")
    if sched:
        if isinstance(sched, list):
            for s in sched:
                lines.append(("cron", s))
        else:
            lines.append(("cron", sched))
    wp = agent.get("watchPath")
    if wp:
        if isinstance(wp, list):
            for p in wp:
                lines.append(("watcher", p))
        else:
            lines.append(("watcher", wp))
    if not lines:
        lines.append((agent.get("type", "?"), "-"))
    return lines


def _in_sandbox() -> bool:
    """Return True if running in a restricted sandbox where crontab is inaccessible."""
    try:
        result = subprocess.run(
            ["crontab", "-l"],
            capture_output=True,
            check=False,
        )
        return result.returncode != 0
    except FileNotFoundError:
        return False  # crontab not installed - not a sandbox issue


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", dest="json_mode", action="store_true")
    parser.add_argument("name", nargs="?")
    args = parser.parse_args()

    if _in_sandbox():
        print(
            "status: running in a restricted sandbox - crontab and process detection are unavailable.\n"
            "Run this command in a regular terminal for accurate agent state.",
            file=sys.stderr,
        )
        return 1

    try:
        if args.name and not args.json_mode:
            raise AgentsLiveError("--json flag is required when querying a single agent")
        if args.name:
            details = agent_details(load_agent_config(args.name))
            print(json.dumps(details, indent=2))
            return 0

        names = list_agents()
        if not names:
            if args.json_mode:
                print('{"agents": []}')
            else:
                print("No agents configured")
            return 0

        agents = []
        for name in names:
            try:
                agents.append(agent_details(load_agent_config(name)))
            except AgentsLiveError:
                continue

        if args.json_mode:
            print(json.dumps({"agents": agents}, indent=2))
        else:
            print(format_table(agents))
        return 0
    except AgentsLiveError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
