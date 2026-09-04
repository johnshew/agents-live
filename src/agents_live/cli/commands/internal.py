"""Internal argv ingress for durable runtime artifacts."""
from __future__ import annotations

import argparse
import importlib.metadata
import json
import os
import shutil
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from collections.abc import Callable
from typing import Any

from ... import __version__, agent, deploy, obs, paths, runtime
from ...dispatch import Firing, dispatch
from ...obs import admin as adminlog
from ...obs import retention
from ...runtime.hosts import filesystem as watchsource
from ...runtime.grammars import parse_watch
from ...runtime.watchloop import run as run_watchloop
from ...state import registry as repos
from .. import lifecycle, upgrade_handoff


_AGENT_FAILURE_THRESHOLD = 3


def main(
    argv: list[str] | None = None,
    *,
    metadata: runtime.artifacts.InvocationMetadata | None = None,
) -> int:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    watch = commands.add_parser("watch-loop")
    watch.add_argument("name")
    watch.add_argument("--watch-expression")
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
        return _maintain(dry_run=args.dry_run, metadata=metadata)
    return _watch(args, metadata)


def _maintain(
    *,
    dry_run: bool,
    metadata: runtime.artifacts.InvocationMetadata | None = None,
) -> int:
    if dry_run:
        return _maintain_once(dry_run=True)
    fields = {
        "source": "scheduler" if metadata is not None else "cli",
        "subscription_id": metadata.id if metadata is not None else "",
        "scope": metadata.scope if metadata is not None else "",
        "target": metadata.target if metadata is not None else "runtime",
    }
    with adminlog.operation("maintenance", **fields) as end:
        end.update(
            convergence_changes=0,
            convergence_failures=0,
            health="unknown",
            watchers=0,
            cron=0,
            repositories=0,
            rotated_logs=0,
            removed_archives=0,
            removed_run_artifacts=0,
            smoketest="unknown",
            message="maintenance did not complete",
        )
        code = _maintain_once(dry_run=False, outcome=end)
        end["exit_code"] = code
        if code != 0:
            end.update(
                status="error",
                level="error",
                error_category="maintenance_failed",
            )
        return code


def _maintain_once(*, dry_run: bool, outcome: dict | None = None) -> int:
    outcome = outcome if outcome is not None else {}
    try:
        result = lifecycle.converge(dry_run=dry_run)
        collected = lifecycle.collect(persist=False)
    except lifecycle.CollectionUnavailable as exc:
        outcome["message"] = str(exc)
        print(str(exc), file=sys.stderr)
        return 1
    done = result.done if isinstance(result.done, (list, tuple)) else ()
    failed = result.failed if isinstance(result.failed, (list, tuple)) else ()
    outcome["convergence_changes"] = len(done)
    outcome["convergence_failures"] = len(failed)
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
    outcome.update(
        watchers=len(watchers),
        cron=len(clocks),
        repositories=len(repositories),
    )
    if not dry_run:
        try:
            registered = set(repos.load()["repos"].values()) | repositories
            retained = retention.Result()
            policies = []
            for value in sorted(registered):
                root = Path(value)
                if root.is_dir():
                    policies.append(retention.retention_days(root))
                    retained += retention.maintain(root)
            retained += retention.maintain_host(
                days=max(policies, default=retention.DEFAULT_RETENTION_DAYS))
        except (OSError, ValueError) as exc:
            outcome["message"] = f"retention failed: {exc}"
            print(outcome["message"], file=sys.stderr)
            return 1
        outcome.update(
            rotated_logs=retained.rotated_logs,
            removed_archives=retained.removed_archives,
            removed_run_artifacts=retained.removed_run_artifacts,
        )
    if result.failed or not result.health.healthy:
        outcome["health"] = "unhealthy"
        outcome["message"] = "; ".join(
            f"{operation.key}: {detail}"
            for operation, detail in result.failed
        ) or "; ".join(result.health.detail) or "runtime health is degraded"
        for operation, detail in result.failed:
            print(f"{operation.key}: {detail}", file=sys.stderr)
        return 1
    if dry_run:
        return 0
    payload = {
        "status": "healthy",
        "ts": datetime.now(timezone.utc).isoformat(),
        "watchers": len(watchers),
        "cron": len(clocks),
        "repos": {root: {"status": "ok"} for root in sorted(repositories)},
    }
    active_by_repository: dict[str, set[str]] = {}
    for item in collected.subscriptions:
        if item.target == "runtime" or not item.scope.startswith("repo:"):
            continue
        active_by_repository.setdefault(
            item.scope.removeprefix("repo:"), set()).add(item.target)
    agent_failures = []
    for root, identifiers in sorted(active_by_repository.items()):
        logs = paths.repo_state_dir(Path(root)) / "logs"
        streaks = obs.consecutive_failures(obs.files(logs))
        agent_failures.extend(
            {
                "repository": root,
                "agent": identifier,
                "consecutive_failures": streaks[identifier],
            }
            for identifier in sorted(identifiers)
            if streaks.get(identifier, 0) >= _AGENT_FAILURE_THRESHOLD
        )
    if agent_failures:
        payload["status"] = "degraded"
        payload["agent_failures"] = agent_failures
    previous = _health_beacon()
    smoketest = _smoketest_verdict(previous)
    if smoketest:
        payload["smoketest"] = smoketest
        if smoketest.get("status") == "fail":
            payload["status"] = "degraded"
    outcome.update(
        health=payload["status"],
        watchers=len(watchers),
        cron=len(clocks),
        repositories=len(repositories),
        smoketest=(
            str(smoketest.get("status", "unknown"))
            if smoketest else "unknown"
        ),
        message=f"maintenance completed: {payload['status']}",
    )
    paths.atomic_write_text(
        paths.health_beacon_path(), json.dumps(payload, indent=2) + "\n")
    return 0


