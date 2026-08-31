---
title: Generation-Based Deployment Decision
description: Why installation writes immutable version directories and selects one with a current directory link
ms.date: 2026-08-30
ms.topic: concept
---

# Generation-based deployment

## Status

Implemented for self-managed installation. Public Windows and POSIX bootstrap
scripts authenticate an official wheel, install it into its final versioned
directory, and invoke that version's absolute `agents-live` command to finish
initialization. The initialized version activates itself and exposes one stable
command directory on PATH.

This decision completes the installer architecture required by
[#395](https://github.com/johnshew/agents-live/issues/395) and supplies the
deployment model used by [#334](https://github.com/johnshew/agents-live/issues/334).

## Context

`uv tool upgrade` rewrites its environment in place. On Windows, an Agents Live
process can be executing from that environment while upgrading it. Windows may
then retain a running executable while other package files have already been
removed, leaving an installation on neither version.

A complete new version can instead be installed beside the running version.
Existing processes continue from the old directory, while later commands enter
the new one through a stable directory path.

## Decision

An installation owns this layout:

```text
<installation root>/
    versions/<version>/       complete immutable environment
    versions/.staging-<id>/  incomplete environment
    current/                  link to the active version directory
    owner.json                installation owner
```

The default root is `%LOCALAPPDATA%\agents-live` on Windows and
`$XDG_DATA_HOME/agents-live` on POSIX. `AGENTS_LIVE_INSTALL_ROOT` overrides it.
The stable PATH entry is `current\Scripts` on Windows and `current/bin` on
POSIX.

`current` is a local directory junction on Windows and a relative symbolic link
on POSIX. It is the only active-generation fact. There is no second pointer
file to disagree with it, and Agents Live does not guess the newest installed
version when the link is missing or invalid.

Agents Live uses the console entry points generated inside each Python
environment. It does not build or publish a separate native launcher. Changing
versions changes the target of `current`, not the PATH entry or a running
generation's files.

### Installation flow

The public bootstrap is deliberately limited to transport and placement:

1. Resolve an exact stable GitHub release, or the latest stable release.
2. Verify the wheel's recorded size and SHA-256 digest before executing it.
3. Create `versions/<version>` and install the wheel into that dedicated
   environment.
4. Invoke that directory's absolute `agents-live install-release` command.

The final dedicated version then validates and seals itself, performs migration
and cleanup, switches `current` to itself, records installation ownership, and
ensures the stable command directory is on PATH. It refuses to initialize a
different version directory. Bootstrap inputs cross this private boundary in
environment variables; the public command grammar does not gain artifact
transport flags.

Repeating bootstrap for the same version is idempotent. A complete sealed
version can be reused after validation. An incomplete staging directory is
removed and recreated.

### Activation

On POSIX, activation creates a temporary relative symbolic link and replaces
`current` with `os.replace`. On Windows, activation replaces the local directory
junction and restores the previous target if creating the new junction fails.
Windows does not provide the same atomic replacement primitive for directory
junctions, so interruption can temporarily leave `current` absent; both version
directories remain complete and repair consists only of recreating the link.

Activation validates the target before changing `current`. A target outside the
installation's `versions` directory is invalid and is never followed as an
active generation.

### Failure semantics

| Interrupted state | Recovery |
|---|---|
| downloading | discard the temporary download and authenticate again |
| staging | delete the `.staging-` directory and install again |
| installed, not initialized | invoke that version's absolute command again |
| initialized, not active | validate it and switch `current` |
| missing or invalid `current` | report the damage and recreate the intended link; never guess |
| activation failure | retain or restore the previous `current` target |

A running process keeps using the complete version from which it started.
Collection may remove an old version only after it is neither active, retained,
nor held by a process.

### Ownership

The running image remains the primary ownership evidence:

| Owner | Evidence | Upgrade mechanism |
|---|---|---|
| `agents-live` | running image is inside `versions/<version>` | install a new version and switch `current` |
| `uv` | `uv-receipt.toml` is beside the running image | migrate through supported bootstrap, or use uv |
| `unmanaged` | neither condition applies | original installation channel |

Initialization records self-managed ownership only after the dedicated version
has validated and activated. A conflicting running channel and installation
root are reported as contested rather than silently overwritten.

## Consequences

PATH is configured once. Scheduled commands and interactive shells can use the
stable generated command under `current`, while running watchers and dashboards
finish on their original immutable version. Release assets consist of the wheel,
source distribution, Windows and POSIX bootstrap scripts, and checksum manifest.

The project owns installation, activation, migration, retention, and cleanup.
It does not own a custom launcher or a duplicate active-version manifest. The
clean-root readiness gate runs bootstrap twice on Windows and Linux with package
index resolution for Agents Live disabled, then verifies authenticated bytes,
the exact version, ownership, idempotency, and the resolved `current` target.