"""Internal built-in host check-and-repair loop.

Promoted from a consumer-project agent (2026-07-19): the loop is
host-scoped - it repairs the host-global crontab, converges the
host-global uv tool environment, and sweeps every registered repository -
so it ships with the package and keeps its state at the user level
(``paths.state_home()``), never inside any project tree.

Two modes share this module:

- **Host mode** (``agents-live internal maintain``, the crontab entries'
    form): ensures its own ``@reboot`` + hourly crontab lines, converges
  declared plugin wheels into the tool environment, runs a per-repo
  sweep for every registered repository (each in its own subprocess -
  project-root resolution is process-global, so one process never
  operates on two repos), gates the framework smoketest on a content
  fingerprint, checks the Windows heartbeat on WSL, and writes the host
  ``health.ok`` beacon. Events are logged to
  ``<state home>/logs/health-check.log``.
- **Sweep mode** (``agents-live --repo <root> internal maintain --sweep``,
  internal): converges the repo's persisted crontab entries
    through internal migration, prunes orphans and fleet-wide registry orphans,
  enforces multi-host ownership, and restarts dead watchers. Prints one
  JSON summary to stdout for the host loop to aggregate.

Unavailable ownership DEGRADES the beacon and abstains from
enforcement - it never aborts the loop (the 2026-07-19 incident: an
ownership preflight failure silenced the previous, agent-based loop for
a day; the built-in loop must always complete and report).

The framework smoketest runs against the default registered repository
only when smoke-relevant content changed: handler/lib/plugin sources in
that repo, or the installed ``agents_live``-related distributions.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import os
import shlex
import signal
import subprocess
import sys
import time
from pathlib import Path

from . import heartbeat, ownership, paths, plugins, preflight, repos
from .headless import (
    AgentsLiveError,
    EventLog,
    agent_details,
    agent_file_exists,
    clean_path,
    cli_shim_path,
    crontab_lock,
    current_crontab_lines,
    install_crontab,
    list_agents,
    list_reboot_watcher_agent_names,
    load_agent_config,
    packaged_execution,
    repo_root,
)

PREFIX = "[health-check]"
HEALTH_SCHEDULES = ("@reboot", "0 * * * *")
SWEEP_TIMEOUT_S = 300
SMOKETEST_RUNTIME = "agency copilot"
SMOKETEST_TIMEOUT_S = 360
SMOKETEST_BUSY_EXIT = 75
# Smoke-relevant repo content: executable support code, not agent
# prompts or docs (the smoketest makes a real agent call, so it only
# re-runs when something that could break it changed).
SMOKETEST_DIR_NAMES = ("handlers", "lib", "plugins")


def _err(message: str) -> None:
    print(f"{PREFIX} {message}", file=sys.stderr)


# --- Host crontab entries ---------------------------------------------------

def build_health_cron_lines() -> list[str]:
    """The canonical host-level crontab lines for this loop.

    No ``cd`` and no ``--repo``: the loop is host-scoped and resolves
    registered repositories itself. PATH rides inside each line
    (self-contained crontab lines, same policy as agent entries).
    """
    shim = shlex.quote(str(cli_shim_path()))
    prefix = f"PATH={shlex.quote(clean_path())}"
    return [f"{sched} {prefix} {shim} internal maintain --quiet 2>&1"
            for sched in HEALTH_SCHEDULES]


def health_cron_line_matches(line: str) -> bool:
    """True when *line* invokes this built-in loop (any install of it).

    Matches both the internal maintenance command and legacy health-check
    entries so convergence removes the retired public invocation.
    """
    try:
        tokens = shlex.split(line)
    except ValueError:
        tokens = line.split()
    is_maintenance = (
        "health-check" in tokens
        or any(tokens[index:index + 2] == ["internal", "maintain"]
               for index in range(len(tokens) - 1))
    )
    return is_maintenance and any(
        Path(token).name == "agents-live" for token in tokens)


def ensure_health_cron_lines(*, install: bool = True) -> bool:
    """Install/converge the loop's crontab entries. True when changed.

    ``install=False`` converges existing entries after an upgrade re-homes
    the pinned shim path, but never adds them to an uninitialized host.
    """
    desired = build_health_cron_lines()
    with crontab_lock():
        lines = current_crontab_lines()
        if lines is None:
            raise AgentsLiveError("crontab is not accessible")
        kept = [line for line in lines if not health_cron_line_matches(line)]
        current = [line for line in lines if health_cron_line_matches(line)]
        if current == desired or (not current and not install):
            return False
        install_crontab(kept + desired)
        return True


def remove_health_cron_lines() -> bool:
    """Remove the loop's crontab entries (uninstall). True when removed."""
    with crontab_lock():
        lines = current_crontab_lines()
        if lines is None:
            raise AgentsLiveError("crontab is not accessible")
        kept = [line for line in lines if not health_cron_line_matches(line)]
        if len(kept) == len(lines):
            return False
        install_crontab(kept)
        return True


