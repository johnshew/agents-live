#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.12"
# ///
"""Run development checks and retain input-qualified local evidence."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import runpy
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = 1


def _git(*arguments: str) -> str:
    return subprocess.check_output(
        ["git", *arguments], cwd=ROOT, text=True).strip()


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def _source() -> str:
    names = _git("ls-files", "--cached", "--others", "--exclude-standard", "-z")
    digest = hashlib.sha256()
    for name in sorted(set(names.split("\0")) - {""}):
        path = ROOT / name
        digest.update(name.encode())
        digest.update(b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest()
                      if path.is_file() else b"missing")
    return digest.hexdigest()


def _execution_environment() -> dict[str, str]:
    environment = dict(os.environ)
    activation = environment.pop("VIRTUAL_ENV", None)
    if activation:
        scripts = Path(activation) / ("Scripts" if os.name == "nt" else "bin")
        environment["PATH"] = os.pathsep.join(
            entry for entry in environment.get("PATH", "").split(os.pathsep)
            if Path(entry) != scripts)
    return environment


def _environment() -> dict:
    probe = (
        "import importlib.metadata,json,platform,sys; "
        "print(json.dumps({'python':sys.version,'executable':sys.executable,"
        "'platform':platform.platform(),'packages':sorted("
        "(item.metadata['Name'],item.version) for item in "
        "importlib.metadata.distributions())}))"
    )
    environment = json.loads(subprocess.check_output(
        ["uv", "run", "--with-editable", ".", "python", "-c", probe],
        cwd=ROOT, text=True, env=_execution_environment()))
    environment["executable"] = Path(environment["executable"]).name
    environment["uv"] = subprocess.check_output(["uv", "--version"], text=True).strip()
    environment["runner_python"] = sys.version
    environment["architecture"] = platform.machine()
    variables = {key: value.replace(str(ROOT), "{repository}")
                 for key, value in _execution_environment().items()
                 if key.upper() not in {"PWD", "OLDPWD"}}
    environment["variables_sha256"] = _digest(variables)
    return environment


def _commands(profile: str, tests: list[str]) -> list[list[str]]:
    if profile == "focused":
        if not tests:
            raise ValueError("focused requires at least one unittest selector")
        return [["uv", "run", "--with-editable", ".", "python", "-m",
                 "unittest", *tests, "-v"]]
    if tests:
        raise ValueError("test selectors are only accepted with focused")
    if profile == "release":
        return [["uv", "run", "--script", "tools/release.py", "--gates"]]
    release = runpy.run_path(str(ROOT / "tools" / "release.py"))
    commands = release["_gate_commands"]()
    source_commands = [
        command for command in commands
        if "--build-artifacts" not in command
        and "tools/dashboard-readiness.py" not in command
    ]
    return [["uv", "run", "ruff", "check", "--select", "F811", "src", "tools", "tests"],
            *source_commands]


def _receipt_directory() -> Path:
    common = Path(_git("rev-parse", "--git-common-dir"))
    return (ROOT / common).resolve() / "validation"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("profile", choices=("focused", "pr", "release"))
    parser.add_argument("tests", nargs="*")
    parser.add_argument("--reuse", action="store_true")
    parser.add_argument("--plan", action="store_true")
    args = parser.parse_args(argv)
    try:
        commands = _commands(args.profile, args.tests)
    except ValueError as exc:
        parser.error(str(exc))
    if args.reuse and args.profile == "release":
        parser.error("release gates are not reusable development evidence")
    if args.plan:
        print(json.dumps({"profile": args.profile, "commands": commands}, indent=2))
        return 0

    environment = _environment()
    identity = {"schema": SCHEMA, "profile": args.profile,
                "commit": _git("rev-parse", "HEAD"), "source_sha256": _source(),
                "environment": environment,
                "commands": [["{repository}" if argument == str(ROOT) else argument
                              for argument in command] for command in commands]}
    receipt = _receipt_directory() / f"{_digest(identity)}.json"
    if args.reuse and receipt.is_file():
        try:
            previous = json.loads(receipt.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            previous = {}
        if (previous.get("identity") == identity and previous.get("exit_code") == 0
                and previous.get("source_unchanged") is True
                and len(previous.get("results", [])) == len(commands)
                and all(item.get("exit_code") == 0 for item in previous["results"])):
            print(f"Reused {args.profile} evidence: {receipt}")
            return 0

    results = []
    exit_code = 0
    for command in commands:
        print("+ " + subprocess.list2cmdline(command), flush=True)
        started = time.monotonic()
        try:
            exit_code = subprocess.run(
                command, cwd=ROOT,
                env={**_execution_environment(), "AGENTS_LIVE_REPO": str(ROOT)}, check=False).returncode
        except OSError as exc:
            print(f"Cannot execute {command[0]}: {exc}", file=sys.stderr)
            exit_code = 127
        results.append({"command": command, "exit_code": exit_code,
                        "duration_seconds": round(time.monotonic() - started, 3)})
        if exit_code:
            break
    unchanged = identity["source_sha256"] == _source()
    if not unchanged:
        print("Source changed during validation; evidence is not reusable.", file=sys.stderr)
        exit_code = exit_code or 2
    artifacts = {}
    if args.profile == "release":
        artifacts = {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                     for path in (ROOT / "dist").glob("*.whl")}
    receipt.parent.mkdir(parents=True, exist_ok=True)
    document = {"identity": identity, "results": results, "exit_code": exit_code,
                "source_unchanged": unchanged, "wheel_sha256": artifacts,
                "recorded_at": datetime.now(timezone.utc).isoformat()}
    temporary = receipt.with_suffix(f".{os.getpid()}.tmp")
    temporary.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    temporary.replace(receipt)
    print(f"Validation evidence: {receipt}")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())