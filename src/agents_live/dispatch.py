"""The fixed handoff from one runtime firing to one agent outcome."""
from __future__ import annotations

import contextlib
import json
import os
import tempfile
import time
import uuid
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path

from . import agent, obs, runtime, state
from .agent import Outcome, RawOutput, Request, Step, StepContext
from .runtime import ChildRunner, parse_schedule
from .runtime.budget import claim as claim_budget
from .runtime.hosts.processes import pid_exists

# An unreadable lock is only abandoned once it outlives any plausible run.
_LOCK_MAX_AGE_SECONDS = 24 * 60 * 60
# A log line, not the artifact: enough to diagnose, not enough to bloat.
_RECORDED_MAX_CHARS = 4096


@dataclass(frozen=True)
class Firing:
    agent_id: str
    root: str
    origin: str
    subscription_key: str = ""
    changed_files: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.origin not in {"clock", "boot", "watch", "manual"}:
            raise ValueError(f"unknown firing origin: {self.origin}")


def dispatch(
    firing: Firing,
    *,
    runner: ChildRunner | None = None,
    now: datetime | None = None,
) -> Outcome:
    root = Path(firing.root).resolve()
    run_id = uuid.uuid4().hex
    events = _event_path(root, firing.agent_id)
    if firing.origin != "manual":
        try:
            if not state.is_started(root, firing.agent_id):
                return _skip(events, firing, run_id, "not-started")
        except state.StartedStateUnavailable as exc:
            return _failure(events, firing, run_id, "state_unavailable", str(exc))

    try:
        spec = agent.load(firing.agent_id, root=root)
    except agent.UnsupportedSchemaVersion as exc:
        return _failure(events, firing, run_id, "runtime_outdated", str(exc))
    except agent.DefinitionError as exc:
        return _failure(events, firing, run_id, "agent_invalid", str(exc))

    # However the agent was named, record it under its canonical
    # identifier. `run --name <display name>` otherwise writes a second
    # log file that identifier-keyed readers never find, which hid manual
    # runs from the dashboard's history, cost, and health columns.
    if spec.identifier != firing.agent_id:
        firing = replace(firing, agent_id=spec.identifier)
        events = _event_path(root, spec.identifier)

    config = spec.execution
    if config is None:
        return _failure(
            events, firing, run_id, "agent_invalid",
            f"skill '{spec.name}' has no Agents Live execution metadata")
    if firing.origin == "clock":
        instant = now or datetime.now().astimezone()
        if not any(parse_schedule(item).matches(instant) for item in config.schedules):
            return _skip(events, firing, run_id, "not-due")

    lock = _RunLock(root, firing.agent_id)
    if not lock.acquire():
        return _skip(events, firing, run_id, "already-running")
    try:
        budget = claim_budget(
            _budget_path(root), now=(now.timestamp() if now is not None else None))
        if not budget.allowed:
            return _skip(events, firing, run_id, "dispatch-budget")
        selected_runner = runner or runtime.current().child_runner
        try:
            events.parent.mkdir(parents=True, exist_ok=True)
            return _pipeline(spec, firing, selected_runner, run_id, events)
        except (agent.DefinitionError, ValueError) as exc:
            return _failure(
                events, firing, run_id, "agent_invalid", str(exc))
        except OSError as exc:
            return _failure(
                events, firing, run_id, "cli_crash", str(exc))
        except RuntimeError as exc:
            return _failure(
                events, firing, run_id, "resource_unavailable", str(exc))
    finally:
        lock.release()


def _pipeline(spec, firing: Firing, runner: ChildRunner, run_id: str, events: Path) -> Outcome:
    shape = agent.shape(spec)
    results = {}
    request = Request(changed_files=firing.changed_files)
    with _resource(spec, shape.needs_mcp, run_id) as resource_env:
        if shape.has_pre:
            launch = agent.prepare(
                spec, Step.PRE, StepContext(request, resource_env=resource_env))
            results[Step.PRE] = _run(
                spec, Step.PRE, launch, runner, run_id=run_id)
            if not results[Step.PRE].ok or results[Step.PRE].skip:
                return _finish(spec, results, firing, run_id, events)

        if shape.has_agent:
            launch = agent.prepare(
                spec,
                Step.AGENT,
                StepContext(request, pre=results.get(Step.PRE), resource_env=resource_env),
            )
            timeout_retries = 1
            empty_retries = 2
            attempt = 0
            while True:
                attempt += 1
                result = _run(
                    spec, Step.AGENT, launch, runner,
                    run_id=run_id, attempt=attempt)
                results[Step.AGENT] = result
                if not result.retryable:
                    break
                if result.category == "timeout" and timeout_retries:
                    timeout_retries -= 1
                    continue
                if result.category == "empty_output" and empty_retries:
                    empty_retries -= 1
                    time.sleep(2)
                    continue
                break
            if not results[Step.AGENT].ok:
                return _finish(spec, results, firing, run_id, events)

        if shape.has_post:
            launch = agent.prepare(
                spec,
                Step.POST,
                StepContext(
                    request,
                    pre=results.get(Step.PRE),
                    agent=results.get(Step.AGENT),
                    resource_env=resource_env,
                ),
            )
            results[Step.POST] = _run(
                spec, Step.POST, launch, runner, run_id=run_id)
    return _finish(spec, results, firing, run_id, events)


