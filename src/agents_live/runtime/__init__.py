"""Host automation port.

The runtime owns durable triggers, detached processes, change sources, child
execution, liveness, dependency probes, and convergence. It receives only
immutable primitive records across the seam.
"""
from .convergence import configure, converge, current, dependency_health, health
from .diff import diff
from .grammars import (
    Schedule,
    ScheduleSyntaxError,
    Watch,
    WatchSyntaxError,
    parse_schedule,
    parse_watch,
)
from .protocols import ChangeSource, ChildRunner, HostAdapter, Supervisor, TriggerStore
from .values import (
    ChildResult,
    Converged,
    DependencyHealth,
    Health,
    InstalledTrigger,
    Operation,
    ProcessRef,
    RenderedSubscription,
    RuntimeTarget,
    Subscription,
)

__all__ = [
    "ChangeSource",
    "ChildResult",
    "ChildRunner",
    "Converged",
    "DependencyHealth",
    "Health",
    "HostAdapter",
    "InstalledTrigger",
    "Operation",
    "ProcessRef",
    "RenderedSubscription",
    "RuntimeTarget",
    "Schedule",
    "ScheduleSyntaxError",
    "Subscription",
    "Supervisor",
    "TriggerStore",
    "Watch",
    "WatchSyntaxError",
    "configure",
    "converge",
    "current",
    "dependency_health",
    "diff",
    "health",
    "parse_schedule",
    "parse_watch",
]
