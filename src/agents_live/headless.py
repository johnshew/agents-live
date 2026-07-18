#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["PyYAML", "jsonschema"]
# ///
"""Shared library for the agents-live runtime. NOT an entry point.

Despite the name, this module is never executed directly — it has no
``main()`` and no CLI. It is the common library imported by every
executable in the skill (``run.py``, ``activate.py``, ``status.py``,
``teardown.py``, ``prereqs.py``, ``smoketest.py``, ``dashboard.py``):

* agent discovery and config: ``AgentConfig``, frontmatter parsing,
  ``load_agent_config`` / ``list_agents``, MCP resolution;
* agent invocation: command/env/flag construction per agent family
  (claude, copilot, agency variants), ``headless_agent`` execution with
  timeout/retry, output normalization and usage-stat parsing;
* pipeline stages: ``run_pre_processor`` / ``run_post_processor``;
* structured JSONL logging: ``log_event`` and the stage helpers, plus
  the ``AgentsLiveError`` hierarchy whose subclasses carry the
  ``error_category`` used for triage.

The single-run orchestration that sequences these pieces lives in
``run.py`` (the script cron lines and watchers actually invoke); keeping
this module import-only means cron's entry point stays a thin, readable
script. ``ownership.py`` deliberately does NOT import this module so any
layer can use it without a dependency cycle.
"""
from __future__ import annotations

import glob
import hashlib
import json
import os
import re
import shlex
import shutil
import sys
import signal
import subprocess
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from . import agent_adapters
from . import paths
from .mcp_config_loader import load_mcp_servers

HEADLESS_PROMPT = (
    "You are a headless automated agent. Output ONLY what the prompt asks for. "
    "Never explain, never ask questions, never add commentary."
)
HEADLESS_TIMEOUT = 120
# Safe-output size cap (proposal §3.9): generous default - observed real
# agent outputs are a few KB - overridable per agent via `output-max-bytes`.
DEFAULT_OUTPUT_MAX_BYTES = 1_048_576
VALID_MODES = frozenset({"plan", "write", "pipeline"})
HEADLESS_EMPTY_OUTPUT_RETRIES = 2
HEADLESS_EMPTY_OUTPUT_RETRY_DELAY_S = 2.0
HEADLESS_TIMEOUT_RETRIES = 1
AGENT_HELP_TIMEOUT = 10.0
ANSI_RE = re.compile(
    r"\x1b"          # ESC
    r"(?:"
    r"\[[0-9;?]*[A-Za-z]"   # CSI sequences (including ? for DEC private modes)
    r"|"
    r"\][^\x07\x1b]*(?:\x07|\x1b\\)"  # OSC sequences (terminated by BEL or ST)
    r"|"
    r"[()][0-9A-Za-z]"      # Character set designation
    r"|"
    r"[=>NOMDEHcZ78]"       # Simple two-char sequences
    r")"
)
COPILOT_NOISE_PREFIXES = (
    "●",
    "  │",
    "  └",
    "✗",
    "Script ",
    "Total usage",
    "API time",
    "Total session",
    "Total code",
    "Breakdown",
    " claude-",
    "🤖",
    "📁",
    "📦",
    "🧠",
    "✅",
    "╔",
    "║",
    "╠",
    "╚",
)
# Visible agent prompts live directly under Agents/; `_index_.md` is auto-generated.
EXCLUDED_AGENT_FILE_NAMES = {"_index_.md"}
MAX_RAW_OUTPUT_LOG_LENGTH = 20_000
MAX_LOG_FIELD_LENGTH = MAX_RAW_OUTPUT_LOG_LENGTH
COPILOT_OUTPUT_MAX_LINES = 100
SCRIPT_DIR = Path(__file__).resolve().parent

# The public API — exactly what the consumer scripts (run.py, activate.py,
# status.py, teardown.py, prereqs.py, smoketest.py, dashboard.py) import.
# Everything else in this module is underscore-private implementation.
__all__ = [
    # errors (subclasses carry .category for structured triage)
    "AgentsLiveError",
    "AgentTimeoutError",
    "CliCrashError",
    "OutputParseError",
    "AgentOutputInvalidError",
    "HandlerCrashError",
    "PreProcessorCrashError",
    # data records
    "AgentConfig",
    "AgentResult",
    "PreProcessorResult",
    "ResolvedMcp",
    "ExtractionRecord",
    # paths and discovery
    "repo_root",
    "clean_path",
    "agents_dir",
    "logs_root",
    "ensure_logs_dir",
    "list_agents",
    "load_agent_config",
    "extract_prompt_body",
    # JSONL logging
    "MAX_LOG_FIELD_LENGTH",
    "EventLog",
    "set_log_run_id",
    "log_event",
    "log_stage_start",
    "log_stage_end",
    "system_log",
    # agent execution
    "headless_agent",
    "resolve_agent_command",
    "run_pre_processor",
    "run_post_processor",
    # host trigger state (cron + watchers)
    "current_crontab_lines",
    "install_crontab",
    "cron_line_matches",
    "remove_cron_entries",
    "cron_is_active",
    "packaged_execution",
    "cli_shim_path",
    "run_invocation",
    "ensure_watcher_invocation",
    "build_reboot_watcher_line",
    "install_watcher_reboot_line",
    "remove_watcher_reboot_line",
    "list_reboot_watcher_agent_names",
    "find_watcher_pid",
    "stop_watcher",
    "list_active_agent_names",
    "agent_details",
]


def _extract_traceback(stderr: str) -> str | None:
    """Return the last Python traceback from stderr, or None."""
    marker = "Traceback (most recent call last):"
    idx = stderr.rfind(marker)
    if idx == -1:
        return None
    return stderr[idx:].strip()


class AgentsLiveError(RuntimeError):
    """Base error for Agents Live failures.

    ``category`` feeds the structured ``error_category`` log field that
    self-healing agents triage on. Subclasses override it so callers
    classify by exception type instead of matching message text (which
    misfired when e.g. agent stderr embedded in a crash message happened
    to contain "timed out").
    """
    category = "agent_error"


class AgentTimeoutError(AgentsLiveError):
    """Agent subprocess exceeded its timeout (retryable in headless_agent)."""
    category = "timeout"


class CliCrashError(AgentsLiveError):
    """A required CLI is missing from PATH."""
    category = "cli_crash"


class OutputParseError(AgentsLiveError):
    """Agent output should have contained JSON but none could be parsed."""
    category = "output_parse_error"


class AgentOutputInvalidError(AgentsLiveError):
    """Agent output failed a safe-output validation (proposal §3.9).

    Deliberately NOT ``agent_invalid``: the agent definition may be
    perfectly valid; it is this run's output that violates the declared
    contract (schema, size cap, path roots, or provenance)."""
    category = "agent_output_invalid"


class HandlerCrashError(AgentsLiveError):
    """Handler / post-processor script exited non-zero."""
    category = "handler_crash"


class PreProcessorCrashError(AgentsLiveError):
    """Pre-processor script exited non-zero."""
    category = "pre_processor_crash"


class AgentInvalidError(AgentsLiveError):
    """The agent definition itself is invalid, missing, or ambiguous."""
    category = "agent_invalid"


@dataclass(frozen=True)
class ResolvedMcp:
    flag: str
    env: dict[str, str] = field(default_factory=dict)
    # Raw stdio server spec (.vscode/mcp.json entry). When set on the copilot
    # agent path, _build_agent_command emits it via --additional-mcp-config
    # instead of --mcp, bypassing agency's built-in `npx` proxy (which hangs
    # on tools/call with MCP error -32001).
    stdio_spec: dict | None = None


@dataclass(frozen=True)
class AgentResult:
    output: str
    stderr: str
    model: str | None = None
    tokens_in: str | None = None
    tokens_out: str | None = None
    tokens_cached: str | None = None
    premium_requests: str | None = None
    credits: str | None = None
    cost_usd: str | None = None
    transcript_path: str | None = None
    structured_output: dict | list | None = None


@dataclass(frozen=True)
class PreProcessorResult:
    output: str
    skip: bool = False
    stderr: str = ""


@dataclass(frozen=True)
class AgentConfig:
    name: str
    prompt_path: Path
    # The unattended execution adapter. Frontmatter key is `runtime:`
    # (renamed from `agent:` 2026-07-12, convergence C1 - the agent is
    # the file artifact; this field selects who executes it headlessly).
    runtime: str = "agency copilot"
    mode: str = "plan"
    model: str | None = None
    allow_tools: list[str] = field(default_factory=list)
    handler: str | None = None
    pre_processor: str | None = None
    post_processor: str | None = None
    schedule: list[str] = field(default_factory=list)
    watch_path: list[str] = field(default_factory=list)
    watch_ignore: list[str] = field(default_factory=list)
    mcps: list[str] = field(default_factory=list)
    requested_mcps: list[str] | None = None
    env: dict[str, str] = field(default_factory=dict)
    timeout: int | None = None
    transcript: bool = True
    debounce: int | None = None  # seconds; in-process quiet window before watcher dispatch
    owner: str | None = None  # frontmatter seed for Agents/data/agent-owners.json; consulted only on first activation
    # Safe-output validations (proposal §3.9), enforced by headless_agent
    # before any post-processor sees the output. All opt-in except the
    # size cap, which falls back to DEFAULT_OUTPUT_MAX_BYTES.
    output_schema: dict | str | None = None  # inline JSON Schema, or sidecar file reference
    output_max_bytes: int | None = None
    output_path_roots: list[str] = field(default_factory=list)
    output_provenance: str | None = None  # "strict" or None (accept-and-act default)
    # Ecosystem-standard agent metadata (convergence C1): parsed and
    # passed through so one file is both a standard *.agent.md agent and
    # a triggered agent. Interactive surfaces honor these; the runner
    # does not enforce them yet (tools -> enforcement mapping lands with
    # the C3 adapter work).
    description: str | None = None
    tools: list[str] = field(default_factory=list)
    user_invocable: bool | None = None
    disable_model_invocation: bool | None = None
    argument_hint: str | None = None
    # Populated by _resolve_agent_config for copilot agents: stdio MCP specs that
    # _build_agent_command will merge into a temp --additional-mcp-config file.
    stdio_mcp_specs: dict[str, dict] = field(default_factory=dict)
    # Set by _resolve_agent_config on its return value. Makes a second
    # _resolve_agent_config call a free no-op, so the boundary (headless_agent,
    # run.py) resolves once and the inner builders (_build_agent_command,
    # _build_agent_env) never re-read .vscode/mcp.json.
    resolved: bool = False

    def _resolve_handler_path(self, name: str | None) -> Path | None:
        if not name:
            return None
        if "/" in name or "\\" in name:
            return repo_root() / name
        # Resolve bare handler names relative to the agent's own directory
        return self.prompt_path.parent / "handlers" / name

    @property
    def prompt_reference(self) -> str:
        return _repo_relative(self.prompt_path)

    @property
    def handler_path(self) -> Path | None:
        # post_processor takes precedence, falls back to handler for compat
        return self._resolve_handler_path(self.post_processor or self.handler)

    @property
    def handler_reference(self) -> str | None:
        if not self.handler_path:
            return None
        return _repo_relative(self.handler_path)

    @property
    def pre_processor_path(self) -> Path | None:
        return self._resolve_handler_path(self.pre_processor)

    @property
    def pre_processor_reference(self) -> str | None:
        if not self.pre_processor_path:
            return None
        return _repo_relative(self.pre_processor_path)

    @property
    def post_processor_path(self) -> Path | None:
        return self.handler_path  # same resolution

    @property
    def post_processor_reference(self) -> str | None:
        return self.handler_reference

    @property
    def requires_stdout_json(self) -> bool:
        """True when a post-processor consumes the agent's stdout as JSON.

        Pipeline-mode agents publish their structured output to the pipeline
        MCP store and the post-processor fetches it with
        ``get("/output/...")``, so their stdout is just narration -- non-JSON
        stdout is expected, not an error. Both the parse-error logging and the
        refuse-to-invoke enforcement key off this so they cannot drift apart.
        """
        return bool(self.post_processor) and self.mode != "pipeline"

    @property
    def agent_log(self) -> Path:
        return logs_root() / f"{self.name}.log"

    @property
    def transcript_log(self) -> Path:
        """Path where the full session transcript is written."""
        return logs_root() / f"{self.name}-transcript.md"

    @property
    def all_watch_paths(self) -> list[str]:
        """Return watchPath as a list."""
        return self.watch_path

    def watch_path_absolute_for(self, wp: str) -> Path:
        """Resolve a single watch path string to an absolute path."""
        p = Path(wp)
        if p.is_absolute():
            return p
        return repo_root() / wp

    @property
    def watch_path_absolute(self) -> Path | None:
        """Return the first watch path as an absolute path."""
        if not self.watch_path:
            return None
        return self.watch_path_absolute_for(self.watch_path[0])

    @property
    def trigger_type(self) -> str:
        if self.schedule and self.watch_path:
            return "multi"
        if self.schedule:
            return "cron"
        if self.watch_path:
            return "watcher"
        raise AgentsLiveError(
            f"agent '{self.name}' has no schedule or watchPath in {self.prompt_reference}"
        )


