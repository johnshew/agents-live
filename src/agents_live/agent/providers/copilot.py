"""GitHub Copilot CLI provider plugin."""
from __future__ import annotations

import re

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
            argv.extend(("--mcp", mcp.name))
        project_config = environment.get("AGENTS_LIVE_PROJECT_MCP_CONFIG")
        if project_config:
            argv.extend(("--additional-mcp-config", f"@{project_config}"))
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
        return Completion("\n".join(lines).strip(), usage=_usage(raw.stdout))


_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_CREDITS = re.compile(r"^\s*AI Credits\s+([0-9]+(?:\.[0-9]+)?)", re.MULTILINE)
_TOKENS = re.compile(
    r"^\s*Tokens\s+\S\s*([0-9.]+[kKmM]?)"
    r"(?:\s*\(([0-9.]+[kKmM]?) cached\))?"
    r".*?\S\s*([0-9.]+[kKmM]?)", re.MULTILINE)


def _usage(stdout: str) -> tuple[tuple[str, str | None], ...]:
    """What the CLI reported it spent, from its own session footer.

    AI credits are retained as the native quantity. GitHub defines one credit
    as a $0.01 list-price equivalent, so the adapter also emits normalized
    ``list_cost_usd``. That is not an invoice charge: allowances, promotions,
    and negotiated discounts remain outside provider run telemetry.
    """
    text = _ANSI.sub("", stdout)
    values: list[tuple[str, str | None]] = []
    credits = _CREDITS.search(text)
    if credits:
        credit_value = credits.group(1)
        values.append(("ai_credits", credit_value))
        values.append(("list_cost_usd", str(float(credit_value) * 0.01)))
    tokens = _TOKENS.search(text)
    if tokens:
        values.append(("input_tokens", tokens.group(1)))
        if tokens.group(2):
            values.append(("cached_tokens", tokens.group(2)))
        values.append(("output_tokens", tokens.group(3)))
    return tuple(values)


COPILOT = CopilotProvider()
