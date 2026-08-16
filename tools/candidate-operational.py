#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["playwright>=1.50"]
# ///
"""Exercise installed CLI and dashboard actions against a live repository."""
from __future__ import annotations

import argparse
import contextlib
import errno
import json
import math
import os
import re
import signal
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

READY_TIMEOUT_S = 180.0
ACTION_TIMEOUT_S = 600.0
SHUTDOWN_GRACE_S = 10.0


class OperationalError(RuntimeError):
    pass


def _run(
    cli: Path,
    repo: Path,
    *args: str,
    json_output: bool = False,
) -> subprocess.CompletedProcess[str]:
    argv = [str(cli)]
    if json_output:
        argv.append("--json")
    argv.extend(("--repo", str(repo), *args))
    environment = os.environ.copy()
    environment.pop("AGENTS_LIVE_REPO", None)
    completed = subprocess.run(
        argv, cwd=repo, env=environment, capture_output=True,
        text=True, encoding="utf-8", errors="replace", check=False)
    if completed.returncode != 0:
        raise OperationalError(
            f"{' '.join(args)} exited {completed.returncode}: "
            f"{completed.stderr.strip() or completed.stdout.strip()}")
    return completed


def _json(cli: Path, repo: Path, *args: str) -> dict:
    completed = _run(cli, repo, *args, json_output=True)
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise OperationalError(
            f"{' '.join(args)} returned invalid JSON: {completed.stdout}") from exc
    if not isinstance(payload, dict) or not payload.get("ok"):
        raise OperationalError(f"{' '.join(args)} returned unhealthy JSON: {payload}")
    return payload


def _row(payload: dict, agent_id: str) -> dict:
    rows = [row for row in payload.get("agents", [])
            if isinstance(row, dict) and row.get("identifier") == agent_id]
    if len(rows) != 1:
        raise OperationalError(
            f"status returned {len(rows)} rows for {agent_id!r}")
    if not rows[0].get("loadable"):
        raise OperationalError(f"operational agent is not loadable: {rows[0]}")
    return rows[0]


def _set_started(cli: Path, repo: Path, agent_id: str, started: bool) -> None:
    row = _row(_json(cli, repo, "status", agent_id), agent_id)
    current = row.get("state") == "started"
    if current == started:
        return
    command = "start" if started else "stop"
    _run(cli, repo, command, "--name", agent_id)
    row = _row(_json(cli, repo, "status", agent_id), agent_id)
    if (row.get("state") == "started") != started:
        raise OperationalError(
            f"{command} did not produce the expected state: {row}")


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _api(port: int) -> dict | None:
    try:
        with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/api/agents", timeout=2) as response:
            value = json.loads(response.read().decode("utf-8"))
            return value if isinstance(value, dict) else None
    except (urllib.error.URLError, OSError, json.JSONDecodeError, TimeoutError):
        return None


def _port_answers(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.5)
        return probe.connect_ex(("127.0.0.1", port)) == 0


def _await_api(
    process: subprocess.Popen, port: int, *, observe=None,
) -> dict:
    deadline = time.monotonic() + READY_TIMEOUT_S
    while time.monotonic() < deadline:
        if observe is not None:
            observe()
        if process.poll() is not None:
            if observe is not None:
                observe()
            raise OperationalError(
                f"dashboard exited {process.returncode} before readiness")
        payload = _api(port)
        if payload and payload.get("agents"):
            return payload
        time.sleep(0.5)
    if observe is not None:
        observe()
    raise OperationalError("dashboard did not serve agent rows")


