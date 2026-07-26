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

from . import triggers

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
_SUFFIXES = (BOOT, WATCH)

_NS = "http://schemas.microsoft.com/windows/2004/02/mit/task"
_TASK_VERSION = "1.4"

# Agent names reach a task name, an XML document, and a command line.
# The set that is safe in all three is the set agent files already use.
_SAFE_AGENT_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")

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
            "letters, digits, dot, dash, and underscore")
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
    """The agent *name* schedules for *root*, or None if it is not ours."""
    kind = kind_of_task_name(name)
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
    :func:`agents_live.schedules.claim_due_minute` declines the fires
    that are not real firing times. Guaranteeing a superset is much
    easier than guaranteeing exactness, which is why no valid
    expression is refused (docs/windows-support.md, Scheduling on
    Windows).
    """
    text = schedule.strip()
    if text == triggers.BOOT:
        return [{"kind": "boot"}]
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
    _child(principal, "LogonType", "InteractiveToken")
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


def _run(args: Sequence[str]) -> tuple[int, str, str]:
    """Run a schtasks command and decode both streams.

    Decoded through the console code page, which is what schtasks writes
    even into a pipe; anything undecodable is replaced rather than
    raising, and callers treat a lossy read-back as unverified.
    """
    try:
        completed = subprocess.run([_schtasks(), *args], capture_output=True,
                                   check=False)
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


def read_definition(path: str) -> str | None:
    """The registered XML for the task at *path*, or None if there is none."""
    code, out, err = _run(["/Query", "/TN", path, "/XML", "ONE"])
    if code != 0:
        if _NOT_REGISTERED in err.lower():
            return None
        raise TaskSchedulerUnavailable(err.strip() or "schtasks query failed")
    return out


def _definition_action(document: str) -> tuple[str, str] | None:
    """The command and working directory the registered task would run."""
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
    working_dir = exec_action.findtext(f"{{{_NS}}}WorkingDirectory", default="")
    return command, working_dir


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
    command, working_dir = action
    if _path_key(working_dir) != _path_key(root):
        return False
    return PureWindowsPath(command).stem.casefold() == "agents-live"


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
    if spec.kind == triggers.WATCHER:
        return _register(spec, command, argument_string(list(spec.command[1:])),
                         list(spec.schedules), kind=WATCH)
    if spec.kind != triggers.SCHEDULE:
        raise TaskError(f"cannot register a {spec.kind} trigger as a task")
    boot = [s for s in spec.schedules if s.strip() == triggers.BOOT]
    clock = [s for s in spec.schedules if s.strip() != triggers.BOOT]
    lines = []
    for schedules, kind in ((clock, CLOCK), (boot, BOOT)):
        if not schedules:
            delete(spec.root, spec.name, kind=kind)
            continue
        arguments = argument_string(
            list(spec.command[1:]) + (["--boot"] if kind == BOOT else []))
        lines.append(_register(spec, command, arguments, schedules, kind=kind))
    return "; ".join(lines)


def _register(spec: triggers.TriggerSpec, command: str, arguments: str,
              schedules: Sequence[str], *, kind: str) -> str:
    path = task_path(spec.root, spec.name, kind=kind)
    existing = read_definition(path)
    if existing is not None and not _is_ours(existing, spec.root):
        raise TaskNotOurs(
            f"a scheduled task named {path} exists and is not one this tool "
            "registered for this repository; remove it by hand first")

    purpose = ("restart the watcher for agent" if kind == WATCH
               else "run agent")
    document = build_task_xml(
        command=command, arguments=arguments, working_dir=str(spec.root),
        schedules=schedules,
        description=f"Agents Live: {purpose} '{spec.name}' in {spec.root}",
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
    return f"{path}: {command} {arguments}".rstrip()


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


def is_active(root: Path | str, agent: str) -> bool | None:
    """True if *agent* has a task registered, None if nothing reads.

    Any of the three counts: an agent scheduled only for startup is as
    active as one scheduled by the clock.
    """
    answers = []
    for kind in (CLOCK, BOOT, WATCH):
        try:
            existing = read_definition(task_path(root, agent, kind=kind))
        except TaskSchedulerUnavailable:
            return None
        answers.append(existing is not None and _is_ours(existing, root))
    return any(answers)


def installed_names(root: Path | str, *, kind: str | None = None) -> list[str]:
    """Every agent in *root* with a task registered on this host.

    With *kind*, only agents whose task of that kind is registered: the
    question "which watchers is this host meant to be running" is asked
    of the watcher tasks alone.
    """
    try:
        code, out, _err = _run(["/Query", "/TN", f"{TASK_FOLDER}\\", "/FO",
                                "CSV", "/NH"])
    except TaskSchedulerUnavailable:
        return []
    if code != 0:
        # Nothing registered here yet, or nothing readable: an orphan
        # sweep asks this question and must not fail because of it.
        return []
    names = []
    for row in csv.reader(out.splitlines()):
        if not row:
            continue
        leaf = row[0].rsplit("\\", 1)[-1]
        if kind is not None and kind_of_task_name(leaf) != kind:
            continue
        agent = agent_of_task_name(leaf, root)
        # An agent with a clock, a startup, and a watcher task is one agent.
        if agent and agent not in names:
            names.append(agent)
    return names
