#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["PyYAML"]
# ///
from __future__ import annotations

import argparse
import hashlib
import os
import json
from collections import deque
import shlex
import shutil
import signal
import subprocess
import sys
import time
import types
from pathlib import Path

from .headless import (
    MAX_LOG_FIELD_LENGTH,
    AgentConfig,
    AgentsLiveError,
    clean_path,
    cli_invocation,
    crontab_lock,
    cron_line_matches,
    current_crontab_lines,
    ensure_logs_dir,
    find_watcher_pid,
    install_crontab,
    install_watcher_reboot_line,
    list_active_agent_names,
    list_reboot_watcher_agent_names,
    list_agents,
    load_agent_config,
    log_event,
    logs_root,
    remove_cron_entries,
    remove_watcher_reboot_line,
    repo_root,
    run_invocation,
    stop_watcher,
    system_log,
    agent_file_exists,
)

from . import ownership
from . import paths
from . import preflight

SCRIPT_PATH = Path(__file__).resolve()
RUN_ONCE_PATH = SCRIPT_PATH.with_name("run.py")


# --- Content-hash cascade guard helpers ---

def _hash_file_content(filepath: Path) -> str | None:
    """SHA-256 hex digest of file content, or None if unreadable."""
    try:
        return hashlib.sha256(filepath.read_bytes()).hexdigest()
    except (OSError, FileNotFoundError):
        return None


def _watch_hash_path(name: str) -> Path:
    return paths.repo_state_dir(repo_root()) / f"{name}-watch-hashes.json"


def _load_watch_hashes(name: str) -> dict:
    p = _watch_hash_path(name)
    try:
        return json.loads(p.read_text()) if p.is_file() else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _save_watch_hashes(name: str, hashes: dict) -> None:
    # Prune entries for files that no longer exist (stale after renames)
    files = hashes.get("files", {})
    root = repo_root()
    pruned = {f: h for f, h in files.items() if (root / f).exists()}
    if len(pruned) < len(files):
        hashes = {**hashes, "files": pruned}
    p = _watch_hash_path(name)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(hashes, indent=2) + "\n")


# Only apply hash filtering within this window after the last dispatch.
# Cascades happen within seconds; intentional touches come later.
CASCADE_WINDOW_SECS = 120

# --- Watcher fire-rate circuit breaker ---
# Last-resort backstop against a self-triggering cascade (a post-processor
# writing into its own watched dir, or a mutating-filename rename loop). The
# content-hash guard above stops identical re-fires but not loops whose
# content keeps changing (the 2026-05-02 outage: ~88 concurrent runs from a
# rename loop). If a single watcher process dispatches more than
# FIRE_RATE_MAX_DISPATCHES batches within a FIRE_RATE_WINDOW_SECS sliding
# window, log an error-level alert (picked up by self-heal-log-alerts) and
# exit. Watchers are detached processes (start_new_session), so exiting stays
# down with no auto-restart until reactivated. The cap is set well above any
# human editing rate - a person grooming the task backlog cannot dispatch
# dozens of batches in ten minutes - so legitimate heavy manual use never
# trips it, while a machine-speed cascade blows past it in seconds. Tune here
# if an agent legitimately needs a different ceiling.
FIRE_RATE_WINDOW_SECS = 600
FIRE_RATE_MAX_DISPATCHES = 40


def _validate_handler_paths(config: AgentConfig) -> None:
    """Verify that all referenced handler files exist before activation.

    Raises :class:`AgentsLiveError` if a pre-processor or post-processor
    file is configured but missing on disk.
    """
    if config.pre_processor_path and not config.pre_processor_path.is_file():
        raise AgentsLiveError(
            f"pre-processor not found: {config.pre_processor_reference} "
            f"(agent '{config.name}')"
        )
    if config.handler_path and not config.handler_path.is_file():
        raise AgentsLiveError(
            f"handler/post-processor not found: {config.handler_reference} "
            f"(agent '{config.name}')"
        )


def build_cron_lines(name: str) -> list[str]:
    """The canonical crontab schedule lines for *name* in the current
    execution context (§3.4.2): packaged installs persist the pinned
    shim + --repo; the flat checkout keeps the script form until the F7
    flip migrates it. Shared by activation and `migrate`'s convergence
    check."""
    config = load_agent_config(name)
    if not config.schedule:
        raise AgentsLiveError(f"agent '{name}' has no schedule")
    repo = repo_root()
    run_command = run_invocation(name)
    # An agent may declare several schedules (e.g. "@reboot" plus an hourly
    # cron); emit one crontab line per entry. They all carry the same
    # `--name`, so cron_line_matches removes and re-adds them as a group.
    # PATH rides inside each line (§3.4.2 self-contained crontab lines) so
    # no global PATH= line - which the user or another project may own -
    # ever needs to be touched.
    path_prefix = f"PATH={shlex.quote(clean_path())}"
    return [
        f"{sched} cd {shlex.quote(str(repo))} && {path_prefix} "
        f"{shlex.join(run_command)} 2>&1"
        for sched in config.schedule
    ]


