"""Immutable records for the pure agent port."""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256
from pathlib import Path
from typing import Mapping


class Step(StrEnum):
    PRE = "pre"
    AGENT = "agent"
    POST = "post"


@dataclass(frozen=True)
class ProviderSelector:
    provider: str
    model: str | None = None
    effort: str | None = None

    @property
    def canonical(self) -> str:
        model = f"/{self.model}" if self.model and self.model != "default" else ""
        effort = f":{self.effort}" if self.effort else ""
        return f"{self.provider}{model}{effort}"


@dataclass(frozen=True)
class SkillProperties:
    name: str
    description: str
    license: str | None
    compatibility: str | None
    metadata: tuple[tuple[str, str], ...]
    allowed_tools: str | None


@dataclass(frozen=True)
class AgentsLiveConfig:
    schema_version: str
    schedules: tuple[str, ...]
    watch: str | None
    selector: ProviderSelector
    mode: str
    result_path: str | None
    allow_tools: tuple[str, ...]
    mcps: tuple[str, ...]
    env: tuple[tuple[str, str], ...]
    transcript: bool
    timeout: int | None
    pre_processor: str | None
    post_processor: str | None
    output_schema: dict | str | None
    output_max_bytes: int | None
    output_path_roots: tuple[str, ...]
    output_provenance: str | None


@dataclass(frozen=True)
class AgentSpec:
    root: Path
    skill_root: Path
    prompt_path: Path
    properties: SkillProperties
    execution: AgentsLiveConfig | None
    body: str
    unknown_metadata: tuple[str, ...] = ()
    pipeline_puts: tuple[tuple[str, object], ...] = ()

    @property
    def name(self) -> str:
        return self.properties.name

    @property
    def identifier(self) -> str:
        return identifier_for(self.root, self.prompt_path, self.name)


@dataclass(frozen=True)
class BrokenDefinition:
    path: Path
    message: str

    @property
    def name(self) -> str:
        """The name it would be addressed by, known without parsing it."""
        return self.path.parent.name if self.path.name == "SKILL.md" else self.path.stem

    def identifier_in(self, root: Path) -> str:
        """Its canonical identifier, which needs the path and name only."""
        return identifier_for(root, self.path, self.name)


def identifier_for(root: Path, prompt_path: Path, name: str) -> str:
    relative = prompt_path.resolve().relative_to(
        root.resolve()).as_posix().casefold()
    path_hash = sha256(relative.encode("utf-8")).hexdigest()[:10]
    return f"{name}-{path_hash}"


@dataclass(frozen=True)
class Discovery:
    """What a discovery root holds: the definitions that loaded, and why the
    rest did not. Keeping both lets a caller act on the healthy ones."""

    specs: tuple[AgentSpec, ...]
    broken: tuple[BrokenDefinition, ...] = ()


@dataclass(frozen=True)
class RunShape:
    has_pre: bool
    has_agent: bool
    has_post: bool
    needs_mcp: bool


@dataclass(frozen=True)
class Request:
    text: str = ""
    changed_files: tuple[str, ...] = ()
    env: tuple[tuple[str, str], ...] = ()
    options: tuple[tuple[str, str | bool], ...] = ()


@dataclass(frozen=True)
class StepFiles:
    """Where a step may write control, logs, and an oversized result.

    Named, never created: a processor that writes none of them leaves
    nothing behind, which matters because nothing prunes the run
    directory yet (#259).
    """

    control: Path
    log: Path
    output: Path


@dataclass(frozen=True)
class StepSignals:
    """What a step said through a channel other than its streams."""

    control: dict | None = None
    output: str | None = None


@dataclass(frozen=True)
class StepContext:
    request: Request
    pre: "StepResult | None" = None
    agent: "StepResult | None" = None
    resource_env: tuple[tuple[str, str], ...] = ()
    run_id: str = ""
    origin: str = ""
    attempt: int = 1
    scratch: Path | None = None
    result_snapshot: str | None = None


@dataclass(frozen=True)
class McpServer:
    name: str
    definition: Mapping[str, object]


@dataclass(frozen=True)
class PipelineEndpoint:
    """The run-scoped pipeline MCP server, described without owning it.

    A provider renders its own client configuration from this; the
    pipeline runtime keeps the server, and no host object crosses the
    port.
    """

    name: str
    url: str
    token: str
    bridge_command: tuple[str, ...] = ()


