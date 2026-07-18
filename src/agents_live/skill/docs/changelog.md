# Changelog

Reverse-chronological log of significant changes, newest first. The
changelog starts at the initial public release; earlier development
history is retained in the source repository.

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