def install_cron_agent(name: str) -> str:
    config = load_agent_config(name)
    if not config.schedule:
        raise AgentsLiveError(f"agent '{name}' has no schedule")
    _validate_handler_paths(config)

    ensure_logs_dir()
    new_cron_lines = build_cron_lines(name)

    # Exact --name token matching: a plain substring test would also drop
    # entries for sibling agents whose name contains this one (todo vs
    # todo-push), or arbitrary entries when the name appears in the repo
    # or script path.
    with crontab_lock():
        lines = current_crontab_lines()
        if lines is None:
            # Never treat an unreadable crontab as empty: install_crontab
            # replaces the whole table, which would wipe every entry the
            # read failed to see.
            raise AgentsLiveError("crontab is not accessible")
        lines = [line for line in lines if not cron_line_matches(line, name)]
        lines.extend(new_cron_lines)
        install_crontab(lines)
    return "; ".join(new_cron_lines)


# --- Watcher dispatch ---

def _dispatch_run_once(name: str, changed_files: list[str]) -> None:
    """Run run.py for a watcher dispatch, blocking until it completes.

    Captures stdout/stderr to per-run files and logs the exit code so a
    killed/crashed run.py is diagnosable from the system log. Used for
    both immediate dispatch and Layer-2 debounce expiry.
    """
    all_files_json = json.dumps(changed_files)
    ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    runs_dir = logs_root() / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    out_path = runs_dir / f"{name}-{ts}.watcher.out.txt"
    err_path = runs_dir / f"{name}-{ts}.watcher.err.txt"
    dispatch_started = time.monotonic()
    with open(out_path, "w") as out_f, open(err_path, "w") as err_f:
        proc = subprocess.Popen(
            [*run_invocation(name), "--changed-files", all_files_json],
            cwd=repo_root(),
            stdout=out_f,
            stderr=err_f,
        )
        log_event(system_log(), level="info", agent_name=name,
                  phase="dispatch", status="start",
                  child_pid=proc.pid,
                  stdout=str(out_path.relative_to(repo_root())),
                  stderr=str(err_path.relative_to(repo_root())))
        rc = proc.wait()
    duration_s = round(time.monotonic() - dispatch_started, 1)
    exit_signal = (signal.Signals(-rc).name
                   if rc is not None and rc < 0 else None)
    # Remove empty capture files so the runs dir stays uncluttered
    for p in (out_path, err_path):
        try:
            if p.stat().st_size == 0:
                p.unlink()
        except OSError:
            pass
    log_event(system_log(), level="info", agent_name=name,
              phase="dispatch", status="done",
              child_pid=proc.pid, exit_code=rc,
              signal=exit_signal, duration_s=duration_s)


def _validate_watcher_prereqs(config: AgentConfig) -> list[Path]:
    """Return list of absolute paths to watch (dirs and/or files).

    For files, we watch the parent directory and filter by filename in
    the event loop (inotifywait on a single file breaks on atomic saves
    that replace the inode).
    """
    all_paths = config.all_watch_paths
    if not all_paths:
        raise AgentsLiveError(f"agent '{config.name}' has no watchPath")

    watch_targets: list[Path] = []

    for wp in all_paths:
        abs_path = config.watch_path_absolute_for(wp)
        if abs_path.is_file() or abs_path.suffix:
            # Watch parent dir; we'll filter by filename in the event loop
            abs_path.parent.mkdir(parents=True, exist_ok=True)
        elif not abs_path.is_dir():
            abs_path.mkdir(parents=True, exist_ok=True)
            print(f"Created missing watch directory: {wp}")
        watch_targets.append(abs_path)

    if not shutil.which("inotifywait"):
        raise AgentsLiveError("inotifywait not found")
    return watch_targets


def should_ignore_watch_change(changed_file: str, watch_ignore: list[str] | None = None) -> bool:
    changed_path = Path(changed_file)
    if not changed_path.is_absolute():
        changed_path = (repo_root() / changed_path).resolve()

    try:
        relative = changed_path.relative_to(repo_root())
    except ValueError:
        return False

    if any(part.startswith(".") for part in relative.parts):
        return True
    if any(part == "__pycache__" for part in relative.parts):
        return True
    # Ignore JSONL log files written by the agents-live system itself
    # (prevents recursive triggers), but allow other files under Agents/logs/
    # so watcher agents that deliberately watch subdirectories still fire.
    if relative.suffix == ".log" and (
        relative == Path("Agents/logs") / relative.name
        or Path("Agents/logs") in relative.parents
    ):
        return True
    if watch_ignore and relative.name in watch_ignore:
        return True
    # Support directory-prefix ignores: entries ending with '/' match any
    # file whose repo-relative path starts with that prefix.
    if watch_ignore:
        rel_str = relative.as_posix()
        for pattern in watch_ignore:
            if pattern.endswith("/") and (rel_str + "/").startswith(pattern):
                return True
    return False


