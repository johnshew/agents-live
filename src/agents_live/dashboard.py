#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["duckdb", "nicegui>=2.0", "PyYAML", "pywebview"]
# ///
"""Interactive agents-live control panel (single host).

An HTML control surface over the agents-live lifecycle scripts. It
lists every agent with its live state and ownership, and exposes per-agent
Run / Activate / Pause / Claim buttons that shell the existing scripts
(`run.py`, `activate.py`, `stop.py`), plus a top-bar Health check
that verifies prerequisites (`doctor.py`), activates everything owned by
this host (`activate.py --all`), then runs the built-in `agents-live
health-check` loop (watchers, cron, framework smoketest) and refreshes
the health beacon. The header health label reflects the real host
beacon (`health.ok` under the user-level state home): healthy, degraded
(infra up but smoketest failing), or unhealthy (beacon missing or
stale). State and last-run times are read straight from this repo's log
directory in the state home and the agent configs - no new data layer.
Every action is logged to `dashboard.log` (JSONL) there, with the full
transcript in `dashboard-transcript.log`.

Scope: this build acts on the *local* host. Health check activates
agents owned by this host or `*`; per-agent Claim transfers an agent's
ownership to this host (`activate.py --name X --transfer-to <host>`)
and registers its cron/watcher. Bulk cross-host reassignment to *other*
hosts is still deferred.

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
import json
import re
import subprocess
import sys
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent

# Dual-layout import: packaged, the sibling modules belong to the
# agents_live package and must be imported through it so their relative
# imports resolve; flat, they are top-level scripts beside this file.
# __init__.py is the layout discriminator - the flat scripts dir has none.
if (SCRIPTS_DIR / "__init__.py").is_file():
    if str(SCRIPTS_DIR.parent) not in sys.path:
        sys.path.insert(0, str(SCRIPTS_DIR.parent))
    from agents_live import __version__ as AGENTS_LIVE_VERSION  # noqa: E402
    from agents_live import cli_spec, headless, ownership, paths, repos  # noqa: E402
else:
    if str(SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPTS_DIR))
    import cli_spec  # noqa: E402
    import headless  # noqa: E402
    import ownership  # noqa: E402
    import paths  # noqa: E402
    import repos  # noqa: E402
    try:
        AGENTS_LIVE_VERSION = version("agents-live")
    except PackageNotFoundError:
        AGENTS_LIVE_VERSION = "unknown"
from nicegui import app, ui  # noqa: E402
from nicegui import run as ng_run  # noqa: E402

try:
    REPO_ROOT = headless.repo_root()
except ValueError:
    REPO_ROOT = None
LOGS_DIR = paths.repo_state_dir(REPO_ROOT) / "logs" if REPO_ROOT else None
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


# --- Data ---------------------------------------------------------------

def collect_agents() -> list[dict]:
    """Return agent details for every configured agent, sorted by name."""
    agents: list[dict] = []
    for name in headless.list_agents():
        try:
            agents.append(headless.agent_details(headless.load_agent_config(name)))
        except headless.AgentsLiveError:
            continue
    agents.sort(key=lambda agent: agent["name"])
    return agents


def last_runs(name: str) -> tuple[str, str, str]:
    """(last_ok, last_error, last_status) from the agent log.

    last_status is the status of the most recent `done` entry ("ok",
    "error", "skipped", or "" when the log has no completed runs). It
    drives the health colour the same way the DASHBOARD.md "OK" column
    does: an agent whose last run errored is unhealthy.
    """
    log_file = _require_repo_path(LOGS_DIR) / f"{name}.log"
    if not log_file.is_file():
        return ("-", "-", "")
    last_ok: str | None = None
    last_err: str | None = None
    last_status = ""
    for line in log_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if entry.get("phase") != "done":
            continue
        status = str(entry.get("status", "")).lower()
        last_status = status
        if status == "ok":
            last_ok = entry.get("ts")
        elif status == "error":
            last_err = entry.get("ts")
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
        except json.JSONDecodeError:
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
        from agents_live import qlog as structured_qlog
    else:
        import qlog as structured_qlog

    logs_dir = _require_repo_path(LOGS_DIR)
    if not any(logs_dir.glob("*.log")):
        return {}, {}
    connection = structured_qlog.duckdb.connect(":memory:")
    try:
        structured_qlog.build_view(connection, [str(logs_dir / "*.log")])
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


def _is_local(agent: dict, host: str) -> bool:
    """True when this host already owns (or shares) the agent."""
    owner = agent.get("owner") or "-"
    is_owner = agent.get("isOwner")
    return is_owner is None or owner in ("*",) or owner.lower() == host


def trigger_summary(agent: dict) -> str:
    parts: list[str] = []
    sched = agent.get("schedule")
    if isinstance(sched, list):
        parts += [f"cron {s}" for s in sched]
    elif sched:
        parts.append(f"cron {sched}")
    wp = agent.get("watchPath")
    if isinstance(wp, list):
        parts += [f"watch {p}" for p in wp]
    elif wp:
        parts.append(f"watch {wp}")
    return "  |  ".join(parts) or "-"


# --- Actions ------------------------------------------------------------

DASHBOARD_LOG = LOGS_DIR / "dashboard.log" if LOGS_DIR else None
DASHBOARD_TRANSCRIPT = LOGS_DIR / "dashboard-transcript.log" if LOGS_DIR else None


# Script file -> CLI subcommand, for packaged execution where the flat
# script files cannot be uv-run (their relative imports need the
# package). Derived from the command spec so a module rename or new
# action never needs a hand-edit here (verb-to-module wiring has one
# source of truth).
_CLI_SUBCOMMAND = {
    f"{command.module}.py": command.name
    for command in cli_spec.COMMANDS
    if command.dispatch == "in-process" and not command.hidden
}


def _script_argv(script: str, args: list[str]) -> list[str]:
    """argv for one lifecycle action, for either layout."""
    return headless.cli_invocation(
        _CLI_SUBCOMMAND[script], *args, flat_script=SCRIPTS_DIR / script)


def _run_script(script: str, args: list[str],
                *, timeout: float | None = None) -> tuple[int, str]:
    """Run a lifecycle script with the given args; return (exit_code, output).

    ``timeout`` caps slow checks (e.g. the health-check worker, which runs
    the framework smoketest) so the dashboard can never spin forever; a
    timed-out run reports exit 124 with whatever output was captured.
    """
    try:
        proc = subprocess.run(
            _script_argv(script, args),
            cwd=_require_repo_path(REPO_ROOT),
            capture_output=True,
            text=True,
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


def _log_action(label: str, script: str, args: list[str], code: int,
                out: str, *, agent_name: str | None) -> None:
    """Persist a dashboard action: a JSONL event plus a full transcript.

    `dashboard.log` is the structured record (qlog/timeline-readable);
    `dashboard-transcript.log` keeps the complete, untruncated stdout+
    stderr so a failed Activate/Run can be reviewed after the fact.
    """
    headless.log_event(
        _require_repo_path(DASHBOARD_LOG),
        level="info" if code == 0 else "error",
        phase="action",
        action=label,
        script=script,
        args=args,
        agent_name=agent_name,
        host=ownership.current_host(),
        exit_code=code,
        status="ok" if code == 0 else "error",
        output=out,
    )
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    cmd = " ".join([script, *args])
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

    1. `doctor.py` - environment readiness (gate: abort if a required
       prerequisite is missing, so the failure surfaces up front instead
       of as a cryptic mid-activation error).
    2. `activate.py --all` - ensure every agent owned by this host (or `*`)
       with a trigger is actually registered and running.
    3. The built-in `agents-live health-check` loop - confirm each
       watcher and cron job is alive (self-healing any that died),
       refresh the host `health.ok` beacon, and run the framework
       smoketest.

    The header label then reflects the refreshed beacon (`system_health`),
    and a final notification summarises infrastructure + smoketest so the
    user sees the whole picture, not just the lifecycle scripts' exit
    codes.
    """
    if await do_action("Doctor", "doctor.py", []) != 0:
        _safe_ui(
            ui.notify,
            "Prerequisites failing - resolve the items above before activating.",
            type="warning", timeout=8000,
        )
        return

    await do_action("Activate", "activate.py", ["--all"])
    await do_action(
        "Health check", "health_check.py", ["--quiet"],
        timeout=WORKER_TIMEOUT,
    )
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
        await do_action("Stop", "stop.py", ["--name", name], agent_name=name)


