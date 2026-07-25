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
