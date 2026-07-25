---
title: Native Windows Support Proposal
description: What it would take to run agents-live directly on Windows, using Task Scheduler and Windows file-change notification instead of cron and inotifywait
ms.date: 2026-07-25
ms.topic: concept
---

Status: draft for discussion. No decision has been made.

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
| `run` / `headless` orchestration, modes, pipeline MCP | Portable except the Copilot PTY path |
| Structured logging, `logs`, `timeline`, `dashboard`, `qlog` | Portable |
| Repo-relative path model | Already correct: `watchPath` resolves as `repo_root() / wp` with a containment check ([headless.py](../src/agents_live/headless.py)) |
| Changed-file payload | Already emitted as repo-relative POSIX strings |

## What is not portable

| Dependency | Where | Windows equivalent |
|---|---|---|
| `crontab -l` / `crontab -` | [headless.py](../src/agents_live/headless.py), [activate.py](../src/agents_live/activate.py), [health_check.py](../src/agents_live/health_check.py) | Task Scheduler, or one tick task plus an internal scheduler |
| `inotifywait -m -r` | [activate.py](../src/agents_live/activate.py) | `ReadDirectoryChangesW`, or .NET `FileSystemWatcher` |
| `/proc` scan and `ps -eo pid=,args=` | [headless.py](../src/agents_live/headless.py) | PID files plus `OpenProcess`, or WMI `Win32_Process` |
| `os.kill`, `SIGTERM`, `SIGHUP`, `SIGKILL` | [activate.py](../src/agents_live/activate.py), [headless.py](../src/agents_live/headless.py) | `CTRL_BREAK_EVENT` to a process group, then `TerminateProcess` |
| `fcntl.flock`, `fcntl` non-blocking reads | [headless.py](../src/agents_live/headless.py), [activate.py](../src/agents_live/activate.py) | `msvcrt.locking` or an exclusive-create lock file; overlapped I/O or a reader thread |
| `start_new_session=True` | [activate.py](../src/agents_live/activate.py) | `DETACHED_PROCESS` plus `CREATE_NEW_PROCESS_GROUP` plus `CREATE_NO_WINDOW` |
| `sh -c "cd X && PATH=Y agents-live ..."` cron lines | [activate.py](../src/agents_live/activate.py) | Task actions take an executable plus an argument list and a working directory, so the shell idiom is unnecessary |
| `script -qc` PTY for the Copilot CLI | [headless.py](../src/agents_live/headless.py) | ConPTY through `pywinpty`, or no PTY at all |
| `bash` for `.sh` handlers | [headless.py](../src/agents_live/headless.py) | Python and Node handlers already run natively; `.sh` requires Git Bash and is otherwise refused (see Handlers on Windows) |
| `hostname -s` | [ownership.py](../src/agents_live/ownership.py) | `socket.gethostname()` fallback already exists |

## Design principles

1. Ownership is per runtime environment, not per machine. A machine
   running both WSL and Windows presents two independent environments,
   and every owner value names one of them. An agent owned by a specific
   environment activates only there; nothing infers a second one.
2. Paths stay repo-relative POSIX strings everywhere except the syscall
   boundary. `/` in a `watchPath` means the repository root, not the
   filesystem root. Logs, hashes, changed-file payloads, and status
   output keep one spelling on every platform.
3. Never watch across the WSL boundary. `inotify` does not see changes
   that Windows applications make under `/mnt/c`, and
   `ReadDirectoryChangesW` does not fire for `\\wsl.localhost` paths.
   Silent no-op watchers are the worst failure mode this project can
   ship, so this becomes a validated rule, not documentation.
4. Platform code is a leaf. The scheduler, debounce, cascade guard,
   fire-rate breaker, dispatch, ownership, and logging stay single
   implementations that call down into a small interface.

## The seam

A single module, `hostruntime.py`, with a protocol and two
implementations. Everything platform-specific moves behind it; nothing
else in the tree imports `fcntl`, `signal`, `subprocess` for `crontab`,
or `/proc`.

