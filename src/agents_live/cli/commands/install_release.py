"""Hidden seam from authenticated GitHub release bytes into a generation."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from ... import deploy
from . import install_generation


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Download a verified official release and build one generation"))
    parser.add_argument(
        "version", nargs="?",
        help="Exact stable version; omit to select GitHub's latest stable release")
    parser.add_argument("--install-root", type=Path)
    parser.add_argument("--activate", action="store_true")
    args = parser.parse_args()
    root = args.install_root.expanduser().resolve() if args.install_root else None
    try:
        artifact = deploy.release_artifact.resolve(args.version)
        provenance = deploy.generation.Provenance(
            "github-release", artifact.name, artifact.sha256)
        installed = False
        try:
            built = deploy.generation.load(artifact.version, root=root)
            installed = True
        except deploy.generation.GenerationError:
            target = deploy.layout.generation_dir(artifact.version, root)
            if target.exists() or target.is_symlink():
                raise
        if installed:
            if built.provenance != provenance:
                raise deploy.generation.GenerationError(
                    f"generation {artifact.version} is already installed without "
                    "matching official release provenance and will not be "
                    "overwritten")
            action = "already installed"
            if args.activate:
                deploy.generation.activate(built, root=root)
                action = "already installed and activated"
        else:
            with deploy.release_artifact.verified_download(
                    artifact, root=root) as wheel:
                built = install_generation.install(
                    artifact.version,
                    source=wheel,
                    root=root,
                    activate=args.activate,
                    provenance=provenance,
                )
            action = "built and activated" if args.activate else "built"
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
    print(f"generation executable: {install_generation.executable(built)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
