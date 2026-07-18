# Changelog

Reverse-chronological log of significant changes, newest first. The
changelog starts at the initial public release; earlier development
history is retained in the source repository.

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