def repo_root() -> Path:
    """Resolve the repo/project root (delegates to the paths resolver)."""
    return paths.resolve_root()


def _repo_relative(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(repo_root()))
    except ValueError:
        return str(path)


def clean_path() -> str:
    path_entries = [Path.home() / ".local" / "bin"]
    node_path = shutil.which("node")
    if not node_path:
        # Cron/headless environments lack nvm; search nvm directories directly
        candidates = sorted(
            glob.glob(str(Path.home() / ".nvm/versions/node/*/bin/node")),
            reverse=True,
        )
        for candidate in candidates:
            if os.access(candidate, os.X_OK):
                node_path = candidate
                break
    if node_path:
        path_entries.append(Path(node_path).resolve().parent)
    agency_path = shutil.which("agency")
    if not agency_path:
        # Fallback: check standard agency install location under cron's minimal PATH
        candidate = Path.home() / ".config" / "agency" / "CurrentVersion" / "agency"
        if candidate.is_file():
            agency_path = str(candidate)
    if agency_path:
        path_entries.insert(0, Path(agency_path).resolve().parent)
    path_entries.extend(Path(part) for part in ("/usr/local/bin", "/usr/bin", "/bin"))
    seen: set[str] = set()
    ordered: list[str] = []
    for entry in path_entries:
        value = str(entry)
        if value not in seen:
            ordered.append(value)
            seen.add(value)
    return os.pathsep.join(ordered)


# ---------------------------------------------------------------------------
# Agent directory resolution (multi-directory support)
# ---------------------------------------------------------------------------

def _load_agent_directories_config() -> list[str]:
    """Read additional agent directories from the project config
    (``paths.load_config`` - root ``.agents-live.toml`` or the
    ``[tool.agents-live]`` pyproject table).

    Returns the raw list of repo-relative directory strings from the
    config's ``agent_directories`` key. Returns ``[]`` if there is no
    config, no ``agent_directories`` entry, or the config is unreadable
    (agent discovery falls back to the default directory).
    """
    try:
        dirs = paths.load_config(repo_root()).get("agent_directories", [])
    except ValueError:
        return []
    if isinstance(dirs, list):
        return [str(d) for d in dirs if d]
    return []


def agents_dir() -> Path:
    """Primary agent directory (Agents/). Used for creating ephemeral
    fixtures; canonical new agents default to ``.claude/agents/``
    (convergence C2 - ephemeral ``_`` fixtures must NOT live in native
    agent directories, where interactive surfaces would list them)."""
    return repo_root() / "Agents"


# Native agent directories (convergence C2): the ecosystem-standard
# discovery locations. A file here is a standard agent definition;
# carrying `schedule:`/`watchPath:` extension fields makes it ALSO a
# scheduled agent. `.claude/agents/` is the default canonical location
# (the one path Claude Code, Copilot CLI, and VS Code all read).
NATIVE_AGENT_DIRS = (".claude/agents", ".github/agents")
_AGENT_MD_SUFFIX = ".agent.md"


def _native_agent_dirs() -> list[Path]:
    root = repo_root()
    return [root / rel for rel in NATIVE_AGENT_DIRS if (root / rel).is_dir()]


def is_native_agent_dir(directory: Path) -> bool:
    root = repo_root()
    resolved = directory.resolve()
    return any(resolved == (root / rel).resolve() for rel in NATIVE_AGENT_DIRS)


def _agent_name(path: Path) -> str:
    """Agent name from a definition filename. Both native spellings strip
    to the bare name: ``foo.agent.md`` (the .github convention) and
    ``foo.md`` are the agent ``foo`` - never the double-stem
    ``foo.agent``."""
    if path.name.endswith(_AGENT_MD_SUFFIX):
        return path.name[: -len(_AGENT_MD_SUFFIX)]
    return path.stem


def _has_triggers(path: Path) -> bool:
    """Lenient trigger probe for native-directory files. A native agent
    file without ``schedule``/``watchPath`` is a plain interactive agent,
    not a scheduled agent: discovery skips it without strict validation (its
    frontmatter is the ecosystem's business, not ours). Unreadable or
    unparseable files are likewise skipped - never fatal to discovery."""
    try:
        data = _extract_frontmatter(path.read_text(encoding="utf-8"), path)
    except (OSError, AgentsLiveError, yaml.YAMLError):
        return False
    return bool(data.get("schedule") or data.get("watchPath"))


def _all_agent_dirs() -> list[Path]:
    """All agent directories: primary (Agents/) plus any extras from config.

    The primary directory is always first.  Additional directories are listed
    in config order.  Non-existent directories are silently skipped.
    Native agent directories are scanned separately (see
    :func:`list_agents` / :func:`load_agent_config`) because their files
    need the trigger probe and suffix handling.
    """
    root = repo_root()
    dirs: list[Path] = [root / "Agents"]
    seen = {dirs[0].resolve()}
    for rel in _load_agent_directories_config():
        p = root / rel
        resolved = p.resolve()
        if resolved not in seen and p.is_dir():
            dirs.append(p)
            seen.add(resolved)
    return dirs


def logs_root() -> Path:
    return repo_root() / "Agents" / "logs"


# ---------------------------------------------------------------------------
# Watcher reboot-persistence registry
# ---------------------------------------------------------------------------
# A bare inotifywait watcher does not survive a reboot, so the "intended to be
# running" state must live somewhere durable. That place is the crontab:
# activating a watcher installs an ``@reboot`` line that re-runs
# ``activate.py --ensure-watcher <name>`` (the guarded, idempotent respawn
# path), and tearing it down removes that line. The presence of the line is
# the single source of truth for "this watcher should be running", and it
# survives reboot natively. ``--ensure-watcher`` carries no ``--name`` token,
# so these lines are invisible to the run.py schedule machinery
# (:func:`cron_line_matches`) and never collide with an agent's cron schedule.

ACTIVATE_SCRIPT_PATH = Path(__file__).resolve().with_name("activate.py")
RUN_SCRIPT_PATH = Path(__file__).resolve().with_name("run.py")


def packaged_execution() -> bool:
    """True when running as the installed ``agents_live`` package (the
    export rewrite sets ``__package__``); False in the flat checkout."""
    return bool(__package__)


def cli_shim_path() -> Path:
    """Absolute path of the installed ``agents-live`` shim (§3.4.2: cron
    does not inherit the interactive tool PATH, so persisted entries pin
    the executable). In a uv tool environment the entry-point script
    lives beside the interpreter; PATH lookup is the fallback."""
    beside_interpreter = Path(sys.executable).with_name("agents-live")
    if beside_interpreter.is_file():
        return beside_interpreter.resolve()
    found = shutil.which("agents-live")
    if found:
        return Path(found).resolve()
    raise AgentsLiveError(
        "cannot resolve the agents-live executable to pin into the "
        "crontab entry (install with `uv tool install agents-live`)")


def run_invocation(name: str) -> list[str]:
    """argv persisted into cron entries to execute one run of *name*.

    Packaged: the pinned shim with an explicit ``--repo`` so nothing at
    fire time depends on ambient state (§3.4.2 self-contained crontab
    lines). Flat checkout: the classic ``uv run --script run.py`` form
    (retired at the F7 flip via ``migrate``). Both forms carry the
    ``--name <name>`` token pair that :func:`cron_line_matches` keys on.
    """
    if packaged_execution():
        return [str(cli_shim_path()), "--repo", str(repo_root()),
                "run", "--name", name, "--quiet"]
    uv = shutil.which("uv") or "uv"
    return [uv, "run", "--script", str(RUN_SCRIPT_PATH),
            "--name", name, "--quiet"]


def ensure_watcher_invocation(name: str) -> list[str]:
    """argv persisted into the @reboot respawn line for *name*'s watcher.
    Both forms carry the ``--ensure-watcher <name>`` token pair the
    matchers key on."""
    if packaged_execution():
        return [str(cli_shim_path()), "--repo", str(repo_root()),
                "start", "--ensure-watcher", name]
    uv = shutil.which("uv") or "uv"
    return [uv, "run", "--script", str(ACTIVATE_SCRIPT_PATH),
            "--ensure-watcher", name]


def _watcher_reboot_line_matches(line: str, name: str) -> bool:
    """Return True if a crontab line is the @reboot watcher respawn for
    name. Keyed on the ``--ensure-watcher <name>`` token pair, which both
    the script-path and packaged-shim line forms carry (and which never
    appears in run.py schedule lines)."""
    try:
        tokens = shlex.split(line)
    except ValueError:
        tokens = line.split()
    return any(
        first == "--ensure-watcher" and second == name
        for first, second in zip(tokens, tokens[1:])
    )


def _reboot_watcher_line_agent_name(line: str) -> str | None:
    """Return the agent name carried by a watcher @reboot line, else None."""
    if "--ensure-watcher" not in line:
        return None
    try:
        tokens = shlex.split(line)
    except ValueError:
        tokens = line.split()
    for first, second in zip(tokens, tokens[1:]):
        if first == "--ensure-watcher":
            return second
    return None


def list_reboot_watcher_agent_names() -> list[str]:
    """Every watcher with an @reboot respawn line installed in this crontab.

    This is the durable "intended watchers" set: the reverse of
    :func:`install_watcher_reboot_line`. The presence of a line means the
    watcher is meant to be running; a deliberate teardown removes it. Returns
    ``[]`` when the crontab is empty or unavailable.
    """
    lines = current_crontab_lines() or []
    names: list[str] = []
    for line in lines:
        agent_name = _reboot_watcher_line_agent_name(line)
        if agent_name is not None:
            names.append(agent_name)
    return sorted(set(names))


def build_reboot_watcher_line(name: str) -> str:
    """The canonical @reboot respawn line for *name*'s watcher in the
    current execution context. Shared by activation and `migrate`'s
    convergence check."""
    return (f"@reboot cd {shlex.quote(str(repo_root()))} && "
            f"{shlex.join(ensure_watcher_invocation(name))} 2>&1")


def install_watcher_reboot_line(name: str) -> str:
    """Install the @reboot respawn line for a watcher (idempotent).

    Replaces any existing line for this agent, preserves the crontab ``PATH=``
    line, and leaves every other entry (including the agent's own run.py
    schedule lines) untouched.
    """
    new_line = build_reboot_watcher_line(name)
    path_line = f"PATH={clean_path()}"
    lines = [line for line in (current_crontab_lines() or [])
             if not _watcher_reboot_line_matches(line, name)
             and not line.startswith("PATH=")]
    lines.insert(0, path_line)
    lines.append(new_line)
    install_crontab(lines)
    return new_line


def remove_watcher_reboot_line(name: str) -> bool:
    """Remove the @reboot respawn line for a watcher. Returns True if removed."""
    lines = current_crontab_lines()
    if lines is None:
        raise AgentsLiveError("crontab is not accessible")
    filtered = [line for line in lines if not _watcher_reboot_line_matches(line, name)]
    if len(filtered) == len(lines):
        return False
    install_crontab(filtered)
    return True


def ensure_logs_dir() -> Path:
    log_dir = logs_root()
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir


_LOG_RUN_ID: str | None = None


def set_log_run_id(run_id: str) -> None:
    """Set the execution identifier stamped on all subsequent log events."""
    global _LOG_RUN_ID
    _LOG_RUN_ID = run_id


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def log_event(log_path: Path, **fields: Any) -> None:
    """Append a single JSONL event to a log file.

    Validates and sanitises fields:
    - String values longer than MAX_LOG_FIELD_LENGTH are truncated
      and a ``_truncated`` flag is added.
    - Caller-supplied ``ts`` is silently dropped (always generated).
    - Non-serialisable values are replaced with their ``repr()``.
    """
    log_path.parent.mkdir(parents=True, exist_ok=True)
    fields.pop("ts", None)  # always generated, never caller-supplied
    fields.setdefault("agent_name", log_path.stem)
    fields.setdefault("event_id", uuid.uuid4().hex)
    if _LOG_RUN_ID:
        fields.setdefault("run_id", _LOG_RUN_ID)
    truncated = False
    sanitised: dict[str, Any] = {}
    for key, value in fields.items():
        if value is None:
            continue
        if isinstance(value, str) and len(value) > MAX_LOG_FIELD_LENGTH:
            sanitised[key] = value[:MAX_LOG_FIELD_LENGTH]
            truncated = True
        else:
            sanitised[key] = value
    if truncated:
        sanitised["_truncated"] = True
    entry: dict[str, Any] = {"ts": _utc_now(), "log_schema": 5, **sanitised}
    try:
        line = json.dumps(entry, separators=(",", ":"), default=str)
    except (TypeError, ValueError):
        # Last resort: repr() the entire payload so we never lose the event
        entry = {"ts": _utc_now(), "log_schema": 5,
                 "agent_name": fields["agent_name"],
                 "event_id": fields["event_id"],
                 "level": "error", "phase": "log_event",
                 "message": f"non-serialisable log payload: {repr(sanitised)[:MAX_LOG_FIELD_LENGTH]}"}
        line = json.dumps(entry, separators=(",", ":"))
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def log_stage_start(log_path: Path, phase: str, **fields: Any) -> None:
    """Log a stage start event."""
    log_event(log_path, phase=phase, status="start", **fields)


