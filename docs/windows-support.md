---
title: Native Windows Support Proposal
description: What it would take to run agents-live directly on Windows, using Task Scheduler and Windows file-change notification instead of cron and inotifywait
ms.date: 2026-07-25
ms.topic: concept
---

Status: draft. Feasibility is settled; the open question is whether the
work is worth doing. The design decisions recorded here stand unless
implementation experience overturns them; see the decision log at the end.

Agents Live runs on Linux, with Ubuntu on WSL as the reference setup. The
runtime itself is `uv` plus Python 3.12, which already runs on Windows.
The parts that do not are the two dispatch mechanisms (`crontab` and
`inotifywait`), the process and locking primitives around them, and the
shell idioms embedded in the generated cron lines.

This document asks what a native Windows host would take: what changes,
what stays, how much new code it adds, and where it can be isolated.

## The question in one paragraph

Today a WSL deployment needs a Windows scheduled task whose only job is
to keep the distro alive so cron and the watchers keep running
([heartbeat.py](../src/agents_live/heartbeat.py)). If the runtime ran on
Windows directly, that whole bridge disappears for repositories that
live on `C:`, and file watching becomes reliable for them. The cost is a
second implementation of dispatch, process state, and locking, plus an
ownership model that can say which of the two runtimes on one physical
machine owns an agent.

## Invoking the Copilot CLI on Windows

The Windows Copilot CLI runs headlessly with plain pipes. No
pseudo-terminal, no ConPTY, no new dependency. This holds in a detached
process with no console at all, which is the case a scheduled task
presents, so the runtime spawns the CLI the same way in every context:

```text
<pinned copilot.exe> -p "<prompt>" --autopilot --deny-tool shell
                     --deny-tool write --no-ask-user --no-custom-instructions
```

That is the plan-mode form; write and pipeline modes differ only in the
flags [agent_adapters.py](../src/agents_live/agent_adapters.py) already
builds. Five rules follow from how the CLI behaves, and each one is a bug
the Windows path would otherwise ship:

1. **Pin a fully qualified `copilot.exe`; never resolve by name.** The
   `copilot` on an interactive PATH can be a PowerShell bootstrapper
   rather than the CLI, and VS Code installs exactly such a shim
   (`copilot.ps1` in its extension storage) that can prompt to install or
   update. `CreateProcess` cannot execute a `.ps1`, and a scheduled task
   does not inherit that PATH in any case. This is the concrete failure
   security-model item 4 is written against, and it presents as "Copilot
   does not work headlessly" if it is not ruled out first.
2. **Read both streams.** stdout carries only the model's answer; the
   usage summary, credit cost, and resume identifier go to stderr. This
   is the opposite of the Linux `script -qc` path, where everything
   interleaves into one transcript, so the Windows path needs no
   `COPILOT_NOISE_PREFIXES` filtering on stdout and must not discard
   stderr.
3. **Decode both streams as UTF-8 explicitly.** Capturing through
   Python's text mode without `encoding="utf-8"` falls back to the ANSI
   code page and corrupts the stderr summary.
4. **Do not assume a single output block.** The same prompt yields the
   answer once, twice, or followed by a completion marker depending on
   launch context. Normalization tolerates repetition and trailing
   markers.
5. **Expect LF, not CRLF.** No `\r` stripping is required.

Failure paths are not yet characterized: cancellation and timeout partway
through a run, streaming for long outputs, an expired credential in a
non-interactive context, and logged-off sessions. These belong to the
vertical slice, not to a viability decision.

## What is already portable

| Area | State |
|---|---|
| CLI, config parsing, agent discovery | Portable. TOML, JSON, YAML frontmatter, `pathlib` throughout |
| `run` / `headless` orchestration, modes, pipeline MCP | Mostly portable after process, handler, and executable-resolution paths are abstracted; the Copilot CLI itself needs no PTY on Windows |
| Structured logging, `logs`, `timeline`, `dashboard`, `qlog` | Portable |
| Repo-relative path model | Directionally portable, but Windows needs rooted-path rejection, reparse-point handling, and handle-based identity checks beyond the current resolved-path containment check ([headless.py](../src/agents_live/headless.py)) |
| Changed-file payload | Repo-relative today, but `str(Path)` emits Windows separators; the shared layer must call `as_posix()` explicitly |

## What is not portable

