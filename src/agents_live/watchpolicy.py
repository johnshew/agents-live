"""Watcher policy: what a batch of filesystem events means.

Everything here decides; nothing here waits on a file descriptor, reads
inotify, touches the hash cache, or logs. The watcher owns the event
source and the log, this module owns the rules that turn raw event
paths into a dispatch decision. Splitting them lets the rules be
exercised directly instead of through a live inotifywait, and keeps
them unchanged when the event source is swapped per host
(docs/windows-support.md).

The one-second collection window stays in the watcher: it is a property
of draining a pipe, not a judgement about a batch. The debounce window
below is the frontmatter ``debounce: N`` setting, which decides when an
accumulated batch is finally due.
"""
from __future__ import annotations

from collections import deque
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

# Only apply hash filtering within this window after the last dispatch.
# Cascades happen within seconds; intentional touches come later.
CASCADE_WINDOW_SECS = 120

# --- Watcher fire-rate circuit breaker ---
# Last-resort backstop against a self-triggering cascade (a post-processor
# writing into its own watched dir, or a mutating-filename rename loop). The
# content-hash guard stops identical re-fires but not loops whose content
# keeps changing (the 2026-05-02 outage: ~88 concurrent runs from a rename
# loop). If a single watcher process dispatches more than
# FIRE_RATE_MAX_DISPATCHES batches within a FIRE_RATE_WINDOW_SECS sliding
# window, the watcher logs an error-level alert (picked up by
# self-heal-log-alerts) and exits. Watchers are detached processes
# (start_new_session), so exiting stays down with no auto-restart until
# reactivated. The cap is set well above any human editing rate - a person
# grooming the task backlog cannot dispatch dozens of batches in ten minutes
# - so legitimate heavy manual use never trips it, while a machine-speed
# cascade blows past it in seconds. Tune here if an agent legitimately needs
# a different ceiling.
FIRE_RATE_WINDOW_SECS = 600
FIRE_RATE_MAX_DISPATCHES = 40

# How many paths one dispatch may carry. A rescan after an overflow can
# select two thousand files, and a prompt that lists them all is a
# prompt no agent reads and a command line some hosts refuse. The cap
# is far above any batch a person produces, so ordinary editing never
# meets it; a storm gets a readable prefix and a count of the rest
# rather than a payload that fails to send.
BATCH_FILE_LIMIT = 500


def should_ignore(changed_file: str | Path, *, root: Path,
                  watch_ignore: Sequence[str] | None = None) -> bool:
    """True when this path must never wake an agent."""
    changed_path = Path(changed_file)
    if not changed_path.is_absolute():
        changed_path = (root / changed_path).resolve()

    try:
        relative = changed_path.relative_to(root)
    except ValueError:
        return False

    if any(part.startswith(".") for part in relative.parts):
        return True
    if any(part == "__pycache__" for part in relative.parts):
        return True
    if relative.name == "_index_.md":
        return True
    # Ignore JSONL log files written by the agents-live system itself
    # (prevents recursive triggers), but allow other files under Agents/logs/
    # so watcher agents that deliberately watch subdirectories still fire.
    if relative.suffix == ".log" and (
        relative == Path("Agents/logs") / relative.name
        or Path("Agents/logs") in relative.parents
    ):
        return True
    if watch_ignore and relative.name in watch_ignore:
        return True
    # Support directory-prefix ignores: entries ending with '/' match any
    # file whose repo-relative path starts with that prefix.
    if watch_ignore:
        rel_str = relative.as_posix()
        for pattern in watch_ignore:
            if pattern.endswith("/") and (rel_str + "/").startswith(pattern):
                return True
    return False


def _wanted_by_target(changed_file: str, target_filenames: frozenset[str],
                      dir_targets: Sequence[Path]) -> bool:
    """Whether a file-target watcher cares about this path.

    A watcher asked to watch a single file watches its parent directory
    instead (inotifywait on a file breaks on atomic saves), so events for
    the directory's other files arrive too and are filtered here. Events
    from a directory target the same watcher also holds pass through, and
    so do their recursive subdirectories.
    """
    if not target_filenames:
        return True
    changed = Path(changed_file)
    if changed.name in target_filenames:
        return True
    return any(str(changed).startswith(str(d)) for d in dir_targets)


