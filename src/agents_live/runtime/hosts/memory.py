"""Deterministic host adapter for framework validation."""
from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from pathlib import Path

from ..values import (
    ChildResult,
    Health,
    InstalledTrigger,
    ProcessRef,
    RenderedSubscription,
    Subscription,
)


class MemoryTriggerStore:
    def __init__(self) -> None:
        self.installed: dict[str, InstalledTrigger] = {}

    def install(self, rendered: RenderedSubscription) -> None:
        self.installed[rendered.key] = InstalledTrigger(
            rendered.key, rendered.scope, rendered.kind,
            rendered.fingerprint, rendered.rendered, rendered.target)

    def remove(self, key: str) -> None:
        self.installed.pop(key, None)

    def list(self) -> list[InstalledTrigger]:
        return list(self.installed.values())

    def clear(self) -> int:
        count = len(self.installed)
        self.installed.clear()
        return count


class MemorySupervisor:
    def __init__(self) -> None:
        self.processes: dict[str, ProcessRef] = {}

    def spawn_detached(
        self, argv: Sequence[str], *, role: str, key: str = "",
        fingerprint: str = "", **_kwargs,
    ) -> ProcessRef:
        process = ProcessRef(
            1000 + len(self.processes), 1, Path(argv[0]).name,
            role, key, fingerprint)
        self.processes[key] = process
        return process

    def alive(self, ref: ProcessRef) -> bool:
        return self.processes.get(ref.key) == ref

    def terminate(self, ref: ProcessRef) -> None:
        self.processes.pop(ref.key, None)

    def owned(self, role: str | None = None) -> list[ProcessRef]:
        return [
            process for process in self.processes.values()
            if role is None or process.role == role
        ]


class MemoryChildRunner:
    def __init__(self) -> None:
        self.argv: list[tuple[str, ...]] = []

    def run_child(self, argv: Sequence[str], **_kwargs) -> ChildResult:
        invocation = tuple(argv)
        self.argv.append(invocation)
        return ChildResult(
            invocation, 0,
            json.dumps({"text": "framework smoketest passed"}), "")


class MemoryHost:
    def __init__(self) -> None:
        self.trigger_store = MemoryTriggerStore()
        self.supervisor = MemorySupervisor()
        self.child_runner = MemoryChildRunner()
        self.legacy: dict[str, set[str]] = {}

    def prepare(self) -> None:
        pass

    def render(self, subscription: Subscription) -> RenderedSubscription:
        fingerprint = hashlib.sha256(
            f"{subscription.scope}\0{subscription.target}\0"
            f"{subscription.kind}\0{subscription.trigger}".encode()
        ).hexdigest()
        watcher_argv = (
            "agents-live", "internal", "watch-loop", subscription.target,
        ) if subscription.kind == "watch" else ()
        return RenderedSubscription(
            subscription.key, subscription.scope, subscription.kind,
            fingerprint,
            json.dumps({"target": subscription.target}, sort_keys=True),
            watcher_argv,
            subscription.target,
        )

    def health(self) -> Health:
        return Health(True)

    def legacy_agents(self, root: str) -> set[str]:
        return set(self.legacy.get(str(Path(root).resolve()), set()))

    def remove_legacy(self, root: str, name: str) -> None:
        self.legacy.get(str(Path(root).resolve()), set()).discard(name)

    def change_source(self, roots: Sequence[str]):
        del roots
        return None