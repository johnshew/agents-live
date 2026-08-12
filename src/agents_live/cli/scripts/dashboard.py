#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["duckdb", "nicegui>=2.0", "PyYAML", "pywebview"]
# ///
"""Interactive agents-live control panel (single host).

The dashboard reads the agent, state, and observability ports and invokes the
public ``run``, ``start``, ``stop``, ``doctor``, and ``smoketest`` commands.
Every action is recorded in the repository event stream, with full command
output retained in ``dashboard-transcript.log``.

Scope: this build acts on the local host. Claim transfers an agent's ownership
through the ownership port and then starts it here.

Run it on the host, outside the agent sandbox (it needs crontab and
process access):

    uv run .claude/skills/agents-live/scripts/dashboard.py --dev

It binds to 127.0.0.1 only. Pass --native for a desktop window instead
of a browser tab. `--dev` auto-restarts when dashboard.py changes so it
stays current while you iterate.
"""
from __future__ import annotations

import argparse
import asyncio
import atexit
import json
import os
import re
import socket
import subprocess
import sys
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
PACKAGE_PARENT = SCRIPTS_DIR.parents[2]
if str(PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_PARENT))
from agents_live import __version__ as AGENTS_LIVE_VERSION  # noqa: E402
from agents_live import obs, paths, preflight  # noqa: E402
from agents_live.cli import agent_view  # noqa: E402
from agents_live.cli.scripts import dashboards  # noqa: E402
from agents_live.runtime.hosts import system as hostruntime  # noqa: E402
from agents_live.state import ownership, registry as repos  # noqa: E402
from nicegui import app, ui  # noqa: E402
from nicegui import run as ng_run  # noqa: E402

try:
    # Read-only, and useless without a project: the dashboard is the
    # caller the registry fallback exists for (issue #173).
    REPO_ROOT = paths.resolve_root(allow_sole_registered=True)
    REPO_ERROR: str | None = None
except ValueError as exc:
    REPO_ROOT = None
    REPO_ERROR = str(exc)
LOGS_DIR = paths.repo_state_dir(REPO_ROOT) / "logs" if REPO_ROOT else None
# Shown instead of the agent panel when nothing resolves: an empty table
# reads as broken agent discovery, so the page has to say what happened
# and how to choose a project (issue #173).
NO_PROJECT_HINT = (
    "No project is selected, so there are no agents to show. Start the "
    "dashboard inside an initialized project, pass "
    "`agents-live --repo <path> dashboard`, select a registered project "
    "with `agents-live repos default <path>`, or run "
    "`agents-live dashboard --all-repos`."
)
# The health beacon is host-scoped (written by `agents-live
# health-check`), so the panel works with or without a selected repo.
HEALTH_OK_PATH = paths.health_beacon_path()
# The health-check worker is scheduled hourly; allow a little slack before
# treating the beacon as stale (a missed run shouldn't flap the header).
HEALTH_STALE_MINUTES = 70
# Cap the on-demand health-check worker run from the dashboard. The worker's
# framework smoketest has its own 360s internal timeout; this is a hard outer
# bound so the spinner can never hang forever.
WORKER_TIMEOUT = 480

STATE: dict = {
    "last_refresh": datetime.now(timezone.utc),
    "models": {},
    "filters": {"name": "", "state": "All", "owner": "All",
                "runtime": "All", "failing": False},
}


def _require_repo_path(path: Path | None) -> Path:
    if path is None:
        raise RuntimeError(
            "single-repository dashboard requires a project root; "
            "use --all-repos outside an initialized project")
    return path


def _scope_label() -> str:
    """What the view is scoped to, for the header beside the host label.

    Without it an empty table cannot be told apart from the wrong
    project, and a populated one never says which project it describes.
    """
    return str(REPO_ROOT) if REPO_ROOT is not None else "no project selected"


# --- Data ---------------------------------------------------------------

def collect_agents() -> list[dict]:
    """Return agent details for every configured agent, sorted by name."""
    if REPO_ROOT is None:
        return []
    return [{
        "name": row.name,
        "identifier": row.identifier,
        "description": row.description,
        "state": row.state,
        "owner": row.owner,
        "isOwner": row.is_owner,
        "ownershipAvailable": row.ownership_available,
        "runtime": row.runtime,
        "model": row.model,
        "mode": row.mode,
        "schedule": list(row.schedules),
        "watch": row.watch,
    } for row in agent_view.repository_agents(REPO_ROOT)]


def last_run_index() -> dict[str, tuple[str | None, str | None, str]]:
    """(last_ok, last_error, last_status) for every identifier, in one pass.

    The log directory is read once. Asking per agent instead reads every
    agent's log to answer a question about one of them, and the table
    then does that once per row: on a repository with 21 agents and 50 MB
    of history that is a gigabyte of parsing per refresh, which blocks
    the event loop long enough for the browser to lose the websocket.
    """
    index: dict[str, tuple[str | None, str | None, str]] = {}
    for entry in obs.load(obs.files(_require_repo_path(LOGS_DIR))):
        if entry.get("phase") != "done":
            continue
        identifier = entry.get("agent_name")
        if not isinstance(identifier, str):
            continue
        last_ok, last_err, _ = index.get(identifier, (None, None, ""))
        status = str(entry.get("status", "")).lower()
        if status == "ok":
            last_ok = entry.get("ts")
        elif status == "error":
            last_err = entry.get("ts")
        index[identifier] = (last_ok, last_err, status)
    return index


def last_runs(identifier: str,
              index: dict[str, tuple[str | None, str | None, str]] | None = None
              ) -> tuple[str, str, str]:
    """(last_ok, last_error, last_status) from the agent log.

    last_status is the status of the most recent `done` entry ("ok",
    "error", "skipped", or "" when the log has no completed runs). It
    drives the health colour the same way the DASHBOARD.md "OK" column
    does: an agent whose last run errored is unhealthy.
    """
    if index is None:
        index = last_run_index()
    last_ok, last_err, last_status = index.get(identifier, (None, None, ""))
    now = datetime.now(timezone.utc)
    return (_ago(last_ok, now), _ago(last_err, now), last_status)