# --- UI -----------------------------------------------------------------

def agent_rows() -> list[dict]:
    """Enriched row model shared by the agent table and the health strip."""
    host = ownership.current_host()
    rows: list[dict] = []
    for agent in collect_agents():
        name = agent["name"]
        # Drop the "(pid NNNN)" suffix headless adds for watcher agents; the
        # dashboard only needs the state word, not the process id.
        state = re.sub(r"\s*\(pid \d+\)", "", agent.get("state", "?"))
        owner = agent.get("owner") or "-"
        ok_ago, err_ago, last_status = last_runs(name)
        unhealthy = last_status == "error" and state != "inactive"
        local = _is_local(agent, host)
        runtime = agent.get("runtime") or "agency copilot"
        agent_display = runtime if runtime != "none" else "handler"
        cost_day, cost_week = (agent_cost(name) if runtime != "none" else ("-", "-"))
        model = _agent_model(agent, STATE["models"])
        can_pause = state.startswith("active") or state == "partial"
        can_activate = local and not state.startswith("active")
        rows.append({
            "name": name,
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
            "can_claim": not local,
            "run_tip": "Run this agent once now",
            "activate_tip": (
                "Register this host's cron/watcher for this agent"
                if can_activate else
                ("Already active" if local else
                 f"Owned by another host - use Claim to move it onto {host}")),
            "pause_tip": ("Stop this host's cron/watcher (config preserved)"
                          if can_pause else "Not running on this host"),
            "claim_tip": ("Already local" if local else
                          f"Claim onto {host} (transfer ownership + register trigger)"),
        })
    return rows


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

    The built-in loop (`agents-live health-check`) writes the host
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
                "tip": "the host health.ok beacon is missing - "
                       "`agents-live health-check` has never written a "
                       "healthy beacon. Run the health check."}
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
    name = event.args["name"]
    await do_action("Run", "run.py", ["--name", name], agent_name=name)


