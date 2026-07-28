#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["PyYAML", "mcp", "jsonschema"]
# ///
from __future__ import annotations

import argparse
import contextlib
import json
import os
import signal
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from . import hostruntime, paths, preflight, schedules
from .headless import (
    AgentsLiveError,
    AgentTimeoutError,
    HEADLESS_TIMEOUT,
    HEADLESS_TIMEOUT_RETRIES,
    ensure_logs_dir,
    find_watcher_pid,
    load_agent_config,
    logs_root,
    repo_root,
    resolve_agent_command,
    stop_watcher,
    agents_dir,
)

SCRIPT_DIR = Path(__file__).resolve().parent


def _module_argv(module: str) -> list[str]:
    """argv prefix that executes one lifecycle module in either layout.

    Packaged, the module files cannot be run as scripts (their relative
    imports need the package), so re-enter via ``-m``; flat, they are
    plain top-level scripts beside this file.
    """
    if __package__:
        return [sys.executable, "-m", f"{__package__}.{module}"]
    return [sys.executable, str(SCRIPT_DIR / f"{module}.py")]


def lock_path() -> Path:
    """Where the framework smoketest's exclusive lock lives.

    Resolved on use rather than at import: the module has to be
    importable outside a project - the suite imports it to check the
    timeouts agree - while resolving a root there raises, and the
    command that needs the lock reports a missing root in its own
    words.
    """
    return paths.repo_state_dir(repo_root()) / "smoketest-framework.lock"

# Ephemeral fixture files the smoketest's agents and handlers write via
# repo-relative paths. These must stay inside the repository (watch
# paths are validated to; the write-files handler writes relative to
# the repo root), so they cannot follow the logs to the state home.
FIXTURES_REL = "Agents/_smoketest-tmp"
SMOKETEST_BUSY_EXIT = 75
CLEANUP_COMMAND_TIMEOUT_S = 15
# An agent call gets HEADLESS_TIMEOUT per attempt and is retried, so a
# run that succeeds only on the retry can legitimately take every
# attempt's budget. On a high-latency link that is the common case, not
# the exception. Wait for the framework's real worst case plus room for
# dispatch and the post-processor that writes the file being waited on;
# anything less fails work the framework would have completed.
AGENT_RESULT_TIMEOUT_S = (
    HEADLESS_TIMEOUT * (HEADLESS_TIMEOUT_RETRIES + 1) + 60)
# The quiet window the debounce fixture declares, named once so the
# frontmatter it writes and the wait for its result cannot drift apart.
DEBOUNCE_WINDOW_S = 5
SMOKETEST_AGENT_NAMES = (
    "_smoketest-cron",
    "_smoketest-watcher",
    "_smoketest-preprocessor",
    "_smoketest-debounce",
    "_smoketest-spawn-child",
    "_smoketest-pipeline",
)
# The gate brings its own handlers. It used to reach for the project's
# own Agents/handlers/write-files.sh, which assumed a project that had
# one and a host with bash and jq; a fresh project or a Windows host
# failed on the fixture rather than on the framework. These are Python,
# which the framework dispatches through `uv run` (headless
# _build_handler_command), so each carries a PEP 723 header and uv
# provisions the interpreter. Nothing about the handler then depends on
# what `python` happens to mean in a cron or watcher context.
WRITE_FILES_HANDLER = "_smoketest-write-files.py"
SMOKETEST_HANDLER_NAMES = (
    WRITE_FILES_HANDLER,
    "_smoketest-preprocessor-prep.py",
    "_smoketest-preprocessor-post.py",
)

# PEP 723 inline metadata: no dependencies, so `uv run` builds the
# environment from the standard library alone and never reaches the
# network to execute a handler.
HANDLER_PEP723_HEADER = '''\
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
'''

WRITE_FILES_HANDLER_SOURCE = HANDLER_PEP723_HEADER + '''\
"""Smoketest post-processor: write each entry of a JSON "files" array.

Reads the agent's JSON response on stdin and writes every
{"path": ..., "content": ...} entry relative to the repository root,
refusing absolute paths and parent traversal.
"""
import json
import sys
from pathlib import Path, PurePosixPath

raw = sys.stdin.read()
try:
    payload = json.loads(raw)
except json.JSONDecodeError as exc:
    print(f"[handler] ERROR: input is not valid JSON: {exc}", file=sys.stderr)
    print(f"[handler] Raw input (first 500 chars): {raw[:500]}", file=sys.stderr)
    sys.exit(1)

entries = payload.get("files") or []
if not entries:
    print("[handler] WARNING: no files in output", file=sys.stderr)
    print(f"[handler] {payload.get('summary') or 'No summary provided'}",
          file=sys.stderr)
    sys.exit(0)

for entry in entries:
    rel = entry.get("path")
    if not rel:
        print("[handler] WARNING: skipping entry with no path", file=sys.stderr)
        continue
    parts = PurePosixPath(rel.replace("\\\\", "/"))
    if parts.is_absolute() or ".." in parts.parts or (len(rel) > 1 and rel[1] == ":"):
        print(f"[handler] ERROR: rejecting unsafe path: {rel}", file=sys.stderr)
        continue
    target = Path(*parts.parts)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text((entry.get("content") or "") + "\\n", encoding="utf-8")
    print(f"[handler] wrote: {rel}", file=sys.stderr)

summary = payload.get("summary")
if summary:
    print(f"[handler] {summary}", file=sys.stderr)
'''


def _write_smoketest_handlers() -> None:
    """Install the gate's own handlers into the target project."""
    handlers_dir = repo_root() / "Agents" / "handlers"
    handlers_dir.mkdir(parents=True, exist_ok=True)
    (handlers_dir / WRITE_FILES_HANDLER).write_text(
        WRITE_FILES_HANDLER_SOURCE, encoding="utf-8")


class SmokeFailure(RuntimeError):
    pass


class SmokeInterrupted(RuntimeError):
    pass


def _record_lock_owner(runtime: str, model: str) -> None:
    """Write owner metadata into the held lock file for other runs to report."""
    owner = {
        "pid": os.getpid(),
        "host": socket.gethostname(),
        "runtime": runtime,
        "model": model,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    with lock_path().open("r+", encoding="utf-8") as lock_file:
        lock_file.seek(0)
        lock_file.truncate()
        lock_file.write(json.dumps(owner, separators=(",", ":")) + "\n")
        lock_file.flush()
        os.fsync(lock_file.fileno())


def _report_lock_busy() -> None:
    try:
        owner = lock_path().read_text(encoding="utf-8").strip()
    except OSError:
        owner = ""
    preflight.emit_failure(
        "smoketest",
        f"another framework smoketest is running "
        f"({owner or 'owner metadata unavailable'})",
        code="resource_busy")


def _in_vscode_sandbox() -> tuple[bool, str]:
    """Detect whether we're running inside the VS Code chat-tool sandbox.

    The sandbox is a cgroup-confined wrapper that kills every descendant
    when the originating tool call ends, so any daemon we spawn (the
    smoketest's watcher) gets reaped mid-claude-call.  Returns (in_sandbox,
    reason) so the caller can print a clear diagnostic.
    """
    try:
        pid = os.getppid()
        for _ in range(20):
            if pid <= 1:
                break
            try:
                cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", "replace")
            except OSError:
                break
            if "sandbox-runtime" in cmdline or "vscode-sandbox" in cmdline:
                return True, f"sandbox-runtime detected in ancestor pid {pid}"
            try:
                stat = Path(f"/proc/{pid}/status").read_text()
            except OSError:
                break
            for line in stat.splitlines():
                if line.startswith("PPid:"):
                    pid = int(line.split()[1])
                    break
            else:
                break
    except Exception:
        pass
    return False, ""


def _write_verdict(verdict: str, failed_step: str | None,
                   reason: str | None, started_at: float, runtime: str,
                   model: str, category: str | None = None) -> None:
    """Persist a single-line summary of the smoketest run."""
    try:
        path = logs_root() / "smoketest-framework-result.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "verdict": verdict,
            "runtime": runtime,
            "model": model,
            "duration_s": round(time.time() - started_at, 1),
            "failed_step": failed_step,
            "reason": reason,
            "category": category,
        }
        tmp_path = path.with_suffix(".json.tmp")
        tmp_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp_path, path)
    except OSError:
        pass