# --- Per-repo sweep (runs with the repo resolved) ---------------------------

def _self_argv(*args: str, root: Path | None = None) -> list[str]:
    """argv that re-enters this CLI, optionally pinned to *root*."""
    if packaged_execution():
        base = [str(cli_shim_path())]
    else:
        base = [sys.executable, "-m", "agents_live.cli"]
    if root is not None:
        base += ["--repo", str(root)]
    return base + list(args)


def _converge_crontab(events: list[dict[str, str]]) -> bool:
    """Converge this repo's persisted entries via migrate (in-process).

    Returns False (degrades the beacon) when migration fails or stale
    entries survive it.
    """
    from . import doctor, migrate
    buffer = io.StringIO()
    try:
        saved_argv = sys.argv
        sys.argv = ["agents-live internal migrate"]
        try:
            with contextlib.redirect_stdout(buffer):
                code = migrate.main()
        finally:
            sys.argv = saved_argv
    except Exception as exc:
        msg = f"crontab convergence failed: {exc}"
        _err(f"WARNING: {msg}")
        events.append({"level": "warning", "phase": "converge-crontab",
                       "message": msg})
        return False
    if code != 0:
        msg = f"crontab convergence failed (exit {code})"
        _err(f"WARNING: {msg}")
        events.append({"level": "warning", "phase": "converge-crontab",
                       "message": msg})
        return False
    rewritten = list(dict.fromkeys(
        line.split("'", 2)[1]
        for line in buffer.getvalue().splitlines()
        if line.startswith("Rewriting") and line.count("'") >= 2
    ))
    for name in rewritten:
        events.append({"level": "info", "phase": "converge-crontab",
                       "agent_name": name,
                       "message": f"converged stale crontab entry for '{name}'"})
    try:
        consistency = doctor._crontab_inconsistencies()
    except Exception as exc:  # pragma: no cover - defensive
        _err(f"WARNING: could not verify crontab consistency: {exc}")
        return True
    if consistency is None:
        return True
    stale = consistency[1]
    if stale:
        msg = ("crontab still references missing script(s) after migrate: "
               + ", ".join(stale))
        _err(f"WARNING: {msg}")
        events.append({"level": "warning", "phase": "converge-crontab",
                       "message": msg})
        return False
    return True


def _agent_states() -> dict[str, dict]:
    states: dict[str, dict] = {}
    for name in list_agents():
        try:
            states[name] = agent_details(load_agent_config(name))
        except AgentsLiveError:
            continue
    return states


def _add_persisted_agent_states(states: dict[str, dict]) -> None:
    """Load path-backed definitions named by persisted watcher intent."""
    for selector in list_reboot_watcher_agent_names():
        if selector in states:
            continue
        try:
            states[selector] = agent_details(load_agent_config(selector))
        except AgentsLiveError:
            continue


def _lifecycle(subcommand: str, name: str) -> bool:
    """Run one lifecycle operation (start/stop) for *name* via the CLI
    so the full activation/teardown semantics apply."""
    result = subprocess.run(
        _self_argv(subcommand, "--name", name, root=repo_root()),
        capture_output=True, text=True, check=False,
    )
    if result.returncode == 0:
        return True
    _err(f"ERROR: {subcommand} {name} failed: {result.stderr.strip()[:500]}")
    return False


def _origin_main_synced(root: Path) -> bool:
    """True if a best-effort fetch succeeds and ``HEAD == origin/main``.

    Registry pruning edits shared, git-tracked state, so it must only run
    from a checkout converged with the remote; a stale or offline host
    abstains rather than erasing a still-live ownership pin.
    """
    def _git(*args: str, timeout: int) -> subprocess.CompletedProcess | None:
        try:
            return subprocess.run(
                ["git", *args], cwd=root,
                capture_output=True, text=True, timeout=timeout)
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return None
    fetched = _git("fetch", "--quiet", "origin", "main", timeout=30)
    if fetched is None or fetched.returncode != 0:
        return False
    head = _git("rev-parse", "HEAD", timeout=10)
    origin = _git("rev-parse", "origin/main", timeout=10)
    if head is None or origin is None:
        return False
    if head.returncode != 0 or origin.returncode != 0:
        return False
    return head.stdout.strip() == origin.stdout.strip()