# Agency/Copilot runs report "AI Credits"; Claude runs report dollars
# directly. The repo's billing note fixes the conversion at 1 credit = $0.01
# (see .agents/agents-live.md), so both can be summed into one dollar
# figure.
_CREDIT_TO_USD = 0.01


def agent_cost(name: str) -> tuple[str, str]:
    """(cost_24h, cost_7d) in dollars from the agent log.

    Sums each run's cost over the trailing 24 hours and the trailing 7
    days. Returns ("-", "-") when the log has no cost-bearing runs in the
    7-day window (e.g. handler-only agents or agents that have not run
    recently); an agent that ran in the last week but not the last day
    shows "$0.00" for the 24h figure.
    """
    log_file = _require_repo_path(LOGS_DIR) / f"{name}.log"
    if not log_file.is_file():
        return ("-", "-")
    now = datetime.now(timezone.utc)
    day_cutoff = now - timedelta(days=1)
    week_cutoff = now - timedelta(days=7)
    day_total = 0.0
    week_total = 0.0
    found = False
    for line in log_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or '"cost_usd"' not in line and '"credits"' not in line:
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        ts = entry.get("ts")
        try:
            dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        except (ValueError, TypeError):
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        if dt < week_cutoff:
            continue
        usd = _entry_cost_usd(entry)
        if usd is None:
            continue
        week_total += usd
        if dt >= day_cutoff:
            day_total += usd
        found = True
    if not found:
        return ("-", "-")
    return (f"${day_total:.2f}", f"${week_total:.2f}")


def _running_version() -> str:
    return AGENTS_LIVE_VERSION


def _structured_log_snapshot(agent_names: set[str]) -> tuple[dict[str, int], dict[str, str]]:
    """Return trailing-hour errors and latest reported models via qlog."""
    if (SCRIPTS_DIR / "__init__.py").is_file():
        if str(SCRIPTS_DIR) not in sys.path:
            sys.path.insert(0, str(SCRIPTS_DIR))
        from agents_live.obs import qlog as structured_qlog
    else:
        import qlog as structured_qlog

    logs_dir = _require_repo_path(LOGS_DIR)
    if not any(logs_dir.glob("*.log")):
        return {}, {}
    connection = structured_qlog.duckdb.connect(":memory:")
    try:
        structured_qlog.build_view(connection, [str(logs_dir / "*.log")],
                                   archives=logs_dir / "archive")
        columns = {
            row[0] for row in connection.sql("DESCRIBE log").fetchall()
        }
        if "run_id" in columns and "event_id" in columns:
            event_identity = "CASE WHEN run_id IS NULL THEN event_id ELSE run_id END"
        elif "event_id" in columns:
            event_identity = "event_id"
        else:
            event_identity = "concat(_src, CAST(ts AS VARCHAR))"
        error_rows = connection.sql(
            "SELECT agent_name, count(*) FROM ("
            "SELECT agent_name, phase, status, level, message "
            "FROM log WHERE ts >= now() - INTERVAL 1 HOUR "
            "AND (level = 'error' OR status = 'error') "
            "QUALIFY row_number() OVER (PARTITION BY "
            f"{event_identity}, "
            "agent_name, phase, status, level, message ORDER BY ts) = 1"
            ") errors "
            "GROUP BY agent_name ORDER BY agent_name NULLS LAST"
        ).fetchall()
        model_rows = []
        if "model" in columns:
            model_rows = connection.sql(
                "SELECT agent_name, model FROM log "
                "WHERE agent_name IS NOT NULL AND model IS NOT NULL "
                "QUALIFY row_number() OVER ("
                "PARTITION BY agent_name ORDER BY ts DESC) = 1"
            ).fetchall()
    except (OSError, structured_qlog.duckdb.Error):
        return {}, {}
    finally:
        connection.close()

    errors: dict[str, int] = {}
    framework_errors = 0
    for raw_name, count in error_rows:
        name = str(raw_name or "")
        if name in agent_names:
            errors[name] = int(count)
        else:
            framework_errors += int(count)
    if framework_errors:
        errors["framework"] = framework_errors
    models = {
        str(name): str(model)
        for name, model in model_rows
        if name and model
    }
    return errors, models


def _refresh_summary() -> str:
    names = {agent["name"] for agent in collect_agents()}
    errors, models = _structured_log_snapshot(names)
    STATE["models"] = models
    error_text = ", ".join(
        f"{name} {count}" for name, count in errors.items()) or "none"
    local_now = datetime.now().astimezone()
    timestamp = local_now.strftime("%b %d, %Y %I:%M:%S %p %Z").replace(" 0", " ")
    return (
        f"Agents Live {_running_version()} | errors in last hour: "
        f"{error_text} | {timestamp}"
    )


def _entry_cost_usd(entry: dict) -> float | None:
    """Dollar cost of a single run entry, or None when it carries no cost."""
    cost = entry.get("cost_usd")
    if cost is not None:
        try:
            return float(cost)
        except (ValueError, TypeError):
            return None
    credits = entry.get("credits")
    if credits is not None:
        try:
            return float(credits) * _CREDIT_TO_USD
        except (ValueError, TypeError):
            return None
    return None


def _ago(ts: str | None, now: datetime) -> str:
    if not ts:
        return "-"
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return "?"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    total = max(0, int((now - dt).total_seconds()))
    if total < 60:
        return f"{total}s"
    mins = total // 60
    if mins < 60:
        return f"{mins}m"
    hours = mins // 60
    if hours < 24:
        return f"{hours}h"
    return f"{hours // 24}d"


