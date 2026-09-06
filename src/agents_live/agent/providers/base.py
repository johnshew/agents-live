"""Provider-neutral defaults for the integration contract.

A provider is free to implement the contract from scratch; subclassing
this only saves it from restating behavior that is not provider-specific
(capability checks, generic CLI failure classification, and a JSON
transcript shape). Everything here stays pure: no process, filesystem,
or server object is reachable from a provider.
"""
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
    ToolCall,
    TranscriptSource,
    TranscriptTurn,
)

# What a CLI says when it was handed something it does not understand.
# Provider-neutral because it describes argument parsers, not vendors.
_ARGUMENT_REJECTIONS = (
    "unexpected value",
    "unexpected argument",
    "unknown argument",
    "unknown option",
    "unrecognized argument",
    "unrecognized option",
    "unrecognized arguments",
    "invalid argument",
    "invalid option",
    "invalid value",
)


class ProviderBase:
    """Default contract behavior shared by providers that want it."""

    name: str = ""
    cli: ProviderCli = ProviderCli()
    capabilities: ProviderCapabilities = ProviderCapabilities(frozenset())

    def validate(self, spec: ResolvedSpec) -> str | None:
        """Why this provider cannot run *spec*, before any process starts."""
        capabilities = self.capabilities
        if spec.mode not in capabilities.modes:
            return (
                f"provider {self.name} does not support mode {spec.mode}; "
                f"supported: {', '.join(sorted(capabilities.modes))}"
            )
        if spec.model and capabilities.models is not None and (
                spec.model not in capabilities.models):
            return f"provider {self.name} does not support model {spec.model}"
        if spec.effort and spec.effort not in capabilities.efforts:
            return f"provider {self.name} does not support effort {spec.effort}"
        for server in spec.mcps:
            transport = mcp_transport(server.definition)
            if transport not in capabilities.mcp_transports:
                return (
                    f"provider {self.name} does not support the {transport} "
                    f"transport required by MCP server {server.name}"
                )
        return None

    def artifacts(self, runtime: ProviderRuntime) -> tuple[RunArtifact, ...]:
        """The run-scoped files this provider needs dispatch to create."""
        del runtime
        return ()

    def prepare(self, spec: ResolvedSpec, request: Request) -> Launch:
        raise NotImplementedError

    def parse(self, raw: RawOutput) -> Completion:
        raise NotImplementedError

    def failure(self, raw: RawOutput) -> str | None:
        """The failure category this output shows, in shared vocabulary."""
        text = f"{raw.stderr}\n{raw.stdout}".casefold()
        if "json-schema" in text and "invalid" in text:
            return "output_schema_rejected"
        if any(phrase in text for phrase in _ARGUMENT_REJECTIONS):
            return "cli_argument_rejected"
        return None

    def transcript(self, source: TranscriptSource) -> ProviderTranscript:
        """This provider's recorded output, in provider-neutral turns."""
        final: object = None
        structured: object = None
        try:
            payload = json.loads(source.stdout)
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, dict):
            final = payload.get("text") or payload.get("result")
            structured = payload.get("structured")
        if not isinstance(final, str):
            final = source.stdout.strip() or None
        return ProviderTranscript(
            turns=(TranscriptTurn("assistant", final),) if final else (),
            final=final,
            structured=structured,
            prompt=source.prompt,
        )


def mcp_transport(definition: object) -> str:
    """The transport an MCP server definition asks for.

    Definitions spell a local server as ``stdio``, ``local``, or by
    carrying a command with no type at all; everything else names its
    transport directly.
    """
    declared = definition.get("type") if isinstance(definition, dict) else None
    if isinstance(declared, str) and declared:
        return "stdio" if declared in {"local", "stdio"} else declared
    if isinstance(definition, dict) and definition.get("command"):
        return "stdio"
    return "http"


def tool_call(value: object) -> ToolCall | None:
    """One tool request, whatever the provider called its fields."""
    if not isinstance(value, dict):
        return None
    name = value.get("name") or value.get("toolName") or value.get("tool_name")
    if not isinstance(name, str) or not name:
        return None
    arguments = value.get("arguments", value.get("input", value.get("parameters")))
    return ToolCall(name, arguments)


__all__ = ["ProviderBase", "mcp_transport", "tool_call"]
