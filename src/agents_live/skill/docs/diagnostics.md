---
title: Diagnostics
description: Diagnose definitions, convergence, dispatch, and WSL liveness
ms.date: 2026-08-23
ms.topic: troubleshooting
---

Start with read-only commands:

```bash
agents-live status --all-repos
agents-live doctor --all-repos
agents-live logs timeline --all
```

When a PEP 723 pre-processor or post-processor crashes,
dispatch automatically fresh-resolves only that processor and its literal
script children, then appends one of two diagnoses to the recorded failure:
resolution itself failed, or resolution succeeded and the processor failed
afterward (including likely import/API incompatibility). The processor is
never executed a second time.

Use `agents-live doctor --repair --dry-run` to preview the one convergence diff
and `agents-live doctor --repair` to apply it.

## Native Windows first run

Install one provider CLI and uv through WinGet. Native provider packages avoid
the `.cmd` and `.ps1` shims that unattended dispatch intentionally refuses:

```powershell
winget install Anthropic.ClaudeCode
# Or: winget install GitHub.Copilot
winget install --id=astral-sh.uv -e
```

Open a new PowerShell session after WinGet changes PATH. Install Agents Live,
update PATH for future shells, and invoke the installed executable by its
absolute uv bin path in the current shell:

```powershell
uv tool install agents-live
uv tool update-shell
$agentsLive = Join-Path (uv tool dir --bin) "agents-live.exe"
& $agentsLive --repo C:\path\to\repository init
& $agentsLive --repo C:\path\to\repository doctor
```

Follow the `$PROFILE` completion line printed by `init`, then open another
PowerShell session. Agents Live never edits PowerShell profiles automatically.
If `doctor` reports that only shims answer for a declared provider, install the
native CLI shown in its remediation and rerun doctor.

## Microsoft-managed package source

On a Microsoft-managed, domain-joined host, direct TLS negotiation with
`files.pythonhosted.org` may be rejected by network policy. Confirm that path
before changing uv. On native Windows, use PowerShell:

```powershell
Invoke-WebRequest "https://files.pythonhosted.org" -Method Head
```

Inside WSL, test the WSL network path separately:

```bash
curl --head https://files.pythonhosted.org
```

If the request fails with a TLS handshake alert and the Microsoft package
proxy is available, configure uv through its user-level `uv.toml`. Use
`%APPDATA%\uv\uv.toml` on native Windows and `~/.config/uv/uv.toml` inside
WSL. Windows and WSL are separate runtimes; configure each one independently.
Use this content in both files:

```toml
keyring-provider = "subprocess"

[[index]]
url = "https://packagefeedproxy.microsoft.io/pypi/simple/"
default = true
```

The file-based setting applies to interactive commands and unattended uv
children. For a one-shell diagnostic before writing the file, set the
equivalent environment variables.

Native Windows PowerShell:

```powershell
$env:UV_DEFAULT_INDEX = "https://packagefeedproxy.microsoft.io/pypi/simple/"
$env:UV_KEYRING_PROVIDER = "subprocess"
```

WSL:

```bash
export UV_DEFAULT_INDEX="https://packagefeedproxy.microsoft.io/pypi/simple/"
export UV_KEYRING_PROVIDER="subprocess"
```

Do not disable TLS validation, add a trusted-host bypass, or add public PyPI
as a fallback on a managed host. Authenticate through the approved keyring
bootstrap when the proxy requests credentials.

The proxy can lag a public release. Before a forced reinstall, verify that it
serves the intended version through an isolated exact-version check:

```bash
uvx --refresh --from "agents-live==<expected-version>" agents-live --version
```

If the expected version is unavailable, wait for proxy synchronization or use
a locally built artifact from the exact release tag. Do not run
`uv tool install --force agents-live` against a lagging proxy; it can replace a
newer working installation with the older mirrored release. Keep public PyPI
verification in the release workflow so the published consumer artifact is
still tested independently of Microsoft infrastructure.

