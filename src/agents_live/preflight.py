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
from dataclasses import dataclass, asdict

# Set by cli.py when --json is given, so in-process subcommands and their
# children serialize typed errors as the envelope instead of prose
# (layer 2 of the §3.6 contract - the flag must not stop at preflight).
JSON_ENV_VAR = "AGENTS_LIVE_JSON"


def json_mode() -> bool:
    return os.environ.get(JSON_ENV_VAR, "") == "1"


@dataclass(frozen=True)
class CapabilityFailure:
    code: str          # symbolic error code (see module docstring)
    capability: str    # what was probed, e.g. "crontab", "inotify"
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


def _probe_inotify(operation: str) -> CapabilityFailure | None:
    if shutil.which("inotifywait") is None:
        return CapabilityFailure(
            "dependency_missing", "inotify", operation,
            "inotifywait not found (install inotify-tools)")
    return None


_CAPABILITY_PROBES = {
    "crontab": _probe_crontab,
    "inotify": _probe_inotify,
}


def check(operation: str,
          capabilities: frozenset[str] | set[str],
          ) -> CapabilityFailure | None:
    """Run the static probes for a subcommand; first failure or None.

    ``capabilities`` is declared by the command spec and may be narrowed to
    what the selected work actually needs. An empty set runs nothing."""
    probes = tuple(_CAPABILITY_PROBES[c] for c in sorted(capabilities))
    for probe in probes:
        failure = probe(operation)
        if failure is not None:
            return failure
    return None
