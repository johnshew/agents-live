"""GitHub Copilot CLI provider plugin."""
from __future__ import annotations

from ..values import Completion, Launch, RawOutput, Request, ResolvedSpec


class CopilotProvider:
    name = "copilot"
    models: frozenset[str] | None = None
    efforts = frozenset({"low", "medium", "high", "xhigh"})

    def prepare(self, spec: ResolvedSpec, request: Request) -> Launch:
        environment = dict(spec.env)
        flags = ["--autopilot", "--no-ask-user", "--no-custom-instructions"]
        if spec.mode == "write":
            flags.insert(0, "--allow-all-tools")
        else:
            flags.extend(("--deny-tool", "shell", "--deny-tool", "write"))
            for tool in spec.allow_tools:
                flags.extend(("--allow-tool", tool))
        argv = ["copilot", "-p", spec.prompt, *flags]
        if spec.model:
            argv.extend(("--model", spec.model))
        for mcp in spec.mcps:
            argv.extend(("--mcp", mcp))
        pipeline_config = environment.get("PIPELINE_MCP_COPILOT_CONFIG")
        if pipeline_config:
            argv.extend(("--additional-mcp-config", f"@{pipeline_config}"))
        return Launch(
            tuple(argv),
            spec.env,
            timeout=None,
            use_pty=True,
            filters_tui_noise=True,
            provider=self.name,
        )

    def parse(self, raw: RawOutput) -> Completion:
        lines = [
            line for line in raw.stdout.splitlines()
            if not line.startswith(("GitHub Copilot", "Usage:", "Tip:"))
        ]
        return Completion("\n".join(lines).strip())


COPILOT = CopilotProvider()
