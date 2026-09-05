"""Provider plugin registry."""
from __future__ import annotations

from typing import Protocol

from ..values import Completion, Launch, RawOutput, Request, ResolvedSpec


class Provider(Protocol):
    name: str
    models: frozenset[str] | None
    efforts: frozenset[str]

    def prepare(self, spec: ResolvedSpec, request: Request) -> Launch: ...
    def parse(self, raw: RawOutput) -> Completion: ...


_providers: dict[str, Provider] = {}

#: Retained so a 5.x plugin's entry point group can still be named in a
#: diagnostic. Discovery no longer reads it: plugins are loaded from
#: source by ``agents_live.plugins`` and handed here through
#: :func:`register`. See docs/decisions/plugin-loading.md.
ENTRY_POINT_GROUP = "agents_live.providers"


def register(provider: Provider) -> None:
    if not provider.name:
        raise ValueError("provider name must not be empty")
    if provider.models is not None and not isinstance(provider.models, frozenset):
        raise ValueError(f"provider '{provider.name}' models must be a frozenset or None")
    if not isinstance(provider.efforts, frozenset):
        raise ValueError(f"provider '{provider.name}' efforts must be a frozenset")
    if not callable(provider.prepare) or not callable(provider.parse):
        raise ValueError(f"provider '{provider.name}' does not implement the provider protocol")
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


from .claude import CLAUDE
from .copilot import COPILOT
from .fake import FAKE

register(CLAUDE)
register(COPILOT)
register(FAKE)

__all__ = ["ENTRY_POINT_GROUP", "Provider", "get", "names", "register"]
