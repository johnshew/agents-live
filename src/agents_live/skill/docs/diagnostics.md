---
title: Diagnostics
description: Diagnose definitions, convergence, dispatch, and WSL liveness
ms.date: 2026-10-09
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

## Run timing and usage

Each invocation records one terminal event using the existing event schema.
`duration_s` is monotonic elapsed time through dispatch and resource cleanup,
including retry waits. `pre_duration_s`, `agent_duration_s`, and
`post_duration_s` measure executed child phases, including version probes,
interpretation, and transcript persistence; they exclude launch preparation
and retry waits. A phase that never executes is null, not zero.

`attempt` counts provider CLI invocations, excluding version probes. The
`attempts` attribute retains each invocation's ordinal, provider, child
duration, status, category when available, usage, and transcript reference.
`model_called` means a provider invocation was attempted, not proof of remote
inference. A failed version probe and an ordinary preprocessor skip both
report false. Ordinary skip still ends the invocation without postprocessing.

Successful preprocessor early finishes are completed runs, not skipped firings:
status is `success` (`ok` in `logs`), with
`completion_reason=preprocessor_skip`. Output, diagnostics, and the control
message are retained together. The terminal summary is capped at 4096
characters; `processor_record` references the full captured work record in the
run directory, retained for the configured retention period. It is a
`pre-processor` event, not a second terminal event or provider transcript.
Processor-authored structured logs and output files remain unchanged.

```bash
agents-live logs --columns run_id,status,completion_reason,model_called,processor_record
agents-live logs --log <processor_record> --columns run_id,phase,status,message --format jsonl
```

Provider usage survives timeouts, nonzero exits, invalid output, retries,
postprocessor failures, and later resource failures when it was available.
Run-level usage adds the same reported counter across attempts exactly once.
If any attempt lacks a counter, its run total is null; available measurements
remain in `attempts`. Do not interpret unknown cost as free execution.
Providers without usage remain unknown. Malformed, negative, and nonfinite
native numeric values are not measurements.

Copilot session checkpoints and shutdown records are cumulative, not additive.
Structured `tokenDetails.input` is retained as `input_tokens`, separately from
`cache_read_input_tokens` and `cache_creation_input_tokens`; output stays
`output_tokens`. The legacy text footer reports cache-inclusive input and
`cached_tokens` as its subset. Do not add those subset counters to input.
No token-price table or inferred invoice charge is used.

Use public log queries rather than opening raw runtime files:

```bash
agents-live logs --slow 30
agents-live logs --columns run_id,phase,status,duration_s,pre_duration_s,agent_duration_s,post_duration_s,attempt,model_called
agents-live logs --columns run_id,usage,attempts
```

Older records without these measurements remain null. This accounting does
not introduce nested spans, tracing exporters, aggregate usage summaries, or
a model-bypass processor signal.

## Locks, clock fires and runtime identity

Every successful cross-process lock acquisition emits an `acquired` and, on
normal release including exception unwinding, a `released` event with
`phase=lock`. `operation`, `run_id`, `agent_name`, `repository`, `lock_kind`,
`lock_id` and `acquisition_id` identify its owner and resource. `lock_wait_s`
measures monotonic time to acquisition; `lock_hold_s` measures acquisition to
release. Both contended and uncontended acquisitions are recorded. Aggregate
only `released` rows: an acquired lock whose process dies has no measured hold
duration. Resource paths are hashed rather than recorded. This covers the
runtime launch gate, agent run and dispatch-budget locks, registry, crontab,
deployment, activation/pause locks, and the public `agents-live lock` wrapper.
Processor lock commands inherit the processor's run and agent identity.
Idle watcher pause checks are atomic read-only owner checks, not acquisitions.
They add no lock events; actual pause operations still log every successful
acquisition. Crashed/reused owners clear logically, while unreadable or
unverifiable pause state reports an error instead of claiming a safe read.

Agent terminal events carry `gate_wait_s`, including failed waits. Clock fires
also carry `planned_at`, `actual_start_at`, `launch_lag_s` and `fire_status`.
The actual start is admission to pipeline execution, after gate, concurrency
and budget checks, not terminal-event time. A skipped fire has no actual start.
Normal clock invocations retain their arrival's due minute through a gate wait.
A non-due arrival still skips; `planned_source=previous-due-minute` labels its
most recent due minute, not proof of which native firing caused the invocation.
`arrival-minute` means the definition was not loaded before refusal. Native
schedulers do not supply their original fire timestamp, so startup delay that
crossed a minute cannot be recovered as an exact launch lag.

