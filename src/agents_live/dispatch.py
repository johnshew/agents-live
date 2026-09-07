"""The fixed handoff from one runtime firing to one agent outcome."""
from __future__ import annotations

import contextlib
import json
import os
import shutil
import tempfile
import time
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from . import agent, obs, runtime, state
from .agent import Outcome, RawOutput, Request, Step, StepContext
from .runtime import ChildRunner, parse_schedule
from .runtime.budget import claim as claim_budget
from .runtime import handoff
from .runtime.hosts import system as hostruntime
from .runtime.hosts.processes import pid_exists

# An unreadable lock is only abandoned once it outlives any plausible run.
_LOCK_MAX_AGE_SECONDS = 24 * 60 * 60
# A log line, not the artifact: enough to diagnose, not enough to bloat.
_RECORDED_MAX_CHARS = 4096
# Where run-scoped provider configuration lives for the length of a run.
PROVIDER_DIRECTORY = "provider"


@dataclass(frozen=True)
class Firing:
    agent_id: str
    root: str
    origin: str
    subscription_key: str = ""
    changed_files: tuple[str, ...] = ()
    debounce_ms: int | None = None
    instructions: str = ""
    options: tuple[tuple[str, str | bool], ...] = ()

    def __post_init__(self) -> None:
        if self.origin not in {"clock", "boot", "watch", "manual"}:
            raise ValueError(f"unknown firing origin: {self.origin}")


def dispatch(
    firing: Firing,
    *,
    runner: ChildRunner | None = None,
    now: datetime | None = None,
) -> Outcome:
    started = time.monotonic()
    accounting = _Accounting(firing.agent_id)
    result = _dispatch(firing, accounting, runner=runner, now=now)
    if accounting.attempts:
        result = replace(result, usage=_total_usage(accounting.attempts))
        if not result.transcript:
            result = replace(result, transcript=accounting.attempts[-1].get("transcript"))
    model_called = bool(accounting.attempts)
    transcript_state = (
        "no_model_call" if not model_called
        else "available" if result.transcript
        else "disabled" if not accounting.transcript_enabled
        else "missing"
    )
    obs.record(_event_path(Path(firing.root).resolve(), accounting.identifier), obs.create(
        "firing" if result.status == "skipped" and not accounting.durations else "run",
        result.status,
        repository=firing.root, agent=accounting.identifier,
        run_id=result.run_id, origin=firing.origin,
        category=result.category, message=_recorded(result),
        transcript=result.transcript, usage=result.usage,
        attributes=(
            *_firing_attributes(firing),
            ("duration_s", time.monotonic() - started),
            ("pre_duration_s", accounting.durations.get(Step.PRE)),
            ("agent_duration_s", accounting.durations.get(Step.AGENT)),
            ("post_duration_s", accounting.durations.get(Step.POST)),
            ("attempt", len(accounting.attempts)),
            ("attempts", accounting.attempts),
            ("completion_reason", accounting.completion_reason if result.ok else None),
            ("processor_record", accounting.processor_record),
            ("model_called", model_called),
            ("transcript_state", transcript_state),
        ),
    ))
    return result


@dataclass
class _Accounting:
    identifier: str
    transcript_enabled: bool = True
    completion_reason: str | None = None
    processor_record: str | None = None
    durations: dict[Step, float] = field(default_factory=dict)
    attempts: list[dict] = field(default_factory=list)


def _total_usage(attempts: list[dict]) -> tuple[tuple[str, str | None], ...]:
    usages = [dict(attempt["usage"]) for attempt in attempts]
    if len(usages) == 1:
        return tuple(usages[0].items())
    totals = []
    for key in sorted({key for usage in usages for key in usage}):
        values = []
        for usage in usages:
            value = usage.get(key)
            if not isinstance(value, str):
                break
            multiplier = {"k": 1000, "m": 1000000}.get(value[-1:].lower(), 1)
            try:
                number = Decimal(value[:-1] if multiplier != 1 else value) * multiplier
            except InvalidOperation:
                break
            if not number.is_finite() or number < 0:
                break
            values.append(number)
        totals.append((key, str(sum(values)) if len(values) == len(usages) else None))
    return tuple(totals)


