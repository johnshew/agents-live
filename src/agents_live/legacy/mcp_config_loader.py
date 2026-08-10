"""Dependency-free loading of MCP server definitions from VS Code config."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

MCP_CONFIG_REL = Path(".vscode/mcp.json")


class McpConfigError(ValueError):
    """An existing ``.vscode/mcp.json`` could not be read or parsed.

    Distinct from a missing file (an empty mapping): silently treating a
    malformed config as empty made every defined server degrade to a
    bare ``--mcp <name>`` flag with its command/env definition dropped
    (PKG-004), so parse failures fail closed instead.
    """


def _strip_jsonc(text: str) -> str:
    """*text* minus ``//`` and ``/* */`` comments (string-literal aware)."""
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
    """*text* minus commas that directly precede ``}`` or ``]``."""
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


def load_mcp_servers(root: Path) -> dict[str, Any]:
    """Load MCP server definitions from ``.vscode/mcp.json``.

    The VS Code file is JSONC: ``//`` and ``/* */`` comments (inline or
    full-line) and trailing commas are accepted. A missing file is an
    empty mapping; an existing file that cannot be read or parsed raises
    :class:`McpConfigError` (fail closed - never silently drop the
    user's server definitions).
    """
    path = root / MCP_CONFIG_REL
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
