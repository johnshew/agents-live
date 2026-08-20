"""Structured ownership markers embedded in host artifacts and watcher argv."""
from __future__ import annotations

import base64
import json
import re
import shlex
from dataclasses import asdict, dataclass
from collections.abc import Sequence
from typing import Any

PREFIX = "agents-live:v2:"
_IDENTIFIER = re.compile(r"^[0-9a-f]{24}$")
_BASE64URL = re.compile(r"^[A-Za-z0-9_-]+$")


@dataclass(frozen=True)
class InvocationMetadata:
    id: str
    scope: str
    target: str
    origin: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not _IDENTIFIER.fullmatch(self.id):
            raise ValueError("metadata id must be 24 lowercase hexadecimal characters")
        if not isinstance(self.scope, str) or not isinstance(self.target, str) \
                or not self.scope or not self.target:
            raise ValueError("metadata scope and target must not be empty")
        if self.origin not in {None, "clock", "boot"}:
            raise ValueError("metadata origin must be clock or boot")


def encode(metadata: InvocationMetadata) -> str:
    fields = {
        key: value for key, value in asdict(metadata).items()
        if value is not None
    }
    payload = json.dumps(fields, sort_keys=True, separators=(",", ":")).encode()
    return PREFIX + base64.urlsafe_b64encode(payload).decode().rstrip("=")


def decode(value: str) -> InvocationMetadata | None:
    if not isinstance(value, str) or not value.startswith(PREFIX):
        return None
    encoded = value[len(PREFIX):]
    if not _BASE64URL.fullmatch(encoded):
        return None
    try:
        raw: Any = json.loads(base64.b64decode(
            encoded + "=" * (-len(encoded) % 4), altchars=b"-_", validate=True))
    except (ValueError, TypeError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict) or set(raw) not in (
            {"id", "scope", "target"},
            {"id", "scope", "target", "origin"},
    ):
        return None
    try:
        metadata = InvocationMetadata(**raw)
    except (TypeError, ValueError):
        return None
    return metadata if encode(metadata) == value else None


def from_argv(argv: Sequence[str]) -> InvocationMetadata | None:
    for index, token in enumerate(argv[:-1]):
        if token == "--metadata":
            return decode(argv[index + 1])
    return None


def from_rendered(rendered: str) -> InvocationMetadata | None:
    try:
        return from_argv(shlex.split(rendered))
    except ValueError:
        return None
