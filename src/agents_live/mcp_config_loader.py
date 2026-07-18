"""Dependency-free loading of MCP server definitions from VS Code config."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

MCP_CONFIG_REL = Path(".vscode/mcp.json")


def load_mcp_servers(root: Path) -> dict[str, Any]:
    """Load MCP server definitions from ``.vscode/mcp.json``.

    The VS Code file is JSONC. Full-line ``//`` comments are ignored to match
    the generated configuration used by the agents-live framework.
    Missing or invalid files produce an empty mapping.
    """
    path = root / MCP_CONFIG_REL
    if not path.is_file():
        return {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    lines = [line for line in text.splitlines() if not line.lstrip().startswith("//")]
    try:
        data = json.loads("\n".join(lines))
    except json.JSONDecodeError:
        return {}
    return data.get("mcpServers") or data.get("servers") or {}
