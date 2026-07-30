---
title: Native Windows Support
description: How agents-live runs natively on Windows - Task Scheduler instead of cron, ReadDirectoryChangesW instead of inotifywait, and the seam that keeps the two implementations from leaking into each other
ms.date: 2026-07-30
ms.topic: concept
---

Agents Live runs on three hosts: Linux, Ubuntu on WSL, and native
Windows. This document describes how the Windows half works and why it
is built the way it is. It is an architecture guide, not a plan; the
decision log at the end records what was tried, what was measured, and
what was rejected.

The runtime itself, `uv` plus Python 3.12, was portable from the start.
What was not is everything around it: the two dispatch mechanisms
(`crontab` and `inotifywait`), the process and locking primitives, and
the shell idioms embedded in generated cron lines. Native Windows
support replaces each of those with a host equivalent behind a single
seam, so that scheduling policy, watcher policy, ownership, dispatch,
and logging remain one implementation.

The payoff is direct. A WSL deployment needs a Windows scheduled task
whose only job is to keep the distro alive so cron and the watchers keep
running ([heartbeat.py](../src/agents_live/heartbeat.py)). For a
repository that lives on a Windows volume, the native runtime removes
that bridge entirely and makes file watching reliable. The cost is a
second implementation of dispatch, process state, and locking, plus an
ownership model that can say which of the runtimes on one physical
machine owns an agent.

Status: implemented and covered by CI on `windows-latest` alongside
`ubuntu-latest`. Whether the Windows half earns its keep in the long run
stays an open product question; the engineering question is settled.

## Where the code lives

| Concern | Module | Shape |
|---|---|---|
| Host identity, processes, locking, spawning | [hostruntime.py](../src/agents_live/hostruntime.py) | One module, branching internally |
| Schedule language and canonical form | [triggers.py](../src/agents_live/triggers.py) | Shared. `TriggerSpec`, cron parsing, crontab rendering and matching |
| Choice of dispatch mechanism | [schedules.py](../src/agents_live/schedules.py) | Shared. The only place that decides crontab or Task Scheduler |
| Task Scheduler registration | [wintasks.py](../src/agents_live/wintasks.py) | Windows only |
| File-change event source | [watchsource.py](../src/agents_live/watchsource.py) | Seam. `start`, `poll`, `stop` |
| `ReadDirectoryChangesW` | [winwatch.py](../src/agents_live/winwatch.py) | Windows only |
| Debounce, ignores, cascade guard, fire-rate breaker | [watchpolicy.py](../src/agents_live/watchpolicy.py) | Shared, pure over a batch |
| Windowless task action | [hidden.py](../src/agents_live/hidden.py) | Windows only |
| Windows-side heartbeat for a WSL runtime | [heartbeat.py](../src/agents_live/heartbeat.py) | WSL only, drives PowerShell across the interop boundary |
| Ownership | [ownership.py](../src/agents_live/ownership.py) | Shared, asks the seam for identity |

