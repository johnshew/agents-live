"""Pure load, shape, prepare, interpret, and outcome functions."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Mapping

from ..paths import repo_state_dir
from .definition import DefinitionError, discover_definitions, load_definition
from .mcp import resolve_mcp_servers
from .providers import get as get_provider
from .values import (
    AgentSpec,
    Completion,
    Discovery,
    Launch,
    Outcome,
    PipelineEndpoint,
    ProviderRuntime,
    RawOutput,
    Request,
    ResolvedSpec,
    RunArtifact,
    RunShape,
    Step,
    StepContext,
    StepFiles,
    StepResult,
    StepSignals,
)

DEFAULT_OUTPUT_MAX_BYTES = 10 * 1024 * 1024
DEFAULT_TIMEOUT_SECONDS = 120
# Environment values are bounded, so a processor never has to ask whether
# what it reads is whole.
ENVIRONMENT_VALUE_MAX_BYTES = 32 * 1024


def load(agent_id: str, *, root: Path) -> AgentSpec:
    return load_definition(agent_id, root=root)


def discover(root: Path) -> Discovery:
    return discover_definitions(root)


def shape(spec: AgentSpec) -> RunShape:
    config = _config(spec)
    return RunShape(
        bool(config.pre_processor),
        config.selector.provider != "none",
        bool(config.post_processor),
        config.mode == "pipeline",
    )


def step_files(scratch: Path, step: Step) -> StepFiles:
    """The channels a step may use, named but never created."""
    return StepFiles(
        scratch / f"{step.value}-control.json",
        scratch / f"{step.value}-log.jsonl",
        scratch / f"{step.value}-output",
    )


def provider_artifacts(
    spec: AgentSpec,
    pipeline: PipelineEndpoint | None = None,
) -> tuple[RunArtifact, ...]:
    """What the selected provider needs this run to put on disk.

    The provider describes the files; dispatch owns where they land,
    their permissions, and when they are removed. Nothing here touches
    the filesystem, and no host object reaches the provider.
    """
    config = _config(spec)
    if config.selector.provider == "none":
        return ()
    provider = get_provider(config.selector.provider)
    return provider.artifacts(ProviderRuntime(
        config.mode,
        resolve_mcp_servers(spec.root, config.mcps),
        pipeline,
    ))


def prepare(spec: AgentSpec, step: Step, ctx: StepContext) -> Launch:
    config = _config(spec)
    environment = dict(config.env)
    environment.update(ctx.request.env)
    environment.update(ctx.resource_env)
    environment.update(_run_context(spec, step, ctx))
    if step in {Step.PRE, Step.POST}:
        reference = config.pre_processor if step is Step.PRE else config.post_processor
        if reference is None:
            raise DefinitionError(f"{step.value} processor is not declared")
        path = (spec.skill_root / reference).resolve()
        if not path.is_file():
            raise DefinitionError(f"{step.value} processor not found: {reference}")
        input_text = None
        if step is Step.POST:
            if config.mode == "pipeline":
                # The declared result, so a class 0 post-processor keeps
                # reading its input from stdin after the move to pipeline
                # mode without knowing it happened.
                input_text = ctx.result_snapshot
            else:
                source = ctx.agent or ctx.pre
                if source is not None and source.structured is not None:
                    # The extracted value, not the text it came out of: a
                    # provider wraps its answer in prose and a session
                    # footer, and the processor is the reason the value was
                    # extracted at all.
                    input_text = json.dumps(source.structured)
                elif source is not None:
                    input_text = source.text
        return Launch(
            _processor_argv(path),
            tuple(sorted(environment.items())),
            str(spec.root),
            input_text,
            config.timeout or DEFAULT_TIMEOUT_SECONDS,
        )

    prompt = _prompt(spec, ctx)
    selector = config.selector
    provider = get_provider(selector.provider)
    if config.mode == "pipeline" and config.mcps:
        raise DefinitionError(
            "pipeline mode cannot declare project MCP servers; "
            "only the isolated pipeline MCP is available"
        )
    resolved_mcps = resolve_mcp_servers(spec.root, config.mcps)
    resolved = ResolvedSpec(
        spec.name,
        prompt,
        config.mode,
        config.allow_tools,
        resolved_mcps,
        tuple(sorted(environment.items())),
        provider.name,
        selector.model,
        selector.effort,
        # A schema reaches the CLI only where the CLI enforces it. Where
        # it does not, the port still validates the parsed value, so the
        # guarantee holds either way and is never silently assumed.
        _resolved_output_schema(spec)
        if provider.capabilities.structured_output else None,
    )
    # Everything a provider cannot honor is refused here, before a
    # process exists: an unsupported mode reaching a CLI that ignores the
    # flag would look like a run that succeeded under a policy nothing
    # applied.
    refusal = provider.validate(resolved)
    if refusal is not None:
        raise DefinitionError(refusal)
    launch = provider.prepare(resolved, ctx.request)
    return Launch(
        launch.argv,
        launch.env,
        str(spec.root),
        launch.input_text,
        config.timeout or DEFAULT_TIMEOUT_SECONDS,
        launch.use_pty,
        launch.filters_tui_noise,
        launch.provider,
        launch.prompt,
    )


def _prompt(spec: AgentSpec, ctx: StepContext) -> str:
    """The definition first, then anything this run added, each labelled."""
    sections = [spec.body]
    if ctx.request.changed_files:
        listing = "\n".join(f"  - {item}" for item in ctx.request.changed_files)
        sections.append(f"Files changed:\n{listing}")
    if ctx.request.text:
        sections.append(f"Invocation instructions:\n{ctx.request.text}")
    if ctx.pre and ctx.pre.text:
        sections.append(f"Pre-processor context:\n{ctx.pre.text}")
    return "\n\n".join(sections)


def _run_context(
    spec: AgentSpec, step: Step, ctx: StepContext) -> dict[str, str]:
    """What the run tells its children, in the shape that version asks for."""
    config = _config(spec)
    environment = {
        "AGENTS_LIVE_AGENT_NAME": spec.name,
        "AGENTS_LIVE_AGENT_ID": spec.identifier,
    }
    if config.schema_version == "1":
        environment["AGENTS_LIVE_LOG_FILE"] = str(
            repo_state_dir(spec.root) / "logs" / f"{spec.identifier}.jsonl")
        if ctx.request.changed_files:
            environment["AGENTS_LIVE_CHANGED_FILES"] = json.dumps(
                list(ctx.request.changed_files))
        return environment
    environment.update({
        "AGENTS_LIVE_CONTRACT": "2",
        "AGENTS_LIVE_RUN_ID": ctx.run_id,
        "AGENTS_LIVE_ORIGIN": ctx.origin,
        "AGENTS_LIVE_ATTEMPT": str(ctx.attempt),
        "AGENTS_LIVE_REPO_ROOT": str(spec.root),
        "AGENTS_LIVE_INSTRUCTIONS": ctx.request.text,
        "AGENTS_LIVE_CHANGED_FILES": json.dumps(list(ctx.request.changed_files)),
        "AGENTS_LIVE_OPTIONS": json.dumps(dict(ctx.request.options)),
    })
    if step in {Step.PRE, Step.POST}:
        environment["AGENTS_LIVE_ROLE"] = step.value
    if ctx.scratch is not None:
        files = step_files(ctx.scratch, step)
        environment["AGENTS_LIVE_CONTROL"] = str(files.control)
        environment["AGENTS_LIVE_LOG"] = str(files.log)
        environment["AGENTS_LIVE_OUTPUT"] = str(files.output)
    return environment


def changed_files_overflow(items: tuple[str, ...]) -> str | None:
    """Why this change set cannot be handed to a processor, if it cannot.

    Dropping paths to make it fit would be worse than refusing: a
    processor that loops over the list would skip work it was never told
    about, and nothing downstream could tell that it had.
    """
    encoded = _environment_value_bytes(json.dumps(list(items)))
    if encoded <= ENVIRONMENT_VALUE_MAX_BYTES:
        return None
    return (
        f"{len(items)} changed files need {encoded} bytes, over the "
        f"{ENVIRONMENT_VALUE_MAX_BYTES}-byte limit for one environment "
        "value. Narrow agents-live.watch, or have the processor scan the "
        "repository itself instead of taking a list."
    )


def instructions_overflow(text: str) -> str | None:
    """Why these instructions cannot be handed to a processor, if they cannot.

    Truncating instead would leave the model reading the whole thing and a
    processor reading part of it, with neither able to tell.
    """
    encoded = _environment_value_bytes(text)
    if encoded <= ENVIRONMENT_VALUE_MAX_BYTES:
        return None
    return (
        f"instructions need {encoded} bytes, over the "
        f"{ENVIRONMENT_VALUE_MAX_BYTES}-byte limit for one environment "
        "value. Shorten them, or move the standing part into the definition."
    )


def options_overflow(items: tuple[tuple[str, str | bool], ...]) -> str | None:
    """Why these invocation options cannot be handed to a processor."""
    encoded = _environment_value_bytes(json.dumps(dict(items)))
    if encoded <= ENVIRONMENT_VALUE_MAX_BYTES:
        return None
    return (
        f"options need {encoded} bytes, over the "
        f"{ENVIRONMENT_VALUE_MAX_BYTES}-byte limit for one environment value"
    )


def _environment_value_bytes(value: str) -> int:
    """The portable size of one processor environment value."""
    return len(value.encode("utf-8"))


def interpret(
    spec: AgentSpec,
    step: Step,
    launch: Launch,
    raw: RawOutput,
    signals: StepSignals = StepSignals(),
) -> StepResult:
    if raw.timed_out:
        return StepResult(
            step, False, retryable=step is Step.AGENT,
            category="timeout", message="child timed out")
    if raw.returncode != 0:
        if step is Step.AGENT:
            provider = get_provider(
                launch.provider or _config(spec).selector.provider)
            category = provider.failure(raw) or "cli_crash"
        else:
            category = {
                Step.PRE: "pre_processor_crash",
                Step.POST: "post_processor_crash",
            }[step]
        return StepResult(
            step, False, category=category,
            message=raw.stderr.strip() or f"child exited with status {raw.returncode}")
    text = raw.stdout.rstrip("\n")
    message = raw.stderr.strip()
    if step in {Step.PRE, Step.POST}:
        if signals.output is not None:
            # The result moved to a file, so stdout was diagnostics.
            message = "\n".join(part for part in (message, text) if part)
            text = signals.output.rstrip("\n")
        skip = False
        if _config(spec).schema_version == "1":
            skip = step is Step.PRE and _skip_on_stdout(text)
        elif signals.control is not None:
            skip = signals.control.get("skip") is True
            note = signals.control.get("message")
            if skip and isinstance(note, str) and note:
                message = note
        return StepResult(step, True, skip=skip, text=text, message=message)
    provider = get_provider(launch.provider or _config(spec).selector.provider)
    completion = provider.parse(raw)
    if not completion.text and completion.structured is None:
        return StepResult(
            step, False, retryable=True, category="empty_output",
            message="provider returned no output")
    return _validate_completion(spec, completion, raw.stdout)


def _skip_on_stdout(text: str) -> bool:
    """Version 1 ended a run by printing a skip object; version 2 does not."""
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return False
    return isinstance(parsed, dict) and bool(parsed.get("skip"))


def outcome(spec: AgentSpec, results: Mapping[Step, StepResult]) -> Outcome:
    del spec
    for step in (Step.PRE, Step.AGENT, Step.POST):
        result = results.get(step)
        if result is not None and not result.ok:
            return Outcome(
                False, "failed", result.text, result.structured,
                result.category or "agent_error", result.message,
                result.usage, result.transcript,
            )
    pre = results.get(Step.PRE)
    if pre is not None and pre.skip:
        return Outcome(True, "skipped", pre.text, message=pre.message)
    final = results.get(Step.POST) or results.get(Step.AGENT) or pre
    telemetry = results.get(Step.AGENT) or final
    return Outcome(
        True,
        "success",
        final.text if final else "",
        final.structured if final else None,
        message=final.message if final else "",
        usage=telemetry.usage if telemetry else (),
        transcript=telemetry.transcript if telemetry else None,
    )


def _validate_completion(
    spec: AgentSpec,
    completion: Completion,
    raw_text: str,
) -> StepResult:
    config = _config(spec)
    size = len(raw_text.encode("utf-8", errors="replace"))
    cap = config.output_max_bytes or DEFAULT_OUTPUT_MAX_BYTES
    if size > cap:
        return StepResult(
            Step.AGENT, False, category="agent_output_invalid",
            message=f"agent output is {size} bytes, over the {cap}-byte cap")
    text = completion.text.strip()
    structured = completion.structured
    if structured is None and (
            config.output_schema is not None or config.output_path_roots
            or config.output_provenance == "strict"
            or (config.post_processor and config.mode != "pipeline")):
        structured = _extract_json(text)
        if structured is None:
            return StepResult(
                Step.AGENT, False, category="output_parse_error",
                message="no JSON value could be extracted from provider output")
    error = _validate_schema(spec, structured)
    if error is None:
        error = _validate_paths(spec, structured)
    if error is not None:
        return StepResult(
            Step.AGENT, False, category="agent_output_invalid", message=error)
    return StepResult(
        Step.AGENT, True, text=text, structured=structured,
        usage=completion.usage, transcript=completion.transcript)


def _extract_json(text: str):
    candidates = re.findall(r"```(?:json)?\s*\n(.*?)\n```", text, re.DOTALL | re.IGNORECASE)
    candidates.append(text)
    for candidate in reversed(candidates):
        try:
            return json.loads(candidate.strip())
        except json.JSONDecodeError:
            continue
    return _last_balanced_value(text)


def _last_balanced_value(text: str):
    """The last complete JSON object or array embedded in *text*.

    Provider CLIs append session footers, and stripping them by prefix
    means chasing every release: a copilot footer carrying credits and a
    resume hint began failing agents that had already produced their
    output. Finding the value is version-independent.
    """
    for opening, closing in (("{", "}"), ("[", "]")):
        end = text.rfind(closing)
        while end != -1:
            depth = 0
            for index in range(end, -1, -1):
                character = text[index]
                if character == closing:
                    depth += 1
                elif character == opening:
                    depth -= 1
                    if depth == 0:
                        try:
                            return json.loads(text[index:end + 1])
                        except json.JSONDecodeError:
                            break
            end = text.rfind(closing, 0, end)
    return None


def _validate_schema(spec: AgentSpec, value: object) -> str | None:
    schema = _resolved_output_schema(spec)
    if schema is None:
        return None
    import jsonschema
    try:
        jsonschema.validate(value, schema)
    except jsonschema.ValidationError as exc:
        return f"output does not conform at {exc.json_path}: {exc.validator} failed"
    return None


def _resolved_output_schema(spec: AgentSpec) -> dict | None:
    declared = _config(spec).output_schema
    if declared is None:
        return None
    if isinstance(declared, str):
        try:
            schema = json.loads((spec.skill_root / declared).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise DefinitionError(f"output schema is unreadable: {exc}") from exc
    else:
        schema = declared
    import jsonschema
    try:
        jsonschema.validators.validator_for(schema).check_schema(schema)
    except jsonschema.SchemaError as exc:
        raise DefinitionError(f"output schema is invalid: {exc.message}") from exc
    return schema


def _validate_paths(spec: AgentSpec, value: object) -> str | None:
    roots = _config(spec).output_path_roots
    if not roots:
        return None
    if value is None:
        return "output path roots were declared but output was not JSON"
    allowed = tuple((spec.root / item).resolve() for item in roots)
    for location, item in _iter_paths(value):
        if not isinstance(item, str) or not item:
            return f"output path {location} is not a nonempty string"
        candidate = Path(item)
        if candidate.is_absolute():
            return f"output path {location} is absolute"
        resolved = (spec.root / candidate).resolve()
        if not any(resolved == root or resolved.is_relative_to(root) for root in allowed):
            return f"output path {location} escapes output-path-roots"
    return None


def _iter_paths(value: object, prefix: str = "$"):
    if isinstance(value, dict):
        for key, item in value.items():
            location = f"{prefix}.{key}"
            if key == "path":
                yield location, item
            else:
                yield from _iter_paths(item, location)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _iter_paths(item, f"{prefix}[{index}]")


def _processor_argv(path: Path) -> tuple[str, ...]:
    suffix = path.suffix.lower()
    if suffix == ".py":
        return ("uv", "run", str(path))
    if suffix in {".js", ".ts"}:
        return ("node", str(path))
    if suffix == ".ps1":
        return ("pwsh", "-NoProfile", "-File", str(path))
    if suffix == ".sh":
        return (str(path),)
    return (str(path),)


def _config(spec: AgentSpec):
    if spec.execution is None:
        raise DefinitionError(f"skill '{spec.name}' has no Agents Live execution metadata")
    return spec.execution