def _run(
    spec,
    step: Step,
    launch,
    runner: ChildRunner,
    *,
    run_id: str,
    attempt: int = 1,
):
    environment = os.environ.copy()
    environment.update(launch.env)
    raw = runner.run_child(
        launch.argv,
        cwd=launch.cwd,
        env=environment,
        input_text=launch.input_text,
        timeout=launch.timeout,
        use_pty=launch.use_pty,
    )
    interpreted = agent.interpret(
        spec,
        step,
        launch,
        RawOutput(raw.returncode, raw.stdout, raw.stderr, raw.timed_out),
    )
    if (
        step is Step.AGENT
        and spec.execution is not None
        and spec.execution.transcript
    ):
        transcript = _write_transcript(
            spec, run_id, attempt, raw, interpreted.transcript)
        interpreted = replace(interpreted, transcript=str(transcript))
    return interpreted


def _write_transcript(spec, run_id: str, attempt: int, raw, provider_ref):
    from .paths import repo_state_dir
    directory = repo_state_dir(spec.root) / "runs" / spec.name
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / f"{run_id}-agent-{attempt}.json"
    descriptor, temporary = tempfile.mkstemp(
        dir=directory, prefix=f".{run_id}.", text=True)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump({
                "provider_transcript": provider_ref,
                "returncode": raw.returncode,
                "stderr": raw.stderr,
                "stdout": raw.stdout,
                "timed_out": raw.timed_out,
            }, stream, ensure_ascii=False, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return destination


def _finish(spec, results, firing: Firing, run_id: str, events: Path) -> Outcome:
    result = agent.outcome(spec, results)
    obs.record(events, obs.create(
        "run",
        result.status,
        repository=firing.root,
        agent=firing.agent_id,
        run_id=run_id,
        origin=firing.origin,
        category=result.category,
        message=_recorded(result),
        transcript=result.transcript,
        usage=result.usage,
    ))
    return result


def _recorded(result: Outcome) -> str:
    """What the run produced, for the durable record.

    A scheduled run is invoked with --quiet and its streams go nowhere, so
    anything not recorded here is lost. Bounded because this is a log line,
    not the artifact.
    """
    parts = [part for part in (result.message.strip(), result.text.strip()) if part]
    joined = "\n".join(parts)
    if len(joined) <= _RECORDED_MAX_CHARS:
        return joined
    return joined[:_RECORDED_MAX_CHARS] + "... (truncated)"


def _skip(events: Path, firing: Firing, run_id: str, reason: str) -> Outcome:
    result = Outcome(True, "skipped", message=reason)
    obs.record(events, obs.create(
        "firing", "skipped", repository=firing.root, agent=firing.agent_id,
        run_id=run_id, origin=firing.origin, message=reason))
    return result


def _failure(
    events: Path,
    firing: Firing,
    run_id: str,
    category: str,
    message: str,
) -> Outcome:
    result = Outcome(False, "failed", category=category, message=message)
    obs.record(events, obs.create(
        "run", "failed", repository=firing.root, agent=firing.agent_id,
        run_id=run_id, origin=firing.origin, category=category, message=message))
    return result


@contextlib.contextmanager
def _resource(spec, needed: bool, run_id: str):
    if not needed:
        yield ()
        return
    from .pipeline import pipeline_runtime
    from .paths import repo_state_dir
    log = (
        repo_state_dir(spec.root)
        / "runs"
        / spec.name
        / f"{run_id}-pipeline.jsonl"
    )
    with pipeline_runtime(log, run_id=run_id) as environment:
        yield tuple(sorted(environment.items()))


def _event_path(root: Path, agent_id: str) -> Path:
    from .paths import repo_state_dir
    return repo_state_dir(root) / "logs" / f"{agent_id}.jsonl"


def _budget_path(root: Path) -> Path:
    from .paths import repo_state_dir
    return repo_state_dir(root) / "dispatch-budget.json"


class _RunLock:
    def __init__(self, root: Path, key: str) -> None:
        from .paths import repo_state_dir
        safe = "".join(char if char.isalnum() or char in "-_" else "_" for char in key)
        self.path = repo_state_dir(root) / "locks" / f"{safe}.lock"
        self._owned = False

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists() and self._stale():
            self.path.unlink(missing_ok=True)
        try:
            descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            return False
        with os.fdopen(descriptor, "w", encoding="ascii") as stream:
            json.dump({"pid": os.getpid(), "created": time.time()}, stream)
        self._owned = True
        return True

    def _stale(self) -> bool:
        try:
            value = json.loads(self.path.read_text(encoding="ascii"))
            pid = int(value["pid"])
            if pid <= 0:
                return True
            return not pid_exists(pid)
        except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
            # An unreadable lock names no owner to wait for, so age is the
            # only evidence left. Without this the agent never runs again.
            return self._older_than(_LOCK_MAX_AGE_SECONDS)

    def _older_than(self, seconds: float) -> bool:
        try:
            return (time.time() - self.path.stat().st_mtime) > seconds
        except OSError:
            return False

    def release(self) -> None:
        if self._owned:
            self.path.unlink(missing_ok=True)
            self._owned = False
