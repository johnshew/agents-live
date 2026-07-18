"""Pipeline runtime context for agents-live `mode: pipeline`.

Brings up an in-process :class:`PipelineMcp` and materialises per-agent
MCP config files for the duration of one pipeline run.  Returns a dict
of env vars that ``run.py`` merges into the agent's :class:`AgentConfig`
so the pre-processor, agent, and post-processor subprocesses all see
the same URL + bearer token and pick up the right per-agent config:

* ``PIPELINE_MCP_URL``, ``PIPELINE_MCP_TOKEN`` -- used by
  pre/post-processors that connect over HTTP and by the stdio bridge
  spawned for copilot.
* ``PIPELINE_MCP_CLAUDE_CONFIG`` -- path to a JSON file suitable for
  claude's ``--mcp-config <file>``.
* ``PIPELINE_MCP_COPILOT_CONFIG`` -- path to a JSON file suitable for
  copilot's ``--additional-mcp-config @<file>`` (stdio bridge entry,
  because copilot 1.0.52 does not auto-connect HTTP MCP in ``-p`` mode).
"""
from __future__ import annotations

import json
import shutil
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .pipeline_mcp import PipelineMcp


def _bridge_path() -> Path:
    return Path(__file__).resolve().parent / "pipeline_mcp_stdio_bridge.py"


@contextmanager
def pipeline_runtime(
    agent_log: Path | None,
    seed_puts: list[tuple[str, object]] | None = None,
    run_id: str | None = None,
) -> Iterator[dict[str, str]]:
    mcp = PipelineMcp(agent_log=agent_log, run_id=run_id)
    tmp = Path(tempfile.mkdtemp(prefix="pipeline-mcp-"))
    claude_cfg = tmp / "claude-mcp-config.json"
    copilot_cfg = tmp / "copilot-mcp-config.json"
    try:
        mcp.start()
        if seed_puts:
            mcp.seed(seed_puts)
        claude_cfg.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "pipeline": {
                            "type": "http",
                            "url": mcp.url,
                            "headers": {
                                "Authorization": f"Bearer {mcp.token}"
                            },
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        copilot_cfg.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "pipeline": {
                            "type": "local",
                            "command": "uv",
                            "args": [
                                "run",
                                "--script",
                                str(_bridge_path()),
                            ],
                            "env": {
                                "PIPELINE_MCP_URL": mcp.url,
                                "PIPELINE_MCP_TOKEN": mcp.token,
                            },
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        yield {
            "PIPELINE_MCP_URL": mcp.url,
            "PIPELINE_MCP_TOKEN": mcp.token,
            "PIPELINE_MCP_CLAUDE_CONFIG": str(claude_cfg),
            "PIPELINE_MCP_COPILOT_CONFIG": str(copilot_cfg),
        }
    finally:
        mcp.shutdown()
        shutil.rmtree(tmp, ignore_errors=True)


__all__ = ["pipeline_runtime"]
