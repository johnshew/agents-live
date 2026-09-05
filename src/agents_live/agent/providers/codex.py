"""OpenAI Codex CLI provider plugin."""
from __future__ import annotations

import json
import re
from collections.abc import Mapping

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
    ToolCall,
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
        help_argvs=(("--help",), ("exec", "--help")),
        install_commands=(
            ("linux", "curl -fsSL https://chatgpt.com/codex/install.sh | sh"),
            ("wsl", "curl -fsSL https://chatgpt.com/codex/install.sh | sh"),
            ("windows", "powershell -ExecutionPolicy ByPass -c \"irm https://chatgpt.com/codex/install.ps1 | iex\""),
        ),
    )
    capabilities = ProviderCapabilities(
        modes=frozenset({"plan", "write"}),
        mcp_transports=frozenset({"http", "stdio"}),
        structured_output=True,
        models=None,
        efforts=frozenset({"low", "medium", "high", "xhigh"}),
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
        for server in spec.mcps:
            error = _mcp_error(server.name, server.definition)
            if error is not None:
                return error
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
            "--config", "sandbox_workspace_write.exclude_slash_tmp=true",
            "--config", "sandbox_workspace_write.exclude_tmpdir_env_var=true",
        ]
        if spec.model:
            argv.extend(("--model", spec.model))
        if spec.effort:
            argv.extend(("--config", f'model_reasoning_effort="{spec.effort}"'))
        for server in spec.mcps:
            argv.extend(_mcp_argv(server.name, server.definition))
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
            final,
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
            if not isinstance(item, dict):
                continue
            item_type = item.get("type")
            if item_type == "agent_message":
                text = item.get("text")
                if isinstance(text, str) and text.strip():
                    turns.append(TranscriptTurn("assistant", text.strip()))
            elif item_type == "mcp_tool_call":
                server = item.get("server")
                tool = item.get("tool")
                if isinstance(server, str) and isinstance(tool, str):
                    turns.append(TranscriptTurn(
                        "assistant",
                        tool_calls=(ToolCall(
                            f"{server}/{tool}", item.get("arguments")),),
                    ))
            elif item_type == "command_execution":
                command = item.get("command")
                if isinstance(command, str):
                    turns.append(TranscriptTurn(
                        "assistant", tool_calls=(ToolCall("shell", command),)))
            elif item_type == "file_change":
                turns.append(TranscriptTurn(
                    "assistant",
                    tool_calls=(ToolCall("write", item.get("changes")),),
                ))
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


def _mcp_error(name: str, definition: Mapping[str, object]) -> str | None:
    prefix = f"MCP server {name}"
    transport = definition.get("type")
    if transport in {None, "stdio", "local"}:
        if not isinstance(definition.get("command"), str):
            return f"{prefix} requires a string command"
        allowed = {"type", "command", "args", "env", "cwd"}
    elif transport == "http":
        if not isinstance(definition.get("url"), str):
            return f"{prefix} requires a string url"
        allowed = {
            "type", "url", "bearer_token_env_var", "headers",
            "http_headers", "env_http_headers",
        }
    else:
        return f"{prefix} uses unsupported transport {transport}"
    unknown = set(definition) - allowed
    if unknown:
        return f"{prefix} has unsupported fields: {', '.join(sorted(unknown))}"
    for field in ("args",):
        value = definition.get(field)
        if value is not None and (
                not isinstance(value, list)
                or not all(isinstance(item, str) for item in value)):
            return f"{prefix} field {field} must be an array of strings"
    for field in ("env", "headers", "http_headers", "env_http_headers"):
        value = definition.get(field)
        if value is not None and (
                not isinstance(value, dict)
                or not all(isinstance(key, str) and isinstance(item, str)
                           for key, item in value.items())):
            return f"{prefix} field {field} must be an object of strings"
    for field in ("cwd", "bearer_token_env_var"):
        value = definition.get(field)
        if value is not None and not isinstance(value, str):
            return f"{prefix} field {field} must be a string"
    return None


def _mcp_argv(name: str, definition: Mapping[str, object]) -> tuple[str, ...]:
    key = _toml_key(name)
    values: list[tuple[str, object]] = [
        ("required", True),
        ("default_tools_approval_mode", "approve"),
    ]
    if definition.get("type") in {None, "stdio", "local"}:
        values.append(("command", definition["command"]))
        for field in ("args", "env", "cwd"):
            if field in definition:
                values.append((field, definition[field]))
    else:
        values.append(("url", definition["url"]))
        headers = definition.get("http_headers", definition.get("headers"))
        if headers is not None:
            values.append(("http_headers", headers))
        for field in ("bearer_token_env_var", "env_http_headers"):
            if field in definition:
                values.append((field, definition[field]))
    argv: list[str] = []
    for field, value in values:
        argv.extend(("--config", f"mcp_servers.{key}.{field}={_toml(value)}"))
    return tuple(argv)


def _toml_key(value: str) -> str:
    return value if re.fullmatch(r"[A-Za-z0-9_-]+", value) else _toml(value)


def _toml(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, list):
        return "[" + ",".join(_toml(item) for item in value) + "]"
    if isinstance(value, dict):
        return "{" + ",".join(
            f"{_toml_key(str(key))}={_toml(item)}"
            for key, item in sorted(value.items())
        ) + "}"
    raise TypeError(f"unsupported TOML value: {type(value).__name__}")


CODEX = CodexProvider()