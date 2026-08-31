#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["PyYAML", "mcp[cli]<2", "jsonschema"]
# ///
"""Deploy the current bake branch into the active local installation."""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import platform
import re
import runpy
import signal
import shlex
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import tomllib


ROOT = Path(__file__).resolve().parent.parent
CHANNELS = ROOT / ".github" / "release-channels.toml"
SOURCE = ROOT / "src"
if str(SOURCE) not in sys.path:
    sys.path.insert(0, str(SOURCE))
from agents_live.runtime.hosts import system as hostruntime  # noqa: E402
from agents_live.runtime.hosts.processes import watchers_on_host  # noqa: E402

RELEASE = runpy.run_path(str(ROOT / "tools" / "release.py"))
RELEASE_ERROR = RELEASE["ReleaseError"]
LOCAL_PREPARATION_SCHEMA = 1
LOCAL_DEPLOYMENT_SCHEMA = 1
READY_TIMEOUT_S = 180.0
LOCAL_GATES = (
    ("git", "archive", "HEAD"),
    ("uv", "build", "--wheel", "<git-archive>"),
    ("uv", "run", "--script", "tools/dashboard-readiness.py", "--wheel",
     "<immutable-wheel>"),
)


class LocalDeployError(RuntimeError):
    """A local deployment precondition or verification failed."""


@dataclass(frozen=True)
class Dashboard:
    port: int
    pid: int
    repository: str | None
    modes: tuple[str, ...]


def _say(message: str) -> None:
    print(f"+ local deploy: {message}", flush=True)


def _run(
    argv: list[str], *, capture: bool = False, check: bool = True,
) -> subprocess.CompletedProcess[str]:
    _say(shlex.join(argv))
    environment = os.environ.copy()
    environment.pop("AGENTS_LIVE_REPO", None)
    completed = subprocess.run(
        argv, cwd=ROOT, env=environment, capture_output=capture,
        text=True, encoding="utf-8", errors="replace", check=False,
    )
    if check and completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise LocalDeployError(
            f"{' '.join(argv)} exited {completed.returncode}: {detail}")
    return completed


def _git(*args: str) -> str:
    return _run(["git", *args], capture=True).stdout.strip()


def _bake_configuration() -> tuple[str, str]:
    try:
        bake = tomllib.loads(CHANNELS.read_text(encoding="utf-8"))["bake"]
        branch = bake["branch"]
        version = bake["version"]
    except (OSError, KeyError, TypeError, tomllib.TOMLDecodeError) as exc:
        raise LocalDeployError("cannot read the configured bake channel") from exc
    if not isinstance(branch, str) or not branch.startswith("bake/v"):
        raise LocalDeployError(f"invalid bake branch: {branch!r}")
    if not isinstance(version, str) or re.fullmatch(r"\d+\.\d+\.\d+", version) is None:
        raise LocalDeployError(f"invalid bake version: {version!r}")
    return branch, version


def _synchronize() -> str:
    if _git("status", "--porcelain"):
        raise LocalDeployError("working tree is not clean; refusing to pull")
    branch, _version = _bake_configuration()
    if _git("branch", "--show-current") != branch:
        raise LocalDeployError(
            f"local deployment requires the configured bake branch {branch}")
    _run(["git", "pull", "--ff-only", "origin", branch])
    head = _git("rev-parse", "HEAD")
    if head != _git("rev-parse", f"origin/{branch}") \
            or _git("status", "--porcelain"):
        raise LocalDeployError(
            f"{branch} is not clean and synchronized with origin/{branch}")
    return head


def _require_unchanged_checkout(commit: str) -> None:
    if _git("rev-parse", "HEAD") != commit or _git("status", "--porcelain"):
        raise LocalDeployError(
            "checkout changed while local deployment evidence was prepared")


def _state_directory() -> Path:
    value = _git("rev-parse", "--git-path", "agents-live-local-deploy")
    path = Path(value)
    return path if path.is_absolute() else ROOT / path


def _atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8")
    os.replace(temporary, path)


def _prepared_artifact(commit: str, version: str) -> tuple[Path, str] | None:
    receipt = _state_directory() / "preparation.json"
    try:
        payload = json.loads(receipt.read_text(encoding="utf-8"))
        wheel = Path(payload["wheel"])
    except (OSError, KeyError, TypeError, json.JSONDecodeError):
        return None
    expected = {
        "schema": LOCAL_PREPARATION_SCHEMA,
        "prepared": True,
        "commit": commit,
        "version": version,
        "platform": sys.platform,
        "os_name": os.name,
        "architecture": platform.machine(),
        "gates": [list(command) for command in LOCAL_GATES],
    }
    if any(payload.get(key) != value for key, value in expected.items()) \
            or not wheel.is_file():
        return None
    digest = str(payload.get("wheel_sha256", ""))
    if RELEASE["_sha256"](wheel) != digest:
        return None
    _say(f"reusing validated artifact {wheel.name} ({commit[:8]})")
    return wheel.resolve(), digest