| Dependency | Where | Windows equivalent |
|---|---|---|
| `crontab -l` / `crontab -` | [headless.py](../src/agents_live/headless.py), [activate.py](../src/agents_live/activate.py), [health_check.py](../src/agents_live/health_check.py) | One Task Scheduler task per agent, with a dispatcher that confirms dueness (see Scheduling on Windows) |
| `inotifywait -m -r` | [activate.py](../src/agents_live/activate.py) | `ReadDirectoryChangesW`, or .NET `FileSystemWatcher` |
| `/proc` scan and `ps -eo pid=,args=` | [headless.py](../src/agents_live/headless.py) | PID files plus `OpenProcess`, or WMI `Win32_Process` |
| `os.kill`, `os.killpg`, POSIX signals | [activate.py](../src/agents_live/activate.py), [headless.py](../src/agents_live/headless.py), [health_check.py](../src/agents_live/health_check.py) | Protected stop IPC and a Job Object, then termination of an identity-verified process tree |
| `fcntl.flock`, `fcntl` non-blocking reads | [headless.py](../src/agents_live/headless.py), [activate.py](../src/agents_live/activate.py) | A per-user named mutex with an explicit DACL; overlapped I/O or a reader thread |
| `start_new_session=True` | [activate.py](../src/agents_live/activate.py) | A Job Object and an explicit process group or control channel; detached/no-console flags cannot assume `CTRL_BREAK_EVENT` remains available |
| `sh -c "cd X && PATH=Y agents-live ..."` cron lines | [activate.py](../src/agents_live/activate.py) | A Task Scheduler executable path, one Windows-quoted argument string, and a working directory; Task Scheduler does not store an argument vector |
| `script -qc` PTY for the Copilot CLI | [headless.py](../src/agents_live/headless.py) | Not needed. The Windows CLI writes clean text to a redirected stdout with no console, so plain pipes suffice (see Invoking the Copilot CLI on Windows) |
| `bash` for `.sh` handlers | [headless.py](../src/agents_live/headless.py) | Python and Node handlers already run natively; `.sh` requires Git Bash and is otherwise refused (see Handlers on Windows) |
| `hostname -s` | [ownership.py](../src/agents_live/ownership.py) | `socket.gethostname()` fallback already exists |

## Design principles

1. Ownership is per runtime environment, not per machine. A machine
   running both WSL and Windows presents two independent environments,
   and every owner value names one of them. An agent owned by a specific
   environment activates only there; nothing infers a second one.
2. Paths stay repo-relative POSIX strings everywhere except the syscall
   boundary. `.` in a `watchPath` means the repository root, not the
   filesystem root. Logs, hashes, changed-file payloads, and status
   output keep one spelling on every platform.
3. Windows and WSL never share an on-disk repository. A WSL repository lives
   in the distro's own filesystem; a Windows repository lives on a Windows
   volume, which the distro sees under `/mnt/<drive>`. The Windows runtime
   refuses repositories in the WSL namespace (`\\wsl.localhost`, `\\wsl$`),
   and the WSL runtime retains its current behavior. Cross-runtime file
   watching and coordination are out of scope by deployment rule.
4. Platform code is a leaf. The scheduler, debounce, cascade guard,
   fire-rate breaker, dispatch, ownership, and logging stay single
   implementations that call down into a small interface.
5. The host's native mechanisms dispatch; Agents Live does not keep a clock.
   Scheduling and change notification are surfaced to Task Scheduler and to
   the Windows change-notification API rather than reimplemented on top of a
   resident poller. A `uv`-launched dispatcher is fine when a native trigger
   is what launches it.
6. Preserve the Linux and WSL security posture. Native Windows support adds
   platform mechanics, not a sandbox, policy engine, or new approval system.
7. Deliver a narrow working path before hardening it: Copilot CLI process I/O,
   one scheduled agent, then one watcher. Carry only the few identity and
   quoting checks that prevent acting on the wrong target, and add the rest of
   the lifecycle and failure tests once those paths are demonstrably viable.

## Security model

The developer running Agents Live is an administrator and authorizes agents to
operate on their behalf. Agent definitions, handlers, model tools, and child
processes therefore have the developer's effective authority. Native Windows
support does not claim to isolate an agent from the user, defend against an
administrator, or contain same-user malware. Those goals require operating
system or service isolation beyond this project.

