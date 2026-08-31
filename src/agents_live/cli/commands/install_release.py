"""Hidden seam from authenticated GitHub release bytes into a generation."""
from __future__ import annotations

import argparse
import contextlib
import os
import re
import subprocess
import sys
from pathlib import Path

from ... import deploy
from ...runtime.hosts import system as hostruntime
from ...runtime.spawn import find_uv
from . import install_generation


_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
ENV_WHEEL = "AGENTS_LIVE_BOOTSTRAP_WHEEL"
ENV_WHEEL_SHA256 = "AGENTS_LIVE_BOOTSTRAP_WHEEL_SHA256"
ENV_MIGRATE_UV = "AGENTS_LIVE_BOOTSTRAP_MIGRATE_UV"


def _uv_tool_installed(uv: str) -> bool:
    completed = subprocess.run(
        [uv, "tool", "list"], capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        raise deploy.generation.GenerationError(
            "could not inspect the existing uv tool installation: "
            + (completed.stderr.strip() or completed.stdout.strip()))
    return any(re.match(r"^agents-live\s+v", line)
               for line in completed.stdout.splitlines())


def _retire_uv_tool(uv: str) -> None:
    completed = subprocess.run(
        [uv, "tool", "uninstall", "agents-live"],
        capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        raise deploy.generation.GenerationError(
            "could not retire the uv-managed installation: "
            + (completed.stderr.strip() or completed.stdout.strip()))


def _expose_command_root(root: Path) -> None:
    """Add the stable current command directory to the user's PATH once."""
    hostruntime.expose_user_path_directory(deploy.layout.command_root(root))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Download a verified official release and build one generation"))
    parser.add_argument(
        "version", nargs="?",
        help="Exact stable version; omit to select GitHub's latest stable release")
    parser.add_argument("--install-root", type=Path)
    parser.add_argument("--activate", action="store_true")
    args = parser.parse_args(argv)
    root = args.install_root.expanduser().resolve() if args.install_root else None
    try:
        uv = find_uv()
        migrate_uv = os.environ.get(ENV_MIGRATE_UV) == "1" \
            and _uv_tool_installed(uv)
        _, pointer_state, pointer_detail = deploy.pointer.status(
            deploy.layout.current_path(root))
        if migrate_uv and pointer_state != deploy.pointer.MISSING:
            raise deploy.generation.GenerationError(
                "migration refused because a uv-managed installation and "
                f"{pointer_detail} both exist; retire one owner and retry")
        wheel_value = os.environ.get(ENV_WHEEL, "").strip()
        wheel_sha256 = os.environ.get(ENV_WHEEL_SHA256, "").strip()
        wheel = Path(wheel_value).expanduser().resolve() if wheel_value else None
        if wheel is not None:
            version = deploy.layout.generation_name(args.version or "")
            if _SHA256.fullmatch(wheel_sha256) is None:
                raise deploy.release_artifact.ReleaseArtifactError(
                    "the wheel SHA-256 is invalid")
            expected_name = f"agents_live-{version}-py3-none-any.whl"
            if wheel.name != expected_name:
                raise deploy.release_artifact.ReleaseArtifactError(
                    f"expected wheel {expected_name}, got {wheel.name}")
            artifact = deploy.release_artifact.ReleaseArtifact(
                version, wheel.name, "verified-bootstrap",
                wheel_sha256, wheel.stat().st_size)
            deploy.release_artifact.verify_file(artifact, wheel)
        else:
            artifact = deploy.release_artifact.resolve(args.version)
            if wheel is not None:
                deploy.release_artifact.verify_file(artifact, wheel)
        provenance = deploy.generation.Provenance(
            "github-release", artifact.name, artifact.sha256)
        installed = False
        try:
            built = deploy.generation.load(artifact.version, root=root)
            installed = True
        except deploy.generation.GenerationError:
            target = deploy.layout.generation_dir(artifact.version, root)
            if target.exists() or target.is_symlink():
                install_generation.install_declared_plugins(target)
                built = deploy.generation.adopt(
                    artifact.version,
                    root=root,
                    provenance=provenance,
                    validate=lambda environment: (
                        install_generation.validate_environment(
                            artifact.version, environment)),
                )
                installed = True
        if installed:
            if built.provenance != provenance:
                raise deploy.generation.GenerationError(
                    f"generation {artifact.version} is already installed without "
                    "matching official release provenance and will not be "
                    "overwritten")
            install_generation.validate(built)
            action = "already installed"
        else:
            wheel_context = (
                contextlib.nullcontext(wheel)
                if wheel is not None
                else deploy.release_artifact.verified_download(
                    artifact, root=root))
            with wheel_context as verified_wheel:
                built = install_generation.install(
                    artifact.version,
                    source=verified_wheel,
                    root=root,
                    activate=False,
                    provenance=provenance,
                )
            action = "built"
        if args.activate:
            install_generation.activate_generation(built, root=root)
            deploy.ownership.write_record(deploy.ownership.SELF, root=root)
            if migrate_uv:
                try:
                    _retire_uv_tool(uv)
                except Exception:
                    deploy.generation.clear_activation(root=root)
                    deploy.layout.ownership_path(root).unlink(missing_ok=True)
                    raise
            _expose_command_root(root or deploy.layout.installation_root())
            action = "already installed and activated" if installed \
                else "built and activated"
    except (
        FileNotFoundError,
        OSError,
        ValueError,
        deploy.generation.GenerationError,
        deploy.release_artifact.ReleaseArtifactError,
    ) as exc:
        print(str(exc), file=sys.stderr)
        return 1

    print(
        f"verified {artifact.name} "
        f"(sha256:{artifact.sha256}) from the official GitHub release")
    print(f"{action} generation {built.name}")
    if args.activate:
        print(f"stable command: {deploy.layout.command_path(root=root)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
