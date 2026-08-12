"""Claude Code provider plugin."""
from __future__ import annotations

import json

from ..values import Completion, Launch, RawOutput, Request, ResolvedSpec
from ..mcp import write_mcp_config


class ClaudeProvider:
    name = "claude"
    models: frozenset[str] | None = None
    efforts = frozenset({"low", "medium", "high", "xhigh", "max"})

    def prepare(self, spec: ResolvedSpec, request: Request) -> Launch:
        environment = dict(spec.env)
        tools = list(spec.allow_tools)
        if spec.mode == "plan":
            tools = tools or ["Read", "Glob", "Grep"]
            invalid = set(tools) - {"Read", "Glob", "Grep"}
            if invalid:
                raise ValueError(
                    f"plan mode cannot allow tools: {', '.join(sorted(invalid))}")
            mode = ["--permission-mode", "default", "--allowedTools", *tools]
        elif spec.mode == "pipeline":
            tools = tools or ["mcp__pipeline__get", "mcp__pipeline__put"]
            mode = [
                "--permission-mode", "default", "--strict-mcp-config",
                "--allowedTools", *tools,
            ]
        else:
            mode = ["--dangerously-skip-permissions"]
        argv = [
            "claude", "-p", spec.prompt, "--output-format", "json",
            "--append-system-prompt", "Follow the loaded Agent Skill exactly.",
            *mode,
        ]
        if spec.model:
            argv.extend(("--model", spec.model))
        if spec.effort:
            argv.extend(("--effort", spec.effort))
        for mcp in spec.mcps:
            argv.extend(("--mcp", mcp.name))
        project_config = write_mcp_config(spec.mcps)
        if project_config:
            argv.extend(("--mcp-config", project_config))
        pipeline_config = environment.get("PIPELINE_MCP_CLAUDE_CONFIG")
        if pipeline_config:
            argv.extend(("--mcp-config", pipeline_config))
        return Launch(
            tuple(argv),
            spec.env,
            timeout=None,
            provider=self.name,
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
        return Completion(
            text if isinstance(text, str) else raw.stdout.strip(),
            usage=usage_values,
            transcript=payload.get("session_id")
            if isinstance(payload.get("session_id"), str) else None,
        )


CLAUDE = ClaudeProvider()