def _agent_definition_exists(name: str, root: Path) -> bool:
    """True if the agent exists locally or in the committed tree (HEAD).

    On any git error, returns True (assume defined and never prune).
    """
    if agent_file_exists(name):
        return True
    dirs = ["Agents"]
    try:
        extra = paths.load_config(root).get("agent_directories", [])
    except ValueError:
        extra = []
    if isinstance(extra, list):
        dirs += [str(d) for d in extra if d and str(d) not in dirs]
    for d in (".claude/agents", ".github/agents"):
        if d not in dirs:
            dirs.append(d)
    for d in dirs:
        for filename in (f"{name}.md", f"{name}.agent.md"):
            try:
                probe = subprocess.run(
                    ["git", "cat-file", "-e", f"HEAD:{d}/{filename}"],
                    cwd=root, capture_output=True, text=True, timeout=10,
                )
            except (FileNotFoundError, subprocess.TimeoutExpired):
                return True
            if probe.returncode == 0:
                return True
    return False


def _prune_registry_orphans(root: Path, events: list[dict[str, str]]) -> dict:
    """Prune ``agent-owners.json`` entries whose definition is gone
    fleet-wide (absent at ``origin/main``). Abstains unless converged."""
    if not _origin_main_synced(root):
        return {"abstained": True, "reason": "not converged with origin/main",
                "pruned": [], "degraded": False}
    try:
        owners = ownership.load_owners(rate_limit_secs=10**9)
    except ownership.OwnershipUnavailableError as exc:
        msg = f"ownership registry unavailable; registry prune abstained: {exc}"
        _err(f"WARNING: {msg}")
        events.append({"level": "warning", "phase": "prune-registry",
                       "message": msg})
        return {"abstained": True, "reason": str(exc), "pruned": [],
                "degraded": True}
    pruned: list[str] = []
    for name in sorted(owners):
        if name.startswith("_"):
            continue  # ephemeral fixtures are never registered or pruned
        if _agent_definition_exists(name, root):
            continue
        if ownership.remove_owner(name):
            pruned.append(name)
            msg = (f"pruned registry entry '{name}' (owner {owners[name]}; "
                   "no agent definition at origin/main)")
            events.append({"level": "info", "phase": "prune-registry",
                           "agent_name": name, "message": msg})
            _err(msg)
    return {"abstained": False, "pruned": pruned, "degraded": False}


def _enforce_ownership(
    states: dict[str, dict], events: list[dict[str, str]],
) -> tuple[list[str], bool]:
    """Deactivate agents active here but owned by another host.

    Unavailable ownership abstains and flags degraded health - it must
    never read as "nothing to enforce".
    """
    try:
        owners = ownership.load_owners(rate_limit_secs=10**9)
    except ownership.OwnershipUnavailableError as exc:
        msg = f"ownership registry unavailable; enforcement abstained: {exc}"
        _err(f"WARNING: {msg}")
        events.append({"level": "warning", "phase": "ownership-deactivate",
                       "message": msg})
        return [], True
    if not owners:
        return [], False
    host = ownership.current_host()
    deactivated: list[str] = []
    for name in sorted(states):
        owner = owners.get(name)
        if owner is None or owner == "*" or owner.lower() == host:
            continue
        agent = states[name]
        state = str(agent.get("state", ""))
        trigger_states = agent.get("triggerStates", {})
        active_here = (
            "active" in state.lower()
            or any("active" in str(v).lower() for v in trigger_states.values())
        )
        if not active_here:
            continue
        if _lifecycle("stop", name):
            deactivated.append(name)
            msg = f"deactivated '{name}': ownership assigned to {owner}"
            _err(msg)
            events.append({"level": "info", "phase": "ownership-deactivate",
                           "agent_name": name, "message": msg})
        else:
            events.append({"level": "warning", "phase": "ownership-deactivate",
                           "agent_name": name,
                           "message": f"failed to deactivate '{name}' "
                                      f"(owned by {owner})"})
    return deactivated, False


