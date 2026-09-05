"""GitHub Copilot CLI provider plugin."""
from __future__ import annotations

import json
import re
from decimal import Decimal

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
from .base import ProviderBase, tool_call

HOME = "COPILOT_HOME"
PROJECT_MCP_CONFIG = "AGENTS_LIVE_COPILOT_PROJECT_MCP"
PIPELINE_MCP_CONFIG = "AGENTS_LIVE_COPILOT_PIPELINE_MCP"


class CopilotProvider(ProviderBase):
    name = "copilot"
    cli = ProviderCli(
        executable="copilot",
        probe_argv=("--version",),
        install_commands=(("windows", "winget install GitHub.Copilot"),),
    )
    capabilities = ProviderCapabilities(
        modes=frozenset({"plan", "write", "pipeline"}),
        mcp_transports=frozenset({"http", "sse", "stdio"}),
        structured_output=False,
        models=None,
        efforts=frozenset({"low", "medium", "high", "xhigh"}),
    )

    def validate(self, spec: ResolvedSpec) -> str | None:
        error = super().validate(spec)
        if error is not None:
            return error
        if spec.mode == "pipeline":
            disallowed = set(spec.allow_tools) - {"pipeline"}
            if disallowed:
                return (
                    "pipeline mode cannot allow tools: "
                    + ", ".join(sorted(disallowed)))
        return None

    def artifacts(self, runtime: ProviderRuntime) -> tuple[RunArtifact, ...]:
        """The run-scoped home and MCP configuration this provider needs.

        The isolated home is what keeps an unattended session from
        adopting repository or user customizations it never agreed to
        (#375); the settings file inside it disables hooks explicitly.
        """
        artifacts = [
            RunArtifact("copilot-home", kind="directory", mode=0o700, env=(HOME,)),
            RunArtifact(
                "copilot-home/settings.json",
                text=json.dumps({"disableAllHooks": True}, sort_keys=True),
            ),
        ]
        if runtime.mcps:
            artifacts.append(RunArtifact(
                "copilot-project-mcp.json",
                text=json.dumps(
                    {"mcpServers": {
                        server.name: dict(server.definition)
                        for server in runtime.mcps}},
                    sort_keys=True),
                env=(PROJECT_MCP_CONFIG,),
            ))
        endpoint = runtime.pipeline
        if endpoint is not None and endpoint.bridge_command:
            # A stdio bridge rather than the URL directly: copilot 1.0.52
            # does not auto-connect an HTTP MCP server in `-p` mode.
            command, *arguments = endpoint.bridge_command
            artifacts.append(RunArtifact(
                "copilot-pipeline-mcp.json",
                text=json.dumps({"mcpServers": {endpoint.name: {
                    "type": "local",
                    "command": command,
                    "args": list(arguments),
                    "env": {
                        "PIPELINE_MCP_URL": endpoint.url,
                        "PIPELINE_MCP_TOKEN": endpoint.token,
                    },
                }}}),
                env=(PIPELINE_MCP_CONFIG,),
            ))
        return tuple(artifacts)

    def prepare(self, spec: ResolvedSpec, request: Request) -> Launch:
        """Ask for the machine-readable stream, not the human session.

        The CLI prints its cost footer only to an interactive terminal,
        which Windows cannot allocate, so runs there recorded no spend at
        all. Its JSON stream carries the same figures on every host, and
        it needs no pseudo-terminal: one allocated for the footer's sake
        would hand the child a terminal width to wrap those lines at.
        """
        environment = dict(spec.env)
        environment.update({
            "COPILOT_ALLOW_ALL": "false",
            "GITHUB_COPILOT_PROMPT_MODE_EXTENSIONS": "false",
            "GITHUB_COPILOT_PROMPT_MODE_REPO_HOOKS": "false",
            "GITHUB_COPILOT_PROMPT_MODE_WORKSPACE_MCP": "false",
        })
        flags = ["--autopilot", "--no-ask-user", "--no-custom-instructions"]
        if spec.mode == "write":
            flags.insert(0, "--allow-all-tools")
        elif spec.mode == "pipeline":
            tools = spec.allow_tools or ("pipeline",)
            available = (*tools, "task_complete")
            flags.extend((
                "--deny-tool", "shell",
                "--deny-tool", "write",
                "--disable-builtin-mcps",
                "--available-tools", *available,
            ))
            for tool in available:
                flags.extend(("--allow-tool", tool))
        else:
            flags.extend(("--deny-tool", "shell", "--deny-tool", "write"))
            for tool in spec.allow_tools:
                flags.extend(("--allow-tool", tool))
        argv = [
            "copilot", "-p", spec.prompt, *flags,
            "--output-format", "json",
        ]
        if spec.model:
            argv.extend(("--model", spec.model))
        for variable in (PROJECT_MCP_CONFIG, PIPELINE_MCP_CONFIG):
            config = environment.get(variable)
            if config:
                argv.extend(("--additional-mcp-config", f"@{config}"))
        return Launch(
            tuple(argv),
            tuple(sorted(environment.items())),
            timeout=None,
            provider=self.name,
            prompt=spec.prompt,
        )

    def parse(self, raw: RawOutput) -> Completion:
        json_completion = _json_completion(raw.stdout)
        if json_completion is not None:
            return json_completion
        lines = [
            line for line in raw.stdout.splitlines()
            if not line.startswith(("GitHub Copilot", "Usage:", "Tip:"))
        ]
        return Completion("\n".join(lines).strip(), usage=_usage(raw.stdout))

    def transcript(self, source: TranscriptSource) -> ProviderTranscript:
        turns = _turns(source.stdout)
        final = next(
            (turn.text for turn in reversed(turns)
             if turn.role == "assistant" and turn.text),
            None,
        )
        return ProviderTranscript(
            turns=turns,
            final=final,
            prompt=source.prompt if source.prompt is not None
            else _prompt_from_argv(source.argv),
        )