async def _activate_row(event) -> None:
    name = event.args["name"]
    await do_action("Activate", "activate.py", ["--name", name], agent_name=name)


async def _pause_row(event) -> None:
    name = event.args["name"]
    await do_action("Stop", "stop.py", ["--name", name], agent_name=name)


async def _claim_row(event) -> None:
    name = event.args["name"]
    await do_action("Activate", "activate.py",
                    ["--name", name, "--transfer-to", ownership.current_host()],
                    agent_name=name)


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
    summary = _refresh_summary()
    agent_grid.refresh()
    header_actions.refresh()
    _safe_ui(output_log.push, summary)


def build_page() -> None:
    ui.dark_mode().auto()
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
    host = ownership.current_host()

    with ui.row().classes("w-full items-center justify-between gap-x-4 gap-y-2"):
        with ui.row().classes("items-center gap-4 no-wrap"):
            ui.label("Agents Live").classes("text-xl font-semibold")
            ui.label(host).classes("text-sm text-gray-500")
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

    ui.timer(600.0, _refresh_views, immediate=False)


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

    if args.all_repos:
        build_all_repos_page()
    else:
        build_page()
    app.on_exception(lambda exc: _safe_ui(ui.notify, f"error: {exc}", type="negative"))
    ui.run(
        host="127.0.0.1",
        port=args.port,
        title="Agents Live",
        native=args.native,
        show=args.open_browser,
        reload=args.dev,
        uvicorn_reload_dirs=str(SCRIPTS_DIR),
        uvicorn_reload_includes="dashboard.py",
    )


main()
