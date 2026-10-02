#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.12"
# ///
"""Exercise the public bootstrap against authenticated local release assets."""
from __future__ import annotations

import argparse
import hashlib
import http.server
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path
from urllib.parse import quote, unquote

ROOT = Path(__file__).resolve().parent.parent
VERSION = re.compile(r"agents_live-(?P<version>[^-]+)-py3-none-any\.whl\Z")
STABLE_VERSION = re.compile(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)\Z")
COMMAND_TIMEOUT_SECONDS = 300
SHUTDOWN_GRACE_SECONDS = 5


class ReadinessError(RuntimeError):
    """The public bootstrap did not produce a valid clean installation."""


def _terminate(
    process: subprocess.Popen[str], stop_tree: Callable[[], None] | None = None,
) -> None:
    """Reap only the process tree launched for this fixture command."""
    if os.name == "nt":
        if stop_tree is not None:
            stop_tree()
        elif process.poll() is None:
            process.kill()
    else:
        # The leader can exit while a descendant still holds the output pipes.
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(process.pid, sig)
            except ProcessLookupError:
                break
            deadline = time.monotonic() + SHUTDOWN_GRACE_SECONDS
            while time.monotonic() < deadline:
                process.poll()
                try:
                    os.killpg(process.pid, 0)
                except ProcessLookupError:
                    break
                except PermissionError:
                    pass
                time.sleep(0.05)
            else:
                continue
            break
        else:
            raise ReadinessError(
                f"bootstrap process group {process.pid} survived cleanup")
    try:
        process.communicate(timeout=SHUTDOWN_GRACE_SECONDS)
    except subprocess.TimeoutExpired as exc:
        raise ReadinessError(
            f"bootstrap process {process.pid} did not finish cleanup") from exc


def _execute(
    argv: list[str], *, environment: dict[str, str],
    timeout: float = COMMAND_TIMEOUT_SECONDS,
) -> subprocess.CompletedProcess[str]:
    if os.name == "nt":
        sys.path.insert(0, str(ROOT / "src"))
        try:
            from agents_live.runtime.hosts.system import supervise_child
        finally:
            sys.path.pop(0)

    process = subprocess.Popen(
        argv, cwd=ROOT, env=environment, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        encoding="utf-8", errors="replace", start_new_session=os.name != "nt",
        creationflags=0x00000004 if os.name == "nt" else 0)
    stop_tree = None
    try:
        if os.name == "nt":
            stop_tree = supervise_child(process)
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            raise ReadinessError(
                f"{' '.join(argv)} timed out after {timeout:g} seconds") from exc
        return subprocess.CompletedProcess(argv, process.returncode, stdout, stderr)
    finally:
        try:
            _terminate(process, stop_tree)
        finally:
            if process.stdout is not None:
                process.stdout.close()
            if process.stderr is not None:
                process.stderr.close()


def _run(
    argv: list[str], *, environment: dict[str, str],
    timeout: float = COMMAND_TIMEOUT_SECONDS,
) -> str:
    completed = _execute(argv, environment=environment, timeout=timeout)
    if completed.returncode != 0:
        raise ReadinessError(
            f"{' '.join(argv)} failed ({completed.returncode}):\n"
            f"{completed.stdout}\n{completed.stderr}")
    return completed.stdout


def _run_failure(
    argv: list[str], *, environment: dict[str, str],
    timeout: float = COMMAND_TIMEOUT_SECONDS,
) -> str:
    completed = _execute(argv, environment=environment, timeout=timeout)
    if completed.returncode == 0:
        raise ReadinessError(
            f"{' '.join(argv)} unexpectedly succeeded:\n{completed.stdout}")
    return completed.stdout + completed.stderr


def _assets(wheel: Path) -> dict[str, bytes]:
    paths = [wheel, wheel.parent / "install.ps1", wheel.parent / "install.sh"]
    missing = [str(path) for path in paths if not path.is_file()]
    if missing:
        raise ReadinessError("missing bootstrap assets: " + ", ".join(missing))
    return {path.name: path.read_bytes() for path in paths}


def _metadata(version: str, assets: dict[str, bytes], base: str) -> bytes:
    document = {
        "tag_name": f"v{version}",
        "draft": False,
        "prerelease": STABLE_VERSION.fullmatch(version) is None,
        "assets": [
            {
                "name": name,
                "state": "uploaded",
                "browser_download_url": quote(
                    f"{base}/download/v{version}/{name}", safe=":/"),
                "digest": "sha256:" + hashlib.sha256(content).hexdigest(),
                "size": len(content),
            }
            for name, content in sorted(assets.items())
        ],
    }
    return json.dumps(document).encode("utf-8")


