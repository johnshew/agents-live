"""Provider selector grammar."""
from __future__ import annotations

import re

from .values import ProviderSelector

_NAME = r"[A-Za-z0-9][A-Za-z0-9_.-]*"
_SELECTOR = re.compile(
    rf"^(?P<provider>{_NAME})(?:/(?P<model>{_NAME}))?"
    rf"(?::(?P<effort>low|medium|high|xhigh|max))?$")


def parse_selector(value: str) -> ProviderSelector:
    match = _SELECTOR.fullmatch(value)
    if match is None:
        raise ValueError(f"invalid provider selector: {value}")
    provider = match.group("provider")
    model = match.group("model")
    if provider == "none" and (model or match.group("effort")):
        raise ValueError("selector 'none' cannot name a model or effort")
    return ProviderSelector(provider, model, match.group("effort"))