def sweep() -> dict:
    """The per-repo pass: converge, prune, enforce, restart. Returns the
    summary the host loop aggregates."""
    root = repo_root()
    events: list[dict[str, str]] = []

    crontab_degraded = not _converge_crontab(events)

    # Decommission deleted agents: any cron/watcher still live here whose
    # agent file is gone is torn down, so a deleted definition self-cleans
    # fleet-wide on the next pass.
    from . import activate
    try:
        for name in activate.prune_orphans():
            msg = f"pruned orphaned agent '{name}' (agent file no longer exists)"
            events.append({"level": "info", "phase": "prune-orphan",
                           "agent_name": name, "message": msg})
            _err(msg)
    except Exception as exc:
        _err(f"ERROR: prune-orphans failed: {exc}")

    registry_prune = _prune_registry_orphans(root, events)

    states = _agent_states()
    _add_persisted_agent_states(states)
    ownership_deactivated, ownership_degraded = _enforce_ownership(
        states, events)
    ownership_degraded = ownership_degraded or bool(
        registry_prune.get("degraded"))

    # Intended watchers: the durable set encoded as @reboot respawn lines.
    # A deliberate stop removes the line, so a stopped agent is invisible
    # here and is not restarted ("owned but stopped" is durable).
    intended = [name for name in list_reboot_watcher_agent_names()
                if name not in ownership_deactivated]

    dead: list[str] = []
    restarted: list[str] = []
    failed: list[str] = []
    for name in intended:
        agent = states.get(name)
        if not agent:
            msg = (f"watcher '{name}' has an @reboot respawn line but no "
                   "agent definition found")
            _err(f"WARNING: {msg}")
            events.append({"level": "warning", "phase": "check",
                           "agent_name": name, "message": msg})
            failed.append(name)
            continue
        watcher_state = str(agent.get("triggerStates", {}).get(
            "watcher", agent.get("state", "")))
        if "active" in watcher_state.lower():
            continue
        dead.append(name)
        msg = (f"watcher '{name}' is not running (state: {watcher_state}) - "
               "attempting restart")
        _err(f"WARNING: {msg}")
        events.append({"level": "warning", "phase": "check",
                       "agent_name": name, "message": msg})
        if _lifecycle("start", name):
            restarted.append(name)
            events.append({"level": "info", "phase": "restart",
                           "agent_name": name,
                           "message": f"restarted watcher '{name}'"})
        else:
            failed.append(name)
            events.append({"level": "warning", "phase": "restart",
                           "agent_name": name,
                           "message": f"watcher '{name}' could not be restarted"})

    cron_count = sum(
        1 for agent in states.values()
        if agent.get("schedule") and "active" in str(
            agent.get("triggerStates", {}).get("cron", agent.get("state", ""))
        ).lower()
    )

    return {
        "repo": str(root),
        "status": "error" if failed else "ok",
        "watchers": len(intended),
        "cron": cron_count,
        "dead": dead,
        "restarted": restarted,
        "failed": failed,
        "ownership_deactivated": ownership_deactivated,
        "ownership_degraded": ownership_degraded,
        "crontab_degraded": crontab_degraded,
        "registry_pruned": registry_prune["pruned"],
        "registry_prune_abstained": registry_prune["abstained"],
        "events": events,
    }


def plan_sweep() -> list[dict]:
    """Describe workspace mutations a sweep would perform without applying them."""
    from . import activate  # noqa: PLC0415

    actions: list[dict] = []
    states = _agent_states()
    _add_persisted_agent_states(states)
    running = set(activate.list_active_agent_names())
    defined = set(list_agents())
    for name in sorted(running - defined):
        if not agent_file_exists(name):
            actions.append({"action": "prune-orphaned-trigger", "agent": name})

    owners: dict[str, str] = {}
    try:
        owners = ownership.load_owners(rate_limit_secs=10**9)
    except ownership.OwnershipUnavailableError:
        pass
    host = ownership.current_host()
    ownership_deactivated: set[str] = set()
    for name, agent in sorted(states.items()):
        owner = owners.get(name)
        trigger_states = agent.get("triggerStates", {})
        active_here = (
            "active" in str(agent.get("state", "")).lower()
            or any("active" in str(value).lower()
                   for value in trigger_states.values())
        )
        if owner and owner != "*" and owner.lower() != host and active_here:
            ownership_deactivated.add(name)
            actions.append({
                "action": "deactivate-for-ownership",
                "agent": name,
                "owner": owner,
            })

    for name in list_reboot_watcher_agent_names():
        if name in ownership_deactivated:
            continue
        agent = states.get(name)
        if agent is None:
            if not any(action.get("agent") == name for action in actions):
                actions.append({
                    "action": "prune-orphaned-trigger", "agent": name})
            continue
        watcher_state = str(agent.get("triggerStates", {}).get(
            "watcher", agent.get("state", "")))
        if "active" not in watcher_state.lower():
            actions.append({"action": "restart-watcher", "agent": name})

    head = _git_head(repo_root())
    try:
        remote = subprocess.run(
            ["git", "ls-remote", "origin", "refs/heads/main"], cwd=repo_root(),
            capture_output=True, text=True, check=False, timeout=10,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        remote = None
    remote_sha = (
        remote.stdout.split()[0]
        if remote is not None and remote.returncode == 0 and remote.stdout.split()
        else None
    )
    if head and remote_sha == head:
        for name in sorted(owners):
            if not name.startswith("_") and not _agent_definition_exists(
                    name, repo_root()):
                actions.append({
                    "action": "prune-ownership-record", "agent": name})
    return actions


# --- Smoketest gating (host mode) -------------------------------------------

def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _git_head(root: Path) -> str | None:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=root,
            capture_output=True, text=True, check=True, timeout=5,
        ).stdout.strip()
        return out or None
    except (subprocess.CalledProcessError, FileNotFoundError,
            subprocess.TimeoutExpired, OSError):
        return None