def watch_loop(name: str) -> int:
    config = load_agent_config(name)
    watch_targets = _validate_watcher_prereqs(config)

    ensure_logs_dir()

    # Redirect our own stderr to the agent log so crashes are captured as
    # structured JSONL instead of going to a separate .out file.
    import atexit
    import io
    import traceback

    _orig_stderr = sys.stderr
    _stderr_buf = io.StringIO()
    sys.stderr = _stderr_buf

    def _flush_stderr_to_log() -> None:
        text = _stderr_buf.getvalue().strip()
        if text:
            log_event(config.agent_log, level="error", phase="watcher",
                      message=text[:20_000])
    atexit.register(_flush_stderr_to_log)

    # Separate file targets (watch parent dir + filter) from directory targets.
    watch_dirs: list[Path] = []
    target_filenames: set[str] = set()
    for t in watch_targets:
        if t.is_file() or t.suffix:
            target_filenames.add(t.name)
            watch_dirs.append(t.parent)
        else:
            watch_dirs.append(t)

    # Deduplicate dirs
    seen: set[Path] = set()
    unique_dirs: list[Path] = []
    for d in watch_dirs:
        if d not in seen:
            seen.add(d)
            unique_dirs.append(d)

    # Events: close_write (direct writes), moved_to (atomic saves and
    # files arriving via temp+rename), moved_from (files leaving a
    # watched directory), delete (files removed from a watched directory).
    # moved_to is always included - atomic writes (os.replace) into a
    # watched directory only produce moved_to, not close_write.
    events = "close_write,moved_to,moved_from,delete"
    inotify_args = [
        "inotifywait", "-m", "-r", "-e", events,
        *[str(d) for d in unique_dirs], "--format", "%w%f",
    ]

    process = subprocess.Popen(
        inotify_args,
        cwd=repo_root(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )

    watch_desc = ", ".join(str(d) for d in unique_dirs)
    if target_filenames:
        watch_desc += f" (filtering: {', '.join(sorted(target_filenames))})"
    log_event(config.agent_log, level="info", phase="watcher",
             message=f"inotifywait started, watching {watch_desc}")

    _started_at = time.monotonic()
    # Per-process sliding window of dispatch timestamps for the circuit breaker.
    _dispatch_history: deque[float] = deque()
    shutdown_requested = False
    shutdown_signal: int | None = None

    def handle_shutdown(signum: int, _frame: types.FrameType | None) -> None:
        nonlocal shutdown_requested, shutdown_signal
        shutdown_requested = True
        shutdown_signal = signum
        if process.poll() is None:
            process.terminate()

    signal.signal(signal.SIGTERM, handle_shutdown)
    signal.signal(signal.SIGINT, handle_shutdown)
    signal.signal(signal.SIGHUP, handle_shutdown)

    def _log_exit(reason: str, extra: dict | None = None) -> None:
        uptime_s = round(time.monotonic() - _started_at, 1)
        sig_name = (signal.Signals(shutdown_signal).name
                    if shutdown_signal is not None else None)
        payload = {
            "level": "info",
            "phase": "exit",
            "reason": reason,
            "pid": os.getpid(),
            "uptime_s": uptime_s,
            "signal": sig_name,
        }
        if extra:
            payload.update(extra)
        log_event(config.agent_log, **payload)
        log_event(system_log(), agent_name=name, **payload)

    atexit.register(_log_exit, "atexit")

    try:
        if process.stdout is None:
            raise AgentsLiveError("watcher stdout was not available")

        import select
        import fcntl

        # Use the raw file descriptor for non-blocking I/O.
        # TextIOWrapper.read() can buffer internally and miss select()
        # readiness, so we use os.read() on the raw fd instead.
        fd = process.stdout.fileno()
        flags = fcntl.fcntl(fd, fcntl.F_GETFL)
        fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)

        DEBOUNCE_SECS = 1.0  # collect events for this long before firing

        # Layer-2 debounce state (frontmatter `debounce: N`): batches
        # accumulate here and dispatch only after N seconds of quiet,
        # timed in-process via the select timeout below.
        debounce_files: list[str] = []
        debounce_deadline: float | None = None

        def _fire_debounce(reason: str) -> None:
            """Dispatch everything accumulated in the debounce window."""
            nonlocal debounce_files, debounce_deadline
            files = debounce_files
            debounce_files = []
            debounce_deadline = None
            if not files:
                return
            log_event(config.agent_log, level="info", phase="watcher",
                      message=f"debounce window expired ({reason}): "
                              f"dispatching {len(files)} file(s)")
            _dispatch_run_once(name, files)

        def _drop_debounce(reason: str) -> None:
            """Discard pending debounced files on deliberate shutdown."""
            nonlocal debounce_files, debounce_deadline
            if debounce_files:
                log_event(config.agent_log, level="warning", phase="watcher",
                          message=f"dropping {len(debounce_files)} pending "
                                  f"debounced file(s) on {reason}",
                          changed_files=debounce_files)
            debounce_files = []
            debounce_deadline = None

        pending_line = ""
        while True:
            # Block until an event arrives or, when a Layer-2 debounce
            # window is pending, until that window expires.
            if debounce_deadline is not None:
                remaining_window = debounce_deadline - time.monotonic()
                if remaining_window > 0:
                    ready, _, _ = select.select([fd], [], [], remaining_window)
                else:
                    ready = []
                if shutdown_requested:
                    _drop_debounce("shutdown")
                    return 0
                if not ready:
                    # Quiet window elapsed with no new events: dispatch.
                    _fire_debounce("quiet window elapsed")
                    continue
            else:
                select.select([fd], [], [])
                if shutdown_requested:
                    return 0

            # Read all immediately available events
            changed_files: list[str] = []
            try:
                while True:
                    raw = os.read(fd, 8192)
                    if not raw:
                        # EOF - inotifywait exited
                        inotify_stderr = ""
                        if process.stderr:
                            try:
                                inotify_stderr = process.stderr.read().strip()
                            except Exception:
                                pass
                        log_event(config.agent_log, level="warning", phase="watcher",
                                  message=f"inotifywait exited (rc={process.poll()})"
                                  + (f": {inotify_stderr[:MAX_LOG_FIELD_LENGTH]}" if inotify_stderr else ""))
                        # Don't lose edits already accumulated in a
                        # pending debounce window on an unexpected exit.
                        _fire_debounce("inotifywait exit")
                        return 0
                    chunk = raw.decode("utf-8", errors="replace")
                    pending_line += chunk
                    while "\n" in pending_line:
                        line, pending_line = pending_line.split("\n", 1)
                        f = line.strip()
                        if not f:
                            continue
                        # File-target filter: when watching a parent dir on behalf of
                        # a specific file, only accept events for those files. Events
                        # from recursively-watched subdirs of directory targets pass through.
                        if target_filenames:
                            changed = Path(f)
                            # Accept if the file matches a target filename, or if
                            # it's under a directory target (not a file-target parent)
                            if changed.name not in target_filenames:
                                # Check if this event is from a dir we're watching recursively
                                is_under_dir_target = any(
                                    t.is_dir() and str(changed).startswith(str(t))
                                    for t in watch_targets
                                )
                                if not is_under_dir_target:
                                    continue
                        if not should_ignore_watch_change(f, config.watch_ignore):
                            changed_files.append(f)
            except (BlockingIOError, IOError):
                pass

            if not changed_files:
                continue

            # Wait briefly for more events to arrive (debounce)
            deadline = time.monotonic() + DEBOUNCE_SECS
            while time.monotonic() < deadline:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                ready, _, _ = select.select([fd], [], [], remaining)
                if shutdown_requested:
                    _drop_debounce("shutdown")
                    return 0
                if not ready:
                    break
                try:
                    raw = os.read(fd, 8192)
                    if not raw:
                        break
                    chunk = raw.decode("utf-8", errors="replace")
                    pending_line += chunk
                    while "\n" in pending_line:
                        line, pending_line = pending_line.split("\n", 1)
                        f = line.strip()
                        if not f:
                            continue
                        if target_filenames:
                            changed = Path(f)
                            if changed.name not in target_filenames:
                                is_under_dir_target = any(
                                    t.is_dir() and str(changed).startswith(str(t))
                                    for t in watch_targets
                                )
                                if not is_under_dir_target:
                                    continue
                        if not should_ignore_watch_change(f, config.watch_ignore):
                            changed_files.append(f)
                except (BlockingIOError, IOError):
                    break

            # Deduplicate, keep all unique changed files - convert to repo-relative paths
            root = repo_root()
            unique_files = list(dict.fromkeys(changed_files))
            relative_files = []
            for f in unique_files:
                try:
                    relative_files.append(str(Path(f).relative_to(root)))
                except ValueError:
                    relative_files.append(f)
            # Content-hash cascade guard: drop individual files whose
            # content hasn't changed since the last dispatch for this agent.
            # Only active within CASCADE_WINDOW_SECS of the last dispatch -
            # outside the window, every event dispatches (so touch works).
            cached = _load_watch_hashes(name)
            cached_hashes: dict[str, str] = cached.get("files", {})
            last_dispatch = cached.get("_dispatched_at", 0)
            in_cascade_window = (time.time() - last_dispatch) < CASCADE_WINDOW_SECS

            current_hashes: dict[str, str] = {}
            actually_changed: list[str] = []
            hash_skipped: list[str] = []
            for f in relative_files:
                h = _hash_file_content(root / f)
                if h:
                    current_hashes[f] = h
                    if in_cascade_window and cached_hashes.get(f) == h:
                        hash_skipped.append(f)
                    else:
                        actually_changed.append(f)
                else:
                    # Can't hash (deleted/unreadable) - keep it in the batch
                    actually_changed.append(f)

            if hash_skipped:
                short = {f: current_hashes[f][:12] for f in hash_skipped}
                log_event(config.agent_log, level="info", phase="watcher",
                          message=f"content-hash filtered: {len(hash_skipped)} file(s) unchanged",
                          skipped_files=hash_skipped, hashes=short)

            if not actually_changed:
                log_event(system_log(), level="info", agent_name=name, phase="watcher",
                          message=f"content-hash skip: all {len(relative_files)} file(s) unchanged since last dispatch",
                          changed_files=relative_files)
                continue

            # Cache current hashes + dispatch timestamp
            _save_watch_hashes(name, {
                "files": {**cached_hashes, **current_hashes},
                "_dispatched_at": time.time(),
            })

            short_hashes = {f: current_hashes[f][:12] for f in actually_changed if f in current_hashes}
            log_event(system_log(), level="info", agent_name=name, phase="watcher",
                      message=f"debounce batch: {len(actually_changed)} file(s)" +
                              (f" ({len(hash_skipped)} hash-filtered)" if hash_skipped else ""),
                      changed_files=actually_changed, hashes=short_hashes)

            # Fire-rate circuit breaker: a real dispatch is about to happen.
            # Record it, prune the sliding window, and trip if the rate
            # indicates a cascade rather than human-speed editing.
            _now = time.monotonic()
            _dispatch_history.append(_now)
            _cutoff = _now - FIRE_RATE_WINDOW_SECS
            while _dispatch_history and _dispatch_history[0] < _cutoff:
                _dispatch_history.popleft()
            if len(_dispatch_history) > FIRE_RATE_MAX_DISPATCHES:
                window_min = round(FIRE_RATE_WINDOW_SECS / 60)
                breaker_msg = (
                    f"CIRCUIT BREAKER: {len(_dispatch_history)} dispatches in "
                    f"<{window_min}min exceeds cap of {FIRE_RATE_MAX_DISPATCHES} - "
                    f"likely a watcher cascade; stopping watcher '{name}'"
                )
                log_event(config.agent_log, level="error", phase="watcher",
                          status="circuit_breaker",
                          error_category="cascade_circuit_breaker",
                          message=breaker_msg,
                          dispatch_count=len(_dispatch_history),
                          window_secs=FIRE_RATE_WINDOW_SECS,
                          last_files=actually_changed)
                log_event(system_log(), level="error", agent_name=name, phase="watcher",
                          status="circuit_breaker",
                          error_category="cascade_circuit_breaker",
                          message=breaker_msg,
                          dispatch_count=len(_dispatch_history),
                          window_secs=FIRE_RATE_WINDOW_SECS)
                return 1

            if config.debounce:
                # Layer-2 debounce: accumulate this batch and (re)start the
                # quiet window. Dispatch happens at the top of the loop when
                # the window expires with no new batches.
                for f in actually_changed:
                    if f not in debounce_files:
                        debounce_files.append(f)
                debounce_deadline = time.monotonic() + config.debounce
                log_event(config.agent_log, level="info", phase="watcher",
                          message=f"debounce window (re)started: dispatch in "
                                  f"{config.debounce}s "
                                  f"({len(debounce_files)} file(s) accumulated)")
            else:
                # Immediate dispatch (default).
                _dispatch_run_once(name, actually_changed)
    except SystemExit:
        raise
    except Exception:
        log_event(config.agent_log, level="error", phase="watcher",
                  message=traceback.format_exc()[:20_000])
        raise
    finally:
        sys.stderr = _orig_stderr
        process.terminate()
    return 0


