# Changelog

Reverse-chronological log of significant changes, newest first. The
changelog starts at the initial public release; earlier development
history is retained in the source repository.

## Unreleased

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

## 0.1.6 - 2026-07-18

- fix: the framework smoketest executed lifecycle modules as script
  files (`sys.executable .../status.py` and friends), which dies in a
  packaged install on their relative imports - the last flat-invocation
  holdout, surfaced by razor15's first post-flip health check failing
  at "3/13 verify status". All twelve call sites now share a
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
- prereqs/doctor: agent-CLI notes now distinguish agents owned by this
  host from unclaimed agents (no registry entry, no frontmatter
  `owner:`) - previously both were reported as "owned by this host".
