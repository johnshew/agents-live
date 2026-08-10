"""Windows Task Scheduler: where a Windows host keeps its schedules.

The Windows half of the trigger track (docs/windows-support.md). One
task per agent, all under a dedicated ``\\AgentsLive\\`` folder, so a
single enumeration shows this tool's whole footprint on a machine that -
unlike a crontab, which is one file a developer already knows how to
read - keeps registrations across reboots and outlives whatever created
them.

Nothing outside :mod:`schedules` imports this module. Everything above
that dispatch point speaks ``TriggerSpec``.

The parts that decide what Windows will be told - argument quoting, task
naming, XML rendering, cron translation - are pure and run on any
platform, so they are tested everywhere and not only where they take
effect. Only the four functions that talk to ``schtasks`` are Windows
only.

Two Windows facts shape the rest:

- A task stores one argument *string*, not a vector, so the quoting has
  to be exactly the quoting ``CommandLineToArgvW`` will undo. It is
  verified by round trip on every install rather than trusted, because
  the failure mode is a silently different command, not an error.
- ``schtasks`` writes its query output through the console code page.
  Read-back is therefore treated as advisory-but-strict: anything that
  does not decode cleanly fails the ownership check, and this module
  refuses rather than replacing or deleting a task it cannot confirm.
"""
from __future__ import annotations

import csv
import os
import re
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from collections.abc import Sequence
from datetime import datetime, timedelta
from hashlib import sha256
from math import gcd
from pathlib import Path, PureWindowsPath

try:
    from ...legacy import triggers
    from . import system as hostruntime
except ImportError:  # flat execution: sibling scripts import this flat
    from legacy import triggers  # type: ignore[no-redef]
    from runtime.hosts import system as hostruntime  # type: ignore[no-redef]

# Every registration this tool makes lives here, so the enumeration a
# developer runs to confirm teardown is one command with one answer.
TASK_FOLDER = "\\AgentsLive"

# One task carries one action, so an agent that fires in more than one
# way gets more than one task, and the suffix says which is which. They
# answer the dueness question differently: a clock fire is checked
# against the expression, a startup fire is always due, and a watcher
# respawn does not run the agent at all.
CLOCK = ""
BOOT = ".boot"
WATCH = ".watch"
# The tool's own check-and-repair loop. It is registered like any
# trigger, but it belongs to the tool rather than to an agent, and
# saying so in the name is what keeps the loop from being swept up as
# an orphan by the very pass it runs.
HOST = ".host"
_SUFFIXES = (BOOT, WATCH, HOST)

_NS = "http://schemas.microsoft.com/windows/2004/02/mit/task"
_TASK_VERSION = "1.4"

# The token every task this tool registers runs with. An interactive
# token is what makes a scheduled agent behave like a cron job started
# by the developer: their environment, their credentials, their agent
# CLI logins. The price is that it can only run inside a signed-in
# session, so a machine sitting at the sign-in screen runs nothing.
# Running while logged off means stored credentials or S4U, which is a
# different security posture and deliberately not this one
# (docs/windows-support.md, Security model).
LOGON_TYPE = "InteractiveToken"

# Agent names reach a task name, an XML document, and a command line.
# The set that is safe in all three is the set agent files already use.
# A leading underscore is part of that set: ephemeral agents are named
# `_name` so they match the `Agents/_*` ignore patterns. A leading dot
# or dash is still refused - one hides the task, the other reads as an
# option on the command line.
_SAFE_AGENT_NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._-]*")

_INTERVAL_MINUTE = re.compile(r"\*/(\d{1,4})")

# Task Scheduler names calendar units with elements, not numbers.
_WEEKDAY_ELEMENTS = ("Sunday", "Monday", "Tuesday", "Wednesday", "Thursday",
                     "Friday", "Saturday")
_MONTH_ELEMENTS = ("January", "February", "March", "April", "May", "June",
                   "July", "August", "September", "October", "November",
                   "December")

# argv[0] is parsed by rules of its own, so round-trip verification
# prepends a token that cannot be confused with the arguments under test.
_PROGRAM_TOKEN = '"agents-live-round-trip"'


class TaskError(RuntimeError):
    """This host's task store could not do what was asked of it."""


class ArgumentQuotingError(TaskError):
    """A built argument string does not parse back to what went in."""


class TaskNotOurs(TaskError):
    """A registered task with our name is not one we recognise."""


class ScheduleNotTranslatable(TaskError):
    """No native trigger expresses this schedule exactly, yet."""


class TaskSchedulerUnavailable(TaskError):
    """The task store could not be reached at all."""


# ---------------------------------------------------------------------------
# Argument strings
# ---------------------------------------------------------------------------

def quote_argument(value: str) -> str:
    """Quote one argument the way ``CommandLineToArgvW`` will undo.

    The rule that is easy to get wrong is the backslash rule: a
    backslash is literal unless it runs into a quote, at which point
    the run doubles and an odd count escapes the quote. A path ending
    in a separator sits right before the closing quote, which makes
    every Windows directory argument a case of it.
    """
    if value and not any(ch in value for ch in ' \t\n\v"'):
        return value
    out = ['"']
    backslashes = 0
    for ch in value:
        if ch == "\\":
            backslashes += 1
            continue
        if ch == '"':
            out.append("\\" * (backslashes * 2 + 1))
            out.append('"')
        else:
            out.append("\\" * backslashes)
            out.append(ch)
        backslashes = 0
    out.append("\\" * (backslashes * 2))
    out.append('"')
    return "".join(out)


def parse_command_line(line: str) -> list[str]:
    """Split *line* into arguments the way this host's loader will.

    Windows answers with the function that will actually do it. Other
    platforms answer with the documented rules, so the round-trip check
    below is a real check wherever the suite runs, and the authoritative
    one where it matters.
    """
    if sys.platform == "win32":
        return _parse_command_line_win32(line)
    return _parse_command_line_reference(line)