def _dispatch(
    firing: Firing,
    accounting: _Accounting,
    *,
    runner: ChildRunner | None,
    now: datetime | None,
) -> Outcome:
    root = Path(firing.root).resolve()
    run_id = uuid.uuid4().hex
    if firing.origin != "manual":
        try:
            if not state.is_started(root, firing.agent_id):
                return _skip(run_id, "not-started")
        except state.StartedStateUnavailable as exc:
            return _failure(run_id, "state_unavailable", str(exc))

    try:
        spec = agent.load(firing.agent_id, root=root)
    except agent.UnsupportedSchemaVersion as exc:
        return _failure(run_id, "runtime_outdated", str(exc))
    except agent.DefinitionError as exc:
        return _failure(run_id, "agent_invalid", str(exc))

    # However the agent was named, record it under its canonical
    # identifier. `run --name <display name>` otherwise writes a second
    # log file that identifier-keyed readers never find, which hid manual
    # runs from the dashboard's history, cost, and health columns.
    if spec.identifier != firing.agent_id:
        firing = replace(firing, agent_id=spec.identifier)
    accounting.identifier = spec.identifier

    config = spec.execution
    if config is None:
        return _failure(
            run_id, "agent_invalid",
            f"skill '{spec.name}' has no Agents Live execution metadata")
    accounting.transcript_enabled = config.transcript
    if firing.origin == "clock":
        instant = now or datetime.now().astimezone()
        if not any(parse_schedule(item).matches(instant) for item in config.schedules):
            return _skip(run_id, "not-due")

    lock = _RunLock(root, firing.agent_id)
    try:
        with handoff.gate():
            if not lock.acquire():
                return _skip(run_id, "already-running")
    except hostruntime.LockBusy:
        return _skip(run_id, "runtime-activation")
    try:
        budget = claim_budget(
            _budget_path(root), now=(now.timestamp() if now is not None else None))
        if not budget.allowed:
            return _skip(run_id, "dispatch-budget")
        selected_runner = runner or runtime.current().child_runner
        try:
            _event_path(root, firing.agent_id).parent.mkdir(parents=True, exist_ok=True)
            return _pipeline(spec, firing, selected_runner, run_id, accounting)
        except (agent.DefinitionError, ValueError) as exc:
            return _failure(
                run_id, "agent_invalid", str(exc))
        except OSError as exc:
            return _failure(
                run_id, "cli_crash", str(exc))
        except RuntimeError as exc:
            return _failure(
                run_id, "resource_unavailable", str(exc))
    finally:
        lock.release()