def _prompt_from_argv(argv: tuple[str, ...]) -> str | None:
    """The prompt a pre-6.2 transcript recorded only in the command line."""
    if "-p" not in argv:
        return None
    index = argv.index("-p")
    return argv[index + 1] if index + 1 < len(argv) else None


def _turns(stdout: str) -> tuple[TranscriptTurn, ...]:
    turns: list[TranscriptTurn] = []
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict) or not isinstance(event.get("data"), dict):
            continue
        if event.get("type") not in {"assistant.message", "user.message"}:
            continue
        data = event["data"]
        content = data.get("content")
        requests = data.get("toolRequests")
        calls: tuple[ToolCall, ...] = tuple(
            call for call in (
                tool_call(item) for item in requests
            ) if call is not None
        ) if isinstance(requests, list) else ()
        text = content.strip() if isinstance(content, str) else ""
        if text or calls:
            turns.append(TranscriptTurn(
                "assistant" if event["type"] == "assistant.message" else "user",
                text,
                calls,
            ))
    return tuple(turns)


_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_CREDITS = re.compile(r"^\s*AI Credits\s+([0-9]+(?:\.[0-9]+)?)", re.MULTILINE)
_TOKENS = re.compile(
    r"^\s*Tokens\s+\S\s*([0-9.]+[kKmM]?)"
    r"(?:\s*\(([0-9.]+[kKmM]?) cached\))?"
    r".*?\S\s*([0-9.]+[kKmM]?)", re.MULTILINE)
_NANO_AIU_PER_CREDIT = Decimal("1000000000")


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
        values.append((
            "list_cost_usd",
            str(Decimal(credit_value) * Decimal("0.01")),
        ))
    tokens = _TOKENS.search(text)
    if tokens:
        values.append(("input_tokens", tokens.group(1)))
        if tokens.group(2):
            values.append(("cached_tokens", tokens.group(2)))
        values.append(("output_tokens", tokens.group(3)))
    return tuple(values)


def _json_completion(stdout: str) -> Completion | None:
    assistant_messages: list[str] = []
    final_answers: list[str] = []
    task_summary = ""
    nano_aiu: Decimal | None = None
    recognized = False
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        event_type = event.get("type")
        data = event.get("data")
        if event_type == "assistant.message" and isinstance(data, dict):
            recognized = True
            content = data.get("content")
            if isinstance(content, str) and content.strip():
                phase = data.get("phase")
                if phase == "final_answer":
                    final_answers.append(content.strip())
                elif phase is None:
                    tool_requests = data.get("toolRequests")
                    completes_task = isinstance(tool_requests, list) and any(
                        isinstance(request, dict)
                        and request.get("name") == "task_complete"
                        for request in tool_requests
                    )
                    if not completes_task:
                        assistant_messages.append(content.strip())
        elif event_type == "session.task_complete" and isinstance(data, dict):
            recognized = True
            summary = data.get("summary")
            if isinstance(summary, str) and summary.strip():
                task_summary = summary.strip()
        elif event_type == "session.usage_checkpoint" and isinstance(data, dict):
            recognized = True
            value = data.get("totalNanoAiu")
            if not isinstance(value, bool) and isinstance(value, (int, float, str)):
                try:
                    candidate = Decimal(str(value))
                except ArithmeticError:
                    pass
                else:
                    if candidate.is_finite() and candidate >= 0:
                        nano_aiu = candidate
    if not recognized:
        return None
    usage: tuple[tuple[str, str | None], ...] = ()
    if nano_aiu is not None:
        credits = nano_aiu / _NANO_AIU_PER_CREDIT
        usage = (
            ("ai_credits", str(credits)),
            ("list_cost_usd", str(credits * Decimal("0.01"))),
        )
    answers = final_answers or assistant_messages
    text = answers[-1] if answers else task_summary
    return Completion(text, usage=usage)


COPILOT = CopilotProvider()
