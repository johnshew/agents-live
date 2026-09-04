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
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from urllib.parse import quote, unquote

ROOT = Path(__file__).resolve().parent.parent
VERSION = re.compile(r"agents_live-(?P<version>[^-]+)-py3-none-any\.whl\Z")
STABLE_VERSION = re.compile(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)\Z")


class ReadinessError(RuntimeError):
    """The public bootstrap did not produce a valid clean installation."""


def _run(argv: list[str], *, environment: dict[str, str]) -> str:
    completed = subprocess.run(
        argv, cwd=ROOT, env=environment, capture_output=True, text=True,
        encoding="utf-8", errors="replace", check=False)
    if completed.returncode != 0:
        raise ReadinessError(
            f"{' '.join(argv)} failed ({completed.returncode}):\n"
            f"{completed.stdout}\n{completed.stderr}")
    return completed.stdout


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
    completed = subprocess.run(
        ["uvx", "--from", "pip", "pip", "download", "--dest",
         str(directory), str(wheel)],
        cwd=ROOT, capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        raise ReadinessError(
            "could not prepare the dependency wheelhouse: "
            + (completed.stderr or completed.stdout))
    wheel_copy = directory / wheel.name
    wheel_copy.unlink(missing_ok=True)


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
                prefix="agents-live-bootstrap-") as temporary:
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
                "AGENTS_LIVE_NO_PATH_UPDATE": "1",
                "HOME": str(root / "home"),
                "USERPROFILE": str(root / "home"),
                "APPDATA": str(root / "appdata"),
                "LOCALAPPDATA": str(root / "localappdata"),
                "UV_TOOL_DIR": str(root / "uv-tools"),
                "UV_CACHE_DIR": str(root / "uv-cache"),
                "UV_NO_INDEX": "1",
                "UV_FIND_LINKS": str(wheelhouse),
            }
            if os.name == "nt":
                command = [
                    "pwsh", "-NoProfile", "-File",
                    str(wheel.parent / "install.ps1"),
                ]
                command_path = install_root / "current" / "Scripts" / "agents-live.exe"
            else:
                (root / "home").mkdir()
                command = ["sh", str(wheel.parent / "install.sh")]
                command_path = install_root / "current" / "bin" / "agents-live"
            first = _run(command, environment=environment)
            second = _run(command, environment=environment)
            for output in (first, second):
                if f"agents-live {version}" not in output:
                    raise ReadinessError(
                        "bootstrap did not report the installed exact version")
            installed = _run(
                [str(command_path), "--version"], environment=environment)
            if f"agents-live {version}" not in installed:
                raise ReadinessError("stable command returned the wrong version")
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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())