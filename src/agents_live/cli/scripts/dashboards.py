#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.12"
# ///
"""Registry of dashboards this host is running.

A dashboard outlives the command that launched it, so a port stays held
by a server the operator no longer knows about. The port guard in
``dashboard.py`` reports that the port is taken; without a record of
what took it there is nothing to act on but the process table (#198).

Every dashboard records its port and pid here before it serves and drops
the entry when it exits, which gives ``agents-live dashboard list`` and
``agents-live dashboard stop`` something to name. The registry only ever
describes dashboards this host started: a listener that is a relay to
another host, or a dashboard from a release before this registry
existed, answers the port probe and is absent here. The messages say so
rather than reporting the entry as missing by mistake.

A dashboard that was killed never ran its exit hook, so entries are
pruned against the process table on every read.
"""
from __future__ import annotations

import argparse
import json
import socket
import sys
from datetime import datetime, timezone
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
PACKAGE_PARENT = SCRIPTS_DIR.parents[2]
if str(PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_PARENT))
from agents_live import paths  # noqa: E402
from agents_live.runtime.hosts import system as hostruntime  # noqa: E402

HOST = "127.0.0.1"
# Loopback answers or refuses immediately; a longer wait only delays an
# answer that is already known.
PROBE_TIMEOUT_S = 0.5


def registry_path() -> Path:
    """Host-scoped record of running dashboards."""
    return paths.state_home() / "dashboards.json"


def port_answers(port: int, *, timeout_s: float = PROBE_TIMEOUT_S) -> bool:
    """Whether something accepts a loopback connection on *port*."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(timeout_s)
        return probe.connect_ex((HOST, port)) == 0


def _load() -> list[dict]:
    """Every recorded entry, with unusable ones dropped.

    A truncated or hand-edited file must not turn `dashboard list` into
    a traceback: an unreadable registry means the same thing as an
    absent one, which is that this host has nothing recorded.
    """
    try:
        entries = json.loads(registry_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(entries, list):
        return []
    return [entry for entry in entries
            if isinstance(entry, dict)
            and isinstance(entry.get("port"), int)
            and isinstance(entry.get("pid"), int)]


def _save(entries: list[dict]) -> None:
    path = registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    paths.atomic_write_text(
        path, json.dumps(entries, indent=2, sort_keys=True) + "\n")


def running() -> list[dict]:
    """Recorded dashboards whose process is still alive."""
    entries = _load()
    live = []
    for entry in entries:
        pid = int(entry["pid"])
        if not hostruntime.is_alive(pid):
            continue
        start_token = entry.get("start_token")
        actual = hostruntime.process_start_token(pid)
        if (
            isinstance(start_token, int)
            and actual is not None
            and start_token != actual
        ):
            continue
        live.append(entry)
    if len(live) != len(entries):
        _save(live)
    return live


def record(port: int, pid: int, repo: Path | None) -> None:
    """Record *pid* as the dashboard serving *port*."""
    entries = [entry for entry in running() if entry["port"] != port]
    entries.append({
        "port": port,
        "pid": pid,
        "start_token": hostruntime.process_start_token(pid),
        "repo": str(repo) if repo else "",
        "started": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    })
    _save(entries)


def forget(port: int, pid: int) -> None:
    """Drop the entry this process recorded."""
    entries = _load()
    kept = [entry for entry in entries
            if not (entry["port"] == port and entry["pid"] == pid)]
    if len(kept) != len(entries):
        _save(kept)


def _table(entries: list[dict]) -> str:
    rows = [("PORT", "URL", "PID", "ANSWERING", "STARTED", "REPOSITORY")]
    rows += [
        (str(entry["port"]), f"http://{HOST}:{entry['port']}",
         str(entry["pid"]),
         "yes" if port_answers(int(entry["port"])) else "no",
         str(entry.get("started", "")), str(entry.get("repo", "")) or "-")
        for entry in sorted(entries, key=lambda item: item["port"])
    ]
    widths = [max(len(row[column]) for row in rows)
              for column in range(len(rows[0]))]
    return "\n".join(
        "  ".join(value.ljust(width) for value, width in zip(row, widths))
        .rstrip()
        for row in rows
    )


def _list() -> int:
    entries = running()
    if not entries:
        print("No dashboard started by this host is running.")
        return 0
    print(_table(entries))
    return 0


def _stop(targets: list[dict]) -> int:
    code = 0
    for entry in sorted(targets, key=lambda item: item["port"]):
        port, pid = int(entry["port"]), int(entry["pid"])
        start_token = entry.get("start_token")
        actual = hostruntime.process_start_token(pid)
        if (
            not isinstance(start_token, int)
            or actual is None
            or start_token != actual
        ):
            print(f"error [process_identity_unknown] dashboard stop: "
                  f"refusing to terminate unverified pid {pid} on port {port}")
            code = 1
            continue
        hostruntime.terminate(pid)
        forget(port, pid)
        # Stopping the recorded dashboard does not free a port that a
        # second listener also holds, which is the case the guard in
        # dashboard.py exists for; saying the port is free when it is
        # not would send the operator straight back into it.
        if port_answers(port):
            print(f"error [port_in_use] dashboard stop: stopped pid {pid}, "
                  f"but port {port} still answers; another listener holds it")
            code = 1
        else:
            print(f"Stopped the dashboard on port {port} (pid {pid}).")
    return code


def main() -> int:
    parser = argparse.ArgumentParser(prog="agents-live dashboard")
    actions = parser.add_subparsers(dest="action", required=True)
    actions.add_parser("list")
    stop = actions.add_parser("stop")
    stop.add_argument("--port", type=int)
    stop.add_argument("--all", action="store_true")
    args = parser.parse_args()

    if args.action == "list":
        return _list()

    entries = running()
    if args.all:
        if not entries:
            print("No dashboard started by this host is running.")
            return 0
        return _stop(entries)
    if args.port is None:
        print("error [usage_error] dashboard stop: pass --port PORT or --all")
        return 2
    targets = [entry for entry in entries if entry["port"] == args.port]
    if not targets:
        detail = (
            "another listener holds it, and this host did not start it"
            if port_answers(args.port) else "nothing answers there"
        )
        print(f"error [not_found] dashboard stop: no dashboard recorded on "
              f"port {args.port}; {detail}")
        return 1
    return _stop(targets)


if __name__ == "__main__":
    raise SystemExit(main())
