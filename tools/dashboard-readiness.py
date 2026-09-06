#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["playwright>=1.50"]
# ///
"""Prove a built dashboard serves real rows with real action flags (#279).

Two point releases existed because source-level imports and
``dashboard --help`` passed while the packaged dashboard could not start.
A third existed because the rows it did serve had Start and Stop
reversed. Neither is reachable from the import side: the table is
websocket-rendered, so a successful GET of ``/`` proves only that NiceGUI
bound a port.

This gate launches the artifact against a throwaway local-only project
with one started definition, waits on ``/api/agents``, and asserts the
row, its state, and the availability of the actions that act on it. The
default release plan assigns each launch mode distinct evidence: normal mode
owns the responsive and continuity journeys, all-repositories owns the
repository-qualified Run action, and ``--dev`` owns reload-worker startup.

    uv run --script tools/dashboard-readiness.py                 # built wheel
    uv run --script tools/dashboard-readiness.py --editable      # this checkout
    uv run --script tools/dashboard-readiness.py --plan          # no browser
    uv run --script tools/dashboard-readiness.py --editable \
        --scenario continuity --viewport desktop                 # focused UX

The fixture is a temporary directory with its own state, data, and config
homes, so the gate never reads the developer's registry or touches a real
project.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import shutil
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
READY_TIMEOUT_S = 180.0
POLL_INTERVAL_S = 0.5
SHUTDOWN_GRACE_S = 10.0

SCENARIO_ORDER = (
    "startup", "layout", "continuity", "repositories", "aggregate",
    "disconnect",
)
VIEWPORTS = {
    "desktop": (1280, 720),
    "wide": (1440, 900),
    "mobile": (390, 844),
}
RELEASE_SCENARIOS = {
    "normal": (
        "startup", "layout", "continuity", "repositories", "disconnect",
    ),
    "all-repos": ("startup", "aggregate"),
    "dev": ("startup",),
}

DEFINITION = """---
name: readiness-agent
description: Fixture definition for the dashboard readiness gate.
metadata:
    agents-live.schema-version: "1"
    agents-live.selector: "fake/echo"
    agents-live.schedule: "0 8 * * *"
    agents-live.watch: "docs/** debounce 1s"
---
Report dashboard readiness.
"""

OWNERSHIP_PLUGIN = """import sys

def registry_file_exists(*, root=None):
    return True

def load_owners(*, root=None, rate_limit_secs=60):
    return {"readiness-agent": "*"}

def set_owner(name, owner, *, root=None):
    return None

def remove_owner(name, *, root=None):
    return None

