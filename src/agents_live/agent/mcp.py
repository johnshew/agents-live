"""Project MCP definition resolution for agent providers."""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Iterable

from .definition import DefinitionError
from .values import McpServer

MCP_CONFIG_RELS = (Path(".vscode/mcp.json"), Path(".mcp.json"))


class McpConfigError(ValueError):
    """An existing project MCP config could not be read or parsed."""


def load_mcp_servers(root: Path) -> dict[str, Any]:
    """Load MCP server definitions from the project MCP config files."""
    servers: dict[str, Any] = {}
    for rel in MCP_CONFIG_RELS:
        for name, definition in _load_one(root / rel).items():
            servers.setdefault(name, definition)
    return servers


def resolve_mcp_servers(root: Path, names: tuple[str, ...]) -> tuple[McpServer, ...]:
    if not names:
        return ()
    try:
        servers = load_mcp_servers(root)
    except McpConfigError as exc:
        raise DefinitionError(str(exc)) from exc
    resolved: list[McpServer] = []
    for name in names:
        server = servers.get(name)
        if server is None:
            raise DefinitionError(
                f"MCP server '{name}' is not defined in "
                ".vscode/mcp.json or .mcp.json")
        if not isinstance(server, dict):
            raise DefinitionError(f"MCP server '{name}' definition must be an object")
        resolved.append(McpServer(name, server))
    return tuple(resolved)


def write_mcp_config(mcps: Iterable[McpServer]) -> str | None:
    definitions = {mcp.name: dict(mcp.definition) for mcp in mcps}
    if not definitions:
        return None
    descriptor, path = tempfile.mkstemp(prefix="agents-live-mcp-", suffix=".json")
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump({"mcpServers": definitions}, stream, sort_keys=True)
    return path


def _load_one(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise McpConfigError(f"cannot read {path}: {exc}") from exc
    try:
        data = json.loads(_strip_trailing_commas(_strip_jsonc(text)))
    except (json.JSONDecodeError, McpConfigError) as exc:
        raise McpConfigError(
            f"{path} is not valid JSONC: {exc}; fix the file (agents "
            "would otherwise run without their MCP definitions)") from exc
    if not isinstance(data, dict):
        raise McpConfigError(
            f"{path}: top-level value must be a JSON object, "
            f"not {type(data).__name__}")
    servers = data.get("mcpServers") or data.get("servers") or {}
    if not isinstance(servers, dict):
        raise McpConfigError(
            f"{path}: mcpServers/servers must be a JSON object, "
            f"not {type(servers).__name__}")
    return servers


def _strip_jsonc(text: str) -> str:
    out: list[str] = []
    i, n = 0, len(text)
    in_string = False
    while i < n:
        ch = text[i]
        if in_string:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if ch == '"':
                in_string = False
            i += 1
            continue
        if ch == '"':
            in_string = True
            out.append(ch)
            i += 1
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "/":
            while i < n and text[i] != "\n":
                i += 1
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "*":
            i += 2
            while i + 1 < n and not (text[i] == "*" and text[i + 1] == "/"):
                i += 1
            if i + 1 >= n:
                raise McpConfigError("unterminated /* */ comment")
            i += 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _strip_trailing_commas(text: str) -> str:
    out: list[str] = []
    i, n = 0, len(text)
    in_string = False
    while i < n:
        ch = text[i]
        if in_string:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if ch == '"':
                in_string = False
            i += 1
            continue
        if ch == '"':
            in_string = True
            out.append(ch)
            i += 1
            continue
        if ch == ",":
            j = i + 1
            while j < n and text[j] in " \t\r\n":
                j += 1
            if j < n and text[j] in "}]":
                i += 1
                continue
        out.append(ch)
        i += 1
    return "".join(out)
