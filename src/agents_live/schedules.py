"""Scheduling: handing a trigger to the scheduler this host has.

The dispatch point of the trigger track (docs/windows-support.md).
A ``TriggerSpec`` says what should fire; this module gives it to the
thing that will fire it - the user crontab on POSIX, Task Scheduler on
Windows - and answers the four questions the lifecycle asks: install it,
remove it, is it installed, and what is installed here for this
repository.

It is the only module that chooses between the two, which is what keeps
the question out of activate, stop, status, and the health check. The
crontab mechanics stay where they have always been, in ``headless``;
what moved here is the sequence that used to sit in ``activate``, since
reading and writing the table is the host's business and not
activation's.
"""
from __future__ import annotations

from pathlib import Path

from . import headless, hostruntime, triggers, wintasks


def install(spec: triggers.TriggerSpec) -> str:
    """Persist *spec* and return the trigger it wrote, for display."""
    if hostruntime.native_scheduler() == hostruntime.TASK_SCHEDULER:
        return _windows(lambda: wintasks.install(spec))
    lines = triggers.render(spec)
    # Exact --name token matching: a plain substring test would also drop
    # entries for sibling agents whose name contains this one (todo vs
    # todo-push), or arbitrary entries when the name appears in the repo
    # or script path.
    with headless.crontab_lock():
        installed = headless.current_crontab_lines()
        if installed is None:
            # Never treat an unreadable crontab as empty: install_crontab
            # replaces the whole table, which would wipe every entry the
            # read failed to see.
            raise headless.AgentsLiveError("crontab is not accessible")
        kept = [line for line in installed
                if not headless.cron_line_matches(line, spec.name)]
        headless.install_crontab([*kept, *lines])
    return "; ".join(lines)


def remove(name: str) -> bool:
    """Remove *name*'s schedule from this host. True if one was there."""
    if hostruntime.native_scheduler() == hostruntime.TASK_SCHEDULER:
        return _windows(lambda: wintasks.remove(_root(), name))
    return headless.remove_cron_entries(name)


def is_active(name: str) -> bool | None:
    """Whether *name* is scheduled here; None if that cannot be read."""
    if hostruntime.native_scheduler() == hostruntime.TASK_SCHEDULER:
        return _windows(lambda: wintasks.is_active(_root(), name))
    return headless.cron_is_active(name)


def installed_names() -> list[str]:
    """Every agent this repository has scheduled on this host.

    Runtime-is-truth enumeration: what this returns and what has an
    agent file are compared to find orphans, so an unavailable scheduler
    answers with nothing rather than raising.
    """
    if hostruntime.native_scheduler() == hostruntime.TASK_SCHEDULER:
        return _windows(lambda: wintasks.installed_names(_root()))
    return headless._list_active_cron_agent_names()


def _root() -> Path:
    return headless.repo_root()


def _windows(action):
    """Run a task-store call, reporting its failures as the CLI's own.

    The Windows implementation raises its own errors so it stays a leaf;
    this is the one place they become the typed error the command layer
    already knows how to print.
    """
    try:
        return action()
    except wintasks.TaskError as exc:
        raise headless.AgentsLiveError(str(exc)) from exc