def _prepare_artifact(commit: str, version: str) -> tuple[Path, str]:
    reusable = _prepared_artifact(commit, version)
    if reusable is not None:
        return reusable
    with tempfile.TemporaryDirectory(prefix="agents-live-local-deploy-") as temp:
        temporary = Path(temp)
        archive = temporary / "source.zip"
        _run(["git", "archive", "--format=zip", f"--output={archive}", commit])
        source = temporary / "source"
        shutil.unpack_archive(archive, source)
        _stamp_bake_version(source, RELEASE["_current_version"](), version)
        output = temporary / "dist"
        _run(["uv", "build", "--wheel", "--out-dir", str(output), str(source)])
        wheels = list(output.glob(f"agents_live-{version}-*.whl"))
        if len(wheels) != 1:
            raise LocalDeployError(
                f"immutable build produced {len(wheels)} wheels for {version}")
        wheel = wheels[0]
        digest = RELEASE["_sha256"](wheel)
        artifact = (
            _state_directory() / "artifacts" /
            f"{commit}-{digest}" / wheel.name)
        artifact.parent.mkdir(parents=True, exist_ok=True)
        if not artifact.is_file():
            shutil.copy2(wheel, artifact)
    if RELEASE["_sha256"](artifact) != digest:
        raise LocalDeployError("immutable deployment artifact digest changed")
    _run([
        "uv", "run", "--script", "tools/dashboard-readiness.py",
        "--wheel", str(artifact.resolve()),
    ])
    _require_unchanged_checkout(commit)
    _atomic_json(_state_directory() / "preparation.json", {
        "schema": LOCAL_PREPARATION_SCHEMA,
        "prepared": True,
        "prepared_at": datetime.now(timezone.utc).isoformat(),
        "commit": commit,
        "version": version,
        "wheel": str(artifact.resolve()),
        "wheel_sha256": digest,
        "platform": sys.platform,
        "os_name": os.name,
        "architecture": platform.machine(),
        "gates": [list(command) for command in LOCAL_GATES],
    })
    return artifact.resolve(), digest


def _stamp_bake_version(source: Path, current: str, target: str) -> None:
    replacements = (
        (source / "pyproject.toml", f'version = "{current}"',
         f'version = "{target}"'),
        (source / "src" / "agents_live" / "__init__.py",
         f'__version__ = "{current}"', f'__version__ = "{target}"'),
        (source / "src" / "agents_live" / "skill" / "VERSION",
         f"{current}\n", f"{target}\n"),
    )
    for path, old, new in replacements:
        try:
            content = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise LocalDeployError(
                f"cannot stamp bake version in {path.relative_to(source)}") from exc
        if content.count(old) != 1:
            raise LocalDeployError(
                f"cannot find one {current} version in {path.relative_to(source)}")
        path.write_text(content.replace(old, new), encoding="utf-8")


def _version_tuple(value: str) -> tuple[int, int, int]:
    match = re.match(r"^(\d+)\.(\d+)\.(\d+)(?:\.|\+|$)", value)
    if match is None:
        raise LocalDeployError(f"invalid package version: {value!r}")
    return tuple(int(part) for part in match.groups())


def _installed_cli() -> Path:
    return Path(RELEASE["_installed_cli"]())


def _installed_run(*args: str) -> subprocess.CompletedProcess[str]:
    return RELEASE["_installed_run"](list(args))


def _logical_watchers(
    rows: list[tuple[int, str, str | None]],
) -> tuple[tuple[str, str], ...]:
    """Deduplicate physical launcher/child rows into logical watchers."""
    return tuple(sorted({
        (str(Path(project).resolve()), name)
        for _pid, name, project in rows
        if project
    }))


def _dashboard_modes(pid: int) -> tuple[str, ...]:
    process_lines = hostruntime.process_command_lines()
    command = next((line for process_id, line in process_lines
                    if process_id == pid), None)
    if command is None:
        raise LocalDeployError(
            f"cannot read dashboard process {pid} before stopping it")
    argv = hostruntime.split_command_line(command)
    return tuple(flag for flag in ("--all-repos", "--dev", "--native", "--open")
                 if flag in argv)