def _browser_executable() -> Path:
    candidates = []
    if os.name == "nt":
        for variable in ("PROGRAMFILES(X86)", "PROGRAMFILES", "LOCALAPPDATA"):
            base = os.environ.get(variable)
            if base:
                candidates.extend((
                    Path(base) / "Microsoft/Edge/Application/msedge.exe",
                    Path(base) / "Google/Chrome/Application/chrome.exe",
                ))
    elif sys.platform == "darwin":
        candidates.extend((
            Path("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"),
            Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
        ))
    else:
        for name in ("microsoft-edge", "google-chrome", "chromium", "chromium-browser"):
            found = shutil.which(name)
            if found:
                candidates.append(Path(found))
    browser = next((path for path in candidates if path.is_file()), None)
    if browser is None:
        raise OperationalError(
            "no installed Edge, Chrome, or Chromium browser is available")
    return browser


def _preflight(cli: Path, repo: Path, agent_id: str, cost_agent_id: str) -> None:
    from playwright.sync_api import sync_playwright

    if agent_id == cost_agent_id:
        raise OperationalError(
            "operational and cost acceptance agents must be distinct")
    existing = _run(cli, repo, "dashboard", "list")
    if "No dashboard started by this host is running." not in existing.stdout:
        raise OperationalError(
            "candidate acceptance requires no pre-existing managed dashboard")
    status = _json(cli, repo, "status")
    _row(status, agent_id)
    _row(status, cost_agent_id)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            executable_path=str(_browser_executable()), headless=True)
        browser.close()


def _action_count(cli: Path, repo: Path, agent_id: str, label: str) -> int:
    safe_agent = agent_id.replace("'", "''")
    safe_label = label.replace("'", "''")
    sql = (
        "select count(*) as count from log "
        f"where agent_name = '{safe_agent}' and trigger = 'dashboard' "
        f"and status = 'ok' and message like '{safe_label}:%'"
    )
    completed = _run(
        cli, repo, "logs", "--all", "--sql", sql, "--format", "jsonl")
    try:
        rows = [json.loads(line) for line in completed.stdout.splitlines() if line]
        return int(rows[0]["count"]) if rows else 0
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise OperationalError(
            f"could not read dashboard {label} action count") from exc


def _await_action(
    cli: Path, repo: Path, agent_id: str, label: str, before: int,
) -> None:
    deadline = time.monotonic() + ACTION_TIMEOUT_S
    while time.monotonic() < deadline:
        if _action_count(cli, repo, agent_id, label) > before:
            return
        time.sleep(0.5)
    raise OperationalError(f"dashboard {label} action did not complete")


def _await_dashboard_run(
    cli: Path, repo: Path, agent_id: str, started: str,
) -> str:
    safe_agent = agent_id.replace("'", "''")
    deadline = time.monotonic() + ACTION_TIMEOUT_S
    while time.monotonic() < deadline:
        result = _json(
            cli, repo, "logs", "--all", "--sql",
            "select * from log "
            f"where agent_name = '{safe_agent}' "
            "and trigger = 'dashboard' "
            "and message like 'Run:%' "
            f"and ts >= '{started}' order by ts desc limit 1")
        records = result.get("records")
        action = records[0] if isinstance(records, list) and records else None
        if isinstance(action, dict):
            if action.get("status") != "ok":
                raise OperationalError(
                    f"dashboard Run was not successful: {action}")
            run_id = action.get("run_id")
            if not isinstance(run_id, str) or not re.fullmatch(
                    r"[0-9a-f]+", run_id):
                raise OperationalError(
                    "dashboard Run returned no valid run_id")
            terminal = _json(
                cli, repo, "logs", "--all", "--sql",
                "select * from log "
                f"where run_id = '{run_id}' and phase = 'done' "
                "and status = 'ok' limit 1")
            terminal_records = terminal.get("records")
            if isinstance(terminal_records, list) and any(
                    isinstance(record, dict)
                    and record.get("agent_name") == agent_id
                    and record.get("run_id") == run_id
                    for record in terminal_records):
                return run_id
            raise OperationalError(
                "dashboard Run had no exact successful terminal event")
        time.sleep(0.5)
    raise OperationalError("dashboard Run action did not complete")


