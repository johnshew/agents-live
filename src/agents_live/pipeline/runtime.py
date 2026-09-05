"""Pipeline runtime context for agents-live `mode: pipeline`.

Brings up an in-process :class:`PipelineMcp` for the duration of one
pipeline run and describes it as a value. The session carries the
environment every step sees:

* ``PIPELINE_MCP_URL``, ``PIPELINE_MCP_TOKEN`` -- used by pre/post
  processors that connect over HTTP and by the stdio bridge a provider
  may configure.

How an agent CLI is told about the server is the provider's business:
the session exposes a :class:`PipelineEndpoint` describing the URL,
token, and stdio bridge command, and each provider renders its own
client configuration from it. Nothing here knows a provider's name.
"""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Generator

from ..agent import PipelineEndpoint
from .server import PipelineMcp

SERVER_NAME = "pipeline"


class PipelineSession(dict[str, str]):
    def __init__(
        self,
        environment: dict[str, str],
        mcp: PipelineMcp,
        endpoint: PipelineEndpoint,
    ) -> None:
        super().__init__(environment)
        self._mcp = mcp
        self.endpoint = endpoint

    def snapshot(self, path: str) -> tuple[bool, object]:
        return self._mcp.snapshot(path)


def _bridge_path() -> Path:
    return Path(__file__).resolve().parent / "stdio_bridge.py"


@contextmanager
def pipeline_runtime(
    agent_log: Path | None,
    seed_puts: list[tuple[str, object]] | None = None,
    run_id: str | None = None,
) -> Generator[PipelineSession, None, None]:
    mcp = PipelineMcp(agent_log=agent_log, run_id=run_id)
    try:
        mcp.start()
        if seed_puts:
            mcp.seed(seed_puts)
        endpoint = PipelineEndpoint(
            SERVER_NAME,
            mcp.url,
            mcp.token,
            ("uv", "run", "--script", str(_bridge_path())),
        )
        yield PipelineSession({
            "PIPELINE_MCP_URL": mcp.url,
            "PIPELINE_MCP_TOKEN": mcp.token,
        }, mcp, endpoint)
    finally:
        mcp.shutdown()


__all__ = ["PipelineSession", "SERVER_NAME", "pipeline_runtime"]
