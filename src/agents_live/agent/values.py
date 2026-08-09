"""Immutable records for the pure agent port."""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256
from pathlib import Path


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

    @property
    def name(self) -> str:
        return self.properties.name

    @property
    def identifier(self) -> str:
        relative = self.prompt_path.resolve().relative_to(
            self.root.resolve()).as_posix().casefold()
        path_hash = sha256(relative.encode("utf-8")).hexdigest()[:10]
        return f"{self.name}-{path_hash}"


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


@dataclass(frozen=True)
class StepContext:
    request: Request
    pre: "StepResult | None" = None
    agent: "StepResult | None" = None
    resource_env: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class ResolvedSpec:
    name: str
    prompt: str
    mode: str
    allow_tools: tuple[str, ...]
    mcps: tuple[str, ...]
    env: tuple[tuple[str, str], ...]
    provider: str
    model: str | None
    effort: str | None


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
