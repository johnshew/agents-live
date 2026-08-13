"""Normalize platform-owned probes of paired runtime health."""
from __future__ import annotations

import json

from ..values import ChildResult, DependencyHealth, RuntimeTarget


def unknown(target: RuntimeTarget, detail: str) -> DependencyHealth:
    return DependencyHealth(target.runtime, "unknown", detail)


def from_child(target: RuntimeTarget, result: ChildResult) -> DependencyHealth:
    if result.timed_out:
        return unknown(target, "paired runtime health probe timed out")
    try:
        payload = json.loads(result.stdout)
    except (json.JSONDecodeError, TypeError):
        return unknown(target, "paired runtime health probe returned invalid output")
    if not isinstance(payload, dict) or not isinstance(payload.get("checks"), list):
        return unknown(target, "paired runtime health probe returned invalid output")
    host = next((
        item for item in payload["checks"]
        if isinstance(item, dict) and item.get("check") == "host runtime"
        and isinstance(item.get("ok"), bool)
    ), None)
    if host is None:
        return unknown(target, "paired runtime health probe omitted host status")
    if host["ok"]:
        return DependencyHealth(target.runtime, "healthy", "runtime is healthy")
    return DependencyHealth(
        target.runtime, "unhealthy", "paired runtime reported unhealthy")