def _pipeline(spec, firing: Firing, runner: ChildRunner, run_id: str, accounting: _Accounting) -> Outcome:
    shape = agent.shape(spec)
    config = spec.execution
    if config is None:
        raise agent.DefinitionError(
            f"skill '{spec.name}' has no Agents Live execution metadata")
    results = {}
    request = Request(
        text=firing.instructions,
        changed_files=firing.changed_files,
        options=firing.options,
    )
    if config.schema_version != "1":
        # Every ingress lands here, so the bound holds for adapters and
        # APIs as much as for a typed command line.
        overflow = (
            agent.changed_files_overflow(firing.changed_files)
            or agent.instructions_overflow(firing.instructions)
            or agent.options_overflow(firing.options)
        )
        if overflow is not None:
            return _failure(
                run_id, "invocation_input_overflow", overflow)
    scratch = _scratch(spec, run_id)
    try:
        with _resource(
            spec, shape.needs_mcp, run_id, scratch
        ) as (resource_env, session):
            def context(step: Step, **extra) -> StepContext:
                return StepContext(
                    request,
                    resource_env=resource_env,
                    run_id=run_id,
                    origin=firing.origin,
                    scratch=scratch,
                    **extra,
                )

            def snapshot():
                return (
                    session.snapshot(config.result_path)
                    if session is not None and config.result_path is not None
                    else None
                )

            def finish(pipeline_result=None) -> Outcome:
                return _finish(
                    spec, results, run_id,
                    pipeline_result=pipeline_result)

            if shape.has_pre:
                launch = agent.prepare(spec, Step.PRE, context(Step.PRE))
                results[Step.PRE] = _run(
                    spec, Step.PRE, launch, runner, run_id=run_id,
                    scratch=scratch, accounting=accounting)
                if not results[Step.PRE].ok or results[Step.PRE].skip:
                    if results[Step.PRE].ok:
                        accounting.completion_reason = "preprocessor_skip"
                        completed = results[Step.PRE]
                        record_path = scratch / "pre-completion.jsonl"
                        obs.record(record_path, obs.create(
                            "pre-processor", "success", repository=firing.root,
                            agent=firing.agent_id, run_id=run_id, origin=firing.origin,
                            message="\n".join(part for part in (
                                completed.message.strip(), completed.text.strip()) if part),
                            attributes=(("duration_s", accounting.durations[Step.PRE]),),
                        ))
                        accounting.processor_record = str(record_path)
                    return finish(snapshot())

            if shape.has_agent:
                timeout_retries = 1
                empty_retries = 2
                attempt = 0
                while True:
                    attempt += 1
                    launch = agent.prepare(
                        spec,
                        Step.AGENT,
                        context(
                            Step.AGENT,
                            pre=results.get(Step.PRE),
                            attempt=attempt,
                        ),
                    )
                    result = _run(
                        spec, Step.AGENT, launch, runner,
                        run_id=run_id, attempt=attempt, scratch=scratch,
                        accounting=accounting)
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
                    return finish(snapshot())

            # Taken before the post-processor runs, because that is what it
            # is handed on stdin.
            published = snapshot()
            if shape.has_post:
                launch = agent.prepare(
                    spec,
                    Step.POST,
                    context(
                        Step.POST,
                        pre=results.get(Step.PRE),
                        agent=results.get(Step.AGENT),
                        result_snapshot=(
                            _snapshot_text(published)
                            if config.mode == "pipeline" else None
                        ),
                    ),
                )
                model_result = results.get(Step.AGENT)
                if config.transcript and model_result and model_result.transcript:
                    transcript = Path(model_result.transcript)
                    envelope = json.loads(transcript.read_text(encoding="utf-8"))
                    envelope["postprocessor_input"] = launch.input_text
                    if published is not None:
                        envelope["pipeline_result"] = {
                            "path": config.result_path,
                            "present": published[0],
                            "value": published[1] if published[0] else None,
                        }
                    _write_json(transcript, envelope)
                results[Step.POST] = _run(
                    spec, Step.POST, launch, runner, run_id=run_id,
                    scratch=scratch, accounting=accounting)
            return finish(published)
    finally:
        _discard_if_empty(scratch)


def _scratch(spec, run_id: str) -> Path:
    """Where this run's children may write control, logs, and results."""
    from .paths import repo_state_dir
    from .obs.retention import mark_active
    directory = repo_state_dir(spec.root) / "runs" / spec.name / run_id
    directory.mkdir(parents=True, exist_ok=True)
    mark_active(directory)
    return directory


def _discard_if_empty(scratch: Path) -> None:
    """Leave nothing behind for a run whose children wrote nothing.

    An unused channel should not wait for retention before disappearing.
    """
    from .obs.retention import ACTIVE_MARKER
    with contextlib.suppress(OSError):
        (scratch / ACTIVE_MARKER).unlink(missing_ok=True)
        scratch.rmdir()


def _snapshot_text(published) -> str | None:
    if published is None:
        return None
    present, value = published
    if not present:
        return None
    return value if isinstance(value, str) else json.dumps(value)


def _run(
    spec,
    step: Step,
    launch,
    runner: ChildRunner,
    *,
    run_id: str,
    attempt: int = 1,
    scratch: Path | None = None,
    accounting: _Accounting | None = None,
):
    started = time.monotonic() if accounting is not None else 0.0
    try:
        return _run_child(
            spec, step, launch, runner, run_id=run_id, attempt=attempt,
            scratch=scratch, accounting=accounting)
    finally:
        if accounting is not None:
            accounting.durations[step] = (
                accounting.durations.get(step, 0.0) + time.monotonic() - started)


