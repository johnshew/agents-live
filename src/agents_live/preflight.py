"""Capability preflight + structured error contract (proposal §3.6, Phase 2).

Static, side-effect-free probes run before host-mutating subcommands, and
one error envelope shared by every CLI failure path. Three layers by
design (the preflight is advisory, never a guarantee - TOCTOU):

1. Static preflight (this module): dependency presence, crontab
   readability, inotify availability.
2. The actual operation performs the mutation and converts permission
   failures into the same envelope.
3. Post-verification confirms intended state (smoketest residue pattern).

Symbolic codes carry the meaning; process exit status stays coarse
(0 ok, nonzero error). Codes in use:
    host_permission_required, dependency_missing, agent_invalid,
  agent_failed, agent_output_invalid, ownership_unavailable, no_project_root

stdlib-only; sibling scripts import it flat. Must not import headless.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, asdict

try:
    from . import hostruntime
except ImportError:  # flat execution: qlog and timeline run as scripts
    import hostruntime  # type: ignore[no-redef]

# Set by cli.py when --json is given, so in-process subcommands and their
# children serialize typed errors as the envelope instead of prose
# (layer 2 of the §3.6 contract - the flag must not stop at preflight).
JSON_ENV_VAR = "AGENTS_LIVE_JSON"


def json_mode() -> bool:
    return os.environ.get(JSON_ENV_VAR, "") == "1"


@dataclass(frozen=True)
class CapabilityFailure:
    code: str          # symbolic error code (see module docstring)
    capability: str    # what was probed, e.g. "schedule", "watch"
    operation: str     # the subcommand that needed it
    detail: str        # one concise human sentence


def emit_error(failure: CapabilityFailure, *, json_mode: bool) -> None:
    """One envelope on stdout with --json, one concise line on stderr
    otherwise (proposal §3.6 error contract)."""
    if json_mode:
        print(json.dumps({"error": asdict(failure)}))
    else:
        print(f"error [{failure.code}] {failure.operation}: {failure.detail}",
              file=sys.stderr)


def emit_typed_error(exc: BaseException, operation: str) -> None:
    """Layer-2 error conversion: serialize a typed error (anything
    carrying a ``category`` attribute, i.e. a AgentsLiveError
    subclass) through the same envelope the preflight uses. In json mode
    (see :data:`JSON_ENV_VAR`) the envelope goes to stdout; otherwise one
    concise line goes to stderr."""
    emit_error(
        CapabilityFailure(
            code=str(getattr(exc, "category", "agent_error")),
            capability="agent",
            operation=operation,
            detail=str(exc),
        ),
        json_mode=json_mode(),
    )


def emit_failure(operation: str, detail: str, *,
                 code: str = "operation_failed",
                 capability: str = "command") -> None:
    """Emit a non-exception failure through the shared error envelope."""
    emit_error(
        CapabilityFailure(code, capability, operation, detail),
        json_mode=json_mode(),
    )


def _probe_schedule(operation: str) -> CapabilityFailure | None:
    """Probe whatever this host schedules with, not cron specifically."""
    if hostruntime.native_scheduler() == hostruntime.TASK_SCHEDULER:
        return _probe_task_scheduler(operation)
    return _probe_crontab(operation)


# The two leaves below are imported where they are used rather than at
# module scope: this module has to stay importable by the scripts that
# run flat, and neither leaf is reachable on the hosts they run on. What
# each mechanism costs to probe, and what a refusal from it looks like,
# belongs to the leaf that drives it, not here.

def _probe_task_scheduler(operation: str) -> CapabilityFailure | None:
    try:
        from . import wintasks  # noqa: PLC0415
    except ImportError:  # flat execution, as at the top of this module
        import wintasks  # type: ignore[no-redef]  # noqa: PLC0415

    missing = wintasks.missing_dependency()
    if missing is not None:
        return CapabilityFailure(
            "dependency_missing", "schedule", operation, missing)
    reason = wintasks.probe()
    if reason is not None:
        return CapabilityFailure(
            "host_permission_required", "schedule", operation, reason)
    return None


def _probe_crontab(operation: str) -> CapabilityFailure | None:
    if shutil.which("crontab") is None:
        return CapabilityFailure(
            "dependency_missing", "crontab", operation,
            "crontab binary not found (install cron)")
    try:
        completed = subprocess.run(
            ["crontab", "-l"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return CapabilityFailure(
            "host_permission_required", "crontab", operation,
            f"cannot read the crontab: {exc}")
    # An empty crontab exits 1 with "no crontab for <user>" - that is a
    # readable-and-empty state, not a permission failure.
    if completed.returncode != 0 and "no crontab" not in completed.stderr:
        return CapabilityFailure(
            "host_permission_required", "crontab", operation,
            f"crontab -l failed (rc={completed.returncode}): "
            f"{completed.stderr.strip()[:200]}")
    return None


def _probe_watch(operation: str) -> CapabilityFailure | None:
    """Can this host be told when a file changed?"""
    if hostruntime.id() == hostruntime.WINDOWS:
        return _probe_directory_changes(operation)
    return _probe_inotify(operation)


def _probe_directory_changes(operation: str) -> CapabilityFailure | None:
    try:
        from . import winwatch  # noqa: PLC0415
    except ImportError:  # flat execution, as at the top of this module
        import winwatch  # type: ignore[no-redef]  # noqa: PLC0415

    reason = winwatch.probe()
    if reason is not None:
        return CapabilityFailure(
            "dependency_missing", "watch", operation, reason)
    return None


def _probe_inotify(operation: str) -> CapabilityFailure | None:
    if shutil.which("inotifywait") is None:
        return CapabilityFailure(
            "dependency_missing", "watch", operation,
            "inotifywait not found (install inotify-tools)")
    return None


_CAPABILITY_PROBES = {
    "schedule": _probe_schedule,
    "watch": _probe_watch,
}

# A probe asks the host a question it should answer immediately. Longer
# than this and the answer is itself a finding: the two-minute task
# store walk that blocked a release gate was invisible because nothing
# here writes anything down (#191).
SLOW_PROBE_S = 5.0


def _record_probe(capability: str, operation: str, elapsed: float,
                  failure: CapabilityFailure | None) -> None:
    """Write down a probe that refused or was slow. Never raises.

    Silent when the host answers promptly, which is every ordinary
    dispatch: this stream is read by a person asking why a command was
    slow or refused, and a row per invocation would bury that. Imported
    here rather than at module scope because this module is imported by
    scripts that run flat and must not pay for the log writer.
    """
    try:
        try:
            from . import adminlog  # noqa: PLC0415
        except ImportError:  # flat execution, as at the top of this module
            import adminlog  # type: ignore[no-redef]  # noqa: PLC0415
        adminlog.record(
            "capability-probe",
            status="error" if failure is not None else "ok",
            level="error" if failure is not None else "warning",
            capability=capability,
            needed_by=operation,
            duration_s=round(elapsed, 1),
            error_category=failure.code if failure is not None else None,
            message=(failure.detail if failure is not None
                     else f"{capability} probe took {elapsed:.1f}s"),
        )
    except Exception:  # never fail a command for want of a log line
        pass


def check(operation: str,
          capabilities: frozenset[str] | set[str],
          ) -> CapabilityFailure | None:
    """Run the static probes for a subcommand; first failure or None.

    ``capabilities`` is declared by the command spec and may be narrowed to
    what the selected work actually needs. An empty set runs nothing."""
    for capability in sorted(capabilities):
        started = time.monotonic()
        failure = _CAPABILITY_PROBES[capability](operation)
        elapsed = time.monotonic() - started
        if failure is not None or elapsed >= SLOW_PROBE_S:
            _record_probe(capability, operation, elapsed, failure)
        if failure is not None:
            return failure
    return None
