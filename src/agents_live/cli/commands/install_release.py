"""Hidden seam from authenticated GitHub release bytes into a generation."""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import sys
from pathlib import Path

from ... import deploy
from ...runtime.hosts import system as hostruntime
from . import install_generation


def _expose_command_root(root: Path) -> None:
    """Add the public command directory to the user's PATH once."""
    hostruntime.expose_user_path_directory(
        deploy.layout.public_command_root(root))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Download a verified official release and build one generation"))
    parser.add_argument(
        "version", nargs="?",
        help=("Exact stable or prerelease version; omit to select GitHub's "
              "latest stable release"))
    parser.add_argument("--install-root", type=Path)
    parser.add_argument("--activate", action="store_true")
    # The bootstrap authenticated these bytes against the release API and
    # is running this command out of them. Passing the path builds the
    # generation from exactly those bytes instead of a second download
    # that could differ. Suppressed: this is not public grammar.
    parser.add_argument("--wheel", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    root = args.install_root.expanduser().resolve() if args.install_root else None
    try:
        wheel = args.wheel.expanduser().resolve() if args.wheel else None
        if wheel is not None:
            version = deploy.layout.generation_name(args.version or "")
            expected_name = f"agents_live-{version}-py3-none-any.whl"
            if wheel.name != expected_name:
                raise deploy.release_artifact.ReleaseArtifactError(
                    f"expected wheel {expected_name}, got {wheel.name}")
            artifact = deploy.release_artifact.ReleaseArtifact(
                version, wheel.name, "verified-bootstrap",
                hashlib.sha256(wheel.read_bytes()).hexdigest(),
                wheel.stat().st_size)
        else:
            artifact = deploy.release_artifact.resolve(args.version)
        provenance = deploy.generation.Provenance(
            "github-release", artifact.name, artifact.sha256)
        try:
            built = deploy.generation.load(artifact.version, root=root)
        except deploy.generation.GenerationError:
            installed = False
        else:
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
            install_root = root or deploy.layout.installation_root()
            try:
                _expose_command_root(install_root)
            except Exception:
                deploy.generation.clear_activation(root=root)
                deploy.layout.ownership_path(root).unlink(missing_ok=True)
                raise
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