Successful convergence records clock schedule intent, including withdrawal,
configuration changes, and repository removal. `clock_fires` is a read-only SQL
view of those expected slots and recorded arrivals/outcomes. `missed` means no
recorded invocation after a 120-second grace period. It does not prove a native
scheduler failed. A received fire is not called missed while waiting or running;
its terminal event replaces the arrival rather than counting the run twice.
Terminal outcomes with no calculable due minute remain visible using
`observed_at`; old outcomes without timing are labeled `unmeasured`.
Reboot-only triggers do not create inferred calendar slots.
Partial convergence closes the known interval until another successful
observation. There is no claim before the first retained observation, after
withdrawal, or outside retained history. Multiple schedules due in one minute
produce one expected agent slot; actual attempts keep their own run IDs.
Queries use the owning host's local scheduler timezone, including DST minutes.
`clock_coverage` reports the retained baseline, omits unknown convergence or
changed-timezone intervals, and disables missed-fire inference when structured
records are damaged. The recorded host-timezone signature must match the reader;
query imported history on its owning host rather than assuming a different local
timezone. This adds no catch-up launches,
scheduler-policy changes or span correlation.
Ordinary log queries do not project clock expectations. Queries referencing
clock views are discovered by the SQL parser, including joins, CTEs and quoted
names. Projection enumerates schedule candidates rather than every historical
minute, preserving missing/repeated DST minutes.

All framework events and pipeline journal entries carry `runtime_version` and
`runtime_generation` from the loaded runtime, never the current selection.
Source and other unmanaged environments explicitly report `unmanaged` as their
generation; historical records without identity remain null.

Each question is one public query; put time filters inside SQL:

```bash
agents-live logs --sql "SELECT operation, lock_kind, max(lock_hold_s) AS max_hold_s, quantile_cont(lock_hold_s, .95) AS p95_hold_s FROM log WHERE phase = 'lock' AND status = 'released' AND ts >= now() - INTERVAL 1 DAY GROUP BY operation, lock_kind ORDER BY max_hold_s DESC"
agents-live logs --sql "SELECT agent_name, max(gate_wait_s) AS max_wait_s, quantile_cont(gate_wait_s, .95) AS p95_wait_s FROM log WHERE phase IN ('done', 'firing') AND ts >= now() - INTERVAL 1 DAY GROUP BY agent_name"
agents-live logs --sql "SELECT agent_name, planned_at, observed_at, actual_start_at, launch_lag_s, fire_status, run_id FROM clock_fires WHERE COALESCE(planned_at, observed_at) >= now() - INTERVAL 1 DAY ORDER BY COALESCE(planned_at, observed_at) DESC"
agents-live logs --sql "SELECT * FROM clock_coverage"
```

The dashboard's **Timing** panel uses the same normalized SQL views for
1-hour, 24-hour and 7-day windows. Its read-only `/api/timing?since=24h`
endpoint accepts relative or ISO `since` and optional `until` bounds and
returns host lock max/p95, agent gate waits, clock outcomes, coverage and
watcher exits/stops.
The default upper bound includes the captured current clock tick, so a just-finished
run is visible even on a coarse-resolution clock. An explicit `until` is exclusive.
The selected window also bounds expected-slot projection, not just the displayed
rows. An observation before the window supplies interval context; grace is
measured against current time, not subtracted from a historical window's end.
Single-repository clock queries never infer missed work for other repositories
whose run history was not loaded. Aggregate dashboards load all registered
repository histories before auditing their clocks.

### Watcher exits and functional health

Detached watcher exit observation survives its launching process and watcher
termination. `phase=watcher`, `status=exited` records `watcher_pid`,
`subscription_id`, `exit_code`, `stderr_tail`, `stderr_bytes`,
`stderr_truncated`, `stderr_complete`, `capture_error`, `stop_reason` and
`operation`. Stderr drains continuously and only its final 8 KiB is retained.
An incomplete stream or persistence failure is explicit, not claimed complete.
The observer's loaded runtime identity is retained independently of activation.
Unexpected exits, including unexplained code zero, also appear in `logs --errors`.
Expected cooperative or convergence stops are informational even when the OS
uses a nonzero termination code. Observer/launch failure uses code `-1` when no
child exit code is available; `capture_error` distinguishes it.

