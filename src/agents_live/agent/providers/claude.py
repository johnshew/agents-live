"""Claude Code provider plugin."""
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

PLAN_TOOLS = frozenset({"Read", "Glob", "Grep"})
PROJECT_MCP_CONFIG = "AGENTS_LIVE_CLAUDE_PROJECT_MCP"
PIPELINE_MCP_CONFIG = "AGENTS_LIVE_CLAUDE_PIPELINE_MCP"


class ClaudeProvider(ProviderBase):
    name = "claude"
    cli = ProviderCli(
        executable="claude",
        probe_argv=("--version",),
        install_commands=(("windows", "winget install Anthropic.ClaudeCode"),),
    )
    capabilities = ProviderCapabilities(
        modes=frozenset({"plan", "write", "pipeline"}),
        mcp_transports=frozenset({"http", "sse", "stdio"}),
        structured_output=True,
        models=None,
        efforts=frozenset({"low", "medium", "high", "xhigh", "max"}),
    )

    def validate(self, spec: ResolvedSpec) -> str | None:
        error = super().validate(spec)
        if error is not None:
            return error
        if spec.mode == "plan":
            invalid = set(spec.allow_tools) - PLAN_TOOLS
            if invalid:
                return f"plan mode cannot allow tools: {', '.join(sorted(invalid))}"
        return None

    def artifacts(self, runtime: ProviderRuntime) -> tuple[RunArtifact, ...]:
        """The MCP configuration this run needs on disk before launch.

        Claude reaches the pipeline server over HTTP itself, so the
        endpoint's stdio bridge command is not used here.
        """
        artifacts: list[RunArtifact] = []
        if runtime.mcps:
            artifacts.append(RunArtifact(
                "claude-project-mcp.json",
                text=json.dumps(
                    {"mcpServers": {
                        server.name: dict(server.definition)
                        for server in runtime.mcps}},
                    sort_keys=True),
                env=(PROJECT_MCP_CONFIG,),
            ))
        endpoint = runtime.pipeline
        if endpoint is not None:
            artifacts.append(RunArtifact(
                "claude-pipeline-mcp.json",
                text=json.dumps({"mcpServers": {endpoint.name: {
                    "type": "http",
                    "url": endpoint.url,
                    "headers": {"Authorization": f"Bearer {endpoint.token}"},
                }}}),
                env=(PIPELINE_MCP_CONFIG,),
            ))
        return tuple(artifacts)

    def prepare(self, spec: ResolvedSpec, request: Request) -> Launch:
        environment = dict(spec.env)
        tools = list(spec.allow_tools)
        if spec.mode == "plan":
            tools = tools or sorted(PLAN_TOOLS)
            mode = ["--permission-mode", "default", "--allowedTools", *tools]
        elif spec.mode == "pipeline":
            tools = tools or ["mcp__pipeline__get", "mcp__pipeline__put"]
            mode = [
                "--permission-mode", "default",
                "--allowedTools", *tools,
            ]
        else:
            mode = ["--dangerously-skip-permissions"]
        argv = [
            "claude", "-p", "--bare", "--strict-mcp-config",
            "--output-format", "json",
            "--append-system-prompt", "Follow the loaded Agent Skill exactly.",
            *mode,
        ]
        if spec.model:
            argv.extend(("--model", spec.model))
        if spec.effort:
            argv.extend(("--effort", spec.effort))
        if spec.output_schema is not None:
            argv.extend((
                "--json-schema",
                json.dumps(spec.output_schema, sort_keys=True, separators=(",", ":")),
            ))
        for variable in (PROJECT_MCP_CONFIG, PIPELINE_MCP_CONFIG):
            config = environment.get(variable)
            if config:
                argv.extend(("--mcp-config", config))
        return Launch(
            tuple(argv),
            spec.env,
            # On stdin, not in argv: Windows caps a command line at 32767
            # characters, so a prompt passed as an argument is the one
            # handoff with a hard limit. `-p` with no text reads stdin.
            input_text=spec.prompt,
            timeout=None,
            provider=self.name,
            prompt=spec.prompt,
        )

    def parse(self, raw: RawOutput) -> Completion:
        try:
            payload = json.loads(raw.stdout)
        except json.JSONDecodeError:
            return Completion(raw.stdout.strip())
        if not isinstance(payload, dict):
            return Completion(raw.stdout.strip(), payload)
        text = payload.get("result")
        usage = payload.get("usage")
        usage_values = (
            tuple(sorted((str(key), str(value)) for key, value in usage.items()))
            if isinstance(usage, dict) else ()
        )
        total_cost = payload.get("total_cost_usd")
        if isinstance(total_cost, (int, float)):
            usage_values += (("list_cost_usd", str(total_cost)),)
        return Completion(
            text if isinstance(text, str) else raw.stdout.strip(),
            structured=payload.get("structured_output"),
            usage=usage_values,
            transcript=payload.get("session_id")
            if isinstance(payload.get("session_id"), str) else None,
        )

    def transcript(self, source: TranscriptSource) -> ProviderTranscript:
        try:
            payload = json.loads(source.stdout)
        except json.JSONDecodeError:
            payload = None
        final: object = None
        structured: object = None
        if isinstance(payload, dict):
            final = payload.get("result")
            structured = payload.get("structured_output")
        if not isinstance(final, str):
            final = source.stdout.strip() or None
        return ProviderTranscript(
            turns=(TranscriptTurn("assistant", final),) if final else (),
            final=final,
            structured=structured,
            prompt=source.prompt,
        )


CLAUDE = ClaudeProvider()
