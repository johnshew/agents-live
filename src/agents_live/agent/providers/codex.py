"""OpenAI Codex CLI provider plugin."""
from __future__ import annotations

import json

from ..values import (
    Completion,
    Launch,
    ProviderCapabilities,
    ProviderCli,
    ProviderRuntime,
    ProviderTranscript,
    RawOutput,
    Request,
    ResolvedSpec,
    RunArtifact,
    TranscriptSource,
    TranscriptTurn,
)
from .base import ProviderBase

OUTPUT_SCHEMA = "AGENTS_LIVE_CODEX_OUTPUT_SCHEMA"


class CodexProvider(ProviderBase):
    name = "codex"
    cli = ProviderCli(
        executable="codex",
        probe_argv=("--version",),
        install_commands=(
            ("linux", "curl -fsSL https://chatgpt.com/codex/install.sh | sh"),
            ("wsl", "curl -fsSL https://chatgpt.com/codex/install.sh | sh"),
        ),
    )
    capabilities = ProviderCapabilities(
        modes=frozenset({"plan", "write"}),
        structured_output=True,
        models=None,
        efforts=frozenset({"minimal", "low", "medium", "high", "xhigh"}),
    )

    def artifacts(self, runtime: ProviderRuntime) -> tuple[RunArtifact, ...]:
        if runtime.output_schema is None:
            return ()
        return (RunArtifact(
            "codex-output-schema.json",
            text=json.dumps(
                runtime.output_schema, sort_keys=True, separators=(",", ":")),
            env=(OUTPUT_SCHEMA,),
        ),)

    def validate(self, spec: ResolvedSpec) -> str | None:
        error = super().validate(spec)
        if error is not None:
            return error
        if spec.allow_tools:
            return "provider codex does not support allow-tools"
        return None

    def prepare(self, spec: ResolvedSpec, request: Request) -> Launch:
        del request
        environment = dict(spec.env)
        sandbox = "read-only" if spec.mode == "plan" else "workspace-write"
        argv = [
            "codex", "--ask-for-approval", "never", "exec", "-",
            "--json",
            "--ephemeral",
            "--ignore-user-config",
            "--ignore-rules",
            "--sandbox", sandbox,
            "--strict-config",
            "--color", "never",
        ]
        if spec.model:
            argv.extend(("--model", spec.model))
        if spec.effort:
            argv.extend(("--config", f'model_reasoning_effort="{spec.effort}"'))
        schema = environment.get(OUTPUT_SCHEMA)
        if spec.output_schema is not None and schema:
            argv.extend(("--output-schema", schema))
        return Launch(
            tuple(argv),
            spec.env,
            input_text=spec.prompt,
            timeout=None,
            provider=self.name,
            prompt=spec.prompt,
        )

    def parse(self, raw: RawOutput) -> Completion:
        turns, thread_id, usage = _events(raw.stdout)
        final = next((turn.text for turn in reversed(turns) if turn.text), "")
        structured = None
        if final:
            try:
                structured = json.loads(final)
            except json.JSONDecodeError:
                pass
        return Completion(
            final or raw.stdout.strip(),
            structured=structured,
            usage=usage,
            transcript=thread_id,
        )

    def failure(self, raw: RawOutput) -> str | None:
        text = "\n".join((raw.stderr, raw.stdout, *_error_messages(raw.stdout)))
        folded = text.casefold()
        if any(marker in folded for marker in (
                "401 unauthorized", "missing bearer", "not logged in",
                "authentication required")):
            return "authentication_failed"
        if _has_event(raw.stdout, "turn.failed"):
            return "provider_turn_failed"
        return super().failure(raw)

    def transcript(self, source: TranscriptSource) -> ProviderTranscript:
        turns, _, _ = _events(source.stdout)
        final = next((turn.text for turn in reversed(turns) if turn.text), None)
        structured = None
        if final:
            try:
                structured = json.loads(final)
            except json.JSONDecodeError:
                pass
        return ProviderTranscript(
            turns=turns,
            final=final,
            structured=structured,
            prompt=source.prompt,
        )


def _events(
    stdout: str,
) -> tuple[tuple[TranscriptTurn, ...], str | None, tuple[tuple[str, str], ...]]:
    turns: list[TranscriptTurn] = []
    thread_id = None
    usage: dict[str, str] = {}
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type") == "thread.started":
            value = event.get("thread_id")
            if isinstance(value, str) and value:
                thread_id = value
        elif event.get("type") == "item.completed":
            item = event.get("item")
            if not isinstance(item, dict) or item.get("type") != "agent_message":
                continue
            text = item.get("text")
            if isinstance(text, str) and text.strip():
                turns.append(TranscriptTurn("assistant", text.strip()))
        elif event.get("type") == "turn.completed":
            values = event.get("usage")
            if isinstance(values, dict):
                usage.update({
                    str(key): str(value) for key, value in values.items()
                    if not isinstance(value, (dict, list))
                })
    return tuple(turns), thread_id, tuple(sorted(usage.items()))


def _has_event(stdout: str, event_type: str) -> bool:
    return any(event.get("type") == event_type for event in _json_events(stdout))


def _error_messages(stdout: str) -> tuple[str, ...]:
    messages: list[str] = []
    for event in _json_events(stdout):
        if event.get("type") not in {"error", "turn.failed"}:
            continue
        error = event.get("error")
        value = error.get("message") if isinstance(error, dict) else event.get("message")
        if isinstance(value, str):
            messages.append(value)
    return tuple(messages)


def _json_events(stdout: str) -> tuple[dict, ...]:
    events = []
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return tuple(events)


CODEX = CodexProvider()