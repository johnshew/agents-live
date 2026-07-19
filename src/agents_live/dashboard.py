#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["nicegui>=2.0", "PyYAML", "pywebview"]
# ///
"""Interactive agents-live control panel (single host).

An HTML control surface over the agents-live lifecycle scripts. It
lists every agent with its live state and ownership, and exposes per-agent
Run / Activate / Pause / Claim buttons that shell the existing scripts
(`run.py`, `activate.py`, `stop.py`), plus a top-bar Health check
that verifies prerequisites (`doctor.py`), activates everything owned by
this host (`activate.py --all`), then runs the `agents-live-health-check`
worker (watchers, cron, framework smoketest) and refreshes the health
beacon. The header health label reflects the real beacon
(`Agents/data/health.ok`): healthy, degraded (infra up but smoketest
failing), or unhealthy (beacon missing or stale). State and last-run
times are read straight from `Agents/logs/` and the agent configs - no new
data layer. Every action is logged to `Agents/logs/dashboard.log` (JSONL)
with the full transcript in `Agents/logs/dashboard-transcript.log`.

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
import json
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent

# Dual-layout import: packaged, the sibling modules belong to the
# agents_live package and must be imported through it so their relative
# imports resolve; flat, they are top-level scripts beside this file.
# __init__.py is the layout discriminator - the flat scripts dir has none.
if (SCRIPTS_DIR / "__init__.py").is_file():
    if str(SCRIPTS_DIR.parent) not in sys.path:
        sys.path.insert(0, str(SCRIPTS_DIR.parent))
    from agents_live import cli_spec, headless, ownership, repos  # noqa: E402
else:
    if str(SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPTS_DIR))
    import cli_spec  # noqa: E402
    import headless  # noqa: E402
    import ownership  # noqa: E402
    import repos  # noqa: E402
from nicegui import app, ui  # noqa: E402
from nicegui import run as ng_run  # noqa: E402

try:
    REPO_ROOT = headless.repo_root()
except ValueError:
    REPO_ROOT = None
LOGS_DIR = REPO_ROOT / "Agents" / "logs" if REPO_ROOT else None
HEALTH_OK_PATH = (
    REPO_ROOT / "Agents" / "data" / "health.ok"
    if REPO_ROOT else None
)
# The health-check worker is scheduled hourly; allow a little slack before
# treating the beacon as stale (a missed run shouldn't flap the header).
HEALTH_STALE_MINUTES = 70
# Cap the on-demand health-check worker run from the dashboard. The worker's
# framework smoketest has its own 360s internal timeout; this is a hard outer
# bound so the spinner can never hang forever.
WORKER_TIMEOUT = 480

STATE: dict = {"last_refresh": datetime.now(timezone.utc)}


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


async def do_action(label: str, script: str, args: list[str],
                    *, agent_name: str | None = None,
                    timeout: float | None = None) -> int:
    target = agent_name or " ".join(args)
    # Creating the notification can itself raise if the client already
    # disconnected, so guard it like every other UI touch below.
    note = _safe_ui(ui.notification, f"{label}: {target} ...",
                    spinner=True, timeout=None)
    try:
        code, out = await ng_run.io_bound(_run_script, script, args, timeout=timeout)
    finally:
        if note is not None:
            _safe_ui(note.dismiss)
    ok = code == 0
    # Persist the outcome first so a disconnected client never loses the record.
    _log_action(label, script, args, code, out, agent_name=agent_name)
    _safe_ui(
        ui.notify,
        f"{label} {target}: {'ok' if ok else f'failed (exit {code})'}",
        type="positive" if ok else "negative",
    )
    timestamp = datetime.now().astimezone().strftime("%H:%M:%S %Z")
    _safe_ui(output_log.push, f"[{timestamp}] {label} {target} (exit {code})")
    for line in out.splitlines():
        _safe_ui(output_log.push, f"    {line}")
    _safe_ui(_refresh_views)
    return code


async def health_check() -> None:
    """Verify and report the full health picture for this host.

    Runs every check the system's health depends on, in order, and
    surfaces each result rather than only the prerequisites:

    1. `doctor.py` - environment readiness (gate: abort if a required
       prerequisite is missing, so the failure surfaces up front instead
       of as a cryptic mid-activation error).
    2. `activate.py --all` - ensure every agent owned by this host (or `*`)
       with a trigger is actually registered and running.
    3. The `agents-live-health-check` worker - confirm each watcher
       and cron job is alive (self-healing any that died), refresh the
       `health.ok` beacon, and run the framework smoketest.

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
        "Health check", "run.py",
        ["--name", "agents-live-health-check", "--quiet"],
        agent_name="agents-live-health-check",
        timeout=WORKER_TIMEOUT,
    )
    # Summarise the refreshed beacon so the user sees infra + smoketest,
    # not just exit codes. system_health reads Agents/data/health.ok.
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
        can_pause = state.startswith("active") or state == "partial"
        can_activate = local and not state.startswith("active")
        rows.append({
            "name": name,
            "agent": agent_display,
            "trigger": trigger_summary(agent),
            "state": state,
            "owner": owner,
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


def system_health() -> dict:
    """Real infrastructure health, read from the health-check worker beacon.

    The worker (`agents-live-health-check`) writes
    `Agents/data/health.ok` only after confirming every intended watcher
    is alive (self-healing any that died), so a *fresh* beacon
    means the infrastructure is genuinely up. A missing or stale beacon
    means the worker has not confirmed health within the hour. The nested
    smoketest verdict is surfaced as a distinct *degraded* state: the
    framework end-to-end test is failing even though watcher/cron
    infrastructure is healthy.

    Returns a dict with ``level`` ("ok" | "degraded" | "down"), a short
    ``text`` label for the header, and a longer ``tip`` tooltip.
    """
    now = datetime.now(timezone.utc)
    health_ok_path = _require_repo_path(HEALTH_OK_PATH)
    if not health_ok_path.is_file():
        return {"level": "down", "text": "unhealthy: no beacon",
                "tip": "Agents/data/health.ok is missing - the health-check "
                       "worker has never written a healthy beacon. "
                       "Run the health check."}
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
    {"name": "trigger", "label": "Trigger", "field": "trigger", "align": "left",
     "style": "width: 100%; max-width: 0", "headerStyle": "width: 100%"},
    {"name": "last_ok", "label": "Last OK", "field": "last_ok", "align": "right",
     "style": "width: 64px", "headerStyle": "width: 64px"},
    {"name": "last_err", "label": "Last Err", "field": "last_err", "align": "right",
     "style": "width: 64px", "headerStyle": "width: 64px"},
    {"name": "cost_day", "label": "$ 24h", "field": "cost_day", "align": "right",
     "sortable": True, "style": "width: 64px", "headerStyle": "width: 64px"},
    {"name": "cost_week", "label": "$ 7d", "field": "cost_week", "align": "right",
     "sortable": True, "style": "width: 64px", "headerStyle": "width: 64px"},
]


@ui.refreshable
def agent_grid() -> None:
    STATE["last_refresh"] = datetime.now(timezone.utc)
    table = ui.table(
        columns=_AGENT_COLUMNS, rows=agent_rows(), row_key="name",
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
    table.add_slot("body-cell-agent", '''
        <q-td :props="props">
          <div style="white-space:nowrap">{{ props.row.agent }}</div>
        </q-td>
    ''')
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
    agent_grid.refresh()
    header_actions.refresh()


def build_page() -> None:
    ui.dark_mode().auto()
    ui.add_css(
        ".q-table tbody tr{transition:background-color .08s}"
        ".q-table tbody tr:hover{background-color:rgba(0,0,0,0.045)}"
        ".body--dark .q-table tbody tr:hover{background-color:rgba(255,255,255,0.07)}"
        ".hdr-btn{min-height:0}"
        ".hdr-btn .q-btn__content{min-height:0;white-space:nowrap}"
        ".hdr-btn .q-icon{font-size:0.95em}"
        ".hdr-btn .q-btn__content .q-icon{margin-right:5px}"
        # Stretch the page column to the viewport so the action log can
        # flex-grow into any leftover space on tall screens.
        ".nicegui-content{min-height:100vh}"
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

    with ui.card().classes("w-full"):
        agent_grid()

    ui.label("Action log").classes("text-sm text-gray-500 mt-2")
    global output_log
    output_log = ui.log(max_lines=300).classes(
        "w-full grow font-mono text-xs"
    ).style("min-height:18rem")

    ui.timer(600.0, _refresh_views)


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
