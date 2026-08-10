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
# The third is not an agent's at all: the check-and-repair loop this
# tool runs for itself. It is host-scoped rather than repo-scoped, so it
# names no repository, but it persists in the same store as the rest and
# is installed and removed through the same dispatch point.
MAINTENANCE = "maintenance"

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
_MONTH_NAMES = {
    name: index for index, name in enumerate(
        ("JAN", "FEB", "MAR", "APR", "MAY", "JUN",
         "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"), 1)
}
_WEEKDAY_NAMES = {
    name: index for index, name in enumerate(
        ("SUN", "MON", "TUE", "WED", "THU", "FRI", "SAT"))
}
_SPECIAL_SCHEDULES = {
    "@annually": "0 0 1 1 *",
    "@yearly": "0 0 1 1 *",
    "@monthly": "0 0 1 * *",
    "@weekly": "0 0 * * 0",
    "@daily": "0 0 * * *",
    "@midnight": "0 0 * * *",
    "@hourly": "0 * * * *",
}


class ScheduleSyntaxError(ValueError):
    """A cron expression this project cannot read."""


def _field_values(
    field: str,
    low: int,
    high: int,
    names: dict[str, int] | None = None,
) -> set[int]:
    names = names or {}

    def number(value: str) -> int | None:
        if value.isdigit():
            return int(value)
        return names.get(value.upper())

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
            start_value, end_value = number(start_text), number(end_text)
            if start_value is None or end_value is None:
                raise ScheduleSyntaxError(f"invalid range in cron field '{field}'")
            first, last = start_value, end_value
        elif number(item) is not None:
            first = number(item)
            last = high if step_text else first
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
    text = expression.strip()
    if text == BOOT:
        raise ScheduleSyntaxError("@reboot does not name clock fields")
    text = _SPECIAL_SCHEDULES.get(text.lower(), text)
    fields = text.split()
    if len(fields) != 5:
        raise ScheduleSyntaxError(
            f"schedule '{expression}' does not have five cron fields")
    names = ({}, {}, {}, _MONTH_NAMES, _WEEKDAY_NAMES)
    parsed = tuple(
        _field_values(field, low, high, field_names)
        for field, (low, high), field_names
        in zip(fields, _FIELD_BOUNDS, names, strict=True)
    )
    weekday = {0 if value == 7 else value for value in parsed[4]}
    return parsed[:4] + (weekday,)


def schedule_matches(expression: str, moment: datetime) -> bool:
    """Whether *expression* names *moment* as a firing time.

    Day-of-month and day-of-week are ORed when both are restricted and
    ANDed otherwise, which is cron's rule and the one surprise in the
    language worth stating out loud.
    """
    text = _SPECIAL_SCHEDULES.get(expression.strip().lower(), expression.strip())
    fields = text.split()
    minutes, hours, days, months, weekdays = schedule_fields(text)
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

    The maintenance loop is the exception, and for the same reason: it
    is host-scoped and resolves registered repositories itself, so it
    names no repository and carries no ``cd``.
    """
    if spec.kind == MAINTENANCE:
        return [
            f"{schedule} PATH={shlex.quote(spec.path)} "
            f"{shlex.join(spec.command)} 2>&1"
            for schedule in spec.schedules
        ]
    return [
        f"{schedule} cd {shlex.quote(str(spec.root))} && "
        f"PATH={shlex.quote(spec.path)} {shlex.join(spec.command)} 2>&1"
        for schedule in spec.schedules
    ]


def is_maintenance_line(line: str) -> bool:
    """True when *line* invokes this tool's own check-and-repair loop.

    Matches both the internal maintenance command and legacy
    health-check entries so convergence removes the retired public
    invocation.
    """
    parts = tokens(line)
    is_maintenance = (
        "health-check" in parts
        or any(parts[index:index + 2] == ["internal", "maintain"]
               for index in range(len(parts) - 1))
    )
    return is_maintenance and any(
        Path(token).name == "agents-live" for token in parts)


def tokens(line: str) -> list[str]:
    """The shell tokens of a persisted line, falling back on whitespace."""
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
    parts = tokens(line)
    return any(
        first in {"cd", "--repo"} and second == wanted
        for first, second in zip(parts, parts[1:])
    )


def within(candidate: str, root: Path | str) -> bool:
    """Whether *candidate* names something inside *root*.

    Containment is decided with :class:`Path` parents rather than string
    prefixes, so the separator and the case a path happens to be spelled
    with do not change the answer.
    """
    return bool(candidate) and Path(root) in Path(candidate).parents


def runs_within(line: str, root: Path | str) -> bool:
    """Whether a persisted line executes a program inside *root*.

    The root-agnostic ownership question, asked when the installation is
    what matters and the repository is not: uninstall withdraws entries
    for projects it was never run from, and a root nobody can name can
    still have entries pinned to it (#219).
    """
    return any(within(token, root) for token in tokens(line))


def matches(line: str, *, root: Path | str, name: str, kind: str) -> bool:
    """Whether *line* is this project's *kind* trigger for *name*.

    Exact root and name tokens prevent collisions with other projects
    and with agent names that are substrings of one another (``todo``
    vs ``todo-push``).
    """
    if not belongs_to_root(line, root):
        return False
    parts = tokens(line)
    wanted = _NAME_TOKENS[kind]
    return any(
        first in wanted and second == name
        for first, second in zip(parts, parts[1:])
    )


def agent_name(line: str, *, root: Path | str, kind: str) -> str | None:
    """The agent *line* triggers, or None if it is not such a line.

    The inverse of :func:`matches`, for enumerating what a host has
    installed without knowing the names in advance.
    """
    if not belongs_to_root(line, root):
        return None
    parts = tokens(line)
    if kind == SCHEDULE and not any(
        "run.py" in token or Path(token).name == "agents-live"
        for token in parts
    ):
        # An unrelated crontab entry that happens to sit in this repo.
        return None
    wanted = _NAME_TOKENS[kind]
    for first, second in zip(parts, parts[1:]):
        if first in wanted:
            return second
    return None


def is_canonical(installed: list[str], spec: TriggerSpec) -> bool:
    """Whether what is installed already is what *spec* would write.

    The one place that decides trigger equality, so convergence checks
    cannot drift from what installation produces.
    """
    return sorted(installed) == sorted(render(spec))