def _dashboards(output: str) -> tuple[Dashboard, ...]:
    dashboards = []
    for line in output.splitlines():
        columns = re.split(r"\s{2,}", line.strip())
        if len(columns) == 6 and columns[0].isdigit() \
                and columns[2].isdigit():
            port, pid_value, repository_value = (
                columns[0], columns[2], columns[5])
        elif len(columns) == 5 and columns[0].isdigit() \
                and columns[1].isdigit():
            port, pid_value, repository_value = (
                columns[0], columns[1], columns[4])
        else:
            continue
        pid = int(pid_value)
        modes = _dashboard_modes(pid)
        repository = None if repository_value == "-" else repository_value
        if repository is None and "--all-repos" not in modes:
            raise LocalDeployError(
                f"dashboard on {port} has no restorable repository")
        dashboards.append(Dashboard(
            int(port), pid, repository, modes))
    return tuple(dashboards)


def _running_dashboards() -> tuple[Dashboard, ...]:
    completed = _installed_run("dashboard", "list")
    if completed.returncode != 0:
        raise LocalDeployError(
            completed.stderr.strip() or completed.stdout.strip())
    return _dashboards(completed.stdout)


def _port_answers(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.5)
        return probe.connect_ex(("127.0.0.1", port)) == 0


def _await_port_closed(port: int, *, timeout_s: float = 10.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if not _port_answers(port):
            return
        time.sleep(0.1)
    raise LocalDeployError(f"dashboard port {port} remained in use")


def _stop_dashboard(dashboard: Dashboard) -> None:
    completed = _installed_run(
        "dashboard", "stop", "--port", str(dashboard.port))
    try:
        _await_port_closed(dashboard.port)
    except LocalDeployError:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise LocalDeployError(
            f"could not stop dashboard on {dashboard.port}: {detail}")


def _api(port: int) -> dict | None:
    try:
        with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/api/agents", timeout=2) as response:
            value = json.loads(response.read().decode("utf-8"))
            return value if isinstance(value, dict) else None
    except (urllib.error.URLError, OSError, json.JSONDecodeError, TimeoutError):
        return None


def _await_api_rows(
    port: int, *, timeout_s: float = READY_TIMEOUT_S,
) -> dict:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        payload = _api(port)
        if payload and payload.get("agents"):
            return payload
        time.sleep(0.5)
    raise LocalDeployError(
        f"dashboard on {port} did not serve agent rows")


def _start_dashboard(dashboard: Dashboard) -> None:
    argv = [str(_installed_cli())]
    if dashboard.repository is not None:
        argv.extend(("--repo", dashboard.repository))
    argv.extend(("dashboard", "--port", str(dashboard.port), *dashboard.modes))
    options: dict[str, object] = {}
    if os.name == "nt":
        options["creationflags"] = (
            subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS)
    else:
        options["start_new_session"] = True
    process = subprocess.Popen(
        argv, cwd=dashboard.repository or ROOT, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, **options)
    deadline = time.monotonic() + READY_TIMEOUT_S
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise LocalDeployError(
                f"dashboard on {dashboard.port} exited {process.returncode}")
        try:
            _await_api_rows(dashboard.port, timeout_s=1.0)
        except LocalDeployError:
            continue
        return
    _terminate_dashboard_tree(process, dashboard.port)
    raise LocalDeployError(
        f"dashboard on {dashboard.port} did not serve agent rows")


def _terminate_dashboard_tree(
    process: subprocess.Popen, port: int,
) -> None:
    managed = _installed_run("dashboard", "stop", "--port", str(port))
    if managed.returncode == 0:
        _await_port_closed(port)
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/T", "/F", "/PID", str(process.pid)],
            capture_output=True, check=False)
    else:
        with contextlib.suppress(ProcessLookupError, OSError):
            os.killpg(os.getpgid(process.pid), signal.SIGTERM)
    with contextlib.suppress(subprocess.TimeoutExpired):
        process.wait(timeout=10)
    _await_port_closed(port)


def _restart_dashboards(dashboards: tuple[Dashboard, ...]) -> None:
    failures = []
    for dashboard in dashboards:
        if _port_answers(dashboard.port):
            continue
        try:
            _start_dashboard(dashboard)
        except Exception as exc:
            failures.append((dashboard.port, str(exc)))
    if failures:
        detail = "; ".join(
            f"port {port}: {error}" for port, error in failures)
        raise LocalDeployError(f"could not restore dashboards: {detail}")