def _run_child(
    spec,
    step: Step,
    launch,
    runner: ChildRunner,
    *,
    run_id: str,
    attempt: int,
    scratch: Path | None,
    accounting: _Accounting | None,
):
    environment = os.environ.copy()
    environment.update(launch.env)
    timeout = launch.timeout
    if step is Step.AGENT and launch.provider:
        from .agent.providers import get as get_provider
        cli = get_provider(launch.provider).cli
        if cli.minimum_version is not None:
            probe_started = time.monotonic()
            probe = runner.run_child(
                (launch.argv[0], *cli.probe_argv),
                cwd=launch.cwd,
                env=environment,
                timeout=min(launch.timeout or 30, 30),
            )
            error = cli.version_error(
                probe.stdout if probe.returncode == 0 and not probe.timed_out else "")
            if error:
                return agent.StepResult(
                    step, False, category="cli_version_unsupported", message=error)
            if timeout is not None:
                timeout -= time.monotonic() - probe_started
                if timeout <= 0:
                    return agent.StepResult(
                        step, False, retryable=True, category="timeout",
                        message="provider version probe exhausted the agent timeout")
    attempt_record = None
    if step is Step.AGENT and accounting is not None:
        attempt_record = {
            "attempt": attempt, "provider": launch.provider,
            "status": "failed", "usage": (), "transcript": None,
        }
        accounting.attempts.append(attempt_record)
    started = time.monotonic() if attempt_record is not None else 0.0
    try:
        raw = runner.run_child(
            launch.argv,
            cwd=launch.cwd,
            env=environment,
            input_text=launch.input_text,
            timeout=timeout,
            use_pty=launch.use_pty,
        )
    finally:
        if attempt_record is not None:
            attempt_record["duration_s"] = time.monotonic() - started
    interpreted = agent.interpret(
        spec,
        step,
        launch,
        RawOutput(raw.returncode, raw.stdout, raw.stderr, raw.timed_out),
        _signals(spec, step, scratch),
    )
    if attempt_record is not None:
        attempt_record.update(
            status="success" if interpreted.ok else "failed",
            category=interpreted.category, usage=interpreted.usage,
            transcript=interpreted.transcript,
        )
    if (
        not interpreted.ok
        and interpreted.category in {
            "pre_processor_crash", "post_processor_crash"}
    ):
        try:
            from .cli import processor_check
            diagnosis = processor_check.diagnose(
                Path(launch.cwd or spec.root),
                Path(launch.argv[-1]),
                f"{raw.stderr}\n{raw.stdout}",
            )
        except (OSError, RuntimeError, ValueError):
            diagnosis = None
        if diagnosis:
            interpreted = replace(
                interpreted,
                message=f"{interpreted.message}\n{diagnosis}",
            )
    if (
        step is Step.AGENT
        and spec.execution is not None
        and spec.execution.transcript
    ):
        transcript = _write_transcript(
            spec, run_id, attempt, launch, raw, interpreted.transcript)
        interpreted = replace(interpreted, transcript=str(transcript))
        if attempt_record is not None:
            attempt_record["transcript"] = str(transcript)
    return interpreted


def _signals(spec, step: Step, scratch: Path | None) -> agent.StepSignals:
    """What the step wrote to a channel other than its streams.

    A malformed control file is ignored rather than fatal: it is an
    instruction the step failed to give, and the exit code already said
    whether the step succeeded.
    """
    if scratch is None or step not in {Step.PRE, Step.POST}:
        return agent.StepSignals()
    if spec.execution is not None and spec.execution.schema_version == "1":
        return agent.StepSignals()
    files = agent.step_files(scratch, step)
    control = None
    try:
        parsed = json.loads(files.control.read_text(encoding="utf-8"))
        control = parsed if isinstance(parsed, dict) else None
    except (OSError, UnicodeError, json.JSONDecodeError):
        control = None
    output = None
    try:
        output = files.output.read_text(encoding="utf-8", errors="replace")
    except (OSError, UnicodeError):
        output = None
    return agent.StepSignals(control, output)


def _write_transcript(spec, run_id: str, attempt: int, launch, raw, provider_ref):
    from .paths import repo_state_dir
    directory = repo_state_dir(spec.root) / "runs" / spec.name
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / f"{run_id}-agent-{attempt}.json"
    _write_json(destination, {
        "argv": list(raw.argv),
        "prompt": launch.prompt,
        "provider": launch.provider,
        "provider_transcript": provider_ref,
        "returncode": raw.returncode,
        "stderr": raw.stderr,
        "stdout": raw.stdout,
        "timed_out": raw.timed_out,
    })
    return destination