def activate_watcher(name: str) -> int:
    config = load_agent_config(name)
    _validate_handler_paths(config)
    _validate_watcher_prereqs(config)

    existing_pid = find_watcher_pid(name)
    if existing_pid is not None:
        print("Warning: stopping existing watcher", file=sys.stderr)
        stop_watcher(name)

    ensure_logs_dir()
    loop_argv = cli_invocation("internal", "watch-loop", name,
                               flat_script=SCRIPT_PATH)
    with open("/dev/null", "w") as devnull:
        process = subprocess.Popen(
            loop_argv,
            cwd=repo_root(),
            stdout=devnull,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )

    time.sleep(1)
    if process.poll() is not None:
        # Process died - capture stderr and log it
        stderr_text = ""
        if process.stderr:
            stderr_text = process.stderr.read().strip()
            process.stderr.close()
        agent_log = logs_root() / f"{name}.log"
        if stderr_text:
            log_event(agent_log, level="error", phase="watcher",
                      status="crash", message=stderr_text[:2000])
        raise AgentsLiveError(
            f"watcher process exited immediately with status {process.returncode}"
            + (f": {stderr_text[:200]}" if stderr_text else ""))

    # Process is alive - close our end of the pipe.
    # watch_loop has already redirected sys.stderr to StringIO,
    # so nothing writes to fd2 after startup.
    if process.stderr:
        process.stderr.close()

    return process.pid