def _usage_map(value: object) -> dict[str, object]:
    if not isinstance(value, list):
        return {}
    result = {}
    for item in value:
        if isinstance(item, str):
            try:
                item = json.loads(item)
            except json.JSONDecodeError:
                continue
        if isinstance(item, list) and len(item) == 2 and isinstance(item[0], str):
            result[item[0]] = item[1]
    return result


def _verify_cost_capture(
    cli: Path, repo: Path, agent_id: str,
) -> tuple[str, float]:
    run = _json(cli, repo, "run", "--name", agent_id)
    run_id = _successful_run_id(run, agent_id)
    result = _json(
        cli, repo, "logs", "--all", "--sql",
        f"select * from log where run_id = '{run_id}' limit 20")
    records = result.get("records")
    if not isinstance(records, list):
        raise OperationalError("cost probe logs returned no records")
    for record in records:
        if (
            not isinstance(record, dict)
            or record.get("run_id") != run_id
            or record.get("status") != "ok"
        ):
            continue
        usage = _usage_map(record.get("usage"))
        value = usage.get("list_cost_usd")
        if isinstance(value, bool):
            continue
        try:
            cost = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(cost) and cost > 0:
            return run_id, cost
    raise OperationalError(
        f"cost probe {agent_id!r} produced no positive list_cost_usd usage")


def _successful_run_id(run: dict, agent_id: str) -> str:
    if run.get("status") != "success":
        raise OperationalError(
            f"explicit run for {agent_id!r} was {run.get('status')}: "
            f"{run.get('message', '')}")
    run_id = run.get("run_id")
    if not isinstance(run_id, str) or not re.fullmatch(r"[0-9a-f]+", run_id):
        raise OperationalError(
            f"explicit run for {agent_id!r} returned no valid run_id")
    return run_id


def _dashboard_costs(
    dashboard: dict, cost_agent_id: str,
) -> tuple[float, float]:
    cost_rows = [
        row for row in dashboard.get("agents", [])
        if isinstance(row, dict)
        and row.get("identifier") == cost_agent_id
    ]
    if len(cost_rows) != 1:
        raise OperationalError(
            f"dashboard returned {len(cost_rows)} cost-agent rows")
    cost_values = (
        cost_rows[0].get("cost_day_value"),
        cost_rows[0].get("cost_week_value"),
    )
    try:
        return tuple(
            float(value) if value is not None else 0.0
            for value in cost_values)
    except (TypeError, ValueError) as exc:
        raise OperationalError(
            "dashboard cost row returned a nonnumeric cost") from exc


def _verify_dashboard_cost(
    before: dict, after: dict, cost_agent_id: str, expected_cost: float,
) -> None:
    before_costs = _dashboard_costs(before, cost_agent_id)
    after_costs = _dashboard_costs(after, cost_agent_id)
    deltas = tuple(after_value - before_value
                   for before_value, after_value in zip(
                       before_costs, after_costs))
    if not all(abs(delta - expected_cost) <= 1e-9 for delta in deltas):
        raise OperationalError(
            f"dashboard cost deltas {deltas} did not equal "
            f"accepted run cost {expected_cost}")


def _registered_dashboard_pid(
    cli: Path, repo: Path, port: int,
) -> int | None:
    listed = _run(cli, repo, "dashboard", "list")
    match = re.search(rf"(?m)^\s*{port}\s+(\d+)\b", listed.stdout)
    return int(match.group(1)) if match is not None else None


def _await_dashboard_cost(
    port: int, before: dict, cost_agent_id: str, expected_cost: float,
) -> None:
    deadline = time.monotonic() + ACTION_TIMEOUT_S
    last = None
    while time.monotonic() < deadline:
        last = _api(port)
        if last is not None:
            try:
                _verify_dashboard_cost(
                    before, last, cost_agent_id, expected_cost)
            except OperationalError:
                pass
            else:
                return
        time.sleep(0.5)
    raise OperationalError(
        f"dashboard cost did not include accepted run cost {expected_cost}: "
        f"{last}")


