"""Provider plugin registry."""
from __future__ import annotations

from typing import Protocol

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
)


class Provider(Protocol):
    """Everything one provider integration has to describe about itself.

    The contract is complete: nothing outside a provider module branches
    on a provider's name. It is also pure, so a provider never receives a
    process, filesystem manager, or server object; it describes what a
    run needs and dispatch owns the doing.
    """

    name: str
    cli: ProviderCli
    capabilities: ProviderCapabilities

    def validate(self, spec: ResolvedSpec) -> str | None: ...
    def artifacts(self, runtime: ProviderRuntime) -> tuple[RunArtifact, ...]: ...
    def prepare(self, spec: ResolvedSpec, request: Request) -> Launch: ...
    def parse(self, raw: RawOutput) -> Completion: ...
    def failure(self, raw: RawOutput) -> str | None: ...
    def transcript(self, source: TranscriptSource) -> ProviderTranscript: ...


_providers: dict[str, Provider] = {}

#: Retained so a 5.x plugin's entry point group can still be named in a
#: diagnostic. Discovery no longer reads it: plugins are loaded from
#: source by ``agents_live.plugins`` and handed here through
#: :func:`register`. See docs/decisions/plugin-loading.md.
ENTRY_POINT_GROUP = "agents_live.providers"

CONTRACT_METHODS = (
    "validate", "artifacts", "prepare", "parse", "failure", "transcript")


def register(provider: Provider) -> None:
    if not getattr(provider, "name", ""):
        raise ValueError("provider name must not be empty")
    capabilities = getattr(provider, "capabilities", None)
    if not isinstance(capabilities, ProviderCapabilities):
        raise ValueError(
            f"provider '{provider.name}' capabilities must be a "
            "ProviderCapabilities record")
    if not isinstance(capabilities.modes, frozenset) or not capabilities.modes:
        raise ValueError(
            f"provider '{provider.name}' must declare at least one supported mode")
    if capabilities.models is not None and not isinstance(
            capabilities.models, frozenset):
        raise ValueError(
            f"provider '{provider.name}' models must be a frozenset or None")
    if not isinstance(capabilities.efforts, frozenset):
        raise ValueError(f"provider '{provider.name}' efforts must be a frozenset")
    if not isinstance(capabilities.mcp_transports, frozenset):
        raise ValueError(
            f"provider '{provider.name}' mcp_transports must be a frozenset")
    if not isinstance(getattr(provider, "cli", None), ProviderCli):
        raise ValueError(
            f"provider '{provider.name}' cli must be a ProviderCli record")
    for method in CONTRACT_METHODS:
        if not callable(getattr(provider, method, None)):
            raise ValueError(
                f"provider '{provider.name}' does not implement the provider "
                f"contract: {method} is missing or not callable")
    previous = _providers.get(provider.name)
    if previous is not None and previous is not provider:
        raise ValueError(f"provider '{provider.name}' is already registered")
    _providers[provider.name] = provider


def get(name: str) -> Provider:
    selected = "claude" if name == "default" else name
    try:
        return _providers[selected]
    except KeyError:
        raise ValueError(
            f"unknown provider '{name}'; installed: {', '.join(sorted(_providers))}") from None


def names() -> tuple[str, ...]:
    return tuple(sorted(_providers))


from .base import ProviderBase
from .claude import CLAUDE
from .copilot import COPILOT
from .fake import FAKE

register(CLAUDE)
register(COPILOT)
register(FAKE)

__all__ = [
    "CONTRACT_METHODS",
    "ENTRY_POINT_GROUP",
    "Provider",
    "ProviderBase",
    "get",
    "names",
    "register",
]