def _resolve_activation_ownership(
    config: AgentConfig,
    *,
    batch_mode: bool,
    transfer_to: str | None,
    assume_yes: bool = False,
    dry_run: bool = False,
) -> bool:
    """Resolve ownership for an activation request.

    ``activate`` only ever registers triggers on the host it runs on. To move
    an agent to another machine, set ownership with ``--transfer-to``
    (registry-only, no local registration) and then run ``activate`` on that
    machine.

    When ``dry_run`` is set, NOTHING is mutated - no registry writes
    (``set_owner``), no project-config writes (``initialize`` /
    ``declare_ownership`` in the first-transfer bootstrap; TT-001) - and
    ownership changes are reported as ``[dry-run]`` lines.

    Returns True if activation should proceed on this host, False otherwise.

    Behaviour (per proposal-multi-machine-ownership.md):

    * ``--transfer-to <host>`` only rewrites ``agent-owners.json`` and never
      registers locally, even when ``<host>`` is this machine. Registration
      always happens on the owning machine via a plain ``activate``.
    * Otherwise seed the registry from the frontmatter ``owner:`` (else this
      host) when unset, then activate when the owner is ``*`` or this host.
    EXCEPT in batch mode: ``--all`` never claims - an unregistered agent
      with no frontmatter ``owner:`` is skipped with a note, so a sweep
            (including the dashboard health action) cannot adopt dormant agents.
            Claiming stays a targeted, single-agent ``activate --name`` decision.
        * An agent owned by a different host is skipped: ``--all`` skips silently; a
      targeted ``activate`` prints guidance to transfer ownership first.
    """
    host = ownership.current_host()
    name = config.name

    # Local-only mode (no ownership registry - the public-kernel default,
    # proposal §3.9): every agent is owned here. A --transfer-to IS the
    # declaration of multi-host intent, so it upgrades the project to
    # registry mode itself (through init's sanctioned mutation point) and
    # proceeds - no flags, no hand-editing config.
    if ownership.local_only():
        if not transfer_to:
            return True
        # The registry is a backend the public kernel does not ship
        # (proposal §3.9). Refuse BEFORE declaring registry mode, so a
        # kernel-only install can never write a declaration it cannot
        # honor (which would leave the project failing closed).
        if not ownership.registry_available():
            print(f"'{name}': cannot enable multi-host ownership: no "
                  f"registry backend installed (multi-host ownership is a "
                  f"private plugin; the public kernel is local-only)",
                  file=sys.stderr)
            return False
        # A dry run previews the whole bootstrap without mutating
        # ANYTHING (TT-001): writing the registry declaration but not
        # the registry would leave the project failing closed with
        # "registry declared but missing".
        if dry_run:
            print(f"[dry-run] would declare ownership = \"registry\" and "
                  f"transfer '{name}' ownership: (unset) -> {transfer_to}")
            return False
        from . import init as _init
        try:
            _init.initialize(repo_root())
            _init.declare_ownership(repo_root(), "registry")
        except ValueError as exc:
            print(f"'{name}': cannot enable multi-host ownership: {exc}",
                  file=sys.stderr)
            return False
        print("Declared ownership = \"registry\" (first --transfer-to "
              "enables the multi-host registry).")
        if not ownership.registry_file_exists():
            # Bootstrap: no registry document yet; create it with this
            # transfer as the first entry, then hand off registration to
            # the owning machine like any other transfer.
            ownership.set_owner(name, transfer_to)
            print(f"Transferred '{name}' ownership: (unset) -> {transfer_to}")
            log_event(system_log(), level="info", agent_name=name,
                      phase="ownership-transfer",
                      previous=None, new=transfer_to)
            return False
        # An owners document already existed (declared local over a
        # leftover registry): fall through to the normal strict flow.

    # Ephemeral agents (``_``-prefixed, e.g. smoketest fixtures) are
    # local, single-run artifacts created and torn down within one
    # process. They are never gated by ownership and never seeded into
    # agent-owners.json, so the framework smoketest works on every host
    # regardless of registry state.
    if name.startswith("_"):
        return True

    # Registry mode: a missing/corrupt registry is abstention, never
    # local ownership (a vanished file must not flip a multi-host
    # deployment to activate-everything-here).
    ownership.load_owners()

    # Explicit transfer: registry-only on every host, including this one.
    # Registration happens later via a plain `activate` on the owning machine.
    if transfer_to:
        previous = ownership.load_owners().get(name)
        if previous == transfer_to:
            print(f"'{name}' already owned by {transfer_to}; no change.")
            return False
        if dry_run:
            print(f"[dry-run] would transfer '{name}' ownership: "
                  f"{previous or '(unset)'} -> {transfer_to}")
        else:
            ownership.set_owner(name, transfer_to)
            print(f"Transferred '{name}' ownership: {previous or '(unset)'} -> {transfer_to}")
            log_event(system_log(), level="info", agent_name=name, phase="ownership-transfer",
                      previous=previous, new=transfer_to)
        return False  # registration happens on the owning machine

    # Seed registry if needed: existing owner wins; else frontmatter owner;
    # else this host. A batch sweep never falls through to this-host: an
    # unregistered agent with no frontmatter owner is someone's
    # not-yet-launched work, and adopting it fleet-wide is too big a side
    # effect for --all (which the dashboard health action runs).
    owner = ownership.load_owners().get(name)
    if owner is None:
        frontmatter_owner = config.owner
        if batch_mode and frontmatter_owner in (None, ""):
            print(f"'{name}': unowned and no frontmatter owner; skipped by "
                  f"--all. Run `activate.py --name {name}` here to claim it.")
            return False
        owner = frontmatter_owner if frontmatter_owner not in (None, "") else host
        if dry_run:
            print(f"[dry-run] would seed '{name}' owner -> {owner}")
        else:
            ownership.set_owner(name, owner)

    if owner == ownership.WILDCARD or owner == host:
        return True

    # Owned by a different host: take over only with per-invocation consent.
    # Never prompt in JSON mode - stdout is captured into the envelope, so
    # an interactive question would be invisible and block forever;
    # machine callers consent with --yes.
    take_over = assume_yes
    if (not batch_mode and not take_over and sys.stdin.isatty()
            and not preflight.json_mode()):
        answer = input(
            f"{name} is owned by {owner}; take ownership and activate here? "
            "[y/N] ")
        take_over = answer.strip().lower() in {"y", "yes"}
    if not batch_mode and take_over:
        if dry_run:
            print(f"[dry-run] would transfer '{name}' ownership: "
                  f"{owner} -> {host} and activate here")
        else:
            ownership.set_owner(name, host)
            print(f"Transferred '{name}' ownership: {owner} -> {host}")
            log_event(system_log(), level="info", agent_name=name,
                      phase="ownership-transfer", previous=owner, new=host)
        return True
    if not batch_mode:
        print(
            f"'{name}' is owned by '{owner}'; not activating on '{host}'. "
            f"Run `activate.py --name {name} --transfer-to {host}` here to take "
            f"ownership, then activate.",
            file=sys.stderr,
        )
    return False