### Diagnose a queued Windows upgrade

An installed native Windows tool cannot replace the interpreter from which
the current command is running. `agents-live upgrade` therefore queues one
external helper for that tool environment, prints its operation ID, and exits.
It does not report the runtime replacement as complete at that point. A second
upgrade for the same environment is refused while that helper is pending.

Installed-tool watchers are not a blocker. The upgrade asks them to finish any
active dispatch and exit at the next idle check without changing started state.
The helper waits for the environment to become free, replaces the runtime, and
runs ordinary convergence to restore every still-started watcher. Managed
dashboards remain a fail-closed blocker because an interactive session cannot
be quiesced and recreated transparently; stop the named dashboard and retry.

Run any Agents Live command after the helper finishes, then query the admin
events:

```powershell
agents-live logs admin --since 30m --all `
	--columns ts,run_id,status,message,exit_code,transcript
```

The quiesce, runtime replacement, plugin convergence, watcher restoration, and
terminal events carry the printed correlation ID in `run_id`.
A failed terminal event includes the helper exit code and the path to a bounded
local transcript. If the helper exits without writing a terminal result, the
next CLI invocation records that condition as an error and releases the
pending slot.

The helper bootstraps a runtime at least as new as the installed version. A
lagging package proxy therefore produces a recorded failure rather than
running older upgrade code or downgrading the tool. Use the local-wheel path
below when the intended release is not yet available from the proxy.

Before any runtime mutation, upgrade also inspects registered repositories and
declared plugin wheels. Retired 5.x definitions, unavailable registered
repositories, missing or modified wheels, and retired plugin entry points stop
the upgrade. Current plugin entry points are installed with the candidate
runtime in an isolated environment and must load with the expected provider or
ownership protocol. Migrate or repair unsafe inputs, then run the command
again.

### Validate a local wheel through the proxy

This check separates the local Agents Live artifact from its dependencies. It
installs Agents Live from a wheel path while uv resolves dependencies through
the configured Microsoft proxy. It does not modify the user-level tool.

Record the source revision before building. A wheel built from an uncommitted
branch still carries the package version from `pyproject.toml`, so a `6.0.4`
version string alone does not prove that it matches the published 6.0.4 tag.

Native Windows PowerShell:

```powershell
Test-Path .\pyproject.toml
git status --short --branch
git rev-parse HEAD

$testRoot = Join-Path $env:TEMP "agents-live-wheel-test"
Remove-Item $testRoot -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Path "$testRoot\dist" -Force | Out-Null

uv build --wheel --out-dir "$testRoot\dist" .
$wheel = Get-ChildItem "$testRoot\dist\agents_live-*.whl" |
		Sort-Object LastWriteTime -Descending |
		Select-Object -First 1
if (-not $wheel) { throw "Agents Live wheel was not built" }

uv venv "$testRoot\venv" --python 3.13
uv pip install --python "$testRoot\venv\Scripts\python.exe" $wheel.FullName
```

WSL:

```bash
test -f ./pyproject.toml
git status --short --branch
git rev-parse HEAD

test_root="$(mktemp -d)"
mkdir -p "$test_root/dist"
uv build --wheel --out-dir "$test_root/dist" .
wheel="$(find "$test_root/dist" -maxdepth 1 -name 'agents_live-*.whl' -print -quit)"
test -n "$wheel"