def log_stage_end(log_path: Path, phase: str, *, status: str = "ok", duration_s: float | None = None,
                  **fields: Any) -> None:
    """Log a stage completion event."""
    payload: dict[str, Any] = {"phase": phase, "status": status}
    if duration_s is not None:
        payload["duration_s"] = duration_s
    payload.update(fields)
    log_event(log_path, **payload)


def system_log() -> Path:
    return logs_root() / "agents-live.log"


class EventLog:
    """One JSONL event stream with constant fields bound at construction.

    Binds the log path plus fields every event shares (typically
    ``agent_name=``) once, so per-run call sites stop threading them through
    every call — run.py previously hand-rolled exactly this binding as
    ``tlog``/``tlog_start``/``tlog_end`` closures. The module-level
    :func:`log_event` family remains the writer (and the API for one-off
    callers); this class is its run-scoped face, and the natural seed of
    the backlogged ``event_log.py`` extraction.
    """

    def __init__(self, path: Path, **bound: Any) -> None:
        self.path = path
        self.bound = bound

    def event(self, **fields: Any) -> None:
        """Append one event; explicit fields override bound ones."""
        log_event(self.path, **{**self.bound, **fields})

    def stage_start(self, phase: str, **fields: Any) -> None:
        self.event(phase=phase, status="start", **fields)

    def stage_end(self, phase: str, *, status: str = "ok",
                  duration_s: float | None = None, **fields: Any) -> None:
        payload: dict[str, Any] = {"phase": phase, "status": status}
        if duration_s is not None:
            payload["duration_s"] = duration_s
        payload.update(fields)
        self.event(**payload)


def _extract_frontmatter(text: str, prompt_path: Path) -> dict[str, Any]:
    lines = text.splitlines()
    if len(lines) < 3 or lines[0].strip() != "---":
        raise AgentsLiveError(f"no frontmatter in {_repo_relative(prompt_path)}")

    end_index: int | None = None
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            end_index = index
            break
    if end_index is None:
        raise AgentsLiveError(f"unterminated frontmatter in {_repo_relative(prompt_path)}")

    frontmatter_text = "\n".join(lines[1:end_index])
    data = yaml.safe_load(frontmatter_text) or {}
    if not isinstance(data, dict):
        raise AgentsLiveError(f"frontmatter in {_repo_relative(prompt_path)} must be a mapping")
    return data


def extract_prompt_body(text: str) -> str:
    """Return the body of a prompt file (everything after the frontmatter block).

    If the file has no valid frontmatter (missing opening ``---``, fewer than
    3 lines, or no closing ``---``), the entire text is returned stripped.
    This is intentional — the caller receives usable prompt content regardless
    of whether frontmatter is present.
    """
    lines = text.splitlines()
    if len(lines) < 3 or lines[0].strip() != "---":
        return text.strip()
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            return "\n".join(lines[index + 1:]).strip()
    return text.strip()


def _opt_str(value: Any) -> str | None:
    """Coerce an optional frontmatter scalar: None or "" becomes None, else str."""
    return None if value in (None, "") else str(value)


def _str_list(value: Any, field_name: str, prompt: Path) -> list[str]:
    """Coerce a frontmatter string-or-list field to a list of strings.

    Raises :class:`AgentsLiveError` when the value is neither a string,
    a list, nor empty.
    """
    if value in (None, ""):
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(item) for item in value if item not in (None, "")]
    raise AgentsLiveError(f"{field_name} must be a list in {_repo_relative(prompt)}")


def _parse_frontmatter(prompt_path: str | Path) -> AgentConfig:
    prompt = Path(prompt_path)
    if not prompt.is_absolute():
        prompt = repo_root() / prompt
    if not prompt.is_file():
        raise AgentsLiveError(f"prompt not found: {_repo_relative(prompt)}")

    data = _extract_frontmatter(prompt.read_text(encoding="utf-8"), prompt)
    name = _agent_name(prompt)
    # `runtime:` selects the unattended execution adapter (renamed from
    # `agent:` 2026-07-12, convergence C1 - avoids the ecosystem's
    # `agents:` subagent allowlist and "the agent is the file" framing).
    # Clean break: a leftover `agent:` key fails loudly rather than
    # being silently ignored in favor of the default runtime.
    if "agent" in data:
        raise AgentInvalidError(
            f"frontmatter key `agent:` was renamed to `runtime:` "
            f"(2026-07-12); update {_repo_relative(prompt)}")
    known_runtimes = set(agent_adapters.names()) | {"none"}
    # No default runtime: the kernel must not assume a deployment's
    # adapters (fail closed, TT-SEC-002 - the old default was the private
    # `agency copilot`, which does not exist in the public package).
    runtime = _opt_str(data.get("runtime"))
    if not runtime:
        raise AgentInvalidError(
            f"no `runtime:` declared in {_repo_relative(prompt)}; "
            f"expected one of: {', '.join(sorted(known_runtimes))}")
    if runtime not in known_runtimes:
        raise AgentInvalidError(
            f"unknown runtime '{runtime}' in {_repo_relative(prompt)}; "
            f"expected one of: {', '.join(sorted(known_runtimes))}")
    mode = str(data.get("mode") or "plan")
    if mode not in VALID_MODES:
        raise AgentsLiveError(
            f"unknown mode '{mode}' in {_repo_relative(prompt)}; "
            f"expected one of: {', '.join(sorted(VALID_MODES))}"
        )
    model = _opt_str(data.get("model"))
    allow_tools = _str_list(data.get("allow-tools"), "allow-tools", prompt)
    handler = _opt_str(data.get("handler"))
    pre_processor = _opt_str(data.get("pre-processor"))
    post_processor = _opt_str(data.get("post-processor"))
    schedule_value = data.get("schedule")
    if isinstance(schedule_value, list):
        schedule = [str(v) for v in schedule_value if v not in (None, "")]
    elif schedule_value in (None, ""):
        schedule = []
    else:
        schedule = [str(schedule_value)]
    watch_value = data.get("watchPath")
    if isinstance(watch_value, list):
        watch_path = [str(v) for v in watch_value if v not in (None, "")]
    elif watch_value in (None, ""):
        watch_path = []
    else:
        watch_path = [str(watch_value)]

    watch_ignore = _str_list(data.get("watchIgnore"), "watchIgnore", prompt)
    mcps = _str_list(data.get("mcps"), "mcps", prompt)

    env_raw = data.get("env") or {}
    if not isinstance(env_raw, dict):
        raise AgentsLiveError(f"env must be a mapping in {_repo_relative(prompt)}")
    env = {str(key): str(value) for key, value in env_raw.items()}

    timeout_raw = data.get("timeout")
    timeout = int(timeout_raw) if timeout_raw not in (None, "") else None

    transcript = bool(data.get("transcript", True))

    debounce_raw = data.get("debounce")
    debounce = int(debounce_raw) if debounce_raw not in (None, "") else None

    owner = _opt_str(data.get("owner"))

    # C2 validations for agents in native agent directories. Files
    # without triggers there are interactive agents - discovery never
    # parses them - but they can still be loaded BY NAME (e.g. spawned
    # on demand), so the processor-path rule applies to every parsed
    # native file while the naming rules apply only to triggered ones.
    if is_native_agent_dir(prompt.parent):
        if schedule or watch_path:
            if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", name):
                raise AgentInvalidError(
                    f"agent name '{name}' in a native agent directory must be "
                    f"lowercase alphanumeric with hyphens (Claude Code's "
                    f"subagent name rule): {_repo_relative(prompt)}")
            declared_name = _opt_str(data.get("name"))
            if declared_name and declared_name != name:
                raise AgentInvalidError(
                    f"frontmatter name '{declared_name}' does not match the "
                    f"filename-derived agent name '{name}' in "
                    f"{_repo_relative(prompt)}; the filename is authoritative")
        for label, ref in (("pre-processor", pre_processor),
                           ("post-processor", post_processor),
                           ("handler", handler)):
            if ref and "/" not in ref and "\\" not in ref:
                raise AgentInvalidError(
                    f"{label} '{ref}' in {_repo_relative(prompt)} must be a "
                    f"repo-relative path: native agent directories hold no "
                    f"executables (convergence C2), so bare names have "
                    f"nothing to resolve against")

    # Ecosystem-standard agent metadata (convergence C1): parse and pass
    # through; interactive surfaces honor these, the runner surfaces
    # `description` in status. Booleans are tri-state (absent = None)
    # so downstream policy can distinguish "unset" from an explicit
    # choice.
    description = _opt_str(data.get("description"))
    tools = _str_list(data.get("tools"), "tools", prompt)
    user_invocable_raw = data.get("user-invocable")
    user_invocable = None if user_invocable_raw is None else bool(user_invocable_raw)
    dmi_raw = data.get("disable-model-invocation")
    disable_model_invocation = None if dmi_raw is None else bool(dmi_raw)
    argument_hint = _opt_str(data.get("argument-hint"))

    output_schema_raw = data.get("output-schema")
    if output_schema_raw in (None, ""):
        output_schema: dict | str | None = None
    elif isinstance(output_schema_raw, dict):
        output_schema = output_schema_raw
    elif isinstance(output_schema_raw, str):
        output_schema = output_schema_raw
    else:
        raise AgentsLiveError(
            f"output-schema must be a mapping or a file reference in {_repo_relative(prompt)}")

    output_max_bytes_raw = data.get("output-max-bytes")
    output_max_bytes = (
        int(output_max_bytes_raw) if output_max_bytes_raw not in (None, "") else None)
    if output_max_bytes is not None and output_max_bytes <= 0:
        raise AgentsLiveError(
            f"output-max-bytes must be positive in {_repo_relative(prompt)}")

    output_path_roots = _str_list(
        data.get("output-path-roots"), "output-path-roots", prompt)
    for root in output_path_roots:
        if Path(root).is_absolute():
            raise AgentsLiveError(
                f"output-path-roots must be repo-relative in {_repo_relative(prompt)}: {root}")

    output_provenance = _opt_str(data.get("output-provenance"))
    if output_provenance not in (None, "strict"):
        raise AgentsLiveError(
            f"output-provenance must be 'strict' (or absent) in {_repo_relative(prompt)}")

    if mode == "pipeline" and (output_schema or output_path_roots or output_provenance):
        # Pipeline output flows through the pipeline MCP store, not stdout;
        # declaring stdout validations there would silently validate the
        # wrong surface. Fail loudly instead of ignoring.
        raise AgentsLiveError(
            f"output-schema/output-path-roots/output-provenance validate stdout "
            f"and do not apply to mode: pipeline in {_repo_relative(prompt)}")

    return AgentConfig(
        name=name,
        prompt_path=prompt,
        runtime=runtime,
        mode=mode,
        model=model,
        allow_tools=allow_tools,
        handler=handler,
        pre_processor=pre_processor,
        post_processor=post_processor,
        schedule=schedule,
        watch_path=watch_path,
        watch_ignore=watch_ignore,
        mcps=mcps,
        env=env,
        timeout=timeout,
        transcript=transcript,
        debounce=debounce,
        owner=owner,
        output_schema=output_schema,
        output_max_bytes=output_max_bytes,
        output_path_roots=output_path_roots,
        output_provenance=output_provenance,
        description=description,
        tools=tools,
        user_invocable=user_invocable,
        disable_model_invocation=disable_model_invocation,
        argument_hint=argument_hint,
    )


def load_agent_config(name: str) -> AgentConfig:
    """Load an agent by name, searching the configured agent directories and
    the native agent directories (both filename spellings there).

    Raises :class:`AgentInvalidError` if the name is found in more than one
    location (ambiguous) or in none.
    """
    matches: list[Path] = []
    for d in _all_agent_dirs():
        candidate = d / f"{name}.md"
        if candidate.is_file():
            matches.append(candidate)
    for d in _native_agent_dirs():
        for filename in (f"{name}.md", f"{name}{_AGENT_MD_SUFFIX}"):
            candidate = d / filename
            if candidate.is_file():
                matches.append(candidate)
    if len(matches) > 1:
        locations = ", ".join(_repo_relative(m) for m in matches)
        raise AgentInvalidError(
            f"agent '{name}' found in multiple locations: {locations}"
        )
    if not matches:
        raise AgentInvalidError(f"agent '{name}' not found in any agent directory")
    return _parse_frontmatter(matches[0])