def _is_local(agent: dict) -> bool:
    """True when this runtime already owns (or shares) the agent."""
    owner = agent.get("owner")
    is_owner = agent.get("isOwner")
    if is_owner is not None:
        return bool(is_owner)
    return owner is None or ownership.owns(owner)


def trigger_summary(agent: dict) -> str:
    parts: list[str] = []
    sched = agent.get("schedule")
    if isinstance(sched, list):
        parts += [f"cron {s}" for s in sched]
    elif sched:
        parts.append(f"cron {sched}")
    watch = agent.get("watch")
    if watch:
        parts.append(f"watch {watch}")
    return "  |  ".join(parts) or "-"


# --- Actions ------------------------------------------------------------

DASHBOARD_LOG = LOGS_DIR / "dashboard.jsonl" if LOGS_DIR else None
DASHBOARD_TRANSCRIPT = LOGS_DIR / "dashboard-transcript.log" if LOGS_DIR else None


def _command_argv(command: str, args: list[str]) -> list[str]:
    """Invoke the public CLI from the environment that provides the package.

    Not ``sys.executable``: this script runs under ``uv run --script``,
    whose environment holds NiceGUI and no agents-live (#288).
    """
    return [*repos.cli_base(), command, *args]


def _run_script(command: str, args: list[str],
                *, timeout: float | None = None) -> tuple[int, str]:
    """Run a lifecycle script with the given args; return (exit_code, output).

    ``timeout`` caps slow checks (e.g. the health-check worker, which runs
    the framework smoketest) so the dashboard can never spin forever; a
    timed-out run reports exit 124 with whatever output was captured.
    """
    try:
        proc = subprocess.run(
            _command_argv(command, args),
            cwd=_require_repo_path(REPO_ROOT),
            capture_output=True,
            **hostruntime.CHILD_TEXT,
            timeout=timeout,
            # Never hand children the dashboard's tty: a child that
            # prompts (ownership takeover) would block forever with its
            # question swallowed into the captured pipe.
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired as exc:
        captured = (exc.stdout or "") + (exc.stderr or "")
        if isinstance(captured, bytes):
            captured = captured.decode("utf-8", "replace")
        return 124, (captured.strip() + f"\n[dashboard] timed out after {timeout:.0f}s").strip()
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def _log_action(label: str, command: str, args: list[str], code: int,
                out: str, *, agent_name: str | None) -> None:
    """Persist a dashboard action: a JSONL event plus a full transcript.

    `dashboard.log` is the structured record (qlog/timeline-readable);
    `dashboard-transcript.log` keeps the complete, untruncated stdout+
    stderr so a failed Activate/Run can be reviewed after the fact.
    """
    obs.record(_require_repo_path(DASHBOARD_LOG), obs.create(
        "dashboard-action",
        "success" if code == 0 else "failed",
        repository=str(_require_repo_path(REPO_ROOT)),
        agent=agent_name or "dashboard",
        run_id=str(time.time_ns()),
        origin="dashboard",
        category=None if code == 0 else "command_failed",
        message=f"{label}: {command} {' '.join(args)}",
    ))
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    cmd = " ".join([command, *args])
    header = f"\n===== {ts} {label}: {cmd} (exit {code}) =====\n"
    transcript = _require_repo_path(DASHBOARD_TRANSCRIPT)
    try:
        transcript.parent.mkdir(parents=True, exist_ok=True)
        with transcript.open("a", encoding="utf-8") as handle:
            handle.write(header)
            if out:
                handle.write(out if out.endswith("\n") else out + "\n")
    except OSError:
        pass


def _safe_ui(func, *args, **kwargs):
    """Best-effort UI update.

    NiceGUI raises ``RuntimeError`` when UI is touched after the client
    has disconnected (tab closed/refreshed mid-action). Swallow that so a
    background action still completes and its outcome is logged. Returns
    the wrapped call's result, or ``None`` if the client was gone.
    """
    try:
        return func(*args, **kwargs)
    except RuntimeError:
        return None


class _ActionRequest:
    def __init__(self, label: str, script: str, args: list[str],
                 agent_name: str | None, timeout: float | None,
                 future: asyncio.Future[int]) -> None:
        self.label = label
        self.script = script
        self.args = args
        self.agent_name = agent_name
        self.timeout = timeout
        self.future = future
        self.key = (self.script, tuple(self.args))

    @property
    def description(self) -> str:
        return f"{self.label} {self.agent_name or ' '.join(self.args)}".strip()


_ACTION_QUEUE: deque[_ActionRequest] = deque()
_PENDING_ACTIONS: dict[tuple[str, tuple[str, ...]], _ActionRequest] = {}
_ACTION_WORKER: asyncio.Task[None] | None = None
_ACTION_RUNNING = False


def _push_log(message: str) -> None:
    timestamp = datetime.now().astimezone().strftime("%H:%M:%S %Z")
    _safe_ui(output_log.push, f"[{timestamp}] {message}")


async def _execute_action(request: _ActionRequest) -> int:
    target = request.agent_name or " ".join(request.args)
    # Creating the notification can itself raise if the client already
    # disconnected, so guard it like every other UI touch below.
    note = _safe_ui(ui.notification, f"{request.label}: {target} ...",
                    spinner=True, timeout=None)
    started = time.monotonic()
    _push_log(f"started: {request.description}")
    try:
        code, out = await ng_run.io_bound(
            _run_script, request.script, request.args, timeout=request.timeout)
    finally:
        if note is not None:
            _safe_ui(note.dismiss)
    ok = code == 0
    # Persist the outcome first so a disconnected client never loses the record.
    _log_action(
        request.label, request.script, request.args, code, out,
        agent_name=request.agent_name)
    _safe_ui(
        ui.notify,
        f"{request.label} {target}: {'ok' if ok else f'failed (exit {code})'}",
        type="positive" if ok else "negative",
    )
    elapsed = time.monotonic() - started
    outcome = "completed" if ok else "failed"
    _push_log(
        f"{outcome}: {request.description} (exit {code}, {elapsed:.1f}s)")
    for line in out.splitlines():
        _safe_ui(output_log.push, f"    {line}")
    _safe_ui(_refresh_views)
    return code


async def _process_action_queue() -> None:
    global _ACTION_RUNNING, _ACTION_WORKER
    try:
        while _ACTION_QUEUE:
            request = _ACTION_QUEUE.popleft()
            _PENDING_ACTIONS.pop(request.key, None)
            _ACTION_RUNNING = True
            started = time.monotonic()
            try:
                code = await _execute_action(request)
            except Exception as exc:
                code = -1
                elapsed = time.monotonic() - started
                output = f"unexpected dashboard action error: {exc}"
                _log_action(
                    request.label, request.script, request.args, code, output,
                    agent_name=request.agent_name)
                _push_log(
                    f"failed: {request.description} "
                    f"(exit {code}, {elapsed:.1f}s): {exc}")
                _safe_ui(_refresh_views)
                if not request.future.done():
                    request.future.set_result(code)
            else:
                if not request.future.done():
                    request.future.set_result(code)
            finally:
                _ACTION_RUNNING = False
    finally:
        _ACTION_WORKER = None


async def do_action(label: str, script: str, args: list[str],
                    *, agent_name: str | None = None,
                    timeout: float | None = None) -> int:
    global _ACTION_WORKER
    key = (script, tuple(args))
    pending = _PENDING_ACTIONS.get(key)
    if pending is not None:
        _push_log(f"already queued: {pending.description}")
        return await asyncio.shield(pending.future)

    loop = asyncio.get_running_loop()
    request = _ActionRequest(
        label, script, list(args), agent_name, timeout, loop.create_future())
    if _ACTION_RUNNING or _ACTION_QUEUE:
        _push_log(f"queued: {request.description}")
    _ACTION_QUEUE.append(request)
    _PENDING_ACTIONS[request.key] = request
    if _ACTION_WORKER is None:
        _ACTION_WORKER = asyncio.create_task(_process_action_queue())
    return await asyncio.shield(request.future)


async def health_check() -> None:
    """Verify and report the full health picture for this host.

    Runs every check the system's health depends on, in order, and
    surfaces each result rather than only the prerequisites:

     1. `doctor` - environment readiness (gate: abort if a required
       prerequisite is missing, so the failure surfaces up front instead
       of as a cryptic mid-activation error).
     2. `start --all` - ensure every agent owned by this host (or `*`)
       with a trigger is actually registered and running.
     3. `smoketest` - run the framework's end-to-end validation.

    The header label then reflects the refreshed beacon (`system_health`),
    and a final notification summarises infrastructure + smoketest so the
    user sees the whole picture, not just the lifecycle scripts' exit
    codes.
    """
    if await do_action("Doctor", "doctor", []) != 0:
        _safe_ui(
            ui.notify,
            "Prerequisites failing - resolve the items above before activating.",
            type="warning", timeout=8000,
        )
        return

    await do_action("Start", "start", ["--all"])
    await do_action("Smoketest", "smoketest", [], timeout=WORKER_TIMEOUT)
    # Summarise the refreshed beacon so the user sees infra + smoketest,
    # not just exit codes. system_health reads the host health.ok beacon.
    h = system_health()
    severity = {"ok": "positive", "degraded": "warning", "down": "negative"}
    _safe_ui(
        ui.notify, h["tip"],
        type=severity.get(h["level"], "negative"),
        timeout=12000, multi_line=True,
    )


async def pause_all(names: list[str]) -> None:
    if not names:
        _safe_ui(ui.notify, "Nothing running to stop", type="info")
        return
    for name in names:
        await do_action("Stop", "stop", ["--name", name], agent_name=name)


# --- UI -----------------------------------------------------------------

def agent_rows() -> list[dict]:
    """Enriched row model shared by the agent table and the health strip."""
    rows: list[dict] = []
    host = ownership.current_label()
    runs = last_run_index()
    for agent in collect_agents():
        name = agent["name"]
        identifier = agent["identifier"]
        state = re.sub(r"\s*\(pid \d+\)", "", agent.get("state", "?"))
        owner_value = agent.get("owner")
        ownership_available = agent.get("ownershipAvailable", True)
        owner = (
            ownership.display_owner(owner_value) if owner_value else
            "-" if ownership_available else "Unavailable"
        )
        ok_ago, err_ago, last_status = last_runs(identifier, runs)
        # A failed last run only makes this host's view unhealthy while
        # the agent is still registered here. "stopped" means no trigger
        # is registered on this host - commonly an agent owned by
        # another host, whose stale error belongs to that host's view.
        # "unknown" (scheduler unreadable) keeps the flag rather than
        # hiding a real failure (issue #176).
        unhealthy = last_status == "error" and state != "stopped"
        local = _is_local(agent)
        runtime = agent.get("runtime") or "agency copilot"
        agent_display = runtime if runtime != "none" else "handler"
        cost_day, cost_week = (agent_cost(name) if runtime != "none" else ("-", "-"))
        model = _agent_model(agent, STATE["models"])
        can_pause = local and state == "started"
        can_activate = local and state == "stopped"
        can_claim = ownership_available and not local
        unavailable_tip = "Ownership registry unavailable"
        rows.append({
            "name": name,
            "identifier": identifier,
            "agent": agent_display,
            "trigger": trigger_summary(agent),
            "state": state,
            "owner": owner,
            "model": model,
            "last_ok": ok_ago,
            "last_err": err_ago,
            "cost_day": cost_day,
            "cost_week": cost_week,
            "unhealthy": unhealthy,
            "local": local,
            "can_pause": can_pause,
            "can_activate": can_activate,
            "can_claim": can_claim,
            "run_tip": "Run this agent once now",
            "activate_tip": (
                "Register this host's cron/watcher for this agent"
                if can_activate else
                (unavailable_tip if not ownership_available else
                 "Already active" if local else
                 f"Owned by another host - use Claim to move it onto {host}")),
            "pause_tip": (
                "Stop this host's cron/watcher (config preserved)"
                if can_pause else
                unavailable_tip if not ownership_available else
                "Not running on this host"),
            "claim_tip": (unavailable_tip if not ownership_available else
                          "Already local" if local else
                          f"Claim onto {host} (transfer ownership + register trigger)"),
        })
    return rows


@app.get("/api/agents")
def api_agents() -> dict:
    """Machine-readable snapshot of the rows the agent table renders.

    The page itself draws over a websocket, so an HTTP GET of ``/``
    proves only that a port was bound - the agent names never appear in
    the served HTML. Checks that run outside a browser, the framework
    smoketest among them, read this instead, which makes "the dashboard
    started, resolved a project, and can see its agents" one assertion.
    """
    return {
        "host": ownership.current_label(),
        "repo": str(REPO_ROOT) if REPO_ROOT is not None else None,
        "agents": agent_rows() if REPO_ROOT is not None else [],
    }


def _filtered_agent_rows(rows: list[dict], filters: dict) -> list[dict]:
    name_filter = str(filters.get("name", "")).casefold().strip()
    return [
        row for row in rows
        if (not name_filter or name_filter in row["name"].casefold())
        and (filters.get("state", "All") == "All"
             or row["state"] == filters["state"])
        and (filters.get("owner", "All") == "All"
             or row["owner"] == filters["owner"])
        and (filters.get("runtime", "All") == "All"
             or row["agent"] == filters["runtime"])
        and (not filters.get("failing") or row["unhealthy"])
    ]


def _cost_totals(rows: list[dict]) -> tuple[str, str]:
    def total(field: str) -> str:
        values = [
            float(row[field].removeprefix("$"))
            for row in rows
            if row[field] != "-"
        ]
        return f"${sum(values):.2f}"

    return total("cost_day"), total("cost_week")


def _agent_model(agent: dict, reported_models: dict[str, str]) -> str:
    runtime = agent.get("runtime") or "agency copilot"
    if runtime == "none":
        return "-"
    return reported_models.get(agent["name"]) or agent.get("model") or "default"


def system_health() -> dict:
    """Real infrastructure health, read from the host health beacon.

    Built-in automatic maintenance writes the host
    `health.ok` beacon (under the user-level state home) only after
    confirming every intended watcher is alive (self-healing any that
    died), so a *fresh* beacon means the infrastructure is genuinely up.
    A missing or stale beacon means the loop has not confirmed health
    within the hour. The nested smoketest verdict is surfaced as a
    distinct *degraded* state: the framework end-to-end test is failing
    even though watcher/cron infrastructure is healthy.

    Returns a dict with ``level`` ("ok" | "degraded" | "down"), a short
    ``text`` label for the header, and a longer ``tip`` tooltip.
    """
    now = datetime.now(timezone.utc)
    health_ok_path = HEALTH_OK_PATH
    if not health_ok_path.is_file():
        return {"level": "down", "text": "unhealthy: no beacon",
                  "tip": "the host health.ok beacon is missing. Run "
                      "`agents-live doctor --repair`."}
    mtime = datetime.fromtimestamp(health_ok_path.stat().st_mtime, timezone.utc)
    age_min = (now - mtime).total_seconds() / 60
    ago = _ago(mtime.isoformat(), now)
    try:
        data = json.loads(health_ok_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    if age_min > HEALTH_STALE_MINUTES:
        return {"level": "down", "text": f"unhealthy: beacon stale {ago}",
                "tip": f"health.ok last written {ago} (expected hourly). The "
                       "health-check worker is not confirming infrastructure "
                       "health - run the health check or check its logs."}
    watchers = data.get("watchers")
    cron = data.get("cron")
    counts = (f"{watchers} watchers / {cron} cron"
              if watchers is not None and cron is not None else "infrastructure")
    smoke = data.get("smoketest")
    smoke = smoke if isinstance(smoke, dict) else {}
    smoke_status = str(smoke.get("status", "")).lower()
    if smoke_status == "fail":
        reason = str(smoke.get("reason", "")).strip() or "no reason recorded"
        return {"level": "degraded",
                "text": f"degraded: smoketest failing {ago}",
                "tip": f"Infrastructure healthy ({counts}); framework "
                       f"smoketest is FAILING: {reason}"}
    smoke_note = f"smoketest {smoke_status}" if smoke_status else "smoketest not run"
    return {"level": "ok", "text": f"healthy {ago}",
            "tip": f"Infrastructure healthy ({counts}); {smoke_note}; "
                   f"beacon written {ago}"}


# Row action handlers: the q-table action slots emit (event, row) pairs.

async def _run_row(event) -> None:
    identifier = event.args["identifier"]
    await do_action("Run", "run", ["--name", identifier], agent_name=identifier)


async def _activate_row(event) -> None:
    identifier = event.args["identifier"]
    await do_action("Start", "start", ["--name", identifier], agent_name=identifier)


async def _pause_row(event) -> None:
    identifier = event.args["identifier"]
    await do_action("Stop", "stop", ["--name", identifier], agent_name=identifier)


async def _claim_row(event) -> None:
    name = event.args["name"]
    identifier = event.args["identifier"]
    ownership.set_owner(name, ownership.current_owner_id())
    await do_action("Start", "start", ["--name", identifier],
                    agent_name=identifier)


_AGENT_COLUMNS = [
    {"name": "name", "label": "Agent", "field": "name", "align": "left", "sortable": True},
    {"name": "state", "label": "State", "field": "state", "align": "left", "sortable": True},
    {"name": "actions", "label": "Actions", "field": "actions", "align": "left"},
    {"name": "owner", "label": "Owner", "field": "owner", "align": "left", "sortable": True},
    {"name": "agent", "label": "Runtime", "field": "agent", "align": "left", "sortable": True},
    {"name": "model", "label": "Model", "field": "model", "align": "left", "sortable": True},
    {"name": "trigger", "label": "Trigger", "field": "trigger", "align": "left",
     "style": "width: 100%; max-width: 0", "headerStyle": "width: 100%"},
    {"name": "last_ok", "label": "Last OK", "field": "last_ok", "align": "right",
     "style": "width: 64px", "headerStyle": "width: 64px"},
    {"name": "last_err", "label": "Last Err", "field": "last_err", "align": "right",
     "style": "width: 64px", "headerStyle": "width: 64px"},
    {"name": "cost_day", "label": "$/24h", "field": "cost_day", "align": "right",
     "sortable": True, "style": "width: 64px", "headerStyle": "width: 64px"},
    {"name": "cost_week", "label": "$/1w", "field": "cost_week", "align": "right",
     "sortable": True, "style": "width: 64px", "headerStyle": "width: 64px"},
]


@ui.refreshable
def agent_grid() -> None:
    STATE["last_refresh"] = datetime.now(timezone.utc)
    rows = agent_rows()
    filters = STATE["filters"]
    filtered_rows = _filtered_agent_rows(rows, filters)

    def apply_filters() -> None:
        table.rows = _filtered_agent_rows(rows, filters)
        table.update()
        day, week = _cost_totals(table.rows)
        totals.text = f"Totals: {day} / 24h   {week} / 1w"

    def set_filter(key: str, value) -> None:
        filters[key] = value
        apply_filters()

    with ui.row().classes("w-full items-center gap-2 agent-filters"):
        ui.input(
            "Search agent", value=filters["name"],
            on_change=lambda event: set_filter("name", event.value),
        ).props("dense outlined clearable").classes("min-w-48")
        ui.select(
            ["All", *sorted({row["state"] for row in rows})],
            value=filters["state"], label="State",
            on_change=lambda event: set_filter("state", event.value),
        ).props("dense outlined options-dense")
        ui.select(
            ["All", *sorted({row["owner"] for row in rows})],
            value=filters["owner"], label="Owner",
            on_change=lambda event: set_filter("owner", event.value),
        ).props("dense outlined options-dense")
        ui.select(
            ["All", *sorted({row["agent"] for row in rows})],
            value=filters["runtime"], label="Runtime",
            on_change=lambda event: set_filter("runtime", event.value),
        ).props("dense outlined options-dense")
        ui.checkbox(
            "Failing", value=filters["failing"],
            on_change=lambda event: set_filter("failing", event.value),
        ).props("dense")
    with ui.scroll_area().classes("w-full grow min-h-0 agent-table-scroll"):
        table = ui.table(
            columns=_AGENT_COLUMNS, rows=filtered_rows, row_key="name",
            pagination={"rowsPerPage": 0},
        ).classes("w-full").props("flat dense hide-bottom separator=none")
    table.add_slot("body-cell-name", '''
        <q-td :props="props">
          <div style="white-space:nowrap"
               :title="props.row.unhealthy ? props.row.name + ' - last run errored' : props.row.name"
               :class="props.row.unhealthy ? 'text-red text-weight-medium' : ''">{{ props.row.name }}</div>
        </q-td>
    ''')
    table.add_slot("body-cell-owner", '''
        <q-td :props="props">
          <div style="white-space:nowrap"
               :class="props.row.local ? '' : 'text-grey-6'">{{ props.row.owner }}</div>
        </q-td>
    ''')
    table.add_slot(
        "body-cell-agent",
        '<q-td :props="props"><div style="white-space:nowrap">'
        '{{ props.row.agent }}</div></q-td>',
    )
    table.add_slot(
        "body-cell-model",
        '<q-td :props="props"><div style="white-space:nowrap">'
        '{{ props.row.model }}</div></q-td>',
    )
    table.add_slot("body-cell-trigger", '''
        <q-td :props="props">
          <div class="ellipsis" :title="props.row.trigger">{{ props.row.trigger }}</div>
        </q-td>
    ''')
    table.add_slot("body-cell-state", '''
        <q-td :props="props">
          <span :class="props.row.unhealthy ? 'text-red'
                   : (props.row.state.startsWith('active') ? 'text-green'
                   : props.row.state === 'partial' ? 'text-orange' : 'text-grey-6')"
                   >{{ props.row.state }}</span>
        </q-td>
    ''')
    table.add_slot("header-cell-actions", '''
        <q-th :props="props" class="text-left">{{ props.col.label }}</q-th>
    ''')
    table.add_slot("body-cell-actions", '''
        <q-td :props="props" class="text-left">
          <q-btn flat dense round size="xs" color="primary" icon="play_arrow"
                 :title="props.row.run_tip"
                 @click="() => $parent.$emit('run', props.row)" />
          <q-btn flat dense round size="xs" icon="power_settings_new"
                 :color="props.row.can_activate ? 'primary' : 'grey-7'"
                 :disable="!props.row.can_activate"
                 :title="props.row.activate_tip"
                 @click="() => $parent.$emit('activate', props.row)" />
          <q-btn flat dense round size="xs" icon="stop"
                 :color="props.row.can_pause ? 'primary' : 'grey-7'"
                 :disable="!props.row.can_pause"
                 :title="props.row.pause_tip"
                 @click="() => $parent.$emit('pause', props.row)" />
          <q-btn flat dense round size="xs" icon="download"
                 :color="props.row.can_claim ? 'primary' : 'grey-7'"
                 :disable="!props.row.can_claim"
                 :title="props.row.claim_tip"
                 @click="() => $parent.$emit('claim', props.row)" />
        </q-td>
    ''')
    table.on("run", _run_row)
    table.on("activate", _activate_row)
    table.on("pause", _pause_row)
    table.on("claim", _claim_row)
    day_total, week_total = _cost_totals(filtered_rows)
    totals = ui.label(
        f"Totals: {day_total} / 24h   {week_total} / 1w"
    ).classes("w-full text-right text-xs text-gray-500 pr-4")


@ui.refreshable
def header_actions() -> None:
    rows = agent_rows()
    with ui.row().classes("items-center gap-3 no-wrap"):
        h = system_health()
        color = {"ok": "text-gray-500",
                 "degraded": "text-orange-500",
                 "down": "text-red-400"}.get(h["level"], "text-red-400")
        ui.label(h["text"]).classes("text-sm " + color).tooltip(h["tip"])
        ui.button(
            "Run health check", icon="health_and_safety", on_click=health_check
        ).props("dense color=primary unelevated no-caps").classes("hdr-btn").style(
            "border-radius:6px;padding:3px 10px"
        ).tooltip(
            "Verify everything needed on this host: prerequisites, activate "
            "all owned agents, then run the health-check worker (watchers, "
            "cron, smoketest) and refresh the health beacon."
        )
        running = [r["name"] for r in rows if r["can_pause"]]
        ui.button(
            "Stop all", icon="stop",
            on_click=lambda names=running: pause_all(names),
        ).props("dense unelevated no-caps color=grey-7 text-color=white").classes(
            "hdr-btn"
        ).style("border-radius:6px;padding:3px 10px").set_enabled(bool(running))


def _refresh_views() -> None:
    # One pass for the whole render: the summary, the table, and the
    # header each ask every agent for its state, and without this they
    # would each read the host's process table and task folder again.
    with hostruntime.enumeration_pass():
        summary = _refresh_summary()
        agent_grid.refresh()
        header_actions.refresh()
    _safe_ui(output_log.push, summary)


def _timer_after_first_interval(interval: float, callback) -> None:
    """Register a client timer now without invoking its callback yet."""
    first_tick = True

    def invoke() -> None:
        nonlocal first_tick
        if first_tick:
            first_tick = False
            return
        callback()

    ui.timer(interval, invoke)


def build_page() -> None:
    with hostruntime.enumeration_pass():
        _build_page()


def _build_page() -> None:
    ui.dark_mode().auto()
    if REPO_ROOT is None:
        _build_no_project_page()
        return
    startup_summary = _refresh_summary()
    ui.add_css(
        ".q-table tbody tr{transition:background-color .08s}"
        ".q-table tbody tr:hover{background-color:rgba(0,0,0,0.045)}"
        ".body--dark .q-table tbody tr:hover{background-color:rgba(255,255,255,0.07)}"
        ".hdr-btn{min-height:0}"
        ".hdr-btn .q-btn__content{min-height:0;white-space:nowrap}"
        ".hdr-btn .q-icon{font-size:0.95em}"
        ".hdr-btn .q-btn__content .q-icon{margin-right:5px}"
        ".nicegui-content{height:100vh;overflow:hidden;display:flex;flex-direction:column}"
        ".dashboard-body{display:grid;grid-template-rows:minmax(12rem,1fr) auto "
        "minmax(15rem,.7fr);min-height:0}"
        ".agent-panel{overflow:hidden;display:flex;flex-direction:column}"
        ".agent-table-scroll{min-height:0}"
        ".agent-filters .q-field{min-width:8rem}"
    )
    host = ownership.current_label()

    with ui.row().classes("w-full items-center justify-between gap-x-4 gap-y-2"):
        with ui.row().classes("items-center gap-4 no-wrap"):
            ui.label("Agents Live").classes("text-xl font-semibold")
            ui.label(host).classes("text-sm text-gray-500")
            ui.label(_scope_label()).classes("text-sm text-gray-500")
        with ui.row().classes("items-center gap-3 no-wrap"):
            header_actions()
            refresh_age = ui.label().classes("text-sm text-gray-500")
            ui.button(icon="refresh", on_click=_refresh_views).props("flat round dense")

    def tick_age() -> None:
        ago = _ago(STATE["last_refresh"].isoformat(), datetime.now(timezone.utc))
        refresh_age.text = f"refreshed {ago}"

    tick_age()
    ui.timer(1.0, tick_age)

    with ui.element("div").classes("dashboard-body w-full grow min-h-0"):
        with ui.card().classes("agent-panel w-full min-h-0"):
            agent_grid()

        ui.label("Log").classes("text-sm text-gray-500 mt-2")
        global output_log
        output_log = ui.log(max_lines=300).classes(
            "w-full h-full font-mono text-xs"
        )
        output_log.push(startup_summary)

    _timer_after_first_interval(600.0, _refresh_views)


def _build_no_project_page() -> None:
    """Header plus an explanation, when no project root resolves.

    The agent panel reads agent configs and logs through the project
    root; with none there is nothing to enumerate, so the page states
    that rather than rendering a complete but empty dashboard.
    """
    with ui.row().classes("w-full items-center gap-4"):
        ui.label("Agents Live").classes("text-xl font-semibold")
        ui.label(ownership.current_label()).classes("text-sm text-gray-500")
        ui.label(_scope_label()).classes("text-sm text-gray-500")
    with ui.card().classes("w-full"):
        ui.label("No project selected").classes("text-base font-medium")
        ui.label(NO_PROJECT_HINT).classes("text-sm text-gray-500")
        if REPO_ERROR:
            ui.label(REPO_ERROR).classes("text-xs text-gray-500")


def _all_repos_rows() -> list[dict]:
    payload = repos.collect_status()
    rows = []
    for item in payload["repos"]:
        if "error" in item:
            rows.append({
                "identity": f"{item['name']}/error",
                "repo": item["name"], "name": "-", "state": "error",
                "runtime": "-", "detail": item["error"],
            })
            continue
        for agent in item["result"].get("agents", []):
            rows.append({
                "identity": agent["name"],
                "repo": item["name"], "name": agent["name"],
                "state": agent.get("state", "?"),
                "runtime": agent.get("runtime", "?"), "detail": item["path"],
            })
    return rows


def build_all_repos_page() -> None:
    """Read-only registered-repository view; no lifecycle actions are exposed."""
    ui.dark_mode().auto()
    rows = _all_repos_rows()
    selection = {"value": "All"}

    with ui.row().classes("w-full items-center gap-4"):
        ui.label("Agents Live").classes("text-xl font-semibold")
        ui.label(ownership.current_label()).classes("text-sm text-gray-500")
        ui.label("All registered repositories (read only)").classes(
            "text-sm text-gray-500")
    names = sorted({row["repo"] for row in rows})
    table = ui.table(
        columns=[
            {"name": "repo", "label": "Repository", "field": "repo", "sortable": True},
            {"name": "name", "label": "Agent", "field": "name", "sortable": True},
            {"name": "state", "label": "State", "field": "state", "sortable": True},
            {"name": "runtime", "label": "Runtime", "field": "runtime"},
            {"name": "detail", "label": "Repository path / error", "field": "detail"},
        ],
        rows=rows, row_key="identity",
    ).classes("w-full")

    def apply_filter() -> None:
        selected = selection["value"]
        table.rows = rows if selected == "All" else [
            row for row in rows if row["repo"] == selected]
        table.update()

    def select_repo(event) -> None:
        selection["value"] = event.value
        apply_filter()

    def refresh() -> None:
        nonlocal rows
        rows = _all_repos_rows()
        apply_filter()

    with ui.row().classes("items-center gap-4"):
        ui.select(["All", *names], value="All", label="Repository",
                  on_change=select_repo)
        ui.button("Refresh", on_click=refresh)
    # Same cadence as the single-repo page: the view tracks reality
    # instead of freezing at process start.
    ui.timer(600.0, refresh)
    ui.label(
        "Select one repository with `agents-live --repo NAME dashboard` "
        "to enable actions."
    ).classes("text-sm text-gray-500")


PORT_PROBE_TIMEOUT_S = 0.5
DASHBOARD_HOST = "127.0.0.1"


def port_conflict(host: str, port: int) -> str | None:
    """Describe what already holds ``host:port``, or None if it is free.

    Asked before the server starts, because NiceGUI prints its readiness
    line before uvicorn attempts the bind: without this, a start that
    cannot possibly work announces success first and then fails with a
    bare errno (#175).

    Two different questions, and both have to be asked. Talking to the
    port finds a server that is already answering there, which a bind
    does not: Windows lets a second listener bind an address another
    process is serving unless that process asked for exclusive use, so
    two servers coexist and the first one wins every connection. That is
    how a local dashboard ended up invisible behind a WSL relay while
    reporting no problem (#174). Binding, with exclusive use where the
    platform offers it, then finds a holder that is not yet answering.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(PORT_PROBE_TIMEOUT_S)
        if probe.connect_ex((host, port)) == 0:
            return (f"another server is already answering on {host}:{port}, "
                    f"and its connections would be served instead of this one")
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as binder:
        # SO_EXCLUSIVEADDRUSE is Windows-only and is what makes the bind
        # a real question there; elsewhere a plain bind already answers it.
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            binder.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        try:
            binder.bind((host, port))
        except OSError as exc:
            reason = exc.strerror or str(exc)
            return f"{host}:{port} is not available ({reason})"
    return None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--native", action="store_true", help="Open a desktop window")
    parser.add_argument(
        "--open",
        dest="open_browser",
        action="store_true",
        help="Auto-open a local browser (skip on WSL - open the URL manually)",
    )
    parser.add_argument(
        "--dev",
        action="store_true",
        help="Auto-restart when dashboard.py changes",
    )
    parser.add_argument("--port", type=int, default=8231)
    parser.add_argument(
        "--all-repos", action="store_true",
        help="Show a read-only view of all registered repositories")
    args = parser.parse_args()

    # Asked before anything is announced or built, and only by the
    # process that is going to start a server. NiceGUI re-executes this
    # script as __main__ to build the root page after the app has started;
    # that request must not mistake its own server for a conflict.
    # Under --dev the reloader imports this module as __mp_main__, and
    # the test harness uses a name of its own. Neither starts this server.
    if __name__ == "__main__" and not app.is_started:
        conflict = port_conflict(DASHBOARD_HOST, args.port)
        if conflict is not None:
            preflight.emit_failure(
                "dashboard",
                f"{conflict}; `agents-live dashboard list` shows what this "
                f"host started, `agents-live dashboard stop --port "
                f"{args.port}` stops it, or retry with --port <other>",
                code="port_unavailable")
            raise SystemExit(1)
        # Recorded by the launching process, not the server: under --dev
        # the reloader child holds the socket but comes and goes, while
        # this process owns the port for the whole run. Stopping it takes
        # the child with it, because termination covers descendants.
        dashboards.record(args.port, os.getpid(), REPO_ROOT)
        atexit.register(dashboards.forget, args.port, os.getpid())

    if args.all_repos:
        build_all_repos_page()
    else:
        build_page()
    app.on_exception(lambda exc: _safe_ui(ui.notify, f"error: {exc}", type="negative"))
    ui.run(
        host=DASHBOARD_HOST,
        port=args.port,
        title="Agents Live",
        native=args.native,
        show=args.open_browser,
        reload=args.dev,
        uvicorn_reload_dirs=str(SCRIPTS_DIR),
        uvicorn_reload_includes="dashboard.py",
    )


if __name__ in {"__main__", "__mp_main__"}:
    main()
