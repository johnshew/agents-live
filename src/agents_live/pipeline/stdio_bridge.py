#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.12"
# # mcp is held below 2.0: that release removed the client and server APIs
# # used by this bridge (#205). Lift this with the package bound, not before.
# dependencies = ["mcp>=1.30.0,<2"]
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
from datetime import timedelta

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

import httpx
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
from mcp.types import CallToolResult, TextContent

_REQUEST_TIMEOUT_SECONDS = 180.0


async def _run() -> None:
    url = os.environ.get("PIPELINE_MCP_URL")
    if not url:
        print("pipeline_mcp_stdio_bridge: PIPELINE_MCP_URL not set", file=sys.stderr)
        sys.exit(2)
    token = os.environ.get("PIPELINE_MCP_TOKEN", "")
    headers = {"Authorization": f"Bearer {token}"} if token else {}

    async with (
        httpx.AsyncClient(headers=headers, timeout=httpx.Timeout(30, read=300)) as http,
        streamable_http_client(url, http_client=http) as (read, write, _),
        ClientSession(read, write) as upstream,
    ):
        await upstream.initialize()
        tools_resp = await upstream.list_tools()
        upstream_tools = list(tools_resp.tools)

    srv: Server = Server("pipeline-stdio-bridge")

    @srv.list_tools()
    async def _list_tools():
        return upstream_tools

    @srv.call_tool()
    async def _call_tool(name, arguments):
        try:
            async with (
                httpx.AsyncClient(headers=headers, timeout=httpx.Timeout(30, read=300)) as http,
                streamable_http_client(url, http_client=http) as (read, write, _),
                ClientSession(read, write) as upstream,
            ):
                await upstream.initialize()
                return await upstream.call_tool(name, arguments or {}, read_timeout_seconds=timedelta(seconds=_REQUEST_TIMEOUT_SECONDS))
        except Exception as exc:  # noqa: BLE001 - Transport failures must become MCP errors.
            return CallToolResult(isError=True, content=[TextContent(
                type="text", text=f"pipeline request failed; retry is permitted: {exc}")])

    async with stdio_server() as (read, write):
        await srv.run(read, write, srv.create_initialization_options())


def main() -> None:
    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