def _health_beacon() -> dict:
    try:
        payload = json.loads(paths.health_beacon_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _smoketest_verdict(previous: dict) -> dict:
    prior = previous.get("smoketest")
    prior = prior if isinstance(prior, dict) else {}
    try:
        root = paths.resolve_root()
    except ValueError:
        return prior
    result_path = paths.repo_state_dir(root) / "logs" / \
        "smoketest-framework-result.json"
    try:
        if (
            paths.health_beacon_path().is_file()
            and result_path.stat().st_mtime < paths.health_beacon_path().stat().st_mtime
        ):
            return prior
        result = json.loads(result_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return prior
    if not isinstance(result, dict) or result.get("status") not in {"pass", "fail"}:
        return prior
    return result


def _watch(
    args,
    metadata: runtime.artifacts.InvocationMetadata | None = None,
) -> int:
    root = paths.resolve_root()
    retirement: dict[str, str | None] = {"reason": None, "operation": None}
    watcher_run_id = uuid.uuid4().hex
    watcher_id = args.name

    def should_continue() -> bool:
        operation = upgrade_handoff.quiesce_operation(sys.executable)
        if operation is not None:
            retirement.update(reason="quiesce", operation=operation)
            return False
        if not _runtime_is_current():
            retirement["reason"] = "replacement"
            return False
        return True

    def on_retire(expression: str) -> None:
        if retirement["reason"] == "replacement":
            _restart_watcher(args, root, expression, metadata)
            return
        if retirement["reason"] == "quiesce":
            adminlog.record(
                "upgrade-watchers",
                status="ok",
                upgrade_phase="quiesced",
                correlation_id=retirement["operation"],
                root=str(root),
                watcher=args.name,
                message=f"watcher '{args.name}' quiesced at idle boundary",
            )

    def record(status: str, message: str, **fields: Any) -> None:
        _record_watcher_event(
            root,
            watcher_id,
            watcher_run_id,
            status=status,
            message=message,
            **fields,
        )

    try:
        spec = agent.load(args.name, root=root)
        watcher_id = spec.identifier
        if spec.execution is None or not spec.execution.watch:
            raise agent.DefinitionError(f"'{args.name}' has no watch expression")
        expression = args.watch_expression or spec.execution.watch
        watch = parse_watch(expression)
        roots = _roots(root, watch.includes)
        source = runtime.current().change_source(
            [str(item) for item in roots])
        if source is None:
            raise RuntimeError("this host does not support file watching")
        if hasattr(source, "set_reporter"):
            source.set_reporter(lambda kind, payload: record(
                "degraded",
                _degradation_message(kind),
                category="watch_degraded",
                degradation=kind,
                **payload,
            ))
        record(
            "start",
            "watcher started",
            watcher_pid=os.getpid(),
            watch_expression=watch.canonical,
            watch_root_count=len(roots),
            watch_roots=[str(item) for item in roots],
            watch_debounce_ms=watch.debounce_ms,
            watch_mechanism=watchsource.mechanism(),
        )
        run_watchloop(
            source,
            watch,
            root=root,
            fire=lambda changed: _watch_fire(
                args.name,
                root,
                metadata.id if metadata is not None else "",
                changed,
                watch.debounce_ms,
                record,
            ),
            should_continue=should_continue,
            on_retire=lambda: on_retire(expression),
        )
        reason = retirement["reason"] or "stopped"
        record(
            "ok",
            f"watcher stopped ({reason})",
            stop_reason=reason,
        )
    except watchsource.WatchFailed as exc:
        text = str(exc)
        record(
            "error",
            f"watcher failed: {text}",
            category="watch_failed",
            stop_reason="watch_failed",
            watch_error=text,
        )
        _record_watch_failure(root, watcher_id, text)
        print(text, file=sys.stderr)
        return 1
    except (agent.DefinitionError, RuntimeError, OSError, ValueError) as exc:
        record(
            "error",
            f"watcher stopped: {exc}",
            category="watch_error",
            stop_reason="error",
            watch_error=str(exc),
        )
        print(str(exc), file=sys.stderr)
        return 1
    return 0


def _watch_fire(
    dispatch_name: str,
    root: Path,
    subscription_key: str,
    changed: tuple[str, ...],
    debounce_ms: int,
    record: Callable[..., None],
):
    matched = len(changed)
    record(
        "ok",
        f"watch trigger matched {matched} path(s)",
        matched_path_count=matched,
        watch_debounce_ms=debounce_ms,
    )
    return dispatch(Firing(
        dispatch_name,
        str(root),
        "watch",
        subscription_key,
        changed,
        debounce_ms=debounce_ms,
    ))


def _record_watch_failure(root: Path, watcher_id: str, message: str) -> None:
    try:
        obs.record(_watch_log_path(root, watcher_id), obs.create(
            "run",
            "failed",
            repository=str(root),
            agent=watcher_id,
            run_id=uuid.uuid4().hex,
            origin="watch",
            category="watch_failed",
            message=message,
        ))
    except Exception:
        return


def _record_watcher_event(
    root: Path,
    watcher_id: str,
    watcher_run_id: str,
    *,
    status: str,
    message: str,
    category: str | None = None,
    **fields: object,
) -> None:
    try:
        obs.record(_watch_log_path(root, watcher_id), obs.create(
            "watcher",
            status,
            repository=str(root),
            agent=watcher_id,
            run_id=watcher_run_id,
            origin="watch",
            category=category,
            message=message,
            attributes=tuple(fields.items()),
        ))
    except Exception:
        return


def _watch_log_path(root: Path, watcher_id: str) -> Path:
    return paths.repo_state_dir(root) / "logs" / f"{watcher_id}.jsonl"


def _degradation_message(kind: str) -> str:
    if kind == "overflow":
        return "watch overflow degraded to rescan"
    if kind == "queue-drop":
        return "watch queue dropped events and degraded to rescan"
    if kind == "truncated-rescan":
        return "watch rescan was truncated at the file limit"
    return f"watch degraded: {kind}"


def _runtime_is_current() -> bool:
    """Whether this process loaded the version installed on disk.

    A self-managed installation activates each version in its own
    generation directory, so this process's own distribution metadata
    describes the generation it started from and never changes. Asking it
    would report every watcher current forever and no watcher would ever
    hand off. The installation's active generation is the one fact that
    moves, and a generation is named for the version it contains.

    A uv-managed installation has no generation to read: an upgrade
    rewrites the shared environment underneath the running process, which
    is exactly what its own distribution metadata then reports.
    """
    try:
        return deploy.pointer.read().generation == __version__
    except (deploy.pointer.PointerError, OSError):
        pass
    try:
        return importlib.metadata.version("agents-live") == __version__
    except importlib.metadata.PackageNotFoundError:
        return True


def _restart_watcher(
    args,
    root: Path,
    expression: str,
    metadata: runtime.artifacts.InvocationMetadata | None,
) -> None:
    """Start the replacement after the old change source has stopped."""
    executable = shutil.which("agents-live") or sys.argv[0]
    runtime.current().supervisor.spawn_detached(
        [
            executable,
            "--repo",
            str(root),
            "internal",
            "watch-loop",
            *(
                ("--metadata", runtime.artifacts.encode(metadata))
                if metadata is not None else ()
            ),
            args.name,
            "--watch-expression",
            expression,
        ],
        role="watcher",
        key=metadata.id if metadata is not None else "",
        fingerprint=(
            runtime.artifacts.PREFIX + metadata.id
            if metadata is not None else ""
        ),
        cwd=str(root),
    )


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
