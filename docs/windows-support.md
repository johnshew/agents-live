---
title: Native Windows Support Proposal
description: What it would take to run agents-live directly on Windows, using Task Scheduler and Windows file-change notification instead of cron and inotifywait
ms.date: 2026-07-25
ms.topic: concept
---

Status: draft for discussion. No decision has been made.

> [!IMPORTANT]
> The first decision is feasibility, not architecture hardening. Test the
> native Windows Copilot CLI with redirected output and ConPTY before changing
> the existing runtime. If neither path is viable, the proposal either narrows
> to Claude or stops.

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

## What is already portable

| Area | State |
|---|---|
| CLI, config parsing, agent discovery | Portable. TOML, JSON, YAML frontmatter, `pathlib` throughout |
| `run` / `headless` orchestration, modes, pipeline MCP | Mostly portable after process, handler, executable-resolution, and Copilot PTY paths are abstracted |
| Structured logging, `logs`, `timeline`, `dashboard`, `qlog` | Portable |
| Repo-relative path model | Directionally portable, but Windows needs rooted-path rejection, reparse-point handling, and handle-based identity checks beyond the current resolved-path containment check ([headless.py](../src/agents_live/headless.py)) |
| Changed-file payload | Repo-relative today, but `str(Path)` emits Windows separators; the shared layer must call `as_posix()` explicitly |

## What is not portable

| Dependency | Where | Windows equivalent |
|---|---|---|
| `crontab -l` / `crontab -` | [headless.py](../src/agents_live/headless.py), [activate.py](../src/agents_live/activate.py), [health_check.py](../src/agents_live/health_check.py) | Task Scheduler, or one tick task plus an internal scheduler |
| `inotifywait -m -r` | [activate.py](../src/agents_live/activate.py) | `ReadDirectoryChangesW`, or .NET `FileSystemWatcher` |
| `/proc` scan and `ps -eo pid=,args=` | [headless.py](../src/agents_live/headless.py) | PID files plus `OpenProcess`, or WMI `Win32_Process` |
| `os.kill`, `os.killpg`, POSIX signals | [activate.py](../src/agents_live/activate.py), [headless.py](../src/agents_live/headless.py), [health_check.py](../src/agents_live/health_check.py) | Protected stop IPC and a Job Object, then termination of an identity-verified process tree |
| `fcntl.flock`, `fcntl` non-blocking reads | [headless.py](../src/agents_live/headless.py), [activate.py](../src/agents_live/activate.py) | A per-user named mutex with an explicit DACL; overlapped I/O or a reader thread |
| `start_new_session=True` | [activate.py](../src/agents_live/activate.py) | A Job Object and an explicit process group or control channel; detached/no-console flags cannot assume `CTRL_BREAK_EVENT` remains available |
| `sh -c "cd X && PATH=Y agents-live ..."` cron lines | [activate.py](../src/agents_live/activate.py) | A Task Scheduler executable path, one Windows-quoted argument string, and a working directory; Task Scheduler does not store an argument vector |
| `script -qc` PTY for the Copilot CLI | [headless.py](../src/agents_live/headless.py) | ConPTY through `pywinpty`, or no PTY at all |
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
3. Windows and WSL never share an on-disk repository. WSL repositories live
   under the distro's `/mnt` hierarchy and Windows repositories do not. The
   Windows runtime refuses WSL namespace paths, and the WSL runtime retains its
   current behavior. Cross-runtime file watching and coordination are out of
   scope by deployment rule.
4. Platform code is a leaf. The scheduler, debounce, cascade guard,
   fire-rate breaker, dispatch, ownership, and logging stay single
   implementations that call down into a small interface.
5. Preserve the Linux and WSL security posture. Native Windows support adds
   platform mechanics, not a sandbox, policy engine, or new approval system.
6. Deliver a narrow working path before hardening it: Copilot CLI process I/O,
   one scheduled agent, then one watcher. Add lifecycle and adversarial tests
   once those paths are demonstrably viable.

## Security model

The developer running Agents Live is an administrator and authorizes agents to
operate on their behalf. Agent definitions, handlers, model tools, and child
processes therefore have the developer's effective authority. Native Windows
support does not claim to isolate an agent from the user, defend against an
administrator, or contain same-user malware. Those goals require operating
system or service isolation beyond this project.

The relevant attack surface is unintended authority caused by implementation
errors: registering or deleting the wrong scheduled task, launching the wrong
executable, misquoting arguments, following an unexpected path, terminating an
unrelated process, exposing credentials in command lines or logs, or leaving
persistent tasks behind after uninstall. The baseline should match current WSL
behavior, then use normal Windows APIs and user-scoped state correctly.

Practical hardening, after feasibility, is:

