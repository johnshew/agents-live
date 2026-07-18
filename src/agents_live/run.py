#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["PyYAML", "mcp[cli]", "jsonschema"]
# ///
"""Execute one Agents Live agent run. THE dispatch entry point.

This is the script every trigger actually invokes: the crontab lines
and inotify watchers installed by ``activate.py`` both run
``uv run --script run.py --name <agent>`` (watchers add
``--changed-files``), and ``cli.py run <name>`` dispatches here too.

It is a thin orchestrator over the ``headless.py`` library — the
sequencing lives here, the machinery lives there:

1. pre-dispatch ownership gate (``ownership.py``: skip or abstain if
    another host owns the agent; ephemeral ``_``-prefixed agents exempt);
2. optional pipeline runtime (PipelineMcp) when ``mode: pipeline``;
3. pre-processor (may request skip) -> agent -> post-processor;
4. structured JSONL logging of every phase, with durations and usage
    stats, to the per-agent log and the system log.

Exit status: 0 on success or a clean skip, 1 on error (with the
``error_category`` from the ``AgentsLiveError`` subclass logged for
triage).
"""
from __future__ import annotations

import argparse
import os
import sys
import time
import uuid
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import replace

from pathlib import Path

from .headless import (
    EventLog,
    MAX_LOG_FIELD_LENGTH,
    AgentsLiveError,
    ensure_logs_dir,
    extract_prompt_body,
    headless_agent,
    load_agent_config,
    logs_root,
    run_post_processor,
    run_pre_processor,
    set_log_run_id,
    system_log,
)

from . import ownership
from . import preflight


def _parse_put_fences(prompt_path: Path) -> list[tuple[str, object]]:
    """Parse ``put`` fenced blocks from an agent definition body.

    Recognises blocks of the form::

        ```put /path/segments
        {"json": "value"}
        ```

    Returns ``[(path, value), ...]`` in document order so an agent can
    publish a ``/schemas/*`` definition before binding it via
    ``/output/.../$schema``. Raises ``AgentsLiveError`` on malformed
    JSON so authors see a precise location early. ``put`` blocks without
    a path or with a path missing the leading ``/`` are ignored (matches
    the convention used elsewhere).
    """
    import json as _json
    import re

    text = extract_prompt_body(prompt_path.read_text(encoding="utf-8"))
    pattern = re.compile(
        r"^```put[ \t]+(/[^\s`]+)[ \t]*\n(.*?)\n```",
        re.DOTALL | re.MULTILINE,
    )
    out: list[tuple[str, object]] = []
    for match in pattern.finditer(text):
        path = match.group(1)
        raw = match.group(2)
        try:
            value = _json.loads(raw)
        except _json.JSONDecodeError as exc:
            raise AgentsLiveError(
                f"invalid JSON in `put {path}` fence in "
                f"{prompt_path}: {exc}"
            ) from exc
        out.append((path, value))
    return out


def _maybe_pipeline_runtime(stack: ExitStack, config, run_id: str):
    """If config.mode == 'pipeline', start PipelineMcp and merge env vars
    into the config so pre-processor, agent, and post-processor see them.
    Returns the (possibly updated) config."""
    if config.mode != "pipeline":
        return config
    from .pipeline_runtime import pipeline_runtime
    seed_puts = _parse_put_fences(config.prompt_path)
    pipeline_env = stack.enter_context(
        pipeline_runtime(config.agent_log, seed_puts=seed_puts, run_id=run_id)
    )
    merged_env = {**config.env, **pipeline_env}
    return replace(config, env=merged_env)


def build_prompt_text(prompt_path: Path, changed_files: list[str] | None) -> str:
    """Build prompt text by inlining the body of the prompt file.

    Reads the prompt file and extracts everything after the YAML frontmatter,
    passing it directly via ``-p`` so the agent doesn't need a tool call to
    read its own instructions — avoiding potential tool_use/tool_result
    conversation-history mismatches.
    """
    body = extract_prompt_body(prompt_path.read_text(encoding="utf-8"))
    if changed_files:
        listing = "\n".join(f"  - {f}" for f in changed_files)
        return f"Files changed:\n{listing}\n\n{body}"
    return body