def _write_json(destination: Path, payload: object) -> None:
    descriptor, temporary = tempfile.mkstemp(
        dir=destination.parent, prefix=f".{destination.stem}.", text=True)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _finish(
    spec,
    results,
    run_id: str,
    *,
    pipeline_result: tuple[bool, object] | None = None,
) -> Outcome:
    result = replace(agent.outcome(spec, results), run_id=run_id)
    if pipeline_result is not None:
        present, value = pipeline_result
        result = replace(
            result,
            structured=value if present else None,
            result_status="published" if present else "not_published",
        )
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


def _skip(run_id: str, reason: str) -> Outcome:
    return Outcome(True, "skipped", message=reason, run_id=run_id)


def _failure(
    run_id: str,
    category: str,
    message: str,
) -> Outcome:
    return Outcome(
        False, "failed", category=category, message=message, run_id=run_id)


def _firing_attributes(firing: Firing) -> tuple[tuple[str, object], ...]:
    if firing.origin != "watch":
        return ()
    attributes: list[tuple[str, object]] = [
        ("matched_path_count", len(firing.changed_files)),
    ]
    if firing.debounce_ms is not None:
        attributes.append(("watch_debounce_ms", firing.debounce_ms))
    return tuple(attributes)


@contextlib.contextmanager
def _resource(spec, needed: bool, run_id: str, scratch: Path):
    """Everything one run needs on disk or on a port, and its cleanup.

    The pipeline server comes up first so a provider can describe how it
    would reach it; the provider's own files are materialized after and
    removed before the server goes away.
    """
    environment: dict[str, str] = {}
    pipeline_session = None
    endpoint = None
    config = spec.execution
    with contextlib.ExitStack() as stack:
        if needed:
            from .pipeline import pipeline_runtime
            from .paths import repo_state_dir
            log = (
                repo_state_dir(spec.root)
                / "runs"
                / spec.name
                / f"{run_id}-pipeline.jsonl"
            )
            pipeline_session = stack.enter_context(pipeline_runtime(
                log,
                seed_puts=list(spec.pipeline_puts),
                run_id=run_id,
            ))
            environment.update(pipeline_session)
            endpoint = pipeline_session.endpoint
        if config is not None:
            environment.update(stack.enter_context(_provider_files(
                scratch, agent.provider_artifacts(spec, endpoint))))
        yield tuple(sorted(environment.items())), pipeline_session


@contextlib.contextmanager
def _provider_files(scratch: Path, artifacts):
    """Create what the provider asked for, bind it, and take it away.

    The provider names the files and their permissions; where they live
    and how long they live is dispatch's to decide, so a provider never
    holds a path outside the run it belongs to.
    """
    owned = (scratch / PROVIDER_DIRECTORY).resolve()
    environment: dict[str, str] = {}
    # Resolve every path before creating anything, so a run that asks for
    # somewhere it does not own writes nothing at all.
    placed = tuple(
        (artifact, _artifact_path(owned, artifact.relative_path))
        for artifact in artifacts)
    try:
        owned.mkdir(parents=True, exist_ok=True)
        owned.chmod(0o700)
        for artifact, target in placed:
            if artifact.kind == "directory":
                target.mkdir(parents=True, exist_ok=True)
                target.chmod(artifact.mode)
            elif artifact.kind == "file":
                target.parent.mkdir(parents=True, exist_ok=True)
                descriptor = os.open(
                    target,
                    os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
                    artifact.mode,
                )
                with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                    stream.write(artifact.text or "")
                target.chmod(artifact.mode)
            else:
                raise ValueError(
                    f"unknown provider run artifact kind: {artifact.kind}")
            for name in artifact.env:
                environment[name] = str(target)
        yield environment
    finally:
        shutil.rmtree(owned, ignore_errors=True)


def _artifact_path(owned: Path, relative: str) -> Path:
    """Where an artifact lands, refusing anything outside the run."""
    candidate = Path(relative)
    target = (owned / candidate).resolve()
    if candidate.is_absolute() or not target.is_relative_to(owned):
        raise ValueError(
            f"provider run artifact escapes the run directory: {relative}")
    return target


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