def agent_file_exists(name: str) -> bool:
    """Whether ANY definition file for *name* exists - classic dirs and
    native dirs, both filename spellings - WITHOUT parsing it.

    This is the deletion predicate for orphan pruning (TT-001 review
    finding): a malformed or transiently unreadable definition must read
    as "exists but broken" (abstain), never as "deleted" (teardown).
    Discovery's lenient trigger probe deliberately skips broken native
    files, so absence from :func:`list_agents` is NOT proof the file is
    gone."""
    for d in _all_agent_dirs():
        if (d / f"{name}.md").is_file():
            return True
    for d in _native_agent_dirs():
        for filename in (f"{name}.md", f"{name}{_AGENT_MD_SUFFIX}"):
            if (d / filename).is_file():
                return True
    return False


def list_spawned_definitions() -> list[str]:
    """Names of native-directory definitions WITHOUT triggers that still
    declare a ``runtime:`` - manually spawned agents (e.g.
    exercise-judgment, dispatched by another agent rather than by
    cron/watcher). They are deliberately absent from :func:`list_agents`
    (no triggers = not activated, not pruned), but inventory consumers
    auditing agent-backed work include them."""
    names: list[str] = []
    for d in _native_agent_dirs():
        for path in sorted(d.glob("*.md")):
            if not path.is_file() or path.name in EXCLUDED_AGENT_FILE_NAMES:
                continue
            try:
                data = _extract_frontmatter(path.read_text(encoding="utf-8"), path)
            except (OSError, AgentsLiveError, yaml.YAMLError):
                continue
            if data.get("schedule") or data.get("watchPath"):
                continue
            if "runtime" in data:
                names.append(_agent_name(path))
    return sorted(names)


def list_agents() -> list[str]:
    """List all agent names: every ``*.md`` in the configured agent
    directories, plus native-agent-directory files that carry triggers
    (a native file without ``schedule``/``watchPath`` is a plain
    interactive agent and is skipped - `status`, `start --all`, and
    orphan pruning never touch it).

    Raises :class:`AgentsLiveError` if a name appears in more than one
    location.
    """
    seen: dict[str, Path] = {}  # name → first directory it appeared in
    names: list[str] = []

    def _add(name: str, directory: Path) -> None:
        if name in seen:
            raise AgentsLiveError(
                f"agent '{name}' exists in both {_repo_relative(seen[name])} "
                f"and {_repo_relative(directory)}"
            )
        seen[name] = directory
        names.append(name)

    for d in _all_agent_dirs():
        if not d.is_dir():
            continue
        for path in d.glob("*.md"):
            if not path.is_file() or path.name in EXCLUDED_AGENT_FILE_NAMES:
                continue
            _add(path.stem, d)
    for d in _native_agent_dirs():
        for path in d.glob("*.md"):
            if not path.is_file() or path.name in EXCLUDED_AGENT_FILE_NAMES:
                continue
            if not _has_triggers(path):
                continue
            _add(_agent_name(path), d)
    return sorted(names)


def _load_mcp_servers() -> dict:
    """Load MCP server definitions from .vscode/mcp.json."""
    return load_mcp_servers(repo_root())


def _resolve_mcp(name: str, runtime: str = "claude") -> ResolvedMcp:
    servers = _load_mcp_servers()
    server = servers.get(name)
    if not isinstance(server, dict):
        return ResolvedMcp(flag=name)

    env = {str(key): str(value) for key, value in (server.get("env") or {}).items()}
    server_type = str(server.get("type") or "").strip().lower()
    if server_type == "http":
        url = str(server.get("url") or "").strip()
        if not url:
            return ResolvedMcp(flag=name, env=env)
        flag = f"remote --url {url}"
        oauth_client_id = str(server.get("oauthClientId") or "").strip()
        if oauth_client_id:
            flag = f"{flag} --entra-client-id {oauth_client_id}"
        return ResolvedMcp(flag=flag, env=env)

    command = server.get("command")
    args = [str(item) for item in server.get("args") or []]

    if command and command != "npx":
        # Generic stdio server launched by an arbitrary command (e.g.
        # `uv run --script .../msgraph_mcp.py`).  Agency's --mcp only knows its
        # built-in catalog plus the npx proxy, so a custom-command stdio server
        # can reach copilot only via --additional-mcp-config.  Preserve the raw
        # command/args/env so copilot launches it directly.  (On non-copilot
        # agents stdio_spec is ignored and the bare name is passed to --mcp,
        # matching prior behavior.)
        spec: dict = {
            "type": "stdio",
            "command": str(command),
            "args": args,
            "tools": ["*"],
        }
        if env:
            spec["env"] = dict(env)
        return ResolvedMcp(flag=name, env=env, stdio_spec=spec)

    if command != "npx":
        return ResolvedMcp(flag=name, env=env)

    package_name = next((arg for arg in args if not arg.startswith("-")), None)
    if not package_name:
        return ResolvedMcp(flag=name, env=env)

    extras = [arg for arg in args if arg not in {"-y", package_name}]
    # Both agency and claude need --package; agency also needs --transport stdio
    # to prevent the npx proxy from defaulting to HTTP transport.
    flag = f"npx --package {package_name} --transport stdio"
    if extras:
        flag = f"{flag} -- {' '.join(extras)}"
    # Preserve the raw stdio spec so copilot can launch it directly via
    # --additional-mcp-config; agency's npx proxy hop hangs on tools/call.
    spec = {
        "type": "stdio",
        "command": "npx",
        "args": args,
        "tools": ["*"],
    }
    if env:
        spec["env"] = dict(env)
    return ResolvedMcp(flag=flag, env=env, stdio_spec=spec)


def _resolve_agent_config(config: AgentConfig) -> AgentConfig:
    # Already-resolved configs pass through untouched: headless_agent (and
    # run.py) resolve at the boundary, and the inner builders
    # (_build_agent_command, _build_agent_env, _run_handler, run_pre_processor)
    # call this defensively for direct/test callers. The marker makes the
    # defensive call free — no .vscode/mcp.json re-read, no re-entrancy
    # hazard to keep idempotent by hand.
    if config.resolved:
        return config
    resolved_mcps: list[str] = []
    requested_mcps = list(config.requested_mcps) if config.requested_mcps is not None else list(config.mcps)
    env = dict(config.env)
    stdio_mcp_specs: dict[str, dict] = dict(config.stdio_mcp_specs)
    stdio_via_config = (config.runtime != "none"
                        and _adapter(config.runtime).behavior.stdio_mcps_via_config)
    # Always emit an explicit --mcp flag for every requested server.  We do not
    # rely on workspace auto-load (.mcp.json / .vscode/mcp.json) for triggered
    # agents because the two config files drift and the various CLIs disagree on
    # which one they read.  _build_agent_command() pairs this with
    # --disable-mcp-server for every workspace server, so the only MCPs the
    # agent sees are the ones the agent definition explicitly declared.
    # Exception: on the copilot family, npx-stdio servers route via
    # --additional-mcp-config to avoid agency's npx proxy hop (which hangs
    # on tools/call).
    for mcp_name in config.mcps:
        resolved = _resolve_mcp(mcp_name, runtime=config.runtime)
        if stdio_via_config and resolved.stdio_spec is not None:
            stdio_mcp_specs[mcp_name] = resolved.stdio_spec
        else:
            resolved_mcps.append(resolved.flag)
        env.update(resolved.env)
    return replace(
        config,
        mcps=resolved_mcps,
        requested_mcps=requested_mcps,
        env=env,
        stdio_mcp_specs=stdio_mcp_specs,
        resolved=True,
    )


def _adapter(runtime: str) -> agent_adapters.AgentAdapter:
    """The registered adapter for a runtime name, family-validated.

    ``"none"`` has no adapter; callers must branch on it first. Unknown
    names and unsupported families fail closed as AgentsLiveError.
    """
    try:
        adapter = agent_adapters.get(runtime)
        adapter.behavior  # fail closed on an unsupported family too
        return adapter
    except agent_adapters.UnknownRuntimeError as exc:
        raise AgentsLiveError(str(exc)) from exc


def _runtime_family(runtime: str) -> str:
    """The adapter family ("claude" | "copilot") for a runtime name."""
    return _adapter(runtime).family


def _build_runtime_flags(runtime: str, mode: str, allow_tools: list[str] | None = None) -> list[str]:
    """Permission/tool flags for one agent invocation.

    Fail-closed policy checks live here; the per-family flag vocabulary
    lives on the adapter's family behavior (agent_adapters).
    """
    resolved_allow_tools = list(allow_tools or [])
    # Fail closed: an unrecognized mode must never fall through to write
    # (claude write mode is --dangerously-skip-permissions).
    if mode not in VALID_MODES:
        raise AgentsLiveError(
            f"unknown mode '{mode}'; expected one of: {', '.join(sorted(VALID_MODES))}"
        )
    if runtime == "none":
        return []
    adapter = _adapter(runtime)
    # TT-SEC-002 anchor: refuse a mode the adapter does not declare
    # enforceable rather than guessing at flags.
    if mode not in adapter.modes:
        raise AgentsLiveError(
            f"runtime '{runtime}' does not declare mode '{mode}' enforceable")
    return adapter.mode_flags(mode, resolved_allow_tools)


def _runtime_binary(runtime: str) -> list[str]:
    return list(_adapter(runtime).binary)


@lru_cache(maxsize=8)
def _runtime_supported_flags(runtime: str) -> frozenset[str]:
    try:
        completed = subprocess.run(
            [*_runtime_binary(runtime), "--help"],
            capture_output=True,
            text=True,
            check=False,
            env={"HOME": os.environ.get("HOME", str(Path.home())), "PATH": clean_path()},
            timeout=AGENT_HELP_TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired):
        return frozenset()
    help_text = f"{completed.stdout}\n{completed.stderr}"
    supported_flags: set[str] = set()
    for match in re.finditer(r"(?<!\w)--[A-Za-z][A-Za-z0-9-]*", help_text):
        supported_flags.add(match.group(0))
    return frozenset(supported_flags)


def _workspace_mcp_server_names() -> list[str]:
    """Return the names of all MCP servers the Copilot CLI will auto-load.

    Checks both .vscode/mcp.json (used by headless config) and .mcp.json
    (loaded by the Copilot CLI at runtime).
    """
    servers = set(_load_mcp_servers().keys())
    alt_path = repo_root() / ".mcp.json"
    if alt_path.is_file():
        try:
            import json as _json
            data = _json.loads(alt_path.read_text(encoding="utf-8"))
            alt = data.get("mcpServers") or data.get("servers") or {}
            servers.update(alt.keys())
        except (OSError, ValueError):
            pass
    return sorted(servers)


