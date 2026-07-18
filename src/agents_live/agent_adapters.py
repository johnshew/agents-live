"""Agent adapter registry (proposal §6.7).

Each supported agent CLI is described by one immutable adapter record;
everything agent-specific in the runner keys off the adapter, never off
name-string membership tests. The public kernel registers ``claude`` and
``copilot``; EntraID-authenticated variants live in the private
``agency_adapters`` module, discovered via the import hook at the bottom
and absent from the public export - no compatibility shim.

Behavior lives at two levels:

* **Family behavior** (``_ClaudeBehavior`` / ``_CopilotBehavior``): flag
  building per mode, the headless invocation shape, pipeline-MCP config
  routing, and the family's capabilities (JSON output envelope,
  workspace-MCP confinement, stdio-MCP routing, transcript --share).
  Adapters delegate via ``AgentAdapter.behavior``, keyed off ``family``,
  so registrations stay plain data (``AgentAdapter(name=..., family=...)``).
* **Per-CLI quirks** that differ WITHIN a family are adapter fields:
  ``use_pty`` and ``filters_tui_noise`` are True for the public
  ``copilot`` CLI but not for ``agency copilot``.

Fail-closed by construction (TT-SEC-002 anchor): an unknown runtime name,
an unsupported family, or a mode the adapter does not declare enforceable
raises instead of falling through to a permissive default. Deeper
capability *proof* (verifying the installed CLI actually supports the
restriction flags) lands with the security backlog's effective-capability
work; this registry is where that check plugs in.

stdlib-only; sibling scripts import it flat. Must not import headless
(headless imports this module).
"""
from __future__ import annotations

from dataclasses import dataclass, field


class UnknownRuntimeError(ValueError):
    """No adapter registered under that name, or an adapter whose family
    has no behavior (fail closed at the caller)."""


# Default tool allowlists per mode, used when the agent frontmatter
# declares none.
PLAN_CLAUDE_DEFAULT_TOOLS = ["Read", "Glob", "Grep"]
PIPELINE_CLAUDE_DEFAULT_TOOLS = [
    "mcp__pipeline__get",
    "mcp__pipeline__put",
]
PIPELINE_COPILOT_DEFAULT_TOOLS = ["pipeline"]


class _FamilyBehavior:
    """Behavior shared by every adapter of one family.

    The base class declares the contract only — there is deliberately no
    permissive default implementation for flag building.
    """

    # stdout is a ``--output-format json`` envelope to unwrap
    json_envelope = False
    # npx-stdio MCP servers route via --additional-mcp-config, not --mcp
    stdio_mcps_via_config = False
    # workspace MCP servers must be provably disabled per invocation
    confines_workspace_mcps = False
    # supports --share session-transcript capture
    shares_transcript = False

    def mode_flags(self, mode: str, allow_tools: list[str]) -> list[str]:
        """Permission/tool flags for *mode*. Caller has already validated
        the mode against VALID_MODES and the adapter's declared modes."""
        raise NotImplementedError

    def headless_flags(self, system_prompt: str) -> list[str]:
        """Flags every headless invocation of this family gets."""
        raise NotImplementedError

    def pipeline_mcp_flags(self, env: dict[str, str]) -> list[str]:
        """Flags binding the agent to the pipeline MCP server, from the
        config path the pipeline runtime published into *env*."""
        raise NotImplementedError


class _ClaudeBehavior(_FamilyBehavior):
    json_envelope = True

    def mode_flags(self, mode: str, allow_tools: list[str]) -> list[str]:
        if mode == "plan":
            # Not --permission-mode plan: on claude CLI >= 2.1.x headless plan
            # mode can derail into the CLI's plan-file/approval workflow, which
            # has no approver under -p (observed 2026-07-10: 120s timeout, then
            # "I've written the plan to ~/.claude/plans/..." instead of the
            # agent's required output). Read-only is enforced by the allowlist
            # instead: headless -p auto-denies every tool not listed here.
            tools = allow_tools or list(PLAN_CLAUDE_DEFAULT_TOOLS)
            disallowed = set(tools) - set(PLAN_CLAUDE_DEFAULT_TOOLS)
            if disallowed:
                raise ValueError(
                    f"plan mode cannot allow tools: {', '.join(sorted(disallowed))}")
            return ["--permission-mode", "default", "--allowedTools", *tools]
        if mode == "pipeline":
            tools = allow_tools or list(PIPELINE_CLAUDE_DEFAULT_TOOLS)
            disallowed = set(tools) - set(PIPELINE_CLAUDE_DEFAULT_TOOLS)
            if disallowed:
                raise ValueError(
                    f"pipeline mode cannot allow tools: {', '.join(sorted(disallowed))}")
            # No --tools flag here: on claude CLI >= 2.1.201 any --tools value
            # (including "") strips MCP tools too, leaving the agent unable to
            # reach the pipeline server. Headless -p auto-denies every tool
            # not in --allowedTools, so the allowlist alone enforces the
            # side-channel-only boundary (built-ins exist but are denied).
            return [
                "--permission-mode", "default",
                "--strict-mcp-config",
                "--allowedTools",
                *tools,
            ]
        return ["--dangerously-skip-permissions"]

    def headless_flags(self, system_prompt: str) -> list[str]:
        return ["--output-format", "json", "--append-system-prompt", system_prompt]

    def pipeline_mcp_flags(self, env: dict[str, str]) -> list[str]:
        cfg = env.get("PIPELINE_MCP_CLAUDE_CONFIG")
        return ["--mcp-config", cfg] if cfg else []