def select_batch(raw_paths: Iterable[str], *, root: Path,
                 target_filenames: frozenset[str] = frozenset(),
                 dir_targets: Sequence[Path] = (),
                 watch_ignore: Sequence[str] | None = None) -> list[str]:
    """Repo-relative, de-duplicated paths this watcher should act on."""
    selected: list[str] = []
    for raw in raw_paths:
        if not _wanted_by_target(raw, target_filenames, dir_targets):
            continue
        if should_ignore(raw, root=root, watch_ignore=watch_ignore):
            continue
        try:
            selected.append(str(Path(raw).relative_to(root)))
        except ValueError:
            selected.append(raw)
    return list(dict.fromkeys(selected))


def bound_batch(paths: Sequence[str],
                limit: int = BATCH_FILE_LIMIT) -> tuple[list[str], int]:
    """The paths a dispatch may carry, and how many were left out.

    Truncating with a count is the honest form: the agent is handed a
    list it can act on, and the caller has a number to log. Silently
    dropping the tail would leave neither.
    """
    if len(paths) <= limit:
        return list(paths), 0
    return list(paths[:limit]), len(paths) - limit


@dataclass(frozen=True)
class CascadeDecision:
    """What survived the content-hash guard, and what it hashed to."""

    dispatch: list[str]
    skipped: list[str]
    hashes: dict[str, str]


def apply_cascade_guard(files: Sequence[str], *,
                        cached_hashes: dict[str, str],
                        last_dispatch_at: float, now: float,
                        hasher: Callable[[str], str | None],
                        window_secs: float = CASCADE_WINDOW_SECS,
                        ) -> CascadeDecision:
    """Drop files whose content is unchanged since the last dispatch.

    Active only within ``window_secs`` of that dispatch: outside the
    window every event dispatches, so `touch` still works. Files that
    cannot be hashed (deleted, unreadable) stay in the batch, because a
    deletion is a change the agent may need to see.
    """
    in_window = (now - last_dispatch_at) < window_secs
    hashes: dict[str, str] = {}
    dispatch: list[str] = []
    skipped: list[str] = []
    for name in files:
        digest = hasher(name)
        if digest is None:
            dispatch.append(name)
            continue
        hashes[name] = digest
        if in_window and cached_hashes.get(name) == digest:
            skipped.append(name)
        else:
            dispatch.append(name)
    return CascadeDecision(dispatch=dispatch, skipped=skipped, hashes=hashes)


@dataclass
class FireRateBreaker:
    """Sliding-window cap on dispatches from one watcher process."""

    window_secs: float = FIRE_RATE_WINDOW_SECS
    max_dispatches: int = FIRE_RATE_MAX_DISPATCHES
    _history: deque[float] = field(default_factory=deque, init=False)

    def record(self, now: float) -> bool:
        """Record a dispatch; True when it puts the rate over the cap."""
        self._history.append(now)
        cutoff = now - self.window_secs
        while self._history and self._history[0] < cutoff:
            self._history.popleft()
        return len(self._history) > self.max_dispatches

    @property
    def count(self) -> int:
        """Dispatches inside the current window."""
        return len(self._history)


@dataclass
class DebounceWindow:
    """Frontmatter ``debounce: N``: batches merge until N seconds of quiet.

    Every added batch restarts the window, so a burst of saves produces
    one dispatch. The watcher asks for :meth:`remaining` to size its
    wait and calls :meth:`take` when the window closes or when it is
    shutting down and has to say what it is discarding.
    """

    delay: float
    files: list[str] = field(default_factory=list, init=False)
    deadline: float | None = field(default=None, init=False)

    def add(self, files: Iterable[str], now: float) -> None:
        for name in files:
            if name not in self.files:
                self.files.append(name)
        self.deadline = now + self.delay

    def remaining(self, now: float) -> float | None:
        """Seconds left in the window, or None when none is pending."""
        if self.deadline is None:
            return None
        return self.deadline - now

    def take(self) -> list[str]:
        """Drain the accumulated files and close the window."""
        files = self.files
        self.files = []
        self.deadline = None
        return files