def _verify_cost_attribution(
    cli: Path, repo: Path, agent_id: str, run_id: str, started: str,
) -> None:
    safe_agent = agent_id.replace("'", "''")
    result = _json(
        cli, repo, "logs", "--all", "--sql",
        "select distinct run_id from log "
        f"where agent_name = '{safe_agent}' and ts >= '{started}' "
        "and run_id is not null")
    records = result.get("records")
    observed = {
        record.get("run_id") for record in records
        if isinstance(record, dict) and isinstance(record.get("run_id"), str)
    } if isinstance(records, list) else set()
    if observed != {run_id}:
        raise OperationalError(
            f"cost attribution window contained run IDs {sorted(observed)}; "
            f"expected only {run_id}")


def _terminate_dashboard(
    process: subprocess.Popen, *, process_group: int | None = None,
    dashboard_pid: int | None = None,
) -> None:
    if os.name == "nt":
        if dashboard_pid is None and process.poll() is not None:
            return
        terminated = subprocess.run(
            ["taskkill", "/T", "/F", "/PID",
             str(dashboard_pid or process.pid)],
            capture_output=True, check=False)
        if terminated.returncode != 0 and process.poll() is None:
            raise OperationalError(
                "could not terminate dashboard process tree "
                f"{dashboard_pid or process.pid}")
    else:
        if process_group is None:
            process_group = os.getpgid(process.pid)
        try:
            os.killpg(process_group, signal.SIGTERM)
        except ProcessLookupError:
            process_group = None
        except OSError:
            if process.poll() is None:
                process.terminate()
    if process.poll() is None:
        try:
            process.wait(timeout=SHUTDOWN_GRACE_S)
        except subprocess.TimeoutExpired:
            if os.name != "nt":
                with contextlib.suppress(OSError):
                    os.killpg(process_group, signal.SIGKILL)
            process.kill()
            try:
                process.wait(timeout=SHUTDOWN_GRACE_S)
            except subprocess.TimeoutExpired as exc:
                raise OperationalError(
                    f"dashboard process {process.pid} survived cleanup") from exc
    if process_group is not None:
        deadline = time.monotonic() + SHUTDOWN_GRACE_S
        while time.monotonic() < deadline:
            try:
                os.killpg(process_group, 0)
            except ProcessLookupError:
                return
            except OSError as exc:
                if exc.errno == errno.ESRCH:
                    return
                raise OperationalError(
                    "could not verify dashboard process group "
                    f"{process_group}") from exc
            time.sleep(0.1)
        with contextlib.suppress(OSError):
            os.killpg(process_group, signal.SIGKILL)
        deadline = time.monotonic() + SHUTDOWN_GRACE_S
        while time.monotonic() < deadline:
            try:
                os.killpg(process_group, 0)
            except ProcessLookupError:
                return
            except OSError as exc:
                if exc.errno == errno.ESRCH:
                    return
                raise OperationalError(
                    "could not verify dashboard process group "
                    f"{process_group}") from exc
            time.sleep(0.1)
        raise OperationalError(
            f"dashboard process group {process_group} survived cleanup")


def _verify_dashboard_stopped(cli: Path, repo: Path, port: int) -> None:
    deadline = time.monotonic() + SHUTDOWN_GRACE_S
    while time.monotonic() < deadline:
        listed = _run(cli, repo, "dashboard", "list")
        if not _port_answers(port) and str(port) not in listed.stdout:
            return
        time.sleep(0.25)
    raise OperationalError(
        f"dashboard on port {port} survived operational cleanup")


def _stop_dashboard(
    cli: Path, repo: Path, port: int, process: subprocess.Popen,
    *, process_group: int | None, dashboard_pid: int | None,
) -> None:
    managed = subprocess.run(
        [str(cli), "--repo", str(repo), "dashboard", "stop", "--port",
         str(port)],
        cwd=repo, capture_output=True, text=True, encoding="utf-8",
        errors="replace", check=False)
    if managed.returncode != 0 or _port_answers(port):
        _terminate_dashboard(
            process, process_group=process_group,
            dashboard_pid=dashboard_pid)
    _verify_dashboard_stopped(cli, repo, port)


