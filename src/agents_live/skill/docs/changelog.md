# Changelog

Reverse-chronological log of significant changes, newest first. The
changelog starts at the initial public release; earlier development
history is retained in the source repository.

## Unreleased

- feat: native Windows installs generate PowerShell completion. (#233)
  It sits alongside the Windows-local Bash and Zsh files. Linux and WSL
  continue to install only Bash and Zsh completion in their own XDG data
  home. Neither runtime probes or writes the other runtime's files.
- fix: an upgrade refuses rather than half-rebuilding an installation in use. (#231)
  Windows will not let uv replace a file another process holds open, and uv
  discovers that only part way through rebuilding the tool environment, which
  removed a plugin and left the runtime on the previous version. The upgrade
  now names the watchers and dashboards running out of the installation and
  changes nothing until they stop.

## 5.4.2 - 2026-07-29

- fix: native dashboards no longer reject their own loopback server. (#228)
  NiceGUI re-executes the dashboard script to build the root page after its
  server starts. That page execution now skips launch-only port checks and
  registry writes, preventing the false conflict and its shutdown traceback.
  The framework smoketest now requests the rendered root page as well as the
  agent API so this script-mode path stays covered.

## 5.4.1 - 2026-07-29

- fix: detached update checks no longer lock the invoking directory. (#224)
  Interactive commands can start a background PyPI version check. On Windows,
  that child inherited the project as its working directory and could briefly
  prevent a temporary or disposable project from being removed after the
  command finished. The host-scoped check now runs from the user home.
- fix: release publication runs the canonical gates with complete dependencies. (#218)
  The publish workflow now delegates its audit, suite, and build commands to
  the release tool instead of maintaining a second copy that can drift. This
  prevents a tagged release from passing locally but failing before PyPI when
  a test dependency is added to only one workflow.
- fix: legacy Windows clock tasks repair themselves before over-firing. (#194)
  A task written before scheduled invocations carried `--scheduled` could
  bypass both the dueness check and the duplicate-fire claim, turning every
  coarse Task Scheduler wake into an agent run. The first ambiguous invocation
  now rewrites that task and skips once. A manual command explains the repair
  and can be repeated immediately; quiet scheduled invocations remain silent.
- fix: wheel metadata explicitly declares every packaged runtime asset. (#214)
  The skill payload and Windows heartbeat script no longer reach the wheel
  only as a side effect of being tracked by Git. The obsolete line-ending rule
  for the removed Windows Script Host launcher is also gone.
- fix: `uninstall` removes the tool without stranding host state. (#219)
  Three things outlived it. A running watcher holds the executables uv
  has to delete, so the removal failed on Windows, and it failed after
  the heartbeat, the check-and-repair loop, and the completions were
  already gone. Per-agent triggers were never withdrawn at all, so every
  scheduled task and crontab entry kept firing on schedule at an
  executable that was no longer there. And the uninstalling command is
  itself running out of the environment being deleted. Uninstall now
  stops its own watchers first and refuses to remove anything if one
  survives, so a failure leaves a working installation to retry from; it
  sweeps the triggers host-wide across every project, which is the only
  way to reach entries pinned to a project that has since been deleted;
  and on Windows it hands the final removal to a helper that waits for
  both this process and its launcher to exit. Throughout, a watcher or a
  trigger is claimed only when it runs out of the installation being
  removed: anything aimed at a source checkout still works afterwards
  and is left alone.

## 5.4.0 - 2026-07-28

- fix: a credential on a command line no longer reaches `admin.log`.
  (#212) Every administrative event records the invoking argv, so a
  state change traces back to the cron entry, CLI invocation, or agent
  that caused it. It was recorded verbatim, which meant a token or
  password given as an argument was written to a host log in plaintext
  and printed back by `agents-live logs` on demand. The value after a
  credential flag, and the value half of a `--flag=value` form, are
  replaced with a placeholder; the flag stays, because the shape of the
  invocation is the diagnostic and the value never was.
- fix: a spawned agent is judged by its status, not by how long it ran.
  (#211) The dispatcher waited a second and a half and reported any
  child that had already finished as having died, whatever its status,
  which its caller reads as a failed dispatch. The runs most likely to
  finish inside that window finish on purpose: a pre-processor that
  returns skip, an agent this host does not own. So whether a dispatch
  counted as successful came down to a race between the host's speed
  and a sleep, passing on a slow machine and failing on a fast one.
  Only a non-zero status is a failure now. The log handle opened for
  the child is also closed in the parent, where it had been leaking for
  the life of the caller.
- fix: the export-clean gate reads path names, not just file contents.
  (#213) It scanned the inside of known text files, so a personal path
  in a name shipped whatever the file held; two files named for
  absolute temp paths once survived several releases that way. It also
  could not see a WSL home reached from Windows, because that form is
  backslash-separated and the pattern was written for POSIX, and its
  exclusions compared forward-slashed patterns against a backslashed
  path, so the runtime log and data directories were skipped on POSIX
  only.
- fix: a scheduled agent that declines a fire says why. (#187)
  A native trigger can be coarser than the expression it came from, so
  the dispatcher checks each fire and declines the ones that are not
  firing times. The decline reached the structured log and nothing
  else, so running a scheduled agent by hand outside its firing minute
  printed nothing and exited 0, which reads as a completed run. The
  line is subject to `--quiet`, which every persisted scheduled
  invocation carries, so cron mail and Task Scheduler see what they saw
  before.
- perf: the Windows process table is read without PowerShell. (#168)
  Every question about whether a watcher is running comes back to this
  one read: `status`, the dashboard, the health loop, `stop`, the
  orphan sweep, and an upgrade naming what it left behind. Asking CIM
  for it meant starting PowerShell, which cost seconds and, after #165
  made the read happen once per pass rather than once per agent, was
  most of what a dashboard page build spent its time on. The pids now
  come from a process snapshot and the arguments from `ntdll` directly.
  Measured on a host running about 260 processes: 35ms against 1.3s
  warm, and against 3.0s when PowerShell is cold. The two readers were
  compared live on the same host: 260 processes seen by both, with
  identical text for every one of them, and the same answer for which
  processes are ours. The direct read
  also recovers command lines CIM reports as null. `ntdll` is not a
  contract and the interface is Windows 8.1 and later, so the CIM read
  stands as the fallback whenever the direct one is unavailable.
- fix: the pipeline server is built before its start timeout begins.
  (#207) The five-second budget was meant to cover binding a socket, but
  the work it actually enclosed was importing uvicorn and the mcp SDK
  inside the server thread, over a second warm and much more on a first
  run reading those packages off disk for the first time, which is where
  a cold start ran out of budget and failed once in the smoke test.
  Worse, a failure while building the app was not caught: the thread
  died, nothing signalled readiness, and the caller waited out the full
  timeout and reported what read like a bind failure, so a missing SDK
  and a slow machine were indistinguishable. Construction now happens on
  the calling thread, where an import error raises itself, and the
  timeout covers only the bind.
- fix: the mcp dependency is held below 2.0. (#205)
  That release removes `mcp.server.fastmcp`, which the pipeline server
  is written against, and the dependency carried no upper bound, so any
  new install resolved it and pipeline mode failed at import. The suite
  went from green to three errors within the hour with no change on
  this side, which is the signal a user would have got on a fresh
  install. The bound is lifted by the port to the 2.x server API, not
  before.
- fix: an upgrade names the watchers it left on the old version. (#188)
  Replacing the runtime does not stop the processes already running it:
  a running process has its code loaded and keeps executing it, on
  POSIX because the replaced file keeps its inode for as long as a
  process holds it, and on Windows because the executable a process is
  running cannot be replaced at all while it runs. Either way every
  watcher that was running kept the old release while the upgrade
  reported success. The upgrade now reads the process table before it
  installs and, once the install lands, names each watcher still alive
  by agent, pid, and project, with the restart to run in each project.
  The count and the agent names go on its admin event, because the
  symptom turns up days later and to someone else. Restarting them is
  left to the operator: an upgrade should not interrupt work
  mid-dispatch on its own.
- fix: the code that moved this tool's executables aside is gone. (#190)
  It renamed a locked executable so uv could write the name while the
  running process carried on from the renamed file. Measured against a
  real installation, a uv trampoline refuses the rename as well as the
  write, whether it is the launcher on PATH held by the running command
  or the tool-environment copy held by a watcher, so the mechanism had
  no working case on the platform it existed for. #189 had already
  stopped depending on it. What made the suite miss this is worth
  saying: every test of it mocked the lock check, so the mechanism was
  proved against a host that does not exist.
- fix: the log readers no longer resolve a project root while they load.
  (#202) `qlog.py` and `timeline.py` did it at import, so running either
  directly outside a project raised a traceback from the import
  statement rather than the sentence the command had ready, and the
  smoke suite could only import them from inside a project. Each path is
  now resolved by the function that needs it. An invariant in the suite
  asserts no module resolves a root as it loads, which is the same
  defect fixed in the smoketest in the previous release and guarded
  three different ways across the tree.
- fix: piped log output is now plain ASCII. (#186)
  The query tool drew its table with box characters, and a Windows
  console decodes a captured pipe at its own codepage, so the
  sanctioned way to read runtime state turned to noise exactly when the
  reader was a program. A terminal still gets the drawn table; anything
  else gets column rules, one row per line, and a row count. `--format`
  says so, and names csv and jsonl as the forms to parse.
- fix: an agent timeout records the elapsed time and attempt count.
  (#183) The error carried the configured limit, which is what the
  operator already knew; the elapsed time and the attempt count, which
  distinguish a slow link from a wedged one, were not written down at
  all. Both are on every retry warning, on the terminal error, and on
  the agent phase of a successful run, and the exception carries them
  for a caller that has to explain itself.
- fix: the smoketest says when it stopped at a host limit. (#185)
  An agent that exhausted its retries failed the run with the same
  shape as a broken contract, so a link that was merely slow read as a
  defect in the tool. The verdict names the limit, carries the attempt
  count and the timeout it hit, and records a category that the health
  check reports alongside the reason.
- feat: a capability probe that refuses or drags is logged. (#183)
  Every host-mutating command asks the pre-dispatch gate whether the
  host can do what it is about to be told to do, and until now the
  answer left no trace, so a slow probe looked like a slow command. A
  refusal and any probe over five seconds are recorded with the
  capability, the operation that needed it, how long it took, and why
  it said no. A fast pass stays silent, because a row per invocation
  would bury the interesting one.
- fix: a refused task query fails fast instead of walking the store.
  (#191) Probing the scheduler asked for the tool's own folder and,
  when that came back empty-handed, fell back to querying every task on
  the host, which on a managed host is a two-minute wait for an answer
  already known. The fallback now runs only for the one reply that
  warrants it - the folder is not registered yet - and any other
  refusal is reported as it is read, with a timeout on each query so a
  wedged scheduler stops the probe rather than the command.
- fix: the smoketest reports a missing project root in its own words.
  (#184) The module resolved the root while being imported, so running
  it outside a project raised a traceback before the command could say
  what was wrong. The lock path it needed the root for is resolved when
  the lock is taken.
- refactor: platform knowledge lives in the modules that own it.
  (#191, #184) Windows details had leaked outward: the task folder was
  spelled in two places that had to agree, the watcher prerequisite
  check reasoned about the host inline rather than asking the
  pre-dispatch gate, and the diagnostics read English out of exception
  messages to decide what a host was missing. Each is now a question
  answered by the module that owns the mechanism, so a change to one
  lands in one file. The smoke suite gained an invariants category that
  asserts the arrangement holds - which modules may name a platform,
  that the task folder is spelled once, that every capability a command
  declares can be probed, that the diagnostics describe every mechanism
  a host may dispatch with, and that the release gate smoketests the
  checkout being released - so drift fails the suite instead of
  surviving to a host.
- feat: a running dashboard can be listed and stopped from the CLI.
  (#198) A dashboard outlives the command that launched it, so a port
  stays held by a server the operator no longer knows about. The port
  guard added in 5.3.0 says the port is taken; until now nothing said
  what took it, and recovery meant hunting through the process table by
  hand. Every dashboard now records its port and pid before it serves
  and drops the entry when it exits, which gives `agents-live dashboard
  list` and `agents-live dashboard stop --port P | --all` something to
  act on, and lets the conflict message point at them. The registry
  describes only dashboards this host started: a listener that is a
  relay to another host, or one from an earlier release, answers the
  port probe and is absent from the registry, and the messages say so
  rather than reporting a missing entry. A subcommand's declared root
  kind is now what the pre-dispatch gate reads, so `dashboard list`
  reports on this host from anywhere while `dashboard` still resolves a
  project. Every other subcommand in the spec already declared the same
  kind as its parent, so nothing else changes.

## 5.3.0 - 2026-07-27

- fix: a host-mutating command no longer acts on an unnamed project.
  (#192) Root resolution learned to fall back to the sole registered
  repository so the dashboard had something to show, but it answers for
  every command, not only the read-only ones. "Exactly one repository is
  registered" is the state of every host that has run `repos add` once,
  so `start`, `stop`, `delete` and `migrate` run from an unrelated
  directory silently targeted that project, writing cron entries or
  scheduled tasks into it without ever naming it. The fallback is now
  opt-in and reaches only the read-only commands it was added for: the
  dashboard, `status`, `logs` and `timeline`. Everything else fails
  loudly again, as it did before, and a command added later inherits
  that answer by default. A sole registered repository that has since
  been moved or deleted also no longer turns a missing root into a
  failure about an alias the caller never mentioned.

- fix: the smoketest cleans up after an installed tool.
  (#193) It found the agent runs it had started by looking for
  `run.py` on the command line, which only the flat checkout
  dispatches; an installed package runs the pinned CLI shim with a
  `run` subcommand and no script path anywhere on the line. Cleanup
  therefore matched nothing for every user of the released package,
  reporting success while leaving the fixture's runs behind. Both
  invocation forms are now recognized.

- fix: the export-clean audit checks every drive letter.
  (#195) The personal-path pattern was pinned to `C:\Users\...`, so a
  checkout on any other drive, which is ordinary on a Windows
  development host, could carry a personal path into a release and
  still pass the gate.

- feat: `upgrade --from PATH` installs a local build. (#179)
  Until now the runtime could only be upgraded from PyPI, so the
  installed-tool leg of the testing boundary could not be exercised
  without publishing first. The command now takes the source to install
  from, a project directory or a built artifact, and runs it through
  the same path as a published upgrade, with the payload refresh,
  plugin convergence, and completion update that follow any upgrade.
  It cannot be combined with `--skills-only`, which installs no
  runtime. `--force` on its own lets uv serve a cached build of the
  same directory, which would install the previous source while
  reporting success; the local path therefore asks for the package
  itself to be rebuilt, scoped so that dependencies stay cached rather
  than turning the install into a full re-download.

- fix: an upgrade no longer fails over a launcher it could not replace.
  (#179) Windows holds a lock on a running executable, and a uv
  trampoline refuses to be renamed as well as written, so the launcher
  cannot be moved out of the way while anything is running it. That
  includes any watcher on the host and the upgrade command itself,
  which reaches the runtime through the very launcher being replaced,
  making this the ordinary outcome rather than a rare one. uv builds
  the environment first and publishes launchers last, so what actually
  happened in these runs was a complete upgrade reported as a failure,
  leaving the install state needing a hand check. The upgrade and
  convergence paths now check whether uv rewrote the environment's
  launcher before it stopped, which places the failure at the final
  step and proves everything before it finished, and treat that case as
  the upgrade it was. The launcher is compared against its own recorded
  timestamp rather than against the clock, because a file's stamp comes
  from the coarse system clock while the clock reading does not, and a
  launcher written in the same tick could otherwise pass for one uv
  never touched. The check is confined to Windows and to that
  evidence, so an install that stopped earlier, or failed anywhere
  else, still fails. A note says the launcher was kept, since it
  carries no version and commands run the new runtime either way,
  while uv's own record of the tool stays on the previous install until
  an upgrade runs with nothing holding the launcher.

- fix: the Windows release gate no longer fails on a healthy host.
  Two budgets in the smoketest path were set below the work they wait
  for. The scheduler preflight asked schtasks to walk the whole machine
  task tree, about 2000 lines, measured between 4 and 26 seconds on one
  host, against a 10 second limit; it now queries the folder this tool
  registers into, the form the rest of the code already used, and falls
  back to the root walk with room to finish only when that folder does
  not exist yet. The smoketest also waited 90 seconds for an agent
  result, but an agent call gets its own timeout on each attempt and is
  retried once, so a run that succeeds only on the retry can take both
  budgets. On a high-latency link that is the ordinary case, and the
  gate failed work the framework went on to finish. Both waits now
  derive from the retry-inclusive worst case rather than restating a
  smaller number.

- fix: the dashboard refuses a port something else is already on. (#174, #175)
  NiceGUI prints its readiness line before uvicorn attempts the bind, so
  a start that could not work announced success and then failed with a
  bare errno. Worse on Windows, where a second listener may bind an
  address another process is serving unless that process asked for
  exclusive use: two servers coexisted, the first one took every
  connection, and the new dashboard sat unreachable while reporting no
  problem. The port is now settled before anything is announced, by
  talking to it as well as binding it, and a conflict is refused through
  the standard error envelope as `port_unavailable`.

- fix: the dashboard finds its project and names what it shows. (#173)
  Started outside a project with a single repository registered but no
  default selected, the dashboard rendered a complete page whose agent
  table was empty, with no message and no error, and setting a default
  made every agent appear. `init` always initializes the host-global
  workspace, so root resolution reached that empty workspace and stopped
  there. Resolution now falls back to the sole registered repository
  before the global workspace, and when several are registered without a
  default it fails naming the commands that select one. The header shows
  the project the view is scoped to beside the host label, the
  `--all-repos` view shows the host too, and a host where nothing
  resolves gets that explanation in place of an empty table.

- fix: the dashboard Failing filter ignores agents from another host. (#176)
  The health flag excluded agents whose state was `inactive`, a value no
  code path produces, so the guard was inert and an old error from a run
  on the owning host marked an agent as failing in this host's view. It
  now excludes `stopped`, the state of an agent with no trigger
  registered here, and keeps flagging `unknown`, where the scheduler
  could not be read.

- fix: the framework smoketest brings its own handlers. (#171)
  It set its agents' post-processor to the project's own
  `write-files.sh`, so the gate assumed a project that happened to have
  that handler and a host with both bash and `jq`. A fresh project or a
  Windows host failed on the fixture rather than on the framework. The
  gate now writes its own ephemeral handlers in Python, each carrying a
  PEP 723 header so `uv` provisions the interpreter and nothing depends
  on what `python` means in a cron or watcher context.

- fix: a run someone asks for is no longer discarded as not due. (#172)
  Whether a run came from the clock was inferred from whether the agent
  had a schedule, so `agents-live run <name>` on a scheduled agent was
  treated as a clock fire and refused by the Windows dueness gate,
  reporting success while doing nothing. A run now says how it was
  invoked: persisted schedule entries pass `--scheduled` and only those
  are checked for dueness. On a crontab host the gate was always open,
  so nothing there changes but the recorded trigger. Existing entries
  predate the flag. Until a Windows task is rewritten it fires without
  `--scheduled` and the dueness check is skipped, so the agent runs on
  every trigger the Task Scheduler delivers; that same check is what
  claims a minute, so a repeated fire is no longer suppressed either.
  Those runs report success and spend tokens, which is why they are
  worth correcting rather than waiting out. Correction is not automatic:
  a task is rewritten when its agent next converges, which needs the
  maintenance loop installed. To correct a host deterministically, run
  `agents-live upgrade`, which converges the persisted entries of every
  registered project. Updating the runtime by itself, with `uv tool
  upgrade agents-live`, rewrites nothing. A crontab line is corrected by
  re-activating the agent.

- fix: the smoketest refuses a project it was not asked to act on.
  It targeted whatever root resolved, so on a host with a configured
  default it created, activated, ran, and deleted agents inside a real
  project and dispatched an agent runtime there, without ever naming
  it. A root reached through the configured default or the host-global
  workspace is now refused; `--repo`, the environment variable, or a
  marker at or above the working directory still work.

- fix: the framework smoketest runs on a Task Scheduler host. (#171)
  It stopped at the first Windows-absent primitive every time, so the
  release gate could only be run from WSL and Windows regressions
  reached releases unchallenged. Four assumptions are gone: `SIGHUP` is
  registered only where it exists, process discovery and teardown go
  through the host-neutral `hostruntime` helpers instead of reading
  `/proc`, the `inotifywait` preflight is skipped on a host that watches
  in process, and the post-processor check compares paths in a
  normalized form rather than asserting a POSIX-shaped string.

- fix: an underscore-named agent can register a Windows task. (#171)
  Ephemeral agents are named `_name` so they match the `Agents/_*`
  ignore patterns, but the task-name rule required an alphanumeric first
  character and refused every one of them. A leading dot or dash is
  still refused: one hides the task, the other reads as an option.

- feat: the smoketest reads the dashboard's agent list over HTTP.
  The dashboard is where several recent defects lived and nothing
  exercised it outside a browser. A new step starts it against the
  project under test and reads the new `/api/agents` endpoint, which
  returns the same row model the table binds to. One assertion now
  covers the dashboard binding a port, resolving the intended project,
  and enumerating its agents.

## 5.2.0 - 2026-07-26

- fix: convergence no longer fails on the executable it is running from.
  (#162)
  Windows holds a mandatory lock on a running image, so `uv tool install
  --force` could not rewrite `agents-live.exe` while the convergence was
  itself started through it, and the install ended with "Failed to
  install entrypoint ... os error 32". Retrying re-entered through the
  same executable and failed the same way, so a plugin declaration could
  not be applied from a Windows host at all. A locked image can still be
  renamed: the entry points uv recorded in its own receipt are moved
  aside for the install and put back if the install writes nothing, which
  is the move `uv self update` makes to replace itself. Only a locked
  file is moved, and nothing is moved on POSIX.

- fix: enumerating agents asks the host once instead of once per agent.
  (#165)
  Reading whether an agent is active asked questions that answer for the
  whole machine: the process table, which costs about two seconds on
  Windows because it is a PowerShell CIM query, and the folder of
  registered tasks, which cost three `schtasks` subprocesses per agent
  whether or not the agent had a task. Those reads were taken once per
  agent, the state word and the per-trigger detail each took their own,
  and the dashboard built its rows three times per render. With thirteen
  agents the dashboard never finished a page: the build blocked the event
  loop long enough for the browser connection to be dropped and retried,
  indefinitely. Host-wide reads are now taken once per enumeration pass,
  the task folder listing decides which task definitions are worth
  reading, and the state word is derived from the reading already taken.
  The registered XML still decides ownership wherever ownership is
  asserted. Measured on a thirteen-agent project, a status sweep went
  from 32s to 2.2s and the dashboard from never finishing to interactive
  in 2.3s.

- feat: administrative operations are logged, not just agent runs. (#164)
  Logging recorded what agents did and nothing about how the host was
  changed, so a plugin install, an ownership transfer, a schedule teardown,
  or a version move left no trace anywhere and `agents-live logs` had
  nothing to show. Every module that mutates host state now writes to a
  host-scoped `admin.log` next to the health-check loop's own log, which
  `logs` and `logs timeline` already union in. Administrative events are
  not agents: they carry `scope: "host"` and the pseudo-agent name `admin`,
  so readers that group by `agent_name` keep working while a query can
  still separate administration from agent activity. Each event records the
  invoking command and whether a terminal was attached, so an operation
  traces back to a cron entry, a CLI invocation, or an agent. Writing is
  best-effort and never fails the operation it records.

- fix: plugin convergence no longer upgrades the tool as a side effect.
  (#163) The uv receipt records `agents-live` as a bare name, so the
  `uv tool install --force` that convergence runs resolved to whatever was
  newest on PyPI. Since registration converges, `repos add` and
  `repos default` could move a host to a new version as a side effect of
  registering a directory, silently and with nothing recorded. Convergence
  now pins the primary requirement to the running version; `upgrade`, which
  resolves a new release on purpose, is the only caller that opts out and
  so remains the way to change versions.

- chore: a release can be cut from a Windows checkout. (#169)
  The release script compared the files git reported as changed, which git
  always names with forward slashes, against paths built with the platform
  separator, so the version bump looked like it had touched an unexpected
  file set and preparation aborted on Windows every time.

## 5.1.0 - 2026-07-26

- feat: `repos add` registers a repository from the CLI. (#159)
  The subcommand and its implementation shipped in every release so far,
  but the command spec listed only `list`, `default`, and `remove`, so the
  front end rejected the name a user reaches for first and answered with
  alternatives that did not obviously include registration. Registration
  was still reachable through `init --repo` and through `repos default`,
  which registers a path it does not recognize.

- fix: registering a repository installs the plugins it declares. (#160)
  `init --repo` has always registered and converged in one step, while
  `repos add` and `repos default` stopped after writing the registry
  entry. A registry-mode repository registered that way had no ownership
  backend, and `status` did not say so: it rendered every agent's owner as
  `-`, which reads as "nobody owns this" rather than "this host cannot
  tell". On a newly set-up host that is the reading that invites claiming
  agents another machine already owns. Registration now converges, and
  `status` reports ownership as unavailable when the declared registry has
  no backend.

## 5.0.1 - 2026-07-26

- fix: the dashboard no longer crashes on an agent owned elsewhere. (#157)
  Building the agent table read a `host` name that only existed inside the
  page builder, so every launch raised `NameError` as soon as one agent was
  owned by another runtime. Ownership became a `hostname/runtime/uuid`
  triple in 5.0.0 and no pre-5.0.0 owner value matches, which makes every
  agent foreign until it is claimed: the dashboard failed for exactly the
  upgrade it is needed to recover from. The Claim and Activate tips are
  the only readers of that name, and both are reached only on the branch
  no test covered. `pyflakes` reports an undefined name in milliseconds,
  so the suite now fails on one anywhere in the package.

- docs: release notes carry one reconciled list instead of two. (#155)
  The body concatenated a curated list built from the changelog with
  GitHub's generated pull-request list, so most changes were stated twice,
  once by issue and once by pull request, and neither list was a superset:
  a pull request that landed without a changelog entry appeared only in
  the generated half. The notes are now built in full from the changelog
  entries joined to the pull requests merged since the previous tag, and
  `--generate-notes` is no longer used. A `BREAKING CHANGE:` paragraph is
  lifted into an `Action required` section ahead of the list rather than
  left a link away, which is the part of a major release a reader most
  needs. Rows are annotated `(PR #N fixes #M)`, because GitHub autolinks
  issues and pull requests identically and a bare number cannot be told
  apart. `--notes <tag>` previews the rebuilt body for a release that is
  already published and applies it with `--yes`.

## 5.0.0 - 2026-07-26

- fix: the last registered repository can be removed. (#144)
  `repos remove` refused to drop a repository while it was the default,
  which is right whenever another entry could inherit the role and a dead
  end when it is the only one: `repos default` had no other candidate to
  accept, so the registry could not be emptied through the CLI at all.
  That is exactly the state a first `init --repo` leaves behind, so
  anyone who tried the tool once and changed their mind had to hand-edit
  the config. Removing the last repository now clears the default and
  leaves an empty registry.
- fix!: two WSL distros on one machine are no longer the same owner. (#148)
  A distro's hostname defaults to the Windows computer name, so ownership
  gave both distros the same value and each would answer to the other's
  agents. An owner is now `hostname/runtime/uuid`, where the runtime is
  `windows` or the distro name. Matching reads only the uuid and display
  reads only the hostname and runtime, so a machine or distro rename
  changes how a row reads and never who owns it, and `status` and the
  dashboard show `hostname/runtime` instead of a 32-character hex string.
  `doctor` scopes its agent-CLI warnings through the same matcher, so it
  still checks only the tools this runtime's own agents need.
  BREAKING CHANGE: an owner value that cannot be reduced to a uuid is
  treated as another runtime's, which is what makes the model durable
  against a truncated write, a bad merge, or a hand edit - but it also
  covers every entry written before this release. Those agents stop
  running and shed their local cron and watcher triggers at the next
  health sweep until they are claimed. Claim each one on the machine that
  should own it with `agents-live start <agent> --transfer-here`. An agent
  with no registry entry at all is unclaimed rather than foreign and keeps
  running, so local-mode repositories are unaffected.
- feat: `start --transfer-here` claims an agent for this runtime. (#148)
  `--transfer-to` needs a full `hostname/runtime/uuid` identity, which is
  only obtainable by copying it out of `agent-owners.json`. That is
  reasonable for assigning an agent to a machine you are not on, and
  unusable for the common case of claiming one where you are standing.
  Both flags change the registry only; activation stays a separate step on
  the owning machine.
- docs: the project now carries an explicit MIT license. (#147)
  The repository had no `LICENSE` file and the package declared no license
  metadata, which left a published tool defaulting to all rights reserved.
  The wheel and sdist now carry the license expression and the license
  file. The README install instructions are also split per platform, so
  Windows and WSL no longer read as footnotes to an apt line.

## 4.0.0 - 2026-07-25

- fix!: the WSL heartbeat no longer runs on VBScript. (#137)
  Its scheduled task launched `wscript.exe` on a packaged script that
  asked Windows to hide a console it had just created - a scripting host
  Windows 11 is removing, and a hiding technique the default terminal
  application can override. The task now runs `wslg.exe`, the windowless
  launcher WSL itself ships, which is given no console to begin with.
  BREAKING CHANGE: the retired script lived inside the tool environment,
  so upgrading the package in place leaves the old task pointing at a file
  that is gone; it then fails every five minutes and stops holding WSL
  open. Python packaging runs no code on install or uninstall, so neither
  version can correct this unaided. Remove the old task with the version
  that registered it, then reinstall: `agents-live uninstall
  --retain-state`, `uv tool install agents-live`, `agents-live init`.
  After an in-place upgrade, `agents-live init` alone re-registers the
  task, and `doctor` names the stale action until it does.
- feat: `init` registers the Windows heartbeat on WSL. (#137)
  Keeping WSL alive is part of initializing host support, not a step to
  remember afterwards: without it, scheduled agents only run while a WSL
  session happens to be open. A host that cannot reach Task Scheduler is
  reported with the command that repairs it, and the rest of init still
  succeeds.
- test: the suite runs on native Windows, and CI keeps it that way. (#119)
  Every push and pull request now runs the tests on Windows as well as
  Linux. Getting there fixed four defects the Windows host exposed: a
  repo-relative path came back spelled with backslashes, which broke the
  cache key it doubles as; the guard that keeps an agent directory inside
  the repository let through `/tmp/agents` and `C:agents`, neither of which
  Windows counts as absolute; a running watcher was not recognised as
  belonging to its own repository when its command line used forward
  slashes; and a health sweep read the host's real crontab, which does not
  exist there. New tests cover the two ways a lifecycle operation ends up
  aimed at the wrong thing - a reused process id, and one repository
  reached under a second name through a junction.
- fix: writing a config file no longer fails on native Windows. (#119)
  Every write that restricts the file's permissions went through a call
  Windows only grew in 3.13, so under Python 3.12 registering a repository,
  or anything else
  that touches the config, raised an `AttributeError` there - and the
  cleanup behind it left the half-written temporary file in place, because
  Windows will not remove a file that is still open.
- fix: a console window no longer flashes on every native Windows spawn. (#139)
  Starting a watcher, a cron loop, or the maintenance loop opened a window
  that closed again a moment later. The spawn asked for no window and for a
  detached process, and Windows ignores the first request whenever the
  second is made, so every detached child came up owning a fresh console
  with a window for the console host to draw. The child now gets a console
  of its own that is never drawn, which its own descendants inherit.
- feat: harden the failure paths under native Windows. (#136)
  A file-change storm can no longer grow without limit: the watcher's event
  queue is bounded, and past the bound it falls back to the same single
  bounded rescan an overflowed kernel buffer already used. One dispatch
  carries a bounded number of file names, and says in the log how many it
  left out. Registering a scheduled task now verifies that the store kept
  the command and the schedule it was given, so an interrupted or partly
  applied update fails where it happened instead of surfacing later as a
  task that gets rewritten by every maintenance pass. An agent's tasks are
  registered before any are removed, so an interruption leaves an extra
  trigger rather than none. `doctor` states the limit that comes with
  running as you: nothing scheduled runs while nobody is signed in.
- feat: complete the lifecycle commands on native Windows. (#126)
  `doctor`, `upgrade`, `migrate`, and `uninstall` now speak to the store the
  host actually keeps schedules in, rather than assuming crontab. Doctor
  checks that this host can schedule and can watch, naming the mechanism it
  uses and offering a fix that works there, and reports scheduled tasks that
  name an agent or a project directory that is gone. The tool's own
  check-and-repair loop installs as a scheduled task, so upgrades no longer
  fail looking for `crontab`, and an upgrade that re-homes the pinned
  executable rewrites the tasks that referenced the old one. Agents no longer
  open a console window when they run: a scheduled task starts them through a
  windowless launcher, and the ownership check still verifies the command
  that finally runs. Linux and WSL behavior is unchanged.
- fix: a command asked for JSON prints only the document. (#126)
  `migrate` printed its human narration ahead of the JSON, so anything
  parsing it - `doctor --repair` among them - saw a stream it could not
  read. The narration says what the plan already carries; run the command
  without `--json` to see it.
- feat: watch a directory on native Windows. (#126)
  A watcher agent now dispatches on file changes there, using the Windows
  directory-change notification API in place of `inotifywait`. Creations,
  edits, renames, and deletions all reach the same debounce, cascade guard,
  and fire-rate breaker the Linux watcher uses, and a batch of changes still
  becomes one run. If the system drops changes under load, the watcher lists
  the watched directories once, within a fixed limit, rather than losing
  them. Keeping the watcher alive across a logon is a scheduled task
  alongside the agent's own, and preflight now asks the host whether it can
  watch rather than looking for a Linux tool. Linux and WSL behavior is
  unchanged.
- feat: accept every schedule on native Windows. (#126)
  Schedules that do not map exactly onto a Task Scheduler trigger now
  register as a repetition that covers every minute they can name, and each
  fire is checked against the expression before it becomes a run, so an
  agent runs when its schedule says and no more often. Calendar schedules
  register as daily, weekly, or monthly triggers. `@reboot` registers as a
  logon trigger in a task of its own, since a startup trigger needs
  elevation. Linux and WSL behavior is unchanged.
- feat: schedule an agent with Task Scheduler on native Windows. (#126)
  Activation registers one task per agent, named for the agent and its
  repository so two checkouts never collide, and verifies the task it wrote
  before replacing or removing it. Schedules that translate exactly are
  registered and the rest are refused with a clear message until dueness
  checking lands. One module now chooses between crontab and Task Scheduler,
  so activation, status, stop, and the smoke test no longer name a
  mechanism. Linux and WSL behavior is unchanged.
- feat: give a runtime its own ownership identity. (#126)
  Ownership decisions now match against a runtime identity rather than the
  hostname, which stays as a display label, for hosts where the machine
  name cannot provide one. Linux and WSL keep the hostname
  as that identity and are unchanged; a native Windows runtime generates
  `windows:<uuid>` once into the user state home, which on Windows is now
  the local application-data directory rather than the XDG path.
- feat: run an agent in the foreground on native Windows. (#126)
  Agent invocations get the host environment a Windows process needs, the
  executable is pinned before launch rather than resolved by name, the
  terminal path is skipped where the CLI does not need one, handlers that
  need a shell are refused where there is none, and console output no longer
  fails on a legacy code page. Linux and WSL behavior is unchanged.
- refactor: move process control onto the host-runtime seam. (#126)
  Locking, detached spawning, liveness, and termination now live behind that
  seam. Locks are file locks on every platform, and stopping a process now
  stops the tree it started rather than a POSIX process group alone. Linux
  and WSL behavior is unchanged.
- refactor: extract the host-runtime seam. (#120)
  Triggers are described by a spec and rendered into crontab lines in one
  place, watcher policy decides batches without a live event source, and
  runtime identity is answered once. Linux and WSL behavior is unchanged; the
  package also imports on hosts without `fcntl`.
- fix: keep console output ASCII-only. (#121)
  The pre-release audit summary, qlog traceback header, timeline separator,
  and smoketest step labels no longer require a UTF-8 capable console.
- feat: install and maintain user shell completions automatically. (#117)
  Init and runtime upgrades write XDG-aware Bash and Zsh scripts; explicit
  update, current-session sourcing, shell prerequisites, and uninstall cleanup
  remain available through the completion command and CLI reference.

## 3.0.0 - 2026-07-22

- fix: render nested command help from the selected subcommand. (#112)
  `logs timeline --help` now lists only timeline arguments, documents its
  ISO-8601 time filter, and rejects unrelated parent log-query options.
- feat!: simplify startup around explicit agent file paths. (#114)
  Bare `init` bootstraps host support, `init --repo` enrolls an optional default
  workspace, and direct `run` or `start` paths need no registration. Maintenance
  and trigger migration are automatic; `doctor --repair --dry-run` previews
  concrete repairs instead of exposing public `health-check` or `migrate`
  commands, and `repos add` is retired in favor of `init --repo`.

## 2.2.0 - 2026-07-21

- fix: migrate legacy runtime state during ordinary upgrades. (#90)
  Each refreshed project moves in-tree logs and watch hashes into the XDG state
  home with retry-safe collision handling before its skill payload is updated.
- fix: tolerate unavailable plugin wheels until installation is required. (#91)
  Healthy installed plugins no longer block activation after an artifact is
  removed, and repository registration survives plugin diagnostics while
  preserving metadata, identity, and checksum checks at installation time.
- feat: improve dashboard coordination, diagnostics, and agent reporting. (#104)
  Dashboard actions run through a visible FIFO queue; structured error summaries,
  model details, filters, cost totals, and bounded table and log scrolling keep
  operational status usable as the agent list grows.

## 2.1.3 - 2026-07-20

- fix: isolate framework smoketest watcher validation to the current run. (#106)
  Watcher checks reject stale or incomplete log output, ignore generated index
  noise, and reset persisted content hashes so consecutive runs still dispatch.
- docs: clarify post-publish verification and artifact inspection.
  Release checks distinguish PyPI JSON publication from Simple API propagation,
  avoid interactive workflow watchers in automation, and identify the generic
  `Agents/` fixtures intentionally included in the source distribution.

## 2.1.2 - 2026-07-20

- fix: preserve UTC instants across qlog display and filtering. (#99, #100)
  Canonical writers remain RFC 3339 UTC with `Z`; qlog normalizes aware and
  legacy naive timestamps to UTC, keeps `--since` and `--until` independently
  optional, and rejects invalid bounds without an internal traceback.

## 2.1.1 - 2026-07-19

- fix: complete universal CLI help and shell-completion coverage. (#95)
  Every command now lists and completes `--json`, `-h`, `--help`, and `help`;
  top-level and help-target completion follows the full finite public grammar,
  enforced by behavioral Bash and generated Zsh conformance tests.
- fix: reject incomplete first-line summaries before release preparation. (#63)
  Release preview and publication require each changelog bullet's first line
  to end as a standalone sentence.

## 2.1.0 - 2026-07-19

- fix: apply positional agent filters to combined log views. (#89)
  `logs <name> --all` and `logs timeline <name> --all` no longer silently
  ignore the positional name when reading the log union.
- fix: reject non-jsonl explicit formats in JSON log mode.
  `--json logs` returns a usage error instead of an empty-but-ok records
  envelope when `--format` is not jsonl.
- fix: keep `start --all` running when the ownership registry is unavailable.
  Registry failure is per-agent abstention rather than a mid-batch abort, so
  health sweeps degrade instead of erroring.
- fix: prevent dashboard actions from hanging on hidden ownership prompts.
  Dashboard children run with stdin closed, and ownership takeover requires an
  interactive stdout.
- fix: write spawned-agent stderr logs to the user-level state home.
  Logs no longer enter the project tree, so transitional state migration
  converges and synced repositories stay clean.
- fix: append colliding legacy logs during state migration.
  Newline-guarded appends preserve the destination file under live appenders.
- fix: write the health beacon atomically and degrade empty registry sweeps.
  A sweep with no registered repositories reports `degraded` with a warning
  instead of reporting healthy.
- fix: pin release gates to the checkout repository. (#85)
  `AGENTS_LIVE_REPO` prevents gates from falling through to the registry-default
  repository.
- feat: make CLI help available around commands and generate the full public command surface. (#93)
  Completion help includes persistent Bash and Zsh installation commands,
  and upgrades report the installed agents-live version before running.
- docs: repair stale skill documentation references and host setup steps.
  Remove references to the retired scripts tree and `release` verb, fix dead
  cross-links, and add the missing `repos add` step to the host workflow.

## 2.0.2 - 2026-07-19

- fix: stop crashing watchers on their first file-change dispatch.
  The dispatch logger rendered its run-capture paths repo-relative, but
  captures moved to the user-level state home in 2.0.0, so the watcher
  process died with a ValueError on its first dispatch and dropped
  events until the hourly health-check pass restarted it. Capture paths
  are now logged absolute.
- docs: retire stale references to the pre-package flow. (#75, #76, #77, #78)
  overview.md points at GitHub issues and the logs commands instead of
  retired files; the commands.md release section documents the
  definitive-repo gates and guarded workflow; the WSL runbook's timeline
  example uses --last; the Windows heartbeat guide warns that a bare
  tool install drops declared plugin wheels and names the convergence
  paths.

## 2.0.1 - 2026-07-19

- fix: keep the health-check sweep's stdout contract pure JSON when in-process work prints.
  The first pass on a host that prunes retired agent entries no longer
  fails with "sweep emitted non-JSON output"; pruning notices are
  forwarded to stderr and the loop's log instead.

## 2.0.0 - 2026-07-19

- fix: organize GitHub release notes into curated, generated, and reference sections.
  New publications and retries show `Curated Summary` first, GitHub's pull
  request list next, and changelog plus version-range links last.
- feat!: ship the check-and-repair loop as the built-in `agents-live health-check` command.
  The loop no longer depends on a consumer-project agent: it self-installs
  its `@reboot` + hourly crontab entries, converges declared plugin wheels
  into the tool environment, sweeps every registered repository (crontab
  convergence, orphan and registry pruning, ownership enforcement, dead
  watcher restarts), gates the framework smoketest on a content
  fingerprint, and writes the host health beacon. An unavailable ownership
  backend now degrades the beacon and abstains instead of aborting the
  pass. Doctor gains a "health-check loop installed" check and its repair
  hints target the built-in; the dashboard health panel and button use it
  too. `uninstall` removes the loop's entries; `upgrade` converges them
  but never installs - a host opts in by running the command once.
  BREAKING CHANGE: the per-project health-check agent pattern is retired;
  delete such agents and run `agents-live health-check` once per host.
- feat!: move machine-local runtime state to the user-level XDG state home.
  Logs, run artifacts, beacons, watch hashes, and the smoketest lock now
  live under `$XDG_STATE_HOME/agents-live/` (default
  `~/.local/state/agents-live/`), host-level plus one directory per
  repository, so project trees no longer carry machine state that could
  sync or export, and the tool works with no initialized project.
  `Agents/` keeps only git-tracked content and the git-synced ownership
  registry `Agents/data/agent-owners.json`; `init` no longer creates
  `Agents/logs/`. `agents-live logs --all` unions the repository's logs
  with the host-level logs.
  BREAKING CHANGE: `agents-live migrate` (run by every hourly health-check
  pass) moves legacy in-tree state to the new locations; tooling that read
  `Agents/logs/` or `Agents/data/health.ok` directly must switch to
  `agents-live logs` or the state-home paths.
- chore: require reviewable commits and pre-PR branch history checks.
  Plans stay outside git, unshared branches drop empty or superseded commits,
  and synchronization avoids incidental merges from `origin/main`.

## 1.0.0 - 2026-07-19

- fix: emit typed JSON envelopes for usage errors, structured failures, and log records. (#65)
  Under `--json`, argparse usage errors no longer exit with empty output,
  doctor's structured failure payloads pass through untouched, programming
  errors keep their tracebacks, and `logs` renders one stable envelope for
  zero, one, or many rows. The spec gate also accepts `--flag=value` and
  attached short values such as `-n20`.
- fix: keep agent discovery working when a declared plugin wheel is absent from disk. (#66)
  A fresh clone without gitignored build artifacts no longer breaks `status`,
  `run`, `start`, or cron-fired runs; plugin installation still requires the
  wheel and verifies its integrity.
- fix: suppress the interactive ownership-takeover prompt in JSON mode. (#69)
  A machine caller can no longer hang forever on a prompt hidden by captured
  output; consent is given with `start <name> --yes`.
- fix: accept Vixie cron name fields such as `MON-FRI` and `JAN-DEC` in agent schedules. (#68)
- fix: run framework smoketest status checks through the supported JSON environment contract. (#67)
- fix: list every agent name in generated shell completions instead of only the last. (#70)
- fix: stop requiring a literal versioned docs link in the CLI during release preparation.
  The CLI derives its documentation links from the package version at runtime,
  so the release tool rewrites only real version surfaces.
- fix: enforce major release bumps for conventional breaking markers. (#62)
  Release previews recognize `type!:` and scoped `type(scope)!:` entries, plus
  `BREAKING CHANGE:` footers.
- fix: publish one-line changelog summaries in GitHub release notes. (#63)
  Supporting detail remains in the tagged changelog linked from each release;
  GitHub's generated pull request list and compare link follow the summaries.
- fix: refresh release metadata once during `doctor --all-repos`. (#31)
  Child repository checks no longer repeat the same network request.
- fix: reject rootless dashboard access to repository-scoped paths. (#30)
  The all-repositories dashboard no longer carries CWD-relative sentinel paths
  that could write runtime data outside a resolved project.
- fix: pass smoketest changed paths through the supported JSON-array contract. (#41)
  Dispatch uses run's `--changed-files` argument instead of a nonexistent
  singular flag.
- fix: support explicit ownership takeover with `start <name> --yes`. (#47)
  Interactive targeted starts prompt, while non-interactive starts still
  refuse takeover without consent.
- fix: resolve repository aliases in subprocess-dispatched log commands. (#48)
  `logs` and `logs timeline` work with a registered alias or default repository,
  not only an explicit `--repo`, environment override, or local marker.
- fix: preserve co-installed plugin requirements during runtime upgrades.
  `agents-live upgrade` no longer removes plugin wheels recorded in the uv tool
  receipt.
- feat!: unify machine-readable output under position-independent `--json`. (#42)
  Repository lists, migration plans, upgrades, log timelines, and smoketest
  verdicts use typed JSON envelopes. The duplicate `teardown` and `prereqs`
  verbs are removed; use `stop` and `doctor`.
- feat: let projects declare committed plugin wheels with optional SHA-256 pins. (#34)
  `init`, `start`, and `upgrade` converge declarations into the host-global
  tool environment; `doctor` reports missing or broken providers, and
  `repos add` remains read-only.
- feat: adopt moved-project trigger entries with `migrate --adopt <old-root>`. (#32)
  Adoption rejects live roots, matches agents and roots token-exactly, preserves
  unrelated crontab entries, and supports dry-run planning.
- feat: scan shipped text for locally configured machine names during audits. (#29)
  A gitignored local file supplies literal names without committing personal
  host information.
- feat: attach wheel and source distribution artifacts to GitHub releases.
  The trusted-publishing workflow builds once, uploads both artifacts to the
  release, and publishes those same files to PyPI.
- feat: register an existing repository through `repos default <path>`.
  An unregistered path is added before it becomes the fallback repository.
- feat: drive CLI policy and generated interfaces from one command spec. (#36, #37, #38, #73)
  The declarative grammar controls dispatch, cross-command contracts, help,
  the published EBNF grammar, and the command and flag table. Validation
  constraints, JSON dispatch policy, the dashboard verb map, and the
  completion scripts' agent-name verbs are likewise declared on or derived
  from the spec.
- feat: generate bash and zsh completion scripts with `agents-live completions`. (#39)
  Completion includes agent-name suggestions for lifecycle commands.
- feat: move watcher process and reboot plumbing to a hidden namespace. (#43)
  `agents-live migrate` rewrites persisted legacy watcher lines to the
  canonical `internal` invocation.
- feat: run the release audit and unit suite on every pull request and push. (#40)
  The required release gates now run automatically on `main` and PR branches.
- docs: standardize starter-agent instructions on `.claude/agents/`. (#5)
  Existing `Agents/` definitions remain discoverable, while new definitions use
  the native directory shared by Claude Code, Copilot CLI, and VS Code.
- docs: align shipped Markdown with the repository punctuation rules. (#33)
- chore: gate release preparation and publication on the framework smoketest. (#72)
  `tools/release.py` runs `agents-live smoketest` alongside the unit suite and
  the pre-release audit during both `--prepare` and `--publish`.
- chore: require isolated worktrees and a standard implementation loop.
  Branch work no longer changes the shared primary checkout used by concurrent
  sessions.
- chore: normalize historical GitHub release titles and delete merged branches.
  Release metadata now follows one title convention, and merged PR head branches
  are removed automatically.

## 0.3.1 - 2026-07-18

- fix: register the Windows heartbeat task through the packaged
  `run-hidden.vbs` wrapper (`wscript.exe`), so the five-minute cadence
  no longer flashes a visible console window. `doctor` flags direct
  `wsl.exe` registrations and recommends re-running
  `agents-live heartbeat install`, which replaces the action in place.

## 0.3.0 - 2026-07-18

- fix: refuse to modify the crontab when it cannot be read. A transient
  read failure during activation previously installed a fresh table
  containing only the new entries, silently wiping the user's personal
  cron jobs and every other project's triggers.
- fix: stop managing a global crontab `PATH=` line. Each persisted
  agents-live entry now carries its own inline `PATH`, so user-authored
  and other projects' `PATH=` lines are never deleted or overwritten.
- fix: validate agent `schedule:` frontmatter as strict cron syntax
  (five fields or an `@keyword`), so a crafted schedule can no longer
  smuggle shell commands or extra lines into the installed crontab.
- fix: enforce the documented repository-relative contract for
  `watchPath` and pre/post-processors; paths that resolve outside the
  repository root (including via `..` or symlinks) are rejected, so an
  agent definition cannot watch external data or execute code outside
  the project.
- fix: freeze host-seeded pipeline MCP paths. The agent-facing `put`
  can no longer rebind a seeded `$schema` (or supply a forward-declared
  schema document) and thereby validate its own output against a
  schema of its choosing.
- fix: parse `.vscode/mcp.json` as real JSONC - inline and block
  comments and trailing commas now load correctly - and fail closed
  with a typed error on any malformed or non-object document instead
  of silently running agents without their MCP server definitions.
- fix: scope watcher process matching to the current repository, so
  same-named watchers in different projects are no longer cross-reported
  or cross-killed by `stop`, `status`, or orphan pruning.
- fix: recognize packaged-shim cron lines during agent enumeration, so
  orphan pruning and runtime listings see cron-scheduled agents on
  packaged installs again (previously only flat-layout `run.py` lines
  qualified).
- fix: surface crontab entries pinned to a moved or deleted project
  root in `doctor` (repo-scoped matching can never remove them), treat
  an unreadable crontab as a skipped check rather than a passing one,
  and report a fresh user's missing crontab as an empty table instead
  of a restricted sandbox in `status`.
- fix: keep the legacy Windows heartbeat wrapper doing the actual
  keep-alive work (systemd poke and beacon write) before attempting
  migration, and treat a failed migration as a warning; hosts with
  PowerShell interop disabled no longer stop heart-beating entirely.
- fix: refuse `heartbeat install --distro` for a distro other than the
  current one (the beacon verification reads the current distro's
  filesystem, so cross-distro installs always failed half-applied), and
  skip the doctor heartbeat check instead of failing it in sessions
  without `WSL_DISTRO_NAME` (sshd, cron, systemd).
- fix: make `agents-live uninstall` usable on non-WSL hosts by skipping
  the Windows heartbeat cleanup there instead of failing before the uv
  tool could be removed.
- fix: stage skill-payload installs and refreshes so an interrupted
  copy can neither destroy an existing payload nor leave a partial one
  that reports itself current.
- fix: announce an available release once per release instead of after
  every hourly background check, and read `--version` from the same
  version source the update check and doctor use.
- fix: accept `--version` in any position among the global flags, exit
  cleanly on Ctrl-C during `logs` and `dashboard`, and map a
  signal-killed delegated command to the conventional 128+signal exit
  status.
- fix: resolve status LAST OK / LAST ERR columns from the selected
  project's log directory instead of the caller's working directory,
  and from each child repository in `--all-repos` views.
- fix: a registered repository name passed to `--repo` now always
  selects the registry entry, never a same-named directory under the
  caller's current directory; registry mutations are serialized by a
  lock so concurrent `repos` commands cannot drop each other's writes;
  a child repository that fails to launch becomes that repository's
  error row instead of aborting the whole `--all-repos` aggregate.
- fix: run `--all-repos` child collection concurrently, refresh the
  read-only all-repos dashboard periodically instead of freezing at
  process start, and route its child processes through the installed
  CLI shim where the package is not importable.
- fix: harden `upgrade` for minimal-PATH contexts (uv and the freshly
  installed shim are found via the same search paths cron uses) and
  report an invalid `AGENTS_LIVE_REPO` as what it is instead of a
  registry error.
- fix: print the "using default repo" notice for `run`, which executes
  an agent and previously targeted the configured default silently.
- fix: make release readiness explicit by integrating changelog maintenance,
  enforcing the minimum semantic version bump implied by release notes, using
  portable artifact inspection, and verifying the exact published PyPI version.
- feat: drop the user-facing repository alias; `repos add <path>` registers a
  repository under its directory name, and `repos default` / `repos remove`
  accept either the path or that name.
- feat: make `agents-live upgrade` reinstall the latest uv-managed runtime
  without requiring project context, then refresh managed skill payloads from
  the newly installed CLI across the current and registered repositories;
  explicit `--repo`, `--runtime-only`, and `--skills-only` options constrain
  the workflow.
- chore: run the pre-release audit inside the PyPI publish workflow so a
  manually dispatched tag can never publish an unaudited artifact, and
  teach the audit to catch tilde-form personal paths (the shipped docs
  now use a generic `<target-project>` placeholder).

## 0.2.0 - 2026-07-18

- feat: add an XDG user repository registry with aliases, a safe last-resort
  default, documented selection precedence, and absolute-path persistence.
- feat: add isolated, partial-failure-tolerant `status --all-repos` and
  `doctor --all-repos` views plus a read-only dashboard repository selector.
- fix: reject absolute or escaping `agent_directories`, including symlink
  escapes, so within-repository discovery cannot become cross-repository access.
- fix: scope schedule, watcher, migration, and health-check crontab matching
  to the current repository, so projects sharing a user crontab cannot
  cross-report, remove, rewrite, or reject one another's entries.
- fix: honor `--json` before or after `doctor`; both forms now emit the same
  machine-readable result.
- fix: bare `agents-live logs timeline` now shows the last 50 events across
  all agents, and malformed or pre-v5 rows are skipped with a warning rather
  than aborting valid neighboring events.
- fix: `doctor` outside an initialized project now runs the host readiness
  checks instead of refusing to run; project-level checks are reported as
  skipped until `agents-live init` creates the project config.
- feat: promote the WSL Windows heartbeat to distro-level host
  infrastructure. `agents-live heartbeat install --distro <name>` registers
  one Task Scheduler task per distro that invokes the stable uv CLI shim
  and writes the beacon under the user state directory, with no project or
  checkout binding, so a single heartbeat serves every project in the
  distro. Doctor verifies the distro-scoped task and recommends migration
  for legacy checkout-, site-packages-, or project-pinned registrations,
  and `agents-live heartbeat uninstall` removes the task.
- feat: add best-effort PyPI update notifications for interactive CLI use.
  Ordinary commands refresh a shared cache in the background when it is one
  hour old and display each available stable release once; `doctor` always
  performs a fresh check and reports its status. Network, cache, and metadata
  failures never block the requested command, and agents-live never updates
  itself.
- feat: add `agents-live upgrade` as the explicit post-package-upgrade
  workflow for refreshing a project's managed skill payload. Doctor now
  recommends it when package and payload versions differ; `init` keeps its
  existing refresh behavior for compatibility.
- feat: add an `agents-live --version` flag that reports the installed
  package version.

## 0.1.6 - 2026-07-18

- fix: the framework smoketest executed lifecycle modules as script
  files (`sys.executable .../status.py` and friends), which dies in a
  packaged install on their relative imports - the last flat-invocation
  holdout, surfaced by a freshly flipped packaged host's first health
  check failing at "3/13 verify status". All twelve call sites now share a
  layout-aware argv helper: `-m agents_live.<module>` packaged, the
  sibling script file flat. A vestigial flat-era `sys.path` insert
  before the spawn-module step is removed.

## 0.1.5 - 2026-07-18

Packaged dashboard and Windows heartbeat fixes, plus guarded release
automation and a source-to-PyPI testing runbook.

- fix: `dashboard` crashed on launch in a packaged install
  (`ImportError: attempted relative import with no known parent
  package`): it still imported its siblings as flat top-level modules.
  It now branches on layout - packaged, imports go through the
  `agents_live` package; flat, the classic sys.path form. Its action
  buttons had the same latent bug (`uv run <script>` on module files
  whose relative imports need the package); packaged they now re-enter
  through the CLI shim with an explicit `--repo`, the same branch
  spawn takes.
- fix: `windows-heartbeat.sh` derived the repo root by walking up from
  its own location, which only holds in the flat checkout; from
  site-packages the beacon and log landed silently in the uv tool
  directory while Task Scheduler reported success. The repo root can
  now be passed as the first argument; packaged Task Scheduler
  registrations must pin it (`... -- bash <script> <repo>`).
- fix: doctor's "Windows heartbeat configured" check pinned the
  flat-checkout script paths, so it false-PASSed a doomed flat
  registration after migration and flagged correct packaged ones. It
  now expects the scripts installed beside the package - following the
  layout: flat, site-packages, or editable - and requires the repo to
  be pinned in the task action.
- build: `tools/release.py` now previews, prepares, and publishes a
  semantic release through guarded phases. It synchronizes every
  version surface, runs the audit/tests/build, creates an annotated
  tag, leaves target artifacts available for inspection, pushes the
  commit and tag atomically, and safely retries GitHub release creation.
- docs: the contributor testing runbook separates editable source,
  isolated wheel, and installed PyPI validation. It also documents
  update detection, skill-payload refresh, and recovery from an
  editable user-level tool installation.

## 0.1.4 - 2026-07-18

Pre-flip fixes for packaged installs (#1, #6).

- fix: `init` refreshes an existing skill payload when its VERSION
  differs from the vendored payload's, instead of returning early and
  leaving it stale (which made doctor's "rerun agents-live init after
  upgrading" hint a no-op). A refresh replaces only the payload items
  (SKILL.md, VERSION, docs, templates); anything else in the directory
  is left alone. (#1)
- fix: `spawn.spawn_agent` resolved run.py at the flat-checkout
  `.claude/skills/agents-live/scripts/` path and silently skipped in a
  packaged install. It now branches like the rest of the runtime:
  packaged execution re-enters through the CLI shim with an explicit
  `--repo`; the flat form is unchanged. The module stays stdlib-only at
  import time for standalone sys.path consumers. (#6)

## 0.1.3 - 2026-07-18

Packaged watcher fixes (found by greenfield validation of a
`uv tool install` deployment; the flat-checkout layout was unaffected).

- fix: `start` on a watcher agent in a packaged install spawned the
  watch loop via the flat-checkout `uv run --script activate.py` form,
  which dies instantly on the package's relative imports. The packaged
  form now re-enters through the CLI shim
  (`agents-live --repo <root> start --watch-loop <name>`), mirroring
  the existing `@reboot` respawn invocation.
- fix: watcher dispatch reuses `run_invocation()` instead of a
  hardcoded `uv run --script run.py` argv, so dispatch works in both
  layouts.
- fix: the watcher process matchers behind `status`, `stop`, and
  `doctor` required `activate.py` in the argv, so packaged watch loops
  showed as stopped and could not be stopped. A shared discriminator
  now also matches the CLI shim by exact basename.

## 0.1.2 - 2026-07-18

Documentation corrections; no code changes.

- docs: the release README is sourced from a maintained file next to
  SKILL.md (distilled from the overview: positioning, a live-agent
  frontmatter example, the plan/pipeline/write ladder, footprint, and
  honest limits) instead of a heredoc in the release assembler.
- docs: the README and overview state that the `/agents-live` skill is
  optional support for the CLI -- every flow it drives is an ordinary
  `agents-live` command, and the CLI is fully usable without it.
- docs: overview title simplified to "Agents Live Overview".

## 0.1.1 - 2026-07-18

Documentation corrections; no code changes.

- docs: adapters are described as they ship -- `claude` and `copilot`
  built in, with additional adapters (e.g. `agency` variants) registered
  by installed plugins rather than advertised as included.
- docs: multi-machine ownership is documented as local-only by default;
  registry mode is explicitly marked as requiring a plugin-provided
  ownership backend.
- docs: cron line examples show the installed `agents-live` entry-point
  form that activation actually writes; the source-checkout script form
  is retained as a secondary note.
- docs: diagnostics is generic -- deployment-specific agent inventories
  and examples moved out of the distributed docs.

## 0.1.0 - 2026-07-18

Initial public release.

- doctor: new check "intended watchers are running" - flags watchers with
  an @reboot line but no live process. Previously doctor passed vacuously
  when zero watchers were running (the coverage check only tests
  running-without-line, not line-without-running).
- docs: commands.md check 14 uses `pgrep -x inotifywait`; the old
  `-f "inotifywait.*"` pattern self-matched its invoking shell and
  reported a watcher when none was running.
- doctor: agent-CLI notes now distinguish agents owned by this
  host from unclaimed agents (no registry entry, no frontmatter
  `owner:`) - previously both were reported as "owned by this host".