def _server(version: str, assets: dict[str, bytes]):
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            base = f"http://127.0.0.1:{self.server.server_port}"
            path = unquote(self.path)
            if path in ("/releases/latest", f"/releases/tags/v{version}"):
                content = _metadata(version, assets, base)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
            elif path.startswith(f"/download/v{version}/"):
                name = path.rsplit("/", 1)[-1]
                content = assets.get(name, b"")
                self.send_response(200 if name in assets else 404)
                self.send_header("Content-Type", "application/octet-stream")
            else:
                content = b"not found"
                self.send_response(404)
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

        def log_message(self, _format: str, *_args: object) -> None:
            return

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def _prepare_wheelhouse(wheel: Path, directory: Path) -> None:
    _run(
        ["uvx", "--from", "pip", "pip", "download", "--dest",
         str(directory), str(wheel)],
        environment=dict(os.environ))
    wheel_copy = directory / wheel.name
    wheel_copy.unlink(missing_ok=True)


def _isolate_posix_host(root: Path, environment: dict[str, str]) -> None:
    """Keep scheduler state and process supervision inside the bootstrap fixture."""
    fixture = root / "host fixture"
    fixture.mkdir()
    table = fixture / "crontab.txt"
    table.write_text("# bootstrap fixture\n", encoding="utf-8")
    script = fixture / "crontab.py"
    script.write_text('''
from pathlib import Path
import sys

table = Path(__file__).with_name("crontab.txt")
if sys.argv[1:] == ["-l"]:
    sys.stdout.write(table.read_text(encoding="utf-8"))
elif sys.argv[1:] == ["-"]:
    table.write_text(sys.stdin.read(), encoding="utf-8")
else:
    raise SystemExit("bootstrap fixture: unsupported crontab arguments")
''', encoding="utf-8")
    command = fixture / "crontab"
    command.write_text(
        f"#!/bin/sh\nexec {shlex.quote(sys.executable)} "
        f"{shlex.quote(str(script))} \"$@\"\n", encoding="utf-8")
    command.chmod(0o755)
    (fixture / "sitecustomize.py").write_text('''
import importlib.util
import os
import sys

try:
    if importlib.util.find_spec("agents_live") is not None:
        from agents_live import runtime
        from agents_live.runtime.hosts.memory import MemorySupervisor
        from agents_live.runtime.hosts.posix import PosixHost

        host = PosixHost()
        host.supervisor = MemorySupervisor()
        runtime.configure(host)
except Exception as error:
    print(f"bootstrap fixture isolation failed: {error}", file=sys.stderr, flush=True)
    os._exit(1)
''', encoding="utf-8")
    environment["PATH"] = os.pathsep.join(
        (str(fixture), environment.get("PATH", "")))
    environment["PYTHONPATH"] = str(fixture)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment.pop("WSL_DISTRO_NAME", None)