uv venv "$test_root/venv" --python 3.13
uv pip install --python "$test_root/venv/bin/python" "$wheel"
```

Verify the 6.0 ownership module at its current path. The retired
`agents_live.ownership` path returning `None` is expected; 6.0 moved the
implementation under `agents_live.state` and does not ship compatibility
shims.

Native Windows PowerShell:

```powershell
$python = "$testRoot\venv\Scripts\python.exe"
& $python -c "import importlib.metadata as m; print(m.version('agents-live'))"
& $python -c "import importlib.util as u; print(u.find_spec('agents_live.ownership')); print(u.find_spec('agents_live.state.ownership'))"
```

WSL:

```bash
python="$test_root/venv/bin/python"
"$python" -c "import importlib.metadata as m; print(m.version('agents-live'))"
"$python" -c "import importlib.util as u; print(u.find_spec('agents_live.ownership')); print(u.find_spec('agents_live.state.ownership'))"
```

Test status and dashboard against an explicit temporary repository containing
6.0 definitions. Do not let repository fallback select an older registered
project: that mixes artifact validation with definition migration and plugin
compatibility. Invoke the dashboard through the only console entry point,
`agents-live`; there is no `dashboard.exe`.

Create this temporary layout under the test root. `.agents-live.toml` may be
empty; its presence marks the project root.

```text
project/
|-- .agents-live.toml
`-- Agents/
		`-- wheel-check/
				`-- SKILL.md
```

Use this definition. The status and dashboard checks inspect it but do not
invoke the placeholder provider.

```yaml
---
name: wheel-check
description: Verify the locally built Agents Live wheel.
metadata:
	agents-live.schema-version: "2"
	agents-live.selector: "fake/echo"
	agents-live.schedule: "0 8 * * *"
---

Verify local wheel startup.
```

Run the clean environment's console entry point with that project:

```text
<venv-agents-live> --repo <temporary-6.0-project> status --json
<venv-agents-live> --repo <temporary-6.0-project> dashboard --dev --port 8247
```

After the dashboard reports readiness, query its rendered-data contract from
another shell:

```powershell
Invoke-RestMethod "http://127.0.0.1:8247/api/agents"
```

```bash
curl --fail --silent --show-error http://127.0.0.1:8247/api/agents
```

The core wheel does not install a private ownership or provider plugin. An
isolated core test should report `agents-live-private` as absent. Test a
private plugin separately against its declared entry points. In particular,
an older plugin that imports `agents_live.ownership` or declares the retired
`agents_live.agents` entry-point group is a 5.x plugin and is incompatible
with 6.0; a successful core-wheel test does not make that plugin compatible.

Interpret the result in layers:

* A build failure is a source or build-dependency problem.
* A dependency-resolution failure with a local wheel is a proxy or
	authentication problem.
* A successful isolated status and dashboard check validates the local core
	wheel at the recorded commit.
* Failure only in a registered 5.x repository is a definition migration or
	private-plugin compatibility problem, not evidence that the core wheel
	omitted `agents_live.ownership`.

## Definition failures

The loader reports the exact `SKILL.md` and rejected property. Common causes
are an unquoted metadata value, a directory and `name` mismatch, invalid
selector or trigger syntax, an unsupported schema version, a path that escapes
the skill, or a 5.x flat definition. Use `agents-live migrate --dry-run` before
the one-shot conversion.

An unknown `agents-live.*` key does not block execution. `status --json`
reports it in `unknown_metadata`, and `doctor` reports that it may be a typo or
may require a newer runtime. A newer schema version is different: it may change
the meaning of existing fields, so the runtime refuses it until upgraded.

## Collection failures

An unreadable registry, ownership source, or started-state record causes
convergence to abstain. Repair that input rather than deleting runtime
artifacts manually. A registered repository that cannot be read, and a started
definition that no longer parses, are narrower: convergence cannot compute
what they should own, so it holds their existing artifacts and reports them.
`status` lists a definition it cannot read as `unloadable`, and `doctor` names
the file and the reason. Fix the file, or run `stop` to withdraw it.

## Trigger and watcher drift

`doctor --repair --dry-run` shows install, remove, start, and stop operations.
A changed canonical watch expression changes its fingerprint and restarts only
that watcher. All watchers restart once when moving from the 5.x fingerprint
form to 6.0.

After a runtime upgrade, a watcher finishes any dispatch already in progress,
then notices the installed version at the top of its loop. It stops its old
change source and starts the same marked subscription through the current
launcher. An already idle watcher checks within 60 seconds; no manual
stop/start cycle is required.