def emit(text: str, quiet: bool = False) -> None:
    if not quiet:
        print(text)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--name", required=True)
    parser.add_argument("--changed-files", help="JSON array of changed file paths")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    import json as _json
    changed_files: list[str] = _json.loads(args.changed_files) if args.changed_files else []

    run_id = uuid.uuid4().hex
    set_log_run_id(run_id)
    start_time = time.monotonic()
    # Per-run event streams: the agent's own log and the correlated system
    # log, each with agent_name= bound once. Rebound to config values once the
    # config loads; these early bindings cover load failures.
    tlog = EventLog(logs_root() / f"{args.name}.log", agent_name=args.name)
    slog = EventLog(system_log(), agent_name=args.name)
    # Initialized before the try: the except block logs **phase_durations,
    # and a failure before the pipeline starts (e.g. agent not found) must
    # still produce a clean error event, not an UnboundLocalError.
    phase_durations: dict[str, float] = {}  # pre_processor_s, agent_s, post_processor_s
    pipeline_stack = ExitStack()
    try:
        config = load_agent_config(args.name)
        tlog = EventLog(config.agent_log, agent_name=config.name)
        slog = EventLog(system_log(), agent_name=config.name)
        ensure_logs_dir()
        trigger = "file-change" if changed_files else "cron" if config.schedule else "manual"
        os.environ["AGENTS_LIVE_TRIGGER"] = trigger

        # --- Pre-dispatch ownership check -------------------------------------
        # Local-only mode (no registry, proposal §3.9): everything runs here.
        # Otherwise load_owners pulls agent-owners.json from origin
        # (rate-limited 60s, lock-coordinated with git-sync, fail-open) and
        # re-reads disk fresh, so cross-host transfers propagate in seconds.
        host = ownership.current_host()
        try:
            # Ephemeral (_-prefixed) agents are never ownership-gated, so
            # the smoketest works regardless of registry state.
            if config.name.startswith("_") or ownership.local_only():
                owners = {}
            else:
                owners = ownership.load_owners()
        except ownership.OwnershipUnavailableError as exc:
            # Abstain: never treat a vanished/corrupt registry as local
            # ownership - that would run every agent on every host.
            for stream in (tlog, slog):
                stream.event(level="error", phase="ownership", status="error",
                             error_category="ownership_unavailable",
                             message=str(exc), trigger=trigger)
            if preflight.json_mode():
                preflight.emit_error(preflight.CapabilityFailure(
                    "ownership_unavailable", "agent", "run", str(exc)),
                    json_mode=True)
            elif not args.quiet:
                print(f"Ownership registry unavailable; abstaining: {exc}",
                      file=sys.stderr)
            return 1
        owner_value = owners.get(config.name)
        if (
            owner_value is not None
            and owner_value != ownership.WILDCARD
            and owner_value.lower() != host
        ):
            for stream in (tlog, slog):
                stream.event(level="info", phase="ownership-skip",
                             status="skipped", owner=owner_value, host=host,
                             trigger=trigger)
            if not args.quiet:
                print(
                    f"Skipped: '{config.name}' is owned by '{owner_value}', not this host ('{host}')."
                )
            return 0

        config = _maybe_pipeline_runtime(pipeline_stack, config, run_id)

        @contextmanager
        def timed_phase(phase: str, duration_key: str) -> Iterator[dict[str, object]]:
            """Log start/end events with duration for a pipeline phase.

            Yields a dict the body fills with phase-specific end-event
            fields (including an optional ``status`` override). On
            ``AgentsLiveError`` logs ``status=error`` with the duration
            and re-raises; on success records the duration in
            ``phase_durations[duration_key]``.
            """
            started = time.monotonic()
            tlog.stage_start(phase)
            end_fields: dict[str, object] = {}
            try:
                yield end_fields
            except AgentsLiveError:
                tlog.stage_end(
                    phase,
                    status="error",
                    duration_s=round(time.monotonic() - started, 1),
                )
                raise
            duration = round(time.monotonic() - started, 1)
            phase_durations[duration_key] = duration
            tlog.stage_end(phase, duration_s=duration, **end_fields)

        tlog.event(level="info", phase="start", trigger=trigger,
                   runtime=config.runtime, mode=config.mode,
                   pre_processor=config.pre_processor or "none",
                   post_processor=config.post_processor or "log-only",
                   **({"changed_files": changed_files} if changed_files else {}))
        slog.event(level="info", phase="start", trigger=trigger,
                   **({"changed_files": changed_files} if changed_files else {}))

        # Compute step labels based on which pipeline stages are present
        has_pre = bool(config.pre_processor)
        has_agent = config.runtime != "none"
        has_post = bool(config.post_processor)
        steps: list[str] = ["config"]
        if has_pre:
            steps.append("pre-processor")
        if has_agent:
            steps.append("agent")
        if has_post:
            steps.append("post-processor")
        elif has_agent:
            steps.append("log")
        total = len(steps)
        step_num = 1

        emit(f"[{step_num}/{total}] Reading agent config from frontmatter", args.quiet)
        emit(
            f"      runtime: {config.runtime} | mode: {config.mode}"
            + (f" | pre-processor: {config.pre_processor}" if has_pre else "")
            + f" | post-processor: {config.post_processor or 'log-only'}",
            args.quiet,
        )
        emit("", args.quiet)

        pre_processor_context = ""
        run_summary = ""  # first line of post-processor output (or agent output)

        # --- Pre-processor ---
        if has_pre:
            step_num += 1
            emit(f"[{step_num}/{total}] Running pre-processor ({config.pre_processor})...", args.quiet)
            with timed_phase("pre-processor", "pre_processor_s") as end_fields:
                pre_result = run_pre_processor(config, changed_files=changed_files)
                end_fields.update(
                    status="skipped" if pre_result.skip else "ok",
                    output=pre_result.output[:MAX_LOG_FIELD_LENGTH],
                    stderr=pre_result.stderr[:MAX_LOG_FIELD_LENGTH] if pre_result.stderr else "",
                    skip=pre_result.skip,
                )
            if pre_result.skip:
                emit("      Pre-processor returned skip=true, skipping agent.", args.quiet)
                duration_s = round(time.monotonic() - start_time, 1)
                for stream in (tlog, slog):
                    stream.event(level="info", phase="done", status="skipped",
                                 message="pre-processor requested skip",
                                 duration_s=duration_s)
                if not args.quiet:
                    print("")
                    print("Run skipped (pre-processor).")
                return 0
            pre_processor_context = pre_result.output
            if pre_processor_context and not args.quiet:
                print("      Pre-processor output:")
                for line in pre_processor_context.splitlines()[:10]:
                    print(f"        {line}")
                if len(pre_processor_context.splitlines()) > 10:
                    print("        ... (truncated)")
                print("")

        # --- Agent or post-processor-only ---
        if not has_agent:
            if not config.post_processor:
                # Pre-processor-only pipeline — nothing more to do.
                if not config.pre_processor:
                    raise AgentsLiveError("agent: none requires a pre-processor or post-processor")
                duration_s = round(time.monotonic() - start_time, 1)
                for stream in (tlog, slog):
                    stream.event(level="info", phase="done", status="ok",
                                 duration_s=duration_s)
                if not args.quiet:
                    emit(f"Done (pre-processor only, no agent).", args.quiet)
                return 0
            step_num += 1
            emit(f"[{step_num}/{total}] Running post-processor directly (no agent)...", args.quiet)
            # For post-processor-only with pre-processor, pass pre-processor output as input
            processor_input = pre_processor_context if pre_processor_context else None
            with timed_phase("post-processor", "post_processor_s") as end_fields:
                post_output = run_post_processor(config, processor_input, changed_files=changed_files)
                end_fields["message"] = post_output if post_output else ""
            if post_output and not args.quiet:
                for line in post_output.splitlines():
                    print(f"      {line}")
            if post_output:
                run_summary = post_output.splitlines()[0][:200]
        else:
            step_num += 1
            emit(f"[{step_num}/{total}] Executing agent...", args.quiet)
            # Build prompt, appending pre-processor context as a named field
            prompt_text = build_prompt_text(config.prompt_path, changed_files)
            if pre_processor_context:
                escaped = pre_processor_context.replace('"', '\\"')
                prompt_text = f'{prompt_text}\n\npre-processor="{escaped}"'
            with timed_phase("agent", "agent_s") as end_fields:
                result = headless_agent(config, prompt_text, stream=not args.quiet)
                output = result.output
                usage_fields = {
                    key: value
                    for key, value in {
                        "model": result.model,
                        "tokens_in": result.tokens_in,
                        "tokens_out": result.tokens_out,
                        "tokens_cached": result.tokens_cached,
                        "premium_requests": result.premium_requests,
                        "credits": result.credits,
                        "cost_usd": result.cost_usd,
                        "transcript_path": result.transcript_path,
                        "structured_output": result.structured_output,
                    }.items()
                    if value is not None
                }
                end_fields.update(
                    output=output[:MAX_LOG_FIELD_LENGTH],
                    **usage_fields,
                )
            if args.quiet:
                print(f"Agent output:\n{output}")
            else:
                print("      Agent output:")
                lines = output.splitlines()
                for line in lines[:20]:
                    print(line)
                if len(lines) > 20:
                    print("      ... (truncated)")
                print("")

            if config.post_processor:
                step_num += 1
                emit(f"[{step_num}/{total}] Post-processor result:", args.quiet)
                with timed_phase("post-processor", "post_processor_s") as end_fields:
                    post_output = run_post_processor(config, output, changed_files=changed_files)
                    end_fields["message"] = post_output if post_output else ""
                if post_output:
                    if args.quiet:
                        print(post_output)
                    else:
                        for line in post_output.splitlines():
                            print(f"      {line}")
                    run_summary = post_output.splitlines()[0][:200]
            else:
                step_num += 1
                emit(f"[{step_num}/{total}] Output logged", args.quiet)
                if output:
                    run_summary = output.splitlines()[0][:200]

        duration_s = round(time.monotonic() - start_time, 1)
        done_extra = {**phase_durations}
        if run_summary:
            done_extra["summary"] = run_summary
        for stream in (tlog, slog):
            stream.event(level="info", phase="done", status="ok",
                         duration_s=duration_s, **done_extra)

        if not args.quiet:
            print("")
            print("Run complete.")
        return 0
    except AgentsLiveError as exc:
        duration_s = round(time.monotonic() - start_time, 1)
        # Classify the error for structured triage by self-healing agents.
        # The category rides on the exception type (AgentsLiveError
        # subclasses in headless.py), not message text — a crash whose
        # stderr detail happens to contain "timed out" stays agent_error.
        msg = str(exc)
        error_category = exc.category
        for stream in (tlog, slog):
            stream.event(level="error", phase="done", status="error",
                         message=msg, error_category=error_category,
                         duration_s=duration_s, **phase_durations)
        if preflight.json_mode():
            # Layer 2 (§3.6): with --json the typed error leaves as the
            # envelope on stdout, not prose on stderr.
            preflight.emit_typed_error(exc, "run")
        else:
            print(f"Error: {exc}", file=sys.stderr)
        return 1
    finally:
        pipeline_stack.close()


if __name__ == "__main__":
    raise SystemExit(main())
