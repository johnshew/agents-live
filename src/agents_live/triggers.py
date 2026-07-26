"""Triggers: what should fire, separate from how a host persists it.

``TriggerSpec`` is the desired state - an agent, its repository, the
schedules it fires on, and the command a firing runs. It says nothing
about crontabs. Callers that want an agent scheduled describe it with a
spec and let the host layer decide what to write.

Below the vocabulary sits the POSIX form: one crontab line per schedule,
self-contained (§3.4.2), plus the matchers that recognise a line this
project owns. These are the crontab's business alone and become
``PosixRuntime`` internals when that class lands
(docs/windows-support.md); a Windows host renders the same spec into a
scheduled task instead. Nothing outside the host layer should read or
build a line.
"""
from __future__ import annotations

import shlex
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

# A schedule trigger runs the agent; a watcher trigger respawns its
# file watcher after a reboot. They persist in the same table and are
# told apart by the token pair they carry.
SCHEDULE = "schedule"
WATCHER = "watcher"

# The token that names the agent, per kind. Legacy flag-form watcher
# lines stay recognisable so migrate can replace them.
_NAME_TOKENS = {
    SCHEDULE: ("--name",),
    WATCHER: ("ensure-watcher", "--ensure-watcher"),
}


@dataclass(frozen=True)
class TriggerSpec:
    """What should fire, for one agent and one kind of trigger."""

    name: str
    kind: str
    root: Path
    schedules: tuple[str, ...]
    command: tuple[str, ...]
    path: str


# --- The schedule language itself ---
#
# Cron expressions are the one schedule vocabulary on every host
# (docs/windows-support.md), so reading them belongs here rather than
# beside either dispatch mechanism. On Linux the crontab answers "is
# this minute a firing time"; on Windows the native trigger is allowed
# to be coarser than the expression, and this predicate answers it
# instead.

BOOT = "@reboot"

_FIELD_BOUNDS = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7))


class ScheduleSyntaxError(ValueError):
    """A cron expression this project cannot read."""


def _field_values(field: str, low: int, high: int) -> set[int]:
    values: set[int] = set()
    for part in field.split(","):
        item, _, step_text = part.partition("/")
        step = int(step_text) if step_text.isdigit() and int(step_text) else 0
        if step_text and not step:
            raise ScheduleSyntaxError(f"invalid step in cron field '{field}'")
        step = step or 1
        if item == "*":
            first, last = low, high
        elif "-" in item:
            start_text, _, end_text = item.partition("-")
            if not (start_text.isdigit() and end_text.isdigit()):
                raise ScheduleSyntaxError(f"invalid range in cron field '{field}'")
            first, last = int(start_text), int(end_text)
        elif item.isdigit():
            first = last = int(item)
        else:
            raise ScheduleSyntaxError(f"invalid cron field '{field}'")
        if not (low <= first <= high and low <= last <= high and first <= last):
            raise ScheduleSyntaxError(
                f"cron field '{field}' is outside {low}-{high}")
        values.update(range(first, last + 1, step))
    if not values:
        raise ScheduleSyntaxError(f"empty cron field '{field}'")
    return values


def schedule_fields(expression: str) -> tuple[set[int], ...]:
    """The value set of each of the five fields of *expression*.

    Weekday 7 is folded onto 0, as cron does, so Sunday has one
    spelling by the time anything compares against it.
    """
    fields = expression.split()
    if len(fields) != 5:
        raise ScheduleSyntaxError(
            f"schedule '{expression}' does not have five cron fields")
    parsed = tuple(_field_values(field, low, high)
                   for field, (low, high) in zip(fields, _FIELD_BOUNDS))
    weekday = {0 if value == 7 else value for value in parsed[4]}
    return parsed[:4] + (weekday,)


def schedule_matches(expression: str, moment: datetime) -> bool:
    """Whether *expression* names *moment* as a firing time.

    Day-of-month and day-of-week are ORed when both are restricted and
    ANDed otherwise, which is cron's rule and the one surprise in the
    language worth stating out loud.
    """
    fields = expression.split()
    minutes, hours, days, months, weekdays = schedule_fields(expression)
    if moment.minute not in minutes or moment.hour not in hours:
        return False
    if moment.month not in months:
        return False
    day_restricted = fields[2] != "*"
    weekday_restricted = fields[4] != "*"
    day_ok = moment.day in days
    # Python's Monday=0 against cron's Sunday=0.
    weekday_ok = ((moment.weekday() + 1) % 7) in weekdays
    if day_restricted and weekday_restricted:
        return day_ok or weekday_ok
    return day_ok and weekday_ok


def schedule_minutes(expression: str) -> set[int]:
    """The minutes of the hour *expression* can fire in."""
    return schedule_fields(expression)[0]


# --- POSIX crontab form ---

def render(spec: TriggerSpec) -> list[str]:
    """The canonical crontab lines for *spec*, one per schedule.

    Each line is self-contained (§3.4.2): it cds to the repository and
    carries its own PATH, so nothing at fire time depends on ambient
    state and no shared ``PATH=`` line - which the user or another
    project may own - ever has to be touched.
    """
    return [
        f"{schedule} cd {shlex.quote(str(spec.root))} && "
        f"PATH={shlex.quote(spec.path)} {shlex.join(spec.command)} 2>&1"
        for schedule in spec.schedules
    ]


def _tokens(line: str) -> list[str]:
    try:
        return shlex.split(line)
    except ValueError:
        return line.split()


def belongs_to_root(line: str, root: Path | str) -> bool:
    """Whether a persisted line names *root* as its repository.

    The crontab is host-global, so this is what keeps one project from
    touching another project's entries.
    """
    wanted = str(root)
    tokens = _tokens(line)
    return any(
        first in {"cd", "--repo"} and second == wanted
        for first, second in zip(tokens, tokens[1:])
    )


def matches(line: str, *, root: Path | str, name: str, kind: str) -> bool:
    """Whether *line* is this project's *kind* trigger for *name*.

    Exact root and name tokens prevent collisions with other projects
    and with agent names that are substrings of one another (``todo``
    vs ``todo-push``).
    """
    if not belongs_to_root(line, root):
        return False
    tokens = _tokens(line)
    wanted = _NAME_TOKENS[kind]
    return any(
        first in wanted and second == name
        for first, second in zip(tokens, tokens[1:])
    )


def agent_name(line: str, *, root: Path | str, kind: str) -> str | None:
    """The agent *line* triggers, or None if it is not such a line.

    The inverse of :func:`matches`, for enumerating what a host has
    installed without knowing the names in advance.
    """
    if not belongs_to_root(line, root):
        return None
    tokens = _tokens(line)
    if kind == SCHEDULE and not any(
        "run.py" in token or Path(token).name == "agents-live"
        for token in tokens
    ):
        # An unrelated crontab entry that happens to sit in this repo.
        return None
    wanted = _NAME_TOKENS[kind]
    for first, second in zip(tokens, tokens[1:]):
        if first in wanted:
            return second
    return None


def is_canonical(installed: list[str], spec: TriggerSpec) -> bool:
    """Whether what is installed already is what *spec* would write.

    The one place that decides trigger equality, so convergence checks
    cannot drift from what installation produces.
    """
    return sorted(installed) == sorted(render(spec))
