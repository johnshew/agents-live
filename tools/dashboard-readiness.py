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
row, its state, and the availability of the actions that act on it. It
repeats the run with ``--dev``, where NiceGUI starts the reload worker as
``__mp_main__`` and imports resolve differently.

    uv run --script tools/dashboard-readiness.py                 # built wheel
    uv run --script tools/dashboard-readiness.py --editable      # this checkout

The fixture is a temporary directory with its own state, data, and config
homes, so the gate never reads the developer's registry or touches a real
project.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
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

DEFINITION = """---
name: readiness-agent
description: Fixture definition for the dashboard readiness gate.
metadata:
  agents-live.schema-version: "1"
  agents-live.selector: "fake/echo"
  agents-live.schedule: "0 8 * * *"
---
Report dashboard readiness.
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
    """A local-only project with one definition.

    Local-only on purpose: an ownership registry is a private plugin, and
    a gate that needed one would only run where that plugin is installed.
    """
    skill = directory / "Agents" / "readiness-agent"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(DEFINITION, encoding="utf-8")
    (directory / ".agents-live.toml").write_text(
        "# readiness fixture\n", encoding="utf-8")
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
from pathlib import Path
from agents_live import agent, state
root = Path(sys.argv[1]).resolve()
state.replace(root, {agent.load("readiness-agent", root=root).identifier})
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


def _assert_row(payload: dict, mode: str, *, started: bool) -> None:
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


def _assert_operational_viewport(port: int, mode: str) -> None:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            executable_path=str(_browser_executable()), headless=True)
        try:
            page = browser.new_page(viewport={"width": 1280, "height": 720})
            page.goto(f"http://127.0.0.1:{port}/", wait_until="networkidle")
            body = page.locator(".dashboard-body")
            body.wait_for(state="visible")
            if body.locator(
                    ".host-service-panel, .repository-settings-panel").count():
                raise ReadinessError(
                    f"{mode}: settings consume the operational viewport")
            agent_box = page.locator(".agent-panel").bounding_box()
            log_box = page.locator(".dashboard-log-panel").bounding_box()
            if agent_box is None or log_box is None:
                raise ReadinessError(
                    f"{mode}: inventory or log is absent from the first viewport")
            if log_box["height"] < 140:
                raise ReadinessError(
                    f"{mode}: log height {log_box['height']:.0f}px cannot show ten lines")
            if agent_box["y"] < 0 or log_box["y"] + log_box["height"] > 720:
                raise ReadinessError(
                    f"{mode}: inventory or log extends below the 1280x720 viewport")
            if page.evaluate(
                    "document.documentElement.scrollHeight > window.innerHeight"):
                raise ReadinessError(
                    f"{mode}: operational view requires page-level scrolling")
            page.get_by_role("button", name="Settings").click()
            page.locator(".dashboard-settings .host-service-panel").wait_for()
            page.locator(".dashboard-settings .repository-settings-panel").wait_for()
            page.close()
            page = browser.new_page(viewport={"width": 390, "height": 844})
            page.goto(f"http://127.0.0.1:{port}/", wait_until="networkidle")
            page.get_by_role("button", name="Settings").click()
            page.wait_for_function("""
                () => {
                    const drawer = document.querySelector('.dashboard-settings');
                    if (!drawer) return false;
                    const box = drawer.getBoundingClientRect();
                    return box.left >= 0 && box.right <= window.innerWidth;
                }
            """)
            drawer_box = page.locator(".dashboard-settings").bounding_box()
            if drawer_box is None or drawer_box["x"] < 0 \
                    or drawer_box["x"] + drawer_box["width"] > 390:
                raise ReadinessError(
                    f"{mode}: settings drawer extends outside a mobile viewport")
            if page.evaluate(
                    "document.documentElement.scrollWidth > window.innerWidth"):
                raise ReadinessError(
                    f"{mode}: settings drawer creates horizontal page scrolling")
        finally:
            browser.close()
    _say(f"{mode}: inventory and log fit the 1280x720 viewport")


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
    from playwright.sync_api import sync_playwright

    identifier = payload["agents"][0].get("identifier")
    if not isinstance(identifier, str) or not identifier:
        raise ReadinessError(f"{mode}: fixture row has no canonical identifier")
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            executable_path=str(_browser_executable()), headless=True)
        try:
            page = browser.new_page(viewport={"width": 1280, "height": 720})
            page.goto(f"http://127.0.0.1:{port}/", wait_until="networkidle")
            row = page.get_by_role("row").filter(
                has=page.get_by_text("readiness-agent", exact=True))
            if row.count() != 1:
                raise ReadinessError(
                    f"{mode}: aggregate dashboard rendered {row.count()} "
                    "fixture rows")
            row.get_by_role(
                "button", name="Run this agent once now").click()
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
    _assert_row(payload, mode, started=True)
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
        *, dev: bool, source: bool, all_repos: bool = False) -> None:
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
    try:
        payload = _await_rows(process, port, mode)
        _assert_row(payload, mode, started=True)
        _say(f"{mode}: served a started row with Stop available")
        if all_repos:
            _assert_aggregate_run(port, directory, payload, mode)
        else:
            _assert_operational_viewport(port, mode)
            _assert_abortive_disconnect_survives(process, port, mode)
    finally:
        _terminate(process)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--editable", action="store_true",
        help="run this checkout instead of the built wheel")
    parser.add_argument(
        "--skip-dev", action="store_true",
        help="skip the reload-worker mode (for a slow CI host)")
    parser.add_argument(
        "--wheel", type=Path,
        help="validate this exact wheel instead of dist/ for the current version")
    args = parser.parse_args()

    # A Windows handle can outlive the tree kill by a moment; a lingering
    # file must not fail a check that already passed.
    with tempfile.TemporaryDirectory(
            prefix="agents-live-readiness-",
            ignore_cleanup_errors=True) as temp:
        directory = Path(temp).resolve()
        _fixture(directory)
        environment = _environment(directory)
        launcher, python = _launcher(directory, args.editable, args.wheel)
        _seed_started_state(python, directory, environment)
        _check(
            launcher, directory, environment, dev=False,
            source=args.editable)
        _check(
            launcher, directory, environment, dev=False,
            source=args.editable, all_repos=True)
        if not args.skip_dev:
            _check(
                launcher, directory, environment, dev=True,
                source=args.editable)
    _say("ok")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ReadinessError as exc:
        print(f"dashboard readiness failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