@dataclass(frozen=True)
class ProviderRuntime:
    """What one run offers a provider before anything is materialized."""

    mode: str
    mcps: tuple[McpServer, ...] = ()
    pipeline: PipelineEndpoint | None = None
    output_schema: dict | None = None


@dataclass(frozen=True)
class RunArtifact:
    """A run-scoped file or directory a provider needs before launch.

    The provider describes it; dispatch decides where it lands, creates
    it with these permissions, binds ``env`` to its path, and removes it
    when the run ends. ``relative_path`` is resolved under the run's own
    scratch directory and may not escape it.
    """

    relative_path: str
    kind: str = "file"
    text: str | None = None
    mode: int = 0o600
    env: tuple[str, ...] = ()


@dataclass(frozen=True)
class ProviderCli:
    """How a host reaches this provider's command-line tool.

    ``executable`` is ``None`` for a provider that launches no native
    CLI, and such a provider is never probed or offered installation
    guidance. ``probe_argv`` carries the tokens that follow it for a
    liveness check, so a nested command (``copilot help``) is describable
    without a caller knowing the provider's name.
    """

    executable: str | None = None
    probe_argv: tuple[str, ...] = ()
    install_commands: tuple[tuple[str, str], ...] = ()

    def install_command(self, host: str) -> str | None:
        for candidate, command in self.install_commands:
            if candidate == host:
                return command
        return None


@dataclass(frozen=True)
class ProviderCapabilities:
    """What a provider can be asked for, checked before a process starts.

    Anything not listed is unsupported: an unknown mode, transport,
    model, or effort fails closed rather than reaching a CLI that would
    silently ignore the safety guarantee it stands for.
    """

    modes: frozenset[str]
    mcp_transports: frozenset[str] = frozenset()
    structured_output: bool = False
    models: frozenset[str] | None = None
    efforts: frozenset[str] = frozenset()


@dataclass(frozen=True)
class ToolCall:
    name: str
    arguments: object | None = None


@dataclass(frozen=True)
class TranscriptTurn:
    role: str
    text: str = ""
    tool_calls: tuple[ToolCall, ...] = ()


@dataclass(frozen=True)
class TranscriptSource:
    """One recorded run, as the reader found it on disk."""

    stdout: str
    argv: tuple[str, ...] = ()
    prompt: str | None = None


@dataclass(frozen=True)
class ProviderTranscript:
    """One recorded conversation, in provider-neutral terms."""

    turns: tuple[TranscriptTurn, ...] = ()
    final: str | None = None
    structured: object | None = None
    prompt: str | None = None


@dataclass(frozen=True)
class ResolvedSpec:
    name: str
    prompt: str
    mode: str
    allow_tools: tuple[str, ...]
    mcps: tuple[McpServer, ...]
    env: tuple[tuple[str, str], ...]
    provider: str
    model: str | None
    effort: str | None
    output_schema: dict | None = None


@dataclass(frozen=True)
class Launch:
    argv: tuple[str, ...]
    env: tuple[tuple[str, str], ...] = ()
    cwd: str | None = None
    input_text: str | None = None
    timeout: int | None = None
    use_pty: bool = False
    filters_tui_noise: bool = False
    provider: str | None = None
    prompt: str | None = None


@dataclass(frozen=True)
class RawOutput:
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False


@dataclass(frozen=True)
class Completion:
    text: str
    structured: object | None = None
    usage: tuple[tuple[str, str | None], ...] = ()
    transcript: str | None = None


@dataclass(frozen=True)
class StepResult:
    step: Step
    ok: bool
    skip: bool = False
    text: str = ""
    structured: object | None = None
    retryable: bool = False
    category: str | None = None
    message: str = ""
    usage: tuple[tuple[str, str | None], ...] = ()
    transcript: str | None = None


@dataclass(frozen=True)
class Outcome:
    ok: bool
    status: str
    text: str = ""
    structured: object | None = None
    category: str | None = None
    message: str = ""
    usage: tuple[tuple[str, str | None], ...] = ()
    transcript: str | None = None
    run_id: str | None = None
    result_status: str | None = None