1. Create tasks in a dedicated `\AgentsLive\` folder with deterministic
   repository-scoped names. Before replacement or deletion, verify the task's
   action and working directory belong to Agents Live.
2. Persist fully qualified executable paths and use Windows-correct argument
   quoting. Avoid unnecessary shell mediation in scheduled actions.
3. Keep state, logs, and temporary payloads in the user's normal private local
   application-data directory. Do not put credentials in command lines, and
   retain the existing log-redaction expectations.
4. Initially support interactive-token tasks. Logged-off execution, stored
   credentials, S4U, mapped drives, and network repositories remain separate
   follow-up work.
5. Verify process creation time and executable identity before terminating a
   PID. This prevents stale state from stopping an unrelated process without
   pretending to provide a security boundary against the administrator.

## The seam

A single module, `hostruntime.py`, with a protocol and two
implementations. Everything platform-specific moves behind it; nothing
else in the tree imports `fcntl`, `signal`, `subprocess` for `crontab`,
or `/proc`.

```python
class HostRuntime(Protocol):
    id: str                      # "linux" | "wsl" | "windows"

    # trigger persistence (replaces crontab reads and writes)
      def install_triggers(
         self, desired: TriggerIdentity
      ) -> TriggerIdentity: ...
    def remove_triggers(self, name: str) -> None: ...
    def installed_agent_names(self, root: Path) -> list[str] | None: ...
      def trigger_state(self, name: str) -> TriggerIdentity | None: ...

    # watchers
    def spawn_detached(self, argv: list[str], cwd: Path) -> ProcessRef: ...
      def watch_events(self, dirs: list[Path]) -> Iterator[WatchBatch]: ...

    # process state
      def find_process(self, name: str, kind: str) -> ProcessIdentity | None: ...
      def is_alive(self, ref: ProcessIdentity) -> bool: ...
      def terminate(
         self, ref: ProcessIdentity, *, force: bool = False
      ) -> None: ...

    # coordination
      def exclusive_lock(self, key: str) -> AbstractContextManager[LockLease]: ...
