---
title: Agents Live Command Reference
description: Installation, lifecycle, validation, and logging commands for agents-live
ms.date: 2026-07-19
ms.topic: reference
---

## Command reference

Detailed reference for agents-live commands. SKILL.md has the dispatch
table and invocation syntax. Load only the section you need.

Agents Live operates the Claude Code and GitHub Copilot agents you already
use. These commands add and manage local triggers around standard agent
definitions; they do not introduce a separate agent format or runtime.

## Contents

- [install -- Installation Instructions](#install----installation-instructions)
- [prereqs -- Check Environment Readiness](#prereqs----check-environment-readiness)
- [smoketest -- End-to-End Validation](#smoketest----end-to-end-validation)
- [create -- Create an Agent](#create----create-an-agent)
- [release -- Audit, Assemble, and Publish](#release----audit-assemble-and-publish)
- [Logging internals](#logging-internals)

## `install` -- Installation Instructions

Run `prereqs` first to identify what is missing, then install accordingly.
This is an **agent-led judgment call, not a deterministic script**: read
`prereqs` output, decide what actually needs installing on *this* host, and
run it interactively. Do not build or invoke a fire-and-forget installer
that runs every fix command unconditionally.

- **Sudo-gated installs** (`apt install cron/jq/inotify-tools`): if
  passwordless sudo isn't available, don't try to guess or prompt for a
  password. Tell the user the exact command and ask them to run it, then
  continue once they confirm.
- **`agency` CLI / MS365 MCPs**: before suggesting install, consider whether
  this host has Microsoft-account/network access at all (e.g. the user has
  said so, or a prior attempt failed reaching `aka.ms`/`microsoft.com`). If
  not, don't retry the install -- confirm with the user that the host is
  intentionally agency-less and skip it. `prereqs.py` already scopes its
   `agency`/`claude`/`copilot` warnings to agents *owned by this host* (per
   `Agents/data/agent-owners.json`), so a WARN here does not necessarily mean
  action is needed.

### Required tools

1. **uv** -- Python package/environment manager. All scripts run via `uv run`.
   ```bash
   curl -LsSf https://astral.sh/uv/install.sh | sh
   ```
   After install, restart your shell or run `source ~/.local/bin/env`.

2. **Python 3.12+** -- the scripts use 3.12-only syntax (PEP 701
   f-strings). The repo-root `.python-version` file pins `3.12`, so
   `uv run` auto-fetches the correct interpreter on first use. If the
   download is blocked or you want it preinstalled:
   ```bash
   uv python install 3.12
   ```
   Do **not** rely on the system `python3` -- it is often 3.10 and will
   fail with `SyntaxError: f-string expression part cannot include a
   backslash`. Always invoke scripts as `uv run --script ...` (never bare
   `python3`) so the pinned interpreter is used and PEP 723 inline
   dependencies are installed. For `-c` one-liners use `uv run python -c`.

3. **cron** -- Scheduler for cron-triggered agents.
   ```bash
   sudo apt install cron
   ```
   Verify with `crontab -l` (should not error).

4. **inotifywait** -- File-watcher for watch-triggered agents.
   ```bash
   sudo apt install inotify-tools
   ```

5. **jq** -- optional; only needed by shell handlers that parse JSON
   (currently just `write-files.sh`). Python handlers need nothing.
   ```bash
   sudo apt install jq
   ```

6. **agency copilot** -- This deployment's usual agent runtime (agents
   only; skip on agency-less hosts). Requires Node.js + npm first.

   a. Install Node.js if missing:
   ```bash
   nvm install --lts
   ```

   b. Install the GitHub Copilot CLI:
   ```bash
   npm i -g @github/copilot
   ```

   c. Install the Agency CLI:
   ```bash
   curl -sSfL https://aka.ms/InstallTool.sh | sh -s agency && exec $SHELL -l
   ```

   d. Complete interactive auth (one-time):
   ```bash
   agency copilot -p "say hello"
   ```

### Optional tools

- **claude CLI**: Only needed for `claude` or `agency claude` agents.
  ```bash
  npm i -g @anthropic-ai/claude-code
  ```

### Workflow

Full bring-up sequence for a new host. Every step is idempotent -- safe to
re-run.

1. **Check prereqs.** `agents-live doctor` -- reports what's missing,
   scoped to the agents this host owns.
2. **Install missing tools.** Use the commands above. Hand sudo-gated
   `apt install` lines to the user; skip host-inapplicable tools (e.g.
   `agency` on an agency-less host).
3. **Re-check prereqs.** Confirm all *required* checks pass. Optional
   agent-CLI WARNs for agents this host doesn't own are expected.
4. **Activate owned agents.** `agents-live start --all` -- installs the
   cron lines and starts the file-watchers for agents this host owns. Safe on
   every machine: it activates only what this host owns.
5. **Bootstrap the health beacon.** `agents-live run
   agents-live-health-check` -- writes `Agents/data/health.ok`. Until
   this runs the beacon is missing and the dashboard reads "unhealthy".
6. **(WSL only) Register one distro-level Windows heartbeat.** Run
   `agents-live heartbeat install --distro "$WSL_DISTRO_NAME"` so cron fires
   when the machine is idle -- see
   [windows-heartbeat.md](windows-heartbeat.md). Without it, cron only runs
   while a WSL session is open.
7. **Verify.** Confirm `health.ok` is fresh with `status: healthy` and no
   warning `events`. On agency hosts the system `smoketest` also validates
   the agent path end-to-end; it is recorded as `skipped` (not `fail`) on
   agency-less hosts.

Keyring/Secret Service setup for agent-CLI logins is a separate one-time
host step -- see `.agents/new-machine-setup.md` ("Credential storage").

---

## `prereqs` -- Check Environment Readiness

Verify that the environment is ready to run agents. Run each check
below and report the results. If any check fails, explain how to fix it.

`doctor` is the CLI name for the same command and adds the read-only
installation checks (§3.4.1, Phase 3): project config present and
parseable (with every declared agent directory and plugin wheel existing),
each declared plugin distribution installed with resolvable entry points,
registry ownership backed by a resolving `agents_live.ownership` provider,
crontab
entries consistent with the agent files (no orphaned `--name` /
`--ensure-watcher` references, no stale script paths - scoped to lines
referencing this repo, since the crontab is host-global), every active
watcher covered by an `@reboot` respawn line (check 13 below), and the
health beacon fresh within 75 minutes - a stale beacon means the
boot/hourly check-and-repair loop itself is not firing, which is
exactly the state no in-band check can report. `doctor`
never mutates host state; `init` closes by running it so a fresh
install ends verified green. Repairs are one step away: `migrate`
converges stale crontab entries, then running the
`agents-live-health-check` agent once re-verifies and refreshes the
beacon.

Before `init`, a markerless `doctor` runs host readiness checks only (including
cron, inotifywait, and agent CLIs) and notes that project checks are skipped.
Run `agents-live init` to create the project config and enable the full set.

`doctor` always performs a fresh PyPI update check and updates the shared cache.
For interactive terminal use, it also displays the result. Other commands check
when the cached result is missing or one hour old. Checks only write under
`$XDG_CACHE_HOME/agents-live/`; they do not change the project or install an
update.

When `doctor` reports that the project skill payload does not match the
installed package, run `agents-live upgrade --skills-only`. Skill refresh
replaces only the managed payload items (`SKILL.md`, `VERSION`, `docs`, and
`templates`) and preserves other files in the skill directory. It is a no-op
when the payload is already current. Use `init` for first-time project layout
and setup.

Bare `agents-live upgrade` needs no project context. It upgrades the uv-managed
runtime in place so receipt-recorded `--with` requirements survive, unions
plugin declarations from the current and every available registered project,
then invokes the newly installed CLI to refresh their skill payloads.
`--repo PATH|ALIAS` constrains payload refresh to one project, while plugin
convergence remains host-global. `--runtime-only` and `--skills-only` run one
phase. Unavailable repositories produce warnings and a nonzero final result
without blocking valid projects.

### Checks to perform

1. **Platform**: Confirm running on Linux under WSL, not native Windows.
   ```bash
   grep -qi microsoft /proc/version 2>/dev/null
   ```
   Fail message: "Not running in WSL. Switch to a WSL terminal."

2. **uv**: Required -- Python package/environment manager for processor dispatch.
   ```bash
   command -v uv
   ```
   Fix: `curl -LsSf https://astral.sh/uv/install.sh | sh`

3. **Python 3.12+ resolvable by uv**: Required -- scripts use 3.12-only
   syntax (PEP 701). The repo-root `.python-version` pins `3.12`; the
   system `python3` (often 3.10) is **not** sufficient.
   ```bash
   uv run python3 -c 'import sys; assert sys.version_info >= (3,12), sys.version; print(sys.version.split()[0])'
   ```
   Fail symptom if mis-resolved: `SyntaxError: f-string expression part
   cannot include a backslash`.
   Fix: `uv python install 3.12` (uv auto-fetches on first `uv run` if
   network allows; this preinstalls it). Ensure `.python-version` exists
   at the repo root.

4. **node / npm**: Required for installing agent CLIs and MCP servers.
   ```bash
   command -v node && command -v npm
   ```
   Fix: install Node.js (e.g. `nvm install --lts`)

   On WSL, also confirm node/npx are **Linux-native**, not the Windows
   interop build under `/mnt/c`:
   ```bash
   command -v node && command -v npx   # neither should start with /mnt/
   ```
   The Windows node writes MSAL tokens to the Windows keychain, so
   `npx ... --login` for the MS365/Graph MCPs never populates the Linux
   `~/.config/ms365-mcp` cache. Fix: `. ~/.nvm/nvm.sh && nvm use node`
   (and ensure `~/.nvm/...` precedes `/mnt/c` in PATH). At agent runtime
   `mcp_config.clean_path()` already pins the nvm node; this check is for
   the manual login flow.

5. **claude CLI**: Required for `claude` and `agency claude` agents.
   ```bash
   command -v claude
   ```
   Fix: `npm i -g @anthropic-ai/claude-code`

6. **copilot CLI**: Required for `copilot` and `agency copilot` agents (optional if only using Claude agents).
   ```bash
   command -v copilot
   ```
   Fix: `npm i -g @github/copilot`

7. **agency CLI**: Required for `agency claude` and `agency copilot` agents (optional if only using bare agents).
   ```bash
   command -v agency
   ```
   Fix (Linux/WSL): `curl -sSfL https://aka.ms/InstallTool.sh | sh -s agency && exec $SHELL -l`
   Fix (Windows): `iex "& { $(irm aka.ms/InstallTool.ps1)} agency"`
   After install, run `agency copilot -p "say hello"` once to complete interactive EntraID auth.

   **Host-scoped, and skippable without Microsoft-account/network access**:
   `agency` requires Microsoft EntraID auth and reaches microsoft.com
   endpoints. Not every host has that access, and multi-machine ownership
   (`Agents/data/agent-owners.json`) already pins `agency`-based agents to
   specific hosts (see [Multi-machine ownership](#multi-machine-agent-ownership)
   below). `prereqs.py` checks agent-owners.json and only warns about a
   missing `claude`/`copilot`/`agency` CLI if *this host* actually owns a
   agent that declares that runtime; otherwise it reports the CLI as
   "not required on this host". Agents with no registry entry and no
   frontmatter `owner:` (e.g. dispatch-only agents that are never
   activated) are reported separately as "needed for unclaimed agents any
   host may run" -- a conservative warning, not a claim of ownership.
   If your host has no Microsoft-account/network
   access and owns no `agency`-based agents, skip installing `agency`
   entirely -- there is nothing for it to do here.

8. **crontab**: Required for scheduled agents.
   ```bash
   command -v crontab
   ```
   Fix: `sudo apt install cron`

9. **jq**: Optional -- only shell handlers that parse JSON with jq
   need it (currently just `write-files.sh`).
   ```bash
   command -v jq
   ```
   Fix: `sudo apt install jq`

10. **inotifywait**: Required for watch-triggered agents (optional for schedule-only agents).
    ```bash
    command -v inotifywait
    ```
    Fix: `sudo apt install inotify-tools`

11. **Processor directory**: `Agents/handlers/` exists.
    ```bash
    test -d Agents/handlers
    ```

12. **Agent directories**: `.claude/agents/` or `.github/agents/` exists.
    ```bash
   test -d .claude/agents || test -d .github/agents
    ```

13. **Watcher reboot intent registered**: Every active watcher should have an
   `@reboot` respawn entry in crontab - the line is the durable intent set,
   so a watcher without one is invisible to reboot restore AND the health
   check's hourly restart loop. `agents-live doctor` compares running
   watcher processes against the respawn lines and reports uncovered ones.
    ```bash
   agents-live doctor
    ```
   Fix: cycle the watcher (`agents-live stop <name>` then
   `agents-live start <name>` - start reinstalls the line)

14. **File watchers running**: At least one `inotifywait` process should be
   alive if agents with `watchPath` are configured.
    ```bash
    pgrep -c -x inotifywait || echo "no watchers running"
    ```
   Use `-x`, not `-f "inotifywait.*"`: a `-f` pattern matches the shell
   wrapper that carries it (any `bash -c` invocation, e.g. from an agent),
   reporting 1 when no watcher is running.
   Fix: `agents-live start --all`

15. **systemd as WSL init (boot-time only)**: The agents-live scripts make
    no systemd calls (watcher debounce is timed in-process), but on WSL the
    `cron` service only auto-starts at boot when systemd is the init system.
    ```bash
    systemctl is-system-running 2>/dev/null
    ```
    Expected output: `running` or `degraded` (degraded is usually fine).
    Fix: Ensure `/etc/wsl.conf` contains `[boot]\nsystemd=true`, then restart
    WSL with `wsl --shutdown` from Windows. Alternatively start cron manually
    (`sudo service cron start`) after each WSL restart.

    **Why it matters**: Without a running `cron` daemon the entire
    agents-live system is inert after every WSL restart.

16. **Windows heartbeat scheduled task** (WSL only): Keeps WSL alive so cron
      fires continuously. Without it, WSL auto-terminates after ~8 seconds of
      idle and all scheduled agents stop. Doctor verifies that the shared XDG
      state `heartbeat.ok` is less than 10 minutes old. When Windows PowerShell
      interop is available, it also verifies that the distro-scoped task is
      enabled, invokes the stable uv CLI shim, and repeats every 5 minutes.
    ```bash
      agents-live doctor
    ```
    If not active, **ask the user**:

    > The Windows heartbeat is not running. This is a Windows Task Scheduler
    > job that pokes WSL every 5 minutes to prevent idle shutdown. Without it,
    > WSL will terminate after a few seconds of inactivity and all cron/watcher
   > agents will stop firing.
    >
    > Would you like to set it up? (See install instructions in
    > `.claude/skills/agents-live/docs/windows-heartbeat.md`)

    Fix: Run `agents-live heartbeat install --distro "$WSL_DISTRO_NAME"`;
    this also migrates legacy repo/package-pinned tasks after verifying the new
    beacon. See [docs/windows-heartbeat.md](windows-heartbeat.md).

### Output format

```
Prerequisites for agents-live (host: <hostname>):
  [PASS] Platform: WSL (Linux 6.6.x-microsoft-standard-WSL2)
  [PASS] uv: /home/user/.local/bin/uv
  [PASS] Python 3.12+ via uv: 3.12.13 (.python-version pins 3.12)
  [PASS] node: /usr/bin/node (v20.x)
  [PASS] claude CLI: /home/user/.local/bin/claude
   [WARN] copilot CLI: not found (needed for agents owned by this host: exercise-judgment)
   [WARN] agency CLI: not found (not required on this host; no owned agents use agency)
  [PASS] crontab: /usr/bin/crontab
  [PASS] jq: /usr/bin/jq
  [PASS] inotifywait: /usr/bin/inotifywait
  [PASS] Processors: Agents/handlers/
   [PASS] Agents: .claude/agents/, .github/agents/ (3 live agents)
   [PASS] crontab entries match agent files: no orphaned or stale entries
  [PASS] File watchers: 11 inotifywait processes running
  [PASS] cron daemon: running
  [PASS] Windows heartbeat: active (last 3 min ago)

  Ready to go (2 warnings).
```

Agent-CLI warnings (`claude`/`copilot`/`agency`) are host-scoped: `prereqs.py`
cross-references `Agents/data/agent-owners.json` and only names a CLI as
"needed" if an agent owned by *this* host declares that runtime. A host with
no Microsoft-account/network access and no owned `agency` agents can safely
ignore an `agency` WARN.

---

## `smoketest` -- End-to-End Validation

Validates the full chain: create -> frontmatter -> status -> agent CLI -> JSON
output -> post-processor -> file write -> watcher detect -> agent CLI -> confirm -> teardown.

**Requires unsandboxed execution.** The smoketest uses inotifywait (kernel
inotify) and agent CLI network calls, both blocked in the VS Code sandbox.
Always run with `requestUnsandboxedExecution: true` or in a regular terminal.

Before running, verify readiness with `doctor`. If the user asks to run the
smoketest directly, run `doctor` first and stop if any required check fails.

```bash
agents-live smoketest
```

The full 13-step chain is the default. A host-local advisory lock prevents
manual and health-check runs from sharing the fixed `_smoketest-*` resources.
A contender exits with status 75 (`BUSY`) without changing resources or the
persisted verdict; the health check preserves the previous verdict and records
an informational event. The lock owner cleans stale resources before setup and
again in a signal-shielded `finally` block. SIGTERM, SIGHUP, and Ctrl+C produce
an `INTERRUPTED` verdict and clean immediately. SIGKILL and host failure release
the kernel lock automatically; the next invocation's mandatory pre-clean
removes any residue before testing. Cleanup operations are time-bounded, and
active `run.py` descendants are terminated with PID-reuse protection before
runtime registrations and files are removed. The verdict fails only on
verified residue (a resource that survived cleanup); a cleanup command that
errors or times out is a logged diagnostic and never fails an
otherwise-passing run.

The script will:
1. Create a test scheduled agent (`_smoketest-cron`) with frontmatter
2. Create a test watcher agent (`_smoketest-watcher`) with frontmatter
3. Verify `status.py --json` reads frontmatter correctly for both agents
4. Start the watcher
5. Run the scheduled agent via its CLI, validate JSON + post-processor output
6. Verify watcher detects the file change, run watcher agent
7. Confirm all outputs (files, logs, status table)
8. Pre-processor → post-processor pipeline (runtime: none)
9. Pre-processor skip gating
10. `mode: pipeline` routes the agent through `PipelineMcp` and verifies
    liveness via `put` + `get` on `/ping`
11. Spawn module (detached dispatch)
12. Debounced watcher dispatch (in-process quiet window)
13. Tear down all test agents, verify cleanup

---

## `create` -- Create an Agent

Create a standard Claude Code or GitHub Copilot agent definition, then add the
Agents Live frontmatter that makes it runnable on a schedule or file trigger.
Parse the user's description to extract:

- **name** -- slugified from the agent title (e.g. "AI News Digest" -> `ai-news-digest`)
- **runtime** -- required; one of: `claude`, `copilot`, `none`, or a plugin-registered adapter (this deployment: `agency claude`, `agency copilot`)
- **mode** -- `plan` (read-only, preferred), `pipeline`, or `write`
   (Agents Live-managed PipelineMcp side-channel; see
  [approach.md](approach.md#execution-modes))
- **post-processor** -- post-processor script name, or omit for log-only (default: `write-files.sh` if using JSON output)
- **mcps** -- MCP server names for agency agents
- **schedule** -- cron expression (for cron type)
- **watchPath** -- repo-relative directory to monitor (for watcher type)
- **outputDirectory** -- where the post-processor should write files (used in prompt generation)

### Steps

1. Ensure logs directory exists:
   ```bash
   mkdir -p "Agents/logs"
   ```

2. Generate the agent file in `.claude/agents/` or `.github/agents/` with YAML
   frontmatter and agent instructions.
   Include frontmatter with all config fields. If using a post-processor, include the
   JSON output section in the prompt body:
   ```markdown
   ---
   runtime: agency copilot
   mode: plan
   post-processor: write-files.sh
   schedule: "0 9 * * *"
   ---

   # Agent Title

   Instructions for the agent...

   ## Output
   Respond with ONLY a JSON object, no other text:
   {
     "files": [
       { "path": "<outputDirectory>/filename.md", "content": "file content" }
     ],
     "summary": "one-line description of what was produced"
   }
   ```

3. Show the user what was created and suggest next steps:
   ```
    Created agent "<name>":
       prompt:   .claude/agents/<name>.md
     post-processor:  Agents/handlers/<post-processor>
     schedule: <schedule or watchPath>
     agent:    <agent> (<mode> mode)

   Edit the prompt, then: /agents-live run <name>
   ```

---

## `release` -- Audit, Assemble, and Publish

Package the agents-live system as a standalone, shareable release.

### Steps

1. **Changelog readiness** -- run `/changelog-maintenance`, cover every commit
   since the latest tag, complete issue hygiene, and select the recommended
   semantic version bump. Commit any changelog update before continuing.

2. **Pre-release audit** -- scan for personal data, secrets, and portability issues:
   ```bash
   uv run --script .claude/skills/agents-live/scripts/pre-release-audit.py
   ```
   All checks must pass before proceeding.

3. **Assemble** -- copy release-included files into a clean directory:
   ```bash
   bash .claude/skills/agents-live/scripts/assemble-release.sh [output-dir]
   ```
   Default output: `/tmp/agents-live`.

4. **Verify** -- audit and test from the assembled directory:
   ```bash
   cd /tmp/agents-live
   uv run tools/pre-release-audit.py
   uv run --with-editable . python -m unittest tests.test_smoke
   ```

5. **Publish** -- from the assembled public release repository, preview and
   run the guarded release workflow:

   ```bash
   uv run --script tools/release.py --dry-run --bump patch
   uv run --script tools/release.py --prepare --bump patch --yes
   # Inspect dist/ and the local release commit.
   uv run --script tools/release.py --publish --yes
   ```

   Replace `patch` with the bump recommended by changelog maintenance. The
   script rejects an empty `Unreleased` section or undersized bump, validates
   synchronized `main`, updates every version surface, and reruns the release
   gates. Preparation creates the commit and local tag; publication verifies
   that exact state, reruns the gates, pushes atomically, and creates the
   GitHub release that triggers PyPI publishing.

Full details: [release-process.md](release-process.md)

---

## CLI usage

Quick reference for the installed command:

```bash
# Lifecycle
agents-live run my-agent             # test it
agents-live start my-agent           # start cron or watcher
agents-live start --all --dry-run    # preview what would activate (no mutations)
agents-live stop my-agent            # remove triggers, keep agent definition
agents-live teardown my-agent        # alias for stop

# Multi-machine ownership
agents-live start my-agent --transfer-to laptop  # transfer to laptop (does NOT activate locally)
agents-live start my-agent --yes                 # take over from another host (no prompt)

# Info
agents-live status                   # human-readable table
agents-live status --json            # all agents as JSON
agents-live status --json my-agent   # single agent as JSON
agents-live status --all-repos       # repo-qualified, read-only aggregate
agents-live doctor --all-repos       # host once + each registered project
agents-live dashboard --all-repos    # read-only repository selector

# User repository registry
agents-live repos add ~/repos/<target-project>        # registered under its directory name
agents-live repos list
agents-live repos default ~/repos/<target-project>    # registers the path if needed
agents-live --repo <target-project> status
agents-live repos remove ~/repos/<target-project>

# Smoketest (validates full chain for each agent)
agents-live smoketest --runtime claude
agents-live smoketest --runtime copilot
agents-live smoketest --runtime "agency copilot"
```

### Repository selection

The user registry is
`$XDG_CONFIG_HOME/agents-live/config.toml`; when `XDG_CONFIG_HOME` is unset,
the platform-neutral fallback is `~/.config/agents-live/config.toml`.
`repos add` requires an existing directory and stores its normalized absolute
path under the directory's name. `repos default` accepts either a registered
name or an existing path, registering that path first when needed. `repos
remove` accepts either the registered path or name. Duplicate registrations,
malformed configuration, unavailable paths, and removing the current default
fail with an actionable error.

Targets resolve in this order:

1. explicit `--repo` path or registered name;
2. `AGENTS_LIVE_REPO` process/session override;
3. nearest `.agents-live.toml` or `[tool.agents-live]` marker;
4. markerless-git adoption for interactive `run` and `start`;
5. configured default repository.

A default never redirects a command away from a local marker. Mutating commands
print the selected path when they use the default. Cron, watcher, and spawned
commands continue to persist an absolute `--repo` path.

`status --all-repos` and `doctor --all-repos` isolate each repository in a
child process, qualify agent identities as `repo/agent`, and report an invalid
repository without hiding healthy peers. Aggregate doctor runs host checks once
and project checks once per valid repository; its exit status is nonzero when a
required host/project check or repository validation fails. The aggregate
dashboard can filter registered repositories but exposes no actions. Select one
repository explicitly to run, start, stop, teardown, migrate, claim, or repair.

`agent_directories` remains a within-repository discovery setting. Entries must
be relative and may not escape through `..` or symlinks; register another
repository instead of pointing this setting outside the selected root.

### Project-declared plugins

Plugin wheels are project configuration, not an installation runbook:

```toml
# .agents-live.toml
[plugins]
example-plugin = { path = "Agents/plugins/example-plugin-1.0.0-py3-none-any.whl", sha256 = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef" }
```

Each key is the wheel's distribution name. `path` must name an existing
repository-relative `.whl` that does not escape the project, including through
a symlink. `sha256` is optional; when present it must be 64 hexadecimal
characters and is verified before installation. For pyproject configuration,
use `[tool.agents-live.plugins]`.

`init` and a non-dry-run `start` converge the selected project's declarations.
`upgrade` unions declarations across registered repositories because the uv
tool environment is host-global. Existing receipt-recorded co-installed
requirements and the primary agents-live install source are preserved, so
convergence does not turn an editable or pinned install into a PyPI install.
`repos add` never changes the environment; it
reports missing declarations as pending for `init`, `start`, or `upgrade`.
`doctor` is read-only and makes a missing distribution, broken agents-live
entry point, integrity mismatch, or declared registry mode without a resolving
ownership backend a required failure. Its repair command is
`agents-live upgrade`.

### Multi-machine agent ownership

Ownership has two explicit modes. With no `ownership` declaration in
`.agents-live.toml` or `[tool.agents-live]`, the project is local by
definition: every agent is owned by this host and transfers are unavailable.
This is the only mode the core package ships: registry mode requires a
project-declared ownership-backend plugin, and declaring
`ownership = "registry"` without one installed
makes dispatch and ownership mutation abstain rather than fall back to
local mode.

Setting `ownership = "registry"` (with a backend installed) enables
`Agents/data/agent-owners.json`.
Registry values are `"*"` (run everywhere) or a hostname matching
`hostname -s`; optional `owner:` frontmatter seeds a missing entry during
activation. An agent with no registry entry AND no frontmatter `owner:` is
claimed for the current host only by a targeted `activate --name` -
`--all` skips it with a note (2026-07-14: a dashboard health sweep
adopting the dormant email-audit agent showed the fallthrough violated
the "ownership never changes implicitly" contract below).

- `activate --all` is safe on every machine: each host activates only
   the agents it owns and silently skips the rest.
- An explicit start on a non-owning host refuses and prints the
   `--transfer-to` command. Ownership never changes implicitly.
- `--transfer-to <host>` changes the registry owner without activating
   locally. Transferring to the current host and starting are separate
   operations.
- Before each dispatch, the runner calls `ownership.load_owners()`,
  which refreshes the registry through the installed backend (the
  git-backed backend pulls from origin, rate-limited 60s, non-blocking
  against `Agents/data/git-sync.lock`, fail-open on network errors)
   and re-reads disk fresh. A missing or malformed registry in declared
   registry mode raises `OwnershipUnavailableError`, so dispatch and
   ownership mutation abstain rather than silently reverting to local mode.
   Dispatch is skipped when this host is no longer the owner. Transfers
   happen via `ownership.set_owner()`, which the git-backed backend
  persists with a commit and a detached background `git push`
  so the new owner sees the change within seconds.
- A scheduled health-check agent makes a good backstop: this
   deployment runs one that deactivates agents owned by another host
   every cron cycle (≤1h).

---

## CLI flags and cron reference

### Flag mapping (implemented in `headless.py`)

| Flag | `claude` | `copilot` |
|------|----------|-----------|
| Headless prompt | `-p "..."` | `-p "..."` |
| Plan mode | `--permission-mode default --allowedTools Read Glob Grep` (headless auto-denies all other tools; mode-compatible `allow-tools` may narrow the allowlist). NOT `--permission-mode plan` -- see key-learnings.md | `--deny-tool shell --deny-tool write --autopilot`; `allow-tools` cannot restore mutation tools |
| Write mode | `--dangerously-skip-permissions` | `--allow-all-tools --autopilot` |
| No questions | *(implicit with -p)* | `--no-ask-user` |
| Custom instructions | repo defaults | `--no-custom-instructions` in agents-live runtime |
| MCP injection | N/A | N/A (agency: `--mcp <name>`) |

Plugin-registered adapters (e.g. `agency claude`, `agency copilot`) use the
same flags as their base CLI, plus adapter-specific extras such as `--mcp`.

Copilot-based agents pass the full prompt body via `-p` and disable
custom instruction loading with `--no-custom-instructions`. This keeps
background runs self-contained and avoids extra AGENTS/agent-file reads that can
trigger Copilot conversation-history mismatches during unattended execution.

### Cron line examples

Installed package (what `agents-live start` writes):

```bash
# plan + handler (logging is handled internally by the runner via JSONL)
0 0 */3 * * cd /repo && /home/you/.local/bin/agents-live --repo /repo run --name ai-news-digest --quiet 2>&1

# plan + log only
0 9 * * * cd /repo && /home/you/.local/bin/agents-live --repo /repo run --name my-agent --quiet 2>&1
```

Activation writes the absolute path to the `agents-live` shim and an
explicit `--repo` into each cron line, so the entry is self-contained:
nothing at fire time depends on the crontab `PATH=` prefix or the
working directory resolving the right project.

Source-checkout deployments (pre-packaging) instead persist the script
form, retired by `migrate` at cutover:

```bash
0 9 * * * cd /repo && /home/you/.local/bin/uv run --script .claude/skills/agents-live/scripts/run.py --name my-agent --quiet 2>&1
```

### Common cron expressions

| Schedule | Expression |
|----------|-----------|
| Every hour | `0 * * * *` |
| Every 5 minutes | `*/5 * * * *` |
| Daily at 9 AM | `0 9 * * *` |
| Every 3 days | `0 0 */3 * *` |
| Every Monday at 8 AM | `0 8 * * 1` |
| 1st of month | `0 0 1 * *` |

---

## Script architecture

All Python entrypoints import `headless.py` for agent invocation. No script
hard-codes CLI flags -- all flag mapping is in one place.

### headless.py exports

| Function | Purpose |
|----------|---------|
| `load_agent_config(name)` | Load an agent definition into a typed `AgentConfig` |
| `AgentConfig.trigger_type` | Returns `cron`, `watcher`, or `multi` from frontmatter |
| `AgentConfig.transcript_log` | Path to transcript file: `Agents/logs/<name>-transcript.md` |
| `list_agents` | Lists all discovered agent names from configured and native agent directories |
| `_build_runtime_flags(runtime, mode)` | CLI flags for runtime and execution mode |
| `_build_agent_command(config, prompt)` | Full command arguments (incl. `--share` when transcript enabled) |
| `resolve_agent_command(name)` | Full resolved command from prompt frontmatter |
| `headless_agent(config, prompt)` | Run headlessly, return `AgentResult` (incl. `transcript_path`) |

### Log schema

Each JSONL log entry is a flat JSON object. Key fields:

| Field | Type | When |
|-------|------|------|
| `ts` | string | Always -- ISO 8601 UTC timestamp |
| `run_id` | string | All events emitted during one `run.py` execution; absent on historical and lifecycle-only rows |
| `event_id` | string | Every schema-v4-and-later event -- unique physical JSONL row identifier |
| `agent_name` | string | Always -- Agents Live agent name |
| `phase` | string | Always -- `start`, `pre-processor`, `agent`, `post-processor`, `done`, `output` |
| `level` | string | Always -- `info`, `warning`, `error` |
| `status` | string | On `phase: done` -- `ok`, `error`, `skipped` |
| `message` | string | Most entries -- human-readable description |
| `output` | string | On agent/pre-processor phases -- up to `MAX_LOG_FIELD_LENGTH` chars |
| `error_category` | string | On errors -- `timeout`, `cli_crash`, `output_parse_error`, `pre_processor_crash`, `handler_crash`, `agent_error` |
| `traceback` | string \| null | On errors with stderr -- last Python traceback extracted from stderr, or null |
| `transcript_path` | string | On agent phase -- path to transcript file (when available) |
| `model` | string | On agent phase -- model used |
| `tokens_in` | string | On agent phase -- input token count |
| `tokens_out` | string | On agent phase -- output token count |
| `tokens_cached` | string | On agent phase -- cached token count |
| `premium_requests` | string | On agent phase -- copilot premium request count |
| `cost_usd` | string | On agent phase -- claude cost in USD |
| `structured_output` | object \| null | On agent phase -- parsed JSON when `extract_first_json_value()` succeeds |
| `duration_s` | float | On `phase: done` -- total run duration in seconds |
| `trigger` | string | On `phase: start` -- `cron`, `file-change`, `manual` |
| `log_schema` | int | Always -- schema version (current: 5) |

### Schema evolution

Every new log entry carries a `log_schema` integer. `qlog.py` exposes a
normalized query view across live JSONL and archived Parquet, using typed
casts where a current field can have multiple physical representations. The
new project starts at schema v5, so readers do not carry transition logic for
pre-v5 logs.

**Procedure for schema changes:**

1. Update all writers and readers to the new schema in one development change.
2. Cut over the project before it has retained logs. Do not ship compatibility
   or live data-migration code.
3. Record the cutover in the version table below.
4. Update the schema table above and the changelog.
5. Deploy once, restart persistent writers, and run
   `qlog.py --all --check-schema` against the resulting store.

**Version history:**

| Version | Date | Migration |
|---------|------|-----------|
| 1 | (implicit) | Original schema. No `log_schema` field. |
| 2 | 2026-04-26 | Added `task` to all entries. Normalized `ts` to ISO 8601 with `T` separator and `Z` suffix. Coerced `duration_s` to float, `changed_files` to list, `skip` to bool. Expanded token abbreviations (`1.1k` -> `1100`). Dropped null-valued keys. Added `log_schema: 2`. |
| 3 | 2026-05-09 | Repaired type drift that broke cross-log queries. Parsed stringified arrays in `changed_files` / `skipped_files` into real lists, coerced phase durations to floats, coerced exercise and tracking counts to integers, and consistently wrote `log_schema` as an integer. |
| 4 | 2026-07-10 | Added millisecond ISO timestamps, unique `event_id` values, and a shared `run_id` across each `run.py` execution, including PipelineMcp events. Added typed numeric normalization and `--check-schema` to the `qlog.py` live-plus-archive view. Historical rows keep null identifiers. |
| 5 | 2026-07-15 | New-project baseline. Agent identity is `agent_name`; all writers and readers use the schema directly. |

Verify the deployed query contract after each clean cutover:

```bash
agents-live logs --all --check-schema
```

---

## Logging internals

All JSONL logging goes through `log_event()` in `headless.py`. Key
behaviours:

- **Field validation** -- string fields > 20,000 chars are truncated;
  `_truncated: true` is added. Non-serialisable values fall back to
  `str()`; if that fails, a `phase: log_event` error entry is written.
  Caller-supplied `ts` is silently dropped (always generated).
- **`agent_name` field** -- `run.py` uses run-scoped `EventLog` instances that
   inject `agent_name=config.name` into every per-agent log entry.
  Entries are self-describing.
- **`structured_output`** -- when the agent produces parseable JSON,
  `normalize_agent_output()` carries the parsed object through
  `AgentResult.structured_output` and it's written to the JSONL log.
   No agent definition changes needed.
- **`error_category`** -- all error paths (agent, handler, pre-processor)
  include an `error_category` field: `timeout`, `cli_crash`,
  `output_parse_error`, `pre_processor_crash`, `handler_crash`,
  `agent_error`.
- **`traceback`** -- error entries with stderr include the last Python
  traceback in a dedicated field via `_extract_traceback()`.

Full schema and querying docs: [approach.md section 4](approach.md#4-logging).