def _write_stdio_mcp_config(specs: dict[str, dict]) -> Path:
    """Write stdio MCP specs to a temp JSON file for copilot --additional-mcp-config.

    Bypasses agency's built-in `npx` proxy, which hangs on tools/call with
    MCP error -32001. Tool names will appear as `<server>-<tool>` rather than
    `npx-<tool>`. The file is left on disk; copilot reads it at startup and
    /tmp is cleaned by the OS.
    """
    fd, path = tempfile.mkstemp(prefix="agents-live-stdio-mcp-", suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump({"mcpServers": specs}, fh, indent=2)
    return Path(path)


def _build_agent_command(config: AgentConfig, prompt_text: str) -> list[str]:
    resolved = _resolve_agent_config(config)
    if resolved.runtime == "none":
        raise AgentsLiveError("agent: none does not have an agent command")
    adapter = _adapter(resolved.runtime)
    command = [
        *adapter.binary,
        "-p",
        prompt_text,
        *_build_runtime_flags(resolved.runtime, resolved.mode, resolved.allow_tools),
        *adapter.headless_flags(HEADLESS_PROMPT),
    ]
    if resolved.mode == "pipeline":
        command.extend(adapter.pipeline_mcp_flags(resolved.env))
    if resolved.model:
        command.extend(["--model", resolved.model])
    for mcp in resolved.mcps:
        command.extend(["--mcp", mcp])
    if adapter.behavior.confines_workspace_mcps:
        if resolved.stdio_mcp_specs:
            cfg_path = _write_stdio_mcp_config(resolved.stdio_mcp_specs)
            command.extend(["--additional-mcp-config", f"@{cfg_path}"])
        supported_flags = _runtime_supported_flags(resolved.runtime)
        # MCP confinement must FAIL CLOSED (cross-review High 1 /
        # TT-SEC-002): if the help probe cannot prove the disable flags,
        # refusing to run beats silently auto-loading every workspace MCP
        # server (which can include write-capable mail/Graph). A probe
        # failure is never evidence that skipping the flags is safe.
        # Disable every workspace MCP server: _resolve_agent_config() always
        # emits explicit --mcp flags for the servers the agent wants, so
        # the agent's mcps: frontmatter is the single source of truth.
        # Exception: stdio servers re-added via --additional-mcp-config
        # must not also be disabled, or the disable wins and the server
        # vanishes.
        confinement_needed = [
            s for s in _workspace_mcp_server_names()
            if s not in resolved.stdio_mcp_specs
        ]
        if confinement_needed:
            if "--disable-mcp-server" not in supported_flags:
                raise AgentsLiveError(
                    f"cannot prove MCP confinement for agent "
                    f"'{resolved.runtime}': flag probe did not confirm "
                    f"--disable-mcp-server; refusing to run with workspace "
                    f"MCP servers auto-loaded")
            for server_name in confinement_needed:
                command.extend(["--disable-mcp-server", server_name])
        # Additive when the CLI advertises it (current copilot --help does
        # not); workspace confinement above is the required, provable
        # mechanism. Whether default MCPs need their own proof is an open
        # backlog question.
        if "--no-default-mcps" in supported_flags:
            command.append("--no-default-mcps")
        # Session transcript capture: --share writes a full markdown transcript
        # (tool calls, reasoning, results) to the specified path.
        if resolved.transcript and "--share" in supported_flags:
            ensure_logs_dir()
            command.extend(["--share", str(resolved.transcript_log)])
    return command


def resolve_agent_command(name: str, prompt_text: str | None = None) -> str:
    config = load_agent_config(name)
    if prompt_text is None:
        body = extract_prompt_body(config.prompt_path.read_text(encoding="utf-8"))
        prompt_text = body
    if config.runtime == "none":
        if not config.handler_path:
            raise AgentsLiveError("agent: none requires a handler")
        cmd = _build_handler_command(config.handler_path)
        return shlex.join(cmd)
    return shlex.join(_build_agent_command(config, prompt_text))


def _build_agent_env(config: AgentConfig) -> dict[str, str]:
    env = {"HOME": os.environ.get("HOME", str(Path.home())), "PATH": clean_path()}
    env.update(_resolve_agent_config(config).env)
    return env


def _repair_json_quotes(text: str) -> str | None:
    """Attempt to fix unescaped double quotes and invalid escapes in JSON.

    LLMs sometimes produce JSON with:
    - Unescaped inner quotes (ASCII 0x22 inside string values)
    - Invalid escape sequences like \\> or \\* (markdown escapes in JSON strings)

    This function iteratively finds parse errors and fixes them.
    """
    repaired = text
    for _ in range(50):  # cap iterations
        try:
            json.loads(repaired)
            return repaired
        except json.JSONDecodeError as e:
            if e.pos is None or e.pos >= len(repaired):
                return None
            fixed = False
            # Case 1: Invalid escape sequence at error position
            if e.msg == "Invalid \\escape" and e.pos < len(repaired) - 1:
                # The backslash is at e.pos, followed by an invalid char
                # Fix by doubling the backslash (making it literal)
                repaired = repaired[:e.pos] + '\\' + repaired[e.pos:]
                fixed = True
            # Case 2: Unescaped quote - scan backwards from error position
            if not fixed:
                for pos in range(e.pos, max(-1, e.pos - 20), -1):
                    if pos >= len(repaired) or repaired[pos] != '"':
                        continue
                    num_backslashes = 0
                    p = pos - 1
                    while p >= 0 and repaired[p] == '\\':
                        num_backslashes += 1
                        p -= 1
                    if num_backslashes % 2 == 0:
                        repaired = repaired[:pos] + '\\"' + repaired[pos + 1:]
                        fixed = True
                        break
            if not fixed:
                return None
    return None


@dataclass(frozen=True)
class ExtractionRecord:
    """Provenance of a JSON value extracted from agent output (TT-PY-006
    mechanical half). ``source`` is ``"stdout"`` (the whole stdout was one
    JSON document - the clean contract), ``"fence"`` (a ```json code
    fence), ``"scan"`` (raw_decode scan over mixed text), or ``"none"``.
    ``candidate_count`` counts the usable candidates the selection
    heuristics chose among; anything above 1 means ambiguity."""
    text: str
    source: str
    repaired: bool = False
    candidate_count: int = 0


def _extract_json_value(text: str) -> ExtractionRecord:
    # Fast path: the whole stdout is one JSON document - the well-behaved
    # agent contract, and the only shape `output-provenance: strict`
    # accepts.
    whole = text.strip()
    if whole.startswith(("{", "[")):
        try:
            parsed = json.loads(whole)
        except json.JSONDecodeError:
            pass
        else:
            if isinstance(parsed, (dict, list)):
                return ExtractionRecord(whole, "stdout", False, 1)

    # Try all code fences; pick the largest one that parses as valid JSON.
    # The agent may output JSON via a shell tool (with unescaped inner quotes)
    # AND also echo valid JSON in its own response text.
    fenced_matches = list(re.finditer(r"```json\s*(.*?)```", text, flags=re.DOTALL | re.IGNORECASE))
    best_fenced: str = ""
    fenced_ok = 0
    largest_failed: str = ""
    for fenced_match in fenced_matches:
        candidate = fenced_match.group(1).strip()
        if not candidate:
            continue
        try:
            json.loads(candidate)
            fenced_ok += 1
            if len(candidate) > len(best_fenced):
                best_fenced = candidate
        except json.JSONDecodeError:
            _diag = candidate[:200] if len(candidate) > 200 else candidate
            sys.stderr.write(f"[extract_json] code fence found ({len(candidate)} chars) but JSON parse failed; first 200: {repr(_diag)}\n")
            if len(candidate) > len(largest_failed):
                largest_failed = candidate
    if best_fenced:
        return ExtractionRecord(best_fenced, "fence", False, fenced_ok)

    # If the largest code fence failed to parse, attempt repair
    if largest_failed and len(largest_failed) > len(best_fenced):
        repaired = _repair_json_quotes(largest_failed)
        if repaired:
            sys.stderr.write(f"[extract_json] repaired JSON from code fence ({len(largest_failed)} -> {len(repaired)} chars)\n")
            return ExtractionRecord(repaired, "fence", True, 1)

    decoder = json.JSONDecoder()
    # Prefer dict matches over array matches (agent output is typically a dict
    # with results/why/actions keys; arrays inside tool responses are false positives).
    # Among dicts, prefer the largest one (agent output is bigger than tool responses).
    first_array = ""
    largest_dict = ""
    dict_count = 0
    index = 0
    while index < len(text):
        char = text[index]
        if char == "{":
            try:
                _, end = decoder.raw_decode(text, index)
            except json.JSONDecodeError:
                index += 1
                continue
            extracted = text[index:end].strip()
            dict_count += 1
            if len(extracted) > len(largest_dict):
                largest_dict = extracted
            # Nested dicts are strictly smaller than this one; skip past them.
            index = end
        elif char == "[" and not first_array:
            try:
                _, end = decoder.raw_decode(text, index)
            except json.JSONDecodeError:
                index += 1
                continue
            first_array = text[index:end].strip()
            # Keep scanning inside the array: a nested dict still wins.
            index += 1
        else:
            index += 1
    if not fenced_matches and (largest_dict or first_array):
        chosen = largest_dict or first_array
        sys.stderr.write(f"[extract_json] no code fence match; using raw_decode fallback ({len(chosen)} chars, type={'dict' if largest_dict else 'array'})\n")
    if largest_dict or first_array:
        return ExtractionRecord(
            largest_dict or first_array, "scan", False,
            dict_count + (1 if first_array else 0))
    return ExtractionRecord("", "none", False, 0)


def _extract_first_json_value(text: str) -> str:
    """The extracted JSON text only; see :func:`_extract_json_value` for
    the provenance-carrying form."""
    return _extract_json_value(text).text


def _filtered_copilot_output(text: str) -> str:
    cleaned_lines: list[str] = []
    for raw_line in ANSI_RE.sub("", text).replace("\r", "").splitlines():
        line = raw_line.rstrip()
        if not line:
            continue
        if line.startswith(COPILOT_NOISE_PREFIXES):
            continue
        cleaned_lines.append(line)
        if len(cleaned_lines) >= COPILOT_OUTPUT_MAX_LINES:
            break
    return "\n".join(cleaned_lines)


_USAGE_RE = re.compile(
    r"^\s*(\S+)\s+([\d.]+[kKmM]?)\s+in,\s+([\d.]+[kKmM]?)\s+out(?:,\s+([\d.]+[kKmM]?)\s+cached)?",
)
_PREMIUM_RE = re.compile(
    r"Total usage est:\s+([\d.]+)\s+Premium requests",
)
# Copilot / Agency (v2026.4.9+) compact format:
#   Requests  3 Premium (15s)
#   Tokens    ↑ 55.8k • ↓ 805 • 27.8k (cached)
_COPILOT_PREMIUM_RE = re.compile(
    r"Requests\s+([\d.]+)\s+Premium",
)
_COPILOT_CREDITS_RE = re.compile(
    r"AI Credits\s*([\d.]+)",
)
_COPILOT_TOKENS_RE = re.compile(
    r"Tokens\s+[↑⬆]\s*([\d.]+[kKmM]?)\s*[•·]\s*[↓⬇]\s*([\d.]+[kKmM]?)\s*[•·]\s*([\d.]+[kKmM]?)\s*\(cached\)",
)


def _parse_usage_stats(stderr: str) -> dict[str, str | None]:
    """Extract model name, tokens, and premium requests from agent stderr.

    Supports multiple formats:
    - Old agency: ``Total usage est: N Premium requests`` / ``model Nk in, N out, N cached``
    - Copilot / new agency: ``Requests N Premium (Ns)`` / ``Tokens ↑ Nk • ↓ N • Nk (cached)``
    """
    result: dict[str, str | None] = {
        "model": None, "tokens_in": None, "tokens_out": None,
        "tokens_cached": None, "premium_requests": None, "credits": None,
    }
    for line in stderr.splitlines():
        # Old agency format
        m = _PREMIUM_RE.search(line)
        if m:
            result["premium_requests"] = m.group(1)
        # New copilot/agency format (legacy)
        m = _COPILOT_PREMIUM_RE.search(line)
        if m:
            result["premium_requests"] = m.group(1)
        # Current agency format: ``AI Credits 8.3 (27s)``
        m = _COPILOT_CREDITS_RE.search(line)
        if m:
            result["credits"] = m.group(1)
        # Old agency format: ``model 162.3k in, 971 out, 0 cached``
        m = _USAGE_RE.search(line)
        if m:
            result["model"] = m.group(1)
            result["tokens_in"] = m.group(2)
            result["tokens_out"] = m.group(3)
            result["tokens_cached"] = m.group(4)
        # New copilot/agency format: ``Tokens ↑ 55.8k • ↓ 805 • 27.8k (cached)``
        m = _COPILOT_TOKENS_RE.search(line)
        if m:
            result["tokens_in"] = m.group(1)
            result["tokens_out"] = m.group(2)
            result["tokens_cached"] = m.group(3)
    return result


def _parse_claude_json_output(raw_output: str) -> tuple[str, dict[str, str | None]]:
    """Parse claude ``--output-format json`` response.

    Returns ``(result_text, usage_dict)`` where *usage_dict* has the same keys
    as ``_parse_usage_stats()``.  Falls through gracefully if the JSON is
    malformed — returns the raw output string and empty usage.
    """
    usage: dict[str, str | None] = {
        "model": None, "tokens_in": None, "tokens_out": None,
        "tokens_cached": None, "premium_requests": None, "credits": None,
    }
    try:
        data = json.loads(raw_output)
    except (json.JSONDecodeError, TypeError):
        return raw_output, usage
    if not isinstance(data, dict):
        # Valid JSON but not the expected envelope (e.g. a bare array)
        return raw_output, usage

    result_text = data.get("result", raw_output)
    if not isinstance(result_text, str):
        result_text = raw_output

    def _num(value: Any) -> int | float:
        """Coerce a token/cost field to a number; anything else counts as 0."""
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return 0
        return value

    # Extract usage from claude JSON response
    u = data.get("usage", {})
    if not isinstance(u, dict):
        u = {}
    input_tokens = _num(u.get("input_tokens")) + _num(u.get("cache_read_input_tokens"))
    output_tokens = _num(u.get("output_tokens"))
    cached_tokens = _num(u.get("cache_read_input_tokens"))

    def _fmt(n: int | float) -> str:
        if n >= 1000:
            return f"{n / 1000:.1f}k"
        return str(n)

    usage["tokens_in"] = _fmt(input_tokens)
    usage["tokens_out"] = _fmt(output_tokens)
    usage["tokens_cached"] = _fmt(cached_tokens)

    # Extract cost
    cost = data.get("total_cost_usd")
    if not isinstance(cost, bool) and isinstance(cost, (int, float)):
        usage["cost_usd"] = f"{cost:.4f}"

    # Extract model from modelUsage keys
    model_usage = data.get("modelUsage", {})
    if isinstance(model_usage, dict) and model_usage:
        usage["model"] = next(iter(model_usage))

    return result_text, usage


def _normalize_agent_output_pure(config: AgentConfig, raw_output: str) -> tuple[str, dict | list | None, bool, ExtractionRecord]:
    """Pure core of _normalize_agent_output: no logging or other side effects.

    Returns ``(text, parsed_json_or_None, parse_error, extraction_record)``.
    *parse_error* is True when the output looked like it should contain JSON
    (or the agent has a post-processor) but no JSON could be extracted.
    """
    record = _extract_json_value(raw_output)
    if record.text:
        try:
            parsed = json.loads(record.text)
        except (json.JSONDecodeError, TypeError):
            parsed = None
        return record.text.strip(), parsed, False, record

    raw_stripped = raw_output.strip()
    try:
        filters_noise = (config.runtime != "none"
                 and agent_adapters.get(config.runtime).filters_tui_noise)
    except agent_adapters.UnknownRuntimeError:
        filters_noise = False  # cosmetic filter; unknown agents fail elsewhere
    if filters_noise:
        stripped = _filtered_copilot_output(raw_output).strip()
    else:
        stripped = raw_stripped

    looks_like_json_payload = (
        raw_stripped.startswith(("{", "["))
        or re.match(r"```json\b", raw_stripped, flags=re.IGNORECASE) is not None
    )
    parse_error = bool(
        raw_stripped and (config.requires_stdout_json or config.handler or looks_like_json_payload)
    )
    return stripped, None, parse_error, record


def _normalize_agent_output(config: AgentConfig, raw_output: str) -> tuple[str, dict | list | None, ExtractionRecord]:
    """Normalize agent stdout, returning (text, parsed_json_or_None, record).

    Thin logging wrapper around :func:`_normalize_agent_output_pure`; emits a
    parse-error log event when JSON extraction fails on output that should
    have contained JSON.
    """
    stripped, parsed, parse_error, record = _normalize_agent_output_pure(config, raw_output)
    if parse_error:
        log_event(
            config.agent_log,
            phase="agent",
            level="error",
            message="failed to parse JSON from agent output; raw output follows",
            error_category="output_parse_error",
        )
        log_event(
            config.agent_log,
            phase="agent",
            level="error",
            message=f"[raw-output] {raw_output.strip()[:MAX_RAW_OUTPUT_LOG_LENGTH]}",
        )
    return stripped, parsed, record


# ---------------------------------------------------------------------------
# Safe-output validation (proposal §3.9)
# ---------------------------------------------------------------------------

def _enforce_safe_output(config: AgentConfig, raw_output: str,
                         parsed: dict | list | None,
                         record: ExtractionRecord) -> None:
    """Runner-enforced safe-output validations, applied before any
    post-processor sees agent output. The size cap always applies (with a
    generous default); schema, path-root, and provenance checks run only
    when the agent declares them. Raises :class:`AgentOutputInvalidError`.
    Error messages carry structure and digests, never candidate payload
    text (cross-review criterion 10)."""
    max_bytes = config.output_max_bytes or DEFAULT_OUTPUT_MAX_BYTES
    size = len(raw_output.encode("utf-8", errors="replace"))
    if size > max_bytes:
        raise AgentOutputInvalidError(
            f"agent output is {size} bytes, over the {max_bytes}-byte cap "
            f"(output-max-bytes)")

    if config.output_provenance == "strict":
        if (record.source != "stdout" or record.repaired
                or record.candidate_count != 1 or parsed is None):
            raise AgentOutputInvalidError(
                "output-provenance: strict requires the whole stdout to be a "
                "single unrepaired JSON document; got "
                f"source={record.source} repaired={record.repaired} "
                f"candidates={record.candidate_count}")

    if config.output_schema is not None:
        _validate_output_schema(config, parsed)

    if config.output_path_roots:
        _validate_output_paths(config, parsed)


def _load_output_schema(config: AgentConfig) -> dict:
    """Resolve the frontmatter ``output-schema`` value: an inline mapping
    is used as-is; a string is a JSON file reference resolved like
    handlers (repo-relative when it contains a separator, else a sidecar
    in the agent's own directory). A broken schema is an agent-definition
    problem (plain AgentsLiveError), not agent_output_invalid."""
    raw = config.output_schema
    if isinstance(raw, dict):
        return raw
    ref = str(raw)
    if "/" in ref or "\\" in ref:
        schema_path = repo_root() / ref
    else:
        schema_path = config.prompt_path.parent / ref
    try:
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AgentsLiveError(
            f"output-schema {ref} unreadable: {exc}") from exc
    if not isinstance(schema, dict):
        raise AgentsLiveError(f"output-schema {ref} must be a JSON object")
    return schema


def _validate_output_schema(config: AgentConfig, parsed: dict | list | None) -> None:
    # Lazy import: only the agent-executing path (run.py / cli.py envs)
    # needs jsonschema, and only for agents that declare a schema.
    import jsonschema

    schema = _load_output_schema(config)
    try:
        jsonschema.validators.validator_for(schema).check_schema(schema)
    except jsonschema.SchemaError as exc:
        raise AgentsLiveError(
            f"output-schema is not a valid JSON Schema: {exc.message}") from exc
    if parsed is None:
        raise AgentOutputInvalidError(
            "output-schema declared but no JSON could be extracted from agent output")
    try:
        jsonschema.validate(parsed, schema)
    except jsonschema.ValidationError as exc:
        # json_path + validator name only: instance values are agent
        # (attacker-influenceable) payload and stay out of logs.
        raise AgentOutputInvalidError(
            f"agent output does not conform to output-schema at "
            f"{exc.json_path}: {exc.validator} constraint failed") from exc


def _iter_path_fields(value: object, prefix: str = "$"):
    """Yield ``(json_path, value)`` for every field named ``path`` in the
    agent output - the annotated-path convention used by the write-files
    handler pattern."""
    if isinstance(value, dict):
        for key, item in value.items():
            here = f"{prefix}.{key}"
            if key == "path":
                yield here, item
            else:
                yield from _iter_path_fields(item, here)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _iter_path_fields(item, f"{prefix}[{index}]")


def _validate_output_paths(config: AgentConfig, parsed: dict | list | None) -> None:
    if parsed is None:
        raise AgentOutputInvalidError(
            "output-path-roots declared but no JSON could be extracted from agent output")
    base = repo_root().resolve()
    roots = []
    for declared in config.output_path_roots:
        root = (base / declared).resolve()
        if root != base and not root.is_relative_to(base):
            raise AgentsLiveError(
                f"output-path-roots entry escapes the repository: {declared}")
        roots.append(root)
    for field_path, value in _iter_path_fields(parsed):
        if not isinstance(value, str) or not value.strip():
            raise AgentOutputInvalidError(
                f"output path field {field_path} is not a nonempty string")
        if Path(value).is_absolute():
            raise AgentOutputInvalidError(
                f"output path field {field_path} is absolute; only "
                f"repo-relative paths under output-path-roots are allowed")
        resolved_path = (base / value).resolve()
        if not any(resolved_path == root or resolved_path.is_relative_to(root)
                   for root in roots):
            raise AgentOutputInvalidError(
                f"output path field {field_path} escapes the declared "
                f"output-path-roots")


def _persist_run_output(config: AgentConfig, stdout: str, stderr: str, *, label: str = "") -> str | None:
    """Persist full stdout/stderr (and transcript if present) from an agent run.

    Files are named: <agent>-<YYYYMMDDTHHMMSSZ>[-label].{stdout.txt,stderr.txt,transcript.md}
    The first '.' separates the identity (agent + timestamp + label) from the type.
    """
    runs_dir = logs_root() / "runs"
    try:
        runs_dir.mkdir(parents=True, exist_ok=True)
        ts_slug = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        suffix = f"-{label}" if label else ""
        base = f"{config.name}-{ts_slug}{suffix}"
        (runs_dir / f"{base}.stdout.txt").write_text(stdout or "(empty)", encoding="utf-8")
        (runs_dir / f"{base}.stderr.txt").write_text(stderr or "(empty)", encoding="utf-8")
        # Archive the session transcript before the next run overwrites it
        t_log = config.transcript_log
        if t_log.is_file() and t_log.stat().st_size > 0:
            shutil.copy2(t_log, runs_dir / f"{base}.transcript.md")
        return str(runs_dir / base)
    except OSError:
        return None


def _agent_result(
    output: str,
    stderr_text: str,
    usage: dict[str, str | None],
    transcript_path: str | None,
    structured_output: dict | list | None,
) -> AgentResult:
    """Assemble an AgentResult from output plus a _parse_usage_stats-style dict."""
    return AgentResult(
        output=output,
        stderr=stderr_text,
        model=usage.get("model"),
        tokens_in=usage.get("tokens_in"),
        tokens_out=usage.get("tokens_out"),
        tokens_cached=usage.get("tokens_cached"),
        premium_requests=usage.get("premium_requests"),
        credits=usage.get("credits"),
        cost_usd=usage.get("cost_usd"),
        transcript_path=transcript_path,
        structured_output=structured_output,
    )


def headless_agent(config: AgentConfig, prompt_text: str, *, stream: bool = False) -> AgentResult:
    resolved = _resolve_agent_config(config)
    if resolved.runtime == "none":
        raise AgentsLiveError("agent: none cannot be executed through headless_agent")

    adapter = _adapter(resolved.runtime)
    env = _build_agent_env(resolved)
    command = _build_agent_command(resolved, prompt_text)
    timeout = config.timeout or HEADLESS_TIMEOUT

    # Log the exact agent invocation. The -p prompt body is replaced with a
    # placeholder so the row stays grep-friendly; everything else (MCP flags,
    # model, --share, etc.) is preserved verbatim for diagnostics.
    display: list[str] = []
    skip_next = False
    for tok in command:
        if skip_next:
            display.append(f"<prompt {len(tok)} chars>")
            skip_next = False
        elif tok == "-p":
            display.append(tok)
            skip_next = True
        else:
            display.append(tok)
    log_event(
        config.agent_log,
        phase="agent",
        level="info",
        message="agent command",
        command=shlex.join(display),
        prompt_chars=len(prompt_text),
        timeout_s=timeout,
    )

    # Two independent retry budgets, each enforced where it is consumed:
    # timeouts raise once timeout_count exceeds HEADLESS_TIMEOUT_RETRIES,
    # empty outputs raise once empty_count reaches empty_output_attempts.
    empty_output_attempts = HEADLESS_EMPTY_OUTPUT_RETRIES + 1
    timeout_count = 0
    empty_count = 0
    while True:
        try:
            if adapter.use_pty:
                raw_output, stderr_text = _run_copilot_with_pty(command, env, stream=stream, timeout=timeout)
            elif stream:
                raw_output, stderr_text = _run_agent_streaming(command, env, config, timeout=timeout)
            else:
                completed = subprocess.run(
                    command,
                    cwd=repo_root(),
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    check=False,
                )
                stderr_text = completed.stderr
                if completed.returncode != 0:
                    if stderr_text:
                        log_event(config.agent_log, phase="agent", level="error",
                                  message=stderr_text[:MAX_LOG_FIELD_LENGTH],
                                  error_category="cli_crash",
                                  traceback=_extract_traceback(stderr_text))
                    if completed.stdout:
                        log_event(config.agent_log, phase="agent", level="error",
                                  message=f"[stdout] {completed.stdout.strip()[:MAX_LOG_FIELD_LENGTH]}")
                    detail = stderr_text.strip()[:MAX_LOG_FIELD_LENGTH] if stderr_text else ""
                    raise AgentsLiveError(
                        f"agent exited with status {completed.returncode}: {detail or ' '.join(command)}"
                    )
                raw_output = completed.stdout
        except (subprocess.TimeoutExpired, AgentTimeoutError) as exc:
            # Both timeout shapes land here: subprocess.run raises
            # TimeoutExpired (with partial output attached); the PTY and
            # streaming runners raise AgentTimeoutError. Non-timeout
            # AgentsLiveErrors propagate to the caller — classification
            # is by exception type, not message text, so a crash whose
            # stderr happens to contain "timed out" is no longer retried
            # on the timeout budget.
            timeout_count += 1
            partial_stdout = ""
            partial_stderr = ""
            if isinstance(exc, subprocess.TimeoutExpired):
                if exc.stdout:
                    partial_stdout = exc.stdout if isinstance(exc.stdout, str) else exc.stdout.decode("utf-8", errors="replace")
                if exc.stderr:
                    partial_stderr = exc.stderr if isinstance(exc.stderr, str) else exc.stderr.decode("utf-8", errors="replace")

            # Persist output for post-mortem analysis
            _persist_run_output(config, partial_stdout, partial_stderr, label="timeout")

            if timeout_count <= HEADLESS_TIMEOUT_RETRIES:
                # Log warning and retry
                log_event(config.agent_log, phase="agent", level="warning",
                          message=f"agent timed out after {timeout}s (timeout attempt {timeout_count}/{HEADLESS_TIMEOUT_RETRIES + 1}); retrying",
                          error_category="timeout", timeout_s=timeout, attempt=timeout_count)
                continue

            # Final timeout attempt - log and raise
            msg = (f"agent timed out after {timeout}s on retry (attempt {timeout_count}/{HEADLESS_TIMEOUT_RETRIES + 1}); giving up"
                   if HEADLESS_TIMEOUT_RETRIES > 0
                   else f"agent timed out after {timeout}s")
            log_event(config.agent_log, phase="agent", level="error",
                      message=msg,
                      error_category="timeout", timeout_s=timeout, attempt=timeout_count)
            raise AgentTimeoutError(msg) from exc
        except FileNotFoundError as exc:
            log_event(config.agent_log, phase="agent", level="error",
                      message=f"required command not found: {exc.filename}",
                      error_category="cli_crash")
            raise CliCrashError(f"required command not found: {exc.filename}") from exc

        if stderr_text:
            log_event(config.agent_log, phase="agent", level="info", message=stderr_text[:MAX_LOG_FIELD_LENGTH])

        # Claude with --output-format json: unwrap the JSON envelope and
        # extract usage stats from the structured response.
        claude_usage: dict[str, str | None] | None = None
        if adapter.behavior.json_envelope:
            raw_output, claude_usage = _parse_claude_json_output(raw_output)

        stripped, parsed, extraction = _normalize_agent_output(resolved, raw_output)
        # In pipeline mode the post-processor reads the agent's structured
        # output from the pipeline MCP store (get("/output/...")), not from
        # stdout -- stdout is just the agent's narration. Only enforce the
        # stdout-must-be-JSON contract for stdout-consuming post-processors.
        if config.requires_stdout_json and stripped and parsed is None:
            raise OutputParseError(
                "failed to parse JSON from agent output for "
                f"post-processor {config.post_processor_reference}; refusing to invoke handler"
            )

        # Safe-output validations (§3.9): size cap always, the declared
        # opt-ins when present - before persistence (the cap protects
        # disk) and before any post-processor sees the output.
        _enforce_safe_output(resolved, raw_output, parsed, extraction)

        if stripped:
            # Extraction provenance digest (TT-PY-006 mechanical half):
            # structure only, never candidate text.
            log_event(
                config.agent_log,
                phase="agent",
                level="info",
                message="extraction record",
                source=extraction.source,
                repaired=extraction.repaired,
                candidates=extraction.candidate_count,
                output_sha256=hashlib.sha256(
                    stripped.encode("utf-8", errors="replace")).hexdigest()[:16],
            )
            if claude_usage and claude_usage.get("tokens_in"):
                usage = claude_usage
            else:
                usage = _parse_usage_stats(stderr_text)
            # Determine transcript path if transcript capture was enabled
            t_path: str | None = None
            if resolved.transcript and adapter.behavior.shares_transcript:
                t_log = resolved.transcript_log
                if t_log.is_file() and t_log.stat().st_size > 0:
                    t_path = str(t_log)
            # Always persist full run output
            _persist_run_output(config, raw_output, stderr_text)
            return _agent_result(stripped, stderr_text, usage, t_path, parsed)

        # --- Empty output diagnostics ---
        # Determine why output is empty: truly no stdout, or content that
        # normalized to empty (e.g. only copilot noise lines)?
        empty_count += 1
        raw_was_nonempty = bool(raw_output.strip())
        t_log = resolved.transcript_log
        t_size = t_log.stat().st_size if t_log.is_file() else 0
        usage_info = _parse_usage_stats(stderr_text)

        # Classify the empty-output cause for structured logging
        if not raw_output.strip():
            empty_cause = "stdout_empty"
        else:
            empty_cause = "normalize_stripped"

        # NOTE (2026-07-11): the transcript-recovery path that lived here
        # (extract the first JSON value from the --share transcript and
        # treat it as agent output) was DELETED per the cross-review: the
        # transcript quotes untrusted tool results verbatim, so recovery
        # let externally authored JSON become the authoritative action
        # input. Log evidence showed the agency-copilot stdout-handoff bug
        # it worked around fired exactly once, 2026-05-07, never since.
        # The transcript itself is still captured for diagnostics; an
        # empty stdout now fails through the retry budget below.

        # Persist raw stdout/stderr for post-mortem analysis
        debug_file = _persist_run_output(config, raw_output, stderr_text, label="empty")

        # Construct a short stdout preview for the log (first meaningful chars)
        stdout_preview = ""
        if raw_was_nonempty:
            preview_text = raw_output.strip()[:200].replace("\n", "\\n")
            stdout_preview = preview_text

        # Emit structured diagnostic log entry
        if empty_count < empty_output_attempts:
            log_event(
                config.agent_log,
                phase="agent",
                level="warning",
                message=(
                    f"agent returned empty output on attempt {empty_count}/{empty_output_attempts}; "
                    f"retrying in {HEADLESS_EMPTY_OUTPUT_RETRY_DELAY_S:g}s"
                ),
                empty_cause=empty_cause,
                agent=resolved.runtime,
                raw_stdout_len=len(raw_output),
                stdout_preview=stdout_preview or None,
                transcript_size=t_size,
                transcript_path=str(t_log) if t_size > 0 else None,
                transcript_fallback_attempted=bool(resolved.transcript and t_size > 0),
                premium_requests=usage_info.get("premium_requests"),
                tokens_in=usage_info.get("tokens_in"),
                tokens_out=usage_info.get("tokens_out"),
                tokens_cached=usage_info.get("tokens_cached"),
                debug_file=debug_file,
            )
            time.sleep(HEADLESS_EMPTY_OUTPUT_RETRY_DELAY_S)
        else:
            log_event(
                config.agent_log,
                phase="agent",
                level="error",
                message=(
                    f"agent returned empty output on all {empty_output_attempts} attempts"
                ),
                error_category="empty_output",
                empty_cause=empty_cause,
                agent=resolved.runtime,
                raw_stdout_len=len(raw_output),
                stdout_preview=stdout_preview or None,
                transcript_size=t_size,
                transcript_path=str(t_log) if t_size > 0 else None,
                transcript_fallback_attempted=bool(resolved.transcript and t_size > 0),
                premium_requests=usage_info.get("premium_requests"),
                tokens_in=usage_info.get("tokens_in"),
                tokens_out=usage_info.get("tokens_out"),
                tokens_cached=usage_info.get("tokens_cached"),
                debug_file=debug_file,
            )
            raise AgentsLiveError(
                f"agent returned empty output after {empty_output_attempts} attempts; "
                f"debug files in {logs_root() / 'runs'}"
            )


def _run_agent_streaming(command: list[str], env: dict[str, str], config: AgentConfig, *, timeout: int = HEADLESS_TIMEOUT) -> tuple[str, str]:
    """Run an agent subprocess while tee-ing stdout to the terminal in real time.

    stdout and stderr are drained on their own threads so a chatty stderr
    can never fill its pipe buffer and deadlock the child, and the timeout
    bounds the whole run rather than just the wait after stdout EOF.
    """
    proc = subprocess.Popen(
        command,
        cwd=repo_root(),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    stdout_lines: list[str] = []
    stderr_chunks: list[str] = []

    def _drain_stdout() -> None:
        assert proc.stdout is not None  # noqa: S101
        for line in proc.stdout:
            sys.stdout.write(line)
            sys.stdout.flush()
            stdout_lines.append(line)

    def _drain_stderr() -> None:
        assert proc.stderr is not None  # noqa: S101
        stderr_chunks.append(proc.stderr.read())

    readers = [
        threading.Thread(target=_drain_stdout, daemon=True),
        threading.Thread(target=_drain_stderr, daemon=True),
    ]
    for reader in readers:
        reader.start()
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        proc.kill()
        proc.wait()
        raise AgentTimeoutError(f"agent timed out after {timeout}s") from exc
    for reader in readers:
        reader.join(timeout=5)

    stderr_text = "".join(stderr_chunks)
    if proc.returncode != 0:
        stdout_text = "".join(stdout_lines)
        if stderr_text:
            log_event(config.agent_log, phase="agent", level="error",
                      message=stderr_text[:MAX_LOG_FIELD_LENGTH],
                      error_category="cli_crash",
                      traceback=_extract_traceback(stderr_text))
        if stdout_text:
            log_event(config.agent_log, phase="agent", level="error",
                      message=f"[stdout] {stdout_text.strip()[:MAX_LOG_FIELD_LENGTH]}")
        detail = stderr_text.strip()[:MAX_LOG_FIELD_LENGTH] if stderr_text else ""
        raise AgentsLiveError(
            f"agent exited with status {proc.returncode}: {detail or ' '.join(command)}"
        )
    return "".join(stdout_lines), stderr_text


def _run_copilot_with_pty(command: list[str], env: dict[str, str], *, stream: bool = False, timeout: int = HEADLESS_TIMEOUT) -> tuple[str, str]:
    env_items = ["env", "-i", *(f"{key}={value}" for key, value in env.items())]
    command_string = shlex.join(env_items + command)
    with tempfile.NamedTemporaryFile(prefix="agents-live-copilot-", delete=False) as handle:
        transcript_path = Path(handle.name)

    try:
        if stream:
            # Use 'script' with stdout flowing to the terminal in real time
            completed = subprocess.run(
                ["script", "-qc", command_string, str(transcript_path)],
                cwd=repo_root(),
                stderr=subprocess.PIPE,
                text=True,
                timeout=timeout,
                check=False,
            )
        else:
            completed = subprocess.run(
                ["script", "-qc", command_string, str(transcript_path)],
                cwd=repo_root(),
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        if completed.returncode != 0:
            transcript = ""
            try:
                transcript = transcript_path.read_text(encoding="utf-8", errors="ignore").strip()[:MAX_LOG_FIELD_LENGTH]
            except OSError:
                pass
            detail = completed.stderr.strip()[:MAX_LOG_FIELD_LENGTH] or transcript or " ".join(command)
            raise AgentsLiveError(f"agent exited with status {completed.returncode}: {detail}")
        cleaned = ANSI_RE.sub("", transcript_path.read_text(encoding="utf-8", errors="ignore")).replace("\r", "")
        return cleaned, completed.stderr
    finally:
        transcript_path.unlink(missing_ok=True)


# Base packages injected into every Python handler run.
# Add here when a package is used by multiple handlers.
BASE_HANDLER_PACKAGES = ["mcp[cli]"]


def _find_uv() -> str | None:
    """Locate the ``uv`` binary, searching common install paths if needed.

    ``shutil.which`` relies on the current process PATH which is minimal under
    cron or file-watcher contexts.  Fall back to well-known locations so that
    Python handlers can always be executed via ``uv run --with …``.
    """
    found = shutil.which("uv")
    if found:
        return found
    # Also search the directories included in clean_path() / common installs
    candidates = [
        Path.home() / ".local" / "bin" / "uv",
        Path.home() / ".cargo" / "bin" / "uv",
        Path("/usr/local/bin/uv"),
    ]
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def _build_handler_command(handler_path: Path) -> list[str]:
    """Build the full command list to execute a handler by file extension."""
    suffix = handler_path.suffix.lower()
    if suffix == ".py":
        uv = _find_uv()
        if uv:
            cmd = [uv, "run"]
            for pkg in BASE_HANDLER_PACKAGES:
                cmd += ["--with", pkg]
            cmd.append(str(handler_path))
            return cmd
        return [sys.executable, str(handler_path)]
    if suffix in (".js", ".ts"):
        return ["node", str(handler_path)]
    return ["bash", str(handler_path)]


def _run_handler(config: AgentConfig, input_text: str | None, *, changed_files: list[str] | None = None) -> str:
    if not config.handler_path:
        raise AgentsLiveError("handler is required")
    if not config.handler_path.is_file():
        raise AgentsLiveError(f"handler not found: {config.handler_reference}")

    env = os.environ.copy()
    env.update(_resolve_agent_config(config).env)
    env["AGENTS_LIVE_AGENT_NAME"] = config.name
    if changed_files:
        env["AGENTS_LIVE_CHANGED_FILES"] = json.dumps(changed_files)
    run_kwargs: dict[str, Any] = {
        "cwd": repo_root(),
        "capture_output": True,
        "text": True,
        "env": env,
        "check": False,
    }
    # Match the old shell behavior: handler-only runs should see closed stdin.
    if input_text is None:
        run_kwargs["stdin"] = subprocess.DEVNULL
    else:
        run_kwargs["input"] = input_text
    cmd = _build_handler_command(config.handler_path)
    completed = subprocess.run(cmd, **run_kwargs)
    if completed.returncode != 0:
        stderr_text = completed.stderr.strip() if completed.stderr else ""
        if stderr_text:
            log_event(config.agent_log, phase="handler", level="error", message=stderr_text,
                      error_category="handler_crash",
                      traceback=_extract_traceback(stderr_text))
        detail = stderr_text[:MAX_LOG_FIELD_LENGTH] if stderr_text else config.handler_reference
        raise HandlerCrashError(
            f"handler failed with status {completed.returncode}: {detail}"
        )
    if completed.stderr:
        log_event(config.agent_log, phase="handler", level="info", message=completed.stderr.rstrip("\n"))
    return completed.stdout.rstrip("\n")


def run_post_processor(config: AgentConfig, input_text: str | None, *, changed_files: list[str] | None = None) -> str:
    """Alias for _run_handler — runs the post-processor (or handler) script."""
    return _run_handler(config, input_text, changed_files=changed_files)


def run_pre_processor(config: AgentConfig, *, changed_files: list[str] | None = None) -> PreProcessorResult:
    """Run the pre-processor script and return its output with skip detection."""
    if not config.pre_processor_path:
        raise AgentsLiveError("pre-processor is required")
    if not config.pre_processor_path.is_file():
        raise AgentsLiveError(f"pre-processor not found: {config.pre_processor_reference}")

    env = os.environ.copy()
    env.update(_resolve_agent_config(config).env)
    env["AGENTS_LIVE_AGENT_NAME"] = config.name
    if changed_files:
        env["AGENTS_LIVE_CHANGED_FILES"] = json.dumps(changed_files)

    cmd = _build_handler_command(config.pre_processor_path)
    completed = subprocess.run(
        cmd, cwd=repo_root(), capture_output=True, text=True,
        env=env, check=False, stdin=subprocess.DEVNULL,
    )
    stderr_text = completed.stderr.strip() if completed.stderr else ""
    if completed.returncode != 0:
        if stderr_text:
            log_event(config.agent_log, phase="pre-processor", level="error", message=stderr_text,
                      error_category="pre_processor_crash",
                      traceback=_extract_traceback(stderr_text))
        detail = stderr_text[:MAX_LOG_FIELD_LENGTH] if stderr_text else config.pre_processor_reference
        raise PreProcessorCrashError(
            f"pre-processor failed with status {completed.returncode}: {detail}"
        )
    output = completed.stdout.rstrip("\n")
    skip = False
    try:
        parsed = json.loads(output)
        if isinstance(parsed, dict) and parsed.get("skip"):
            skip = True
    except (json.JSONDecodeError, ValueError):
        pass
    if stderr_text:
        log_event(config.agent_log, phase="pre-processor", level="info", message=stderr_text)
    return PreProcessorResult(output=output, skip=skip, stderr=stderr_text)


def current_crontab_lines() -> list[str] | None:
    """Return crontab lines, or None if crontab is unavailable (e.g. sandbox).

    A user with no crontab yet (``crontab -l`` exits 1 with ``no crontab
    for <user>`` on stderr) is an empty crontab, not an unavailable one,
    and returns ``[]``.
    """
    try:
        completed = subprocess.run(
            ["crontab", "-l"],
            cwd=repo_root(),
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError as exc:
        raise CliCrashError("crontab command not found") from exc
    if completed.returncode != 0:
        if "no crontab for" in (completed.stderr or ""):
            return []
        return None
    return [line for line in completed.stdout.splitlines() if line.strip()]


def install_crontab(lines: list[str]) -> None:
    payload = "\n".join(lines) + "\n" if lines else ""
    try:
        subprocess.run(
            ["crontab", "-"],
            cwd=repo_root(),
            input=payload,
            text=True,
            capture_output=True,
            check=True,
        )
    except FileNotFoundError as exc:
        raise CliCrashError("crontab command not found") from exc
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.strip() if exc.stderr else "failed to update crontab"
        raise AgentsLiveError(stderr) from exc


def cron_line_matches(line: str, name: str) -> bool:
    """Return True if a crontab line carries the exact ``--name <name>`` pair.

    Exact-token matching prevents an agent name that is a substring of another
    (e.g. ``todo`` vs ``todo-push``) from matching the wrong entry.
    """
    try:
        tokens = shlex.split(line)
    except ValueError:
        tokens = line.split()
    return any(
        first == "--name" and second == name
        for first, second in zip(tokens, tokens[1:])
    )


def remove_cron_entries(name: str) -> bool:
    lines = current_crontab_lines()
    if lines is None:
        raise AgentsLiveError("crontab is not accessible")
    filtered = [line for line in lines if not cron_line_matches(line, name)]
    if len(filtered) == len(lines):
        return False
    install_crontab(filtered)
    return True


def cron_is_active(name: str) -> bool | None:
    """Return True if active, False if inactive, None if crontab unavailable."""
    lines = current_crontab_lines()
    if lines is None:
        return None
    return any(cron_line_matches(line, name) for line in lines)


def _cron_line_agent_name(line: str) -> str | None:
    """Return the agent name carried by an Agents Live cron line, else None.

    The inverse of :func:`cron_line_matches`: extracts the ``--name`` token
    without knowing the name in advance. Only lines that invoke the run
    script qualify, so unrelated crontab entries are ignored.
    """
    if "run.py" not in line:
        return None
    try:
        tokens = shlex.split(line)
    except ValueError:
        tokens = line.split()
    for first, second in zip(tokens, tokens[1:]):
        if first == "--name":
            return second
    return None


def _list_active_cron_agent_names() -> list[str]:
    """Every Agents Live agent currently installed in this host's crontab.

    Runtime-is-truth enumeration (the reverse of :func:`cron_line_matches`):
    used to find orphans whose agent file was deleted. Returns ``[]`` when the
    crontab is empty or unavailable.
    """
    lines = current_crontab_lines() or []
    return [name for line in lines if (name := _cron_line_agent_name(line))]


def _pid_is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _find_all_watcher_pids(name: str) -> list[int]:
    proc_dir = Path("/proc")
    if proc_dir.is_dir():
        return _find_watcher_pids_proc(name, proc_dir)
    return _find_watcher_pids_ps(name)


def _is_watcher_cmdline(args: list[str], name: str) -> bool:
    """Return True if an argv list is the watch loop for exactly this agent.

    Requires the exact adjacent pair ``["--watch-loop", name]`` so an agent
    name that is a substring of another (``todo`` vs ``todo-push``) never
    matches the wrong watcher.
    """
    if not any("activate.py" in arg for arg in args):
        return False
    return any(
        first == "--watch-loop" and second == name
        for first, second in zip(args, args[1:])
    )


def _find_watcher_pids_proc(name: str, proc_dir: Path) -> list[int]:
    """Read /proc to find watcher processes (avoids subprocess)."""
    pids: list[int] = []
    for entry in proc_dir.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            raw = (entry / "cmdline").read_bytes()
        except (OSError, PermissionError):
            continue
        args = raw.decode("utf-8", errors="replace").split("\x00")
        if not _is_watcher_cmdline(args, name):
            continue
        pids.append(int(entry.name))
    return pids


def _find_watcher_pids_ps(name: str) -> list[int]:
    """Fallback for non-Linux systems using ps."""
    try:
        completed = subprocess.run(
            ["ps", "-eo", "pid=,args="],
            capture_output=True,
            text=True,
            check=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError):
        return []

    pids: list[int] = []
    for line in completed.stdout.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        pid_text, _, args_text = stripped.partition(" ")
        if not _is_watcher_cmdline(args_text.split(), name):
            continue
        try:
            pids.append(int(pid_text))
        except ValueError:
            continue
    return pids


def find_watcher_pid(name: str) -> int | None:
    pids = _find_all_watcher_pids(name)
    return pids[0] if pids else None


def _watcher_cmdline_agent_name(args: list[str]) -> str | None:
    """Return the watched agent name from a watcher argv list, else None.

    The inverse of :func:`_is_watcher_cmdline`: a watch loop runs
    ``activate.py --watch-loop <name>``.
    """
    if not any("activate.py" in arg for arg in args):
        return None
    for first, second in zip(args, args[1:]):
        if first == "--watch-loop":
            return second
    return None


def _list_active_watcher_agent_names() -> list[str]:
    """Every agents-live watcher process running on this host.

    Runtime-is-truth enumeration (the reverse of the per-name watcher
    lookup): scans ``/proc`` (or ``ps`` on non-Linux) for
    ``activate.py --watch-loop <name>`` processes.
    """
    names: list[str] = []
    proc_dir = Path("/proc")
    if proc_dir.is_dir():
        for entry in proc_dir.iterdir():
            if not entry.name.isdigit():
                continue
            try:
                raw = (entry / "cmdline").read_bytes()
            except (OSError, PermissionError):
                continue
            args = raw.decode("utf-8", errors="replace").split("\x00")
            name = _watcher_cmdline_agent_name(args)
            if name:
                names.append(name)
        return names
    try:
        completed = subprocess.run(
            ["ps", "-eo", "args="], capture_output=True, text=True, check=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError):
        return names
    for line in completed.stdout.splitlines():
        name = _watcher_cmdline_agent_name(line.split())
        if name:
            names.append(name)
    return names


def list_active_agent_names() -> set[str]:
    """Union of all agent names live on this host (cron entries + watchers).

    The host's runtime (crontab + ``inotifywait`` processes) is the source of
    truth for what is running. This reverse lookup is what makes orphan
    detection possible: compare it against :func:`list_agents` (the agents that
    still have a definition file); anything running without a definition is an
    orphan left behind by a deleted or renamed agent.
    """
    return set(_list_active_cron_agent_names()) | set(_list_active_watcher_agent_names())


def _wait_for_pids_to_stop(
    pids: list[int],
    stopped_pids: set[int],
    timeout_s: float,
) -> int | None:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        for pid in pids:
            if pid in stopped_pids:
                continue
            if not _pid_is_running(pid):
                stopped_pids.add(pid)
        if len(stopped_pids) == len(pids):
            return next(iter(stopped_pids))
        time.sleep(0.1)
    return None


def stop_watcher(name: str) -> int | None:
    """Stop all live watcher processes for an agent and return the first stopped PID found."""
    pids = [pid for pid in _find_all_watcher_pids(name) if _pid_is_running(pid)]
    if not pids:
        return None

    stopped_pids: set[int] = set()
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            continue

    stopped_pid = _wait_for_pids_to_stop(pids, stopped_pids, timeout_s=5)
    if stopped_pid is not None:
        return stopped_pid

    for pid in pids:
        if pid in stopped_pids:
            continue
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            # A missing PID is already fully stopped, so treat it as complete here.
            stopped_pids.add(pid)

    stopped_pid = _wait_for_pids_to_stop(pids, stopped_pids, timeout_s=2)
    if stopped_pid is not None:
        return stopped_pid

    return next(iter(stopped_pids)) if stopped_pids else None



def _trigger_states(config: AgentConfig) -> dict[str, str]:
    """Return per-trigger state for multi-trigger agents."""
    states: dict[str, str] = {}
    if config.schedule:
        active = cron_is_active(config.name)
        if active is None:
            states["cron"] = "unknown"
        elif active:
            states["cron"] = "active"
        else:
            states["cron"] = "stopped"
    if config.watch_path:
        pid = find_watcher_pid(config.name)
        if pid is not None:
            states["watcher"] = f"active (pid {pid})"
        else:
            states["watcher"] = "stopped"
    return states


def _agent_state(config: AgentConfig) -> str:
    try:
        trigger_type = config.trigger_type
    except AgentsLiveError:
        return "stopped"
    if trigger_type == "multi":
        states = _trigger_states(config)
        if all(s == "unknown" for s in states.values()):
            return "unknown"
        if all(s.startswith("active") or s == "unknown" for s in states.values()):
            return "active"
        if any(s.startswith("active") for s in states.values()):
            return "partial"
        return "stopped"
    if trigger_type == "cron":
        active = cron_is_active(config.name)
        if active is None:
            return "unknown"
        if active:
            return "active"
        return "stopped"
    pid = find_watcher_pid(config.name)
    if pid is not None:
        return f"active (pid {pid})"
    return "stopped"


def agent_details(config: AgentConfig) -> dict[str, Any]:
    details: dict[str, Any] = {
        "name": config.name,
        "type": config.trigger_type,
        "runtime": config.runtime,
        "mode": config.mode,
        "promptPath": config.prompt_reference,
        "state": _agent_state(config),
    }
    if config.description:
        details["description"] = config.description
    if config.model:
        details["model"] = config.model
    if config.pre_processor:
        details["pre-processor"] = config.pre_processor_reference
    if config.handler_reference:
        details["post-processor"] = config.handler_reference
    if config.schedule:
        details["schedule"] = config.schedule
    if config.watch_path:
        details["watchPath"] = config.watch_path
    if config.mcps:
        details["mcps"] = config.mcps
    if config.transcript:
        details["transcript"] = True
    try:
        details["triggerStates"] = _trigger_states(config)
    except AgentsLiveError:
        pass
    # Ownership: best-effort import to avoid circulars during early init.
    try:
        from . import ownership as _ownership  # type: ignore
        # Pass a large rate_limit so status display never triggers a
        # git pull on its own; the dispatcher's load_owners() refreshes.
        owners = _ownership.load_owners(rate_limit_secs=10**9)
        host = _ownership.current_host()
        owner = owners.get(config.name)
        details["owner"] = owner
        details["host"] = host
        details["isOwner"] = (
            owner is None
            or owner == _ownership.WILDCARD
            or owner.lower() == host
        )
    except Exception:  # noqa: BLE001 - status must never fail on this
        pass
    return details
