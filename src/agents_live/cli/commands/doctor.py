"""Read runtime health and optionally invoke the one convergence path."""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from ... import paths, runtime, state
from ...state import registry as repos
from .. import lifecycle, update_check
from . import internal


HEALTH_STALE_SECONDS = 70 * 60


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--all-repos", action="store_true")
    parser.add_argument("--repair", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--quick", action="store_true")
    args = parser.parse_args(argv)
    if args.quick:
        return _quick()
    checks: list[dict[str, object]] = []
    try:
        registry = repos.load()
    except ValueError as exc:
        checks.append({"check": "repository registry", "ok": False, "detail": str(exc)})
        registry = None
    else:
        checks.append({"check": "repository registry", "ok": True,
                       "detail": f"{len(registry['repos'])} registered"})
    host_check_index = len(checks)
    try:
        health = runtime.health()
    except (OSError, RuntimeError, ValueError) as exc:
        checks.append({
            "check": "host runtime", "ok": False, "detail": str(exc)})
    else:
        checks.append(_host_check(health))
    if registry is not None:
        for name, value in sorted(registry["repos"].items()):
            root = state.resolve_root(value) if os.path.isdir(value) else None
            if root is None:
                checks.append({
                    "check": f"repository {name}", "ok": False,
                    "detail": "registered but cannot be read; its triggers are preserved",
                })
                continue
            git_index = _git_index_check(root, name)
            if git_index is not None:
                checks.append(git_index)
            try:
                state.load(root)
            except state.StartedStateUnavailable as exc:
                checks.append({
                    "check": f"started state {name}", "ok": False, "detail": str(exc)})
            else:
                checks.append({
                    "check": f"started state {name}", "ok": True, "detail": "readable"})
                torn = _damaged_records(root)
                if torn:
                    # Reported, not failed: the loss is in history a
                    # healthy host can no longer change, and a permanent
                    # nonzero exit would gate every check that reads it.
                    checks.append({
                        "check": f"event log {name}", "ok": True,
                        "detail": (
                            f"{torn} unreadable record(s) from before "
                            "record-atomic appends; that history will not "
                            "appear in logs, timeline, or the dashboard"),
                    })
        try:
            collected = lifecycle.collect(persist=False)
        except lifecycle.CollectionUnavailable as exc:
            checks.append({
                "check": "definition collection", "ok": False,
                "detail": str(exc),
            })
        else:
            for detail in collected.unavailable_repositories:
                checks.append({
                    "check": "definition collection", "ok": False,
                    "detail": f"{detail}; its installed triggers are preserved",
                })
            for _, message in collected.broken_definitions:
                checks.append({
                    "check": "definition", "ok": False, "detail": message,
                })
            for path, keys in collected.unknown_metadata:
                checks.append({
                    "check": "definition metadata",
                    "ok": False,
                    "detail": (
                        f"{path}: unrecognized metadata "
                        f"{', '.join(keys)}; this may be a typo or require "
                        "a newer agents-live runtime"
                    ),
                })
    if args.repair or args.dry_run:
        try:
            result = lifecycle.converge(dry_run=args.dry_run)
        except lifecycle.CollectionUnavailable as exc:
            checks.append({"check": "repair", "ok": False, "detail": str(exc)})
        else:
            if not args.dry_run:
                checks[host_check_index] = _host_check(result.health)
            checks.append({
                "check": "repair",
                "ok": not result.failed,
                "detail": f"{len(result.done)} changes, {len(result.failed)} failures",
            })
    return _finish(checks)


def _quick() -> int:
    beacon = paths.health_beacon_path()
    if _fresh(beacon):
        return _quick_result(True, "fresh", "cached")
    try:
        with (
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            internal.main(["maintain", "--quiet"])
    except (OSError, RuntimeError, ValueError):
        return _quick_result(False, "health refresh failed", "refresh-failed")
    if _fresh(beacon):
        return _quick_result(True, "fresh", "refreshed")
    return _quick_result(False, "health record remained stale", "refresh-failed")


def _fresh(beacon: Path) -> bool:
    try:
        return time.time() - beacon.stat().st_mtime <= HEALTH_STALE_SECONDS
    except OSError:
        return False


def _quick_result(ok: bool, detail: str, source: str) -> int:
    print(json.dumps({
        "ok": ok,
        "checks": [{
            "check": "automatic maintenance",
            "ok": ok,
            "detail": detail,
            "source": source,
        }],
    }))
    return 0 if ok else 1


def _finish(checks: list[dict[str, object]]) -> int:
    ok = all(bool(item["ok"]) for item in checks)
    if os.environ.get("AGENTS_LIVE_JSON") == "1":
        print(json.dumps({"ok": ok, "checks": checks}))
    else:
        for item in checks:
            print(f"{'ok' if item['ok'] else 'ERROR'}: {item['check']}: {item['detail']}")
        if update_check.interactive():
            print(f"\n{update_check.status_text()}")
    return 0 if ok else 1


def _damaged_records(root) -> int:
    """Undecodable lines in this repository's event logs.

    A dropped line looks exactly like one that was never written, so the
    loss has to be counted somewhere an operator reads (#290).
    """
    from ... import obs, paths  # noqa: PLC0415 - keeps import cost off --help
    try:
        return obs.query.damaged(obs.files(paths.repo_state_dir(root) / "logs"))
    except (OSError, ValueError):
        return 0


def _git_index_check(root: Path, name: str) -> dict[str, object] | None:
    """Report unresolved index entries without exposing repository paths."""
    try:
        probe = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--is-inside-work-tree"],
            capture_output=True, text=True, timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if probe.returncode != 0 or probe.stdout.strip() != "true":
        return None
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "ls-files", "--unmerged", "-z"],
            capture_output=True, timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return {
            "check": f"git index {name}", "ok": False,
            "detail": f"could not inspect the Git index: {exc}",
        }
    if result.returncode != 0:
        detail = (
            result.stderr.decode(errors="replace").strip()
            or f"git exited {result.returncode}"
        )
        return {
            "check": f"git index {name}", "ok": False,
            "detail": f"could not inspect the Git index: {detail}",
        }
    conflicted_paths = {
        record.split(b"\t", 1)[1]
        for record in result.stdout.split(b"\0")
        if b"\t" in record
    }
    count = len(conflicted_paths)
    return {
        "check": f"git index {name}",
        "ok": count == 0,
        "detail": (
            "clean" if count == 0 else
            f"{count} unmerged path(s); resolve the Git index before "
            "running automated agents"
        ),
    }


def _host_check(health: runtime.Health) -> dict[str, object]:
    return {
        "check": "host runtime",
        "ok": health.healthy,
        "detail": "; ".join(health.detail) or health.liveness,
    }


if __name__ == "__main__":
    raise SystemExit(main())
