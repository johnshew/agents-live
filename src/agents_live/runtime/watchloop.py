"""Generic watch policy over a process-scoped ChangeSource."""
from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable
from pathlib import Path

from .grammars import Watch
from .protocols import ChangeSource


def run(
    source: ChangeSource,
    watch: Watch,
    *,
    root: Path,
    fire: Callable[[tuple[str, ...]], None],
    max_dispatches: int = 20,
    window_s: float = 60.0,
    should_continue: Callable[[], bool] | None = None,
    on_retire: Callable[[], None] | None = None,
    idle_check_s: float = 60.0,
) -> None:
    pending: set[str] = set()
    deadline: float | None = None
    dispatches: deque[float] = deque()
    retiring = False
    source.start()
    try:
        while True:
            if should_continue is not None and not should_continue():
                retiring = True
                break
            timeout = (
                idle_check_s
                if deadline is None
                else min(idle_check_s, max(0.0, deadline - time.monotonic()))
            )
            changed = source.poll(timeout)
            for value in changed:
                path = Path(value)
                try:
                    relative = path.resolve().relative_to(root.resolve()).as_posix()
                except ValueError:
                    continue
                if _ignored(relative) or not watch.matches(relative):
                    continue
                pending.add(relative)
                deadline = time.monotonic() + watch.debounce_ms / 1000
            if deadline is None or time.monotonic() < deadline:
                continue
            instant = time.monotonic()
            while dispatches and instant - dispatches[0] >= window_s:
                dispatches.popleft()
            if pending and len(dispatches) < max_dispatches:
                fire(tuple(sorted(pending)))
                dispatches.append(instant)
            pending.clear()
            deadline = None
    finally:
        source.stop()
    if retiring and on_retire is not None:
        on_retire()


def _ignored(path: str) -> bool:
    parts = Path(path).parts
    return (
        any(part.startswith(".") or part == "__pycache__" for part in parts)
        or Path(path).name == "_index_.md"
        or path.endswith(".jsonl")
    )
