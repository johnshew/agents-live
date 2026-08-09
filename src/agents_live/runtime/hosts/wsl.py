"""WSL host adapter with converged Windows-side liveness."""
from __future__ import annotations

import time

from ..values import Health
from . import heartbeat
from .posix import PosixHost


class WslHost(PosixHost):
    def prepare(self) -> None:
        heartbeat.ensure()

    def health(self) -> Health:
        base = super().health()
        beacon = heartbeat.beacon_path()
        fresh = False
        try:
            fresh = time.time() - beacon.stat().st_mtime <= 600
        except OSError:
            pass
        detail = base.detail if fresh else (*base.detail, "WSL liveness beacon is stale")
        return Health(base.healthy and fresh, "fresh" if fresh else "stale", detail=detail)
