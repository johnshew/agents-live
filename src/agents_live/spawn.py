"""Shared process spawning utilities.

Provides robust binary resolution and agent spawning that works across all
execution contexts: interactive shell, inotifywait watchers, and cron jobs.

Consumers import with:

    sys.path.insert(0, str(Path.cwd() / ".claude" / "skills" / "agents-live" / "scripts"))
    from .spawn import find_uv, spawn_agent
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path


def find_uv() -> str:
    """Locate the uv binary, searching common install paths if PATH is minimal.

    Works in all contexts: interactive shells, inotifywait watchers, and
    cron jobs (minimal PATH).

    Returns the resolved path, or raises FileNotFoundError.
    """
    found = shutil.which("uv")
    if found:
        return found
    candidates = [
        Path.home() / ".local" / "bin" / "uv",
        Path.home() / ".cargo" / "bin" / "uv",
        Path("/usr/local/bin/uv"),
    ]
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    raise FileNotFoundError(
        "uv not found in PATH or common install locations "
        "(~/.local/bin, ~/.cargo/bin, /usr/local/bin)"
    )


def spawn_agent(
    root: Path,
    agent_name: str,
    changed_files: list[str],
    operations: list[dict] | None = None,
    quiet: bool = True,
) -> subprocess.Popen | None:
    """Spawn an Agents Live agent as a detached background process.

    Works reliably in all execution contexts by resolving uv via find_uv().
    Detachment is ``start_new_session=True``: the child gets its own session
    and process group, so it survives the caller's exit.

    Args:
        root: Repository root path.
        agent_name: Name of the agent (e.g. "taskflow-agent").
        changed_files: List of relative file paths that changed.
        operations: Optional operations array (set as TASKFLOW_AGENT_OPS env).
        quiet: Suppress agent output (default True).

    Returns:
        The Popen object on success, None on failure (logged to stderr).
    """
    run_script = root / ".claude" / "skills" / "agents-live" / "scripts" / "run.py"
    if not run_script.is_file():
        print(f"[spawn] Agent skipped: run.py not found at {run_script}", file=sys.stderr)
        return None

    try:
        uv = find_uv()
    except FileNotFoundError as exc:
        print(f"[spawn] Agent skipped: {exc}", file=sys.stderr)
        return None

    agent_cmd = [uv, "run", "--script", str(run_script), "--name", agent_name]
    if changed_files:
        agent_cmd += ["--changed-files", json.dumps(changed_files)]
    if quiet:
        agent_cmd.append("--quiet")

    env = os.environ.copy()
    if operations:
        env["TASKFLOW_AGENT_OPS"] = json.dumps(operations)

    # Log stderr to a file so spawned-process failures are diagnosable
    logs_dir = root / "Agents" / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    stderr_log = logs_dir / "spawn-stderr.log"

    try:
        stderr_fh = open(stderr_log, "a")
        from datetime import datetime, timezone
        stderr_fh.write(f"\n--- [{datetime.now(timezone.utc).isoformat()}] spawn {agent_name} "
                        f"files={changed_files} ---\n")
        stderr_fh.flush()
    except OSError:
        stderr_fh = subprocess.DEVNULL

    try:
        proc = subprocess.Popen(
            agent_cmd,
            cwd=str(root),
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=stderr_fh,
            start_new_session=True,
        )
        op_summary = ", ".join(op.get("type", "?") for op in (operations or []))
        files_summary = ", ".join(Path(f).name for f in changed_files[:3])
        print(
            f"[spawn] Dispatched {agent_name} PID={proc.pid} "
            f"(ops: {op_summary or 'none'}) for: {files_summary}",
            file=sys.stderr,
        )

        # Liveness check: wait briefly and verify the child didn't die immediately
        time.sleep(1.5)
        exit_code = proc.poll()
        if exit_code is not None:
            print(
                f"[spawn] WARNING: {agent_name} PID={proc.pid} died immediately "
                f"(exit={exit_code}). Check spawn-stderr.log.",
                file=sys.stderr,
            )
            return None

        return proc
    except Exception as exc:
        print(f"[spawn] Failed to spawn {agent_name}: {exc}", file=sys.stderr)
        return None