def _upgrade_once(repo: Path, wheel: Path) -> str | None:
    completed = _installed_run(
        "--repo", str(repo), "upgrade", "--from", str(wheel))
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise LocalDeployError(f"local-wheel upgrade failed: {detail}")
    match = RELEASE["QUEUED_UPGRADE_RE"].search(completed.stdout)
    if match is None:
        if os.name == "nt":
            raise LocalDeployError(
                "Windows upgrade did not queue a durable helper result")
        return None
    operation_id = match.group("operation")
    result = RELEASE["_wait_for_upgrade_result"](
        Path(match.group("result").strip()))
    if result.get("operation_id") != operation_id \
            or result.get("exit_code") != 0:
        raise LocalDeployError(f"deferred upgrade failed: {result}")
    return operation_id


def _upgrade(repo: Path, wheel: Path, digest: str) -> str | None:
    attempts = 2 if os.name == "nt" else 1
    for attempt in range(1, attempts + 1):
        if RELEASE["_sha256"](wheel) != digest:
            raise LocalDeployError("deployment wheel changed before replacement")
        try:
            operation_id = _upgrade_once(repo, wheel)
        except (LocalDeployError, RELEASE_ERROR):
            if attempt == attempts:
                raise
            _say("Windows upgrade failed; retrying once before dashboard restart")
            continue
        if RELEASE["_sha256"](wheel) != digest:
            raise LocalDeployError("deployment wheel changed during replacement")
        return operation_id
    raise AssertionError("unreachable")


def _direct_url() -> Path:
    executable = _installed_cli()
    environment = executable.parent.parent
    patterns = (
        "Lib/site-packages/agents_live-*.dist-info/direct_url.json",
        "lib/python*/site-packages/agents_live-*.dist-info/direct_url.json",
    )
    matches = [path for pattern in patterns for path in environment.glob(pattern)]
    if len(matches) != 1:
        raise LocalDeployError(
            f"installed runtime has {len(matches)} direct_url.json records")
    try:
        value = json.loads(matches[0].read_text(encoding="utf-8"))
        parsed = urllib.parse.urlparse(value["url"])
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise LocalDeployError("installed direct_url.json is invalid") from exc
    if parsed.scheme != "file":
        raise LocalDeployError(
            f"installed runtime is not from a local file: {value}")
    path = Path(urllib.request.url2pathname(urllib.parse.unquote(parsed.path)))
    if os.name == "nt" and re.match(r"^/[A-Za-z]:", str(path)):
        path = Path(str(path)[1:])
    return path.resolve()


def _verify_upgrade_events(
    operation_id: str,
    local_watchers: tuple[tuple[str, str], ...],
) -> None:
    events = RELEASE["_candidate_events"](operation_id)
    quiesced = tuple(sorted({
        (str(event.get("root")), str(event.get("watcher")))
        for event in events
        if event.get("status") == "ok"
        and event.get("upgrade_phase") == "quiesce-requested"
        and isinstance(event.get("root"), str)
        and event.get("root")
        and isinstance(event.get("watcher"), str)
        and event.get("watcher")
    }))
    def normalized(values: tuple[tuple[str, str], ...]) -> set[tuple[str, str]]:
        return {
            ((str(Path(root).resolve()).casefold() if os.name == "nt"
              else str(Path(root).resolve())), watcher)
            for root, watcher in values
        }

    if normalized(quiesced) != normalized(local_watchers):
        raise LocalDeployError(
            "upgrade lifecycle did not cover the exact local watcher set")
    RELEASE["_verify_candidate_events"](events, local_watchers)


def _postcheck(
    repo: Path,
    wheel: Path,
    version: str,
    baseline: tuple[tuple[object, ...], ...],
    all_watchers: tuple[tuple[str, str], ...],
    local_watchers: tuple[tuple[str, str], ...],
    dashboards: tuple[Dashboard, ...],
    operation_id: str | None,
) -> None:
    with ThreadPoolExecutor(max_workers=3) as pool:
        status_future = pool.submit(RELEASE["_installed_all_json"], "status")
        doctor_future = pool.submit(RELEASE["_installed_all_json"], "doctor")
    if RELEASE["_installed_version"]() != version:
        raise LocalDeployError("installed version does not match the wheel")
    if _direct_url() != wheel:
        raise LocalDeployError("installed direct URL does not match the wheel")
    status = status_future.result()
    if RELEASE["_status_contract"](status) != baseline:
        raise LocalDeployError("deployment changed repository agent state")
    if not doctor_future.result().get("ok"):
        raise LocalDeployError("all-repository doctor is unhealthy")
    current_watchers = RELEASE["_started_watchers"]({
        "agents": RELEASE["_status_rows"](status),
    })
    if current_watchers != all_watchers:
        raise LocalDeployError("deployment changed host-wide started watchers")
    if operation_id is not None:
        _verify_upgrade_events(operation_id, local_watchers)
    for dashboard in dashboards:
        _await_api_rows(dashboard.port)