def _posix_collision_checks(command: list[str], *, environment: dict[str, str]) -> None:
    """Run the bootstrap against foreign and unresolved commands, without activation."""
    for kind in ("file", "directory", "foreign-link", "broken-link",
                 "loop", "missing-target"):
        with tempfile.TemporaryDirectory(
                prefix="agents-live-collision with spaces-") as temporary:
            root = Path(temporary)
            home = root / "home"
            install_root = root / "install"
            link_root = home / ".local" / "bin"
            link_root.mkdir(parents=True)
            link = link_root / "al"
            foreign = home / "foreign"
            foreign.write_text("foreign command\n", encoding="utf-8")
            target = install_root / "current" / "bin" / "al"
            if kind == "file":
                link.write_text("foreign command\n", encoding="utf-8")
            elif kind == "directory":
                link.mkdir()
                (link / "keep").write_text("foreign command\n", encoding="utf-8")
            else:
                destination = {
                    "foreign-link": foreign,
                    "broken-link": home / "missing",
                    "loop": link,
                    "missing-target": target,
                }[kind]
                if kind == "missing-target":
                    target.parent.mkdir(parents=True)
                link.symlink_to(destination)
            before = sorted(install_root.rglob("*"))
            scenario_environment = {
                **environment,
                "HOME": str(home),
                "AGENTS_LIVE_INSTALL_ROOT": str(install_root),
                "XDG_CONFIG_HOME": str(root / "config"),
                "XDG_DATA_HOME": str(root / "data"),
                "XDG_STATE_HOME": str(root / "state"),
            }
            refusal = _run_failure(command, environment=scenario_environment)
            if "already exists and does not point" not in refusal:
                raise ReadinessError(
                    f"{kind}: bootstrap collision refusal did not explain the conflict")
            if kind == "file":
                intact = link.is_file() and not link.is_symlink() and (
                    link.read_text(encoding="utf-8") == "foreign command\n")
            elif kind == "directory":
                intact = link.is_dir() and not link.is_symlink() and (
                    (link / "keep").read_text(encoding="utf-8") == "foreign command\n")
            else:
                intact = link.is_symlink() and link.readlink() == destination
            if not intact or foreign.read_text(encoding="utf-8") != "foreign command\n":
                raise ReadinessError(f"{kind}: bootstrap replaced a foreign command")
            exposed = link_root / "agents-live"
            if exposed.exists() or exposed.is_symlink():
                raise ReadinessError(
                    f"{kind}: bootstrap partially exposed commands after a collision")
            if (sorted(install_root.rglob("*")) != before
                    or install_root.exists() != (kind == "missing-target")):
                raise ReadinessError(
                    f"{kind}: bootstrap changed the install root after a collision")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheel", type=Path, required=True)
    args = parser.parse_args()
    wheel = args.wheel.resolve()
    match = VERSION.fullmatch(wheel.name)
    if match is None:
        raise ReadinessError(f"not an Agents Live wheel: {wheel.name}")
    version = match.group("version")
    assets = _assets(wheel)
    server = _server(version, assets)
    try:
        with tempfile.TemporaryDirectory(
                prefix="agents-live-bootstrap with spaces-") as temporary:
            root = Path(temporary)
            wheelhouse = root / "wheelhouse"
            wheelhouse.mkdir()
            _prepare_wheelhouse(wheel, wheelhouse)
            install_root = root / "install"
            environment = {
                **os.environ,
                "AGENTS_LIVE_INSTALL_ROOT": str(install_root),
                "AGENTS_LIVE_REPO": "",
                "AGENTS_LIVE_RELEASE_API": (
                    f"http://127.0.0.1:{server.server_port}/releases"),
                "AGENTS_LIVE_RELEASE_DOWNLOAD_ROOT": (
                    f"http://127.0.0.1:{server.server_port}/download"),
                "HOME": str(root / "home"),
                "USERPROFILE": str(root / "home"),
                "APPDATA": str(root / "appdata"),
                "LOCALAPPDATA": str(root / "localappdata"),
                "XDG_CONFIG_HOME": str(root / "config"),
                "XDG_DATA_HOME": str(root / "data"),
                "XDG_STATE_HOME": str(root / "state"),
                "UV_TOOL_DIR": str(root / "uv-tools"),
                "UV_CACHE_DIR": str(root / "uv-cache"),
                "UV_NO_INDEX": "1",
                "UV_FIND_LINKS": str(wheelhouse),
            }
            if os.name == "nt":
                environment["AGENTS_LIVE_NO_PATH_UPDATE"] = "1"
                installer = str(wheel.parent / "install.ps1").replace("'", "''")
                command = [
                    "pwsh", "-NoProfile", "-Command",
                    f"& '{installer}'; agents-live --version",
                ]
                command_path = install_root / "current" / "Scripts" / "agents-live.exe"
            else:
                (root / "home").mkdir()
                link_root = root / "home" / ".local" / "bin"
                environment["PATH"] = os.pathsep.join(
                    (str(link_root), environment.get("PATH", "")))
                _isolate_posix_host(root, environment)
                command = ["sh", str(wheel.parent / "install.sh")]
                command_path = install_root / "current" / "bin" / "agents-live"
                link_root.mkdir(parents=True)
                _posix_collision_checks(command, environment=environment)
            first = _run(command, environment=environment)
            second = _run(command, environment=environment)
            for output in (first, second):
                if f"agents-live {version}" not in output:
                    raise ReadinessError(
                        "bootstrap did not report the installed exact version")
                if os.name != "nt" and "Open a new shell" in output:
                    raise ReadinessError(
                        "bootstrap requested a restart when user bin was on PATH")
            installed = _run(
                [str(command_path), "--version"], environment=environment)
            if f"agents-live {version}" not in installed:
                raise ReadinessError("stable command returned the wrong version")
            if os.name != "nt":
                discovered = _run(
                    ["agents-live", "--version"], environment=environment)
                if f"agents-live {version}" not in discovered:
                    raise ReadinessError(
                        "bare command discovery returned the wrong version")
                for name in ("agents-live", "al"):
                    link = link_root / name
                    expected = install_root / "current" / "bin" / name
                    if (not link.is_symlink() or not expected.is_file()
                            or link.resolve(strict=True) != expected.resolve(strict=True)):
                        raise ReadinessError(
                            f"bootstrap did not expose the stable {name} command")
                stale_environment = dict(environment)
                stale_environment["PATH"] = os.pathsep.join(
                    entry for entry in environment["PATH"].split(os.pathsep)
                    if entry != str(link_root))
                restart = _run(command, environment=stale_environment)
                if "Open a new shell" not in restart:
                    raise ReadinessError(
                        "bootstrap did not explain stale-shell command discovery")
            if ((install_root / "current").resolve()
                    != (install_root / "versions" / version).resolve()):
                raise ReadinessError("bootstrap activated the wrong generation")
            if (install_root / "current.json").exists():
                raise ReadinessError("bootstrap left a duplicate current pointer")
            if (install_root / "owner.json").read_text(
                    encoding="utf-8").strip() != "agents-live":
                raise ReadinessError("bootstrap did not adopt installation ownership")
            generations = [
                path.name for path in (install_root / "versions").iterdir()
                if path.is_dir() and (path / "generation.json").is_file()]
            if generations != [version]:
                raise ReadinessError(
                    f"idempotent bootstrap left generations {generations}")
    finally:
        server.shutdown()
        server.server_close()
    print(f"bootstrap readiness passed for {version} on {sys.platform}")
    if os.name != "nt":
        print("Host scheduler state and watcher supervision were fixture-isolated.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())