OWNERSHIP_REGISTRY = sys.modules[__name__]
"""


class ReadinessError(RuntimeError):
    pass


def _say(message: str) -> None:
    print(f"+ dashboard readiness: {message}", flush=True)


def _free_port() -> int:
    """A port the OS just confirmed is free.

    Racy by nature, which is why each mode asks for its own rather than
    reusing a fixed one that a previous run may still hold.
    """
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _wheel(explicit: Path | None = None) -> Path:
    if explicit is not None:
        wheel = explicit.expanduser().resolve()
        if not wheel.is_file():
            raise ReadinessError(f"no built wheel at {wheel}")
        return wheel
    version = subprocess.run(
        ["uv", "version", "--short"], cwd=ROOT, capture_output=True,
        text=True, check=True).stdout.strip()
    wheel = ROOT / "dist" / f"agents_live-{version}-py3-none-any.whl"
    if not wheel.is_file():
        raise ReadinessError(
            f"no built wheel for {version}: run `uv build` first ({wheel})")
    return wheel


def _launcher(
    directory: Path, editable: bool, wheel: Path | None = None,
) -> tuple[list[str], list[str]]:
    """(CLI prefix, python prefix) for the artifact under test.

    The wheel goes into a real environment rather than an ephemeral
    ``uv run --with``. The CLI delegates the dashboard through ``uv run
    --script`` against its own installed path, and uv refuses to treat a
    directory inside its cache as a project - which is exactly where an
    ephemeral install lands when the cache sits in the workspace, as it
    does on CI. An environment also matches how a consumer runs the tool.
    """
    if editable:
        base = ["uv", "run", "--with-editable", str(ROOT)]
        return ([*base, "agents-live"], [*base, "python"])
    candidate = _wheel(wheel)
    collision_root = directory / "collision"
    collision_tools = collision_root / "tools"
    collision_bin = collision_root / "bin"
    collision_bin.mkdir(parents=True)
    collision_alias = collision_bin / ("al.exe" if os.name == "nt" else "al")
    marker = b"unrelated executable\n"
    collision_alias.write_bytes(marker)
    collision_environment = os.environ.copy()
    collision_environment.update({
        "UV_TOOL_DIR": str(collision_tools),
        "UV_TOOL_BIN_DIR": str(collision_bin),
    })
    collision = subprocess.run(
        ["uv", "tool", "install", str(candidate)],
        capture_output=True, text=True, env=collision_environment)
    if collision.returncode == 0 or collision_alias.read_bytes() != marker:
        raise ReadinessError(
            "uv silently replaced an unrelated al executable during install")
    environment = directory / "runtime"
    for command in (
        ["uv", "venv", str(environment)],
        ["uv", "pip", "install", "--python", str(environment),
         str(candidate)],
    ):
        completed = subprocess.run(command, capture_output=True, text=True)
        if completed.returncode != 0:
            raise ReadinessError(
                f"could not build the candidate environment: "
                f"{' '.join(command)}\n{completed.stdout}{completed.stderr}")
    windows = os.name == "nt"
    binaries = environment / ("Scripts" if windows else "bin")
    suffix = ".exe" if windows else ""
    primary = binaries / f"agents-live{suffix}"
    alias = binaries / f"al{suffix}"
    for arguments in (["--version"], ["--help"]):
        outputs = []
        for executable in (primary, alias):
            completed = subprocess.run(
                [str(executable), *arguments], capture_output=True, text=True)
            if completed.returncode != 0:
                raise ReadinessError(
                    f"{executable.name} {' '.join(arguments)} failed:\n"
                    f"{completed.stdout}{completed.stderr}")
            outputs.append(completed.stdout)
        if outputs[0] != outputs[1]:
            raise ReadinessError(
                f"{primary.name} and {alias.name} disagree for "
                f"{' '.join(arguments)}")
    return ([str(binaries / f"agents-live{suffix}")],
            [str(binaries / f"python{suffix}")])


def _fixture(directory: Path) -> None:
    """A registry-owned project with one source-loaded plugin."""
    skill = directory / "Agents" / "readiness-agent"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(DEFINITION, encoding="utf-8")
    plugin = directory / "readiness_ownership.py"
    plugin.write_text(OWNERSHIP_PLUGIN, encoding="utf-8")
    (directory / ".agents-live.toml").write_text(
        'ownership = "registry"\n\n'
        '[plugins.readiness-ownership]\n'
        'path = "readiness_ownership.py"\n',
        encoding="utf-8")
    registry = directory / "config" / "agents-live" / "config.toml"
    registry.parent.mkdir(parents=True)
    registry.write_text(
        f'default_repo = "readiness"\n\n[repos]\n'
        f'readiness = {json.dumps(str(directory))}\n',
        encoding="utf-8",
    )


def _environment(directory: Path) -> dict[str, str]:
    environment = os.environ.copy()
    environment.update({
        "AGENTS_LIVE_REPO": str(directory),
        "XDG_STATE_HOME": str(directory / "state"),
        "XDG_DATA_HOME": str(directory / "data"),
        "XDG_CONFIG_HOME": str(directory / "config"),
    })
    return environment


SEED = """
import sys
import uuid
from pathlib import Path
from agents_live import agent, obs, paths, state
root = Path(sys.argv[1]).resolve()
identifier = agent.load("readiness-agent", root=root).identifier
state.replace(root, {identifier})
obs.record(
    paths.repo_state_dir(root) / "logs" / f"{identifier}.jsonl",
    obs.create(
        "run", "failed", repository=str(root), agent=identifier,
        run_id=uuid.uuid4().hex, origin="readiness",
    ),
)
"""


def _seed_started_state(python: list[str], directory: Path,
                        environment: dict[str, str]) -> None:
    """Mark the definition started without touching this host.

    ``start`` is the honest verb, but its trigger store is the user's own
    crontab, and a gate that called it would leave entries pointing at a
    temporary directory that no longer exists. The dashboard reads the
    started state, which is what this writes.
    """
    completed = subprocess.run(
        [*python, "-c", SEED, str(directory)],
        cwd=directory, env=environment, capture_output=True, text=True)
    if completed.returncode != 0:
        raise ReadinessError(
            "could not seed the fixture's started state: "
            f"{completed.stdout}{completed.stderr}")


def _api_agents(port: int) -> dict | None:
    try:
        with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/api/agents", timeout=2) as response:
            return json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, json.JSONDecodeError, TimeoutError):
        return None


def _api_all_repos(port: int) -> dict | None:
    try:
        with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/api/all-repos", timeout=2) as response:
            return json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, json.JSONDecodeError, TimeoutError):
        return None


def _await_rows(process: subprocess.Popen, port: int, mode: str) -> dict:
    deadline = time.monotonic() + READY_TIMEOUT_S
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise ReadinessError(
                f"{mode}: dashboard exited {process.returncode} before "
                f"serving /api/agents\n{_output(process)}")
        payload = _api_agents(port)
        if payload and payload.get("agents"):
            return payload
        time.sleep(POLL_INTERVAL_S)
    raise ReadinessError(
        f"{mode}: /api/agents served no rows within {READY_TIMEOUT_S:.0f}s\n"
        f"{_output(process)}")


def _output(process: subprocess.Popen) -> str:
    """What the dashboard said, bounded, so a failure is diagnosable.

    A gate that reports only an exit status sends the reader back to
    reproduce it by hand, which on a CI host is the one thing they
    cannot do.
    """
    if process.stdout is None:
        return "  (no output captured)"
    try:
        text = process.stdout.read() or ""
    except (OSError, ValueError):
        return "  (output unavailable)"
    lines = text.splitlines()[-40:]
    return "\n".join(f"  | {line}" for line in lines) or "  (no output)"


def _watcher_health_label(liveness: object) -> str | None:
    return {
        "missing": "Watcher missing",
        "unavailable": "Watcher unavailable",
    }.get(liveness)


def _assert_row(payload: dict, mode: str, *, started: bool,
                expect_failure: bool = True) -> None:
    rows = payload["agents"]
    names = [row.get("name") for row in rows]
    if names != ["readiness-agent"]:
        raise ReadinessError(f"{mode}: unexpected rows {names}")
    row = rows[0]
    expected_state = "started" if started else "stopped"
    if row.get("state") != expected_state:
        raise ReadinessError(
            f"{mode}: state is {row.get('state')!r}, expected "
            f"{expected_state!r}")
    # The reversal in #276 passed every check that read only the state.
    if bool(row.get("can_pause")) is not started:
        raise ReadinessError(
            f"{mode}: can_pause is {row.get('can_pause')!r} for a "
            f"{expected_state} row")
    if bool(row.get("can_activate")) is started:
        raise ReadinessError(
            f"{mode}: can_activate is {row.get('can_activate')!r} for a "
            f"{expected_state} row")
    watcher_liveness = row.get("watcher_liveness")
    watcher_health = _watcher_health_label(watcher_liveness)
    if watcher_health is None:
        raise ReadinessError(
            f"{mode}: started watcher liveness is "
            f"{watcher_liveness!r}, expected 'missing' or 'unavailable'")
    if expect_failure and (
            not row.get("unhealthy")
            or "Failing: newest run" not in str(row.get("health", ""))):
        raise ReadinessError(
            f"{mode}: newest structured run failure is not visible: "
            f"{row.get('health')!r}")
    if watcher_health not in str(row.get("health", "")):
        raise ReadinessError(
            f"{mode}: watcher intent masked {watcher_liveness} liveness: "
            f"{row.get('health')!r}")
    reasons = str(row.get("action_reasons", ""))
    if "Start: Already active" not in reasons or "Claim:" not in reasons:
        raise ReadinessError(
            f"{mode}: disabled action reasons are incomplete: {reasons!r}")


def _browser_executable() -> Path:
    candidates = []
    if os.name == "nt":
        for base in (os.environ.get("PROGRAMFILES"),
                     os.environ.get("PROGRAMFILES(X86)"),
                     os.environ.get("LOCALAPPDATA")):
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
        for name in ("microsoft-edge", "google-chrome", "chromium",
                     "chromium-browser"):
            executable = shutil.which(name)
            if executable:
                candidates.append(Path(executable))
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise ReadinessError("no installed Edge, Chrome, or Chromium browser is available")


def _assert_operational_viewport(
    port: int,
    directory: Path,
    mode: str,
    scenarios: set[str],
    viewport_names: tuple[str, ...],
    watcher_health: str,
) -> None:
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
    from playwright.sync_api import sync_playwright

    all_repos = _api_all_repos(port)
    repositories = all_repos.get("repositories", []) if all_repos else []
    if len(repositories) != 1 or repositories[0].get("path") != str(directory):
        raise ReadinessError(
            f"{mode}: /api/all-repos did not preserve the registered repository")
    repository_name = str(repositories[0].get("name", ""))
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            executable_path=str(_browser_executable()), headless=True)
        try:
            continuity_viewport = viewport_names[0] if viewport_names else None
            for viewport_name in viewport_names:
                width, height = VIEWPORTS[viewport_name]
                started = time.perf_counter()
                page = browser.new_page(viewport={"width": width, "height": height})
                page.goto(f"http://127.0.0.1:{port}/", wait_until="networkidle")
                body = page.locator(".dashboard-body")
                body.wait_for(state="visible")
                groups = page.locator(".repository-group")
                if groups.count() != 1:
                    raise ReadinessError(
                        f"{mode} {width}x{height}: rendered {groups.count()} "
                        "repository groups")
                heading = groups.locator(".repository-heading")
                if str(directory) not in heading.inner_text():
                    raise ReadinessError(
                        f"{mode} {width}x{height}: repository path is absent")
                if page.get_by_label("Search agents or repositories").count() != 1 \
                        or page.get_by_role("button", name="Filters").count() != 1:
                    raise ReadinessError(
                        f"{mode} {width}x{height}: compact controls are absent")
                page.get_by_text(
                    "Attention in all registered repositories:", exact=False,
                ).wait_for()
                page.get_by_text("Failing: newest run", exact=False).wait_for()
                page.get_by_text(watcher_health, exact=False).wait_for()
                start_button = page.get_by_role("button", name="Start: Already active", exact=True)
                start_button.wait_for()
                if start_button.is_enabled():
                    raise ReadinessError(f"{mode}: started agent offers Start")
                if body.locator(
                        ".host-service-panel, .repository-settings-panel").count():
                    raise ReadinessError(
                        f"{mode} {width}x{height}: settings consume the dashboard")
                agent_box = page.locator(".agent-panel").bounding_box()
                log_box = page.locator(".dashboard-log-panel").bounding_box()
                if agent_box is None or log_box is None:
                    raise ReadinessError(
                        f"{mode} {width}x{height}: inventory or log is absent")
                if width >= 1280 and log_box["height"] < 140:
                    raise ReadinessError(
                        f"{mode} {width}x{height}: log cannot show ten lines")
                if agent_box["y"] < 0 or log_box["y"] + log_box["height"] > height:
                    raise ReadinessError(
                        f"{mode} {width}x{height}: operational regions overflow")
                toolbar_boxes = page.locator(".agent-toolbar > *").evaluate_all(
                    "elements => elements.map(element => { const bounds = element.getBoundingClientRect(); "
                    "return {left: bounds.left, right: bounds.right, top: bounds.top, bottom: bounds.bottom}; })")
                for index, bounds in enumerate(toolbar_boxes):
                    if bounds["left"] < 0 or bounds["right"] > width:
                        raise ReadinessError(f"{mode} {width}x{height}: toolbar control overflows")
                    for other in toolbar_boxes[index + 1:]:
                        if min(bounds["right"], other["right"]) > max(bounds["left"], other["left"]) + 1 \
                                and min(bounds["bottom"], other["bottom"]) > max(bounds["top"], other["top"]) + 1:
                            raise ReadinessError(f"{mode} {width}x{height}: toolbar controls overlap")
                if screenshot_dir := os.environ.get("AGENTS_LIVE_READINESS_SCREENSHOTS"):
                    destination = Path(screenshot_dir)
                    destination.mkdir(parents=True, exist_ok=True)
                    page.screenshot(path=str(destination / f"{mode}-{viewport_name}.png"))
                if "continuity" not in scenarios \
                        or viewport_name != continuity_viewport:
                    page.close()
                    _say(
                        f"{mode}: layout {viewport_name} passed in "
                        f"{time.perf_counter() - started:.1f}s")
                    continue
                scope = page.get_by_label("Repository scope")
                scope.click()
                page.get_by_role("option", name=repository_name, exact=True).click()
                page.get_by_text(
                    f"{repository_name} | {directory}", exact=True).wait_for()
                search = page.get_by_label("Search agents or repositories")
                search.fill("readiness")
                table_id = page.locator(".virtualized-agent-table").get_attribute("id")
                page.wait_for_function("window.agentsLiveContinuity !== undefined")
                row_checkbox = page.locator(
                    ".repository-group tbody [role=checkbox]").first
                if row_checkbox.get_attribute("aria-checked") != "true":
                    row_checkbox.click()
                page.get_by_text("1 agents selected", exact=True).wait_for()
                search.fill("no matching agent")
                page.get_by_text("0 of 1 agents", exact=False).wait_for()
                page.get_by_text("1 agents selected", exact=True).wait_for()
                search.fill("readiness")
                page.get_by_text("readiness-agent", exact=True).wait_for()
                if page.locator(".virtualized-agent-table").get_attribute("id") != table_id:
                    raise ReadinessError(f"{mode}: filtering replaced the inventory table")
                row_checkbox = page.locator(
                    ".repository-group tbody [role=checkbox]").first
                try:
                    page.wait_for_function("""() => document.querySelector(
                        '.repository-group tbody [role=checkbox]')
                        ?.getAttribute('aria-checked') === 'true'""")
                except PlaywrightTimeoutError as exc:
                    filtered_state = page.evaluate("""() => ({
                        persisted: JSON.parse(sessionStorage.getItem(
                            'agents-live-dashboard-view') || '{}').settings?.selection || [],
                        rows: Array.from(document.querySelectorAll(
                            '.repository-group tbody tr')).map(row => ({
                                key: row.querySelector('[data-agent-key]')
                                    ?.dataset.agentKey || null,
                                checked: row.querySelector('[role=checkbox]')
                                    ?.getAttribute('aria-checked') || null,
                            })),
                    })""")
                    raise ReadinessError(
                        f"{mode} {width}x{height}: filtered selection did not "
                        f"restore: {filtered_state}") from exc
                row_checkbox.click()
                page.get_by_text("0 agents selected", exact=True).wait_for()
                row_checkbox.click()
                page.get_by_text("1 agents selected", exact=True).wait_for()
                page.get_by_role("button", name="Reverse-sort", exact=True).click()
                page.wait_for_function("() => JSON.parse(sessionStorage.getItem("
                                       "'agents-live-dashboard-view')).settings.descending === true")
                if page.locator(".virtualized-agent-table").get_attribute("id") != table_id:
                    raise ReadinessError(f"{mode}: sorting replaced the inventory table")
                page.get_by_role("button", name="Flat list", exact=True).click()
                page.locator(".repository-heading").wait_for(state="hidden")
                page.get_by_role("button", name="Group by repository", exact=True).click()
                page.locator(".repository-heading").wait_for(state="visible")
                page.wait_for_function("() => document.querySelector("
                    "'.repository-group tbody [role=checkbox]')?.getAttribute('aria-checked') === 'true'")
                table_id = page.locator(".virtualized-agent-table").get_attribute("id")
                page.get_by_role("button", name="Filters", exact=True).click()
                page.get_by_label("State", exact=True).click()
                page.get_by_role("option", name="started", exact=True).click()
                page.get_by_role("button", name="Close filters", exact=True).click()
                state_chip = page.locator(".inventory-filter-status .q-chip").filter(has_text="State: started")
                state_chip.wait_for()
                state_chip.locator(".q-chip__icon--remove").click()
                page.locator(".inventory-filter-status").wait_for(state="hidden")
                page.get_by_role("button", name="Filters", exact=True).click()
                page.get_by_label("State", exact=True).click()
                page.get_by_role("option", name="started", exact=True).click()
                page.get_by_role("button", name="Close filters", exact=True).click()
                state_chip.wait_for()
                splitter = page.get_by_role(
                    "separator", name="Resize-inventory-and-activity")
                splitter.focus()
                page.keyboard.press("End")
                if splitter.get_attribute("aria-valuenow") != "75":
                    raise ReadinessError(
                        f"{mode} {width}x{height}: keyboard split resize failed")
                refresh = page.get_by_role("button", name="Refresh")
                refresh.focus()
                page.keyboard.press("Enter")
                page.get_by_text("manual refresh: Snapshot", exact=False).last.wait_for()
                if page.locator(".virtualized-agent-table").get_attribute("id") != table_id:
                    raise ReadinessError(f"{mode}: refresh replaced the inventory table")
                if search.input_value() != "readiness" \
                        or row_checkbox.get_attribute("aria-checked") != "true":
                    raise ReadinessError(
                        f"{mode} {width}x{height}: refresh lost filter or selection")
                if page.evaluate(
                        "document.activeElement?.getAttribute('aria-label')") != "Refresh":
                    raise ReadinessError(
                        f"{mode} {width}x{height}: refresh did not restore focus")
                persisted = page.evaluate("""() => JSON.parse(sessionStorage.getItem(
                    'agents-live-dashboard-view') || '{}')""")
                if len(persisted.get("settings", {}).get("selection", [])) != 1:
                    raise ReadinessError(
                        f"{mode} {width}x{height}: selection was not persisted "
                        "before reconnect")
                page.reload(wait_until="networkidle")
                page.wait_for_function("window.agentsLiveContinuity !== undefined")
                page.locator(".inventory-filter-status .q-chip").filter(has_text="State: started").wait_for()
                page.get_by_role("button", name="Filters", exact=True).click()
                if page.get_by_label("State", exact=True).input_value() != "started":
                    raise ReadinessError(f"{mode}: reloaded filter control disagrees with saved state")
                page.get_by_role("button", name="Close filters", exact=True).click()
                search = page.get_by_label("Search agents or repositories")
                row_checkbox = page.locator(
                    ".repository-group tbody [role=checkbox]").first
                splitter = page.get_by_role(
                    "separator", name="Resize-inventory-and-activity")
                try:
                    page.wait_for_function("""() => document.querySelector(
                        '.repository-group tbody [role=checkbox]')
                        ?.getAttribute('aria-checked') === 'true'""")
                except PlaywrightTimeoutError as exc:
                    reconnect_state = page.evaluate("""() => ({
                        persisted: JSON.parse(sessionStorage.getItem(
                            'agents-live-dashboard-view') || '{}').settings?.selection || [],
                        rows: Array.from(document.querySelectorAll(
                            '.repository-group tbody tr')).map(row => ({
                                key: row.querySelector('[data-agent-key]')
                                    ?.dataset.agentKey || null,
                                checked: row.querySelector('[role=checkbox]')
                                    ?.getAttribute('aria-checked') || null,
                            })),
                    })""")
                    raise ReadinessError(
                        f"{mode} {width}x{height}: reconnect selection "
                        f"did not restore: {reconnect_state}") from exc
                search_value = search.input_value()
                selected_value = row_checkbox.get_attribute("aria-checked")
                split_value = splitter.get_attribute("aria-valuenow")
                if search_value != "readiness" or selected_value != "true" \
                    or split_value != "75":
                    raise ReadinessError(
                    f"{mode} {width}x{height}: reconnect lost view state "
                    f"(search={search_value!r}, selected={selected_value!r}, "
                    f"split={split_value!r})")
                activity = page.locator(".dashboard-log-panel .activity-log")
                activity.evaluate("""element => {
                    for (let index = 0; index < 40; index += 1) {
                        const line = document.createElement('div');
                        line.textContent = `continuity fixture ${index}`;
                        element.appendChild(line);
                    }
                    element.scrollTop = 0;
                }""")
                if activity.evaluate(
                        "element => element.scrollHeight <= element.clientHeight"):
                    raise ReadinessError(
                        f"{mode} {width}x{height}: activity fixture did not overflow")
                refresh = page.get_by_role("button", name="Refresh")
                refresh.click()
                page.get_by_text("manual refresh: Snapshot", exact=False).last.wait_for()
                if activity.evaluate("element => element.scrollTop") > 4:
                    raise ReadinessError(
                        f"{mode} {width}x{height}: refresh moved non-bottom activity")
                before = body.bounding_box()
                settings_button = page.get_by_role("button", name="Settings")
                settings = page.locator(".dashboard-settings")
                settings.wait_for(state="hidden")
                settings_button.click()
                settings.wait_for(state="visible")
                settings_box = settings.bounding_box()
                if settings_box is None or abs(settings_box["x"]) > 1 \
                        or abs(settings_box["y"]) > 1 \
                        or abs(settings_box["width"] - width) > 1 \
                        or abs(settings_box["height"] - height) > 1:
                    raise ReadinessError(
                        f"{mode} {width}x{height}: settings is not full-screen")
                if settings.get_attribute("aria-modal") != "true":
                    raise ReadinessError(
                        f"{mode} {width}x{height}: settings lacks modal semantics")
                settings.locator(".host-service-panel").wait_for()
                repository_panel = settings.locator(".repository-settings-panel")
                repository_panel.wait_for()
                repository_text = repository_panel.inner_text()
                for expected in (repository_name, str(directory), "Available",
                                 "1 agent definition discovered",
                                 "Default fallback"):
                    if expected not in repository_text:
                        raise ReadinessError(
                            f"{mode} {width}x{height}: settings omitted {expected!r}")
                if body.bounding_box() != before:
                    raise ReadinessError(
                        f"{mode} {width}x{height}: settings resized the dashboard "
                        f"from {before} to {body.bounding_box()}")
                if page.evaluate(
                        "document.documentElement.scrollWidth > window.innerWidth"):
                    raise ReadinessError(
                        f"{mode} {width}x{height}: page scrolls horizontally")
                page.wait_for_function("""() => JSON.parse(sessionStorage.getItem(
                    'agents-live-dashboard-view') || '{}').settingsOpen === true""")
                page.reload(wait_until="networkidle")
                settings = page.locator(".dashboard-settings")
                settings.wait_for(state="visible")
                page.get_by_role("button", name="Close-settings").focus()
                page.keyboard.press("Enter")
                settings.wait_for(state="hidden")
                page.wait_for_function("""() => JSON.parse(sessionStorage.getItem(
                    'agents-live-dashboard-view') || '{}').settingsOpen === false""")
                if search.input_value() != "readiness" or body.bounding_box() != before:
                    raise ReadinessError(
                        f"{mode} {width}x{height}: closing settings lost context")
                try:
                    page.wait_for_function(
                        "document.activeElement?.getAttribute('aria-label') === "
                        "'Settings'",
                        timeout=3000,
                    )
                except PlaywrightTimeoutError:
                    raise ReadinessError(
                        f"{mode} {width}x{height}: focus did not return to Settings")
                page.close()

                _say(
                    f"{mode}: layout and continuity {viewport_name} passed in "
                    f"{time.perf_counter() - started:.1f}s")

            if "repositories" not in scenarios:
                return

            empty = directory / "empty-repository"
            (empty / "Agents").mkdir(parents=True, exist_ok=True)
            page = browser.new_page(viewport={"width": 1280, "height": 720})
            page.goto(f"http://127.0.0.1:{port}/", wait_until="networkidle")
            page.evaluate("window.__repositoryMutationAcceptance = 'retained'")
            scope = page.get_by_label("Repository scope")
            scope.click()
            page.get_by_role("option", name=repository_name, exact=True).click()
            page.get_by_role("button", name="Settings").click()
            page.get_by_label("Repository path").fill(str(empty))
            page.get_by_role("button", name="Register", exact=True).click()
            page.get_by_role("status").get_by_text(
                "Registered empty-repository successfully; discovered 0 agent "
                f"definitions. The current view remains scoped to {repository_name}.",
                exact=True,
            ).wait_for()
            if page.evaluate("window.__repositoryMutationAcceptance") != "retained":
                raise ReadinessError(f"{mode}: registration reloaded the page")
            empty_row = page.locator(".repository-setting-row").filter(
                has=page.get_by_text("empty-repository", exact=True))
            if "0 agent definitions discovered" not in empty_row.inner_text():
                raise ReadinessError(f"{mode}: zero definitions is not explicit")
            page.get_by_role("button", name="Close-settings").click()
            scope.click()
            page.get_by_role("option", name="empty-repository", exact=True).wait_for()
            page.get_by_role("option", name="All", exact=True).click()
            shutil.rmtree(empty)
            page.get_by_role("button", name="Refresh").click()
            page.get_by_text("Stale", exact=True).wait_for()
            page.get_by_text("readiness-agent", exact=True).wait_for()
            page.get_by_role("button", name="Settings").click()
            empty_row = page.locator(".repository-setting-row").filter(
                has=page.get_by_text("empty-repository", exact=True))
            if "Discovery failed" not in empty_row.inner_text():
                raise ReadinessError(f"{mode}: discovery failure is not explicit")
            empty_row.get_by_role("button", name="Unregister").click()
            confirmation = page.get_by_text(
                "This removes only the registry entry.", exact=False)
            confirmation.wait_for()
            page.get_by_role("button", name="Unregister", exact=True).last.click()
            page.get_by_role("status").get_by_text(
                "Repository files, definitions, logs, triggers, and runtime "
                "state were not deleted.", exact=False).wait_for()
            if page.evaluate("window.__repositoryMutationAcceptance") != "retained":
                raise ReadinessError(f"{mode}: unregister reloaded the page")
            page.get_by_role("button", name="Close-settings").click()
            scope.click()
            if page.get_by_role(
                    "option", name="empty-repository", exact=True).count():
                raise ReadinessError(f"{mode}: unregister did not refresh selector")

            registry = directory / "config" / "agents-live" / "config.toml"
            registry_text = registry.read_text(encoding="utf-8")
            registry.write_text("not valid = [", encoding="utf-8")
            page.keyboard.press("Escape")
            page.get_by_role("button", name="Refresh").click()
            page.get_by_text("Data stale:", exact=False).wait_for()
            page.get_by_text("readiness-agent", exact=True).wait_for()
            registry.write_text(registry_text, encoding="utf-8")
            page.get_by_role("button", name="Refresh").click()
            page.locator(".dashboard-health-label").get_by_text(
                "Host ", exact=False).wait_for()
            page.get_by_role("button", name="Settings").click()
            page.get_by_role("dialog").wait_for(state="visible")
            page.keyboard.press("Escape")
            page.get_by_role("dialog").wait_for(state="hidden")

            scale_repositories = directory / "scale-repositories"
            registry_lines = [registry_text.rstrip()]
            for index in range(12):
                scale_repository = scale_repositories / f"repo-{index:02d}"
                skill = scale_repository / "Agents" / "scale-agent"
                skill.mkdir(parents=True)
                skill.joinpath("SKILL.md").write_text(
                    DEFINITION.replace(
                        "name: readiness-agent", "name: scale-agent"),
                    encoding="utf-8",
                )
                registry_lines.append(
                    f'scale{index:02d} = {json.dumps(str(scale_repository))}')
            registry.write_text("\n".join(registry_lines) + "\n", encoding="utf-8")
            page.get_by_role("button", name="Refresh").click()
            page.wait_for_function(
                "document.querySelectorAll('.repository-group').length === 10")
            registered_names = {
                repository_name, *(f"scale{index:02d}" for index in range(12))
            }
            mounted_names = {
                heading.locator(".text-sm.font-medium").inner_text().removesuffix(
                    " (default)")
                for heading in page.locator(".repository-heading").all()
            }
            deferred_names = registered_names - mounted_names
            deferred_selector = page.get_by_label(
                re.compile(r"Show one of 3 more repositories"))
            if len(mounted_names) != 10 or len(deferred_names) != 3 \
                    or mounted_names | deferred_names != registered_names \
                    or deferred_selector.count() != 1:
                raise ReadinessError(
                    f"{mode}: progressive repository rendering mounted "
                    f"{sorted(mounted_names)} and deferred "
                    f"{sorted(deferred_names)} with an invalid selector")
            scope.click()
            for name in sorted(registered_names):
                if page.get_by_role("option", name=name, exact=True).count() != 1:
                    raise ReadinessError(
                        f"{mode}: repository scope omitted {name}")
            page.keyboard.press("Escape")
            deferred_selector.click()
            first_deferred = sorted(deferred_names)[0]
            page.get_by_role(
                "option", name=f"{first_deferred} | 1 agents", exact=True
            ).wait_for()
            for name in sorted(deferred_names):
                if page.get_by_role(
                        "option", name=f"{name} | 1 agents", exact=True
                ).count() != 1:
                    raise ReadinessError(
                        f"{mode}: deferred repository selector omitted {name}")
            selected_deferred = sorted(deferred_names)[-1]
            page.get_by_role(
                "option", name=f"{selected_deferred} | 1 agents",
                exact=True).click()
            page.locator(".repository-group").filter(
                has=page.get_by_text(selected_deferred, exact=True)).wait_for()
            if page.locator(".repository-group").count() != 10:
                raise ReadinessError(
                    f"{mode}: on-demand repository access exceeded mount cap")
            registry.write_text(registry_text, encoding="utf-8")
            shutil.rmtree(scale_repositories)
            page.get_by_role("button", name="Refresh").click()
            page.wait_for_function(
                "document.querySelectorAll('.repository-group').length === 1")

            scale_root = directory / "Agents"
            for index in range(150):
                skill = scale_root / f"scale-{index:03d}"
                skill.mkdir()
                skill.joinpath("SKILL.md").write_text(
                    DEFINITION.replace(
                        "name: readiness-agent", f"name: scale-{index:03d}"),
                    encoding="utf-8",
                )
            page.get_by_role("button", name="Refresh").click()
            page.get_by_text("151 of 151 agents", exact=False).wait_for()
            inventory = page.locator(".agent-table-scroll")
            inventory.evaluate("element => { element.scrollTop = 0; }")
            page.get_by_text("readiness-agent", exact=True).wait_for()
            rendered_rows = page.locator(
                ".virtualized-agent-table [data-agent-key]").count()
            if not 0 < rendered_rows < 151:
                raise ReadinessError(
                    f"{mode}: virtual table mounted {rendered_rows} of 151 rows")
            layout = page.evaluate("""() => {
                const inventory = document.querySelector('.agent-table-scroll');
                const nested = [...inventory.querySelectorAll('*')].filter(element =>
                    /(auto|scroll)/.test(getComputedStyle(element).overflowY)
                    && element.scrollHeight > element.clientHeight);
                const rows = [...inventory.querySelectorAll('[data-agent-key]')]
                    .map(element => element.closest('tr').getBoundingClientRect().height);
                return {nested: nested.length, tallest: Math.max(...rows)};
            }""")
            if layout["nested"] or layout["tallest"] > 40:
                raise ReadinessError(f"{mode}: inventory is not compact: {layout}")
            inventory.evaluate("element => { element.scrollTop = element.scrollHeight; }")
            page.get_by_text("scale-149", exact=True).wait_for()
            page.get_by_role("button", name="Refresh").click()
            page.get_by_text("manual refresh: Snapshot", exact=False).last.wait_for()
            page.get_by_text("scale-149", exact=True).wait_for()
            inventory.evaluate("element => { element.scrollTop = 0; }")
            page.get_by_text("readiness-agent", exact=True).wait_for()
            page.get_by_role("button", name="Reverse-sort").click()
            page.wait_for_function("""() => document.querySelector(
                '.virtualized-agent-table [data-agent-key]')?.textContent.trim()
                === 'scale-149'""")
            inventory.evaluate("element => { element.scrollTop = element.scrollHeight; }")
            last_row = page.get_by_role("row").filter(
                has=page.get_by_text("readiness-agent", exact=True))
            other_page = browser.new_page()
            other_page.goto(f"http://127.0.0.1:{port}/", wait_until="networkidle")
            last_row.get_by_role("button", name="Run this agent once now").click()
            identifier = next(row["identifier"] for row in repositories[0]["rows"]
                              if row["name"] == "readiness-agent")
            try:
                _await_aggregate_run(directory, identifier, mode)
            except ReadinessError as exc:
                raise ReadinessError(
                    f"{exc}\nvisible activity:\n"
                    f"{page.locator('.activity-log').inner_text()}") from exc
            try:
                page.get_by_text("action completion: Snapshot", exact=False).last.wait_for()
            except PlaywrightTimeoutError as exc:
                raise ReadinessError(
                    f"{mode}: missing action completion refresh\n"
                    f"{page.locator('.activity-log').inner_text()}") from exc
            if "completed: Run" in other_page.locator(".activity-log").inner_text():
                raise ReadinessError(f"{mode}: action wrote into another tab's activity")
            other_page.close()
            for index in range(150):
                shutil.rmtree(scale_root / f"scale-{index:03d}")
            page.close()
        finally:
            browser.close()
    _say(f"{mode}: repository lifecycle and scale passed")


def _await_aggregate_run(directory: Path, identifier: str, mode: str) -> None:
    deadline = time.monotonic() + READY_TIMEOUT_S
    while time.monotonic() < deadline:
        for log in directory.rglob("dashboard.jsonl"):
            try:
                records = [
                    json.loads(line) for line in log.read_text(
                        encoding="utf-8").splitlines() if line.strip()
                ]
            except (OSError, json.JSONDecodeError):
                continue
            if any(
                    record.get("event") == "dashboard-action"
                    and record.get("status") == "success"
                    and record.get("repository") == str(directory)
                    and record.get("agent") == identifier
                    and str(record.get("message", "")).startswith("Run:")
                    for record in records):
                return
        time.sleep(POLL_INTERVAL_S)
    raise ReadinessError(
        f"{mode}: aggregate Run produced no repository-qualified evidence")


def _assert_aggregate_run(port: int, directory: Path,
                          payload: dict, mode: str) -> None:
    from playwright.sync_api import expect, sync_playwright

    identifier = payload["agents"][0].get("identifier")
    if not isinstance(identifier, str) or not identifier:
        raise ReadinessError(f"{mode}: fixture row has no canonical identifier")
    all_repos = _api_all_repos(port)
    repository_rows = [
        row
        for repository in (all_repos or {}).get("repositories", [])
        for row in repository.get("rows", [])
        if row.get("identifier") == identifier
    ]
    if len(repository_rows) != 1 or not repository_rows[0].get("can_run"):
        raise ReadinessError(
            f"{mode}: aggregate Run is unavailable: {repository_rows!r}")
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            executable_path=str(_browser_executable()), headless=True)
        try:
            page = browser.new_page(viewport={"width": 1280, "height": 720})
            browser_errors: list[str] = []
            page.on("pageerror", lambda error: browser_errors.append(str(error)))
            page.on(
                "console",
                lambda message: browser_errors.append(message.text)
                if message.type == "error" else None,
            )
            page.goto(f"http://127.0.0.1:{port}/", wait_until="networkidle")
            row = page.get_by_role("row").filter(
                has=page.get_by_text("readiness-agent", exact=True))
            expect(row).to_have_count(1, timeout=15000)
            expect(row).to_be_visible(timeout=15000)
            if row.count() != 1:
                raise ReadinessError(
                    f"{mode}: aggregate dashboard rendered {row.count()} "
                    "fixture rows")
            run_button = row.get_by_role(
                "button", name="Run this agent once now")
            if not run_button.is_enabled():
                raise ReadinessError(
                    f"{mode}: aggregate Run rendered disabled: "
                    f"{run_button.evaluate('element => element.outerHTML')}")
            run_button.click()
            page.wait_for_timeout(500)
            if browser_errors:
                raise ReadinessError(
                    f"{mode}: aggregate Run raised browser errors: "
                    f"{browser_errors!r}")
            _await_aggregate_run(directory, identifier, mode)
        finally:
            browser.close()
    _say(f"{mode}: aggregate Run kept repository-qualified evidence")


def _assert_abortive_disconnect_survives(
        process: subprocess.Popen, port: int, mode: str) -> None:
    if os.name != "nt":
        return
    request = (
        "GET /socket.io/?EIO=4&transport=websocket HTTP/1.1\r\n"
        f"Host: 127.0.0.1:{port}\r\n"
        "Connection: Upgrade\r\n"
        "Upgrade: websocket\r\n"
        "Sec-WebSocket-Version: 13\r\n"
        "Sec-WebSocket-Key: SGVsbG9Xb3JsZDEyMzQ1Ng==\r\n\r\n"
    ).encode("ascii")
    for _ in range(25):
        with socket.socket() as client:
            client.settimeout(2)
            client.setsockopt(
                socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("hh", 1, 0))
            client.connect(("127.0.0.1", port))
            with contextlib.suppress(OSError):
                client.sendall(request)
    payload = _await_rows(process, port, f"{mode} after client resets")
    _assert_row(payload, mode, started=True, expect_failure=False)
    _say(f"{mode}: remained available after abortive client disconnects")


def _terminate(process: subprocess.Popen) -> None:
    """Stop the dashboard and every descendant it spawned.

    The CLI delegates to a script that starts the server, and ``--dev``
    adds a reload worker, so the process to signal is never the one that
    holds the port. POSIX has the process group; Windows needs the tree
    named explicitly, and a survivor there keeps a file handle open,
    which fails the temporary directory rather than the check.
    """
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/T", "/F", "/PID", str(process.pid)],
            capture_output=True, check=False)
    else:
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            process.terminate()
    try:
        process.wait(timeout=SHUTDOWN_GRACE_S)
    except subprocess.TimeoutExpired:
        if os.name != "nt":
            with contextlib.suppress(OSError):
                os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        process.kill()
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(timeout=SHUTDOWN_GRACE_S)


def _check(launcher: list[str], directory: Path, environment: dict[str, str],
    *, dev: bool, source: bool, all_repos: bool = False,
    scenarios: tuple[str, ...] = SCENARIO_ORDER,
    viewport_names: tuple[str, ...] = tuple(VIEWPORTS)) -> None:
    mode = ("source" if source else "packaged") + (" --dev" if dev else "")
    if all_repos:
        mode += " all-repositories"
    port = _free_port()
    argv = [*launcher, "--repo", str(directory), "dashboard",
            "--port", str(port)]
    if all_repos:
        argv.append("--all-repos")
    if dev:
        argv.append("--dev")
    _say(f"{mode}: starting on port {port}")
    process = subprocess.Popen(
        argv, cwd=directory, env=environment,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        **({} if os.name == "nt" else {"start_new_session": True}))
    check_started = time.perf_counter()
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

    try:
        started = time.perf_counter()
        payload = _await_rows(process, port, mode)
        _assert_row(payload, mode, started=True)
        watcher_health = _watcher_health_label(
            payload["agents"][0].get("watcher_liveness"))
        assert watcher_health is not None
        _say(
            f"{mode}: startup served a started row with Stop available in "
            f"{time.perf_counter() - started:.1f}s")
        visual_scenarios = set(scenarios) & {"layout", "continuity"}
        if visual_scenarios:
            _assert_operational_viewport(
                port, directory, mode, visual_scenarios, viewport_names,
                watcher_health)
        if "aggregate" in scenarios:
            started = time.perf_counter()
            _assert_aggregate_run(port, directory, payload, mode)
            _say(
                f"{mode}: aggregate passed in "
                f"{time.perf_counter() - started:.1f}s")
        if "repositories" in scenarios:
            started = time.perf_counter()
            _assert_operational_viewport(
                port, directory, mode, {"repositories"}, (), watcher_health)
            _say(
                f"{mode}: repositories passed in "
                f"{time.perf_counter() - started:.1f}s")
        if "disconnect" in scenarios and not all_repos:
            started = time.perf_counter()
            _assert_abortive_disconnect_survives(process, port, mode)
            _say(
                f"{mode}: disconnect passed in "
                f"{time.perf_counter() - started:.1f}s")
        _say(f"{mode}: completed in {time.perf_counter() - check_started:.1f}s")
    except (ReadinessError, PlaywrightTimeoutError) as exc:
        _terminate(process)
        output = process.stdout.read().strip() if process.stdout else ""
        if output:
            raise ReadinessError(
                f"{exc}\ndashboard output:\n{output[-4000:]}") from exc
        raise
    finally:
        _terminate(process)


def _plan(args: argparse.Namespace) -> dict:
    selected_modes = args.launch_mode
    if selected_modes is None:
        selected_modes = ["normal"] if args.scenario or args.viewport else [
            "normal", "all-repos", "dev"]
    if args.skip_dev:
        selected_modes = [mode for mode in selected_modes if mode != "dev"]
    if not selected_modes:
        raise ReadinessError("no launch modes remain after --skip-dev")

    requested = set(args.scenario or ())
    if args.viewport and not requested & {"layout", "continuity"}:
        raise ReadinessError(
            "--viewport requires the layout or continuity scenario")

    runs = []
    for launch_mode in selected_modes:
        scenarios = (
            tuple(name for name in SCENARIO_ORDER
                  if name == "startup" or name in requested)
            if requested else RELEASE_SCENARIOS[launch_mode]
        )
        if "layout" in scenarios:
            viewports = tuple(args.viewport or VIEWPORTS)
        elif "continuity" in scenarios:
            viewports = tuple(args.viewport or ("desktop",))
        else:
            viewports = ()
        runs.append({
            "mode": launch_mode,
            "scenarios": list(scenarios),
            "viewports": list(viewports),
        })
    return {
        "artifact": "source" if args.editable else "packaged",
        "runs": runs,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    artifact = parser.add_mutually_exclusive_group()
    artifact.add_argument(
        "--editable", action="store_true",
        help="run this checkout instead of the built wheel")
    parser.add_argument(
        "--skip-dev", action="store_true",
        help="skip the reload-worker mode (for a slow CI host)")
    artifact.add_argument(
        "--wheel", type=Path,
        help="validate this exact wheel instead of dist/ for the current version")
    parser.add_argument(
        "--plan", action="store_true",
        help="print the resolved test plan as JSON without running it")
    parser.add_argument(
        "--launch-mode", action="append",
        choices=tuple(RELEASE_SCENARIOS),
        help="run only this server mode; repeat to select multiple modes")
    parser.add_argument(
        "--scenario", action="append", choices=SCENARIO_ORDER,
        help="run only this scenario; repeat to select multiple scenarios")
    parser.add_argument(
        "--viewport", action="append", choices=tuple(VIEWPORTS),
        help="limit layout or continuity checks; repeat to select viewports")
    args = parser.parse_args()

    try:
        plan = _plan(args)
    except ReadinessError as exc:
        parser.error(str(exc))
    if args.plan:
        print(json.dumps(plan, indent=2))
        return 0

    # A Windows handle can outlive the tree kill by a moment; a lingering
    # file must not fail a check that already passed.
    with tempfile.TemporaryDirectory(
            prefix="agents-live-readiness-",
            ignore_cleanup_errors=True) as temp:
        directory = Path(temp).resolve()
        _fixture(directory)
        environment = _environment(directory)
        launcher, python = _launcher(directory, args.editable, args.wheel)
        total_started = time.perf_counter()
        for run in plan["runs"]:
            _seed_started_state(python, directory, environment)
            _check(
                launcher, directory, environment,
                dev=run["mode"] == "dev",
                source=args.editable,
                all_repos=run["mode"] == "all-repos",
                scenarios=tuple(run["scenarios"]),
                viewport_names=tuple(run["viewports"]),
            )
    _say(f"ok in {time.perf_counter() - total_started:.1f}s")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ReadinessError as exc:
        print(f"dashboard readiness failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