def _write_receipt(
    *, commit: str, version: str, previous_version: str, wheel: Path,
    wheel_sha256: str, operation_id: str | None,
    baseline: tuple[tuple[object, ...], ...],
    watchers: tuple[tuple[str, str], ...],
    dashboards: tuple[Dashboard, ...],
) -> Path:
    destination = _state_directory() / "receipt.json"
    _atomic_json(destination, {
        "schema": LOCAL_DEPLOYMENT_SCHEMA,
        "deployed": True,
        "deployed_at": datetime.now(timezone.utc).isoformat(),
        "commit": commit,
        "version": version,
        "previous_version": previous_version,
        "wheel": str(wheel),
        "wheel_sha256": wheel_sha256,
        "platform": sys.platform,
        "os_name": os.name,
        "architecture": platform.machine(),
        "operation_id": operation_id,
        "contract": [list(row) for row in baseline],
        "watchers": [list(row) for row in watchers],
        "dashboards": [asdict(dashboard) for dashboard in dashboards],
    })
    return destination


def deploy(repo: Path, *, allow_downgrade: bool = False) -> Path:
    commit = _synchronize()
    _branch, target = _bake_configuration()
    version = f"{target}.dev0+g{commit[:8]}"
    previous_version = RELEASE["_installed_version"]()
    if _version_tuple(version) < _version_tuple(previous_version) \
            and not allow_downgrade:
        raise LocalDeployError(
            f"source version {version} is older than installed version "
            f"{previous_version}; pass --allow-downgrade only when intentional")
    root = repo.expanduser().resolve()
    if not root.is_dir():
        raise LocalDeployError(f"repository does not exist: {root}")
    wheel, digest = _prepare_artifact(commit, version)
    _require_unchanged_checkout(commit)
    with ThreadPoolExecutor(max_workers=3) as pool:
        status_future = pool.submit(RELEASE["_installed_all_json"], "status")
        doctor_future = pool.submit(RELEASE["_installed_all_json"], "doctor")
    baseline_status = status_future.result()
    baseline = RELEASE["_status_contract"](baseline_status)
    if not doctor_future.result().get("ok"):
        raise LocalDeployError("all-repository doctor is unhealthy before upgrade")
    all_watchers = RELEASE["_started_watchers"]({
        "agents": RELEASE["_status_rows"](baseline_status),
    })
    environment = _installed_cli().parent.parent
    local_watchers = _logical_watchers(
        watchers_on_host(under=environment))
    if not set(local_watchers) <= set(all_watchers):
        raise LocalDeployError(
            "running local watchers are outside the all-repository baseline")
    dashboards = _running_dashboards()
    stopped: list[Dashboard] = []
    try:
        for dashboard in dashboards:
            _stop_dashboard(dashboard)
            stopped.append(dashboard)
        operation_id = _upgrade(root, wheel, digest)
        _restart_dashboards(tuple(stopped))
        _postcheck(
            root, wheel, version, baseline, all_watchers, local_watchers,
            tuple(stopped), operation_id)
    except BaseException:
        with contextlib.suppress(Exception):
            _restart_dashboards(tuple(stopped))
        raise
    receipt = _write_receipt(
        commit=commit, version=version, previous_version=previous_version,
        wheel=wheel, wheel_sha256=digest, operation_id=operation_id,
        baseline=baseline, watchers=all_watchers, dashboards=tuple(stopped))
    _say(f"deployed {commit[:8]} from {wheel.name}")
    _say(f"receipt: {receipt}")
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo", type=Path, required=True,
        help="Representative live repository used for state verification")
    parser.add_argument(
        "--allow-downgrade", action="store_true",
        help="Allow a local package version below the installed version")
    args = parser.parse_args()
    deploy(args.repo, allow_downgrade=args.allow_downgrade)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("local deployment interrupted", file=sys.stderr)
        raise SystemExit(130)
    except (OSError, LocalDeployError, RELEASE_ERROR,
            subprocess.CalledProcessError) as exc:
        print(f"local deployment failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc