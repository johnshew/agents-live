# Changelog

Reverse-chronological log of significant changes, newest first. The
changelog starts at the initial public release; earlier development
history is retained in the source repository.

## Unreleased

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