So the thing worth engineering against is not an attacker; it is this tool's
own bugs acting with that authority on the wrong target: deleting a task it
does not own, terminating a recycled PID, mis-splitting an argument string,
or leaving persistence behind after uninstall. A check earns its place when it
prevents a bug class that would otherwise ship, costs almost nothing, and does
not delay the first working agent. Anything that only defends the developer
from the developer is theater here, and is deferred by default.

Built in from the start, because each is cheap and each prevents a real
failure:

1. **Verify argument quoting by round trip.** Task Scheduler stores one
   argument string, so a repository path containing a space or a quote
   silently changes the command that runs. Build the string, parse it back
   with `CommandLineToArgvW`, and refuse on mismatch. Highest value per line
   in this list.
2. **Verify task identity before replacing or deleting.** Register in a
   dedicated `\AgentsLive\` folder with deterministic repository-scoped
   names, and confirm the action and working directory are ours first. This
   is the same check `cron_line_matches` already performs on Linux.
3. **Verify process identity before terminating.** Compare PID, process
   creation time, and expected image name. PID reuse is common; stale state
   killing an unrelated process is the bug this prevents.
4. **Pin a fully qualified executable and an explicit working directory.**
   Required regardless, since a scheduled task does not inherit the
   interactive `PATH`, and it removes the case where a different executable
   answers to the same name.
5. **Pass changed-file payloads through a file in the state directory.** The
   command line is length-limited, so a large batch would truncate or fail;
   that bound, not secrecy, is the reason.
6. **Keep state, logs, and payloads under the user's local application-data
   directory, keep credentials off command lines, and keep existing log
   redaction.** Nothing new, just no regression from current behavior.

Deliberately deferred, with the condition that would bring each back:

- **Custom ACLs on tasks, state, and named kernel objects.** Default per-user
   ACLs already match the trust model. Revisit only if Agents Live ever runs
   as a service or under a different principal than the developer.
- **SIDs, repository IDs, and launch nonces in process identity.** PID plus
   creation time plus image name already answers "is this the process I
   started". Revisit if multi-principal execution appears.
- **Job objects for watcher process trees.** Revisit if orphaned grandchild
   processes show up in practice.
- **Handle-based volume and file identity revalidated before each use.** This
   is TOCTOU hardening for code running at a different privilege level than
   the file's owner. Here the two are the same account. Lexical containment
   plus explicit rejection of unsupported path forms is proportionate.
- **Logged-off execution.** Interactive-token tasks first; stored
   credentials, S4U, mapped drives, and network repositories are separate
   follow-up work.

## The seam

A single module, `hostruntime.py`, with a protocol and two
implementations. Everything platform-specific moves behind it; nothing
else in the tree imports `fcntl`, `signal`, `subprocess` for `crontab`,
or `/proc`.

```python
class HostRuntime(Protocol):
    id: str                      # "linux" | "wsl" | "windows"

    # trigger persistence (replaces crontab reads and writes)
    def install_triggers(self, desired: TriggerSpec) -> TriggerIdentity: ...
    def remove_triggers(self, name: str) -> None: ...
    def installed_agent_names(self, root: Path) -> list[str] | None: ...
    def trigger_state(self, name: str) -> TriggerIdentity | None: ...

    # watchers
    def spawn_detached(
        self, argv: list[str], cwd: Path
    ) -> ProcessIdentity: ...
    def watch_events(self, dirs: list[Path]) -> Iterator[WatchBatch]: ...

    # process state
    def find_process(
        self, name: str, kind: str
    ) -> ProcessIdentity | None: ...
    def is_alive(self, ref: ProcessIdentity) -> bool: ...
    def terminate(
        self, ref: ProcessIdentity, *, force: bool = False
    ) -> None: ...

    # coordination
    def exclusive_lock(
        self, key: str
    ) -> AbstractContextManager[LockLease]: ...
