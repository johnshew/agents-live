"""Scheduling: handing a trigger to the scheduler this host has.

The dispatch point of the trigger track (docs/windows-support.md). A
``TriggerSpec`` says what should fire; this module gives it to the thing
that will fire it and answers the questions the lifecycle asks: install
it, remove it, is it installed, and what is installed here.

It chooses the store exactly once, in :func:`_store`. The two stores -
``crontasks`` for the user crontab, ``wintasks`` for Task Scheduler -
answer every question below with the same signatures, so nothing here
branches on the platform and nothing above here knows there are two.
What a stored trigger looks like belongs to the store; what one means
belongs to ``triggers``. What is left is the audit trail and the error
type the command layer prints, and that is what this module adds.
"""
from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

from .. import adminlog, paths
from ..runtime.hosts import crontab as crontasks
from ..runtime.hosts import system as hostruntime
from ..runtime.hosts import task_scheduler as wintasks
from . import headless, triggers


def _store():
    """The trigger store this host schedules with."""
    if hostruntime.native_scheduler() == hostruntime.TASK_SCHEDULER:
        return wintasks
    return crontasks


def _call(action):
    """Run a store call, reporting its failures as the CLI's own.

    The Task Scheduler store raises its own error type so it stays a
    leaf; this is the one place those become the typed error the command
    layer already knows how to print. The crontab store raises that
    typed error directly and passes straight through.
    """
    try:
        return action()
    except wintasks.TaskError as exc:
        raise headless.AgentsLiveError(str(exc)) from exc


def _root() -> Path:
    return headless.repo_root()


def install(spec: triggers.TriggerSpec) -> str:
    """Persist *spec* and return the trigger it wrote, for display."""
    written = _call(lambda: _store().install(spec))
    adminlog.record("schedule-install", agent=spec.name, root=str(spec.root),
                    scheduler=hostruntime.native_scheduler(), trigger=written)
    return written


def remove(name: str) -> bool:
    """Remove *name*'s schedule from this host. True if one was there."""
    root = _root()
    removed = _call(lambda: _store().remove(root, name))
    if removed:
        adminlog.record("schedule-remove", agent=name, root=str(root),
                        scheduler=hostruntime.native_scheduler())
    return removed


def remove_all_under(environment: Path) -> int:
    """Withdraw every trigger on this host that runs out of *environment*.

    Uninstall's counterpart to the per-agent removals above. It asks the
    host-wide question those cannot: a trigger belongs to the
    installation being removed, whatever project asked for it, and after
    the removal it would fire at an executable that is no longer there.
    No project root is resolved, because there may be none to resolve
    and the answer would not narrow the sweep anyway.
    """
    removed = _call(lambda: _store().remove_under(environment))
    if removed:
        adminlog.record("schedule-sweep", count=removed,
                        scheduler=hostruntime.native_scheduler())
    return removed


def is_active(name: str) -> bool | None:
    """Whether *name* is scheduled here; None if that cannot be read."""
    return _call(lambda: _store().is_active(_root(), name))


def installed_names() -> list[str]:
    """Every agent this repository has scheduled on this host.

    Runtime-is-truth enumeration: what this returns and what has an
    agent file are compared to find orphans, so an unavailable scheduler
    answers with nothing rather than raising.
    """
    return _call(lambda: _store().installed_names(_root()))


def install_watcher_respawn(name: str) -> str:
    """Persist "this watcher should be running" so a restart restores it.

    A watcher is a process, and a process does not survive a reboot; the
    durable statement of intent is a startup trigger that re-runs the
    guarded respawn. Which store holds it is the same question this
    module answers for schedules, so it is answered in the same place.
    """
    written = _call(lambda: _store().install(headless.watcher_spec(name)))
    adminlog.record("watcher-respawn-install", agent=name, root=str(_root()),
                    scheduler=hostruntime.native_scheduler(), trigger=written)
    return written


def remove_watcher_respawn(name: str) -> bool:
    """Withdraw that intent. True if there was one to withdraw."""
    store = _store()
    root = _root()
    removed = _call(lambda: store.delete(root, name, kind=store.WATCH))
    if removed:
        adminlog.record("watcher-respawn-remove", agent=name,
                        root=str(root),
                        scheduler=hostruntime.native_scheduler())
    return removed