def prune_orphans(*, dry_run: bool = False) -> list[str]:
    """Tear down cron/watcher entries whose agent definition no longer exists.

    The host's runtime (crontab + watcher processes) is the source of truth
    for what is running. Anything live here without a backing ``*.md`` agent
    file is an orphan left behind by a deleted or renamed agent. Removing the
    file then becomes a complete decommission: the next reconcile on each host
    enumerates its own runtime, finds the orphan, and tears it down (cron +
    watcher), so no per-host manual stop is needed.

    Returns the list of pruned agent names. Safe to call repeatedly: an agent
    whose file still exists is never touched, and ``remove_*``/``stop_watcher``
    are no-ops when there is nothing to remove.
    """
    running = list_active_agent_names()
    if not running:
        return []
    defined = set(list_agents())
    pruned: list[str] = []
    for name in sorted(running - defined):
        # TT-001: discovery skips malformed native files leniently, so
        # "not listed" is not "deleted". Only a missing FILE (checked
        # without parsing, across every agent location) proves deletion;
        # an existing-but-broken definition gets abstention + a warning,
        # never stop.
        if agent_file_exists(name):
            log_event(system_log(), level="warning", agent_name=name,
                      phase="prune-orphan",
                      message="definition file exists but is not parseable "
                              "as an agent; abstaining from prune - repair "
                              "the file")
            print(f"Skipped '{name}': definition file exists but is not "
                  f"parseable as an agent; repair it (not pruned)")
            continue
        if dry_run:
            print(f"[dry-run] would prune orphaned agent '{name}' (no agent file)")
            pruned.append(name)
            continue
        try:
            remove_cron_entries(name)
            remove_watcher_reboot_line(name)
        except AgentsLiveError:
            pass  # crontab unavailable; watcher stop below still applies
        stop_watcher(name)
        log_event(system_log(), level="info", agent_name=name, phase="prune-orphan",
                  message="removed cron/watcher for deleted agent definition")
        print(f"Pruned orphaned agent '{name}' (no agent file)")
        pruned.append(name)
    return pruned


