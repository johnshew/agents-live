"""Host automation port.

The runtime owns durable triggers, detached processes, change sources, child
execution, liveness, and convergence. It deliberately knows targets only as
strings.
"""
from .convergence import configure, converge, current, health
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
    Health,
    InstalledTrigger,
    Operation,
    ProcessRef,
    RenderedSubscription,
    Subscription,
)

__all__ = [
    "ChangeSource",
    "ChildResult",
    "ChildRunner",
    "Converged",
    "Health",
    "HostAdapter",
    "InstalledTrigger",
    "Operation",
    "ProcessRef",
    "RenderedSubscription",
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
    "diff",
    "health",
    "parse_schedule",
    "parse_watch",
]