class _CopilotBehavior(_FamilyBehavior):
    stdio_mcps_via_config = True
    confines_workspace_mcps = True
    shares_transcript = True

    def mode_flags(self, mode: str, allow_tools: list[str]) -> list[str]:
        if mode == "write":
            return ["--allow-all-tools", "--autopilot"]
        flags = ["--autopilot"]
        if mode == "pipeline":
            tools = allow_tools or list(PIPELINE_COPILOT_DEFAULT_TOOLS)
            disallowed = set(tools) - set(PIPELINE_COPILOT_DEFAULT_TOOLS)
            if disallowed:
                raise ValueError(
                    f"pipeline mode cannot allow tools: {', '.join(sorted(disallowed))}")
            flags.extend(["--deny-tool", "shell", "--deny-tool", "write"])
            for tool_name in tools:
                flags.extend(["--allow-tool", tool_name])
            return flags
        # plan
        disallowed = {"shell", "write"} & set(allow_tools)
        if disallowed:
            raise ValueError(
                f"plan mode cannot allow tools: {', '.join(sorted(disallowed))}")
        flags.extend(["--deny-tool", "shell", "--deny-tool", "write"])
        for tool_name in allow_tools:
            flags.extend(["--allow-tool", tool_name])
        return flags

    def headless_flags(self, system_prompt: str) -> list[str]:
        # Copilot takes no system-prompt injection; suppress interactivity
        # and repo custom instructions instead.
        return ["--no-ask-user", "--no-custom-instructions"]

    def pipeline_mcp_flags(self, env: dict[str, str]) -> list[str]:
        cfg = env.get("PIPELINE_MCP_COPILOT_CONFIG")
        return ["--additional-mcp-config", f"@{cfg}"] if cfg else []


_FAMILIES: dict[str, _FamilyBehavior] = {
    "claude": _ClaudeBehavior(),
    "copilot": _CopilotBehavior(),
}


@dataclass(frozen=True)
class AgentAdapter:
    name: str                     # frontmatter `runtime:` value
    binary: tuple[str, ...]       # argv prefix that launches the CLI
    family: str                   # "claude" | "copilot": flag/parse behavior
    private: bool = False         # True: ships only in this deployment
    # Modes this adapter can enforce headlessly. The flag builder refuses
    # any mode not declared here rather than guessing at flags.
    modes: frozenset[str] = field(
        default_factory=lambda: frozenset({"plan", "write", "pipeline"}))
    # Per-CLI quirks that differ within a family:
    use_pty: bool = False           # drive through a pseudo-terminal
    filters_tui_noise: bool = False  # strip TUI banner/usage noise from stdout

    @property
    def behavior(self) -> _FamilyBehavior:
        try:
            return _FAMILIES[self.family]
        except KeyError:
            raise UnknownRuntimeError(
                f"runtime '{self.name}' has unsupported adapter family "
                f"'{self.family}'") from None

    def mode_flags(self, mode: str, allow_tools: list[str]) -> list[str]:
        return self.behavior.mode_flags(mode, allow_tools)

    def headless_flags(self, system_prompt: str) -> list[str]:
        return self.behavior.headless_flags(system_prompt)

    def pipeline_mcp_flags(self, env: dict[str, str]) -> list[str]:
        return self.behavior.pipeline_mcp_flags(env)


_REGISTRY: dict[str, AgentAdapter] = {}


def register(adapter: AgentAdapter) -> None:
    """Add an adapter to the registry, validating at register time
    (2026-07-12 review Low): a plugin's bad registration fails loudly at
    load, never at dispatch. Re-registering an IDENTICAL record is
    tolerated (the flat import hook and the entry-point plugin can both
    fire for the same adapters during the transition); a conflicting
    record for an existing name is rejected."""
    if not adapter.name or not adapter.name.strip():
        raise ValueError("adapter registration rejected: empty name")
    if not adapter.binary or not all(adapter.binary):
        raise ValueError(
            f"adapter '{adapter.name}' registration rejected: empty binary")
    if adapter.family not in _FAMILIES:
        raise ValueError(
            f"adapter '{adapter.name}' registration rejected: unsupported "
            f"family '{adapter.family}' (known: {', '.join(sorted(_FAMILIES))})")
    existing = _REGISTRY.get(adapter.name)
    if existing is not None and existing != adapter:
        raise ValueError(
            f"adapter '{adapter.name}' registration rejected: conflicts "
            f"with an already-registered adapter")
    _REGISTRY[adapter.name] = adapter


def get(name: str) -> AgentAdapter:
    try:
        return _REGISTRY[name]
    except KeyError as exc:
        raise UnknownRuntimeError(f"unknown runtime '{name}'") from exc


def names() -> list[str]:
    return sorted(_REGISTRY)


register(AgentAdapter(name="claude", binary=("claude",), family="claude"))
register(AgentAdapter(name="copilot", binary=("copilot",), family="copilot",
                      use_pty=True, filters_tui_noise=True))

# Private-adapter discovery, two mechanisms (proposal §3.9 plugin
# pattern):
#
# 1. Entry points: an installed plugin package exposes the
#    ``agents_live.agents`` group; each entry point resolves to a module
#    (or callable) whose load registers its adapters via ``register()``.
#    A broken INSTALLED plugin raises - a deployment that installed
#    private adapters must never silently lose them.
# 2. Flat sibling import of ``agency_adapters`` (this repository's
#    pre-flip deployment). The public export omits the file, so the
#    hook is a no-op there; only that module's own absence is
#    tolerated.
def _discover_plugins() -> None:
    from importlib.metadata import entry_points
    for ep in entry_points(group="agents_live.agents"):
        loaded = ep.load()
        if callable(loaded):
            loaded()
    try:
        import importlib
        importlib.import_module("agency_adapters")
    except ModuleNotFoundError as exc:
        if exc.name != "agency_adapters":
            raise


_discover_plugins()