def activate_one(
    name: str,
    *,
    batch_mode: bool = False,
    transfer_to: str | None = None,
    assume_yes: bool = False,
    dry_run: bool = False,
) -> list[str]:
    """Activate a single agent. Returns list of activated trigger types.

    When ``dry_run`` is set, nothing is mutated (no cron install, no
    watcher start, no registry writes); the actions that *would* be taken
    are printed instead.
    """
    config = load_agent_config(name)
    ensure_logs_dir()
    if not _resolve_activation_ownership(
        config,
        batch_mode=batch_mode,
        transfer_to=transfer_to,
        assume_yes=assume_yes,
        dry_run=dry_run,
    ):
        return []
    activated = []
    if config.schedule:
        if dry_run:
            print(f"[dry-run] would activate cron for '{config.name}': {', '.join(config.schedule)}")
        else:
            cron_line = install_cron_agent(config.name)
            log_event(system_log(), level="info", agent_name=config.name, phase="activate", type="cron",
                      schedule=config.schedule)
            print(f"Activated cron for '{config.name}': {cron_line}")
        activated.append("cron")

    if config.watch_path:
        if dry_run:
            print(f"[dry-run] would activate watcher for '{config.name}': "
                  f"watching {config.watch_path}")
        else:
            pid = activate_watcher(config.name)
            install_watcher_reboot_line(config.name)
            log_event(system_log(), level="info", agent_name=config.name, phase="activate", type="watcher",
                      watchPath=config.watch_path, pid=pid)
            print(f"Activated watcher for '{config.name}': watching {config.watch_path}, pid {pid}")
        activated.append("watcher")

    return activated