Never inspect runtime log files by hand. Use `agents-live logs` and
`agents-live logs timeline`; they correlate versioned event records and
provider transcripts.

## Log and run-output retention

Automatic maintenance applies a 30-day retention period by default. Configure a
repository with a positive whole number of days:

```toml
retention_days = 14
```

The same key may appear under `[tool.agents-live]` in `pyproject.toml`.
Maintenance atomically rotates an append-only `.jsonl` or `.log` file when its
oldest timestamp crosses the boundary. The rotated segment stays queryable
through `agents-live logs` and `agents-live logs timeline`; it is removed only
after its last write also crosses the boundary. Run transcripts, pipeline
journals, and processor control, log, and oversized-output files are removed
after the same period. Active runs carry a process-owned marker and are never
pruned.

Host-scoped logs use the longest configured period among available registered
repositories, or 30 days when none supplies a policy. Repository configuration
therefore cannot shorten another registered repository's host-level history.

## Uninstall outcomes

`agents-live uninstall` stops watchers running from the managed tool
environment before it removes host integration. If a watcher remains alive
after the grace period, uninstall exits nonzero, names the surviving process,
and removes nothing. Stop the named watcher, or use `agents-live stop` for its
definition, then run uninstall again.

Native Windows cannot delete the executable that is running the uninstall.
After host cleanup succeeds, the command queues an external helper, reports
that removal will finish after the command exits, and returns success.
`uv tool list` can continue to show Agents Live briefly while that helper waits
for the tool environment to become idle.

## Dispatch skips

Automatic firings can be skipped because the definition is stopped, a clock
fire is not due, another run of the same agent holds the lock, or the durable
dispatch budget is exhausted. These are successful skip outcomes, not child
failures.

Failure categories include `state_unavailable`, `agent_invalid`, `timeout`,
`cli_crash`, `pre_processor_crash`, `post_processor_crash`,
`empty_output`, `output_parse_error`, and `agent_output_invalid`.

## WSL liveness

There is no public heartbeat command.

```bash
agents-live doctor
agents-live doctor --repair
```

A repair stages a distinct Windows task and requires a fresh beacon before
swapping. If it fails, verify PowerShell interop, the stable uv tool shim,
`wslg.exe`, Task Scheduler policy, and `WSL_DISTRO_NAME` in the interactive
session. The previous working task remains registered after a failed stage.

After a WSL restart, verify and repair the recorded started intent from inside
the distribution:

```bash
systemctl is-active cron
agents-live --repo /path/to/repository status --json
agents-live --repo /path/to/repository doctor --repair
agents-live --repo /path/to/repository logs --errors --since 1h --limit 10
```

`doctor --repair` restores missing or drifted artifacts for definitions already
recorded as started. Do not use `start --all` as repair: that command
deliberately records every executable definition as started, including ones an
operator previously stopped.

When Windows launches a WSL command that depends on Node, a provider CLI, or
another tool initialized by `.bashrc`, use an interactive shell:

```powershell
wsl -d <distribution> --cd /path/to/repository -e bash -ic `
	'agents-live --repo /path/to/repository doctor'
```

A login-only noninteractive shell (`bash -lc`) does not read `.bashrc` and can
produce false missing-tool diagnoses. Commands that inspect only kernel or
system state do not need an interactive shell.

For a WSL restart loop, inspect the current kernel log inside the distribution:

```bash
dmesg --time-format iso 2>/dev/null | grep -E 'p9io|SIGTERM|corrupted' | tail -20
```

Repeated `p9io` failures followed by `SIGTERM` indicate a Windows/Linux 9P
interop failure, not an Agents Live trigger defect. Reduce recurring access to
Windows executables and paths, avoid a Windows credential-helper executable in
scheduled Linux Git work, and remove duplicate MCP server launches before
restarting WSL. Then rerun `doctor --repair` and inspect correlated events with
`agents-live logs timeline` rather than reading runtime files directly.