# A failure category, carried in the verdict so a reader does not have
# to interpret the reason text.
ENVIRONMENT_LIMIT = "environment"
ASSERTION_FAILURE = "assertion"


def exhausted_retries(started_at: float) -> str | None:
    """What a fixture agent gave up on for want of time, or None.

    A step whose agent exhausted its retry budget on timeout failed
    because of the host or the link, not because the framework did the
    wrong thing. No wait budget can rescue it, since the framework has
    already stopped trying. Reported identically to a real defect it
    costs an investigation, or it teaches the reflex of re-running the
    gate until it passes, which is worse (#185). The evidence is the
    terminal timeout row the retry loop writes (#183).
    """
    since = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(started_at))
    for name in SMOKETEST_AGENT_NAMES:
        log_path = logs_root() / f"{name}.log"
        if not log_path.is_file():
            continue
        try:
            lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for raw_line in reversed(lines):
            try:
                entry = json.loads(raw_line)
            except json.JSONDecodeError:
                continue
            if not isinstance(entry, dict):
                continue
            if str(entry.get("ts", "")) < since:
                break
            if (entry.get("level") == "error"
                    and entry.get("error_category") == AgentTimeoutError.category):
                attempts = entry.get("attempts") or entry.get("attempt") or "all"
                timeout_s = entry.get("timeout_s")
                budget = f" of {timeout_s}s each" if timeout_s else ""
                return (f"{name} exhausted {attempts} attempts{budget}; "
                        f"this is a host or link limit, not a smoketest "
                        f"assertion failure")
    return None


def fail(message: str) -> None:
    raise SmokeFailure(message)


