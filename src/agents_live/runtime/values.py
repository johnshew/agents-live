"""Immutable primitive records crossing the runtime seam."""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256


@dataclass(frozen=True)
class Subscription:
    key: str
    scope: str
    target: str
    kind: str
    trigger: str

    @classmethod
    def create(cls, *, scope: str, target: str, kind: str, trigger: str) -> "Subscription":
        material = "\0".join((scope, target, kind, trigger)).encode()
        key = sha256(material).hexdigest()[:24]
        return cls(key=key, scope=scope, target=target, kind=kind, trigger=trigger)

    def __post_init__(self) -> None:
        if self.kind not in {"schedule", "watch"}:
            raise ValueError(f"unknown subscription kind: {self.kind}")
        if not self.key or not self.scope or not self.target or not self.trigger:
            raise ValueError("subscription fields must not be empty")


@dataclass(frozen=True)
class RenderedSubscription:
    key: str
    scope: str
    kind: str
    fingerprint: str
    rendered: str
    watcher_argv: tuple[str, ...] = ()
    target: str = ""


@dataclass(frozen=True)
class InstalledTrigger:
    key: str
    scope: str
    kind: str
    fingerprint: str
    rendered: str
    # Empty for an artifact written before targets were recorded; such an
    # artifact simply cannot be protected by target.
    target: str = ""


@dataclass(frozen=True)
class ProcessRef:
    pid: int
    created_at: float
    image: str
    role: str
    key: str = ""
    fingerprint: str = ""

    def __post_init__(self) -> None:
        if self.pid <= 0:
            raise ValueError("process pid must be positive")
        if self.role not in {"watcher", "provider-child", "maintenance"}:
            raise ValueError(f"unknown process role: {self.role}")


@dataclass(frozen=True)
class ChildResult:
    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False


@dataclass(frozen=True)
class Operation:
    kind: str
    key: str
    detail: str
    rendered: RenderedSubscription | None = None
    process: ProcessRef | None = None


@dataclass(frozen=True)
class Health:
    healthy: bool
    liveness: str = "not-required"
    budget_tripped: bool = False
    detail: tuple[str, ...] = ()


@dataclass(frozen=True)
class Converged:
    dry_run: bool
    done: tuple[Operation, ...]
    failed: tuple[tuple[Operation, str], ...]
    health: Health
