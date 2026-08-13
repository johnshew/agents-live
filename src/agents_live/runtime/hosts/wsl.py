"""WSL host adapter with converged Windows-side liveness."""
from __future__ import annotations

import time
from collections.abc import Sequence

from ..values import DependencyHealth, Health, RuntimeTarget
from . import dependency_health as dependencies, wsl_liveness
from .posix import PosixHost


class WslHost(PosixHost):
    def prepare(self) -> None:
        wsl_liveness.ensure()

    def health(self) -> Health:
        base = super().health()
        beacon = wsl_liveness.beacon_path()
        fresh = False
        try:
            fresh = time.time() - beacon.stat().st_mtime <= 600
        except OSError:
            pass
        detail = base.detail if fresh else (*base.detail, "WSL liveness beacon is stale")
        return Health(base.healthy and fresh, "fresh" if fresh else "stale", detail=detail)

    def dependency_health(
        self, targets: Sequence[RuntimeTarget],
    ) -> tuple[DependencyHealth, ...]:
        found = []
        for target in targets:
            if not target.paired or target.runtime != "windows":
                found.append(dependencies.unknown(
                    target, "owning runtime is not reachable from this host"))
                continue
            try:
                result = self.child_runner.run_child(
                    ["powershell.exe", "-NoProfile", "-NonInteractive",
                     "-Command", "agents-live doctor --json"],
                    timeout=30,
                )
            except (OSError, RuntimeError, ValueError):
                found.append(dependencies.unknown(
                    target, "paired runtime health probe could not start"))
            else:
                found.append(dependencies.from_child(target, result))
        return tuple(found)
