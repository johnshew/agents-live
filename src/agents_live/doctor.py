#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["PyYAML", "jsonschema"]
# ///
# The dependencies are headless.py's: the crontab-consistency and
# watcher-coverage checks import it, and without its deps in this script
# env the imports fail silently and the checks self-skip.
"""Check environment readiness for Agents Live agents (the `doctor` command).

Mirrors the checks documented in docs/commands.md (`doctor` section).
Each check is classified `required` or optional. Exit 0 when every
required check passes, 1 otherwise. `--json` emits a machine-readable
summary for callers such as the dashboard Health check button, which
runs this as a gate before `activate.py --all`.

Required checks gate activation/runtime (uv, Python 3.12, the two
dispatch mechanisms, the agent directories). The mechanisms are named by
what they do rather than by the tool that does them, because the tool
differs by host: a crontab and inotifywait on POSIX, Task Scheduler and
Windows directory-change notification on Windows. Agent CLIs
(node/npm/claude/copilot/agency) and jq are reported as optional: their
absence does not break activation - a cron/watcher still installs - but
an agent will fail at run time if its runtime CLI is missing, and shell
handlers that parse JSON with jq need jq installed.
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from . import paths
from . import preflight
from . import update_check
from . import repos
from . import hostruntime
from . import watchsource
from . import wintasks
from .paths import resolve_root

try:
    REPO = resolve_root()
except ValueError:
    REPO = None


def _project_checks_enabled() -> bool:
    return REPO is not None and paths.config_source(REPO) is not None


def _has(cmd: str) -> bool:
    return shutil.which(cmd) is not None


def _fix(posix: str, windows: str) -> str:
    """The remedy for this host. An apt line helps nobody on Windows."""
    return windows if hostruntime.id() == hostruntime.WINDOWS else posix


# What each host dispatches with, named by the tool the developer would
# go looking for. Doctor reports the mechanism, not the Linux tool: the
# capability is the same question on both hosts and only the answer to
# "which program" differs. Keyed by the mechanism each module reports,
# so the row is selected by the same answer the runtime acts on.
_MECHANISMS = {
    "schedule": {
        hostruntime.CRONTAB: ("crontab", "sudo apt install cron", ""),
        hostruntime.TASK_SCHEDULER: (
            "Task Scheduler", "no install needed; the task store "
            "ships with Windows, so a failure here is a permission "
            "or policy problem",
            "agents-live registers one task per agent under "
            f"{wintasks.TASK_FOLDER}"),
    },
    "watch": {
        watchsource.INOTIFY: (
            "inotifywait", "sudo apt install inotify-tools",
            "required for file-watcher agents "
            "(note-index, todo-index, ...)"),
        watchsource.DIRECTORY_CHANGES: (
            "directory change notification",
            "no install needed; the notifications come from the "
            "Windows kernel",
            "required for file-watcher agents "
            "(note-index, todo-index, ...)"),
    },
}


def _mechanism_of(capability: str) -> str:
    """What this host serves *capability* with, asked of its owner."""
    if capability == "schedule":
        return hostruntime.native_scheduler()
    return watchsource.mechanism()


def _mechanism_check(capability: str) -> tuple[str, bool, bool, str, str]:
    """A required check for one dispatch mechanism on this host."""
    name, fix, note = _MECHANISMS[capability][_mechanism_of(capability)]
    failure = preflight.check("doctor", {capability})
    if failure is not None and failure.detail:
        note = f"{note}; {failure.detail}" if note else failure.detail
    return name, failure is None, True, fix, note


def _owns(owner: str) -> bool:
    """Whether this runtime owns ``owner``, best-effort.

    Doctor reports; it never blocks on ownership. A runtime that cannot
    read its own identity answers "yes" here so the CLI checks stay
    conservative - warning about a tool that may not be needed is a much
    smaller failure than silently skipping one that is.
    """
    from . import ownership  # noqa: PLC0415
    try:
        return ownership.owns(owner)
    except ownership.OwnershipUnavailableError:
        return True


def _agent_cli_needed_by_host() -> dict[str, dict[str, list[str]]]:
    """Map each agent-CLI keyword ("claude", "copilot", "agency") to the
    names of agents that require it, split into "owned" (this runtime or
    "*", per agent-owners.json falling back to frontmatter `owner:`) and
    "unclaimed" (no registry entry and no frontmatter owner - any host may
    end up running these, so they still warrant a conservative warning, but
    they are not owned by this host).

    An owner value this runtime cannot match is another runtime's, so it
    is scoped out exactly like one naming a different machine.

    Some hosts (e.g. a machine without Microsoft-account/network access)
    never run `agency`-based agents because ownership pins those agents to a
    different host. Reporting a blanket WARN for missing `agency`/`claude`/
    `copilot` CLIs on such hosts is misleading, so callers use this to scope
    the warning to what the host actually needs.
    """
    result: dict[str, dict[str, list[str]]] = {
        kw: {"owned": [], "unclaimed": []} for kw in ("claude", "copilot", "agency")
    }
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from .headless import list_agents, load_agent_config  # noqa: PLC0415
    except Exception:
        return result  # best-effort only; unavailable == no host-scoping info

    owners_path = REPO / "Agents" / "data" / "agent-owners.json"
    owners: dict[str, str] = {}
    if owners_path.is_file():
        try:
            owners = json.loads(owners_path.read_text()).get("owners", {})
        except (OSError, json.JSONDecodeError):
            owners = {}

    try:
        names = list_agents()
    except Exception:
        return result

    for name in names:
        owner = owners.get(name)
        try:
            cfg = load_agent_config(name)
        except Exception:
            continue
        if owner is None:
            owner = cfg.owner  # unclaimed: fall back to frontmatter seed (may be None)
        if owner is not None and not _owns(owner):
            continue  # owned by another runtime; not this host's concern
        bucket = "unclaimed" if owner is None else "owned"
        runtime = (cfg.runtime or "").strip().lower()
        for keyword in ("claude", "copilot", "agency"):
            if keyword in runtime.split():
                result[keyword][bucket].append(name)
    return result


def _node_is_wsl_native() -> bool:
    """True when a node this runtime owns is available.

    The Windows node reached through interop writes MSAL tokens to the
    Windows credential store, so `npx ... --login` for the MS365/Graph
    MCPs never populates the Linux cache the MCP launcher reads. Either
    program answering is enough: they ship together, and a project
    launcher may reach for either.
    """
    return any(hostruntime.tool_is_native(command)
               for command in ("npx", "node"))


def _python_312_resolvable() -> bool:
    """True when `uv run` resolves an interpreter >= 3.12 (scripts need 3.12).

    The interpreter is asked for by the name the host uses, which is not
    the same on both: see :func:`hostruntime.interpreter_name`.
    """
    try:
        result = subprocess.run(
            ["uv", "run", hostruntime.interpreter_name(), "-c",
             "import sys; print(1 if sys.version_info >= (3, 12) else 0)"],
            cwd=REPO, capture_output=True, timeout=120,
            **hostruntime.CHILD_TEXT,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0 and result.stdout.strip() == "1"


def _windows_heartbeat_config() -> tuple[bool, str] | None:
    """Validate the Windows Task Scheduler heartbeat when interop is available."""
    from . import heartbeat  # noqa: PLC0415
    try:
        task, legacy = heartbeat.task_configuration()
        distro = heartbeat.current_distro()
    except (heartbeat.InteropUnavailable, heartbeat.DistroUnknown):
        # Neither says anything about how the task is configured: one
        # host cannot ask the other side, the other cannot name itself.
        return None
    except RuntimeError as exc:
        return False, str(exc)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if task is None:
        note = f"task {heartbeat.task_name(distro)!r} not found"
        if legacy:
            note += f"; legacy {heartbeat.LEGACY_TASK!r} requires migration"
        return False, note

    execute = str(task.get("Execute") or "").replace("\\", "/").lower()
    arguments = str(task.get("Arguments") or "").replace("\\", "/").lower()
    try:
        expected_execute, expected_arguments = heartbeat.task_action(distro)
    except RuntimeError as exc:
        # A registered task that cannot be described is not a task that
        # can be judged, and the reason is the finding.
        return False, str(exc)
    expected_execute = expected_execute.replace("\\", "/").lower()
    expected_arguments = expected_arguments.replace("\\", "/").lower()
    problems: list[str] = []
    if task.get("Enabled") is not True:
        problems.append("task disabled")
    if execute != expected_execute:
        superseded = heartbeat.SUPERSEDED_ACTIONS.get(
            execute.rsplit("/", 1)[-1])
        if superseded:
            problems.append(
                f"task launches the heartbeat {superseded}; re-run "
                "`agents-live heartbeat install`")
        else:
            problems.append(
                f"unexpected executable: {task.get('Execute') or '(none)'}")
    if arguments != expected_arguments:
        problems.append("action does not use the windowless stable "
                        "agents-live CLI shim invocation")
        if any(token in arguments for token in heartbeat.LEGACY_ACTION_TOKENS):
            problems.append("action pins a legacy package, Python, or project path")
    if str(task.get("Interval") or "").upper() != "PT5M":
        interval = task.get("Interval") or "(none)"
        problems.append(f"repetition is {interval}, expected PT5M")
    if legacy:
        problems.append(f"legacy {heartbeat.LEGACY_TASK!r} task requires migration")
    note = "; ".join(problems) if problems else (
        f"enabled; distro {distro}; windowless stable CLI shim; "
        "repeats every 5 min")
    return not problems, note


def _config_state() -> tuple[bool, str]:
    """(ok, note) for the project-config installation check (§3.4.1):
    the config home must be present (it is also the root marker) and
    parseable, and every declared agent directory must exist."""
    source = paths.config_source(REPO)
    if source is None:
        return False, "no .agents-live.toml or [tool.agents-live] table"
    try:
        config = paths.load_config(REPO)
    except ValueError as exc:
        return False, str(exc)
    try:
        directories = paths.validated_agent_directories(
            REPO, config.get("agent_directories", []))
    except ValueError as exc:
        return False, str(exc)
    missing = [str(d.relative_to(REPO)) for d in directories if not d.is_dir()]
    if missing:
        return False, f"declared agent directories missing: {', '.join(missing)}"
    return True, f"config: {source.name}"


def _trigger_inconsistencies() -> tuple[list[str], list[str]] | None:
    """(orphaned agent names, stale entries) from what this host has
    registered, or None when the store is unreadable (check skipped).

    Whichever store the host uses, the two questions are the same:
    which registered triggers name an agent that no longer has a
    definition file, and which name something on disk that is no longer
    there - a script that was deleted, or a project root that moved.
    Repo-scoped matching can never tear down an entry pinned to a root
    that no longer exists, so doctor is the only place those surface.
    """
    if hostruntime.native_scheduler() == hostruntime.TASK_SCHEDULER:
        return _task_inconsistencies()
    return _crontab_inconsistencies()


def _task_session_requirement() -> tuple[bool, str] | None:
    """Whether every registered task still needs a signed-in session.

    A task store runs an agent in the owner's session and only while
    that owner is signed in. That is a real limit on when agents run, so
    the check states it whether or not anything is wrong, and fails when
    a task has been given a different logon type by hand - which changes
    what the task may do and what credentials it does it with.

    Only the logon type is compared. The store rewrites the user it was
    given as a SID, so the name this tool wrote and the name it reads
    back are never the same string even when they are the same person.
    """
    if hostruntime.native_scheduler() != hostruntime.TASK_SCHEDULER:
        return None
    from . import wintasks  # noqa: PLC0415

    try:
        tasks = wintasks.registered_tasks()
    except Exception:
        return None
    wrong = sorted(task["name"] for task in tasks
                   if task.get("principal") is not None
                   and task["principal"][1] != wintasks.LOGON_TYPE)
    if wrong:
        return False, ("registered with another logon type: "
                       + ", ".join(wrong))
    return True, (f"{len(tasks)} task(s) run with an interactive token; "
                  "nothing scheduled runs while nobody is signed in")


def _task_inconsistencies() -> tuple[list[str], list[str]] | None:
    """The registered-task form of :func:`_trigger_inconsistencies`."""
    if REPO is None:
        return None
    from . import wintasks  # noqa: PLC0415

    try:
        tasks = wintasks.registered_tasks()
    except Exception:
        return None
    referenced: set[str] = set()
    missing_roots: set[str] = set()
    for task in tasks:
        working_dir = task["working_dir"]
        if not working_dir:
            continue
        name = wintasks.agent_of_task_name(task["name"], REPO)
        if name is not None:
            referenced.add(name)
            continue
        # Another project's task is that project's business - unless the
        # project it was registered for is gone, in which case no run of
        # this tool anywhere can ever match it by name again.
        if not Path(working_dir).is_dir():
            missing_roots.add(working_dir)
    orphans: list[str] = []
    if referenced:
        try:
            from .headless import list_agents  # noqa: PLC0415
            existing = set(list_agents())
        except Exception:
            existing = None  # discovery unavailable: skip orphan half
        if existing is not None:
            orphans = sorted(
                name for name in referenced
                if name not in existing and not name.startswith("_"))
    stale = [f"{root} (project root moved or deleted)"
             for root in sorted(missing_roots)]
    return orphans, stale


def _crontab_inconsistencies() -> tuple[list[str], list[str]] | None:
    """(orphaned agent names, stale script paths) from the installed
    crontab, or None when the crontab is unreadable (check skipped).

    Orphans are ``--name``/``ensure-watcher`` references whose agent
    file no longer exists; stale entries are ``.py`` script references
    that no longer exist on disk (§4 migration concern: pre-cutover
    ``uv run .../scripts/...`` lines) and agents-live lines pinned to a
    project root that no longer exists (a moved or deleted checkout -
    repo-scoped matching can never tear those down, so doctor is the
    only place they surface)."""
    try:
        completed = subprocess.run(
            ["crontab", "-l"], capture_output=True, timeout=10,
            **hostruntime.CHILD_TEXT)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        if "no crontab for" in (completed.stderr or ""):
            return [], []  # no crontab yet: consistent by definition
        return None  # unreadable is not the same as empty: skip, don't vouch
    try:
        from . import crontasks  # noqa: PLC0415
        from .headless import repo_root  # noqa: PLC0415
        project_root = repo_root()
    except Exception:
        return None
    referenced: set[str] = set()
    script_paths: set[str] = set()
    missing_roots: set[str] = set()
    for line in completed.stdout.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        # The crontab is host-global; only lines referencing THIS repo
        # are this project's concern (another project's agents are not
        # orphans here) - except lines whose pinned root no longer
        # exists at all, which no project can ever match or remove.
        if not crontasks.belongs_to_root(stripped, project_root):
            try:
                foreign_tokens = shlex.split(stripped)
            except ValueError:
                foreign_tokens = stripped.split()
            shaped = any(t in ("--name", "ensure-watcher",
                              "--ensure-watcher")
                         for t in foreign_tokens)
            root = next(
                (second for first, second in
                 zip(foreign_tokens, foreign_tokens[1:])
                 if first in ("cd", "--repo")),
                None)
            if shaped and root and not Path(root).is_dir():
                missing_roots.add(root)
            continue
        tokens = stripped.split()
        for index, token in enumerate(tokens):
            if token in ("--name", "ensure-watcher",
                         "--ensure-watcher") and index + 1 < len(tokens):
                referenced.add(tokens[index + 1])
            if token.endswith(".py") and "/" in token:
                script_paths.add(token)
    orphans: list[str] = []
    if referenced:
        try:
            from .headless import list_agents  # noqa: PLC0415
            existing = set(list_agents())
        except Exception:
            existing = None  # discovery unavailable: skip orphan half
        if existing is not None:
            orphans = sorted(
                name for name in referenced
                if name not in existing and not name.startswith("_"))
    stale = sorted(p for p in script_paths if not Path(p).is_file())
    stale.extend(f"{root} (project root moved or deleted)"
                 for root in sorted(missing_roots))
    return orphans, stale


def _native_agents() -> dict[str, list[tuple[str, dict]]] | None:
    """Agents in the native agent directories, keyed by dir
    (raw frontmatter dicts - policy lints must not depend on strict
    parsing succeeding). None = discovery helpers unavailable (PyYAML
    missing outside the CLI env)."""
    try:
        from . import headless  # noqa: PLC0415
    except Exception:
        return None
    found: dict[str, list[tuple[str, dict]]] = {}
    for rel in (".claude/agents", ".github/agents"):
        d = REPO / rel
        if not d.is_dir():
            continue
        entries: list[tuple[str, dict]] = []
        for path in sorted(d.glob("*.md")):
            try:
                if not headless._has_triggers(path):
                    continue
                data = headless._extract_frontmatter(
                    path.read_text(encoding="utf-8"), path)
            except Exception:
                continue  # parse problems surface via status/run, not doctor
            entries.append((headless._agent_name(path), data))
        found[rel] = entries
    return found


def _native_agent_lints(native: dict[str, list[tuple[str, dict]]]) -> tuple[list[str], list[str]]:
    """(delegation-convention violations, cloud-exposure violations) -
    the C2 policy checks from the convergence proposal (risks 8 and 2).

    Claude Code has no `disable-model-invocation` equivalent, so
    Agent definitions in `.claude/agents/` carry the convention instead: a
    description containing "never delegate" plus the field for the
    surfaces that do honor it. Write/pipeline agents in
    `.github/agents/` can appear and run on github.com without the
    agents-live envelope, so they must pin `target: vscode`."""
    delegation: list[str] = []
    exposure: list[str] = []
    for name, data in native.get(".claude/agents", []):
        description = str(data.get("description") or "").lower()
        if ("never delegate" not in description
                or data.get("disable-model-invocation") is not True):
            delegation.append(name)
    for name, data in native.get(".github/agents", []):
        if (str(data.get("mode") or "plan") in ("write", "pipeline")
                and str(data.get("target") or "") != "vscode"):
            exposure.append(name)
    return delegation, exposure


def _copilot_tolerance_probe(native: dict[str, list[tuple[str, dict]]]) -> tuple[bool, str] | None:
    """Tripwire for convergence risk 1: the Copilot CLI's fast-fail
    listing must still include our native agent files even though they
    carry extension fields (and its `.claude/agents/` read is
    undocumented). None = not applicable (no copilot, no native agents,
    or the probe could not run)."""
    if not _has("copilot"):
        return None
    names = [name for entries in native.values() for name, _ in entries]
    if not names:
        return None
    try:
        r = subprocess.run(
            ["copilot", "--agent", "__agents_live_doctor_probe__", "-p", "probe"],
            capture_output=True, **hostruntime.CHILD_TEXT, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    listing = (r.stdout or "") + (r.stderr or "")
    listed = [name for name in names if name in listing]
    if listed:
        return True, f"copilot lists {len(listed)}/{len(names)} native agents"
    return False, ("copilot's fast-fail listing shows none of the native "
                   "agents - extension-field tolerance or the "
                   ".claude/agents read may have regressed")


def _package_checks() -> list[tuple[str, bool, bool, str, str]] | None:
    """Packaged-install checks (§3.4.1, Phase 4): the package itself is
    importable with a version, and the installed skill payload's VERSION
    marker matches it (skill and CLI must not drift). None when running
    from the flat checkout - not applicable there."""
    if not __package__:
        return None
    import importlib
    results: list[tuple[str, bool, bool, str, str]] = []
    package_name = __package__.split(".")[0]
    try:
        pkg = importlib.import_module(package_name)
        version = str(getattr(pkg, "__version__", "") or "")
    except Exception as exc:  # a broken install must surface, not crash
        return [("agents-live package importable", False, True,
                 "reinstall with `uv tool install agents-live`",
                 f"import failed: {exc}")]
    ok = bool(version)
    results.append((
        "agents-live package importable", ok, True,
        "reinstall with `uv tool install agents-live`",
        f"version {version}" if ok else "package has no __version__"))
    marker = REPO / ".claude" / "skills" / "agents-live" / "VERSION"
    if not marker.is_file():
        results.append((
            "skill payload version matches package", True, False,
            "run `agents-live init` to install the skill payload",
            "no installed skill payload to check (the skill is optional)"))
        return results
    installed = marker.read_text(encoding="utf-8").strip()
    results.append((
        "skill payload version matches package", installed == version, False,
        "run `agents-live upgrade --skills-only` to refresh project skill payloads",
        f"skill {installed} vs package {version}"))
    return results


def _add_windows_heartbeat_checks(add) -> None:
    from . import heartbeat

    beacon = heartbeat.beacon_path()
    if beacon.is_file():
        heartbeat_age_min = (time.time() - beacon.stat().st_mtime) / 60
        heartbeat_ok = heartbeat_age_min <= 10
        heartbeat_note = f"written {heartbeat_age_min:.0f} min ago"
    else:
        heartbeat_ok = False
        heartbeat_note = f"never written ({beacon})"
    add("Windows heartbeat working", heartbeat_ok, False,
        "run `agents-live heartbeat install --distro <name>`; see "
        ".claude/skills/agents-live/docs/windows-heartbeat.md",
        note=heartbeat_note)

    heartbeat_config = _windows_heartbeat_config()
    if heartbeat_config is not None:
        config_ok, config_note = heartbeat_config
        add("Windows heartbeat configured", config_ok, False,
            "run `agents-live heartbeat install --distro <name>` to install "
            "or migrate the task; see "
            ".claude/skills/agents-live/docs/windows-heartbeat.md",
            note=config_note)


def collect() -> list[dict]:
    """Run every check; return a list of result dicts."""
    checks: list[dict] = []
    project_checks = _project_checks_enabled()

    def add(name: str, ok: bool, required: bool, fix: str, note: str = "") -> None:
        checks.append({
            "name": name, "ok": ok, "required": required,
            "fix": fix, "note": note,
        })

    def add_host_runtime_checks() -> None:
        add("node", _has("node"), False, "install Node.js (e.g. nvm install --lts)")
        add("npm", _has("npm"), False, "install Node.js (e.g. nvm install --lts)")
        if hostruntime.id() == hostruntime.WSL:
            add("node is WSL-native (not /mnt/c interop)",
                _node_is_wsl_native(), False,
                ". ~/.nvm/nvm.sh && nvm use node  "
                "(ensure ~/.nvm precedes /mnt/c in PATH)",
                note="Windows-interop node writes MSAL tokens to the Windows "
                     "keychain; MCP logins won't populate ~/.config/ms365-mcp")

        needed = _agent_cli_needed_by_host() if project_checks else {}

        def agent_note(keyword: str, default: str) -> str:
            if not project_checks:
                return f"agent requirements unavailable until init; {default}"
            buckets = needed.get(keyword, {})
            owned = buckets.get("owned", [])
            unclaimed = buckets.get("unclaimed", [])
            parts = []
            if owned:
                parts.append(
                    f"needed for agents owned by this host: {', '.join(owned)}")
            if unclaimed:
                parts.append("needed for unclaimed agents any host may run: "
                             f"{', '.join(unclaimed)}")
            if parts:
                return "; ".join(parts)
            return (f"not required on this host (no owned agents use {keyword}); "
                    f"{default}")

        add("claude CLI", _has("claude"), False,
            "npm i -g @anthropic-ai/claude-code",
            note=agent_note(
                "claude", "skip if this host never runs claude/agency claude agents"))
        add("copilot CLI", _has("copilot"), False, "npm i -g @github/copilot",
            note=agent_note(
                "copilot", "skip if this host never runs copilot/agency copilot agents"))
        add("agency CLI", _has("agency"), False,
            "curl -sSfL https://aka.ms/InstallTool.sh | sh -s agency",
            note=agent_note(
                "agency", "skip if this host has no Microsoft-account/network "
                          "access, or no owned agents use agency"))

    add("uv", _has("uv"), True,
        "curl -LsSf https://astral.sh/uv/install.sh | sh")
    add("python3.12 (via uv)", _python_312_resolvable(), True,
        "uv python install 3.12  (ensure repo-root .python-version pins 3.12)")
    add(*_mechanism_check("schedule"))
    add("jq", _has("jq"), False, _fix("sudo apt install jq",
                                      "winget install jqlang.jq"),
        note="only needed by custom shell handlers that parse JSON; the "
             "shipped write-files.py handler does not use it")
    add(*_mechanism_check("watch"))
    add_host_runtime_checks()

    if not project_checks:
        if hostruntime.id() == hostruntime.WSL:
            _add_windows_heartbeat_checks(add)
        return checks

    # project_checks implies REPO is not None (_project_checks_enabled).
    add("Agents/ directory", (REPO / "Agents").is_dir(), True, "repo layout issue")
    add("Agents/handlers/ directory", (REPO / "Agents" / "handlers").is_dir(), True,
        "repo layout issue")

    # Packaged-install checks (§3.4.1, Phase 4) - flat checkout skips.
    for entry in _package_checks() or []:
        add(*entry)

    # Installation checks (§3.4.1 doctor, Phase 3) - strictly read-only.
    config_ok, config_note = _config_state()
    add("project config", config_ok, True,
        "run `agents-live init` (never hand-edit the config)",
        note=config_note)
    try:
        from . import plugins  # noqa: PLC0415
        for plugin_name, plugin_ok, plugin_note in plugins.checks(REPO):
            add(
                f"plugin {plugin_name} installed and entry points resolve",
                plugin_ok,
                True,
                "run `agents-live upgrade` to converge declared plugins",
                note=plugin_note,
            )
    except (OSError, ValueError, plugins.PluginError) as exc:
        add(
            "declared plugins valid",
            False,
            True,
            "repair [plugins], then run `agents-live upgrade`",
            note=str(exc),
        )
    registry_declared = False
    try:
        from . import ownership  # noqa: PLC0415
        registry_declared = paths.load_config(REPO).get("ownership") == "registry"
        registry_available = ownership.registry_available() if registry_declared else True
    except Exception as exc:
        registry_available = False
        registry_note = str(exc)
    else:
        registry_note = (
            "agents_live.ownership registry backend resolves"
            if registry_available else
            "ownership = \"registry\" but no agents_live.ownership "
            "registry backend resolves"
        )
    if registry_declared:
        add(
            "registry ownership backend resolves",
            registry_available,
            True,
            "run `agents-live upgrade` to converge declared plugins",
            note=registry_note,
        )
    consistency = _trigger_inconsistencies()
    if consistency is not None:
        store = ("the task store"
                 if hostruntime.native_scheduler() == hostruntime.TASK_SCHEDULER
                 else "crontab")
        orphans, stale = consistency
        note = ""
        if orphans:
            note = f"{store} references deleted agent(s): {', '.join(orphans)}"
        if stale:
            joined = ", ".join(stale)
            note = f"{note}; " if note else ""
            note += f"{store} references missing script(s): {joined}"
        add(f"{store} entries match agent files",
            not orphans and not stale, False,
            "run `agents-live doctor --repair` to converge stale script paths "
            "and orphaned triggers; " + _fix(
                "edit with `crontab -e` for entries from a moved or deleted "
                "project root",
                "remove tasks from a moved or deleted project root by hand "
                "in Task Scheduler"),
            note=note or "no orphaned or stale entries")

    session = _task_session_requirement()
    if session is not None:
        ok, session_note = session
        add("scheduled tasks need a signed-in session", ok, False,
            "re-register with `agents-live start --name <agent>`; a task "
            "given another logon type by hand no longer runs as you",
            note=session_note)

    # Watcher self-heal coverage (commands.md check 13): a running watcher
    # without its @reboot respawn line is invisible to both reboot restore
    # and the health check's restart loop - the line IS the durable intent
    # registry (see the health-check agent doc).
    try:
        from . import schedules  # noqa: PLC0415
        from .headless import (  # noqa: PLC0415
            _list_active_watcher_agent_names)
        running = set(_list_active_watcher_agent_names())
        intended = set(schedules.watcher_respawn_names())
        uncovered = sorted(running - intended)
        dead = sorted(intended - running)
    except Exception:
        uncovered = None  # enumeration unavailable: skip
        dead = None
    if uncovered is not None:
        add("active watchers have respawn entries", not uncovered, False,
            "cycle the watcher: `stop <name>` then `start <name>` "
            "(start reinstalls the entry)",
            note=(f"unhealable watcher(s): {', '.join(uncovered)}" if uncovered
                  else "all running watchers are self-heal covered"))
    # The inverse gap (commands.md check 14): a respawn entry marks a watcher
    # as intended on this host, but its process may have died. The hourly
    # health check restarts these; doctor is the out-of-band detector for
    # the state where that loop itself is not firing. Without this check the
    # coverage check above passes vacuously when zero watchers are running.
    if dead is not None:
        add("intended watchers are running", not dead, False,
            "run `start <name>` to relaunch, or run "
            "`agents-live doctor --repair`",
            note=(f"dead watcher(s) with respawn intent: {', '.join(dead)}"
                  if dead else "all intended watchers have live processes"))

    # The built-in check-and-repair loop must be installed: its startup +
    # hourly entries are what keep everything above converged. The loop
    # self-installs its entries on every run, so the repair is simply to
    # run it once.
    try:
        from . import health_check  # noqa: PLC0415
        installed = health_check.health_entries_installed()
    except Exception:
        installed = None  # unreadable store: skip, don't vouch
    if installed is not None:
        add("automatic maintenance installed", installed, False,
            "run `agents-live doctor --repair`",
            note=("startup + hourly entries present" if installed
                  else "no automatic maintenance entries on this host"))

    # Liveness of the check-and-repair loop itself. The health check (boot +
    # hourly) converges the triggers and restarts dead watchers; when its own
    # entry is broken nothing runs and nothing logs, so a stale beacon is the
    # one signal left. doctor is the out-of-band detector for that state.
    beacon = paths.health_beacon_path()
    if beacon.is_file():
        age_min = (time.time() - beacon.stat().st_mtime) / 60
        beacon_ok = age_min <= 75
        beacon_note = f"written {age_min:.0f} min ago"
    else:
        beacon_ok = False
        beacon_note = "never written (maintenance has not run on this host)"
    add("health beacon fresh (automatic maintenance alive)", beacon_ok, False,
        "run `agents-live doctor --repair` and re-check",
        note=beacon_note)

    # Native-agent policy lints + platform tripwire (convergence C2).
    native = _native_agents()
    if native is not None and any(native.values()):
        delegation, exposure = _native_agent_lints(native)
        add("native agents: delegation convention", not delegation, False,
            "add `disable-model-invocation: true` and a description "
            "containing 'Never delegate' (Claude Code honors only the "
            "description convention)",
            note=(f"missing on: {', '.join(delegation)}" if delegation
                  else "all .claude/agents definitions carry the convention"))
        add("native agents: cloud exposure policy", not exposure, False,
            "set `target: vscode` on write/pipeline agents in .github/agents/",
            note=(f"exposed: {', '.join(exposure)}" if exposure
                  else "no unpinned write/pipeline agents in .github/agents"))
        probe = _copilot_tolerance_probe(native)
        if probe is not None:
            probe_ok, probe_note = probe
            add("native agents: copilot still parses extension fields",
                probe_ok, False,
                "platform compatibility event: re-verify the converged "
                "format against the installed copilot CLI (convergence "
                "risk 1)", note=probe_note)

    if hostruntime.id() == hostruntime.WSL:
        _add_windows_heartbeat_checks(add)

    return checks


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check agents-live prerequisites.")
    parser.add_argument(
        "--all-repos", action="store_true",
        help="Run host checks once and project checks for every registered repo")
    parser.add_argument(
        "--repair", action="store_true",
        help="Run automatic maintenance immediately before diagnosis")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Show the repair plan without changing anything")
    args = parser.parse_args(argv)
    if args.dry_run and not args.repair:
        parser.error("--dry-run requires --repair")
    if args.all_repos and args.repair:
        parser.error("--all-repos and --repair are mutually exclusive")
    json_mode = preflight.json_mode()

    if args.repair:
        from . import health_check  # noqa: PLC0415
        repair_status = health_check.repair(
            dry_run=args.dry_run, quiet=json_mode)
        if args.dry_run or repair_status != 0:
            return repair_status

    if args.all_repos:
        payload = repos.collect_doctor()
        if json_mode:
            print(json.dumps(payload, indent=2))
        else:
            print(f"Host checks: {'PASS' if payload['host'].get('ok') else 'FAIL'}")
            for item in payload["repos"]:
                state = "PASS" if item["ok"] else "FAIL"
                detail = f": {item['error']}" if "error" in item else ""
                print(f"  [{state}] {item['name']} ({item['path']}){detail}")
        return 0 if payload["ok"] else 1

    project_checks = _project_checks_enabled()
    checks = collect()
    required_failures = [c for c in checks if c["required"] and not c["ok"]]
    optional_failures = [c for c in checks if not c["required"] and not c["ok"]]
    ok = not required_failures

    if json_mode:
        print(json.dumps({
            "ok": ok,
            "host": _hostname(),
            "project_checks": {
                "status": "run" if project_checks else "skipped",
                "reason": None if project_checks else "project not initialized",
            },
            "checks": checks,
        }, indent=2))
        return 0 if ok else 1

    host = _hostname()
    print(f"Prerequisites for agents-live (host: {host}):\n")
    if not project_checks:
        print("  Project checks skipped until `agents-live init` creates a "
              "project config.\n")
    for c in checks:
        if c["ok"]:
            mark = "PASS"
        else:
            mark = "FAIL" if c["required"] else "WARN"
        line = f"  [{mark}] {c['name']}"
        if c["note"]:
            line += f"  ({c['note']})"
        print(line)
        if not c["ok"]:
            print(f"         fix: {c['fix']}")

    print()
    if required_failures:
        names = ", ".join(c["name"] for c in required_failures)
        preflight.emit_failure(
            "doctor",
            f"{len(required_failures)} required check(s) failing: {names}",
            code="required_checks_failed")
    elif optional_failures:
        names = ", ".join(c["name"] for c in optional_failures)
        print(f"OK (required checks pass); {len(optional_failures)} optional missing: {names}")
    else:
        print("OK: all checks pass.")
    if update_check.interactive():
        print(f"\n{update_check.status_text()}")
    return 0 if ok else 1


def _hostname() -> str:
    try:
        out = subprocess.run(["hostname", "-s"], capture_output=True,
                             **hostruntime.CHILD_TEXT,
                             timeout=2).stdout.strip()
        if out:
            return out.lower()
    except (OSError, subprocess.TimeoutExpired):
        pass
    import socket
    return socket.gethostname().split(".", 1)[0].lower()


if __name__ == "__main__":
    raise SystemExit(main())