```python
class HostRuntime(Protocol):
    id: str                      # "linux" | "wsl" | "windows"

    # trigger persistence (replaces crontab reads and writes)
    def install_triggers(self, name: str, triggers: Triggers) -> None: ...
    def remove_triggers(self, name: str) -> None: ...
    def installed_agent_names(self, root: Path) -> list[str] | None: ...
    def trigger_state(self, name: str) -> TriggerState: ...

    # watchers
    def spawn_detached(self, argv: list[str], cwd: Path) -> ProcessRef: ...
    def watch_events(self, dirs: list[Path]) -> Iterator[Path]: ...

    # process state
    def find_process(self, name: str, kind: str) -> ProcessRef | None: ...
    def is_alive(self, ref: ProcessRef) -> bool: ...
    def terminate(self, ref: ProcessRef, *, force: bool = False) -> None: ...

    # coordination
    def exclusive_lock(self, path: Path) -> AbstractContextManager[None]: ...
```

Ten to twelve methods. `installed_agent_names` returns `None` for "state
is unreadable here", which the existing sandbox handling in
[status.py](../src/agents_live/status.py) already models. `watch_events`
yields absolute paths and the shared loop converts them once, so the
debounce, ignore rules, content-hash cascade guard, and fire-rate
breaker in [activate.py](../src/agents_live/activate.py) stay exactly as
they are.

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
it fits `watch_events` exactly. Roughly 200 to 250 lines including
buffer-overflow recovery, which must be handled: when the change buffer
overflows the API reports it and the watcher has to fall back to a
directory rescan.

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

Recommendation: A, with B as the fallback if the ctypes surface proves
troublesome in practice. C reads as the cheap option but trades a
well-understood in-process failure mode for an interop one.

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
The costs are real but bounded: a cron-expression evaluator of about 80
lines, last-fired bookkeeping so a catch-up run after sleep does not
fire the same minute twice, a process start per minute, and a decision
about `@reboot` semantics (a startup trigger on the same task).

Recommendation: Option 2. It keeps one schedule language, one state
model, and one place where scheduling semantics live, and it is less
code than a faithful cron-to-trigger translator. Option 1 remains the
better answer if per-agent visibility inside the Windows Task Scheduler
UI turns out to matter to operators.

Either way, task actions take an executable and an argument list, so the
generated `cd X && PATH=Y ...` line disappears on Windows. The pinned
absolute path to the installed shim, which the current cron lines
already use, is exactly the right shape for a task action.

## Ownership generalization

Owner values today are `"*"` or a short hostname
([ownership.py](../src/agents_live/ownership.py)). One physical machine
can now host two runtime environments, so hostname alone is ambiguous.

Proposal: keep the value a single string and extend its grammar.

| Value | Meaning |
|---|---|
| `*` | Run in every runtime environment. On a machine hosting both WSL and Windows, that is both of them |
| `<host>` | Transitional: the default WSL distro on that host, or native Linux on that host |
| `<host>/wsl:<distro>` | That distro on that host |
| `<host>/windows` | The Windows runtime on that host |

Two functions carry it: `current_owner_id()` returning the fully
qualified form, and `owner_matches(spec)` implementing the table.
Because a bare hostname never matches the Windows runtime, no existing
deployment starts running an agent twice when a Windows runtime appears
on the same machine. Windows ownership is always explicit, which is the
correct default for the failure mode that matters.

`*` keeps its plain reading: every runtime environment, including two on
one machine. It is an explicit operator choice, and each environment
keeps its own state, logs, and watcher, so the two runs are independent
rather than racing over shared state. The one thing they share is the
repository working tree, which is already true of `*` across machines
today.

The bare hostname form stays readable until the developer calls for its
removal. `--transfer-to` and `init` write the fully qualified form from
the start, so the compatibility read path stops mattering once existing
registries are rewritten; [migrate.py](../src/agents_live/migrate.py)
performs that rewrite. Removal is a deliberate, announced change, not a
dated deprecation.

## Paths and repository identity

- `repo_state_key` hashes the resolved absolute path
  ([paths.py](../src/agents_live/paths.py)). The same repository is
  `C:\repos\x` to Windows and `/mnt/c/repos/x` to WSL, so the two
  runtimes get different state directories. That is correct: state is
  per-runtime. Logs for one repository will be split across two state
  homes, which `logs --all-repos` and the dashboard need to understand.
