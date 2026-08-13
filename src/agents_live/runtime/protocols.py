"""Lifetime-separated runtime protocols."""
from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import IO, Protocol

from .values import (
    ChildResult,
    Health,
    InstalledTrigger,
    ProcessRef,
    RenderedSubscription,
    Subscription,
)


class TriggerStore(Protocol):
    def install(self, rendered: RenderedSubscription) -> None: ...
    def remove(self, key: str) -> None: ...
    def list(self) -> list[InstalledTrigger]: ...
    def clear(self) -> int: ...


class Supervisor(Protocol):
    def spawn_detached(
        self,
        argv: Sequence[str],
        *,
        role: str,
        key: str = "",
        fingerprint: str = "",
        cwd: str | None = None,
        stdout: IO[bytes] | int | None = None,
        stderr: IO[bytes] | int | None = None,
    ) -> ProcessRef: ...
    def adopt(
        self, pid: int, *, role: str, key: str = "",
        fingerprint: str = "", image: str = "",
    ) -> ProcessRef: ...
    def defer_until_environment_exits(
        self, argv: Sequence[str], environment: Path | str, *,
        operation_id: str | None = None,
        result_path: Path | str | None = None,
        transcript_path: Path | str | None = None,
        transcript_limit: int = 65536,
        wait_timeout_s: int = 900,
    ) -> ProcessRef | None: ...
    def alive(self, ref: ProcessRef) -> bool: ...
    def terminate(self, ref: ProcessRef) -> None: ...
    def owned(self, role: str | None = None) -> list[ProcessRef]: ...


class ChangeSource(Protocol):
    def start(self) -> None: ...
    def poll(self, timeout: float | None) -> list[str]: ...
    def stop(self) -> None: ...


class ChildRunner(Protocol):
    def run_child(
        self,
        argv: Sequence[str],
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        input_text: str | None = None,
        timeout: float | None = None,
        use_pty: bool = False,
    ) -> ChildResult: ...


class HostAdapter(Protocol):
    trigger_store: TriggerStore
    supervisor: Supervisor
    child_runner: ChildRunner

    def prepare(self) -> None: ...
    def render(self, subscription: Subscription) -> RenderedSubscription: ...
    def legacy_agents(self, root: str) -> set[str]: ...
    def remove_legacy(self, root: str, name: str) -> None: ...
    def health(self) -> Health: ...
    def change_source(self, roots: Sequence[str]) -> ChangeSource | None: ...
