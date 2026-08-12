#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
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
import signal
import socket
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


def _wheel() -> Path:
    version = subprocess.run(
        ["uv", "version", "--short"], cwd=ROOT, capture_output=True,
        text=True, check=True).stdout.strip()
    wheel = ROOT / "dist" / f"agents_live-{version}-py3-none-any.whl"
    if not wheel.is_file():
        raise ReadinessError(
            f"no built wheel for {version}: run `uv build` first ({wheel})")
    return wheel


def _launcher(directory: Path, editable: bool) -> tuple[list[str], list[str]]:
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
    environment = directory / "runtime"
    for command in (
        ["uv", "venv", str(environment)],
        ["uv", "pip", "install", "--python", str(environment), str(_wheel())],
    ):
        completed = subprocess.run(command, capture_output=True, text=True)
        if completed.returncode != 0:
            raise ReadinessError(
                f"could not build the candidate environment: "
                f"{' '.join(command)}\n{completed.stdout}{completed.stderr}")
    windows = os.name == "nt"
    binaries = environment / ("Scripts" if windows else "bin")
    suffix = ".exe" if windows else ""
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
           *, dev: bool) -> None:
    mode = "--dev" if dev else "packaged"
    port = _free_port()
    argv = [*launcher, "--repo", str(directory), "dashboard",
            "--port", str(port)]
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
    args = parser.parse_args()

    # A Windows handle can outlive the tree kill by a moment; a lingering
    # file must not fail a check that already passed.
    with tempfile.TemporaryDirectory(
            prefix="agents-live-readiness-",
            ignore_cleanup_errors=True) as temp:
        directory = Path(temp).resolve()
        _fixture(directory)
        environment = _environment(directory)
        launcher, python = _launcher(directory, args.editable)
        _seed_started_state(python, directory, environment)
        _check(launcher, directory, environment, dev=False)
        if not args.skip_dev:
            _check(launcher, directory, environment, dev=True)
    _say("ok")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ReadinessError as exc:
        print(f"dashboard readiness failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