Nothing outside `hostruntime.py`, `wintasks.py`, `winwatch.py`,
`hidden.py`, and `heartbeat.py` calls a Windows API or runs a Windows
program. `activate`, `stop`, `status`, `doctor`, `migrate`, `uninstall`,
and `smoketest` ask `schedules.py`, `watchsource.py`, or the host
runtime and get an answer. `TestPlatformSeam` in the smoke suite asserts
this, so a module that starts naming a platform fails the suite rather
than the invariant quietly going stale (#191).

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
this path would otherwise ship:

1. **Pin a fully qualified `copilot.exe`; never resolve by name.** The
   `copilot` on an interactive PATH can be a PowerShell bootstrapper
   rather than the CLI, and VS Code installs exactly such a shim
   (`copilot.ps1` in its extension storage) that can prompt to install or
   update. `CreateProcess` cannot execute a `.ps1`; pinning therefore
   searches PATH and PATHEXT in order, skips script and batch shims, and
   selects the first native executable. This is the concrete failure
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

## The platform boundary

What crosses the boundary unchanged:

| Area | State |
|---|---|
| CLI, config parsing, agent discovery | Portable. TOML, JSON, YAML frontmatter, `pathlib` throughout |
| `run` / `headless` orchestration, modes, pipeline MCP | Portable. Process creation, environment construction, executable pinning, and handler selection go through the host runtime; the Copilot CLI itself needs no PTY on Windows |
| Structured logging, `logs`, `timeline`, `dashboard`, `qlog` | Portable |
| Repo-relative path model | Portable, with rooted-path rejection by `anchor` rather than `is_absolute` and reparse points resolved before containment is decided ([paths.py](../src/agents_live/paths.py)) |
| Changed-file payload | Repo-relative POSIX on every host: `as_posix()`, because the string is an identifier and a cache key, not a filesystem argument ([watchpolicy.py](../src/agents_live/watchpolicy.py)) |

What does not, and what replaces it:

| Dependency | Where | Windows equivalent |
|---|---|---|
| `crontab -l` / `crontab -` | [crontasks.py](../src/agents_live/crontasks.py), behind the store seam in [schedules.py](../src/agents_live/schedules.py) | One Task Scheduler task per agent, with a dispatcher that confirms dueness (see Scheduling on Windows). [wintasks.py](../src/agents_live/wintasks.py) answers the same questions with the same signatures, so the dispatch point selects a store rather than branching per operation |
| `inotifywait -m -r` | [activate.py](../src/agents_live/activate.py) | `ReadDirectoryChangesW` through `ctypes`, behind the `EventSource` seam in [watchsource.py](../src/agents_live/watchsource.py) |
| `/proc` scan and `ps -eo pid=,args=` | [headless.py](../src/agents_live/headless.py) | A `Win32_Process` snapshot of pid and command line, so a watcher is still found by what it runs rather than by a remembered pid. Reading one back is `hostruntime.split_command_line`, beside the enumeration that produced it: Windows joined the arguments by quoting rules, not by spaces, and a repo root routinely has one |
| `os.kill`, `os.killpg`, POSIX signals | [activate.py](../src/agents_live/activate.py), [headless.py](../src/agents_live/headless.py), [health_check.py](../src/agents_live/health_check.py) | `OpenProcess` for liveness and `TerminateProcess` over a snapshot-derived process tree; identity verification before forced termination |
| `fcntl.flock`, `fcntl` non-blocking reads | [headless.py](../src/agents_live/headless.py), [activate.py](../src/agents_live/activate.py) | `LockFileEx` on a byte past any content, which behaves like `flock` and unlike a named mutex |
| `start_new_session=True` | [activate.py](../src/agents_live/activate.py) | `CREATE_NEW_PROCESS_GROUP` plus `CREATE_NO_WINDOW`, and never `DETACHED_PROCESS`: a detached child allocates a console of its own, which the desktop then draws (see the decision log). Termination walks the tree |
| `sh -c "cd X && PATH=Y agents-live ..."` cron lines | [activate.py](../src/agents_live/activate.py) | A Task Scheduler executable path, one Windows-quoted argument string, and a working directory; Task Scheduler does not store an argument vector |
| `script -qc` PTY for the Copilot CLI | [headless.py](../src/agents_live/headless.py) | Not needed. The Windows CLI writes clean text to a redirected stdout with no console, so plain pipes suffice, and `supports_pty` selects the plain path (see Invoking the Copilot CLI on Windows) |
| `env -i` plus `HOME` and `PATH` as the whole agent environment | [headless.py](../src/agents_live/headless.py) | A Windows child needs `SystemRoot` to load at all, plus the profile, temp, and processor variables; `base_env` supplies that floor, and PATH is inherited rather than constructed (see the decision log) |
| Command names resolved by the child | [headless.py](../src/agents_live/headless.py) | `CreateProcess` searches the launching process's PATH, not the child's environment, so `pin_executable` resolves an absolute executable up front and refuses script and batch shims |
| ASCII-safe console output | every command that prints | A Windows console defaults to a legacy code page, so UTF-8 output raises `UnicodeEncodeError`; `use_utf8_io` reconfigures the streams, exports `PYTHONUTF8`, and restores the console code page on exit |
| Locale-decoded child output | every module that captures a subprocess | The same legacy code page decodes captured bytes, so `text=True` alone reads mojibake on Windows and correctly on Linux; `hostruntime.CHILD_TEXT` states UTF-8 for every child this tool configures. Children that write something else settle it themselves: `schtasks` is read as `oem` in [wintasks.py](../src/agents_live/wintasks.py), and PowerShell writes the OEM page into a pipe whatever the host, so `powershell_argv` tells it to write UTF-8 instead |
| `bash` for `.sh` handlers | [headless.py](../src/agents_live/headless.py) | Python and Node handlers already run natively; `shell_interpreter` reports no shell on Windows, so `.sh` and any unrecognized extension are refused (see Handlers on Windows) |
| `hostname -s` | [ownership.py](../src/agents_live/ownership.py) | Nothing platform-specific. Every runtime, Windows and POSIX alike, owns under a generated `hostname/runtime/uuid` identity, because one machine hosts several runtimes and a hostname names all of them |
| `os.fchmod` when writing state atomically | [paths.py](../src/agents_live/paths.py) | `os.chmod` on the temporary file: Windows grew `os.fchmod` only in Python 3.13, and has no POSIX mode bits to narrow in any version |

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
7. A check earns its place by preventing a bug class, not by covering a
   threat model the trust model does not have. Identity and quoting
   checks that stop the tool acting on the wrong target are built in;
   everything else is deferred with a stated condition for revisiting.
8. Refactor where a regression is visible. Structural change to shared code
   lands on Linux and WSL under tests that pin current behavior, before the
   Windows implementation can confuse the diagnosis. The exception is a
   boundary the two platforms define differently, which is written after a
   throwaway spike on the platform that does not yet run - never before.

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
prevents a bug class that would otherwise ship and costs almost nothing.
Anything that only defends the developer from the developer is theater here,
and is deferred by default.

Built in, because each is cheap and each prevents a real failure:

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

The seam is three modules, not one, and it is a set of functions rather
than a protocol with two classes. That is the first thing the
implementation changed about the design.

[hostruntime.py](../src/agents_live/hostruntime.py) answers questions
about the host: which runtime this is, where user state lives, what to
call this runtime in an owner value, which scheduler is native, what a
child process's environment floor has to contain, whether a PTY is
available, how an executable is pinned, how an exclusive lock is taken,
and how a process is spawned, found, checked, and terminated. Nothing
else in the tree imports `fcntl`, `signal`, `/proc`, or `ctypes.wintypes`.

[schedules.py](../src/agents_live/schedules.py) is the only place that
chooses between crontab and Task Scheduler. `activate`, `stop`,
`status`, `doctor`, `migrate`, and `smoketest` call `install`, `remove`,
`is_active`, `installed_names`, and their watcher and maintenance
counterparts, and never name a mechanism.

[watchsource.py](../src/agents_live/watchsource.py) is the one place
that is a protocol, because it is the one place with genuine per-host
state:

```python
class EventSource(Protocol):
    def start(self) -> None: ...
    def poll(self, timeout: float | None) -> list[str]: ...
    def stop(self) -> None: ...
```

`PosixEventSource` runs `inotifywait` and reads its stdout;
[winwatch.py](../src/agents_live/winwatch.py) drives
`ReadDirectoryChangesW`. `open_source` picks one. A watch loop never
learns which it got.

### Why functions rather than a protocol object

The original design was a `HostRuntime` protocol with `PosixRuntime` and
`WindowsRuntime` implementations, carrying `TriggerIdentity`,
`ProcessIdentity`, `WatchBatch`, and `LockLease` value types. Building
it showed the object was paying for nothing. Almost every member is a
pure function of the host, called once, with no instance state to hold;
the parts that do carry state are the event source, which became the
protocol, and the task store, which is Windows-only and lives in
`wintasks.py`. A single dispatching object would have added a
constructor, an injection point, and a second name for every operation,
in exchange for a polymorphism that only one member needed.

What did survive intact is the vocabulary. `TriggerSpec` in
[triggers.py](../src/agents_live/triggers.py) is the desired state: the
parsed schedule expressions, watch paths, and repository identity. It
replaced the preformatted cron line as the shared currency, which is the
change that made a second dispatch mechanism possible at all. Rendering
and matching crontab lines became implementation detail alongside it,
and [migrate.py](../src/agents_live/migrate.py) converges against a spec
rather than against a string it rebuilds itself.

Watcher policy came out of the loop the same way. Debounce, ignore
rules, the content-hash cascade guard, and the fire-rate breaker are a
pure function over a batch in
[watchpolicy.py](../src/agents_live/watchpolicy.py), reachable in tests
without an event source of any kind.

### Where the two platforms genuinely disagree

Four places where a POSIX-derived interface would have been wrong rather
than merely incomplete. Each was settled by a throwaway Windows spike
before the corresponding code was written, and two of the four
predictions turned out to be wrong.

| Concern | POSIX shape | What Windows actually needed |
|---|---|---|
| Exclusive lock | `fcntl.flock`: advisory, per-descriptor, released on close | Predicted a named mutex, with its thread affinity and abandoned-mutex semantics. It is `LockFileEx` on a byte past any content, which behaves like `flock` |
| Terminate | `os.killpg` on a process group | Windows has no process group. Predicted a Job Object for tree identity; a job cannot supply it after its creator exits, so termination walks a `Win32_Process` snapshot |
| Watch events | `inotifywait` is a path stream with no failure vocabulary | Buffer overflow, root invalidation, and `ERROR_NOTIFY_ENUM_DIR` are real states. They degrade to one bounded rescan rather than becoming caller-visible variants |
| Detached spawn | `start_new_session=True` | Not `DETACHED_PROCESS`. A detached child allocates its own console, which the desktop draws; `CREATE_NO_WINDOW` alone is what makes a spawn invisible |

The lesson those four share is that the interface had to be written
after the spike, not before it. Every prediction made from the POSIX
side that was not checked against a running Windows host was either
wrong or more complicated than it needed to be.

## File change notification on Windows

[winwatch.py](../src/agents_live/winwatch.py) calls
`ReadDirectoryChangesW` through `ctypes`. It is the direct analogue of
inotify: one handle per watched directory, recursive, delivering change
records whose paths are already relative to the watched directory. No
new dependency, and cancellable from another thread with `CancelIoEx`.
The cost is that the implementation owns the details: validating record
offsets and UTF-16 lengths, pairing rename records, handling root
deletion, reparse points, cancellation, and `ERROR_NOTIFY_ENUM_DIR`, and
treating buffer overflow as a degraded state rather than an error.

Two alternatives were weighed and not built.

The `watchdog` package would have removed the `ctypes` work at the cost
of a runtime dependency and a more abstract event model than the loop
expects. It was also a standing temptation to adopt on Linux, which
would replace a battle-tested `inotifywait` path for no benefit. The
spike showed the direct call was small enough that the dependency bought
nothing.

A PowerShell child process wrapping .NET `FileSystemWatcher` would have
been the smallest diff, since the loop already reads newline-delimited
paths from a child's stdout. It trades a well-understood in-process
failure mode for an interop and quoting boundary, costs a PowerShell
start per watcher, and adds a script to the package payload.

Every failure the API can report degrades to the same response: one
bounded rescan. Overflow, a dropped event, and a full internal queue all
mean "the truth is on disk, go look", and the rescan is capped so that a
storm cannot turn into unbounded work. A batch of changed files goes to
a file in the state directory rather than onto the command line, which
is length-limited.

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
working directory. The argument string is built with Windows
`CommandLineToArgvW`-compatible quoting and verified by round trip, the
executable is fully qualified rather than PATH-resolved, and the
registered definition is read back and compared after registration.

### What registration looks like

[wintasks.py](../src/agents_live/wintasks.py) builds and registers task
definitions, and reading a cron expression lives in
[triggers.py](../src/agents_live/triggers.py), with the schedule
language rather than with either dispatch mechanism.

A task is named `<agent>@<digest>`, where the digest covers the
repository root, and lives in the `\AgentsLive` folder of the task
store. That makes the same agent in two checkouts two tasks that cannot
replace or delete each other, and it makes enumeration cheap. Before
any replace or delete, the registered definition is read back and
checked: the action has to be an `agents-live` command whose working
directory is that repository, and a definition that does not decode or
does not parse fails the check rather than passing it.

Calendar schedules register as calendar triggers, daily, weekly, or
monthly. Everything else registers as a repetition whose step covers
every minute the expression can name, and `claim_due_minute` declines
the fires that are not firing times. No valid expression is refused;
an unreadable one is.

An agent's `@reboot` schedules register as a second task with a `.boot`
suffix. One task carries one action, and its action carries `--boot`,
which is how a startup fire is told apart from a clock fire: the first
is exact and asks nothing, the second has to be checked.

No task action names the tool directly. A task runs with an interactive
token, in the developer's own session, so a console program named as
the action opens a console window on every fire. The action names
`pythonw` instead, and `pythonw` runs
[hidden.py](../src/agents_live/hidden.py), which starts the real
command with `CREATE_NO_WINDOW`. Ownership reads back through that
wrapper to the program that finally runs, so the identity check is
unchanged by it.


### Watchers are started by Task Scheduler, not scheduled by it

Each file-watch agent keeps one small watcher process, exactly as on
Linux today. A scheduled task with a logon trigger, a startup trigger,
and a repetition interval runs
`agents-live internal ensure-watcher <name>`, which exits immediately
when the watcher is alive and respawns it when it is not. That one task
replaces both the `@reboot` respawn line and the health-check restart.

A task name carries a suffix
naming what it is for, empty for a clock task, `.boot` for a startup run
of the agent, and `.watch` for the respawn of its watcher, so the three
cannot collide and enumeration, ownership, and removal read all of them.
The choice between that task and the crontab `@reboot` line belongs to
[schedules.py](../src/agents_live/schedules.py), like every other
mechanism choice.
The watcher stays resident because Windows publishes no native trigger
for "this directory changed". Task Scheduler event triggers read the
Windows event log, and file-system changes are not written there. The
one genuinely process-free alternative, a permanent WMI event consumer
over `CIM_DataFile`, requires administrator registration, polls
expensively, and is a well-known malware persistence pattern that
endpoint protection flags on sight; it is rejected. So the native
mechanism's job is to start the watcher and keep it started, which is
what the ensure-watcher task does.

## Ownership

An owner value is `"*"` or a `hostname/runtime/uuid` identity
([ownership.py](../src/agents_live/ownership.py)). One physical machine
can host several runtime environments, so a hostname alone is ambiguous.
The value stays a single string, which the registry backend requires,
and its grammar carries the three parts.

| Value | Meaning |
|---|---|
| `*` | Run in every environment that has activated that repository |
| `<host>/<runtime>/<uuid>` | One runtime registration: the host and runtime are the display label, the uuid is the match |
| anything else | Not this runtime's. It does not run here and is not cleaned up here |

The runtime part is `windows` on native Windows and the distro name on
WSL. WSL is the case that needs a name of its own: a distro's hostname
defaults to the Windows computer name, so two distros on one machine are
indistinguishable by hostname, and only the distro name tells a reader
which row belongs to which.

Each runtime generates a UUID once into its user state home. The UUID is
stable across repository moves, machine renames, and package upgrades,
but is regenerated for a new user profile. `owns()` compares only that
UUID, so renaming a host or a distro changes how a row reads and never
who owns it; `display_owner()` reads only the host and runtime, so a
table never shows a 32-character hex string.

Splitting match from display is also what makes the model durable
against a damaged registry. Corruption, a hand edit, a truncated write,
a bad merge, and a restored backup all arrive as the same thing: a value
the matcher cannot reduce to a UUID. Rather than guess, this runtime
treats every one of them as someone else's, which is the safe direction
- the agent goes inert here and its entry survives for the machine that
can prove the claim. Recovering is one deliberate
`start --name <agent> --transfer-here`. That is why there is no
migration path and no legacy value: "unmatchable" is a permanent runtime
category, not a transition state.

The one value that is not unmatchable is an *absent* one. An agent with
no registry entry is unclaimed, not foreign, and it runs here. Local
mode depends on that: `load_owners()` returns an empty mapping and every
agent is absent.

An identity file that exists but does not hold a UUID raises
`OwnershipUnavailableError`, the same abstention an unreadable registry
produces: a runtime that cannot say who it is cannot claim an agent.

Windows repositories and WSL repositories are physically separate by
deployment rule, so their registries do not describe the same checkout
and require no cross-runtime lease.

## Paths and repository identity

- `repo_state_key` hashes the resolved absolute path
   ([paths.py](../src/agents_live/paths.py)). Windows and WSL repositories are
   physically distinct, so separate state keys and logs are expected. The
   dashboard does not need to correlate two spellings of one checkout.
- The user state home has to resolve somewhere sane on Windows. XDG
  variables are honored first, then the host's own per-user state
  directory: `~/.local/state` on POSIX and `%LOCALAPPDATA%` on Windows,
  where a roaming profile would otherwise carry machine-local runtime
  state to another machine.
- Windows path hazards that need explicit handling include case-insensitive
   ignore matching, long-path support across every child executable, reserved
   device names, alternate data streams, trailing-dot and trailing-space
   aliases, UNC and device namespaces, and drive-relative paths. Unsupported
   forms are rejected explicitly.
- A repo-relative path is tested for escape with `Path.anchor`, not
   `Path.is_absolute()`. On Windows `is_absolute()` is false for a rooted
   but driveless path (`/tmp/agents`) and for a drive-relative one
   (`C:agents`), so an `is_absolute` guard lets both through.
- `watchPath: /` is rooted on both POSIX and Windows. `.` means the
   repository root, and every rooted watch path is rejected.
- `Path.resolve()` plus ancestry comparison is a lexical check, not a complete
   Windows containment boundary, and it is the proportionate one here: the
   watcher and the watched files belong to the same account. Resolution runs
   every time, so a junction retargeted outside the repository stops being
   accepted the moment it is retargeted, and root invalidation is a watcher
   state rather than something to prevent.
- Repo-containment of a running process's command line is decided with
   `Path`, not string prefixes. Windows accepts either separator for the
   same file and compares the two case-insensitively, so a watcher
   launched with forward slashes is still recognised as its own.

## Handlers on Windows

Handlers are dispatched by file extension: `.py` through
`uv run --with`, `.js` through `node`, anything else through the host's
shell ([headless.py](../src/agents_live/headless.py)). Python and Node
handlers are mostly portable, so plan mode works on Windows. The shipped
`write-files.py` template is the generic JSON-to-files example and has no
third-party dependencies. TypeScript is currently passed to `node`
directly, which only works when the installed Node.js runtime supports
that file's syntax.

The shell fallback is POSIX-only. On Windows, `shell_interpreter` reports
no shell, so `.sh` and unrecognized extensions are refused at dispatch
with an error recommending Python or Node. Agents Live does not probe for
Git Bash, configure a project-specific interpreter, refuse activation in
advance, or report shell-handler readiness through `doctor`. Those are
limitations, not implied capabilities. Use a `.py` handler for portable
automation; installing `jq` does not make a shell handler runnable on
Windows.

## Working on a Windows host

Work on this needs a native Windows host, and none of it should require
a PyPI release or disturb a WSL deployment running on the same machine.
The separation the design already assumes makes that straightforward:
the two runtimes have different state homes, different
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
upgrade.

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

### The suite and CI

[test_smoke.py](../tests/test_smoke.py) runs against temporary projects
and runs on Windows unchanged. CI runs it on `windows-latest` and
`ubuntu-latest`, with `fail-fast` off so a Windows-only break still
reports; the pre-release audit runs on Linux only, because it reads the
tree rather than the host.

A handful of tests skip on Windows, each for a stated reason rather than
convenience. `TestCrontabConvergenceBehavior` drives a real `crontab`
process from a shebang script, which `CreateProcess` cannot run and
which no Windows host dispatches through. Three more run something under
`bash`: the WSL compatibility wrapper, which is a POSIX script a WSL
crontab executes inside the distro, and the generated bash completion. A
bare `bash` on a Windows PATH is as likely to be the WSL launcher as a
shell, so what those measure there is the host, not the artifact. One
test runs only on Windows: the command-line round trip for a repository
path containing a space, because only Windows reads a command line back
through a quoting parser.

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

- **Logged-off execution.** Tasks run with an interactive token, so
  nothing fires while nobody is signed in. `doctor` says so. Stored
  credentials, S4U, mapped drives, and network repositories are separate
  work if that limit ever needs lifting.
- **What an installed Copilot CLI looks like on Windows**, now that
  pinning refuses batch shims. If the npm package's global bin entry is
  a `.cmd` rather than an executable, the runtime has to pin the
  interpreter and entry script instead, and the adapter's `binary` grows
  a Windows spelling.

## Decision log

The log is this document's history. Entries are newest first, and each
records what was decided, what it replaced, and what measurement or
failure settled it. Superseded planning content - the implementation
order, phasing, and sizing estimates this document carried while the
work was in progress - was removed once complete; it remains in git
history.

### 2026-07-30: a fixture belongs to its run, and the sweep must not adopt one

The first fix for #232 gave the smoketest a hidden `--cleanup-only` mode
so a timed-out run could be cleaned up by the process that killed it.
The reasoning behind it did not survive being checked. A timeout writes
a `fail` verdict, and the smoketest gate only skips on a previous
`pass`, so the next hourly maintenance pass re-runs it and its preflight
cleanup removes the residue anyway. The cleanup mode was buying about an
hour of tidiness for a new CLI surface, a detached spawn, and a test.

What was actually wrong sat one layer up. Underscore-prefixed fixtures
are ephemeral by construction, and five places in the tree already knew
it - the ownership gate, registry pruning, the sweep's ownership-record
prune, and two `doctor` orphan reports. The watcher restart sweep was
the one place that did not, so a respawn entry a killed run left behind
read as durable intent. The sweep tried to restart a fixture whose
fixture directory was already half-removed, the restart failed, the
failure set `infra_ok` false, and `infra_ok` gates the smoketest. The
residue was suppressing the run whose preflight cleanup would have
removed it. That is not an hour, it is permanent, and it matches what
#232 observed: a beacon stuck at the prior failed verdict.

So the mode came out and `headless.is_ephemeral` went in, named once and
used at all six sites including the restart sweep. The rule is that a
fixture belongs to the run that created it: nothing host-scoped adopts
one, so residue is inert until the next run clears it. What remains of
the original fix is the part that was always right - temporary files
instead of pipes, so the wait ends on the process rather than on handles
a detached descendant inherited.

The general form, worth applying to the next platform defect: when a
fix needs a new mode to compensate for behavior elsewhere, check whether
the behavior elsewhere is the defect.

### 2026-07-30: the trigger track gets the seam the watcher track had

The watcher track has had the right shape since it landed: an
`EventSource` protocol, two implementations, and `watchpolicy` holding
every rule and knowing about neither. The trigger track only looked the
same. `wintasks` was a real store, but the crontab half never left
`headless`, so `schedules` had no POSIX module to name and repeated
`if native_scheduler() == TASK_SCHEDULER` sixteen times, once per
operation, reaching into private `headless` members on the other branch.

Extracting `crontasks` as the peer of `wintasks` removed all sixteen.
The two stores now answer the same questions with the same signatures,
`schedules` chooses once in `_store()`, and what a stored trigger looks
like - a crontab line, a registered task - never leaves the store. The
store-level questions that had accumulated in the dispatch point
(`current_form`, the maintenance trio, `persisted_roots`) moved into
both stores, because each was already a question about what the store
holds.

Nothing about the crontab lines changed in the move. The measurable
result is that adding a third store, or changing what one stores, is now
one module rather than an edit at every branch.

### 2026-07-30: decoding a child's output is a seam member, not a habit

`use_utf8_io` had covered this process's own streams since the console
work, but nothing covered the other direction. Around forty subprocess
captures across fifteen modules passed `text=True` and inherited the
locale encoding, which is UTF-8 on POSIX and the ANSI code page on
Windows: the same call reads correctly on one host and mojibake on the
other, and that is what made passing smoketest steps report failure.

The first fix was a file-local mapping in `smoketest.py`, which repaired
the harness and left the product alone. The rule that replaced it is that
no capture may rely on the locale: `hostruntime.CHILD_TEXT` states UTF-8
for every child this tool launches and configures, and a child that
writes something else decodes itself - `wintasks` already read `schtasks`
as `oem`, which is why the rule is "state it" rather than "always UTF-8".

An `ast` walk in the suite enforces it across the package: a
`subprocess.run` or `Popen` with `text=` and no `encoding=` fails. That
is one assertion for a whole class of defect, and it runs on Linux where
the defect cannot be observed.

The review that followed found the rule's first real exception, and it
was not on the Windows side of the tree. PowerShell writes the console
OEM code page into a pipe: `café` came back as two undecodable bytes and
an em dash was flattened to `-` inside the child, before any decoder
could have helped. That affected the WSL heartbeat, which reaches
`powershell.exe` over interop, and the process enumeration fallback.
Decoding as `oem` is not available to fix it, because the codec is
Windows-only and the heartbeat runs from WSL. Telling the child what to
write is, so `powershell_argv` prefixes every invocation with an output
encoding and is the only place that builds a PowerShell command line.

### 2026-07-28: an owner value names a runtime three ways, and matches on one

The generated identity of 2026-07-26 fixed Windows and left WSL broken in
exactly the way it described. `hostname_identifies_runtime()` answered
"yes" for every POSIX runtime, so a WSL distro owned agents under its
hostname - and a distro's hostname defaults to the Windows computer
name, which is not something the distro chose or a user typically
overrides. Two distros on one machine therefore computed the same owner
value and would each answer to the other's agents. The seam had asked
the right question and gotten a wrong answer for the one platform the
whole document is about.

The fix separates the two jobs an owner value was doing at once. It is
now `hostname/runtime/uuid`: the hostname and the runtime are read only
for display, the uuid is read only for matching. Neither half can spoil
the other, so a machine rename or a distro rename changes how a row
reads and never who owns it, and a table never has to show 32
characters of hex to be exact. The runtime part is `windows` or the
distro name, which is what finally distinguishes two WSL rows for a
human.

A single delimited string rather than a nested object, because the
registry backend validates owner values as `str` and raises
`OwnershipUnavailableError` on anything else - and that error is
fleet-wide abstention, so one dict-valued record would take the registry
offline for every machine, not just the one that wrote it.

The consequence was the interesting part. Every existing bare-hostname
entry stops matching, and the temptation was to write a migration that
recognizes and upgrades them. That was rejected for something with a
longer life: matching cannot produce a uuid from a bare hostname, and it
cannot produce one from a truncated write, a bad merge, a hand edit, or
a restored backup either. All of those are one category, and the rule
covers all of them at once - **if a value cannot be reduced to a uuid,
it is not this runtime's.** The agent goes inert here, its entry is left
for the machine that can prove the claim, and one deliberate
`start --name <agent> --transfer-here` fixes it. Bare hostnames are not
legacy values awaiting migration; they are the first instance of a
permanent state, and the code is smaller for treating them that way.

The one value that had to stay outside the rule is an absent one.
Absent means unclaimed and still runs here, which local mode depends on
entirely: `load_owners()` returns `{}` and every agent is absent. That
boundary - absent versus present-but-unmatchable - is the only place the
model is subtle, so it is asserted directly in the enforcement tests.

`--transfer-to` kept its meaning and gained `--transfer-here` beside it.
The old flag now needs a full triple, which is only obtainable by
copying it out of `agent-owners.json`; that is fine for the rare case of
assigning an agent to a machine you are not on, and unusable for the
common one. `--transfer-here` fills in the identity a runtime can always
name.

### 2026-07-28: WSL already ships the windowless launcher

The heartbeat's task action ran `wscript.exe` on a packaged VBScript that
called `WScript.Shell.Run(cmd, 0, True)`. That was two bets going bad at
once. VBScript is a Feature on Demand in Windows 11 and is being removed,
and persisted task definitions outlive the decision that wrote them. And
window style 0 is `SW_HIDE`, which asks for a console and then hides it -
so whether anything appears depends on the default terminal application,
and Windows Terminal reopens it somewhere visible.

`pythonw`, the native side's answer, is not available here: the heartbeat
is registered from inside the distro, and a WSL-only install has no
Windows Python to point at. Three other options were measured on a real
host and rejected:

| Rejected | Measurement |
|---|---|
| `conhost.exe --headless <command>` | Returns in about a second, exit 0, and the command never runs. It expects to be driven as a pseudoconsole host, not used as a launcher |
| A non-interactive principal (`-LogonType S4U`) | `Register-ScheduledTask` fails with "Access is denied" unelevated, and the heartbeat installs as an ordinary user |
| A resolved Windows `pythonw.exe` | Present only if the developer also installed Python on Windows, which a WSL deployment has no reason to have done |

The answer was already installed: `wslg.exe`, the GUI-subsystem build of
`wsl.exe` that WSL ships to start Linux GUI programs. Windows gives a
GUI-subsystem process no console at all, so there is no window to hide
and nothing for a terminal application to reopen. It needs no packaged
file, no `\\wsl.localhost` path in the action, and no second runtime. It
is not on `PATH`, so it is looked up where WSL installs it -
`%ProgramFiles%\WSL` for the MSI package, the `WindowsApps` execution
alias for the Store package - and the result is cached for the process.

Two properties of it are worth writing down. It takes its own options
from the Windows command line and hands everything after `--` to the
distro's shell as written, so the action's argument string is quoted by
two different rules in its two halves. And it does not dependably report
the Linux command's exit status: a task firing `exit 9` was observed
returning both 9 and 0. Nothing reads that status. The beacon file the
heartbeat writes is the health signal, `doctor` compares its age, and
that is unaffected.

The old shapes are not migrated in place, because a task definition is
the developer's to keep: `doctor` names each superseded launcher for what
it was and points at `agents-live heartbeat install`, which converges the
action on the next run.

### 2026-07-27: this document became an architecture guide

While the work was in progress, this was a proposal: what native Windows
support would take, in what order, at what cost. Every step in that order
is now implemented and under CI, which made the plan sections a record of
a road already travelled rather than a description of the system.

They were removed rather than marked complete. A reader arriving at this
document wants to know how Windows support works and why it is shaped
the way it is; a fourteen-step order, a phasing table, and a line-count
estimate answer none of that, and each one costs the reader a paragraph
of orientation before they learn anything. Their content is not lost:
git history holds every revision, and the decision log below holds the
reasoning that outlived them.

What stayed is what still describes the system: the platform boundary,
the seam and why it is functions rather than an object, the two dispatch
mechanisms, the ownership grammar, the path rules, and the security
model. Those are architecture. The plan was scaffolding.

### 2026-07-27: the suite was written by a POSIX host, and it showed

Running the suite natively on Windows for the first time produced 40
failures out of 352 tests. Almost none of them were Windows defects in
the product. They were assumptions the suite had never had to state,
because the only host that had ever run it agreed with them.

Four of them were worth fixing in the product rather than the tests:

- A repo-relative path came back from `select_batch` spelled with
  backslashes. That path is an identifier, not something handed to the
  filesystem: it keys the content-hash cache, names files in the log,
  and is read by the agent. `should_ignore` already compared it with
  `as_posix()`, so the two halves of one module disagreed. Forward
  slashes everywhere.
- `Path("/tmp/agents").is_absolute()` is `False` on Windows, and so is
  `Path("C:agents")`. The repo-relative guard on `agent_directories`
  used `is_absolute()`, so both spellings walked straight past it.
  `anchor` catches all three forms on both platforms.
- A watcher's argv was matched against its repository with a string
  prefix and `os.sep`. Windows accepts either separator for the same
  file and compares them case-insensitively, so a loop launched with
  forward slashes was not recognised as belonging to the repository
  that started it. `Path` containment answers the question the check
  was actually asking.
- Two health-check tests read the developer's real crontab. On Windows
  there is no such command and the sweep crashed; on Linux the result
  quietly depended on whose machine ran it.

A fifth arrived only once CI ran, because the local host was on Python
3.13 and the runner resolved the package's minimum, 3.12. `os.fchmod`
does not exist on Windows before 3.13, so every atomic write that asked
for a mode raised `AttributeError` there - and the cleanup path then
failed too, because it unlinked a temporary file whose descriptor from
`mkstemp` nobody had closed, which Windows refuses. The permission is
now set by descriptor where the host has one and by path otherwise, and
the cleanup closes before it unlinks. The lesson is that the matrix has
two axes: the host that exposed the first four, and the interpreter
version that exposed this one.

The rest were the suite's own POSIX habits: crontab lines built by
interpolating a root that `shlex.split` then ate the backslashes out of,
`Path.home()` redirected by setting `HOME` but not `USERPROFILE`, a
virtualenv interpreter looked for in `bin` rather than `Scripts`, and a
test that chdir'd into a temporary directory and only left it after the
directory had been deleted - which POSIX permits and Windows does not.

Some tests stay skipped on Windows, and deliberately.
`TestCrontabConvergenceBehavior` drives a real `crontab` process from a
shebang script, which `CreateProcess` cannot run and which no Windows
host would dispatch through anyway; the Task Scheduler branch it would
otherwise cover has its own tests. Three more run something under
`bash`: the WSL compatibility wrapper, which is a POSIX script a WSL
crontab executes inside the distro, and the generated bash completion.
A bare `bash` on a Windows PATH is as likely to be the WSL launcher as
a shell - which is exactly how CI failed while the local host, with Git
Bash first on PATH, passed - so what those tests measure there is the
host, not the artifact. The command-line round-trip test for a
repository path containing a space runs only on Windows, because only
Windows reads a command line back through a quoting parser.

The lesson is narrower than "test on both platforms". It is that a
fixture which hand-builds a string the product elsewhere builds with a
quoting function has silently forked the format, and the fork stays
invisible until a host turns up whose paths contain characters the
format has to escape.

### 2026-07-25: a detached child is the one thing a spawn must not be

Every detached spawn flashed a console window on the desktop. Starting a
watcher opened a window that shut again a moment later, and a test run
that spawns processes produced a burst of them. The flags asked for
`DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW`, which
reads as "no window" and is not: Windows ignores `CREATE_NO_WINDOW`
whenever `DETACHED_PROCESS` or `CREATE_NEW_CONSOLE` is also set, so the
one flag naming the symptom was the one flag with no effect.

Measuring settled it. The same child, spawned under each flag set,
reported `GetConsoleCP()` and `GetConsoleWindow()`:

| creation flags | code page | console window |
| --- | --- | --- |
| `DETACHED_PROCESS \| CREATE_NEW_PROCESS_GROUP \| CREATE_NO_WINDOW` | 437 | non-zero |
| `CREATE_NEW_PROCESS_GROUP \| CREATE_NO_WINDOW` | 437 | 0 |

Detaching does not mean "no console". It means "not the parent's
console", and Windows supplies a new one with a window attached.
`CREATE_NO_WINDOW` on its own gives the child a console of its own that
is never drawn, which is what the spawn wanted all along, and which
descendants inherit rather than allocating another. Process group
isolation is the flag that was actually carrying the detachment, and it
stays.

Sampling visible top-level windows every 10 ms is what made this
provable rather than anecdotal: a flash is over long before a one-second
poll notices, and the failure had been mistaken for the scheduled tasks
and then for `uv` more than once. Before the change, three runs of the
process tests opened sixteen windows; after it, those runs and a full
351-test run opened none. The same four tests also went from about 1.1 s
to about 0.37 s, because allocating a console and handing it to the
default console host was most of what they were doing.

The regression test asserts the pair that distinguishes the two states:
a non-zero code page, so the child still has a console to hand down, and
a zero window handle, so nothing is ever drawn.

### 2026-07-25: a queue that cannot grow, and a batch that cannot either

The watcher had two unbounded places between the kernel and an agent.
The queue the reader threads write to had no maximum, so a storm large
enough to outrun the loop grew it until the machine ran out of memory;
and a batch selected by policy had no maximum either, so a rescan could
hand an agent two thousand file names in one prompt.

Both are bounded now, and both bounds degrade to something already in
the design rather than to a new behavior. A queue that is full drops the
event and records the drop, which the source then treats exactly as it
treats a kernel buffer overflow: one bounded rescan of the watched
directories, a superset of what was lost. A batch past its limit is cut
and the number left out is logged.

The reader never blocks waiting for room. Blocking looks like the safer
choice and is the worse one: a reader parked on a full queue has stopped
calling `ReadDirectoryChangesW`, and records that arrive with no read
pending are dropped by the kernel anyway. Blocking would lose the same
events and hide the loss.

### 2026-07-25: registration verifies what the store kept, not that it kept something

Registering a task wrote a definition and read it back to prove the
write landed. That proved a task exists, not that it is the right one.
The read-back now has to match the action that was asked for and fire on
the schedule that was asked for.

This is where an update interrupted partway through becomes visible, and
it is also where the class of bug that produced the `PT60M` loop gets
caught: a schedule this tool can write but cannot read back as the same
thing used to look like a clean registration that every later
maintenance pass rewrote, forever. Failing at the moment of writing
costs one loud error instead of an invisible rewrite every fifteen
minutes.

Installation also registers before it removes. An agent whose set of
schedules changes needs both a write and a delete, and an interruption
between them leaves the store holding one side. Registering first means
the surviving state is an extra task, which is visible and converges on
the next pass; deleting first means the surviving state is an agent with
no trigger at all, which is silence.

### 2026-07-25: a scheduled run needs somebody signed in, and says so

Tasks are registered with an interactive token, which is what makes a
scheduled agent behave like a cron job the developer started: their
environment, their credentials, their agent CLI logins. The price is
that nothing runs while the machine sits at the sign-in screen. Running
logged off means stored credentials or S4U, a different security posture
than the one in the security model above, and still not this one.

The limit is now stated by `doctor` rather than left to be discovered
from a run that never happened, and the same check fails when a task has
been given a different logon type by hand. It compares only the logon
type: the store rewrites the user it was given as a SID, so the name
written and the name read back are never the same string even when they
name the same person.

### 2026-07-26: a task action names a program that has no window

A scheduled task runs with an interactive token so the agent gets the
developer's own session, environment, and credentials. The cost of that
choice is that Windows draws a console window for any console program
started as the action, once per fire, on top of whatever the developer
was doing. Four agents on five-minute schedules made the point better
than any argument could.

Task Scheduler has no setting for this. `Hidden` hides the task from the
scheduler's own list, not the window, and the settings that do run
invisibly all run in session 0, which is the logged-off execution that
this design deliberately deferred.

So the action names a program with no console to show, and that program
starts the real one hidden. The interpreter that is already installed
beside the pinned executable answers: `pythonw` is a windowless build of
the same Python, and [hidden.py](../src/agents_live/hidden.py) is eleven
lines that start the command with `CREATE_NO_WINDOW` and exit with its
status. The WSL heartbeat has the same problem and cannot use the same
answer, because it is registered from inside the distro and there is no
Windows Python to assume; the entry below settles that half.

The indirection is not incidental. Under `pythonw` there are no standard
streams at all, so running the CLI directly there would fail on its
first write; started from the launcher it gets a console of its own that
simply is not drawn, and its output behaves exactly as it does anywhere
else. The argument vector stays a vector the whole way, so the quoting
round trip still verifies the command that will run.

Ownership was the one thing that could have quietly broken. `_is_ours`
asks whether the action is an `agents-live` command working in this
repository, and the action is now an interpreter. It reads back through
the wrapper to the program that finally runs, and anything it does not
recognise as its own wrapper is taken at face value, so an interpreter
running something else stays exactly as foreign as it was.

### 2026-07-26: doctor names the mechanism, not the Linux tool

Doctor checked for `crontab` and `inotifywait` by name on every host, so
a working Windows machine failed two required checks and was told to
install them with `apt`. The check that matters is not "is this program
present" but "can this host schedule and can it watch", which is the
question `preflight` already answers through the capability seam. So the
mechanism checks ask `preflight` and report the name the host actually
uses, Task Scheduler and directory change notification on Windows,
`crontab` and `inotifywait` elsewhere, with a fix line that names a
package manager that exists there. The same reasoning retired
`python3.12 (via uv)`: the Microsoft Store installs a `python3` alias
that is not Python, so the probe asks for `python` on Windows.

### 2026-07-26: the check-and-repair loop is a trigger like any other

The loop installed itself by writing crontab lines directly, which is
why `upgrade` failed on Windows with `crontab: command not found`. It is
now a `TriggerSpec` with its own kind, dispatched through
[schedules.py](../src/agents_live/schedules.py) like every other
trigger, so it lands in whichever store the host keeps schedules in.

Its root is the state directory rather than a repository. That is what
makes it nameable on Windows: a task is `<agent>@<digest of root>`, so
the loop becomes one `maintenance@<digest>` task, and the ownership
check has a working directory to verify against. It carries both its
startup and its hourly trigger in that single task, because the loop
does the same work either way and has no `--boot` to tell them apart.

The name also carries a kind of its own, `.host`. Registered as an
agent's task it was destructive: the loop's first pass reads every
registered task, finds one whose agent file does not exist because
nothing in any project defines it, and prunes it - the loop deleted
itself on every run, and its state directory was swept as if it were a
project. A kind that says "this belongs to the tool" is what keeps the
enumeration a repository does from ever seeing it.

### 2026-07-26: convergence compares a form, not a document

Upgrade converges persisted entries when the pinned shim path moves. On
crontab that is a string comparison against the rendered line. A task
has no such line, and comparing the registered XML would report a change
on every run: the document carries a start boundary computed when it was
written, so two identical registrations differ by their timestamps.

So a task is compared as a form: the command, the argument string, and a
canonical signature of what it was asked to fire on. The signature is
built from the schedule and read back out of the registered document
with the boundary date normalized away, which makes a round trip that
loses a schedule a test failure rather than an infinite convergence.

Reading it back means reading what the store chose to write, not what it
was given. An hourly repetition is registered as `PT60M` and returned as
`PT1H`: matched only as minutes it reads as a task that lost its
repetition, and convergence rewrites the task on every pass forever. The
read-back parses the whole duration, so the comparison is between what
the two sides mean rather than how each spells it.

### 2026-07-26: in JSON mode the whole of stdout is the document

`doctor --repair` builds its plan by running `internal migrate
--dry-run` and parsing the result, and on Windows it failed with
`invalid trigger migration plan`. The cause was older than Windows:
`migrate` printed its human narration before the document, so the JSON
mode emitted a stream no parser could read, and only Windows had a
caller that parsed it.

A command in JSON mode now says nothing except the document. The
narration is not lost - it says what the plan already carries, and a
developer reading it runs the command without `--json`.

### 2026-07-26: `--adopt` is refused where a name carries its root

`migrate --adopt <old-root>` rewrites entries left behind by a project
that moved. It works on crontab because a line carries its root as text
and can be found by reading it. A task is found by a name that digests
its root, and a root that is gone cannot be digested, so there is
nothing to look up. Rather than fail obscurely, `--adopt` on a task host
refuses with the reason and the alternative: run `start --all` from the
new location, which registers the moved project under its new name.
Entries pinned to the old root then surface in `doctor`, which reads the
task folder rather than asking for names, and are the only place they
can surface at all.

### 2026-07-26: `ReadDirectoryChangesW` directly, measured before chosen

The spike answered the questions the recommendation asked it to answer,
and every answer favored option A.

Cancellation is clean: an overlapped read parked in
`WaitForSingleObject` ends promptly when another thread calls
`CancelIoEx`, and `GetOverlappedResult` then reports
`ERROR_OPERATION_ABORTED`, which is a stop rather than a fault. Renames
arrive as a pair of records, the old name and the new one, both relative
to the watched root, so the loop sees two changed paths, which is what
the debounce already expects from a move on Linux. Overflow is
unambiguous: the API reports zero bytes returned rather than a partial
buffer, so a dropped batch cannot be mistaken for a small one. Deleting
the watched root fails the read with access denied, which the loop
reports as a watch that ended rather than as an event.

None of that needed a second implementation to interpret. Building
`watchdog` to compare against would have measured a wrapper over the
same call, and adopting it would have added a runtime dependency and a
second event model for no reliability the spike could not already see.
So the comparison stopped at the point the answer stopped moving.

Two details cost more than expected. `ctypes.wintypes` is unavailable on
Linux, so the module declares its own `DWORD` and `HANDLE` aliases and
imports everywhere, which is what lets the record parser be tested off
Windows. And `wstring_at` decodes with the platform's `wchar_t`, four
bytes on Linux, so the parser reads the change records with
`struct.unpack_from` and an explicit UTF-16LE decode instead of trusting
the host's idea of a wide character.

### 2026-07-26: overflow degrades to one bounded rescan

When the kernel drops changes, the watcher does not ask what it missed;
it lists the watched directories once and treats every file it finds as
changed, then goes back to reading events. The list is capped, at two
thousand files, and the cap is a constant rather than a setting because
the number that matters is "small enough that a rescan cannot itself
become the storm".

The alternative, retrying the read immediately in the hope of catching
up, cannot work: the dropped records are gone, and under a storm the
retry overflows again. One bounded rescan converts an unbounded unknown
into a bounded known, and the debounce and the hash filter that already
sit downstream discard the files that did not really change.

### 2026-07-26: the watcher reads through an event source, not a pipe

`watch_loop` used to own a subprocess, a non-blocking pipe, and a
`select` call. It now owns an `EventSource`: `start`, `poll(timeout)`
returning a batch of paths, `stop`, and a `WatchFailed` exception for
the end of the stream. `inotifywait` moved behind that protocol
unchanged, and the Windows implementation appears as a peer rather than
as a branch inside the loop.

The loop's debounce, collection window, cascade guard, and fire-rate
breaker did not change, which is the test of the seam: the WSL suite
stayed green through the extraction, before any Windows code ran through
it.

Two small platform facts surfaced with it. `SIGHUP` does not exist on
Windows, so the shutdown handlers register by name and skip what the
platform does not define. And a blocking `poll` has to be woken by
`stop`, so stopping puts a sentinel on the queue rather than relying on
a timeout to notice.

### 2026-07-26: the capability is `watch`, and a watcher is found by query

Two things broke on the first live `start`, both because a capability
was named after its Linux implementation.

The preflight probe asked whether `inotifywait` was on PATH before any
command that might watch, which no Windows host can satisfy. The
capability is now `watch`, and the probe asks the host: `kernel32` must
load and expose `ReadDirectoryChangesW` there, `inotifywait` must be
present here. Naming the capability after the need rather than the tool
is the same move the rest of the seam makes.

Then finding a running watcher failed, because both implementations read
process command lines from `/proc` or `ps`, and Windows has neither. The
host runtime now answers "what is running and how was it started" as a
query, through a CIM lookup on Windows and `ps` on POSIX, and the
argument splitting follows the platform's quoting rules rather than
splitting on spaces. The executable check compares the stem of the
program name, because the same watcher is `agents-live` on one host and
`agents-live.exe` on the other.

### 2026-07-26: the coarse trigger is a repetition on a divisor of the hour

The superset rule turned out to be one line. Take the minutes of the
hour an expression can name, take the greatest common divisor of their
offsets from the earliest, fold it against 60 so the repetition keeps
its phase from hour to hour, and repeat on that step. Every minute the
expression names is covered, the step is the widest one that covers
them, and nothing about the other four fields has to be modelled at all
because the dueness predicate reads the whole expression anyway.

`*/7` is the honest illustration: seven does not divide sixty, cron
restarts the step every hour, so the covering step is one minute. That
schedule costs sixty process starts an hour to run at most nine times,
which is the price of coarseness and an argument for keeping supersets
tight, not for refusing the expression.

Reading cron expressions moved into `triggers.py`, next to the schedule
vocabulary rather than beside either dispatch mechanism. The predicate
is not a Windows concept; it is what a crontab already does for us on
the other host.

### 2026-07-26: dueness claims the minute, and claims nothing else

`claim_due_minute` answers one question about one moment and writes one
file: the minute it just allowed, per agent, under the repository state
directory. A repetition that fires twice inside a matching minute
therefore runs the agent once.

What it deliberately does not do is anything a scheduler does. There is
no run queue, no catch-up policy, no drift compensation, and no memory
beyond the last allowed minute. Missed starts after sleep,
daylight-saving folds and gaps, and restart behavior stay with Task
Scheduler, which is the only component that knows about them. A run
that cannot write its marker still runs; the only thing lost is the
same-minute guard, which is a smaller failure than declining a real
firing time.

On a crontab host the predicate returns true without looking at
anything. Cron fires only at firing times, and asking again would be a
way to disagree with it rather than a safeguard.

### 2026-07-26: `@reboot` is a logon trigger in a task of its own

Two findings, one from the design and one from the machine.

A task carries one action, and the dueness check has to treat a startup
fire differently from a clock fire: the startup trigger is exact, while
the repetition is not. Rather than infer which trigger fired, an
agent's `@reboot` schedules register as a separate task whose action
carries `--boot`. The task name gets a `.boot` suffix, and enumeration,
ownership, and removal read through it, so one agent stays one agent
everywhere above the task store.

Then registration failed with "Access is denied". A `BootTrigger`
requires elevation, which a user-scoped tool does not have and should
not ask for. A `LogonTrigger` for the owning user registers without it,
and it is the closer match regardless: the task runs with an
interactive token, so what it actually needs is a session, not a boot.
The observable difference is that an agent resumes when the developer
logs on rather than when the machine powers up, which for a tool that
runs a developer's agents in a developer's session is the behavior
`@reboot` was standing in for.

### 2026-07-26: scheduling dispatches through one module, not a runtime object

The seam extracted in step 3 anticipated a `PosixRuntime` holding the
crontab members and a `WindowsRuntime` holding their Task Scheduler
counterparts. Step 9 did not build that. It added
[schedules.py](../src/agents_live/schedules.py), a module with
`install`, `remove`, `is_active`, and `installed_names`, which asks
`hostruntime.native_scheduler()` once and calls either the existing
crontab primitives or the new [wintasks.py](../src/agents_live/wintasks.py)
leaf.

The crontab primitives were already free functions with no state
between them, and the Windows side has none either: a task store is not
something a process holds open. A class would have been a namespace
with a constructor, and every call site would have had to acquire an
instance before asking a question. The module is the same dispatch with
less ceremony, and it moves the same amount of platform knowledge out of
the call sites, which was the point of the seam. If a future member does
need state, the module can hand one out then.

Three consequences worth recording. First, the crontab lock, read,
filter, and write sequence moved out of `activate` into the POSIX branch
of `schedules.install`, so `activate` now loads config, validates
handler paths, and delegates. Second, the CLI capability formerly called
`crontab` is now `schedule`, and its preflight probe checks `schtasks`
on Windows and the `crontab` binary elsewhere; the old name failed every
Windows `start` before any work began. Third, the smoke suite pins
`native_scheduler` to crontab in its temp-project base class, because
the crontab-shaped assertions predate Windows and, unpinned, registered
real tasks on the developer's machine.

### 2026-07-26: exact translation now, coarse triggers with step 10

The design says translation produces a superset and a dueness predicate
declines the fires that are not due. Step 9 implemented only the exact
half and refuses the rest.

A superset without the predicate is not a partial implementation of the
design; it is a different behavior, one that runs agents at times their
schedule does not name. Refusing a schedule tells the developer exactly
what is not supported yet. Accepting it silently would not. The refusal
message names step 10 so the limitation reads as a stage rather than a
verdict.

### 2026-07-26: what a Windows task read-back can and cannot verify

Registration verifies itself by reading the definition back, and two
limits of that check are worth stating.

`schtasks /query /xml` writes through the console code page, so a path
outside that code page comes back with replacement characters. Rather
than guess, `_is_ours` treats any replacement character as a failed
check: an unverifiable task is one this tool will not touch. A
non-ASCII repository path therefore cannot be adopted or removed by
name today, which is a real gap and a better one than deleting the
wrong task.

`schtasks` also distinguishes a missing task from a missing folder by
wording alone, "cannot find the file" against "cannot find the path",
and the first registration in a fresh install hits the second. Matching
only the first turned an ordinary empty-folder state into a hard
"Task Scheduler unavailable" error. The check now matches the shared
prefix.

The principal is an interactive token at least privilege, so a task
runs only while the owning user is logged on and each run flashes a
console window. That is the honest default for a tool that runs a
developer's agents with the developer's credentials; a service account
or stored password would buy background execution at a cost this
trust model does not want to pay.

Decisions that changed the approach recorded above. The document states
the current approach; this log says when it changed and why.

### 2026-07-26: a Windows runtime is named by what it generated, not by the machine

Step 8 gave the Windows runtime an identity. Ownership matching had one
vocabulary, the hostname, which works while a machine hosts one runtime
and stops working the moment it hosts two: a Windows installation and
the WSL distro beside it report related names and would answer to each
other's. So `current_host` became what it always was in the log payloads
and beacons, a display label, and `current_owner_id` became the value
every ownership decision compares against.

The split is worth more than the Windows case. Five call sites -
activation, dispatch, two health-check sweeps, and the dashboard - each
read "the host" and each meant "the thing an owner value names". They now
say so, and the two concepts can differ on a host where they must.

The seam answers the question rather than the platform: nothing outside
`hostruntime` asks whether this is Windows, only whether the machine
name identifies the runtime. Where it does not, the identity is
`<runtime>:<uuid>`, generated once into the user state home with an
exclusive create so two commands racing on first use settle on one
identity. A file that exists but holds something else raises
`OwnershipUnavailableError` rather than regenerating: a new identity
would silently unclaim every agent the old one owned.

The state home moved on Windows in the same step, from the XDG spelling
to `%LOCALAPPDATA%`, because that is where the identity lives and
because a roaming profile would otherwise carry one machine's runtime
state to another. An explicit `XDG_STATE_HOME` still wins everywhere,
which is what lets a test point the whole tree at a temp directory.
This was a clean break, taken while the only Windows state was a scratch
project's logs.

### 2026-07-26: a Windows run builds its environment and inherits its PATH

Step 7 ran an agent to completion on a native Windows repository. Four
things about launching a CLI there differ from the POSIX path, and all
four are seam members rather than branches in the run path.

A Windows child cannot start without `SystemRoot`. The POSIX path hands
an agent `HOME` and a constructed `PATH` and nothing else; doing the
same on Windows produced exit status 3221226505 (`STATUS_STACK_BUFFER_OVERRUN`)
with nothing on either stream, because the loader could not find the
system DLLs. `base_env` supplies the platform floor - `SystemRoot` first,
then the profile, temp, shell, and processor variables a Windows program
expects to read - and the run path keeps building the environment rather
than inheriting one.

PATH is inherited on Windows and constructed on POSIX. Cron hands a job
almost nothing, which is why `clean_path` exists; Task Scheduler runs a
task with the owning user's environment block, so the inherited PATH is
the same one an interactive run sees. Dropping it would lose every
per-user install location a Windows CLI actually lands in - winget
shims, npm's global bin, nvm4w - for no gain, so `inherits_path` says
yes there and `find_tool` (the off-PATH search for `uv` and `node`)
returns nothing, because `shutil.which` has already looked everywhere it
would.

What is lost that way is PATH hygiene, and pinning replaces it.
`CreateProcess` resolves a bare command name against the launching
process's PATH, not the environment handed to the child, so a name pins
nothing on Windows regardless. `pin_executable` resolves argv[0] to an
absolute executable before launch. It searches PATH and PATHEXT in order,
skipping a `.ps1`, which Windows cannot execute, and `.bat` or `.cmd`
shims, which Windows runs through `cmd.exe` - a second parse of the
argument string where a prompt body carrying `&` or `|` would run as a
command. It fails closed when only those shims answer to the name. POSIX
returns the name unchanged, where `execvp` searches the child's own PATH
and the constructed PATH is the pin.

Output has to be UTF-8 on purpose. A Windows console defaults to a
legacy code page and Python follows it, so `agents-live logs` died with
`UnicodeEncodeError` on the box-drawing characters in its own table. The
project's own console output is ASCII by policy, but that policy cannot
reach the two sources that matter here: DuckDB renders the log table,
and the agent's answer is whatever the model wrote. `use_utf8_io`
reconfigures this process's streams, exports `PYTHONUTF8` so the
subcommand scripts the CLI launches start the same way, and switches the
console code page for the life of the run, restoring it on exit because
the console belongs to the shell rather than to the run.

The first run that proved this used the `claude` runtime, which installs a
native executable. A later native installation showed the mainstream
Copilot layout directly: VS Code's script and batch shims precede the
WinGet `copilot.exe` on PATH. Pinning must continue past those refused
entries to reach the installed CLI. Full Windows orchestration remains the
release proof for cancellation, timeout, expired credentials, and streaming.

### 2026-07-26: locking is a file lock and termination is a tree walk

The step 6 spike measured named mutexes, `LockFileEx`, job objects, and
Toolhelp32 snapshots on a native Windows host as a standard
non-elevated user. Two predictions in this document did not survive it.

A named mutex is the wrong lock. `Local\` names resolve per logon
session, so an interactive process and a session 0 process would each
believe they held it; the mutex is thread-owned, so a lock cannot be
released by a different thread than took it; and a holder that dies
leaves `WAIT_ABANDONED` for the next waiter to interpret rather than
simply releasing. `LockFileEx` has none of those properties and matches
`flock` closely enough that one contract covers both platforms: a
crashed holder leaves no lock, a second handle in the same process is
excluded, and there is no thread affinity. Windows file locks are
mandatory, so `exclusive_lock` locks a single byte at offset 2**62,
which excludes other holders while leaving the file's owner metadata
readable by the process that lost the race.

A job object cannot carry process-tree identity. Reopening one by name
after its creator exits fails with `ERROR_FILE_NOT_FOUND` while the
whole tree is still running, so `terminate` cannot be "reopen the job
and terminate it". Termination instead enumerates descendants from a
Toolhelp32 snapshot before terminating the root, since terminating the
root first orphans its children and loses the parent links that identify
them. A snapshot walk costs about 16 ms, against roughly 1.9 s for the
`Win32_Process` query that would supply command lines, so identity
verification for step 12 has to come from something cheaper than a
command-line match.

`find_process` is deliberately not written yet. POSIX finds a watcher by
scanning argv, and Windows cannot afford that scan, so the two platforms
disagree about what the lookup even takes as input. Writing it now would
encode a guess; it waits for step 12, where the PID record and identity
check it needs exist.

### 2026-07-25: the seam is extracted on Linux before Windows begins

An earlier draft placed "extract the host runtime" after the Windows
foreground, scheduler, and watcher prototypes, so that the interface
would generalize only what two implementations had proven. That order is
withdrawn. Extraction now happens on Linux and WSL first, because a
refactor that touches eight modules needs a platform where a regression
is visible immediately, and because two of the four tracks pay for
themselves there regardless of whether Windows ever ships: `TriggerSpec`
removes a cron-line-string leak spanning `activate.py`, `headless.py`,
and `migrate.py`, and splitting `watch_loop` makes debounce, the cascade
guard, and the fire-rate breaker testable without a live `inotifywait`.

Three conditions come with the reversal. The `fcntl` imports at module
scope are moved first, since the package does not import on Windows at
all today. Behavior-pinning tests for crontab convergence and the watcher
loop are written before the extraction, because the current suite mocks
`current_crontab_lines` and `install_crontab` and never spawns
`inotifywait`, which means it stubs precisely the subsystems the seam
replaces. And the two members whose semantics the platforms define
differently, `exclusive_lock` and `terminate`, wait for a throwaway
Windows spike rather than being derived from `fcntl.flock` and
`os.killpg`; the original objection to speculative interfaces is correct
for those, and only those.

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
blocked. Windows reports no shell interpreter, so `.sh` and unrecognized
extensions are refused at dispatch. There is no Git Bash probe or
configured-interpreter escape hatch; portable examples use Python.

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