def run_status(*args: str) -> str:
    # status.py has no --json flag of its own; JSON mode is the env
    # contract the CLI sets (preflight.JSON_ENV_VAR), so translate the
    # conventional flag into that contract for the direct module child.
    argv = [arg for arg in args if arg != "--json"]
    env = None
    if len(argv) != len(args):
        env = {**os.environ, preflight.JSON_ENV_VAR: "1"}
    completed = subprocess.run(
        [*_module_argv("status"), *argv],
        cwd=repo_root(),
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    if completed.returncode != 0:
        raise SmokeFailure(completed.stderr.strip() or completed.stdout.strip() or "status failed")
    return completed.stdout.strip()


def run_agent(name: str, changed_files: list[str] | None = None) -> str:
    """Execute an agent via run.py and return its stdout."""
    cmd = [*_module_argv("run"), "--name", name]
    if changed_files:
        cmd.extend(["--changed-files", json.dumps(changed_files)])
    completed = subprocess.run(
        cmd, cwd=repo_root(), capture_output=True, text=True, check=False,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise SmokeFailure(f"run.py failed for {name}: {detail}")
    return completed.stdout


def read_agent_output_from_log(
    name: str, *, start_offset: int = 0, require_done: bool = False,
    required_trigger: str | None = None,
) -> str:
    """Read the most recent agent output from the agent's JSONL log."""
    log_path = logs_root() / f"{name}.log"
    if not log_path.is_file():
        raise SmokeFailure(f"No log file found: {log_path.name}")
    agent_output = ""
    eligible_run_ids: set[str] = set()
    pending_outputs: dict[str, str] = {}
    with log_path.open("rb") as log_file:
        log_file.seek(start_offset)
        for raw_line in log_file:
            try:
                entry = json.loads(raw_line)
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            run_id = entry.get("run_id")
            if (
                require_done
                and isinstance(run_id, str)
                and entry.get("phase") == "start"
                and (required_trigger is None or entry.get("trigger") == required_trigger)
            ):
                eligible_run_ids.add(run_id)
            elif entry.get("phase") == "agent" and entry.get("status") == "ok":
                if not require_done:
                    agent_output = entry.get("output", "")
                elif isinstance(run_id, str) and run_id in eligible_run_ids:
                    pending_outputs[run_id] = entry.get("output", "")
            elif (
                require_done
                and isinstance(run_id, str)
                and entry.get("phase") == "done"
            ):
                pending_output = pending_outputs.pop(run_id, "")
                if entry.get("status") == "ok" and pending_output:
                    agent_output = pending_output
    if not agent_output:
        raise SmokeFailure(f"No successful agent output found in {log_path.name}")
    return agent_output


def _is_agent_run(command: str) -> bool:
    """Whether *command* is an agent run, in either invocation form.

    Two forms exist (``headless.cli_invocation``): the flat checkout
    dispatches ``uv run --script .../run.py``, while an installed
    package dispatches the pinned CLI shim with a ``run`` subcommand and
    no script path anywhere on the line. Testing for ``run.py`` alone
    therefore never matches an installed tool - which is the form every
    user of the released package has, so the cleanup this feeds silently
    found nothing there (issue #193).
    """
    return "run.py" in command or "run" in command.split()


def _smoketest_run_pids() -> list[int]:
    """Find live agent-run processes for smoke fixtures by command line.

    ``hostruntime.process_command_lines`` is the host-neutral way to ask
    this: ``/proc`` does not exist on Windows, and a process snapshot
    there carries no arguments, which are what name the agent. The
    fixture names are distinctive enough (``_smoketest-``) that a
    substring match cannot reach a real agent.
    """
    self_pid = os.getpid()
    matches: list[int] = []
    for pid, command in hostruntime.process_command_lines():
        if pid == self_pid or not _is_agent_run(command):
            continue
        if any(f"--name {name}" in command for name in SMOKETEST_AGENT_NAMES):
            matches.append(pid)
    return matches


def _stop_smoketest_processes() -> list[int]:
    """Stop every smoke agent run and its descendants; return survivors.

    ``hostruntime.terminate`` already enumerates and stops a tree the way
    the host requires - process group on POSIX, parent links on Windows -
    so cleanup does not reconstruct one itself.
    """
    pids = _smoketest_run_pids()
    for pid in pids:
        hostruntime.terminate(pid)
    return sorted(pid for pid in pids if hostruntime.is_alive(pid))


# The dashboard is a separate process with heavier dependencies, so give
# it room to resolve and start before calling the probe failed.
DASHBOARD_READY_TIMEOUT_S = 120


def _check_dashboard_lists_agents(expected: list[str]) -> None:
    """Serve the dashboard and read its agent list back over HTTP.

    The page draws over a websocket, so the served HTML never carries an
    agent name and a GET of ``/`` would prove only that a port was
    bound. ``/api/agents`` is the row model the table itself binds to,
    which makes "the dashboard started, resolved this project, and can
    see its agents" a single assertion from outside the process - the
    part no in-process check reaches.
    """
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    process = subprocess.Popen(
        [*_module_argv("cli"), "--repo", str(repo_root()),
         "dashboard", "--port", str(port)],
        cwd=repo_root(), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True,
    )
    url = f"http://127.0.0.1:{port}/api/agents"
    try:
        deadline = time.monotonic() + DASHBOARD_READY_TIMEOUT_S
        payload = None
        while time.monotonic() < deadline:
            if process.poll() is not None:
                output = (process.stdout.read() if process.stdout else "") or ""
                fail(f"Dashboard exited before serving (exit {process.returncode}): "
                     f"{output.strip()[-500:]}")
            try:
                with urllib.request.urlopen(url, timeout=5) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                break
            except (urllib.error.URLError, OSError, json.JSONDecodeError):
                time.sleep(1)
        if payload is None:
            fail(f"Dashboard did not answer {url} within {DASHBOARD_READY_TIMEOUT_S}s")
        print(f"  Served: {url}")
        print(f"  Scope: host={payload.get('host')} repo={payload.get('repo')}")
        if payload.get("repo") != str(repo_root()):
            fail(f"Dashboard scoped to {payload.get('repo')!r}, "
                 f"expected {str(repo_root())!r}")
        listed = {agent.get("name") for agent in payload.get("agents", [])}
        missing = [name for name in expected if name not in listed]
        if missing:
            fail(f"Dashboard agent list missing {missing}; listed {sorted(listed)}")
        for name in expected:
            print(f"  Listed: {name}")
    finally:
        hostruntime.terminate(process.pid)


def cleanup() -> tuple[list[str], list[str]]:
    """Idempotently remove every host resource a framework smoketest can own.

    Returns ``(residue, diagnostics)``. ``residue`` lists resources verified
    to have survived cleanup - the only thing that can break the next run and
    the only thing that should fail a verdict. ``diagnostics`` records cleanup
    commands that errored or timed out on the way; when the residue checks
    come back clean those carry no information about system state.
    """
    residue: list[str] = []
    diagnostics: list[str] = []
    # Ephemeral smoketest agent/handler names use a leading underscore so they
    # match the `Agents/_*` and `Agents/handlers/_*` gitignore patterns and
    # never get caught by git-sync mid-run. Keep all names in this script in
    # sync with .gitignore.
    surviving_pids = _stop_smoketest_processes()
    if surviving_pids:
        residue.append(f"smoketest child processes still active: {surviving_pids}")
    for name in SMOKETEST_AGENT_NAMES:
        try:
            result = subprocess.run(
                [*_module_argv("stop"), "--name", name],
                cwd=repo_root(), check=False, capture_output=True, text=True,
                timeout=CLEANUP_COMMAND_TIMEOUT_S,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            diagnostics.append(f"stop {name}: {exc}")
            continue
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
            diagnostics.append(f"stop {name}: {detail[:200]}")
    # Teardown only stops scheduling; remove smoketest files ourselves
    for name in SMOKETEST_AGENT_NAMES:
        prompt = agents_dir() / f"{name}.md"
        prompt.unlink(missing_ok=True)
        (paths.repo_state_dir(repo_root()) / f"{name}-watch-hashes.json").unlink(
            missing_ok=True)
    trigger_dir = repo_root() / FIXTURES_REL / "_smoketest-watcher"
    trigger_file = trigger_dir / "trigger.txt"
    trigger_file.unlink(missing_ok=True)
    # Clean up debounce test artifacts
    debounce_dir = repo_root() / FIXTURES_REL / "_smoketest-debounce"
    (debounce_dir / "trigger.txt").unlink(missing_ok=True)
    (repo_root() / FIXTURES_REL / "_smoketest-debounce-result.txt").unlink(missing_ok=True)
    # Clean up spawn test artifacts
    (repo_root() / FIXTURES_REL / "_smoketest-spawn-child-result.txt").unlink(missing_ok=True)
    # Clean up pre-processor smoketest artifacts
    for fname in SMOKETEST_HANDLER_NAMES:
        handler = repo_root() / "Agents" / "handlers" / fname
        handler.unlink(missing_ok=True)
    # Also remove legacy non-prefixed names left over from older smoketest runs
    for legacy in ("smoketest-cron", "smoketest-watcher", "smoketest-preprocessor", "smoketest-debounce", "smoketest-spawn-child", "smoketest-pipeline"):
        (agents_dir() / f"{legacy}.md").unlink(missing_ok=True)
    for legacy in ("smoketest-preprocessor-prep.py", "smoketest-preprocessor-post.sh",
                   "_smoketest-preprocessor-post.sh"):
        (repo_root() / "Agents" / "handlers" / legacy).unlink(missing_ok=True)
    # Remove empty smoketest directories
    for d in (trigger_dir, debounce_dir, repo_root() / FIXTURES_REL):
        try:
            d.rmdir()
        except OSError:
            pass
    remaining_pids = _smoketest_run_pids()
    if remaining_pids:
        residue.append(f"smoketest child processes remain after cleanup: {remaining_pids}")
    active_agents = [
        name for name in SMOKETEST_AGENT_NAMES
        if schedules.is_active(name) or find_watcher_pid(name) is not None
    ]
    if active_agents:
        residue.append(f"smoketest runtime state remains after cleanup: {active_agents}")
    remaining_files = [
        str(path.relative_to(repo_root()))
        for path in [
            *(agents_dir() / f"{name}.md" for name in SMOKETEST_AGENT_NAMES),
            *(repo_root() / "Agents" / "handlers" / name for name in SMOKETEST_HANDLER_NAMES),
        ]
        if path.exists()
    ]
    if remaining_files:
        residue.append(f"smoketest files remain after cleanup: {remaining_files}")
    return residue, diagnostics


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime", default="claude")
    parser.add_argument("--model", default=None, help="Model to use (default: sonnet for claude, claude-haiku-4.5 for copilot)")
    args = parser.parse_args()
    started_at = time.time()
    model_for_verdict = args.model or ("sonnet" if args.runtime in ("claude", "agency claude") else "claude-haiku-4.5")

    # This test creates, activates, runs, and deletes agents inside the
    # project it targets, and dispatches a real agent runtime there.
    # Reaching that project by fallback - the configured default or the
    # global workspace - means acting on whichever project the host
    # happens to point at, which on a developer machine is a real one.
    # Only an intentional pointer counts: --repo, the environment
    # variable, or a marker at or above the working directory.
    target = repo_root()
    reached_by = paths.resolution_source()
    if reached_by in ("default", "global"):
        how = ("the configured default project" if reached_by == "default"
               else "the host-global workspace")
        preflight.emit_failure(
            "smoketest",
            f"refusing to run against {target}: it was reached through "
            f"{how} rather than being named. This test creates, activates, "
            "runs, and deletes agents in its target; name one with "
            "`agents-live --repo <path> smoketest`, or run from inside it",
            code="unnamed_target")
        return 1

    smoketest_lock = contextlib.ExitStack()
    try:
        smoketest_lock.enter_context(
            hostruntime.exclusive_lock(lock_path()))
    except hostruntime.LockBusy:
        _report_lock_busy()
        return SMOKETEST_BUSY_EXIT
    _record_lock_owner(args.runtime, model_for_verdict)

    # Windows has no SIGHUP; SIGTERM exists on both platforms.
    handled_signals = tuple(
        getattr(signal, name)
        for name in ("SIGTERM", "SIGHUP")
        if hasattr(signal, name)
    )
    previous_handlers = {signum: signal.getsignal(signum) for signum in handled_signals}

    def interrupt_handler(signum: int, _frame: object) -> None:
        raise SmokeInterrupted(f"received {signal.Signals(signum).name}")

    for signum in handled_signals:
        signal.signal(signum, interrupt_handler)

    result = 1
    current_step = "preflight cleanup"
    try:
        stale_residue, stale_diagnostics = cleanup()
        for diagnostic in stale_diagnostics:
            print(f"WARNING: preflight cleanup: {diagnostic}", file=sys.stderr)
        if stale_residue:
            reason = "; ".join(stale_residue)
            preflight.emit_failure(
                "smoketest", f"stale smoketest cleanup failed: {reason}",
                code="cleanup_failed")
            _write_verdict("FAIL", failed_step=current_step, reason=reason[:500],
                           started_at=started_at, runtime=args.runtime, model=model_for_verdict)
        else:
            _write_verdict("RUNNING", failed_step=None, reason=None,
                           started_at=started_at, runtime=args.runtime, model=model_for_verdict)
            result = _run_locked(args, started_at, model_for_verdict)
    except (KeyboardInterrupt, SmokeInterrupted) as exc:
        reason = str(exc) or "interrupted"
        preflight.emit_failure(
            "smoketest", f"{reason}; cleaning up", code="interrupted")
        _write_verdict("INTERRUPTED", failed_step="interrupted", reason=reason,
                       started_at=started_at, runtime=args.runtime, model=model_for_verdict)
        result = 130
    except Exception as exc:
        reason = f"unexpected {type(exc).__name__}: {exc}"
        preflight.emit_failure("smoketest", reason)
        _write_verdict("FAIL", failed_step="unexpected exception", reason=reason[:500],
                       started_at=started_at, runtime=args.runtime, model=model_for_verdict)
        result = 1
    finally:
        # Once stop starts, repeated terminal signals must not interrupt it.
        sigint_handler = signal.getsignal(signal.SIGINT)
        for signum in (*handled_signals, signal.SIGINT):
            signal.signal(signum, signal.SIG_IGN)
        try:
            final_residue, final_diagnostics = cleanup()
            for diagnostic in final_diagnostics:
                print(f"WARNING: final cleanup: {diagnostic}", file=sys.stderr)
            if final_residue:
                reason = "; ".join(final_residue)
                preflight.emit_failure(
                    "smoketest", f"final smoketest cleanup failed: {reason}",
                    code="cleanup_failed")
                _write_verdict("FAIL", failed_step="final cleanup", reason=reason[:500],
                               started_at=started_at, runtime=args.runtime, model=model_for_verdict)
                result = 1
        finally:
            signal.signal(signal.SIGINT, sigint_handler)
            for signum, handler in previous_handlers.items():
                signal.signal(signum, handler)
            smoketest_lock.close()
    if preflight.json_mode():
        try:
            verdict = json.loads(
                (logs_root() / "smoketest-framework-result.json").read_text(
                    encoding="utf-8"))
            print(json.dumps(verdict))
        except (OSError, json.JSONDecodeError):
            pass
    return result


def _run_locked(args: argparse.Namespace, started_at: float, model_for_verdict: str) -> int:
    """Run the framework test while the caller owns the exclusive lock."""

    # Refuse to run inside the VS Code chat-tool sandbox: cgroup stop
    # kills the watcher daemon mid-claude-call, so steps 6/11/12 never
    # complete and the failure mode is opaque.
    in_sandbox, sandbox_reason = _in_vscode_sandbox()
    if in_sandbox:
        msg = (f"refusing to run inside VS Code sandbox ({sandbox_reason}); "
               "the watcher daemon is killed when the tool call ends; rerun "
               "with requestUnsandboxedExecution=true, or whitelist this "
               "command via chat.tools.terminal.autoApprove")
        preflight.emit_failure(
            "smoketest", msg, code="sandbox_unsupported")
        _write_verdict("FAIL", failed_step="preflight", reason=sandbox_reason,
                       started_at=started_at, runtime=args.runtime, model=model_for_verdict)
        return 1

    # Pre-flight: fail fast if environment can't support the smoketest.
    # Avoids burning the watcher wait budget in sandboxed environments.
    # Only a crontab host watches through inotifywait; a Task Scheduler
    # host watches in-process and has nothing external to probe.
    if hostruntime.native_scheduler() == hostruntime.CRONTAB:
        if not shutil.which("inotifywait"):
            preflight.emit_failure(
                "smoketest",
                "inotifywait not found; install it with "
                "`sudo apt install inotify-tools`",
                code="dependency_missing")
            _write_verdict("FAIL", failed_step="preflight", reason="inotifywait missing",
                           started_at=started_at, runtime=args.runtime, model=model_for_verdict)
            return 1
        # Quick inotify syscall check (sandbox may block even if binary exists).
        # `-t 1` so inotifywait exits cleanly after 1s with code 2; `-t 0` means
        # wait indefinitely per the manpage, which would just hit our subprocess
        # timeout and falsely report sandboxing.
        try:
            probe = subprocess.run(
                ["inotifywait", "-t", "1", "-e", "close_write", "/dev/null"],
                capture_output=True, timeout=5,
            )
            # exit code 2 = timeout (expected), 0 = event detected, both fine
            if probe.returncode not in (0, 1, 2):
                msg = f"inotifywait unusable (exit {probe.returncode})"
                preflight.emit_failure(
                    "smoketest",
                    f"{msg}; this smoketest requires unsandboxed execution",
                    code="sandbox_unsupported")
                _write_verdict("FAIL", failed_step="preflight", reason=msg,
                               started_at=started_at, runtime=args.runtime, model=model_for_verdict)
                return 1
        except (subprocess.TimeoutExpired, OSError) as e:
            preflight.emit_failure(
                "smoketest",
                f"inotifywait blocked ({e}); this smoketest requires "
                "unsandboxed execution",
                code="sandbox_unsupported")
            _write_verdict("FAIL", failed_step="preflight", reason=f"inotifywait blocked: {e}",
                           started_at=started_at, runtime=args.runtime, model=model_for_verdict)
            return 1

    model = args.model or ("sonnet" if args.runtime in ("claude", "agency claude") else "claude-haiku-4.5")

    cron_name = "_smoketest-cron"
    watcher_name = "_smoketest-watcher"
    trigger_dir = repo_root() / FIXTURES_REL / watcher_name
    trigger_file = trigger_dir / "trigger.txt"
    current_step = "preflight"

    print(f"Using runtime: {args.runtime}, model: {model}")

    try:
        current_step = "1/14 create cron agent"
        print(f"[1/14] Creating test cron agent \"{cron_name}\"...")
        ensure_logs_dir()
        _write_smoketest_handlers()
        (repo_root() / "Agents" / f"{cron_name}.md").write_text(
            "\n".join(
                [
                    "---",
                    f"runtime: {args.runtime}",
                    f"model: {model}",
                    "mode: plan",
                    f"post-processor: {WRITE_FILES_HANDLER}",
                    'schedule: "0 0 */3 * *"',
                    "---",
                    "",
                    "# Smoketest Cron Agent",
                    "",
                    "This is a local repository smoketest.",
                    "Do not call tools or write files yourself. The post-processor writes",
                    "the file from the JSON response after you finish.",
                    "Follow the steps below and return the required JSON exactly.",
                    "",
                    "## Steps",
                    "",
                    "1. Read this prompt completely",
                    "2. Build the JSON object shown below",
                    '3. Keep the magic field exactly "xyzzy"',
                    "4. Keep the file entry exactly as shown",
                    "5. Output the JSON object only",
                    "",
                    "## Required output",
                    "",
                    f'{{"files":[{{"path":"{FIXTURES_REL}/_smoketest-watcher/trigger.txt","content":"smoketest-trigger-fired"}}],"summary":"Smoketest trigger file written","magic":"xyzzy"}}',
                ]
            ),
            encoding="utf-8",
        )
        print(f"  Created: Agents/{cron_name}.md")

        print("")
        current_step = "2/14 create watcher agent"
        print(f"[2/14] Creating test watcher agent \"{watcher_name}\"...")
        trigger_dir.mkdir(parents=True, exist_ok=True)
        (repo_root() / "Agents" / f"{watcher_name}.md").write_text(
            "\n".join(
                [
                    "---",
                    f"runtime: {args.runtime}",
                    f"model: {model}",
                    "mode: plan",
                    f"watchPath: {FIXTURES_REL}/{watcher_name}/",
                    "---",
                    "",
                    "# Smoketest Watcher Agent",
                    "",
                    "This is a local repository smoketest.",
                    "Follow the steps below and return the required JSON exactly.",
                    "",
                    "## Steps",
                    "",
                    f"1. Read the file `{FIXTURES_REL}/_smoketest-watcher/trigger.txt`",
                    '2. Verify its content is "smoketest-trigger-fired"',
                    '3. Build the JSON object shown below, setting status to "pass" or "fail"',
                    '4. Keep the "trigger" and "magic" fields exactly as shown',
                    "5. Output the JSON object only",
                    "",
                    "## Required output",
                    "",
                    '{"status":"pass","trigger":"smoketest-trigger-fired","magic":"xyzzy"}',
                ]
            ),
            encoding="utf-8",
        )
        print(f"  Created: Agents/{watcher_name}.md")

        print("")
        current_step = "3/14 verify status"
        print("[3/14] Verifying status reads frontmatter correctly...")
        cron_status = json.loads(run_status("--json", cron_name))
        print("  Cron agent status JSON:")
        print(f"  {json.dumps(cron_status, separators=(',', ':'))}")
        if cron_status.get("type") != "cron":
            fail(f"Expected type=cron, got {cron_status.get('type')}")
        if cron_status.get("runtime") != args.runtime:
            fail(f"Expected runtime={args.runtime}")
        if cron_status.get("mode") != "plan":
            fail("Expected mode=plan")
        if cron_status.get("state") != "stopped":
            fail("Expected state=stopped (not yet activated)")
        # Status reports paths in the host's own separator, so compare on a
        # normalized form rather than asserting a POSIX-shaped string.
        post_processor = (cron_status.get("post-processor") or "").replace("\\", "/")
        expected_pp = f"Agents/handlers/{WRITE_FILES_HANDLER}"
        if post_processor != expected_pp:
            fail(f"Expected post-processor={expected_pp}, got "
                 f"{cron_status.get('post-processor')!r}")
        print("  Cron agent: fields verified")

        watcher_status = json.loads(run_status("--json", watcher_name))
        if watcher_status.get("type") != "watcher":
            fail("Expected type=watcher")
        wp = watcher_status.get("watchPath")
        expected_wp = f"{FIXTURES_REL}/{watcher_name}/"
        if wp != [expected_wp] and wp != expected_wp:
            fail(f"Expected watchPath=[{expected_wp!r}], got {wp!r}")
        print("  Watcher agent: fields verified")

        all_status = json.loads(run_status("--json"))
        agent_count = sum(
            1 for agent in all_status.get("agents", []) if agent.get("name") in {cron_name, watcher_name}
        )
        if agent_count != 2:
            fail(f"Expected 2 smoketest agents in status, got {agent_count}")
        print("  All-agent status: both agents present")

        cron_config = load_agent_config(cron_name)
        resolved = resolve_agent_command(cron_config.name)
        if args.runtime in {"claude", "agency claude"} and (
            "--permission-mode default" not in resolved or "--allowedTools" not in resolved
        ):
            fail("Missing read-only plan-mode flags (--permission-mode default + --allowedTools)")
        if args.runtime in {"copilot", "agency copilot"} and "--deny-tool shell" not in resolved:
            fail("Missing deny-tool flags")
        print("  Command resolution: OK")
        print("  Status checks: PASS")

        print("")
        current_step = "4/14 activate watcher"
        print(f"[4/14] Activating watcher via activate.py for \"{watcher_name}\"...")
        watcher_log = logs_root() / f"{watcher_name}.log"
        try:
            watcher_log_offset = watcher_log.stat().st_size
        except FileNotFoundError:
            watcher_log_offset = 0
        if (hostruntime.native_scheduler() == hostruntime.CRONTAB
                and not shutil.which("inotifywait")):
            fail("inotifywait not found. Install with: sudo apt install inotify-tools")
        activate_result = subprocess.run(
            [*_module_argv("activate"), "--name", watcher_name],
            cwd=repo_root(), capture_output=True, text=True, check=False,
        )
        if activate_result.returncode != 0:
            fail(f"activate.py failed: {activate_result.stderr.strip() or activate_result.stdout.strip()}")
        print(f"  {activate_result.stdout.strip()}")
        watcher_pid = find_watcher_pid(watcher_name)
        if watcher_pid is None:
            fail("Watcher process not found after activation")
        print(f"  Watcher confirmed running (pid: {watcher_pid})")

        print("")
        current_step = "5/14 run cron agent"
        print(f"[5/14] Running cron agent via run.py (simulates cron trigger)...")
        print(f"  Invoking: run.py --name {cron_name}")
        run_output = run_agent(cron_name)
        for line in run_output.strip().splitlines():
            print(f"  {line}")
        cron_output = read_agent_output_from_log(cron_name)
        print(f"  Agent output from log ({len(cron_output.encode('utf-8'))} bytes)")
        cron_json = json.loads(cron_output)
        if cron_json.get("magic") != "xyzzy":
            fail("Agent output missing magic:xyzzy - JSON may be truncated or wrong")
        print("  Valid JSON with magic:xyzzy verified")
        if not trigger_file.is_file():
            fail("Handler did not write trigger file")
        if trigger_file.read_text(encoding="utf-8").strip() != "smoketest-trigger-fired":
            fail("Trigger content mismatch")
        print("  Trigger file verified")

        print("")
        current_step = "6/14 watcher dispatch"
        print("[6/14] Waiting for watcher to detect change and run agent...")
        # The watcher (started via activate.py) should detect the trigger file
        # written by step 5's handler and automatically invoke run.py.
        max_wait = AGENT_RESULT_TIMEOUT_S
        poll_interval = 3
        waited = 0
        watcher_output = ""
        while waited < max_wait:
            if watcher_log.is_file():
                try:
                    watcher_output = read_agent_output_from_log(
                        watcher_name,
                        start_offset=watcher_log_offset,
                        require_done=True,
                        required_trigger="file-change",
                    )
                except SmokeFailure:
                    pass
                if watcher_output:
                    break
            time.sleep(poll_interval)
            waited += poll_interval
            if waited % 15 == 0:
                print(f"  Still waiting... ({waited}s)")
        if not watcher_output:
            fail(f"Watcher did not produce agent output within {max_wait}s")
        print(f"  Watcher fired automatically after {waited}s")
        print(f"  Agent output from log: {watcher_output.splitlines()[0]}")
        watcher_json = json.loads(watcher_output)
        if watcher_json.get("magic") != "xyzzy":
            fail("Watcher output missing magic:xyzzy")
        if watcher_json.get("status") != "pass":
            fail("Watcher reported status != pass")
        print("  Watcher confirmed: status=pass, magic=xyzzy")

        print("")
        current_step = "7/14 confirm outputs"
        print("[7/14] Confirming outputs...")
        if not trigger_file.is_file():
            fail("Trigger file missing after run")
        if trigger_file.read_text(encoding="utf-8").strip() != "smoketest-trigger-fired":
            fail("Trigger file content wrong")
        print("  Trigger file: OK")
        cron_log = logs_root() / f"{cron_name}.log"
        if not cron_log.is_file():
            fail("Agent log missing - run.py should have created it")
        print(f"  Agent log: {cron_log.name} exists")
        watcher_log_file = logs_root() / f"{watcher_name}.log"
        if not watcher_log_file.is_file():
            fail("Watcher log missing - watcher should have created it")
        print(f"  Watcher log: {watcher_log_file.name} exists")
        print("  Logs: OK")
        table_output = run_status()
        if cron_name not in table_output:
            fail("Cron agent missing from status table")
        if watcher_name not in table_output:
            fail("Watcher agent missing from status table")
        print("  Status table:")
        for line in table_output.splitlines():
            print(f"    {line}")
        print("  Output confirmation: PASS")

        print("")
        current_step = "8/14 dashboard http"
        print("[8/14] Serving the dashboard and reading its agent list over HTTP...")
        _check_dashboard_lists_agents([cron_name, watcher_name])
        print("  Dashboard: PASS")

        print("")
        current_step = "9/14 pre-processor pipeline"
        print("[9/14] Validating pre-processor -> post-processor pipeline (agent: none)...")
        preprocessor_name = "_smoketest-preprocessor"
        handlers_dir = repo_root() / "Agents" / "handlers"
        handlers_dir.mkdir(parents=True, exist_ok=True)

        # Create a Python pre-processor that outputs structured JSON
        pre_processor_path = handlers_dir / "_smoketest-preprocessor-prep.py"
        pre_processor_path.write_text(
            HANDLER_PEP723_HEADER +
            'import json, sys\n'
            'print(json.dumps({"magic": "pre-xyzzy", "data": [1, 2, 3], "skip": False}))\n',
            encoding="utf-8",
        )

        # Create a post-processor that reads pre-processor output from stdin
        # and verifies the magic value
        post_processor_path = handlers_dir / "_smoketest-preprocessor-post.py"
        post_processor_path.write_text(
            HANDLER_PEP723_HEADER +
            'import sys\n'
            'data = sys.stdin.read()\n'
            'if "pre-xyzzy" in data:\n'
            '    print("post-processor: received pre-processor data with magic")\n'
            'else:\n'
            '    print("post-processor: MISSING pre-processor data", file=sys.stderr)\n'
            '    sys.exit(1)\n',
            encoding="utf-8",
        )
        post_processor_path.chmod(0o755)

        (repo_root() / "Agents" / f"{preprocessor_name}.md").write_text(
            "\n".join([
                "---",
                "runtime: none",
                "pre-processor: _smoketest-preprocessor-prep.py",
                "post-processor: _smoketest-preprocessor-post.py",
                'schedule: "0 0 1 1 *"',
                "---",
                "",
                "# Smoketest Pre-processor Agent",
                "",
                "Validates the pre-processor -> post-processor pipeline with agent: none.",
            ]),
            encoding="utf-8",
        )
        print(f"  Created: pre-processor ({pre_processor_path.name}), post-processor ({post_processor_path.name}), agent ({preprocessor_name}.md)")

        # Clear any stale log from previous runs
        preprocessor_log = logs_root() / f"{preprocessor_name}.log"
        preprocessor_log.unlink(missing_ok=True)

        preprocessor_output = run_agent(preprocessor_name)
        print(f"  run.py output:")
        for line in preprocessor_output.strip().splitlines():
            print(f"    {line}")

        # Verify pre-processor phase was logged
        preprocessor_log = logs_root() / f"{preprocessor_name}.log"
        if not preprocessor_log.is_file():
            fail("Pre-processor agent log not created")
        pre_phase_found = False
        post_phase_found = False
        for line in preprocessor_log.read_text(encoding="utf-8").strip().splitlines():
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if entry.get("phase") == "pre-processor" and entry.get("status") == "ok":
                pre_phase_found = True
                pre_output = entry.get("output", "")
                if "pre-xyzzy" not in pre_output:
                    fail("Pre-processor log output missing magic value")
            if entry.get("phase") == "post-processor" and entry.get("status") == "ok":
                post_phase_found = True
        if not pre_phase_found:
            fail("No pre-processor phase logged in JSONL")
        if not post_phase_found:
            fail("No post-processor phase logged in JSONL")
        print("  Pre-processor logged: OK")
        print("  Post-processor received pre-processor data: OK")
        print("  pre-processor -> post-processor (agent: none): PASS")

        print("")
        current_step = "10/14 skip gating"
        print("[10/14] Validating pre-processor skip gating...")
        # Rewrite the pre-processor to output skip: true
        pre_processor_path.write_text(
            'import json\n'
            'print(json.dumps({"skip": True, "reason": "smoketest-skip"}))\n',
            encoding="utf-8",
        )
        # Clear the log for a fresh run
        preprocessor_log.unlink(missing_ok=True)
        skip_output = run_agent(preprocessor_name)
        print(f"  run.py output:")
        for line in skip_output.strip().splitlines():
            print(f"    {line}")

        # Verify skip was logged and post-processor was NOT called
        skip_logged = False
        post_after_skip = False
        for line in preprocessor_log.read_text(encoding="utf-8").strip().splitlines():
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if entry.get("phase") == "pre-processor" and entry.get("skip") is True:
                skip_logged = True
            if entry.get("phase") == "post-processor":
                post_after_skip = True
        if not skip_logged:
            fail("Pre-processor skip not logged")
        if post_after_skip:
            fail("Post-processor ran despite pre-processor skip=true")
        print("  Skip gating: PASS")

        print("")
        current_step = "11/14 pipeline MCP"
        print("[11/14] Validating mode: pipeline routes agent through PipelineMcp...")
        # mode: pipeline is the framework's in-process MCP side-channel. The
        # agent below has no MCPs of its own; the framework brings up
        # PipelineMcp, injects --mcp-config / --additional-mcp-config, and the
        # agent must reach the `put` / `get` tools exposed
        # by that server. Liveness convention: put then get '/ping'. We
        # verify by scanning the JSONL agent log for those ops.
        pipeline_name = "_smoketest-pipeline"
        pipeline_value = "pipeline-xyzzy"
        pipeline_path = "/ping"
        pipeline_log = logs_root() / f"{pipeline_name}.log"
        pipeline_log.unlink(missing_ok=True)
        (agents_dir() / f"{pipeline_name}.md").write_text(
            "\n".join([
                "---",
                f"runtime: {args.runtime}",
                f"model: {model}",
                "mode: pipeline",
                'schedule: "0 0 1 1 *"',
                "timeout: 120",
                "---",
                "",
                "# Smoketest Pipeline-Mode Agent",
                "",
                "Do the following in order, using the `pipeline` MCP server:",
                "",
                f'1. Call `put` with `path="{pipeline_path}"` and',
                f'   `value="{pipeline_value}"`.',
                f'2. Call `get` with `path="{pipeline_path}"` and',
                f'   confirm the returned `value` is exactly `{pipeline_value}`.',
                '3. Output exactly the JSON object `{"status":"ok"}` and stop.',
                "   Do not output anything else.",
            ]),
            encoding="utf-8",
        )
        print(f"  Created: {pipeline_name}.md (mode: pipeline, runtime: {args.runtime})")

        pipeline_output = run_agent(pipeline_name)
        for line in pipeline_output.strip().splitlines():
            print(f"  {line}")
        agent_out = read_agent_output_from_log(pipeline_name).strip()
        if '"status"' not in agent_out or '"ok"' not in agent_out:
            fail(f"Pipeline-mode agent output not {{'status':'ok'}}: {agent_out[:200]!r}")
        puts_matched = 0
        gets_matched = 0
        final_puts = None
        final_gets = None
        for line in pipeline_log.read_text(encoding="utf-8").strip().splitlines():
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if entry.get("component") != "pipeline-mcp":
                continue
            op = entry.get("op")
            if op == "put" and entry.get("path") == pipeline_path and entry.get("value") == pipeline_value:
                puts_matched += 1
            elif op == "get" and entry.get("path") == pipeline_path and entry.get("present") is True:
                gets_matched += 1
            elif op == "final-state":
                final_puts = entry.get("puts")
                final_gets = entry.get("gets")
        if puts_matched < 1:
            fail(
                f"PipelineMcp recorded no put on {pipeline_path!r} with value "
                f"{pipeline_value!r} (framework wiring failed to route agent through pipeline server)"
            )
        if gets_matched < 1:
            fail(
                f"PipelineMcp recorded no get on {pipeline_path!r} (agent did "
                "not complete the put/get liveness handshake)"
            )
        print(
            f"  PipelineMcp recorded puts_matched={puts_matched}, "
            f"gets_matched={gets_matched}; "
            f"final_state puts={final_puts} gets={final_gets}"
        )
        # Cleanup pipeline test artifacts
        subprocess.run(
            [*_module_argv("stop"), "--name", pipeline_name],
            cwd=repo_root(), check=False, capture_output=True, text=True,
        )
        (agents_dir() / f"{pipeline_name}.md").unlink(missing_ok=True)
        print("  mode: pipeline (PipelineMcp side-channel): PASS")

        print("")
        current_step = "12/14 spawn module"
        print("[12/14] Validating spawn module (detached dispatch)...")
        # Tests the shared spawn utility from its portable location:
        # - find_uv() resolves the binary in any context
        # - spawn_agent() launches a detached child (start_new_session)
        #   that survives the caller and completes
        from .spawn import find_uv, spawn_agent

        uv_path = find_uv()
        print(f"  find_uv() -> {uv_path}")
        if not Path(uv_path).is_file():
            fail(f"find_uv() returned non-existent path: {uv_path}")

        # Create a minimal agent for spawn to invoke
        spawn_agent_name = "_smoketest-spawn-child"
        spawn_result_file = repo_root() / FIXTURES_REL / f"{spawn_agent_name}-result.txt"
        spawn_result_file.unlink(missing_ok=True)
        (agents_dir() / f"{spawn_agent_name}.md").write_text(
            "\n".join([
                "---",
                f"runtime: {args.runtime}",
                f"model: {model}",
                "mode: plan",
                f"post-processor: {WRITE_FILES_HANDLER}",
                'schedule: "0 0 1 1 *"',
                "---",
                "",
                "# Smoketest Spawn Child",
                "",
                "Do not call tools or write files yourself. The post-processor writes",
                "the file from the JSON response after you finish.",
                "Output exactly this JSON:",
                "",
                f'{{"files":[{{"path":"{FIXTURES_REL}/{spawn_agent_name}-result.txt","content":"spawn-passed"}}],"summary":"Spawn child completed","magic":"spawn-xyzzy"}}',
            ]),
            encoding="utf-8",
        )

        proc = spawn_agent(
            repo_root(), spawn_agent_name, ["smoketest-spawn-trigger.md"],
            quiet=True,
        )
        if proc is None:
            fail("spawn_agent() returned None - check spawn-stderr.log")
        print(f"  Spawned child PID={proc.pid}")

        # Wait for the child to produce output
        spawn_log = logs_root() / f"{spawn_agent_name}.log"
        max_wait = AGENT_RESULT_TIMEOUT_S
        poll_interval = 3
        waited = 0
        while waited < max_wait:
            if spawn_result_file.is_file():
                break
            time.sleep(poll_interval)
            waited += poll_interval
            if waited % 15 == 0:
                print(f"  Still waiting... ({waited}s)")
        if not spawn_result_file.is_file():
            log_tail = ""
            if spawn_log.is_file():
                log_tail = spawn_log.read_text(encoding="utf-8").strip()[-500:]
            fail(f"Spawn child did not produce result within {max_wait}s. Log tail: {log_tail}")
        content = spawn_result_file.read_text(encoding="utf-8").strip()
        if content != "spawn-passed":
            fail(f"Spawn result content wrong: {content!r}")
        print(f"  Child completed ({waited}s), result verified")

        # Cleanup spawn test artifacts
        spawn_result_file.unlink(missing_ok=True)
        subprocess.run([*_module_argv("stop"), "--name", spawn_agent_name],
                       cwd=repo_root(), check=False, capture_output=True, text=True)
        (agents_dir() / f"{spawn_agent_name}.md").unlink(missing_ok=True)
        print("  Spawn module (detached dispatch): PASS")

        print("")
        current_step = "13/14 debounced dispatch"
        print("[13/14] Validating debounced watcher dispatch (in-process quiet window)...")
        # This tests the full debounce path: watcher fires, but instead of
        # immediate dispatch, batches accumulate until the in-process quiet
        # window expires. Trigger multiple times, verify only one agent run
        # happens after the quiet window.
        debounce_name = "_smoketest-debounce"
        debounce_dir = repo_root() / FIXTURES_REL / debounce_name
        debounce_dir.mkdir(parents=True, exist_ok=True)
        debounce_trigger = debounce_dir / "trigger.txt"
        debounce_trigger.unlink(missing_ok=True)

        # The write-files handler writes the output. Result file is written
        # OUTSIDE the watched directory to avoid self-triggering the watcher.
        (agents_dir() / f"{debounce_name}.md").write_text(
            "\n".join([
                "---",
                f"runtime: {args.runtime}",
                f"model: {model}",
                "mode: plan",
                f"post-processor: {WRITE_FILES_HANDLER}",
                f"watchPath: {FIXTURES_REL}/{debounce_name}/",
                f"debounce: {DEBOUNCE_WINDOW_S}",
                "---",
                "",
                "# Smoketest Debounce Agent",
                "",
                "This is a smoketest for debounced watcher dispatch.",
                "Do not write files yourself. Read only the trigger file, then return",
                "the JSON response so the post-processor can write the result file.",
                "Read the trigger file, verify it exists, and output JSON.",
                "",
                "## Steps",
                "",
                f'1. Read the file `{FIXTURES_REL}/{debounce_name}/trigger.txt`',
                "2. Build the JSON object shown below",
                '3. Keep the magic field exactly "debounce-xyzzy"',
                "4. Output the JSON object only",
                "",
                "## Required output",
                "",
                f'{{"files":[{{"path":"{FIXTURES_REL}/{debounce_name}-result.txt","content":"debounce-passed"}}],"summary":"Debounce test passed","magic":"debounce-xyzzy"}}',
            ]),
            encoding="utf-8",
        )
        print(f"  Created: {debounce_name}.md (debounce: {DEBOUNCE_WINDOW_S}s)")

        # Activate the watcher
        activate_result = subprocess.run(
            [*_module_argv("activate"), "--name", debounce_name],
            cwd=repo_root(), capture_output=True, text=True, check=False,
        )
        if activate_result.returncode != 0:
            fail(f"activate.py failed for debounce agent: {activate_result.stderr.strip()[:200]}")
        debounce_pid = find_watcher_pid(debounce_name)
        if debounce_pid is None:
            fail("Debounce watcher process not found after activation")
        print(f"  Watcher activated (pid: {debounce_pid})")

        # Note the cutoff time so we only count log entries written during
        # this test (the log is append-only and may contain prior runs).
        debounce_log = logs_root() / f"{debounce_name}.log"
        debounce_cutoff_ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

        # Trigger three times to exercise BOTH debounce layers:
        #   Layer 1: 1s internal batch debounce (DEBOUNCE_SECS in activate.py)
        #   Layer 2: 5s in-process quiet window (frontmatter debounce: 5)
        # T=0.0 and T=0.5 land in the same 1s batch (tests layer 1).
        # T=5.0 creates a NEW batch that resets the quiet window (tests layer 2).
        debounce_trigger.write_text("trigger-1", encoding="utf-8")
        time.sleep(0.5)
        debounce_trigger.write_text("trigger-2", encoding="utf-8")
        time.sleep(4.5)
        debounce_trigger.write_text("trigger-3", encoding="utf-8")
        print("  Triggered three times (0s, 0.5s, 5s) - exercises both debounce layers")

        # Wait for the quiet window to expire and the agent to complete.
        # Third trigger batches at ~T=6, window expires at T=6 + debounce(5) = T~11.
        # The agent call itself starts only after that, so the wait has to
        # clear the quiet window on top of the agent's own budget.
        debounce_result_file = repo_root() / FIXTURES_REL / f"{debounce_name}-result.txt"
        max_wait = AGENT_RESULT_TIMEOUT_S + DEBOUNCE_WINDOW_S
        poll_interval = 3
        waited = 0
        while waited < max_wait:
            if debounce_result_file.is_file():
                break
            time.sleep(poll_interval)
            waited += poll_interval
            if waited % 15 == 0:
                print(f"  Still waiting... ({waited}s)")
        if not debounce_result_file.is_file():
            # Check log for clues
            log_tail = ""
            if debounce_log.is_file():
                log_tail = debounce_log.read_text(encoding="utf-8").strip()[-500:]
            fail(f"Debounce agent did not produce result within {max_wait}s. Log tail: {log_tail}")
        content = debounce_result_file.read_text(encoding="utf-8").strip()
        if content != "debounce-passed":
            fail(f"Debounce result content wrong: {content!r}")
        print(f"  Agent completed after debounce window ({waited}s)")

        # Verify only one successful agent run (not two). Only count
        # entries with ts >= the cutoff captured before we triggered;
        # the log is append-only and may carry prior runs.
        agent_runs = 0
        if debounce_log.is_file():
            for line in debounce_log.read_text(encoding="utf-8").splitlines():
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if entry.get("phase") != "agent" or entry.get("status") != "ok":
                    continue
                if entry.get("ts", "") < debounce_cutoff_ts:
                    continue
                agent_runs += 1
        if agent_runs > 1:
            fail(f"Expected 1 agent run (debounced), got {agent_runs}")
        print(f"  Confirmed: {agent_runs} agent run (three triggers debounced into one)")
        print("  Debounced watcher dispatch: PASS")

        # Cleanup debounce test
        stop_watcher(debounce_name)
        subprocess.run([*_module_argv("stop"), "--name", debounce_name],
                       cwd=repo_root(), check=False, capture_output=True, text=True)
        (agents_dir() / f"{debounce_name}.md").unlink(missing_ok=True)
        debounce_trigger.unlink(missing_ok=True)
        debounce_result_file.unlink(missing_ok=True)

        print("")
        current_step = "14/14 stop"
        print("[14/14] Tearing down test agents...")
        stop_watcher(watcher_name)
        subprocess.run([*_module_argv("stop"), "--name", cron_name], cwd=repo_root(), check=False, capture_output=True, text=True)
        subprocess.run([*_module_argv("stop"), "--name", watcher_name], cwd=repo_root(), check=False, capture_output=True, text=True)
        subprocess.run([*_module_argv("stop"), "--name", preprocessor_name], cwd=repo_root(), check=False, capture_output=True, text=True)
        # Teardown only stops scheduling; remove smoketest files ourselves
        for name in (cron_name, watcher_name, preprocessor_name):
            prompt = agents_dir() / f"{name}.md"
            prompt.unlink(missing_ok=True)
        # Remove pre-processor smoketest processor scripts
        pre_processor_path.unlink(missing_ok=True)
        post_processor_path.unlink(missing_ok=True)
        if (agents_dir() / f"{cron_name}.md").exists():
            fail("Cron agent file still exists after cleanup")
        if (agents_dir() / f"{watcher_name}.md").exists():
            fail("Watcher agent file still exists after cleanup")
        if (agents_dir() / f"{preprocessor_name}.md").exists():
            fail("Pre-processor agent file still exists after cleanup")
        final_status = json.loads(run_status("--json"))
        remaining = len([
            agent for agent in final_status.get("agents", [])
            if agent.get("name") in {cron_name, watcher_name, preprocessor_name}
        ])
        if remaining != 0:
            fail("Agents still appear in status after stop")
        print("  Cleanup verified: all agents removed from disk and status")
        print(f"  Log files preserved in {logs_root()}/")

        print("")
        print(f"PASS - full chain validated ({args.runtime}):")
        print(f"  create -> frontmatter -> status -> activate watcher -> {args.runtime} CLI -> JSON output ->")
        print(f"  post-processor -> file write -> watcher detect -> auto-run -> confirm outputs ->")
        print(f"  dashboard HTTP agent list ->")
        print(f"  pre-processor -> post-processor (agent:none) -> skip gating ->")
        print(f"  mode: pipeline (PipelineMcp side-channel) ->")
        print(f"  spawn module (detached dispatch) -> debounced dispatch (quiet window) -> stop")
        _write_verdict("PASS", failed_step=None, reason=None,
                       started_at=started_at, runtime=args.runtime, model=model_for_verdict)
        return 0
    except (SmokeFailure, AgentsLiveError, json.JSONDecodeError) as exc:
        environmental = exhausted_retries(started_at)
        reason = str(exc)
        if environmental:
            reason = f"{reason} - {environmental}"
        preflight.emit_failure("smoketest", reason)
        _write_verdict("FAIL", failed_step=current_step, reason=reason[:500],
                       started_at=started_at, runtime=args.runtime,
                       model=model_for_verdict,
                       category=ENVIRONMENT_LIMIT if environmental
                       else ASSERTION_FAILURE)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