- The user state home has to resolve somewhere sane on Windows. XDG
  variables are honored first today; `%LOCALAPPDATA%\agents-live` is the
  native answer when they are absent.
- Windows path hazards that need explicit handling: case-insensitive
  comparison when matching changed files against ignore patterns, the
  260-character limit for deep repository trees, and reserved device
  names. None is hard; all are silent if missed.
- The containment check on `watchPath` must stay a prefix check on
  resolved paths, not a string check, so that drive letters and short
  names cannot escape the repository root.

## Handlers on Windows

Handlers are dispatched by file extension: `.py` through
`uv run --with`, `.js` through `node`, anything else through `bash`
([headless.py](../src/agents_live/headless.py)). Python and Node
handlers are already portable, so plan mode works on Windows with them
unchanged. Only `.sh` needs an interpreter Windows does not ship.

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

## Risks, hardest first

1. **Copilot CLI without a PTY.** The adapter drives Copilot through
   `script -qc` and filters TUI noise, because the CLI writes agent
   output to the terminal rather than to stdout. Windows has no
   `script`; the equivalents are ConPTY through `pywinpty` or no PTY at
   all. This cannot be settled from WSL: the Linux binary's behavior
   says nothing about how the Windows build handles a redirected stdout,
   and a WSL experiment would answer a different question. It has to be
   measured on a native Windows host with the Windows Copilot CLI
   installed. Until then, native Windows may support the `claude`
   runtime only. This is the single largest scoping question.
2. **Shell handlers.** Decided above: `.sh` handlers are refused on
   Windows unless a real Git Bash or an explicitly configured
   interpreter is present. The residual risk is operator surprise for
   anyone carrying shell handlers across from a WSL deployment, which
   the `doctor` check exists to catch before activation does.
3. **Unintended double activation.** A checkout reachable from both
   runtime environments makes it easy to activate the same agent twice
   by accident. `*` makes that deliberate and supported; the guard is
   that every other owner value names exactly one environment, and that
   `status` shows which environment a row came from.
4. **Cross-boundary watching.** Covered above; needs a validation rule
   in preflight and a `doctor` check, not a paragraph in the docs.
5. **Task Scheduler and logon state.** A task that must run while no
   user is logged on either stores credentials or uses an S4U logon,
   which then has no mapped drives and a restricted profile. The current
   heartbeat sidesteps this because a WSL session is already implied.
6. **Graceful stop.** No `SIGTERM`. The watcher needs a documented stop
   protocol, most likely `CTRL_BREAK_EVENT` to a process group created
   for that purpose, with `TerminateProcess` as the escalation. Without
   it the in-flight debounce and log-close behavior on stop is lost.
7. **Process discovery.** Command-line matching through WMI is slow
   enough to be felt in `status`. PID files recorded at spawn, validated
   against process creation time, are the better primitive, and adopting
   them on both platforms removes the `/proc` and `ps` fallback pair.
8. **Testing.** The smoke suite assumes POSIX behavior in places and CI
   has no Windows runner. Windows support is not real until CI proves it
   on every commit, and that job is part of the cost.

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

1. Settle the Copilot PTY scope and the scheduling choice. Nothing else
   starts before these.
2. Pure refactor: introduce `HostRuntime`, move POSIX code behind it, no
   behavior change, existing tests unchanged and passing.
3. Ownership generalization with the transitional read path and its
   expiry recorded.
4. Windows scheduling and `run` on Windows for the supported runtimes,
   cron-triggered agents only.
5. Windows watcher, plus the preflight rule that refuses cross-boundary
   watch paths.
6. `doctor`, `status`, `stop`, `uninstall`, and `upgrade` parity, a
   Windows CI job, and documentation.

Each phase after the second is independently useful. Phase 4 alone gives
a Windows host scheduled agents with no WSL dependency at all.

## Non-goals

- Replacing the Linux implementation with a cross-platform library.
- Sharing runtime state between two runtime environments. Each keeps its
  own state home, logs, and watchers, including when `*` runs an agent
  in both.
- Watching files across the WSL boundary in either direction.
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
