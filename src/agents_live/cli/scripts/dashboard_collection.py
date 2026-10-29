"""Isolate dashboard snapshot collection from the UI event loop."""
from __future__ import annotations

import atexit
import json
import subprocess
import sys
import threading
from pathlib import Path


class DashboardCollector:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._process: subprocess.Popen | None = None
        atexit.register(self.close)

    def collect(self, previous: dict, settings: dict,
                health_check_running: bool) -> tuple[dict, dict]:
        with self._lock:
            if self._process is None or self._process.poll() is not None:
                if self._process is not None:
                    self.close()
                from agents_live.runtime.hosts import system as hostruntime
                # Its own group keeps console Ctrl+C for the dashboard, which
                # stops the worker through close(); EOF on stdin also ends it.
                self._process = hostruntime.spawn_detached(
                    [sys.executable, "-u", str(Path(__file__).resolve())],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=None, text=True)
            process = self._process
            assert process.stdin is not None and process.stdout is not None
            try:
                process.stdin.write(json.dumps({
                    "previous": previous,
                    "settings": settings,
                    "health_check_running": health_check_running,
                }) + "\n")
                process.stdin.flush()
                line = process.stdout.readline()
            except OSError as exc:
                raise RuntimeError(f"dashboard collector communication failed: {exc}") from exc
            if not line:
                raise RuntimeError(
                    f"dashboard collector exited before responding (status {process.poll()})")
            try:
                response = json.loads(line)
            except ValueError as exc:
                raise RuntimeError("dashboard collector returned invalid data") from exc
            if not isinstance(response, dict) or not response.get("ok"):
                detail = response.get("error") if isinstance(response, dict) else response
                raise RuntimeError(f"dashboard collection failed: {detail}")
            return response["snapshot"], response["service"]

    def close(self) -> None:
        process = self._process
        if process is None:
            return
        self._process = None
        if process.poll() is None:
            from agents_live.runtime.hosts import system as hostruntime
            hostruntime.terminate(process.pid, grace_s=5)
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        for stream in (process.stdin, process.stdout):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    if process.poll() is None:
                        raise


def _serve() -> None:
    protocol = sys.stdout
    sys.stdout = sys.stderr
    package_parent = Path(__file__).resolve().parents[3]
    if str(package_parent) not in sys.path:
        sys.path.append(str(package_parent))
    from agents_live.cli.scripts import dashboard

    for line in sys.stdin:
        try:
            request = json.loads(line)
            dashboard.STATE["health_check_running"] = request["health_check_running"]
            snapshot, service = dashboard.collect_snapshot(
                request["previous"], request["settings"])
            response = {"ok": True, "snapshot": snapshot, "service": service}
        except Exception as exc:
            print(f"Dashboard collector failed: {type(exc).__name__}: {exc}",
                  file=sys.stderr, flush=True)
            response = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        protocol.write(json.dumps(response) + "\n")
        protocol.flush()


if __name__ == "__main__":
    _serve()
