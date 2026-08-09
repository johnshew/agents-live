"""Pure load, shape, prepare, interpret, and outcome functions."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Mapping

from .definition import DefinitionError, discover_definitions, load_definition
from .providers import get as get_provider
from .values import (
    AgentSpec,
    Completion,
    Discovery,
    Launch,
    Outcome,
    RawOutput,
    Request,
    ResolvedSpec,
    RunShape,
    Step,
    StepContext,
    StepResult,
)

DEFAULT_OUTPUT_MAX_BYTES = 10 * 1024 * 1024
DEFAULT_TIMEOUT_SECONDS = 120


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


def prepare(spec: AgentSpec, step: Step, ctx: StepContext) -> Launch:
    config = _config(spec)
    environment = dict(config.env)
    environment.update(ctx.request.env)
    environment.update(ctx.resource_env)
    environment["AGENTS_LIVE_AGENT_NAME"] = spec.name
    if ctx.request.changed_files:
        environment["AGENTS_LIVE_CHANGED_FILES"] = json.dumps(ctx.request.changed_files)
    if step in {Step.PRE, Step.POST}:
        reference = config.pre_processor if step is Step.PRE else config.post_processor
        if reference is None:
            raise DefinitionError(f"{step.value} processor is not declared")
        path = (spec.skill_root / reference).resolve()
        if not path.is_file():
            raise DefinitionError(f"{step.value} processor not found: {reference}")
        input_text = None
        if step is Step.POST:
            source = None if config.mode == "pipeline" else (ctx.agent or ctx.pre)
            input_text = source.text if source else None
        return Launch(
            _processor_argv(path),
            tuple(sorted(environment.items())),
            str(spec.root),
            input_text,
            config.timeout or DEFAULT_TIMEOUT_SECONDS,
        )

    prompt = spec.body
    if ctx.request.changed_files:
        listing = "\n".join(f"  - {item}" for item in ctx.request.changed_files)
        prompt = f"Files changed:\n{listing}\n\n{prompt}"
    if ctx.request.text:
        prompt = f"{ctx.request.text}\n\n{prompt}"
    if ctx.pre and ctx.pre.text:
        prompt = f"{prompt}\n\nPre-processor context:\n{ctx.pre.text}"
    selector = config.selector
    provider = get_provider(selector.provider)
    if selector.model and provider.models is not None and selector.model not in provider.models:
        raise DefinitionError(
            f"provider {provider.name} does not support model {selector.model}")
    if selector.effort and selector.effort not in provider.efforts:
        raise DefinitionError(
            f"provider {provider.name} does not support effort {selector.effort}")
    resolved = ResolvedSpec(
        spec.name,
        prompt,
        config.mode,
        config.allow_tools,
        config.mcps,
        tuple(sorted(environment.items())),
        provider.name,
        selector.model,
        selector.effort,
    )
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
    )


def interpret(
    spec: AgentSpec,
    step: Step,
    launch: Launch,
    raw: RawOutput,
) -> StepResult:
    if raw.timed_out:
        return StepResult(
            step, False, retryable=step is Step.AGENT,
            category="timeout", message="child timed out")
    if raw.returncode != 0:
        category = {
            Step.PRE: "pre_processor_crash",
            Step.POST: "post_processor_crash",
            Step.AGENT: "cli_crash",
        }[step]
        return StepResult(
            step, False, category=category,
            message=raw.stderr.strip() or f"child exited with status {raw.returncode}")
    text = raw.stdout.rstrip("\n")
    if step is Step.PRE:
        skip = False
        try:
            parsed = json.loads(text)
            skip = isinstance(parsed, dict) and bool(parsed.get("skip"))
        except json.JSONDecodeError:
            pass
        return StepResult(step, True, skip=skip, text=text)
    if step is Step.POST:
        return StepResult(step, True, text=text)
    provider = get_provider(launch.provider or _config(spec).selector.provider)
    completion = provider.parse(raw)
    if not completion.text and completion.structured is None:
        return StepResult(
            step, False, retryable=True, category="empty_output",
            message="provider returned no output")
    return _validate_completion(spec, completion, raw.stdout)


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
        return Outcome(True, "skipped", pre.text)
    final = results.get(Step.POST) or results.get(Step.AGENT) or pre
    return Outcome(
        True,
        "success",
        final.text if final else "",
        final.structured if final else None,
        usage=final.usage if final else (),
        transcript=final.transcript if final else None,
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
    return None


def _validate_schema(spec: AgentSpec, value: object) -> str | None:
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
        jsonschema.validate(value, schema)
    except jsonschema.SchemaError as exc:
        raise DefinitionError(f"output schema is invalid: {exc.message}") from exc
    except jsonschema.ValidationError as exc:
        return f"output does not conform at {exc.json_path}: {exc.validator} failed"
    return None


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
        return ("sh", str(path))
    return (str(path),)


def _config(spec: AgentSpec):
    if spec.execution is None:
        raise DefinitionError(f"skill '{spec.name}' has no Agents Live execution metadata")
    return spec.execution
