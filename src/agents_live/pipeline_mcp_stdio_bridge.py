#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["mcp"]
# ///
"""Stdio MCP bridge to the in-process pipeline-mcp HTTP server.

Copilot CLI does not auto-connect HTTP-type MCP servers in headless
`-p` mode (Phase 0b finding #2). To inject pipeline-mcp into copilot
agents we ship this small stdio MCP server that proxies every tool
call to the loopback HTTP server.

Wire-up:
  * `run.py` exports `PIPELINE_MCP_URL` and `PIPELINE_MCP_TOKEN`.
  * Copilot's `mcp-config.json` lists this script as a `local`
    (stdio) server named `pipeline`.
  * Copilot launches the bridge, calls `list_tools` / `call_tool`
    over stdio; the bridge forwards both to the upstream HTTP server
    with the bearer token attached.

The bridge is intentionally dumb: no caching, no schema, no policy.
The upstream `PipelineMcp` remains the single source of truth.
"""
from __future__ import annotations

import asyncio
import os
import sys

# Strip proxy env vars before importing httpx-backed clients. The
# pipeline-mcp server is always on 127.0.0.1, so proxy routing is
# nonsense; some sandboxes inject `socks*://` URLs that require an
# extra package (`socksio`) just to construct the transport.
for _var in (
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
    "http_proxy", "https_proxy", "all_proxy",
):
    os.environ.pop(_var, None)
os.environ["NO_PROXY"] = "127.0.0.1,localhost"
os.environ["no_proxy"] = "127.0.0.1,localhost"

from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server


async def _run() -> None:
    url = os.environ.get("PIPELINE_MCP_URL")
    if not url:
        print("pipeline_mcp_stdio_bridge: PIPELINE_MCP_URL not set", file=sys.stderr)
        sys.exit(2)
    token = os.environ.get("PIPELINE_MCP_TOKEN", "")
    headers = {"Authorization": f"Bearer {token}"} if token else {}

    async with streamablehttp_client(url, headers=headers) as (read, write, _):
        async with ClientSession(read, write) as upstream:
            await upstream.initialize()
            tools_resp = await upstream.list_tools()
            upstream_tools = list(tools_resp.tools)

            srv: Server = Server("pipeline-stdio-bridge")

            @srv.list_tools()
            async def _list_tools():  # type: ignore[no-untyped-def]
                return upstream_tools

            @srv.call_tool()
            async def _call_tool(name, arguments):  # type: ignore[no-untyped-def]
                result = await upstream.call_tool(name, arguments or {})
                content = list(result.content)
                structured = getattr(result, "structuredContent", None)
                if structured is not None:
                    return content, structured
                return content

            async with stdio_server() as (r, w):
                await srv.run(r, w, srv.create_initialization_options())


def main() -> None:
    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