def _installed_framework_dists() -> list[str]:
    """name-version of the installed agents-live dist plus every dist
    providing a kernel entry point, so a tool upgrade or plugin change
    re-triggers the smoketest even when no repo file changed."""
    import importlib.metadata
    names: set[str] = set()
    for dist in importlib.metadata.distributions():
        try:
            name = dist.metadata["Name"] or ""
        except Exception:
            continue
        canonical = name.replace("_", "-").lower()
        relevant = canonical.startswith("agents-live") or any(
            ep.group in plugins.ENTRY_POINT_GROUPS
            for ep in dist.entry_points)
        if relevant:
            names.add(f"{name}-{dist.version}")
    return sorted(names)


def smoketest_source_fingerprint(root: Path) -> str | None:
    """Hash smoke-relevant content: repo handler/lib/plugin sources
    (including uncommitted changes) plus the installed framework dists."""
    digest = hashlib.sha256()
    agent_dirs = ["Agents"]
    try:
        extra = paths.load_config(root).get("agent_directories", [])
    except ValueError:
        extra = []
    if isinstance(extra, list):
        agent_dirs += [str(d) for d in extra if d and str(d) not in agent_dirs]
    try:
        for agent_dir in agent_dirs:
            for sub in SMOKETEST_DIR_NAMES:
                base = root / agent_dir / sub
                if not base.is_dir():
                    continue
                for path in sorted(p for p in base.rglob("*") if p.is_file()):
                    if "__pycache__" in path.parts:
                        continue
                    digest.update(path.relative_to(root).as_posix().encode())
                    digest.update(b"\0")
                    digest.update(path.read_bytes())
                    digest.update(b"\0")
    except OSError:
        return None
    for dist in _installed_framework_dists():
        digest.update(dist.encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()


def _resolve_smoketest_runtime() -> str | None:
    """Runtime for the framework smoketest, or None to skip on this host.

    The smoketest makes a real agent call; hosts without the agency CLI
    own only handler-only agents, so there is no agent path to validate.
    """
    import shutil
    if shutil.which("agency"):
        return SMOKETEST_RUNTIME
    candidate = Path.home() / ".config" / "agency" / "CurrentVersion" / "agency"
    if candidate.is_file():
        return SMOKETEST_RUNTIME
    return None


def _run_smoketest(root: Path, runtime: str) -> dict:
    started = time.time()
    result_path = paths.repo_state_dir(root) / "logs" / \
        "smoketest-framework-result.json"
    try:
        process = subprocess.Popen(
            _self_argv("smoketest", "--runtime", runtime, root=root),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            start_new_session=True,
        )
        try:
            stdout, stderr = process.communicate(timeout=SMOKETEST_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.communicate(timeout=15)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.communicate()
            return {"status": "fail",
                    "duration_s": round(time.time() - started, 1),
                    "runtime": runtime,
                    "reason": f"timeout after {SMOKETEST_TIMEOUT_S}s"}
        duration = round(time.time() - started, 1)
        persisted: dict = {}
        try:
            if result_path.stat().st_mtime >= started - 1:
                persisted = json.loads(result_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            persisted = {}
        detail = {key: persisted[key]
                  for key in ("runtime", "model", "failed_step", "reason")
                  if persisted.get(key) is not None}
        if process.returncode == 0:
            return {"status": "pass", "duration_s": duration,
                    "runtime": runtime, **detail}
        if process.returncode == SMOKETEST_BUSY_EXIT:
            return {"status": "busy", "duration_s": duration,
                    "runtime": runtime,
                    "reason": "another framework smoketest is already running"}
        tail = (stderr or stdout or "").strip().splitlines()[-1:]
        reason = tail[0][:200] if tail else f"exit {process.returncode}"
        return {"status": "fail", "duration_s": duration, "runtime": runtime,
                "reason": detail.get("reason", reason), **detail}
    except (FileNotFoundError, OSError) as exc:
        return {"status": "fail",
                "duration_s": round(time.time() - started, 1),
                "reason": f"could not invoke smoketest: {exc}"}


def maybe_run_smoketest(root: Path, prev: dict,
                        events: list[dict[str, str]]) -> dict:
    """Run the framework smoketest only when relevant content changed
    (or the previous verdict was not a pass)."""
    runtime = _resolve_smoketest_runtime()
    if runtime is None:
        events.append({"level": "info", "phase": "smoketest",
                       "message": "smoketest skipped (no agency CLI on this host)"})
        return {"status": "skipped", "reason": "no agency CLI on this host",
                "sha": _git_head(root), "ts": _now_iso()}

    current_sha = _git_head(root)
    current_fingerprint = smoketest_source_fingerprint(root)
    if current_sha is None:
        events.append({"level": "warning", "phase": "smoketest",
                       "message": "could not resolve HEAD sha; skipping smoketest"})
        return prev or {"status": "unknown", "reason": "no git sha"}

    prev = prev if isinstance(prev, dict) else {}
    prev_status = prev.get("status")
    prev_fingerprint = prev.get("source_fingerprint")

    if prev.get("sha") and prev_fingerprint:
        if prev_status == "pass" and prev_fingerprint == current_fingerprint:
            # Pass + identical relevant content: nothing to do, even if
            # HEAD moved for an unrelated change. Preserve the test time.
            return prev
        if prev_status != "pass":
            _err(f"smoketest: retrying (previous verdict: {prev_status})")
    else:
        _err("smoketest: no prior verdict, running bootstrap smoketest")

    verdict = _run_smoketest(root, runtime)
    if verdict["status"] == "busy":
        events.append({"level": "info", "phase": "smoketest",
                       "message": verdict["reason"]})
        return prev or {**verdict, "sha": current_sha,
                        "source_fingerprint": current_fingerprint,
                        "ts": _now_iso()}
    verdict["sha"] = current_sha
    verdict["source_fingerprint"] = current_fingerprint
    verdict["ts"] = _now_iso()
    level = "info" if verdict["status"] == "pass" else "warning"
    msg = f"smoketest {verdict['status']} ({verdict.get('duration_s', 0)}s)"
    if verdict["status"] != "pass":
        msg += f": {verdict.get('reason', '')}"
    events.append({"level": level, "phase": "smoketest", "message": msg})
    _err(msg)
    return verdict


# --- Host loop ---------------------------------------------------------------

def _registered_roots() -> list[tuple[str, Path]]:
    """Every initialized workspace maintained by the host loop."""
    rows: list[tuple[str, Path]] = []
    global_root = paths.global_root()
    if paths.config_source(global_root) is not None:
        rows.append(("global", global_root))
    rows.extend(
        (alias, Path(path)) for alias, path, error in repos.entries()
        if error is None and Path(path) != global_root
    )
    local = paths.local_root()
    if local is not None and all(root != local for _, root in rows):
        rows.append((local.name, local))
    for root in persisted_roots():
        if all(existing != root for _, existing in rows):
            rows.append((root.name, root))
    return rows


def persisted_roots() -> list[Path]:
    """Existing roots pinned by active Agents Live trigger invocations."""
    roots: list[Path] = []
    for line in current_crontab_lines() or []:
        try:
            tokens = shlex.split(line)
        except ValueError:
            continue
        if not any(Path(token).name == "agents-live" for token in tokens):
            continue
        for first, second in zip(tokens, tokens[1:]):
            if first != "--repo":
                continue
            root = Path(second).expanduser().resolve()
            if root.is_dir() and root not in roots:
                roots.append(root)
            break
    return roots


def _check_windows_heartbeat(events: list[dict[str, str]]) -> None:
    if not heartbeat.is_wsl():
        return
    beacon = heartbeat.beacon_path()
    if beacon.is_file():
        age_min = (time.time() - beacon.stat().st_mtime) / 60
        if age_min > 10:
            msg = (f"Windows heartbeat stale ({age_min:.0f} min old) - "
                   "Task Scheduler may not be running")
            _err(f"WARNING: {msg}")
            events.append({"level": "warning", "phase": "heartbeat",
                           "message": msg})
        return
    msg = ("Windows heartbeat beacon missing - run "
           "`agents-live heartbeat install`")
    _err(f"WARNING: {msg}")
    events.append({"level": "warning", "phase": "heartbeat", "message": msg})


def _load_previous_beacon() -> dict:
    beacon = paths.health_beacon_path()
    if not beacon.is_file():
        return {}
    try:
        data = json.loads(beacon.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def run_host_loop(quiet: bool) -> int:
    log = EventLog(paths.host_logs_dir() / "health-check.log",
                   agent_name="health-check")
    started = time.time()
    log.stage_start("start", trigger="cron" if not sys.stdout.isatty()
                    else "manual")
    events: list[dict[str, str]] = []

    # Self-heal this loop's own crontab entries first: the loop must
    # survive tool reinstalls that re-home the pinned shim path.
    try:
        if ensure_health_cron_lines():
            events.append({"level": "info", "phase": "ensure-cron",
                           "message": "converged health-check crontab entries"})
    except AgentsLiveError as exc:
        _err(f"WARNING: could not converge health-check crontab entries: {exc}")

    targets = _registered_roots()
    if not targets:
        # Cron runs from $HOME, so an unregistered host resolves no
        # repository at all: the loop would sweep nothing yet still
        # report healthy. Surface the misconfiguration instead.
        msg = ("no initialized workspace; run `agents-live init` before "
               "automatic maintenance")
        _err(f"WARNING: {msg}")
        events.append({"level": "warning", "phase": "sweep", "message": msg})

    # Converge declared plugin wheels: a bare tool reinstall drops
    # co-installed plugins, and with ownership = "registry" a missing
    # backend degrades every sweep, so this hourly pass is the safety net.
    try:
        if plugins.converge([root for _, root in targets]):
            msg = "converged declared plugin wheel(s) into the tool environment"
            _err(msg)
            events.append({"level": "info", "phase": "ensure-framework",
                           "message": msg})
    except Exception as exc:
        msg = f"plugin convergence failed: {exc}"
        _err(f"WARNING: {msg}")
        events.append({"level": "warning", "phase": "ensure-framework",
                       "message": msg})

    sweeps: dict[str, dict] = {}
    for alias, root in targets:
        try:
            result = subprocess.run(
                _self_argv("internal", "maintain", "--sweep", root=root),
                capture_output=True, text=True, timeout=SWEEP_TIMEOUT_S,
            )
        except subprocess.TimeoutExpired:
            sweeps[alias] = {"repo": str(root), "status": "error",
                             "reason": f"sweep timed out after {SWEEP_TIMEOUT_S}s"}
            continue
        if result.returncode != 0 and not result.stdout.strip():
            sweeps[alias] = {"repo": str(root), "status": "error",
                             "reason": result.stderr.strip()[:500]
                             or f"sweep exit {result.returncode}"}
            continue
        try:
            sweeps[alias] = json.loads(result.stdout)
        except json.JSONDecodeError:
            sweeps[alias] = {"repo": str(root), "status": "error",
                             "reason": "sweep emitted non-JSON output"}
    for alias, result in sweeps.items():
        for event in result.get("events", []):
            log.event(**{"phase": "sweep", "repo": alias, **event})
        if result.get("status") == "error" and result.get("reason"):
            _err(f"ERROR: sweep {alias}: {result['reason']}")

    watcher_total = sum(int(s.get("watchers") or 0) for s in sweeps.values())
    cron_total = sum(int(s.get("cron") or 0) for s in sweeps.values())
    failed = [f"{alias}:{name}" for alias, s in sweeps.items()
              for name in s.get("failed", [])]
    sweep_errors = [alias for alias, s in sweeps.items()
                    if s.get("status") == "error"]
    ownership_degraded = any(s.get("ownership_degraded") for s in sweeps.values())
    crontab_degraded = any(s.get("crontab_degraded") for s in sweeps.values())

    _check_windows_heartbeat(events)

    infra_ok = not failed and not sweep_errors
    smoketest_field: dict = {}
    if infra_ok and targets:
        default = repos.default_root() or targets[0][1]
        prev = _load_previous_beacon().get("smoketest")
        smoketest_field = maybe_run_smoketest(
            default, prev if isinstance(prev, dict) else {}, events)

    # A failing smoketest, unavailable ownership (enforcement abstained),
    # or an unconverged crontab means the system is NOT healthy even
    # though watcher/cron infrastructure is up. Freshness (mtime) stays
    # the liveness signal in all states.
    if infra_ok:
        beacon_status = (
            "degraded"
            if smoketest_field.get("status") == "fail" or ownership_degraded
            or crontab_degraded or not targets
            else "healthy"
        )
        payload = {
            "status": beacon_status,
            "ts": _now_iso(),
            "host": ownership.current_host(),
            "watchers": watcher_total,
            "cron": cron_total,
            "ownership": "unavailable" if ownership_degraded else "ok",
            "crontab": "stale" if crontab_degraded else "ok",
            "smoketest": smoketest_field,
            "repos": {alias: {k: v for k, v in s.items() if k != "events"}
                      for alias, s in sweeps.items()},
        }
        if not targets:
            payload["reason"] = "no registered repositories"
        paths.atomic_write_text(
            paths.health_beacon_path(),
            json.dumps(payload, indent=2) + "\n",
        )

    summary = {
        "status": "error" if (failed or sweep_errors) else "ok",
        "watchers": watcher_total,
        "cron": cron_total,
        "repos": sorted(sweeps),
        "failed": failed,
        "sweep_errors": sweep_errors,
        "ownership_degraded": ownership_degraded,
        "crontab_degraded": crontab_degraded,
        "smoketest": smoketest_field,
        "events": events,
    }
    if not quiet or summary["status"] != "ok":
        for event in events:
            _err(f"{event['phase']}: {event['message']}")
    if not failed and not sweep_errors:
        _err(f"all healthy: {watcher_total} watchers, {cron_total} cron "
             f"agents across {len(sweeps)} repo(s)")
    print(json.dumps(summary))
    log.stage_end("done", status=summary["status"],
                  duration_s=round(time.time() - started, 1),
                  summary=json.dumps(summary)[:2000])
    return 1 if (failed or sweep_errors) else 0


def repair(*, dry_run: bool = False, quiet: bool = False) -> int:
    """Run immediate automatic maintenance or report its mutation plan."""
    if not dry_run:
        return run_host_loop(quiet=quiet)
    targets = _registered_roots()
    actions: list[dict] = []
    lines = current_crontab_lines()
    if lines is None:
        preflight.emit_failure("doctor", "crontab is not accessible")
        return 1
    desired = build_health_cron_lines()
    current = [line for line in lines if health_cron_line_matches(line)]
    if current != desired:
        actions.append({
            "action": "replace-maintenance-schedule",
            "remove": current,
            "add": desired,
        })
    try:
        declarations = plugins.union([root for _, root in targets])
        pending = sorted(
            plugin.name for plugin in declarations.values()
            if not plugins._installed_state(plugin)[0]
        )
    except (OSError, ValueError, plugins.PluginError) as exc:
        preflight.emit_failure("doctor", f"could not plan plugin repair: {exc}")
        return 1
    if pending:
        actions.append({"action": "converge-plugins", "plugins": pending})
    for _, root in targets:
        completed = subprocess.run(
            _self_argv(
                "--json", "internal", "migrate", "--dry-run", root=root),
            capture_output=True, text=True, check=False,
        )
        if completed.returncode != 0:
            preflight.emit_failure(
                "doctor", completed.stderr.strip()
                or f"could not plan trigger migration for {root}")
            return 1
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError:
            preflight.emit_failure(
                "doctor", f"invalid trigger migration plan for {root}")
            return 1
        plan = payload.get("plan", {})
        for kind in ("schedule", "watcher"):
            for selector, change in plan.get(kind, {}).items():
                actions.append({
                    "action": f"rewrite-{kind}",
                    "workspace": str(root),
                    "agent": selector,
                    "remove": change[0],
                    "add": change[1],
                })
        for selector in plan.get("missing", []):
            actions.append({
                "action": "prune-orphaned-trigger",
                "workspace": str(root),
                "agent": selector,
            })
        completed = subprocess.run(
            _self_argv(
                "internal", "maintain", "--sweep", "--dry-run", root=root),
            capture_output=True, text=True, check=False,
        )
        if completed.returncode != 0:
            preflight.emit_failure(
                "doctor", completed.stderr.strip()
                or f"could not plan workspace repairs for {root}")
            return 1
        try:
            workspace_actions = json.loads(completed.stdout)["actions"]
        except (json.JSONDecodeError, KeyError, TypeError):
            preflight.emit_failure(
                "doctor", f"invalid workspace repair plan for {root}")
            return 1
        for action in workspace_actions:
            actions.append({**action, "workspace": str(root)})

    if targets:
        default = repos.default_root() or targets[0][1]
        runtime = _resolve_smoketest_runtime()
        previous = _load_previous_beacon().get("smoketest", {})
        head = _git_head(default)
        if runtime is not None and head is not None:
            fingerprint = smoketest_source_fingerprint(default)
            if not (
                isinstance(previous, dict)
                and previous.get("sha")
                and previous.get("status") == "pass"
                and previous.get("source_fingerprint") == fingerprint
            ):
                actions.append({
                    "action": "run-smoketest",
                    "workspace": str(default),
                    "runtime": runtime,
                })
    unique_actions = []
    seen_actions: set[tuple[object, object, object]] = set()
    for action in actions:
        key = (
            action.get("action"),
            action.get("workspace"),
            action.get("agent"),
        )
        if key in seen_actions:
            continue
        seen_actions.add(key)
        unique_actions.append(action)
    print(json.dumps({
        "status": "planned",
        "targets": [str(root) for _, root in targets],
        "actions": unique_actions,
    }, indent=2))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="agents-live internal maintain",
        description="Run the built-in host check-and-repair loop.")
    parser.add_argument("--quiet", action="store_true",
                        help="suppress progress output")
    parser.add_argument("--sweep", action="store_true",
                        help=argparse.SUPPRESS)  # internal per-repo mode
    parser.add_argument("--dry-run", action="store_true",
                        help=argparse.SUPPRESS)
    args = parser.parse_args()

    if args.dry_run and not args.sweep:
        parser.error("--dry-run requires --sweep")

    if args.sweep:
        if args.dry_run:
            print(json.dumps({"status": "planned", "actions": plan_sweep()}))
            return 0
        # The sweep's stdout contract is exactly one JSON document (the
        # host loop parses it). In-process work can print - notably
        # activate.prune_orphans reporting each pruned entry - so capture
        # everything and forward it to stderr instead.
        buffer = io.StringIO()
        try:
            with contextlib.redirect_stdout(buffer):
                result = sweep()
        except Exception as exc:
            preflight.emit_failure("health-check", f"sweep failed: {exc}")
            return 1
        finally:
            captured = buffer.getvalue().strip()
            if captured:
                print(captured, file=sys.stderr)
        print(json.dumps(result))
        return 1 if result["failed"] else 0
    return run_host_loop(quiet=args.quiet)


if __name__ == "__main__":
    raise SystemExit(main())
