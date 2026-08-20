"""Read runtime health and optionally invoke the one convergence path."""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import subprocess
import time
from pathlib import Path

from ... import agent, paths, runtime, state
from ...runtime.hosts import system as hostruntime
from ...state import registry as repos
from .. import lifecycle, update_check
from . import internal


HEALTH_STALE_SECONDS = 60 * 60


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
    selected_roots: tuple[Path, ...] | None = ()
    if registry is not None:
        repository_items = sorted(registry["repos"].items())
        if not args.all_repos:
            try:
                selected_root = state.resolve_root(allow_sole_registered=True)
            except ValueError:
                repository_items = []
            else:
                selected_name = next((
                    name for name, value in repository_items
                    if Path(value).resolve() == selected_root
                ), selected_root.name)
                repository_items = [(selected_name, str(selected_root))]
        checked_roots: list[Path] = []
        for name, value in repository_items:
            root = state.resolve_root(value) if os.path.isdir(value) else None
            if root is None:
                checks.append({
                    "check": f"repository {name}", "ok": False,
                    "detail": "registered but cannot be read; its triggers are preserved",
                })
                continue
            checked_roots.append(root)
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
        selected_roots = None if args.all_repos else tuple(checked_roots)
        try:
            collected = lifecycle.collect(
                selected_roots=selected_roots, persist=False)
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
        if hostruntime.id() == hostruntime.WINDOWS:
            checks.extend(_provider_cli_checks(
                _configured_provider_names({
                    "repos": dict(repository_items),
                })))
        health_payload = _health_payload(paths.health_beacon_path())
        if health_payload is not None:
            checks.extend(_agent_failure_checks(health_payload))
    if args.repair or args.dry_run:
        try:
            result = lifecycle.converge(
                selected_roots=selected_roots, dry_run=args.dry_run)
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


def _configured_provider_names(registry: dict) -> set[str]:
    names: set[str] = set()
    for value in registry.get("repos", {}).values():
        root = state.resolve_root(value) if os.path.isdir(value) else None
        if root is None:
            continue
        try:
            discovery = agent.discover(root)
        except (OSError, ValueError, agent.DefinitionError):
            continue
        for spec in discovery.specs:
            config = spec.execution
            if config is not None and config.selector.provider in {"claude", "copilot"}:
                names.add(config.selector.provider)
    return names


def _provider_cli_checks(names: set[str]) -> list[dict[str, object]]:
    remediation = {
        "claude": "winget install Anthropic.ClaudeCode",
        "copilot": "winget install GitHub.Copilot",
    }
    checks = []
    for name in sorted(names):
        try:
            executable = hostruntime.pin_executable(name)
        except hostruntime.ExecutableNotFound as exc:
            checks.append({
                "check": f"provider CLI {name}",
                "ok": False,
                "detail": f"{exc}; install the native CLI with "
                          f"`{remediation[name]}`",
            })
        else:
            checks.append({
                "check": f"provider CLI {name}",
                "ok": True,
                "detail": f"launchable executable: {executable}",
            })
    return checks


def _quick() -> int:
    beacon = paths.health_beacon_path()
    payload = _health_payload(beacon)
    if payload is not None:
        return _quick_payload(payload, "cached")
    try:
        with (
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            internal.main(["maintain", "--quiet"])
    except (OSError, RuntimeError, ValueError):
        return _quick_result(
            False,
            "automatic maintenance could not refresh health",
            "refresh-failed",
            category="maintenance_failed",
            remedy="agents-live doctor",
        )
    payload = _health_payload(beacon)
    if payload is not None:
        return _quick_payload(payload, "refreshed")
    return _quick_result(
        False,
        "automatic maintenance wrote no fresh valid health record",
        "refresh-failed",
        category="health_record_missing",
        remedy="agents-live doctor",
    )


def _quick_payload(payload: dict, source: str) -> int:
    smoketest = payload.get("smoketest")
    smoketest_status = (
        str(smoketest.get("status", "")).lower()
        if isinstance(smoketest, dict) else ""
    )
    if payload.get("status") == "healthy" and smoketest_status == "pass":
        return _quick_result(True, "fresh", source)
    if smoketest_status == "fail":
        return _quick_result(
            False,
            "current framework smoketest verdict is failed",
            source,
            category="smoketest_failed",
            remedy="agents-live smoketest",
        )
    if smoketest_status != "pass":
        return _quick_result(
            False,
            "current framework smoketest verdict is missing or unknown",
            source,
            category="smoketest_unknown",
            remedy="agents-live smoketest",
        )
    agent_checks = _agent_failure_checks(payload)
    if agent_checks:
        check = agent_checks[0]
        return _quick_result(
            False,
            str(check["detail"]),
            source,
            category="agent_repeated_failures",
            remedy=str(check["remedy"]),
        )
    return _quick_result(
        False,
        "current health record is degraded",
        source,
        category="health_degraded",
        remedy="agents-live doctor",
    )


def _agent_failure_checks(payload: dict) -> list[dict[str, object]]:
    raw = payload.get("agent_failures")
    if not isinstance(raw, list):
        return []
    checks = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        identifier = item.get("agent")
        count = item.get("consecutive_failures")
        if not isinstance(identifier, str) or not isinstance(count, int):
            continue
        checks.append({
            "check": f"agent health {identifier}",
            "ok": False,
            "detail": f"{identifier} has {count} consecutive failures",
            "remedy": f"agents-live logs --agent {identifier} --errors",
        })
    return checks


def _fresh(beacon: Path) -> bool:
    try:
        return time.time() - beacon.stat().st_mtime <= HEALTH_STALE_SECONDS
    except OSError:
        return False


def _health_payload(beacon: Path) -> dict | None:
    if not _fresh(beacon):
        return None
    try:
        payload = json.loads(beacon.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _quick_result(
    ok: bool,
    detail: str,
    source: str,
    *,
    category: str | None = None,
    remedy: str | None = None,
) -> int:
    check = {
        "check": "automatic maintenance",
        "ok": ok,
        "detail": detail,
        "source": source,
    }
    if category is not None:
        check["category"] = category
    if remedy is not None:
        check["remedy"] = remedy
    print(json.dumps({
        "ok": ok,
        "checks": [check],
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
