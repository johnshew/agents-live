"""Read retired v1 ownership markers during artifact replacement."""
from __future__ import annotations

import base64
import json
from typing import Any

PREFIX = "agents-live:v1:"


def decode(value: str) -> dict[str, str] | None:
    token = value.strip()
    if token.startswith("#"):
        token = token[1:].strip()
    if not token.startswith(PREFIX):
        return None
    encoded = token[len(PREFIX):]
    try:
        raw: Any = json.loads(base64.urlsafe_b64decode(
            encoded + "=" * (-len(encoded) % 4)))
    except (ValueError, TypeError, json.JSONDecodeError):
        return None
    required = {"key", "scope", "kind", "fingerprint"}
    if not isinstance(raw, dict) or not all(
            isinstance(key, str) and isinstance(item, str)
            for key, item in raw.items()):
        return None
    return raw if required <= raw.keys() else None


def from_rendered(rendered: str) -> dict[str, str] | None:
    marker = rendered.rsplit("#", 1)[-1] if "#" in rendered else rendered
    return decode(marker)
