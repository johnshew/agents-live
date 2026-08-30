"""Deterministic provider used by the conformance suite."""
from __future__ import annotations

import json
import sys

from ..values import Completion, Launch, RawOutput, Request, ResolvedSpec


class FakeProvider:
    name = "fake"
    models = frozenset({"default", "echo"})
    efforts = frozenset({"low", "medium", "high", "xhigh", "max"})

    def prepare(self, spec: ResolvedSpec, request: Request) -> Launch:
        return Launch(
            (
                sys.executable,
                "-m",
                "agents_live.agent.providers.fake_cli",
                "--prompt",
                spec.prompt,
            ),
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


FAKE = FakeProvider()