def _dashboard_actions(
    cli: Path, repo: Path, agent_id: str, display_name: str, baseline: bool,
    cost_agent_id: str,
) -> None:
    from playwright.sync_api import sync_playwright

    port = _free_port()
    environment = os.environ.copy()
    environment.pop("AGENTS_LIVE_REPO", None)
    existing = _run(cli, repo, "dashboard", "list")
    if "No dashboard started by this host is running." not in existing.stdout:
        raise OperationalError(
            "candidate acceptance requires no pre-existing managed dashboard")
    process = subprocess.Popen(
        [str(cli), "--repo", str(repo), "dashboard", "--port", str(port)],
        cwd=repo, env=environment, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        **({} if os.name == "nt" else {"start_new_session": True}))
    process_group = None if os.name == "nt" else os.getpgid(process.pid)
    dashboard_pid = None

    def observe_dashboard_pid() -> None:
        nonlocal dashboard_pid
        observed = _registered_dashboard_pid(cli, repo, port)
        if observed is not None:
            dashboard_pid = observed

    try:
        dashboard = _await_api(
            process, port, observe=observe_dashboard_pid)
        listed = _run(cli, repo, "dashboard", "list")
        if str(port) not in listed.stdout:
            raise OperationalError(
                "dashboard list did not report the operational dashboard")
        observe_dashboard_pid()
        if dashboard_pid is None:
            raise OperationalError(
                "dashboard list did not report its process identity")
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                executable_path=str(_browser_executable()), headless=True)
            try:
                page = browser.new_page()
                page.goto(f"http://127.0.0.1:{port}/", wait_until="networkidle")
                row = page.get_by_role("row").filter(
                    has=page.get_by_text(display_name, exact=True))
                if row.count() != 1:
                    raise OperationalError(
                        f"dashboard rendered {row.count()} rows for "
                        f"{display_name!r}")

                health_before = _action_count(
                    cli, repo, "dashboard", "Health check")
                refresh_lines = page.get_by_text(
                    re.compile(
                        r"^\[[^\]]+\] Health check dashboard refresh complete$"
                    )
                )
                refresh_count = refresh_lines.count()
                page.get_by_role("button", name="Run health check").click()
                _await_action(
                    cli, repo, "dashboard", "Health check", health_before)
                page.get_by_text(re.compile(r"^healthy ")).first.wait_for(
                    state="visible", timeout=ACTION_TIMEOUT_S * 1000)
                refresh_lines.nth(refresh_count).wait_for(
                    state="visible", timeout=ACTION_TIMEOUT_S * 1000)

                dashboard_run_started = datetime.now(timezone.utc).isoformat()
                row.get_by_role(
                    "button", name="Run this agent once now").click()
                _await_dashboard_run(
                    cli, repo, agent_id, dashboard_run_started)

                if baseline:
                    before = _action_count(cli, repo, agent_id, "Stop")
                    row.get_by_role(
                        "button",
                        name="Stop this host's cron/watcher (config preserved)",
                    ).click()
                    _await_action(cli, repo, agent_id, "Stop", before)
                    row.get_by_role(
                        "button",
                        name="Register this host's cron/watcher",
                    ).wait_for(
                        state="visible", timeout=ACTION_TIMEOUT_S * 1000)
                    before = _action_count(cli, repo, agent_id, "Start")
                    row.get_by_role(
                        "button",
                        name="Register this host's cron/watcher",
                    ).click()
                    _await_action(cli, repo, agent_id, "Start", before)
                else:
                    before = _action_count(cli, repo, agent_id, "Start")
                    row.get_by_role(
                        "button",
                        name="Register this host's cron/watcher",
                    ).click()
                    _await_action(cli, repo, agent_id, "Start", before)
                    row.get_by_role(
                        "button",
                        name="Stop this host's cron/watcher (config preserved)",
                    ).wait_for(
                        state="visible", timeout=ACTION_TIMEOUT_S * 1000)
                    before = _action_count(cli, repo, agent_id, "Stop")
                    row.get_by_role(
                        "button",
                        name="Stop this host's cron/watcher (config preserved)",
                    ).click()
                    _await_action(cli, repo, agent_id, "Stop", before)
                    row.get_by_role(
                        "button",
                        name="Register this host's cron/watcher",
                    ).wait_for(
                        state="visible", timeout=ACTION_TIMEOUT_S * 1000)

                cost_window_started = datetime.now(timezone.utc).isoformat()
                dashboard = _api(port)
                if dashboard is None:
                    raise OperationalError(
                        "dashboard API was unavailable before the cost probe")
                cost_run_id, accepted_cost = _verify_cost_capture(
                    cli, repo, cost_agent_id)
                _await_dashboard_cost(
                    port, dashboard, cost_agent_id, accepted_cost)
                _verify_cost_attribution(
                    cli, repo, cost_agent_id, cost_run_id,
                    cost_window_started)
            finally:
                browser.close()
    finally:
        _stop_dashboard(
            cli, repo, port, process, process_group=process_group,
            dashboard_pid=dashboard_pid)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cli", type=Path, required=True)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--agent", required=True)
    parser.add_argument("--cost-agent", required=True)
    parser.add_argument(
        "--preflight", action="store_true",
        help="Validate browser, dashboard, and selected agents without mutation",
    )
    args = parser.parse_args()
    cli = args.cli.resolve()
    repo = args.repo.resolve()
    if args.preflight:
        _preflight(cli, repo, args.agent, args.cost_agent)
        print(json.dumps({"ok": True, "phase": "preflight"}))
        return 0
    before = _row(_json(cli, repo, "status", args.agent), args.agent)
    baseline = before.get("state") == "started"
    display_name = str(before.get("name") or args.agent)
    failure: BaseException | None = None
    try:
        _json(cli, repo, "doctor")
        run_started = datetime.now(timezone.utc).isoformat()
        run = _json(cli, repo, "run", "--name", args.agent)
        run_id = _successful_run_id(run, args.agent)
        _run(
            cli, repo, "logs", "timeline", args.agent,
            "--all", "--since", run_started)
        log_result = _json(
            cli, repo, "logs", "--all", "--sql",
            f"select * from log where run_id = '{run_id}' "
            f"and ts >= '{run_started}' limit 20")
        records = log_result.get("records")
        if not isinstance(records, list) or not any(
                isinstance(record, dict)
                and record.get("agent_name") == args.agent
                and record.get("run_id") == run_id
                and record.get("phase") == "done"
                and record.get("status") == "ok"
                for record in records):
            raise OperationalError(
                "logs did not include a new successful terminal run event")
        _set_started(cli, repo, args.agent, not baseline)
        _set_started(cli, repo, args.agent, baseline)
        _dashboard_actions(
            cli, repo, args.agent, display_name, baseline,
            args.cost_agent)
        after = _row(_json(cli, repo, "status", args.agent), args.agent)
        if after.get("state") != before.get("state"):
            raise OperationalError("operational pass did not restore agent state")
        _json(cli, repo, "doctor")
    except BaseException as exc:
        failure = exc
        raise
    finally:
        try:
            _set_started(cli, repo, args.agent, baseline)
        except Exception as restore_error:
            if failure is None:
                raise
            print(f"restore failed: {restore_error}", file=sys.stderr)
    print(json.dumps({
        "ok": True,
        "agent": args.agent,
        "cost_agent": args.cost_agent,
        "baseline_state": before.get("state"),
        "surfaces": ["cli", "dashboard"],
    }))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except OperationalError as exc:
        print(f"candidate operational acceptance failed: {exc}", file=sys.stderr)
        raise SystemExit(1)