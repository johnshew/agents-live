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

from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

from . import adminlog, headless, hostruntime, paths, triggers, wintasks


def install(spec: triggers.TriggerSpec) -> str:
    """Persist *spec* and return the trigger it wrote, for display."""
    written = _install(spec)
    adminlog.record("schedule-install", agent=spec.name, root=str(spec.root),
                    scheduler=hostruntime.native_scheduler(), trigger=written)
    return written


def _install(spec: triggers.TriggerSpec) -> str:
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
        removed = _windows(lambda: wintasks.remove(_root(), name))
    else:
        removed = headless.remove_cron_entries(name)
    if removed:
        adminlog.record("schedule-remove", agent=name, root=_logged_root(),
                        scheduler=hostruntime.native_scheduler())
    return removed


def _logged_root() -> str | None:
    """The project root as an audit field, or None when there is none.

    A removal on the crontab branch never needs the root to do its work,
    so resolving one for the record must not be able to fail the removal;
    a host with no selected project simply records no root.
    """
    try:
        return str(_root())
    except Exception:
        return None


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


def install_watcher_respawn(name: str) -> str:
    """Persist "this watcher should be running" so a restart restores it.

    A watcher is a process, and a process does not survive a reboot; the
    durable statement of intent is a startup trigger that re-runs the
    guarded respawn. Which store holds it is the same question this
    module answers for schedules, so it is answered in the same place.
    """
    if hostruntime.native_scheduler() == hostruntime.TASK_SCHEDULER:
        written = _windows(lambda: wintasks.install(headless.watcher_spec(name)))
    else:
        written = headless.install_watcher_reboot_line(name)
    adminlog.record("watcher-respawn-install", agent=name, root=_logged_root(),
                    scheduler=hostruntime.native_scheduler(), trigger=written)
    return written


def remove_watcher_respawn(name: str) -> bool:
    """Withdraw that intent. True if there was one to withdraw."""
    if hostruntime.native_scheduler() == hostruntime.TASK_SCHEDULER:
        removed = _windows(
            lambda: wintasks.delete(_root(), name, kind=wintasks.WATCH))
    else:
        removed = headless.remove_watcher_reboot_line(name)
    if removed:
        adminlog.record("watcher-respawn-remove", agent=name,
                        root=_logged_root(),
                        scheduler=hostruntime.native_scheduler())
    return removed


def watcher_respawn_names() -> list[str]:
    """Every watcher this host is meant to be running for this repository."""
    if hostruntime.native_scheduler() == hostruntime.TASK_SCHEDULER:
        return _windows(
            lambda: wintasks.installed_names(_root(), kind=wintasks.WATCH))
    return headless.list_reboot_watcher_agent_names()


def current_form(spec: triggers.TriggerSpec) -> tuple[list[str], list[str]]:
    """(what is registered for *spec* here, what *spec* asks for).

    Convergence needs both halves in a shape it can compare and print,
    without knowing which store holds them: crontab lines on POSIX, one
    line per task on Windows. Equal halves mean nothing to migrate.
    """
    if hostruntime.native_scheduler() == hostruntime.TASK_SCHEDULER:
        def read() -> tuple[list[str], list[str]]:
            old: list[str] = []
            new: list[str] = []
            for kind in wintasks.kinds(spec):
                registered = wintasks.registered_form(
                    spec.root, spec.name, kind=kind)
                desired = wintasks.desired_form(spec, kind=kind)
                if registered is not None:
                    old.append(_task_line(spec, kind, registered))
                if desired is not None:
                    new.append(_task_line(spec, kind, desired))
            return old, new
        return _windows(read)
    lines = headless.current_crontab_lines() or []
    if spec.kind == triggers.WATCHER:
        old = [line for line in lines
               if headless._reboot_watcher_line_agent_name(line) == spec.name]
    else:
        old = [line for line in lines
               if headless.cron_line_matches(line, spec.name)]
    return old, triggers.render(spec)