def main() -> int:
    from . import plugins

    parser = argparse.ArgumentParser()
    internal_commands = parser.add_subparsers(dest="internal_command")
    for command in ("watch-loop", "ensure-watcher", "list-reboot-watchers"):
        child = internal_commands.add_parser(command)
        if command != "list-reboot-watchers":
            child.add_argument("name")

    parser.add_argument("--name")
    parser.add_argument("--all", action="store_true", help="Activate all agents that have a schedule or watchPath")
    parser.add_argument(
        "--transfer-to",
        dest="transfer_to",
        help="Transfer ownership of --name to the given host (registry-only): "
             "updates Agents/data/agent-owners.json without registering any "
             "triggers, even when the host is this machine. Registration "
             "happens on the owning machine via a plain `activate --name`.",
    )
    parser.add_argument(
        "--dry-run", "-n", dest="dry_run", action="store_true",
        help="Preview what would be activated without mutating crontab, "
             "watchers, or agent-owners.json.",
    )
    parser.add_argument(
        "--yes", action="store_true",
        help="Take ownership from another host without prompting.",
    )
    parser.add_argument(
        "--prune-orphans", dest="prune_orphans", action="store_true",
        help="Tear down cron/watcher entries on this host whose agent "
             "definition file no longer exists, then exit. Implied at the "
             "start of --all so reconcile also decommissions deleted agents.",
    )
    args = parser.parse_args()
    if args.yes and (args.all or not args.name):
        parser.error("--yes requires a targeted --name and cannot be used with --all")

    try:
        if getattr(args, "internal_command", None) == "list-reboot-watchers":
            for agent_name in list_reboot_watcher_agent_names():
                print(agent_name)
            return 0

        if getattr(args, "internal_command", None) == "ensure-watcher":
            # Guarded, idempotent respawn used by the @reboot crontab line.
            pid = activate_watcher(args.name)
            print(f"Ensured watcher for '{args.name}': pid {pid}")
            return 0

        if getattr(args, "internal_command", None) == "watch-loop":
            agent_log = logs_root() / f"{args.name}.log"
            try:
                return watch_loop(args.name)
            except Exception:
                import traceback
                ensure_logs_dir()
                log_event(agent_log, level="error", phase="watcher",
                          status="crash", message=traceback.format_exc()[:2000])
                return 1

        if not args.dry_run:
            try:
                plugins.converge([repo_root()])
            except (OSError, ValueError, plugins.PluginError) as exc:
                raise AgentsLiveError(f"plugin convergence failed: {exc}") from exc

        if args.prune_orphans and not (args.all or args.name):
            pruned = prune_orphans(dry_run=args.dry_run)
            verb = "Would prune" if args.dry_run else "Pruned"
            print(f"{verb} {len(pruned)} orphaned agent(s)"
                  + (f": {', '.join(pruned)}" if pruned else ""))
            return 0

        if args.all:
            # Reconcile this host to the repo: drop orphans first (deleted
            # agent files), then activate what is still defined and owned here.
            prune_orphans(dry_run=args.dry_run)
            candidates = list_agents()
            errors = []
            total = 0
            for name in candidates:
                try:
                    config = load_agent_config(name)
                except AgentsLiveError:
                    continue
                if not config.schedule and not config.watch_path:
                    continue
                try:
                    activated = activate_one(name, batch_mode=True, dry_run=args.dry_run)
                    if activated:
                        total += 1
                except AgentsLiveError as exc:
                    print(f"  FAILED {name}: {exc}", file=sys.stderr)
                    errors.append(name)
            verb = "Would activate" if args.dry_run else "Activated"
            print(f"\n{verb} {total} agents" + (f" ({len(errors)} failed)" if errors else ""))
            return 1 if errors else 0

        if not args.name:
            raise AgentsLiveError("--name or --all is required")

        activated = activate_one(
            args.name,
            batch_mode=False,
            transfer_to=args.transfer_to,
            assume_yes=args.yes,
            dry_run=args.dry_run,
        )
        if not activated and not args.transfer_to:
            # Ownership skip is a valid outcome; only raise when there's
            # no work to do at all (no schedule/watchPath defined).
            config = load_agent_config(args.name)
            if not config.schedule and not config.watch_path:
                raise AgentsLiveError(
                    f"agent '{args.name}' has no schedule or watchPath"
                )
        return 0
    except (AgentsLiveError, ownership.OwnershipUnavailableError) as exc:
        # Layer 2 (§3.6): typed errors leave as the envelope in json
        # mode, one concise stderr line otherwise.
        preflight.emit_typed_error(exc, "start")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