def watcher_respawn_names() -> list[str]:
    """Every watcher this host is meant to be running for this repository."""
    store = _store()
    return _call(lambda: store.installed_names(_root(), kind=store.WATCH))


def repair_legacy_clock(name: str) -> bool:
    """Rewrite a registered trigger that predates ``--scheduled``.

    Its current invocation is ambiguous: it may be a stale trigger
    firing, or a person running the scheduled agent by hand. The caller
    skips that invocation once; the rewritten trigger identifies every
    later fire, while a repeated manual command runs normally (#194).
    Only a native task can fire more often than its cron expression
    says, so the crontab store always answers no.
    """
    spec = headless.schedule_spec(name)
    if not _call(lambda: _store().clock_task_predates_scheduled_flag(
            spec.root, spec.name)):
        return False
    install(spec)
    return True


def current_form(spec: triggers.TriggerSpec) -> tuple[list[str], list[str]]:
    """(what is registered for *spec* here, what *spec* asks for).

    Convergence needs both halves in a shape it can compare and print,
    without knowing which store holds them: crontab lines on POSIX, one
    line per task on Windows. Equal halves mean nothing to migrate.
    """
    return _call(lambda: _store().current_form(spec))


def install_maintenance(spec: triggers.TriggerSpec, *,
                        install: bool = True) -> bool:
    """Persist this tool's own check-and-repair loop. True when changed.

    ``install=False`` converges an existing installation after an upgrade
    re-homes the pinned shim path, but never adds the loop to a host that
    has not asked for it.
    """
    return _call(lambda: _store().install_maintenance(spec, create=install))


def remove_maintenance(spec: triggers.TriggerSpec) -> bool:
    """Withdraw the loop from this host. True when there was one."""
    return _call(lambda: _store().remove_maintenance(spec))


def maintenance_installed(spec: triggers.TriggerSpec) -> bool | None:
    """Whether the loop is registered here; None if that cannot be read."""
    change = maintenance_change(spec)
    return None if change is None else bool(change[0])


def maintenance_change(spec: triggers.TriggerSpec
                       ) -> tuple[list[str], list[str]] | None:
    """(what is registered for the loop, what should be), or None when
    the store cannot be read.

    Both halves are display strings for the same reason the install
    functions return one: a repair plan has to show the developer the
    thing that will change, and what that thing looks like is the
    store's business.
    """
    return _call(lambda: _store().maintenance_change(spec))


def persisted_roots() -> list[Path]:
    """Every existing repository this host has a trigger registered for.

    The host loop maintains what the host actually runs, so it asks the
    store rather than a registry: an entry pinned to a repository is
    that repository asking to be maintained.
    """
    return _call(lambda: _store().persisted_roots())


def claim_due_minute(name: str, schedules: Sequence[str],
                     *, moment: datetime | None = None) -> bool:
    """Whether this wake of *name* is a real firing time, claiming it.

    Cron fires only at firing times, so on POSIX the answer is always
    yes. A native trigger is allowed to be coarser than the expression
    it came from, so on Windows this is the predicate that makes the
    coarseness safe: it asks whether any of the agent's expressions
    names this minute, and it claims the minute so a repetition that
    fires twice inside one matching minute still runs the agent once.

    A predicate, not a scheduler: one question about one moment. Missed
    starts, catch-up after sleep, and daylight-saving folds stay with
    Task Scheduler, which is the only component that knows about them.

    The one question here that is not the store's to answer: it is about
    how coarse a store is allowed to be, so it stays at the dispatch
    point that knows which store answered.
    """
    if _store() is not wintasks:
        return True
    moment = (moment or datetime.now()).replace(second=0, microsecond=0)
    if not any(triggers.schedule_matches(expression, moment)
               for expression in schedules
               if expression.strip() != triggers.BOOT):
        return False
    stamp = moment.strftime("%Y-%m-%dT%H:%M")
    marker = _due_marker(name)
    try:
        if marker.read_text(encoding="utf-8").strip() == stamp:
            return False
    except OSError:
        pass
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(stamp, encoding="utf-8")
    except OSError:
        # A run that cannot record itself still ran; the only thing lost
        # is the guard against a second fire inside the same minute.
        pass
    return True


def _due_marker(name: str) -> Path:
    return paths.repo_state_dir(_root()) / "last-fired" / f"{name}.txt"