```

The interface stays small, but its results cannot erase provenance or failure
states. `TriggerSpec` is the desired state: the parsed schedule expressions,
watch paths, and repository identity. `TriggerIdentity` is what is actually
registered: the task path, principal, action, working directory, settings, and
ownership marker. `ProcessIdentity` is PID, creation time, and canonical
executable, which together answer whether this is still the process that was
started. `WatchBatch` distinguishes changes, overflow, root invalidation,
degraded rescan, and fatal error. `LockLease` identifies the held kernel
object. Registration and deletion verify ownership rather than blindly
replacing a matching name.

`installed_agent_names` returns `None` for "state
is unreadable here", which the existing sandbox handling in
[status.py](../src/agents_live/status.py) already models. `watch_events`
yields typed batches. The shared loop normalizes accepted changed paths to
repo-relative POSIX strings, bounds each batch, and responds to overflow or
root invalidation before applying debounce, ignore rules, the content-hash
cascade guard, and the fire-rate breaker in
[activate.py](../src/agents_live/activate.py).

`TriggerSpec` carries the parsed schedule expressions and watch paths
rather than a preformatted cron line. That is the important change on
the POSIX side: `build_cron_lines` becomes an implementation detail of
`PosixRuntime` instead of a shared concept, and
[migrate.py](../src/agents_live/migrate.py) converges against the
runtime's canonical form rather than against a string it builds itself.

## File change notification on Windows

Three options.

**A. `ReadDirectoryChangesW` through `ctypes`.** The direct analogue of
inotify: one handle per watched directory, recursive, delivering change
records whose paths are already relative to the watched directory. No
new dependency, cancellable from another thread with `CancelIoEx`, and
it fits a typed `watch_events` contract. The implementation must validate
record offsets and UTF-16 lengths, pair rename records, handle root deletion,
reparse points, cancellation, and `ERROR_NOTIFY_ENUM_DIR`, and report buffer
overflow as a degraded state. Overflow recovery uses a bounded rescan with
backoff rather than an unbounded immediate retry.

**B. The `watchdog` package.** Removes the ctypes work and is
well-tested, at the cost of a runtime dependency and an event model that
is more abstract than the one the current loop expects. Tempting to then
use it on Linux too; that would replace a battle-tested inotifywait path
for no benefit and should not be part of this work.

**C. A PowerShell child process wrapping .NET `FileSystemWatcher`.**
Smallest diff, because the current loop already reads newline-delimited
paths from a child process stdout. Costs a PowerShell start per watcher,
inherits execution-policy and quoting hazards, and adds a script to the
package payload.

Recommendation: prototype A and B behind the typed contract, then choose from
measured reliability and maintenance cost. Do not commit to a line-count
estimate before overflow, cancellation, and rescan tests pass. C trades a
well-understood in-process failure mode for an interop and quoting boundary.

Event storms need explicit limits on queued paths, batch bytes, debounce
memory, file size hashed, rescan frequency, and total rescan work. A batch of
changed files goes to a file in the state directory rather than onto the
command line, which is length-limited.

## Scheduling on Windows

Task Scheduler owns the timing. Agents Live registers one task per agent,
translating that agent's cron expressions into native triggers, and the
task action runs a dispatcher that confirms the agent is due before
running it. Windows decides when to wake; the dispatcher decides whether
this particular wake is a real firing time. No resident process, no
internal clock.

Translation is exact wherever cron maps cleanly onto a native trigger. A
task carries many triggers, so lists and ranges expand instead of
failing:

| Cron form | Native trigger |
|---|---|
| `M H * * *` | Daily at H:M |
| `M H * * D` | Weekly on D at H:M |
| `M H D * *` | Monthly on day D at H:M |
| `*/N * * * *` | Once, repeating every N minutes, indefinite duration |
| `0,30 * * * *` | Two triggers, or Once plus a 30-minute repetition |
| `0 9-17 * * 1-5` | Cross product: 5 days by 9 hours, 45 triggers |
| `@reboot` | At startup |

Where cron does not map exactly, translation produces a superset of the
true firing times, a coarser trigger such as hourly or every fifteen
minutes, and the dispatcher declines the fires that are not due.
Guaranteeing a superset is far easier than guaranteeing exactness, so no
expression is refused and cron stays the single schedule language on
both platforms. The cases that need it are day-of-month and day-of-week
both restricted, which cron ORs and no single native trigger expresses;
step values that do not divide their field evenly, where a native
repetition anchored at registration time would drift off the cron
minute; and expressions whose exact expansion exceeds the trigger cap.

The dispatcher is a predicate, not a scheduler. Given one expression and
one timestamp it answers due or not due. No run queue, no catch-up
policy, no drift compensation: those stay with Task Scheduler, including
its "run as soon as possible after a missed start" behavior for machines
that were asleep. Coarse triggers carry exactly one piece of state, the
last fired minute per agent, so a repetition that fires twice inside one
matching minute still runs the agent once.

Two consequences to document for operators: a coarse trigger shows a
cadence in the Task Scheduler UI that does not match the agent's stated
schedule, and a declined fire costs a process start. Keeping supersets
tight keeps both small.

Rejected: a single per-minute tick task driving an internal scheduler.
It reduces Task Scheduler to a pulse generator and moves time zones,
daylight-saving folds and gaps, clock rollback, sleep and catch-up, and
overlap policy into Agents Live. Less code to write, far more to own,
and it contradicts the principle that the host's native mechanisms
dispatch.

Task Scheduler stores an executable path, one argument string, and a
working directory. Build the argument string with Windows
`CommandLineToArgvW`-compatible quoting and verify it by round trip. Pin
a fully qualified executable rather than a PATH-resolved name, then read
back and compare the registered task definition.

### Watchers are started by Task Scheduler, not scheduled by it

Each file-watch agent keeps one small watcher process, exactly as on
Linux today. A scheduled task with a logon trigger, a startup trigger,
and a repetition interval runs
`agents-live internal ensure-watcher <name>`, which exits immediately
when the watcher is alive and respawns it when it is not. That one task
replaces both the `@reboot` respawn line and the health-check restart.

The watcher stays resident because Windows publishes no native trigger
for "this directory changed". Task Scheduler event triggers read the
Windows event log, and file-system changes are not written there. The
one genuinely process-free alternative, a permanent WMI event consumer
over `CIM_DataFile`, requires administrator registration, polls
expensively, and is a well-known malware persistence pattern that
endpoint protection flags on sight; it is rejected. So the native
mechanism's job is to start the watcher and keep it started, which is
what the ensure-watcher task does.

## Ownership generalization

Owner values today are `"*"` or a short hostname
([ownership.py](../src/agents_live/ownership.py)). One physical machine
can now host two runtime environments, so hostname alone is ambiguous.

Proposal: keep the value a single string and extend its grammar.

| Value | Meaning |
|---|---|
| `*` | Keep the existing meaning: run in every environment that has activated that repository |
| `<host>` | Keep the existing Linux and WSL owner value and matching behavior |
| `windows:<uuid>` | One native Windows runtime registration; status also shows its hostname as a display label |

Native Windows initialization generates a UUID once and stores it in the
user's `%LOCALAPPDATA%\agents-live` state. `current_owner_id()` returns
`windows:<uuid>` on Windows and retains the current hostname behavior on Linux
and WSL. The UUID is stable across repository moves and package upgrades but
is regenerated for a new Windows user profile. The hostname remains a display
label rather than part of Windows matching.

This change does not migrate existing WSL registrations or alter their
security posture. Windows repositories and WSL repositories are physically
separate by deployment rule, so their registries do not describe the same
checkout and require no cross-runtime lease or ownership migration.

## Paths and repository identity

- `repo_state_key` hashes the resolved absolute path
   ([paths.py](../src/agents_live/paths.py)). Windows and WSL repositories are
   physically distinct, so separate state keys and logs are expected. The
   dashboard does not need to correlate two spellings of one checkout.
- The user state home has to resolve somewhere sane on Windows. XDG
  variables are honored first today; `%LOCALAPPDATA%\agents-live` is the
  native answer when they are absent.
- Windows path hazards that need explicit handling include case-insensitive
   ignore matching, long-path support across every child executable, reserved
   device names, alternate data streams, trailing-dot and trailing-space
   aliases, UNC and device namespaces, and drive-relative paths. Reject
   unsupported forms explicitly.
- `watchPath: /` is rooted on both POSIX and Windows. Use `.` for the
   repository root and reject all rooted watch paths.
- `Path.resolve()` plus ancestry comparison is a lexical check, not a complete
   Windows containment boundary, and it is the proportionate one here: the
   watcher and the watched files belong to the same account. Reject
   unsupported path forms and unexpected reparse points explicitly, and treat
   root invalidation as a watcher state rather than something to prevent.

## Handlers on Windows

Handlers are dispatched by file extension: `.py` through
`uv run --with`, `.js` through `node`, anything else through `bash`
([headless.py](../src/agents_live/headless.py)). Python and Node
handlers are mostly portable, so plan mode works on Windows after executable
resolution and process creation are abstracted. TypeScript is currently passed
to `node` directly, which only works when the installed Node.js runtime
supports that file's syntax. Any extension not explicitly recognized currently
falls through to `bash`; Windows support must replace that fallback with an
allowlist and reject unknown extensions.

Decision: fail closed, and say so at three layers, reusing the existing
capability-probe contract in [preflight.py](../src/agents_live/preflight.py)
with a new capability name (`handler_interpreter`) and the existing
`dependency_missing` code.

1. **Activation refuses.** `start` probes the interpreter each agent's
   handler needs and refuses the agent when it is unavailable. Nothing
   gets scheduled that can only ever fail.
2. **Per-run preflight re-probes and logs.** The probe is advisory and
   subject to TOCTOU, so the run path repeats it. On failure it writes a
   structured event (`level=error`, `error_category="dependency_missing"`)
   and exits nonzero. This is the event a health-monitoring agent reads,
   and it fires before the model call, so a run that cannot finish costs
   no tokens.
3. **`doctor` reports standing state.** Which agents declare a shell
   handler, and whether a usable interpreter exists. No `--repair`: the
   fix is converting the handler to Python or installing Git Bash, and
   neither is the tool's to do silently.

The probe cannot simply accept whatever `bash` resolves to. On Windows,
`bash` on PATH is usually `C:\Windows\System32\bash.exe`, the WSL
launcher. That would run the handler inside the distro against Windows
path spellings and a different repository root, and it would appear to
work. Accepting it is worse than refusing. The probe therefore accepts a
Git Bash installation or an interpreter the operator names explicitly in
project config, and rejects the WSL launcher by identity with a message
that explains why.

On Linux the probe always succeeds, so nothing about current behavior
changes.

## Recommended implementation order

1. **Build one direct foreground run.** Make `agents-live run` execute one
   approved agent from a Windows repository without scheduling or watching.
   Resolve process creation, path spelling, handler selection, executable
   pinning, and logs before adding persistence. Agent invocation is already
   settled (see Invoking the Copilot CLI on Windows); this step is the
   orchestration around it, including the failure paths that invocation
   work did not cover.
2. **Add the native Windows runtime ID.** Generate `windows:<uuid>` in the
   Windows user state home and use it for Windows ownership matching. Leave
   Linux and WSL hostname behavior unchanged.
3. **Register one scheduled run.** Use a user-scoped Task Scheduler action with
   a fully qualified executable, Windows-correct arguments, an explicit
   working directory, and enough metadata to identify the task during removal.
4. **Confirm schedule semantics.** Translate a real agent's cron expression
   into native triggers, measure how much of the existing schedule
   vocabulary maps exactly, and confirm DST, sleep, missed-start, and
   restart behavior on a live task before generalizing activation and
   status.
5. **Build one watcher.** Compare `ReadDirectoryChangesW` and `watchdog` on a
   native Windows repository. Prove cancellation, rename, overflow, root
   deletion, and bounded rescan behavior before choosing the implementation.
6. **Extract the host runtime.** Refactor only the abstractions proven by the
   foreground, scheduler, and watcher prototypes. Keep existing Linux and WSL
   behavior unchanged and avoid speculative interface methods.
7. **Complete lifecycle commands.** Add `start`, `status`, `doctor`, `stop`,
   `uninstall`, and `upgrade` parity. Verify task identity before replacement
   or deletion and process identity before forced termination.
8. **Harden failure paths.** Bound watcher queues and payloads, define
   logged-off execution limits, and make interrupted task updates recoverable.
   This is correctness hardening within the trusted-administrator model, not
   sandboxing.
9. **Add Windows CI and regression tests.** Cover argument quoting, task
   identity and collisions, stale PID reuse, rejected path forms, watcher
   overflow, interrupted upgrades, and uninstall cleanup after the
   implementation exists to test.

## Testing on Windows without publishing

Every step above needs a native Windows host, and none of them should
require a PyPI release or disturb a WSL deployment running on the same
machine. The separation this design already assumes makes that
straightforward: the two runtimes have different state homes, different
registries, and physically separate checkouts, so a Windows experiment and
a live WSL deployment coexist on one machine without seeing each other.

### The edit and test loop

Development stays where it is. Edit in the WSL checkout, commit to a
branch, and push. On the Windows host, keep a separate native clone of
this repository under the Windows user profile and `git pull` that branch.
Two checkouts of the same repository, synchronized through the remote.

Do not shortcut that by pointing Windows `uv` at `\\wsl.localhost\...`. It
is the exact boundary the runtime is designed to refuse, and a green
result there would say nothing about a native checkout.

From PowerShell in the Windows clone, the command prefixes from
[testing.md](../.agents/testing.md) apply unchanged:

```powershell
uv run --with-editable . agents-live --repo $HOME\scratch\win-test doctor
uv run --with-editable . agents-live --repo $HOME\scratch\win-test start
```

The boundary table in that runbook still holds: an editable-source pass
does not prove the wheel works, and the wheel does not prove the installed
tool works.

### The target project is always scratch

Never the agents-live clone itself, never a real project. `agents-live
init` into a scratch directory under the Windows profile, then add one
cron agent and one watch agent. Mutating commands carry an explicit
`--repo`. This is the same discipline as Linux testing, and it matters
more here because a mistake registers persistent scheduled tasks rather
than a crontab line.

### Proving the artifact without a release

Build in the Windows clone and run the wheel in an isolated environment:

```powershell
uv build
uvx --from dist\agents_live-<version>-py3-none-any.whl agents-live --help
```

Use `uv tool install --from <wheel> agents-live` only when the installed
tool path is itself under test, and uninstall afterward; it puts a real
`agents-live` on PATH. Never run `agents-live upgrade` on the test host:
it fetches from PyPI and replaces the build being tested.

### Verifying agent invocation needs none of this

Checking that the Copilot CLI still behaves as Invoking the Copilot CLI on
Windows describes takes a PowerShell session on a Windows host with no
agents-live installed, plus a throwaway scheduled task that is unregistered
afterward. It is the cheapest way to re-confirm the foundation after a CLI
upgrade. Everything else in this section applies from the foreground run
onward.

### Cleaning up is the part Linux does not teach

This is the real difference. A stray cron line is visible in `crontab -l`
and disappears with the crontab. A registered task lives in a machine-wide
store, survives reboots, and keeps running whether or not the developer
remembers creating it. Test hygiene has to be explicit:

- Everything Agents Live registers is under `\AgentsLive\`, so one
  enumeration shows the whole footprint:
  `Get-ScheduledTask -TaskPath '\AgentsLive\*'`.
- Tear down with `agents-live stop` and `agents-live uninstall`. The
  enumeration above is how the developer confirms teardown worked, not the
  routine way to remove things.
- End a session by confirming that enumeration is empty and that no
  watcher processes are left running.

Two Windows facilities answer most "it did not run" questions before
anything in the agents-live logs does: `Get-ScheduledTaskInfo` reports last
run time and last result, and the Task Scheduler operational log in Event
Viewer explains why a trigger did not fire. Read those first, then
`agents-live logs`.

### Where the smoke suite fits

[test_smoke.py](../tests/test_smoke.py) runs against temporary projects, so
it should run unchanged on Windows once the host runtime exists. Until
then it exercises POSIX mechanics and is not a Windows gate. The Windows CI
job in the hardening phase runs it alongside the regression checks named
in the security model.

## Size and blast radius

Rough, and stated as a range because the prototypes come before the
refactor that generalizes them:

- Extraction of `HostRuntime` and the POSIX implementation: mostly moved
  code, close to zero net new lines, but it touches `activate.py`,
  `headless.py`, `health_check.py`, `status.py`, `stop.py`,
  `migrate.py`, `uninstall.py`, and `doctor.py`. This is the change that
  needs the most review attention and the one that carries regression
  risk for the platform that already works.
- Windows implementation: 700 to 1,000 new lines, concentrated in one
  or two modules. Task registration and enumeration around 250,
  `ReadDirectoryChangesW` around 250, process and lock primitives around
  150, cron-to-trigger translation and the due predicate around 200.
- Ownership grammar: under 100 lines plus migration.
- Doctor, preflight, and status parity: a few hundred lines spread
  across existing modules, mostly branching on `runtime.id` for
  diagnostics text.
- Tests and CI: comparable in size to the Windows implementation itself.

So the answer to "how much would it bloat the codebase" is that the
platform-specific part is genuinely isolable and roughly one module's
worth of new code. The part that cannot be isolated is the diagnostic
and operator-facing surface, where every check that names `crontab` or
`inotifywait` has to learn a second vocabulary. That is where the
sprawl would come from if it is not designed deliberately.

## Phasing

1. Vertical slice: foreground `run`, one scheduled agent, and one watched
   agent on native Windows.
2. Consolidation: extract the host runtime around the working slice and add
   the Windows runtime UUID without changing Linux or WSL behavior.
3. Productization: complete lifecycle commands, diagnostics, supported
   handlers, schedule semantics, and operator documentation.
4. Hardening: add Windows CI, regression tests for the identity and quoting
   checks, bounded failure handling, and transactional upgrade and uninstall
   behavior.

Each phase has an explicit stop decision. The nearest one belongs to the
vertical slice: if foreground `run`, a registered task, or a watcher cannot
be made to work on a native repository, the proposal narrows or ends there
rather than after a broad refactor. Hardening beyond the checks listed in
the security model follows a working vertical slice rather than attempting
to design isolation that the trusted-administrator model cannot provide.

## Non-goals

- Replacing the Linux implementation with a cross-platform library.
- Sharing runtime state between two runtime environments. Each keeps its
   own state home, logs, watchers, and physically separate repositories.
- Watching files across the WSL boundary in either direction.
- Sandboxing agents from the developer account or administrator. That needs
   operating-system or service isolation outside this project.
- macOS, which stays untested and out of scope here even though the
  POSIX runtime would mostly apply.

## Open questions

- How does the Copilot CLI behave on its failure paths in a
  non-interactive context: cancellation and timeout partway through a
  run, streaming for long outputs, an expired credential, and a
  logged-off session? Answered by the foreground run, not in the
  abstract.
- How much of the real schedule vocabulary translates exactly, and how
  coarse the superset triggers have to be for the rest? Measured on the
  first registered task, not settled in the abstract.

## Decision log

Decisions that changed the approach recorded above. The document states
the current approach; this log says when it changed and why.

### 2026-07-25: no PTY on Windows, and the executable is pinned

The Windows Copilot CLI was exercised headlessly in three launch
contexts, including a fully detached process with no console, and
produced clean output and a zero exit in all of them. ConPTY and
`pywinpty` are therefore out of the design, and the earlier draft's
feasibility gate is closed in favor of the vertical slice.

Two consequences reach beyond Windows. `use_pty` and `filters_tui_noise`
on the Copilot adapter
([agent_adapters.py](../src/agents_live/agent_adapters.py)) describe the
Linux `script -qc` path rather than the Copilot family, so they become
host-specific rather than adapter-wide. And `copilot` on an interactive
PATH can be a PowerShell bootstrapper rather than the CLI, which promotes
"pin a fully qualified executable" from hygiene to a correctness
requirement: name resolution fails in a way that reads as "Copilot does
not work headlessly".

### 2026-07-25: scheduling uses native triggers plus a dueness check

Task Scheduler owns the timing. Cron expressions translate to native
triggers exactly wherever they map cleanly, and to a coarser superset
otherwise, with a dispatcher that declines fires that are not due.
Chosen over exact-translation-only, which would have to refuse
expressions that no single native trigger expresses, and over a
per-minute tick task with an internal scheduler, which would reduce
Windows to a pulse generator and move time zones, daylight-saving
handling, catch-up, and overlap policy into Agents Live. The earlier
draft of this document recommended the tick task; that recommendation is
withdrawn. Mechanics get confirmed on the first registered task.

### 2026-07-25: watchers stay resident, started by a scheduled task

One watcher process per file-watch agent, kept alive by a scheduled task
with logon, startup, and repetition triggers running
`agents-live internal ensure-watcher <name>`. Windows publishes no
native trigger for a directory change, and the process-free alternative,
a permanent WMI event consumer, needs administrator rights, polls
expensively, and looks like malware persistence to endpoint protection.
Residency is therefore inherent to file watching, not a design
preference, and the native mechanism's role is to start and restart the
watcher.

### 2026-07-25: `.sh` handlers are refused on Windows

Python and Node handlers already run natively, so plan mode is not
blocked. `.sh` requires Git Bash or an explicitly configured
interpreter; the WSL launcher on PATH is rejected by identity because
accepting it would run handlers against the wrong root and appear to
work.

### 2026-07-25: Windows ownership is a generated UUID

`windows:<uuid>`, generated once into the Windows user state home, with
the hostname kept as a display label only. Linux and WSL hostname
matching is unchanged, and no cross-runtime migration is needed because
the two environments never share a checkout.

### 2026-07-25: hardening narrowed to bug prevention

The security model now names six cheap checks that ship with the first
working path, and defers custom ACLs, SID and nonce process identity, job
objects, and handle-based TOCTOU revalidation with a stated condition for
revisiting each. The earlier draft carried those as requirements, which
would have added privilege-boundary machinery to a tool that runs as the
developer and makes no claim to constrain them.
