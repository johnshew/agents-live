"""Deterministic provider used by the conformance suite.

It also stands in for a complete third-party integration: it contributes
run-scoped environment, asks for temporary configuration to be
materialized, declares a nested probe command, refuses a mode it cannot
honor, and normalizes its own transcript. Nothing here is claude- or
copilot-shaped, so the surrounding modules cannot be passing by name.
"""
from __future__ import annotations

import json
import sys

from ..values import (
    Completion,
    Launch,
    ProviderCapabilities,
    ProviderCli,
    ProviderRuntime,
    ProviderTranscript,
    RawOutput,
    Request,
    ResolvedSpec,
    RunArtifact,
    TranscriptSource,
    TranscriptTurn,
)
from .base import ProviderBase

HOME = "AGENTS_LIVE_FAKE_HOME"
SETTINGS = "AGENTS_LIVE_FAKE_SETTINGS"
MCP_CONFIG = "AGENTS_LIVE_FAKE_MCP"


class FakeProvider(ProviderBase):
    name = "fake"
    cli = ProviderCli(
        # Nested: the executable alone is an interpreter, and the tokens
        # after it are what actually answers for this provider.
        executable=sys.executable,
        probe_argv=("-m", "agents_live.agent.providers.fake_cli", "--help"),
    )
    capabilities = ProviderCapabilities(
        # No write mode: this CLI never edits anything, and claiming the
        # authority would make an unsupported guarantee look supported.
        modes=frozenset({"plan", "pipeline"}),
        mcp_transports=frozenset({"stdio"}),
        structured_output=True,
        models=frozenset({"default", "echo"}),
        efforts=frozenset({"low", "medium", "high", "xhigh", "max"}),
    )

    def artifacts(self, runtime: ProviderRuntime) -> tuple[RunArtifact, ...]:
        artifacts = [
            RunArtifact("fake-home", kind="directory", mode=0o700, env=(HOME,)),
            RunArtifact(
                "fake-home/settings.json",
                text=json.dumps({"isolated": True}, sort_keys=True),
                env=(SETTINGS,),
            ),
        ]
        servers = {
            server.name: dict(server.definition) for server in runtime.mcps}
        endpoint = runtime.pipeline
        if endpoint is not None:
            servers[endpoint.name] = {
                "type": "stdio",
                "command": list(endpoint.bridge_command),
                "url": endpoint.url,
            }
        if servers:
            artifacts.append(RunArtifact(
                "fake-mcp.json",
                text=json.dumps({"mcpServers": servers}, sort_keys=True),
                env=(MCP_CONFIG,),
            ))
        return tuple(artifacts)

    def prepare(self, spec: ResolvedSpec, request: Request) -> Launch:
        environment = dict(spec.env)
        argv = [
            sys.executable,
            "-m",
            "agents_live.agent.providers.fake_cli",
            "--prompt",
            spec.prompt,
        ]
        settings = environment.get(SETTINGS)
        if settings:
            argv.extend(("--settings", settings))
        config = environment.get(MCP_CONFIG)
        if config:
            argv.extend(("--mcp-config", config))
        return Launch(
            tuple(argv),
            spec.env,
            timeout=None,
            provider=self.name,
            prompt=spec.prompt,
        )

    def parse(self, raw: RawOutput) -> Completion:
        try:
            payload = json.loads(raw.stdout)
        except json.JSONDecodeError:
            return Completion(raw.stdout.strip())
        return Completion(
            payload.get("text", "") if isinstance(payload, dict) else raw.stdout.strip(),
            payload.get("structured") if isinstance(payload, dict) else payload,
        )

    def transcript(self, source: TranscriptSource) -> ProviderTranscript:
        try:
            payload = json.loads(source.stdout)
        except json.JSONDecodeError:
            payload = None
        if not isinstance(payload, dict):
            return super().transcript(source)
        prompt = source.prompt
        final = payload.get("text")
        final = final if isinstance(final, str) and final else None
        turns = []
        if prompt:
            turns.append(TranscriptTurn("user", prompt))
        if final:
            turns.append(TranscriptTurn("assistant", final))
        return ProviderTranscript(
            turns=tuple(turns),
            final=final,
            structured=payload.get("structured"),
            prompt=prompt,
        )


FAKE = FakeProvider()
