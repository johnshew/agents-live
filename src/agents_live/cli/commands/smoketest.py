"""Deterministic end-to-end validation of the 6.0 framework seams."""
from __future__ import annotations

import argparse
import json
import os
import tempfile
import time
from pathlib import Path

from ... import agent, obs, paths, runtime
from ...dispatch import Firing, dispatch
from ...runtime.hosts.memory import MemoryHost


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime", help=argparse.SUPPRESS)
    parser.add_argument("--model", help=argparse.SUPPRESS)
    parser.parse_args(argv)
    started = time.monotonic()
    target = paths.resolve_root()
    try:
        with tempfile.TemporaryDirectory(prefix="agents-live-smoketest-") as temporary:
            outcome = _run(Path(temporary).resolve())
    except Exception as exc:
        _write_verdict(target, "fail", started, str(exc))
        print(f"Framework smoketest failed: {exc}")
        return 1
    _write_verdict(target, "pass", started)
    print(outcome)
    return 0


def _run(root: Path) -> str:
    skill = root / "Agents" / "framework-smoke"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\n"
        "name: framework-smoke\n"
        "description: Validate the Agents Live framework seams.\n"
        "metadata:\n"
        '  agents-live.schema-version: "1"\n'
        '  agents-live.selector: "fake/echo"\n'
        '  agents-live.schedule: "0 8 * * *"\n'
        '  agents-live.watch: "docs/** debounce 1s"\n'
        "---\n"
        "Return a successful framework validation result.\n",
        encoding="utf-8",
    )
    state_home = root / "state"
    previous_state = os.environ.get("XDG_STATE_HOME")
    os.environ["XDG_STATE_HOME"] = str(state_home)
    paths.clear_cache()
    try:
        spec = agent.load("framework-smoke", root=root)
        scope = f"repo:{root}"
        target = f"agent:{spec.identifier}"
        subscriptions = (
            runtime.Subscription.create(
                scope=scope, target=target, kind="schedule",
                trigger=runtime.parse_schedule(
                    spec.execution.schedules[0]).canonical),
            runtime.Subscription.create(
                scope=scope, target=target, kind="watch",
                trigger=runtime.parse_watch(spec.execution.watch).canonical),
        )
        host = MemoryHost()
        converged = runtime.converge(subscriptions, _host=host)
        if converged.failed or len(host.trigger_store.list()) != 2:
            raise RuntimeError("subscription convergence failed")
        if len(host.supervisor.owned("watcher")) != 1:
            raise RuntimeError("watcher convergence failed")
        result = dispatch(
            Firing(spec.identifier, str(root), "manual"),
            runner=host.child_runner,
        )
        if not result.ok:
            raise RuntimeError(result.message or result.category or "dispatch failed")
        records = obs.load(obs.files(paths.repo_state_dir(root) / "logs"))
        if not any(
            record["agent_name"] == spec.identifier
            and record["phase"] == "done"
            and record["status"] == "ok"
            for record in records
        ):
            raise RuntimeError("completed dispatch event was not queryable")
        removed = runtime.converge((), _host=host)
        if removed.failed or host.trigger_store.list() or host.supervisor.owned():
            raise RuntimeError("subscription cleanup failed")
        return result.text
    finally:
        if previous_state is None:
            os.environ.pop("XDG_STATE_HOME", None)
        else:
            os.environ["XDG_STATE_HOME"] = previous_state
        paths.clear_cache()


def _write_verdict(
    root: Path, status: str, started: float, reason: str | None = None,
) -> None:
    destination = paths.repo_state_dir(root) / "logs" / \
        "smoketest-framework-result.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "status": status,
        "duration_s": round(time.monotonic() - started, 3),
        "runtime": "fake",
    }
    if reason:
        payload["reason"] = reason
    destination.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    raise SystemExit(main())