Every convergence stop records `status=stopping` and its reason before attempting
termination. A busy tree records `deferred`; that is not an exit. Exit records
prefer the persisted stop intent, then cooperative activation/replacement or
watch-failure diagnosis. An otherwise unexplained exit, even code zero, is
`unexpected_exit`; no cause is invented. Historical unsupervised processes have
no recoverable exit code or discarded stderr, but their new stop intents are
logged.

Maintenance checks progress written by the actual loop after source startup and
at poll/dispatch boundaries. Polls use a 15-second bound; progress expires after
45 seconds. A newly spawned process gets 30 seconds to initialize. Missing,
stopped or stale progress is not a functioning loop, even with matching argv.
Idle unhealthy owners are replaced, recovered owners are rechecked before stop,
and active-run lock holders remain protected regardless of heartbeat age.
This detects stalled/non-watching owners at the next maintenance pass, not
instantaneously. It does not certify native notification delivery. Killing the
observer itself, losing host power, or losing writable storage can prevent an
exit record; retained partial stderr/state is not an OS exit-code receipt.

```bash
agents-live logs --sql "SELECT ts, agent_name, watcher_pid, status, exit_code, stop_reason, operation, stderr_tail, stderr_bytes, stderr_truncated, stderr_complete, capture_error FROM log WHERE phase = 'watcher' AND status IN ('exited', 'stopping', 'deferred') AND ts >= now() - INTERVAL 1 DAY ORDER BY ts DESC"
```

The **Watcher exits and stops** table in dashboard **Timing** and the `watchers`
array in `/api/timing` expose the same records. Completed observation state
follows host log retention; active observations are not collected.

## Native Windows first run

Install one provider CLI through WinGet. Native provider packages avoid
the `.cmd` and `.ps1` shims that unattended dispatch intentionally refuses:

```powershell
winget install Anthropic.ClaudeCode
# Or: winget install GitHub.Copilot
```

Run the official release bootstrap. It installs uv if needed, verifies the
wheel against GitHub's recorded size and SHA-256 digest, installs it into its
version directory, and exposes the stable command in the current PowerShell
process and future sessions:

```powershell
irm https://github.com/johnshew/agents-live/releases/latest/download/install.ps1 | iex
agents-live --repo C:\path\to\repository init
agents-live --repo C:\path\to\repository doctor
```

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

The bootstrap reports GitHub metadata and asset proxy/TLS failures explicitly
and never falls back to a Python index for Agents Live. Dependencies installed
inside the verified generation still use uv's configured index. If that
dependency request fails with a TLS handshake alert and the Microsoft package
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

### Diagnose a generation upgrade

`agents-live upgrade` builds and validates a complete generation beside the
active one, then switches the stable `current` link. Activation withdraws
triggers and retires idle owned watchers before switching. A watcher tree with
a live run lock is left to finish and record its current fire, then retire or
hand off through the stable launcher. Replacement for that subscription is
deferred until completion; maintenance restores missing still-started watchers.

Activation permits in-flight `run` processes to finish on their original
generation without waiting. Their shared per-agent locks prevent the selected
runtime from launching overlapping work. Active runtime maintenance or a held
launch gate can still refuse activation: retry after the mutation completes.
Do not terminate ordinary agent work to force an upgrade. `versions remove`
and `versions collect` preserve any generation still used by a process and
refuse deletion when host process inventory cannot be verified.

On Windows, refreshing `.claude/skills/agents-live` retries transient access
denied and sharing violations with increasing delays, sharing at most 2.5 seconds
of backoff across the two directory renames. The complete payload is staged
before replacement; a failed promotion restores the previous payload and removes
staging. Restoration and cleanup have separate bounded retry budgets.
If replacement still fails, the error names the affected path and explains that
an open handle or active agent run may be blocking it. Wait for active runs to
finish, close applications holding the path open, then retry. This diagnosis is
a likely cause, not proof of a particular process holding a file. Recovery or
cleanup failures are reported rather than silently ignored.