def _task_line(spec: triggers.TriggerSpec, kind: str,
               form: tuple[str, str, list[tuple]]) -> str:
    command, arguments, signature = form
    fires = " ".join(":".join(str(part) for part in entry)
                     for entry in signature)
    path = wintasks.task_path(spec.root, spec.name, kind=kind)
    return f"{path} [{fires}]: {command} {arguments}".rstrip()


def install_maintenance(spec: triggers.TriggerSpec, *,
                        install: bool = True) -> bool:
    """Persist this tool's own check-and-repair loop. True when changed.

    ``install=False`` converges an existing installation after an upgrade
    re-homes the pinned shim path, but never adds the loop to a host that
    has not asked for it.
    """
    if hostruntime.native_scheduler() == hostruntime.TASK_SCHEDULER:
        def register() -> bool:
            current, desired = current_form(spec)
            if current == desired or (not current and not install):
                return False
            wintasks.install(spec)
            return True
        return _windows(register)
    desired = triggers.render(spec)
    with headless.crontab_lock():
        lines = headless.current_crontab_lines()
        if lines is None:
            raise headless.AgentsLiveError("crontab is not accessible")
        kept = [line for line in lines if not triggers.is_maintenance_line(line)]
        current = [line for line in lines if triggers.is_maintenance_line(line)]
        if current == desired or (not current and not install):
            return False
        headless.install_crontab(kept + desired)
        return True


def remove_maintenance(spec: triggers.TriggerSpec) -> bool:
    """Withdraw the loop from this host. True when there was one."""
    if hostruntime.native_scheduler() == hostruntime.TASK_SCHEDULER:
        return _windows(lambda: wintasks.delete(
            spec.root, spec.name, kind=wintasks.HOST))
    with headless.crontab_lock():
        lines = headless.current_crontab_lines()
        if lines is None:
            raise headless.AgentsLiveError("crontab is not accessible")
        kept = [line for line in lines if not triggers.is_maintenance_line(line)]
        if len(kept) == len(lines):
            return False
        headless.install_crontab(kept)
        return True


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
    if hostruntime.native_scheduler() == hostruntime.TASK_SCHEDULER:
        return current_form(spec)
    lines = headless.current_crontab_lines()
    if lines is None:
        return None
    return ([line for line in lines if triggers.is_maintenance_line(line)],
            triggers.render(spec))


def persisted_roots() -> list[Path]:
    """Every existing repository this host has a trigger registered for.

    The host loop maintains what the host actually runs, so it asks the
    store rather than a registry: an entry pinned to a repository is
    that repository asking to be maintained.
    """
    if hostruntime.native_scheduler() == hostruntime.TASK_SCHEDULER:
        def read() -> list[Path]:
            roots: list[Path] = []
            for task in wintasks.registered_tasks():
                # The loop's own task is pinned to the tool's state
                # directory, which is not a project and has nothing to
                # sweep. On a crontab host it names no repository at
                # all; here the kind is what says the same thing.
                if wintasks.kind_of_task_name(task["name"]) == wintasks.HOST:
                    continue
                directory = task["working_dir"]
                if not directory:
                    continue
                root = Path(directory).expanduser()
                if root.is_dir() and root not in roots:
                    roots.append(root)
            return roots
        return _windows(read)
    roots: list[Path] = []
    for line in headless.current_crontab_lines() or []:
        tokens = triggers.tokens(line)
        if not any(Path(token).name == "agents-live" for token in tokens):
            continue
        for first, second in zip(tokens, tokens[1:]):
            if first != "--repo":
                continue
            root = Path(second).expanduser().resolve()
            if root.is_dir() and root not in roots:
                roots.append(root)
            break
    return roots


def _root() -> Path:
    return headless.repo_root()


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
    """
    if hostruntime.native_scheduler() != hostruntime.TASK_SCHEDULER:
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