def _parse_command_line_win32(line: str) -> list[str]:
    import ctypes  # noqa: PLC0415 - Windows-only, and only for this call

    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    shell32.CommandLineToArgvW.restype = ctypes.POINTER(ctypes.c_wchar_p)
    shell32.CommandLineToArgvW.argtypes = [ctypes.c_wchar_p,
                                           ctypes.POINTER(ctypes.c_int)]
    count = ctypes.c_int(0)
    argv = shell32.CommandLineToArgvW(line, ctypes.byref(count))
    if not argv:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return [argv[index] or "" for index in range(count.value)]
    finally:
        kernel32.LocalFree(argv)


def _parse_command_line_reference(line: str) -> list[str]:
    """The documented Windows argument-splitting rules, in Python."""
    args: list[str] = []
    current: list[str] = []
    started = False
    in_quotes = False
    index = 0
    while index < len(line):
        ch = line[index]
        if not in_quotes and ch in " \t":
            if started:
                args.append("".join(current))
                current = []
                started = False
            index += 1
            continue
        started = True
        if ch == "\\":
            slashes = 0
            while index < len(line) and line[index] == "\\":
                slashes += 1
                index += 1
            if index < len(line) and line[index] == '"':
                current.append("\\" * (slashes // 2))
                if slashes % 2:
                    current.append('"')
                    index += 1
            else:
                current.append("\\" * slashes)
            continue
        if ch == '"':
            if in_quotes and index + 1 < len(line) and line[index + 1] == '"':
                current.append('"')
                index += 2
                continue
            in_quotes = not in_quotes
            index += 1
            continue
        current.append(ch)
        index += 1
    if started:
        args.append("".join(current))
    return args


def argument_string(args: Sequence[str]) -> str:
    """The verified argument string for *args*.

    Built, parsed back, and compared. A mismatch means the command that
    would run is not the command that was asked for, which is a bug in
    the quoting above rather than anything a caller can fix, so it stops
    the install instead of reaching the task store.
    """
    line = " ".join(quote_argument(arg) for arg in args)
    parsed = parse_command_line(f"{_PROGRAM_TOKEN} {line}")[1:]
    if parsed != list(args):
        raise ArgumentQuotingError(
            "the argument string does not parse back to the command it was "
            f"built from: {parsed!r} != {list(args)!r}")
    return line


# ---------------------------------------------------------------------------
# Hidden execution
# ---------------------------------------------------------------------------

# A task action names a program Windows will start in the developer's
# own session, so it has to be one that has no window to show. These
# are the interpreter that has none and the module it runs.
_HIDDEN_HOST = "pythonw"
_HIDDEN_MODULE = "agents_live.runtime.hosts.hidden"
_LEGACY_HIDDEN_MODULE = "agents_live.hidden"
# -P keeps the working directory off sys.path, so a repository that
# happens to contain a matching name cannot answer the import.
_HIDDEN_ARGS = ("-P", "-m", _HIDDEN_MODULE)


def hidden_host() -> Path | None:
    """The windowless interpreter beside this one, or None if absent.

    Same installation the pinned executable comes from:
    :func:`headless.cli_shim_path` also resolves against the interpreter
    that is running, and both end up in the environment ``uv tool
    install`` made.
    """
    if not sys.executable:
        return None
    host = Path(sys.executable).with_name(
        f"{_HIDDEN_HOST}{Path(sys.executable).suffix}")
    return host if host.is_file() else None


def action_form(command: str, args: Sequence[str]) -> tuple[str, str]:
    """The ``(Command, Arguments)`` that run *command* with no window.

    A task runs with an interactive token, in the developer's session,
    so a console program named directly opens a console window - once
    per fire, on top of whatever they were doing. The action therefore
    names ``pythonw``, which has no console to show, and it starts the
    real command with ``CREATE_NO_WINDOW``
    (see :mod:`agents_live.runtime.hosts.hidden`).

    The indirection is what keeps the agent's own output working: run
    under ``pythonw`` directly it would have no standard streams at all
    and fail on its first write, while started this way it gets a
    console of its own that simply is not drawn.

    Wrapping is not identity: :func:`_action_program` reads back through
    it, so ownership still asks what finally runs. With no interpreter
    to wrap with, the command is registered plainly - a visible window
    is worse than none, and better than an agent that does not run.
    """
    host = hidden_host()
    if host is None:
        return command, argument_string(list(args))
    return str(host), argument_string([*_HIDDEN_ARGS, command, *args])


def _action_program(command: str, arguments: str) -> str:
    """The program a registered action ultimately runs.

    Anything not recognised as this module's own wrapper is taken at
    face value, so an interpreter running something else stays exactly
    as foreign as it was.
    """
    if PureWindowsPath(command).stem.casefold() != _HIDDEN_HOST:
        return command
    parts = parse_command_line(f"{_PROGRAM_TOKEN} {arguments}")[1:]
    module = next(
        (name for name in (_HIDDEN_MODULE, _LEGACY_HIDDEN_MODULE)
         if name in parts),
        None,
    )
    if module is None:
        return command
    index = parts.index(module) + 1
    return parts[index] if index < len(parts) else command


# ---------------------------------------------------------------------------
# Task identity
# ---------------------------------------------------------------------------

def _path_key(value: Path | str) -> str:
    """A comparable form of a Windows path, computed the same on any host.

    ``os.path.normcase`` folds case only on Windows, which would make the
    task digest and the ownership check depend on where the code runs.
    Task-store paths are always Windows paths, so read them as such.
    """
    return PureWindowsPath(str(value)).as_posix().rstrip("/").casefold()


def task_name(root: Path | str, agent: str, *, kind: str = CLOCK) -> str:
    """The task name for *agent* in the repository at *root*.

    Deterministic and repository-scoped: the same agent in two checkouts
    registers two tasks, and neither can replace or delete the other.
    The digest carries the root because a task name is one flat string
    with no room for a path.

    *kind* names which of the agent's tasks this is: its clock schedule,
    its ``@reboot`` schedule, or the respawn that puts its watcher back
    after a restart.
    """
    if not _SAFE_AGENT_NAME.fullmatch(agent):
        raise TaskError(
            f"agent name '{agent}' cannot be part of a task name; use "
            "letters, digits, dot, dash, and underscore, starting with a "
            "letter, digit, or underscore")
    digest = sha256(_path_key(root).encode("utf-8")).hexdigest()
    return f"{agent}@{digest[:8]}{kind}"


def task_path(root: Path | str, agent: str, *, kind: str = CLOCK) -> str:
    """The full task-store path for *agent*, inside the tool's folder."""
    return f"{TASK_FOLDER}\\{task_name(root, agent, kind=kind)}"


def kind_of_task_name(name: str) -> str:
    """Which of an agent's tasks *name* is: clock, startup, or watcher."""
    for suffix in _SUFFIXES:
        if name.endswith(suffix):
            return suffix
    return CLOCK


def agent_of_task_name(name: str, root: Path | str) -> str | None:
    """The agent *name* schedules for *root*, or None if it is not ours.

    A host task names no agent: nothing in a project defines it, so
    every caller asking "which agent is this" must hear nothing.
    """
    kind = kind_of_task_name(name)
    if kind == HOST:
        return None
    if kind:
        name = name[:-len(kind)]
    prefix, _, digest = name.rpartition("@")
    if not prefix or not digest:
        return None
    expected = sha256(_path_key(root).encode("utf-8")).hexdigest()
    return prefix if digest == expected[:8] else None


# ---------------------------------------------------------------------------
# Schedules
# ---------------------------------------------------------------------------

def translate(schedule: str) -> list[dict[str, object]]:
    """Native triggers that fire at least whenever *schedule* says.

    Exact wherever cron maps cleanly onto a trigger. Everywhere else the
    trigger is a superset - a repetition on a minute step that covers
    every minute the expression can name - and the dueness check in
    :func:`agents_live.legacy.schedules.claim_due_minute` declines the fires
    that are not real firing times. Guaranteeing a superset is much
    easier than guaranteeing exactness, which is why no valid
    expression is refused (docs/windows-support.md, Scheduling on
    Windows).
    """
    text = schedule.strip()
    if text == triggers.BOOT:
        return [{"kind": "boot"}]
    text = triggers._SPECIAL_SCHEDULES.get(text.lower(), text)
    try:
        minutes, _hours, days, _months, weekdays = triggers.schedule_fields(text)
    except triggers.ScheduleSyntaxError as exc:
        raise ScheduleNotTranslatable(str(exc)) from exc
    minute, hour, day_of_month, month, day_of_week = text.split()

    exact_time = (len(minutes) == 1 and hour.isdigit()
                  and month == "*")
    if exact_time:
        clock = {"hour": int(hour), "minute": next(iter(minutes))}
        if (day_of_month, day_of_week) == ("*", "*"):
            return [{"kind": "daily", **clock}]
        if day_of_month == "*" and len(weekdays) == 1:
            return [{"kind": "weekly", "weekday": next(iter(weekdays)), **clock}]
        if day_of_week == "*" and len(days) == 1:
            return [{"kind": "monthly", "day": next(iter(days)), **clock}]

    if (day_of_month, month, day_of_week) == ("*", "*", "*"):
        step = _INTERVAL_MINUTE.fullmatch(minute)
        if step and hour == "*" and 60 % int(step.group(1)) == 0:
            return [{"kind": "interval", "minutes": int(step.group(1)),
                     "anchor_minute": 0}]
        if len(minutes) == 1 and hour == "*":
            return [{"kind": "interval", "minutes": 60,
                     "anchor_minute": next(iter(minutes))}]

    return [_covering_interval(minutes)]


def _covering_interval(minutes: set[int]) -> dict[str, object]:
    """A repetition whose fires include every minute in *minutes*.

    The step is the largest one that still lands on all of them: the
    greatest common divisor of their offsets from the earliest, folded
    against the hour so the repetition keeps its phase across hours.
    A wider expression costs more declined fires, never a missed one.
    """
    anchor = min(minutes)
    step = 60
    for value in minutes:
        step = gcd(step, value - anchor)
    return {"kind": "interval", "minutes": step, "anchor_minute": anchor % step}



def _boundary(trigger: dict[str, object], now: datetime) -> str:
    """The first firing time for *trigger*, at or after *now*.

    Anchored forward rather than at a fixed date in the past: Task
    Scheduler treats a long-missed start as something to catch up on, so
    a past anchor would run the agent once the moment it is registered.
    """
    start = now.replace(second=0, microsecond=0)
    if trigger["kind"] in ("daily", "weekly", "monthly"):
        start = start.replace(hour=int(trigger["hour"]),
                              minute=int(trigger["minute"]))
        if start <= now:
            start += timedelta(days=1)
        return start.strftime("%Y-%m-%dT%H:%M:%S")
    step = int(trigger["minutes"])
    anchor = int(trigger["anchor_minute"])
    start = start.replace(minute=0) + timedelta(minutes=anchor)
    while start <= now:
        start += timedelta(minutes=step)
    return start.strftime("%Y-%m-%dT%H:%M:%S")


# ---------------------------------------------------------------------------
# Task XML
# ---------------------------------------------------------------------------

def _child(parent: ET.Element, tag: str, text: str | None = None) -> ET.Element:
    element = ET.SubElement(parent, f"{{{_NS}}}{tag}")
    if text is not None:
        element.text = text
    return element


def _append_trigger(parent: ET.Element, trigger: dict[str, object],
                    now: datetime, user_id: str) -> None:
    if trigger["kind"] == "boot":
        # A BootTrigger needs elevation to register, which a user-scoped
        # tool does not have and should not ask for. Logon is the closer
        # match anyway: the task runs with an interactive token, so a
        # session is what it actually needs (docs/windows-support.md).
        element = _child(parent, "LogonTrigger")
        _child(element, "Enabled", "true")
        _child(element, "UserId", user_id)
        return
    if trigger["kind"] == "daily":
        element = _child(parent, "CalendarTrigger")
        _child(element, "StartBoundary", _boundary(trigger, now))
        _child(element, "Enabled", "true")
        schedule = _child(element, "ScheduleByDay")
        _child(schedule, "DaysInterval", "1")
        return
    if trigger["kind"] == "weekly":
        element = _child(parent, "CalendarTrigger")
        _child(element, "StartBoundary", _boundary(trigger, now))
        _child(element, "Enabled", "true")
        schedule = _child(element, "ScheduleByWeek")
        days = _child(schedule, "DaysOfWeek")
        _child(days, _WEEKDAY_ELEMENTS[int(trigger["weekday"])])
        _child(schedule, "WeeksInterval", "1")
        return
    if trigger["kind"] == "monthly":
        element = _child(parent, "CalendarTrigger")
        _child(element, "StartBoundary", _boundary(trigger, now))
        _child(element, "Enabled", "true")
        schedule = _child(element, "ScheduleByMonth")
        days = _child(schedule, "DaysOfMonth")
        _child(days, "Day", str(int(trigger["day"])))
        months = _child(schedule, "Months")
        for name in _MONTH_ELEMENTS:
            _child(months, name)
        return
    element = _child(parent, "TimeTrigger")
    _child(element, "StartBoundary", _boundary(trigger, now))
    _child(element, "Enabled", "true")
    repetition = _child(element, "Repetition")
    _child(repetition, "Interval", f"PT{int(trigger['minutes'])}M")
    _child(repetition, "StopAtDurationEnd", "false")


def build_task_xml(*, command: str, arguments: str, working_dir: str,
                   schedules: Sequence[str], description: str, uri: str,
                   user_id: str, now: datetime | None = None) -> str:
    """The task definition document for one agent.

    Built as a tree rather than a template so a repository path that
    contains an ampersand or a quote is escaped by the XML writer
    instead of by hand.
    """
    now = now or datetime.now()
    ET.register_namespace("", _NS)
    task = ET.Element(f"{{{_NS}}}Task", {"version": _TASK_VERSION})

    registration = _child(task, "RegistrationInfo")
    _child(registration, "Description", description)
    _child(registration, "URI", uri)

    trigger_parent = _child(task, "Triggers")
    for schedule in schedules:
        for trigger in translate(schedule):
            _append_trigger(trigger_parent, trigger, now, user_id)

    principals = _child(task, "Principals")
    principal = ET.SubElement(principals, f"{{{_NS}}}Principal", {"id": "Author"})
    _child(principal, "UserId", user_id)
    # An interactive token runs the agent as the developer, with the
    # developer's environment and credentials, only while they are logged
    # on. Logged-off execution needs stored credentials or S4U and is
    # deliberately separate work (docs/windows-support.md, Security model).
    _child(principal, "LogonType", LOGON_TYPE)
    _child(principal, "RunLevel", "LeastPrivilege")

    settings = _child(task, "Settings")
    # IgnoreNew, so a slow agent is never run twice at once; cron would
    # overlap, and overlapping agent runs share one log and one lock.
    _child(settings, "MultipleInstancesPolicy", "IgnoreNew")
    # Battery state is not a scheduling input on any other host, and a
    # laptop would otherwise silently stop running agents when unplugged.
    _child(settings, "DisallowStartIfOnBatteries", "false")
    _child(settings, "StopIfGoingOnBatteries", "false")
    _child(settings, "AllowHardTerminate", "true")
    # Catch-up after sleep stays Task Scheduler's job, per the design.
    _child(settings, "StartWhenAvailable", "true")
    _child(settings, "RunOnlyIfNetworkAvailable", "false")
    idle = _child(settings, "IdleSettings")
    _child(idle, "StopOnIdleEnd", "false")
    _child(idle, "RestartOnIdle", "false")
    _child(settings, "AllowStartOnDemand", "true")
    _child(settings, "Enabled", "true")
    _child(settings, "Hidden", "false")
    _child(settings, "RunOnlyIfIdle", "false")
    # Waking a sleeping machine is not something cron does.
    _child(settings, "WakeToRun", "false")
    _child(settings, "ExecutionTimeLimit", "PT24H")
    _child(settings, "Priority", "7")

    actions = ET.SubElement(task, f"{{{_NS}}}Actions", {"Context": "Author"})
    exec_action = _child(actions, "Exec")
    _child(exec_action, "Command", command)
    if arguments:
        _child(exec_action, "Arguments", arguments)
    _child(exec_action, "WorkingDirectory", working_dir)

    body = ET.tostring(task, encoding="unicode")
    return f'<?xml version="1.0" encoding="UTF-16"?>\r\n{body}'


# ---------------------------------------------------------------------------
# The task store
# ---------------------------------------------------------------------------

def _schtasks() -> str:
    system32 = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32"
    candidate = system32 / "schtasks.exe"
    if candidate.is_file():
        return str(candidate)
    found = shutil.which("schtasks")
    if found is None:
        raise TaskSchedulerUnavailable("schtasks was not found on this host")
    return found


def _run(args: Sequence[str], *,
         timeout: float | None = None) -> tuple[int, str, str]:
    """Run a schtasks command and decode both streams.

    Decoded through the console code page, which is what schtasks writes
    even into a pipe; anything undecodable is replaced rather than
    raising, and callers treat a lossy read-back as unverified.
    """
    try:
        completed = subprocess.run([_schtasks(), *args], capture_output=True,
                                   check=False, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise TaskSchedulerUnavailable(
            f"schtasks did not answer within {timeout:g}s") from exc
    except OSError as exc:
        raise TaskSchedulerUnavailable(f"schtasks could not be run: {exc}") from exc
    encoding = "oem" if sys.platform == "win32" else "utf-8"
    out = completed.stdout.decode(encoding, errors="replace")
    err = completed.stderr.decode(encoding, errors="replace")
    return completed.returncode, out, err


def current_user_id() -> str:
    """The principal a registered task runs as: this user."""
    user = os.environ.get("USERNAME") or ""
    domain = os.environ.get("USERDOMAIN") or os.environ.get("COMPUTERNAME") or ""
    if not user:
        raise TaskError("cannot determine the user a task would run as")
    return f"{domain}\\{user}" if domain else user


# schtasks says "cannot find the file specified" for a missing task and
# "cannot find the path specified" for a folder that no task has ever
# been registered into. Both mean the same thing here: not registered.
_NOT_REGISTERED = "cannot find the "

# How long each half of the probe below may take. Our own folder answers
# in a fraction of a second once anything has been registered there. The
# root walks the whole machine's task tree - about 2000 lines on a normal
# install, measured anywhere from 4 to 26 seconds on the same host - so it
# gets room to finish, and is only ever reached when our folder is absent.
_FOLDER_QUERY_TIMEOUT_S = 15
_ROOT_QUERY_TIMEOUT_S = 120


def missing_dependency() -> str | None:
    """What this host is missing to reach the task store, or None."""
    try:
        _schtasks()
    except TaskSchedulerUnavailable as exc:
        return str(exc)
    return None


def probe() -> str | None:
    """Why this user cannot read the task store, or None if they can.

    Side-effect free, and asks about our own folder first: querying the
    root is slow enough to turn an advisory probe into a hard failure.
    A refusal is decided there and reported immediately - only "not
    registered yet" is ambiguous enough to be worth the root walk, and
    that answer already proves the store was reachable.
    """
    for target, timeout in ((TASK_FOLDER + "\\", _FOLDER_QUERY_TIMEOUT_S),
                            ("\\", _ROOT_QUERY_TIMEOUT_S)):
        try:
            code, _out, err = _run(["/Query", "/TN", target, "/FO", "LIST"],
                                   timeout=timeout)
        except TaskSchedulerUnavailable as exc:
            return f"cannot read the task store: {exc}"
        if code == 0:
            return None
        if _NOT_REGISTERED not in err.lower():
            return (f"schtasks query failed (rc={code}): "
                    f"{err.strip()[:200]}")
    return None


def read_definition(path: str) -> str | None:
    """The registered XML for the task at *path*, or None if there is none."""
    code, out, err = _run(["/Query", "/TN", path, "/XML", "ONE"])
    if code != 0:
        if _NOT_REGISTERED in err.lower():
            return None
        raise TaskSchedulerUnavailable(err.strip() or "schtasks query failed")
    return out


def principal_of_definition(document: str) -> tuple[str, str] | None:
    """The user a registered task runs as, and the token it runs with.

    What decides whether a scheduled agent can run at all: an
    interactive token means the run happens in the owner's session and
    only while they are signed in. Reading it back is how the tool can
    say that out loud instead of leaving a missed run unexplained.
    """
    body = document.split("?>", 1)[-1].strip()
    try:
        task = ET.fromstring(body)
    except ET.ParseError:
        return None
    principal = task.find(f".//{{{_NS}}}Principal")
    if principal is None:
        return None
    return (principal.findtext(f"{{{_NS}}}UserId", default=""),
            principal.findtext(f"{{{_NS}}}LogonType", default=""))


def _definition_action(document: str) -> tuple[str, str, str] | None:
    """The command, arguments, and working directory of a registered task."""
    # The document declares UTF-16 but arrives already decoded, so the
    # declaration is dropped rather than believed.
    body = document.split("?>", 1)[-1].strip()
    try:
        task = ET.fromstring(body)
    except ET.ParseError:
        return None
    exec_action = task.find(f".//{{{_NS}}}Exec")
    if exec_action is None:
        return None
    command = exec_action.findtext(f"{{{_NS}}}Command", default="")
    arguments = exec_action.findtext(f"{{{_NS}}}Arguments", default="")
    working_dir = exec_action.findtext(f"{{{_NS}}}WorkingDirectory", default="")
    return command, arguments, working_dir


def _is_ours(document: str, root: Path | str) -> bool:
    """Whether a registered definition is one this tool wrote for *root*.

    The name already carries a digest of the root, so this is the second
    of the two checks the security model asks for before a replace or a
    delete: the action itself has to be an agents-live command working in
    that repository. A definition that does not decode or does not parse
    fails, which is what makes an unverifiable task safe from us.
    """
    if "\ufffd" in document:
        return False
    action = _definition_action(document)
    if action is None:
        return False
    command, arguments, working_dir = action
    if _path_key(working_dir) != _path_key(root):
        return False
    program = _action_program(command, arguments)
    return PureWindowsPath(program).stem.casefold() == "agents-live"


def _kind_plan(spec: triggers.TriggerSpec
               ) -> dict[str, tuple[list[str], list[str]]]:
    """Which task kinds *spec* asks for: schedules and extra arguments.

    The one place that knows a spec becomes more than one task. A kind
    present with no schedules is a kind the agent used to declare and
    no longer does; a kind that is absent was never this spec's to own.
    """
    if spec.kind == triggers.WATCHER:
        return {WATCH: (list(spec.schedules), [])}
    if spec.kind == triggers.MAINTENANCE:
        # One task carrying every trigger the loop runs on. Nothing has
        # to tell a startup fire from a clock fire here: the loop does
        # the same work either way, so there is no --boot to add and no
        # second task to add it to.
        return {HOST: (list(spec.schedules), [])}
    if spec.kind != triggers.SCHEDULE:
        raise TaskError(f"cannot register a {spec.kind} trigger as a task")
    return {
        CLOCK: ([s for s in spec.schedules if s.strip() != triggers.BOOT], []),
        BOOT: ([s for s in spec.schedules if s.strip() == triggers.BOOT],
               ["--boot"]),
    }


def kinds(spec: triggers.TriggerSpec) -> tuple[str, ...]:
    """Every task kind *spec* could own, registered here or not.

    Convergence has to look at the kinds a spec can own rather than the
    kinds it currently asks for, because a task the spec has stopped
    asking for is exactly the one that has to be found and removed.
    """
    return tuple(_kind_plan(spec))


def desired_form(spec: triggers.TriggerSpec, *, kind: str
                 ) -> tuple[str, str, list[tuple]] | None:
    """What *kind*'s task should run and fire on, or None for no task.

    The comparable form of a registration: a command, an argument
    string, and what the store was asked to fire on. It is the wrapped
    form, because that is what a task holds and convergence has to
    compare like with like.
    """
    plan = _kind_plan(spec)
    if kind not in plan:
        return None
    schedules, extra = plan[kind]
    if not schedules:
        return None
    command, arguments = action_form(spec.command[0],
                                     list(spec.command[1:]) + extra)
    return command, arguments, trigger_signature(schedules)


def trigger_signature(schedules: Sequence[str]) -> list[tuple]:
    """What *schedules* ask the store to fire on, comparably.

    Not the XML: a registered document carries a start boundary
    computed from the moment it was written, so two documents that fire
    identically differ by the clock. The signature keeps what the
    boundary means (the phase of a repetition, the time of day of a
    calendar trigger) and drops when it was chosen.
    """
    return sorted(_signature_of(trigger)
                  for schedule in schedules
                  for trigger in translate(schedule))


def _signature_of(trigger: dict[str, object]) -> tuple:
    kind = trigger["kind"]
    if kind == "boot":
        return ("logon",)
    if kind == "interval":
        return ("interval", int(trigger["minutes"]),
                int(trigger["anchor_minute"]))
    if kind == "weekly":
        return ("weekly", int(trigger["weekday"]), int(trigger["hour"]),
                int(trigger["minute"]))
    if kind == "monthly":
        return ("monthly", int(trigger["day"]), int(trigger["hour"]),
                int(trigger["minute"]))
    return ("daily", int(trigger["hour"]), int(trigger["minute"]))


_ISO_INTERVAL = re.compile(r"P(?:(\d+)D)?T(?:(\d+)H)?(?:(\d+)M)?\Z")


def _interval_minutes(interval: str) -> int | None:
    """Minutes in an ISO-8601 repetition interval, or None if unreadable.

    Written as ``PT60M`` and read back as ``PT1H``: the scheduler
    normalizes the duration it stores, so an interval that is only ever
    matched as minutes reads as a task that lost its repetition, and
    convergence rewrites it forever.
    """
    match = _ISO_INTERVAL.fullmatch(interval.strip())
    if match is None:
        return None
    days, hours, minutes = (int(part or 0) for part in match.groups())
    total = days * 1440 + hours * 60 + minutes
    return total or None


def _definition_signature(document: str) -> list[tuple]:
    """The same signature, read back out of a registered definition."""
    body = document.split("?>", 1)[-1].strip()
    try:
        task = ET.fromstring(body)
    except ET.ParseError:
        return []
    found: list[tuple] = []
    parent = task.find(f"{{{_NS}}}Triggers")
    for element in ([] if parent is None else list(parent)):
        tag = element.tag.rsplit("}", 1)[-1]
        if tag == "LogonTrigger":
            found.append(("logon",))
            continue
        boundary = element.findtext(f"{{{_NS}}}StartBoundary", default="")
        try:
            start = datetime.strptime(boundary, "%Y-%m-%dT%H:%M:%S")
        except ValueError:
            continue
        if tag == "TimeTrigger":
            minutes = _interval_minutes(element.findtext(
                f".//{{{_NS}}}Interval", default=""))
            if minutes is None:
                continue
            found.append(("interval", minutes, start.minute % minutes))
            continue
        if element.find(f".//{{{_NS}}}ScheduleByWeek") is not None:
            days = element.find(f".//{{{_NS}}}DaysOfWeek")
            weekday = next(
                (index for index, name in enumerate(_WEEKDAY_ELEMENTS)
                 if days is not None
                 and days.find(f"{{{_NS}}}{name}") is not None),
                None)
            if weekday is None:
                continue
            found.append(("weekly", weekday, start.hour, start.minute))
            continue
        if element.find(f".//{{{_NS}}}ScheduleByMonth") is not None:
            day = element.findtext(f".//{{{_NS}}}Day", default="")
            if not day.isdigit():
                continue
            found.append(("monthly", int(day), start.hour, start.minute))
            continue
        found.append(("daily", start.hour, start.minute))
    return sorted(found)


def install(spec: triggers.TriggerSpec) -> str:
    """Register *spec* as this host's tasks for one agent.

    Returns a one-line description of what was registered, for the same
    reason the crontab path returns the line it wrote: the developer
    should see the thing that will run. An agent with both clock and
    ``@reboot`` schedules gets two tasks; one that loses a kind of
    schedule loses the matching task in the same call, so the store
    never keeps a trigger the agent no longer declares.

    A watcher respawn is the third kind: one startup task whose action
    is not a run of the agent but the guarded restart of its watcher.
    """
    command = spec.command[0]
    # A task store holds Windows paths, so the question "is this fully
    # qualified" is a Windows question wherever it is asked.
    if not PureWindowsPath(command).is_absolute():
        raise TaskError(
            f"a task must name a fully qualified executable, not '{command}'")
    lines = []
    plan = _kind_plan(spec)
    # Registrations first, removals second. An interruption between the
    # two then leaves the agent with more triggers than it asked for,
    # never with none: a task that fires when it should not is visible
    # and converges on the next pass, while an agent that has quietly
    # stopped firing is neither.
    for kind, (schedules, extra) in plan.items():
        if not schedules:
            continue
        args = list(spec.command[1:]) + extra
        registered, arguments = action_form(command, args)
        path = _register(spec, registered, arguments, schedules, kind=kind)
        # Reported without the wrapper: what the developer wants to see
        # is the command that runs, not the interpreter that hides it.
        lines.append(f"{path}: {command} {argument_string(args)}".rstrip())
    for kind, (schedules, _extra) in plan.items():
        if not schedules:
            delete(spec.root, spec.name, kind=kind)
    return "; ".join(lines)


def _register(spec: triggers.TriggerSpec, command: str, arguments: str,
              schedules: Sequence[str], *, kind: str) -> str:
    """Write one task and read it back. Returns the path it was given."""
    path = task_path(spec.root, spec.name, kind=kind)
    existing = read_definition(path)
    if existing is not None and not _is_ours(existing, spec.root):
        raise TaskNotOurs(
            f"a scheduled task named {path} exists and is not one this tool "
            "registered for this repository; remove it by hand first")

    if spec.kind == triggers.MAINTENANCE:
        description = "Agents Live: run the check-and-repair loop for this user"
    else:
        purpose = ("restart the watcher for agent" if kind == WATCH
                   else "run agent")
        description = f"Agents Live: {purpose} '{spec.name}' in {spec.root}"
    document = build_task_xml(
        command=command, arguments=arguments, working_dir=str(spec.root),
        schedules=schedules,
        description=description,
        uri=path, user_id=current_user_id())
    handle, xml_file = tempfile.mkstemp(suffix=".xml", prefix="agents-live-task-")
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(document.encode("utf-16"))
        code, _out, err = _run(["/Create", "/TN", path, "/XML", xml_file, "/F"])
    finally:
        Path(xml_file).unlink(missing_ok=True)
    if code != 0:
        raise TaskError(err.strip() or f"schtasks refused to register {path}")

    registered = read_definition(path)
    if registered is None:
        raise TaskError(f"{path} was registered but cannot be read back")
    _verify(path, registered, command, arguments, schedules)
    return path


def _verify(path: str, document: str, command: str, arguments: str,
            schedules: Sequence[str]) -> None:
    """Fail unless the store holds what registration asked it to hold.

    Reading the definition back is not enough on its own: it says a
    task is there, not that it is the right one. Comparing the action
    and the trigger signature catches an update that was interrupted or
    only partly applied at the moment it happened, and it catches a
    schedule this tool can write but cannot read back as the same
    thing - which otherwise looks like a task that converges cleanly
    and is then rewritten by every maintenance pass, forever.
    """
    action = _definition_action(document)
    if action is None or action[0] != command or action[1] != arguments:
        raise TaskError(
            f"{path} was registered but reads back running something else; "
            "remove it by hand in Task Scheduler and register again")
    if _definition_signature(document) != trigger_signature(schedules):
        raise TaskError(
            f"{path} was registered but reads back firing on a different "
            f"schedule than {', '.join(schedules)}")


def registered_tasks() -> list[dict[str, str]]:
    """Every task this tool could have registered, whatever repository.

    The consistency check has to see tasks pinned to a project root that
    no longer exists, and those can never be found by name: the name
    carries a digest of the root, and a root nobody can name cannot be
    digested. So this reads the folder rather than asking for a name,
    and reports what each task would run: ``{"name", "command",
    "arguments", "working_dir", "principal"}``.
    """
    try:
        code, out, _err = _run(["/Query", "/TN", f"{TASK_FOLDER}\\", "/FO",
                                "CSV", "/NH"])
    except TaskSchedulerUnavailable:
        return []
    if code != 0:
        return []
    tasks = []
    seen: set[str] = set()
    for row in csv.reader(out.splitlines()):
        if not row:
            continue
        leaf = row[0].rsplit("\\", 1)[-1]
        # A query reports a task once per trigger, and a task that fires
        # two ways is still one task.
        if leaf in seen:
            continue
        seen.add(leaf)
        try:
            document = read_definition(f"{TASK_FOLDER}\\{leaf}")
        except TaskSchedulerUnavailable:
            continue
        if document is None:
            continue
        action = _definition_action(document)
        if action is None:
            continue
        command, arguments, working_dir = action
        tasks.append({"name": leaf, "command": command,
                      "arguments": arguments, "working_dir": working_dir,
                      "principal": principal_of_definition(document)})
    return tasks


def registered_form(root: Path | str, agent: str,
                    *, kind: str) -> tuple[str, str, list[tuple]] | None:
    """What *agent*'s task of *kind* runs and fires on, or None.

    The read-back counterpart of :func:`desired_form`, so convergence
    compares two values of the same shape. A task this tool did not
    write for *root* reads as absent: it is not ours to converge.
    """
    try:
        document = read_definition(task_path(root, agent, kind=kind))
    except TaskSchedulerUnavailable:
        return None
    if document is None or not _is_ours(document, root):
        return None
    action = _definition_action(document)
    if action is None:
        return None
    command, arguments, _working_dir = action
    return command, arguments, _definition_signature(document)


def clock_task_predates_scheduled_flag(root: Path | str, agent: str) -> bool:
    """Whether the registered clock action can over-fire as a manual run."""
    registered = registered_form(root, agent, kind=CLOCK)
    if registered is None:
        return False
    _command, arguments, _signature = registered
    args = parse_command_line(f"{_PROGRAM_TOKEN} {arguments}")[1:]
    return "--scheduled" not in args


def delete(root: Path | str, agent: str, *, kind: str) -> bool:
    path = task_path(root, agent, kind=kind)
    existing = read_definition(path)
    if existing is None:
        return False
    if not _is_ours(existing, root):
        raise TaskNotOurs(
            f"refusing to delete {path}: it is not a task this tool "
            "registered for this repository")
    code, _out, err = _run(["/Delete", "/TN", path, "/F"])
    if code != 0:
        raise TaskError(err.strip() or f"schtasks refused to delete {path}")
    return True


def remove(root: Path | str, agent: str) -> bool:
    """Delete *agent*'s tasks: clock, startup, and watcher respawn."""
    removed = False
    for kind in (CLOCK, BOOT, WATCH):
        removed = delete(root, agent, kind=kind) or removed
    return removed


def remove_under(environment: Path | str) -> int:
    """Delete every registered task that runs a program inside *environment*.

    Ownership is decided by what the action finally executes, read back
    through the windowless wrapper, rather than by the repository the
    name digests: uninstall has to reach tasks whose project it was
    never run from, including ones pinned to a root nobody can name any
    more. A task a developer pointed at a source checkout keeps working
    after the tool goes, so it is left alone (#219).
    """
    removed = 0
    for task in registered_tasks():
        program = _action_program(task["command"], task["arguments"])
        if not _within(program, environment):
            continue
        path = f"{TASK_FOLDER}\\{task['name']}"
        code, _out, err = _run(["/Delete", "/TN", path, "/F"])
        if code != 0:
            raise TaskError(err.strip() or f"schtasks refused to delete {path}")
        removed += 1
    return removed


def _within(candidate: str, root: Path | str) -> bool:
    """Whether *candidate* names something inside *root*.

    Read as a Windows path for the same reason as :func:`_path_key`: a
    task store holds Windows paths whatever host is asking about them.
    """
    if not candidate:
        return False
    key = _path_key(root)
    return _path_key(candidate).startswith(f"{key}/")


def is_active(root: Path | str, agent: str) -> bool | None:
    """True if *agent* has a task registered, None if nothing reads.

    Any of the three counts: an agent scheduled only for startup is as
    active as one scheduled by the clock.

    The folder listing decides which definitions are worth reading. It
    is one query for the whole host and is taken once per enumeration
    pass, so an agent with one task costs one definition read and an
    agent with none costs nothing - where every agent used to cost
    three regardless. A listing that cannot be read decides nothing,
    and all three are read as before.
    """
    kinds: tuple[str, ...] = (CLOCK, BOOT, WATCH)
    registered = registered_task_names()
    if registered is not None:
        present = {name.casefold() for name in registered}
        kinds = tuple(kind for kind in kinds
                      if task_name(root, agent, kind=kind).casefold() in present)
        if not kinds:
            return False
    answers = []
    for kind in kinds:
        try:
            existing = read_definition(task_path(root, agent, kind=kind))
        except TaskSchedulerUnavailable:
            return None
        answers.append(existing is not None and _is_ours(existing, root))
    return any(answers)


def _read_task_names() -> list[str] | None:
    """Every task name in the tool's folder, or None if that cannot be read.

    Names only, from one ``schtasks`` query. Nothing is decided from
    this listing that ownership rests on: the verbose form of the query
    reports neither the working directory nor a stable, locale-free
    action, so :func:`_is_ours` still reads the registered XML. An empty
    folder and an unreachable task store are told apart because the
    caller has to treat them differently.
    """
    try:
        code, out, err = _run(["/Query", "/TN", f"{TASK_FOLDER}\\", "/FO",
                               "CSV", "/NH"])
    except TaskSchedulerUnavailable:
        return None
    if code != 0:
        # No folder yet means nothing is registered, which is an answer.
        # Anything else is a store that did not answer.
        return [] if _NOT_REGISTERED in err.lower() else None
    names: list[str] = []
    for row in csv.reader(out.splitlines()):
        if row and row[0].strip():
            names.append(row[0].rsplit("\\", 1)[-1])
    return names


def registered_task_names() -> list[str] | None:
    """The tool's registered task names, read once per enumeration pass."""
    return hostruntime.pass_cached("wintasks-registered-names", _read_task_names)


def installed_names(root: Path | str, *, kind: str | None = None) -> list[str]:
    """Every agent in *root* with a task registered on this host.

    With *kind*, only agents whose task of that kind is registered: the
    question "which watchers is this host meant to be running" is asked
    of the watcher tasks alone.

    Runtime-is-truth enumeration: an orphan sweep asks this question, so
    a store that cannot be read answers with nothing rather than
    raising.
    """
    names = []
    for leaf in registered_task_names() or []:
        if kind is not None and kind_of_task_name(leaf) != kind:
            continue
        agent = agent_of_task_name(leaf, root)
        # An agent with a clock, a startup, and a watcher task is one agent.
        if agent and agent not in names:
            names.append(agent)
    return names


# --- The store-level questions `schedules` asks of either store --------------
#
# Their crontab peers are in `crontasks`; the two answer with the same
# signatures so the dispatch point selects a module instead of branching
# per operation. What a registered trigger looks like stays in here.

def _task_line(spec: triggers.TriggerSpec, kind: str,
               form: tuple[str, str, list[tuple]]) -> str:
    command, arguments, signature = form
    fires = " ".join(":".join(str(part) for part in entry)
                     for entry in signature)
    path = task_path(spec.root, spec.name, kind=kind)
    return f"{path} [{fires}]: {command} {arguments}".rstrip()


def current_form(spec: triggers.TriggerSpec) -> tuple[list[str], list[str]]:
    """(what is registered for *spec* here, what *spec* asks for).

    One display line per task, because a spec that fires two ways
    registers two tasks and a repair plan has to name each.
    """
    current: list[str] = []
    desired: list[str] = []
    for kind in kinds(spec):
        registered = registered_form(spec.root, spec.name, kind=kind)
        wanted = desired_form(spec, kind=kind)
        if registered is not None:
            current.append(_task_line(spec, kind, registered))
        if wanted is not None:
            desired.append(_task_line(spec, kind, wanted))
    return current, desired


def install_maintenance(spec: triggers.TriggerSpec, *,
                        create: bool = True) -> bool:
    """Persist the check-and-repair loop's own trigger. True when changed.

    ``create=False`` converges a loop that is already registered after an
    upgrade re-homes the pinned shim path, but never adds one to a host
    that has not asked for it.
    """
    current, desired = current_form(spec)
    if current == desired or (not current and not create):
        return False
    install(spec)
    return True


def remove_maintenance(spec: triggers.TriggerSpec) -> bool:
    """Withdraw the loop from this host. True when there was one."""
    return delete(spec.root, spec.name, kind=HOST)


def maintenance_change(spec: triggers.TriggerSpec
                       ) -> tuple[list[str], list[str]] | None:
    """(registered, desired) for the loop, or None when nothing reads."""
    return current_form(spec)


def persisted_roots() -> list[Path]:
    """Every existing repository this host has a trigger registered for."""
    roots: list[Path] = []
    for task in registered_tasks():
        # The loop's own task is pinned to the tool's state directory,
        # which is not a project and has nothing to sweep. On a crontab
        # host it names no repository at all; here the kind says it.
        if kind_of_task_name(task["name"]) == HOST:
            continue
        directory = task["working_dir"]
        if not directory:
            continue
        root = Path(directory).expanduser()
        if root.is_dir() and root not in roots:
            roots.append(root)
    return roots