Use `agents-live versions list` to compare installed and active versions.
If activation selected an unsuitable release, run `agents-live generations
activate VERSION` to select a retained validated generation. Use `generations
remove VERSION` to discard an inactive candidate before rebuilding that exact
version, or `versions collect` to remove older inactive and unheld versions.

A package-manager or checkout command cannot replace itself. If `upgrade`
reports an unsupported installation, run the verified release bootstrap once
and continue through the stable command it prints.

Before any runtime mutation, upgrade also inspects registered repositories and
declared plugin wheels. Retired 5.x definitions, unavailable registered
repositories, missing or modified wheels, and retired plugin entry points stop
the upgrade. Current plugin entry points are installed with the candidate
runtime in an isolated environment and must load with the expected provider or
ownership protocol. Migrate or repair unsafe inputs, then run the command
again.

Self-managed versions remain under the installation root rather than replacing
one environment in place. The complete PEP 440 version is the directory name;
numbered RC versions coexist with other candidates from the same release line.
Historical development versions retain their commit suffix. Selection changes only `current`, then runs
automatic maintenance through the selected command. A dispatch that had already
started can finish on its original immutable version while new dispatches and
converged watchers use the selected version.

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

## Run transcripts

Default `agents-live logs` output includes `run_id` and `has_transcript` so a
recorded conversation is discoverable without querying the log schema. Read
one run by its exact ID, or select recent runs for an agent:

```bash
agents-live logs transcript <run-id>
agents-live logs transcript --agent link-check --last 3 --summary
agents-live logs transcript --agent link-check --since 2h --errors --summary
agents-live logs transcript <run-id> --json
agents-live logs transcript <run-id> --attempts --json
agents-live logs transcript <run-id> --attempt 1 --summary
```

The default rendering shows normalized user and assistant turns plus tool
calls. `--json` returns the same provider-neutral fields in a `transcripts`
array. `--summary` limits the prompt and final text to 6,000 characters each
and lists at most 100 tool names. Use `--raw` with one run ID only when the
normalized view omits provider detail needed for diagnosis.

`--attempt N` selects one invocation, including a timeout before a successful
retry. `--attempts` reports all known attempts with their usage and availability.
With recording enabled, an envelope exists before launch and bounded diagnostic
streams are retained while the child runs. Active or interrupted runs can be read
without a terminal event: `status` remains `unfinished`, and an incomplete
envelope reports `not_yet_finalized`. This state does not claim the process is
still alive. Missing old events are never fabricated.

Pruning replaces an expired provider envelope with a content-free availability
marker for one additional retention period. The query reports `pruned` while
that marker exists; after its expiry, surviving run events report `missing`.
Attempt records separate probe, provider duration, cleanup, parsing and
transcript persistence time. The provider duration includes cleanup; do not
sum the overlapping fields as independent elapsed intervals.

With transcript recording enabled, provider output is saved before
postprocessing. A postprocessor crash or timeout retains the model transcript
and usage even though the run fails. For pipeline runs with a declared result
path, the snapshot handed to the postprocessor is also saved before that step.
Full `--json` output includes `pipeline_result` with `path`, `present`, and
`value`; `present` distinguishes an unpublished value from published JSON null.
Full output also includes `postprocessor_input`, the exact stdin submitted to
the postprocessor, captured before it starts. These proposal fields are omitted
from `--summary`; they may contain sensitive task content and should not be
copied into public issues. Disabling transcript recording disables these
captures too. Missing historical artifacts are not reconstructed.
Processor logs and the pipeline journal
remain subject to normal retention; the journal records operations, not values.

`transcript_state` distinguishes `available`, `no_model_call`, `disabled`,
`missing`, `pruned`, `not_yet_finalized`, `oversized`, `corrupt`, `invalid_path`, and `unknown`. `unknown` is retained for
older records whose null transcript field did not say whether a model ran. A
transcript can become `missing` after retention removes its artifact while an
older archived event remains queryable. `invalid_path` means a log row points
outside the repository's managed run storage; the command refuses to read it.

Transcript paths, envelope fields, and provider output formats are private
runtime details. The normalized CLI fields are the supported retrieval
contract; neither readable nor JSON output exposes the storage path.

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
`empty_output`, `output_parse_error`, `output_schema_rejected`, and
`agent_output_invalid`. `output_schema_rejected` means the provider refused
the declared JSON Schema before running; an output value that fails local
schema validation is `agent_output_invalid`.

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