```

The interface stays small, but its results cannot erase provenance or failure
states. `TriggerIdentity` includes the task path, principal, action, working
directory, settings, ACL fingerprint, and ownership marker. `ProcessIdentity`
includes PID, creation time, canonical executable, user SID, repository ID,
and a random launch nonce. `WatchBatch` distinguishes changes, overflow, root
invalidation, degraded rescan, and fatal error. `LockLease` identifies the
held kernel object. Registration and deletion verify ownership rather than
blindly replacing a matching name.

`installed_agent_names` returns `None` for "state
is unreadable here", which the existing sandbox handling in
[status.py](../src/agents_live/status.py) already models. `watch_events`
yields typed batches. The shared loop normalizes accepted changed paths to
repo-relative POSIX strings, bounds each batch, and responds to overflow or
root invalidation before applying debounce, ignore rules, the content-hash
cascade guard, and the fire-rate breaker in
[activate.py](../src/agents_live/activate.py).

`Triggers` carries the parsed schedule expressions and watch paths
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
memory, file size hashed, rescan frequency, and total rescan work. Changed-file
payloads must use an ACL-protected spool file or pipe, not the command line,
which is size-limited and visible to local process inspection.

Independent of the choice, a Windows watcher process needs to survive
logoff and reboot. On Linux that is an `@reboot` respawn line plus the
health check. On Windows it is one scheduled task per watcher with a
logon or startup trigger plus a repetition interval, running
`agents-live internal ensure-watcher <name>`, which is a no-op when the
watcher is alive. That single mechanism replaces both the `@reboot` line
and the health-check restart.

## Scheduling on Windows

**Option 1: translate schedules to native triggers.** Each agent
schedule becomes a registered task, named by convention so it can be
enumerated and removed the way cron lines are today. The repository
already drives `Register-ScheduledTask` through PowerShell for the
heartbeat, so the machinery exists. The problem is expressiveness: cron
lists, ranges, and steps do not map onto Daily, Weekly, and Monthly
triggers without either multiple triggers per agent or a Once trigger
with a repetition interval, and some expressions cannot be represented
at all. That means a documented supported subset and a rejection path
for the rest, which is a lasting behavioral difference between
platforms.

**Option 2: one tick task plus an internal scheduler.** Register a
single per-user task that fires every minute and runs
`agents-live internal tick`. That command evaluates every activated
agent's cron expression against the current minute and dispatches. Cron
syntax stays the universal schedule language, no translation is lost,
and `status` reads one state file instead of querying Task Scheduler.
The costs include a maintained cron evaluator, last-fired bookkeeping,
serialized tick execution, a process start per minute, and explicit policies
for time zones, daylight-saving folds and gaps, clock rollback, sleep and
catch-up, overlapping runs, and `@reboot` semantics. Task Scheduler must
reject overlapping tick instances, and last-fired state must be committed
atomically before dispatch.

Recommendation: Option 2, using a maintained parser rather than a new compact
cron rules engine and differential tests against current cron behavior. It
keeps one schedule language and one state model. Option 1 remains the better
answer if per-agent visibility inside the Windows Task Scheduler UI turns out
to matter to operators.

Either way, Task Scheduler stores an executable path, one argument string, and
a working directory. Build the argument string with Windows
`CommandLineToArgvW`-compatible quoting and verify it by round trip. Pin a
fully qualified, ACL-verified executable, not a PATH-resolved or writable
shim, then read back and compare the complete registered task definition.

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
- `Path.resolve()` plus ancestry comparison is a useful lexical check, not a
   complete Windows containment boundary. Reject unexpected reparse points,
   open the target by handle, obtain its final path and volume/file identity,
   and revalidate immediately before watching, executing, or mutating it. Fail
   closed if identity changes.

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

1. **Test native Copilot CLI I/O.** On a native Windows host, run the Windows
   Copilot CLI with redirected standard streams. Record output, escape
   sequences, exit behavior, cancellation, and authentication behavior. If
   redirected I/O fails, repeat through ConPTY. This determines whether the
   primary runtime is possible and whether `pywinpty` is required.
2. **Build one direct foreground run.** Make `agents-live run` execute one
   approved agent from a Windows repository without scheduling or watching.
   Resolve process creation, path spelling, handler selection, and logs before
   adding persistence.
3. **Add the native Windows runtime ID.** Generate `windows:<uuid>` in the
   Windows user state home and use it for Windows ownership matching. Leave
   Linux and WSL hostname behavior unchanged.
4. **Register one scheduled run.** Use a user-scoped Task Scheduler action with
   a fully qualified executable, Windows-correct arguments, an explicit
   working directory, and enough metadata to identify the task during removal.
5. **Choose schedule semantics.** Prototype the one-minute tick with a
   maintained cron parser. Confirm DST, overlap, sleep, and restart behavior
   before generalizing activation and status.
6. **Build one watcher.** Compare `ReadDirectoryChangesW` and `watchdog` on a
   native Windows repository. Prove cancellation, rename, overflow, root
   deletion, and bounded rescan behavior before choosing the implementation.
7. **Extract the host runtime.** Refactor only the abstractions proven by the
   foreground, scheduler, and watcher prototypes. Keep existing Linux and WSL
   behavior unchanged and avoid speculative interface methods.
8. **Complete lifecycle commands.** Add `start`, `status`, `doctor`, `stop`,
   `uninstall`, and `upgrade` parity. Verify task identity before replacement
   or deletion and process identity before forced termination.
9. **Harden failure paths.** Bound watcher queues and payloads, remove secrets
   from command lines, define logged-off execution limits, and make interrupted
   task updates recoverable. This is correctness hardening within the trusted-
   administrator model, not sandboxing.
10. **Add Windows CI and adversarial tests.** Cover quoting, task collisions,
    stale PID reuse, path aliases and reparse points, watcher overflow,
    interrupted upgrades, and uninstall cleanup after the implementation
    exists to test.

## Size and blast radius

Rough, and stated as a range because the refactor comes first:

- Extraction of `HostRuntime` and the POSIX implementation: mostly moved
  code, close to zero net new lines, but it touches `activate.py`,
  `headless.py`, `health_check.py`, `status.py`, `stop.py`,
  `migrate.py`, `uninstall.py`, and `doctor.py`. This is the change that
  needs the most review attention and the one that carries regression
  risk for the platform that already works.
- Windows implementation: 700 to 1,000 new lines, concentrated in one
  or two modules. Task registration and enumeration around 250,
  `ReadDirectoryChangesW` around 250, process and lock primitives around
  150, tick scheduler and cron evaluation around 200.
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

1. Feasibility spike: Copilot redirected I/O, then ConPTY only if needed.
2. Vertical slice: foreground `run`, one scheduled agent, and one watched
   agent on native Windows.
3. Consolidation: extract the host runtime around the working slice and add
   the Windows runtime UUID without changing Linux or WSL behavior.
4. Productization: complete lifecycle commands, diagnostics, supported
   handlers, schedule semantics, and operator documentation.
5. Hardening: add Windows CI, adversarial tests, bounded failure handling, and
   transactional upgrade and uninstall behavior.

Each phase has an explicit stop decision. Failure of Copilot redirected I/O
and ConPTY narrows the supported runtime or ends the work before a broad
refactor. Hardening follows a working vertical slice rather than attempting to
design isolation that the trusted-administrator model cannot provide.

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

- Does the Copilot CLI work headlessly on Windows without a PTY?
  **Investigate on Windows only.** Run the Windows Copilot CLI with `-p`
  and a redirected stdout on a native Windows host and record whether
  the agent output arrives on stdout, whether TUI escape sequences need
  filtering, and what the exit code is. Do not attempt this from WSL:
  the Linux binary is a different build with a different terminal
  assumption, so a WSL result would be misleading either way. If output
  does not reach a redirected stdout, the follow-up question is whether
  ConPTY through `pywinpty` is acceptable as a dependency.
- Tick scheduler or translated per-agent tasks?
