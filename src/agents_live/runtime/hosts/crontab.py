"""The user crontab as the POSIX trigger store.

What a stored trigger looks like, a crontab line here and a registered task
on Windows, stays inside the host adapter. The compatibility trigger grammar
still renders and recognizes pre-6.0 lines; this module owns the atomic
read-modify-write around them.
"""
from __future__ import annotations

import subprocess
from collections.abc import Iterator, Sequence
from contextlib import ExitStack, contextmanager
from pathlib import Path

from ... import paths
from ...legacy import triggers
from . import system as hostruntime

# The kinds a caller can name, spelled as ``wintasks`` spells them so
# the two stores stay substitutable. A crontab has no separate clock and
# startup registration - ``@reboot`` is just another schedule - so
# ``CLOCK`` covers both and there is no ``BOOT``.
CLOCK = triggers.SCHEDULE
WATCH = triggers.WATCHER
HOST = triggers.MAINTENANCE


def _cwd() -> Path:
    """Somewhere to run ``crontab`` from.

    The crontab is host-global, so the directory only guards against a
    deleted process cwd; host-scoped callers (the maintenance loop,
    uninstall) legitimately have no project.
    """
    try:
        return paths.resolve_root()
    except ValueError:
        return Path.home()


def lines() -> list[str] | None:
    """Every crontab line, or None when the crontab cannot be read.

    A user with no crontab yet (``crontab -l`` exits 1 with ``no crontab
    for <user>``) has an empty crontab, not an unreadable one, and gets
    ``[]``. The difference matters: :func:`write` replaces the whole
    table, so treating an unreadable crontab as empty would erase every
    entry the read failed to see.
    """
    try:
        completed = subprocess.run(
            ["crontab", "-l"], cwd=_cwd(), capture_output=True, check=False,
            **hostruntime.CHILD_TEXT,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("crontab command not found") from exc
    if completed.returncode != 0:
        if "no crontab for" in (completed.stderr or ""):
            return []
        return None
    return [line for line in completed.stdout.splitlines() if line.strip()]


def write(new_lines: Sequence[str]) -> None:
    """Replace the crontab with *new_lines*."""
    payload = "\n".join(new_lines) + "\n" if new_lines else ""
    try:
        subprocess.run(
            ["crontab", "-"], cwd=_cwd(), input=payload, capture_output=True,
            check=True, **hostruntime.CHILD_TEXT,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("crontab command not found") from exc
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr.strip() if exc.stderr else "failed to update crontab"
        raise RuntimeError(detail) from exc


@contextmanager
def lock() -> Iterator[None]:
    """Fail fast if another agents-live process is mutating the crontab."""
    from .wsl_liveness import state_dir  # noqa: PLC0415 - stdlib-only module

    # One resolver for the host state dir: wsl_liveness.state_dir() applies
    # expanduser() to XDG_STATE_HOME, and wsl_liveness.uninstall cleans up
    # this lock file - a second inline resolution here would drift.
    held = ExitStack()
    try:
        held.enter_context(
            hostruntime.exclusive_lock(state_dir() / "crontab.lock"))
    except hostruntime.LockBusy as exc:
        raise RuntimeError(
            "crontab is busy; another agents-live process is updating it; retry"
        ) from exc
    try:
        yield
    finally:
        held.close()


@contextmanager
def _editing() -> Iterator[list[str]]:
    """Hold the lock over a read the caller may replace.

    The caller mutates the yielded list in place and the new table is
    written on exit only when it differs, so a no-op edit does not
    rewrite the crontab.
    """
    with lock():
        current = lines()
        if current is None:
            raise RuntimeError("crontab is not accessible")
        working = list(current)
        yield working
        if working != current:
            write(working)


def belongs_to_root(line: str, root: Path | str) -> bool:
    """Whether a persisted line names *root* as its repository."""
    return triggers.belongs_to_root(line, Path(root))


def matches(line: str, root: Path | str, agent: str, *,
            kind: str = CLOCK) -> bool:
    """Whether *line* is *agent*'s trigger of *kind* in *root*."""
    return triggers.matches(line, root=Path(root), name=agent, kind=kind)


def agent_of_line(line: str, root: Path | str, *,
                  kind: str = CLOCK) -> str | None:
    """The agent *line* names, or None when it is not ours."""
    return triggers.agent_name(line, root=Path(root), kind=kind)


def install(spec: triggers.TriggerSpec) -> str:
    """Persist *spec*, replacing any lines it supersedes.

    Exact ``--name`` token matching, never a substring: a substring test
    would also drop entries for sibling agents whose name contains this
    one (todo vs todo-push), or arbitrary entries when the name appears
    in the repo or script path.
    """
    rendered = triggers.render(spec)
    with _editing() as table:
        kept = [line for line in table
                if not matches(line, spec.root, spec.name, kind=spec.kind)]
        table[:] = [*kept, *rendered]
    return "; ".join(rendered)


def delete(root: Path | str, agent: str, *, kind: str) -> bool:
    """Withdraw *agent*'s trigger of *kind*. True when there was one."""
    removed = False
    with _editing() as table:
        kept = [line for line in table
                if not matches(line, root, agent, kind=kind)]
        removed = len(kept) != len(table)
        table[:] = kept
    return removed


def remove(root: Path | str, agent: str) -> bool:
    """Withdraw *agent*'s schedule. True when there was one.

    The watcher respawn line is not touched here; the lifecycle removes
    it separately, through :func:`delete` with ``WATCH``.
    """
    return delete(root, agent, kind=CLOCK)


def remove_under(environment: Path | str) -> int:
    """Drop every line that runs a program inside *environment*.

    Host-wide and root-agnostic, because the entries uninstall has to
    withdraw outlive the projects that installed them and cannot be
    reached by name. Pinning on the executable is what keeps the sweep
    honest: an entry running out of a source checkout is a developer's
    own and is left alone (#219).
    """
    removed = 0
    with _editing() as table:
        kept = [line for line in table
                if not triggers.runs_within(line, Path(environment))]
        removed = len(table) - len(kept)
        table[:] = kept
    return removed


def is_active(root: Path | str, agent: str) -> bool | None:
    """True when *agent* is scheduled here; None if that cannot be read."""
    table = lines()
    if table is None:
        return None
    return any(matches(line, root, agent) for line in table)


def installed_names(root: Path | str, *,
                    kind: str | None = None) -> list[str]:
    """Every agent in *root* with a trigger registered on this host.

    Runtime-is-truth enumeration: an orphan sweep asks this question, so
    an unreadable crontab answers with nothing rather than raising.
    """
    wanted = CLOCK if kind is None else kind
    names: list[str] = []
    for line in lines() or []:
        if wanted is WATCH and not belongs_to_root(line, root):
            continue
        agent = agent_of_line(line, root, kind=wanted)
        if agent is not None and agent not in names:
            names.append(agent)
    return sorted(names) if wanted is WATCH else names


def clock_task_predates_scheduled_flag(root: Path | str, agent: str) -> bool:
    """Always False: only a native task can fire coarser than its cron.

    Cron fires at firing times, so a POSIX line never needs the
    ``--scheduled`` marker that tells a repetition apart from a person
    running the agent by hand (#194).
    """
    return False


def current_form(spec: triggers.TriggerSpec
                 ) -> tuple[list[str], list[str]]:
    """(what is registered for *spec* here, what *spec* asks for)."""
    table = lines() or []
    if spec.kind == HOST:
        current = [line for line in table
                   if triggers.is_maintenance_line(line)]
    else:
        current = [line for line in table
                   if matches(line, spec.root, spec.name, kind=spec.kind)]
    return current, triggers.render(spec)


def install_maintenance(spec: triggers.TriggerSpec, *,
                        create: bool = True) -> bool:
    """Persist the check-and-repair loop's own trigger. True when changed.

    ``create=False`` converges a loop that is already registered after an
    upgrade re-homes the pinned shim path, but never adds one to a host
    that has not asked for it.
    """
    desired = triggers.render(spec)
    with _editing() as table:
        current = [line for line in table if triggers.is_maintenance_line(line)]
        if current == desired or (not current and not create):
            return False
        table[:] = [line for line in table
                    if not triggers.is_maintenance_line(line)] + desired
        return True


def remove_maintenance(spec: triggers.TriggerSpec) -> bool:
    """Withdraw the loop from this host. True when there was one."""
    del spec  # the crontab holds one loop entry, whatever it points at
    with _editing() as table:
        kept = [line for line in table
                if not triggers.is_maintenance_line(line)]
        removed = len(kept) != len(table)
        table[:] = kept
        return removed


def maintenance_change(spec: triggers.TriggerSpec
                       ) -> tuple[list[str], list[str]] | None:
    """(registered, desired) for the loop, or None when nothing reads."""
    table = lines()
    if table is None:
        return None
    return ([line for line in table if triggers.is_maintenance_line(line)],
            triggers.render(spec))


def persisted_roots() -> list[Path]:
    """Every existing repository this host has a trigger registered for."""
    roots: list[Path] = []
    for line in lines() or []:
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
