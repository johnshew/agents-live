---
title: Generation-Based Deployment Decision
description: Why installation writes immutable version directories and selects one with a current directory link
ms.date: 2026-09-04
ms.topic: concept
---

# Generation-based deployment

## Status

Implemented for self-managed installation. Public Windows and POSIX bootstrap
scripts authenticate an official wheel and hand it to that wheel's own
`install-release` command, which builds the versioned directory, validates it,
seals it, and activates it. The initialized version exposes one stable command
directory on PATH.

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
    current/                  link to the active version directory
    owner.json                installation owner
```

The directory key is the package's complete PEP 440 version, not its release
base. Stable releases therefore use names such as `6.6.1`, while local bake
artifacts use names such as `6.6.1.dev0+gabc1234`. Multiple bakes of the same
release line coexist without collision and remain independently selectable.

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

The public bootstrap is deliberately limited to transport. It authenticates
bytes and hands them to the package; it does not know the installation layout.

1. Resolve an exact stable GitHub release, or the latest stable release.
2. Verify the wheel's recorded size and SHA-256 digest before executing it.
3. Run that wheel's own `agents-live install-release` through an ephemeral
   `uv tool run --isolated --from <wheel>` environment, passing the verified
   wheel path.
4. After it succeeds, retire any uv-managed installation.

The bootstrap does not create `versions/`, does not name a staging directory,
and does not promote anything. Every generation on every host is therefore
built by one code path, whether it comes from bootstrap, upgrade, or a local
bake. Before 6.8 the shell scripts laid out the generation themselves and the
package sealed what they had built, which put the layout, the staging
convention, and the promotion step into three places in two languages.

Step 3 is safe because `uv venv --python <interpreter>` resolves to that
interpreter's *base* installation, not to the ephemeral environment invoking
it. A generation built from the bootstrap environment therefore survives that
environment's disposal.

The verified wheel path crosses this private boundary as a suppressed
`--wheel` flag on a hidden command. It is not part of the public command
grammar, does not appear in generated help, completions, or the EBNF surface,
and is passed rather than re-derived so that the generation is built from the
exact bytes the bootstrap authenticated instead of a second download that
could differ.

Plugins are not installed into a generation. They are declared by registered
repositories and loaded dynamically at runtime, so a generation never depends
on mutating its environment later, and a generation installed before a plugin
was declared still loads it. See
[plugin-loading.md](plugin-loading.md).

### Completeness is a record, not a name

A generation directory is built in place at `versions/<version>/`. It is
complete when it contains a valid `generation.json`, which is written last and
written atomically.

The alternative, used before 6.8, was to populate `versions/.staging-<version>/`
and rename it into place on success. Both designs solve the same problem: a
directory must never be mistaken for a usable generation while it is still
being filled. Interrupt an in-place build with no completeness marker and the
half-populated directory is reported as installed, can be selected, and,
because immutability refuses to rewrite an installed generation, permanently
blocks its own reinstallation.

The record was chosen over the name prefix because it was already required.
`load` already refuses a generation without a valid record, so the prefix was a
second mechanism for a fact the design already recorded. Keying every
completeness test on the record removes the prefix, the staging path helper,
the staging discard step, and the promotion rename.

The cost is that the directory no longer appears atomically; the invariant
becomes that a directory is never trusted, only a record. The gain is that the
promotion rename disappears, and with it the Windows retry loop that existed
because antivirus and search indexers hold a freshly written directory open
long enough to fail the rename.

Repeating bootstrap for the same version is idempotent. A complete sealed
version is reused after validation. A directory without a valid record is
discarded and rebuilt.

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

The installation behaves as a local version store. Installing a new version
does not remove the others, and selecting an already installed version does not
rewrite it. Selection moves `current`, then invokes automatic host maintenance
through the newly selected generation's own command. That generation converges
native triggers and still-started watchers to its implementation. New
dispatches resolve through `current`; an in-flight dispatch, watcher, or
dashboard may finish from the immutable generation where it began.

The hidden installation commands implement this installation and switching
protocol for bootstrap, upgrade, and bake acceptance. They are not a public
version-manager command surface. A future public selector can use the same
protocol without adding a second active-version record.

### Failure semantics

| Interrupted state | Recovery |
|---|---|
| downloading | discard the temporary download and authenticate again |
| populating | the directory has no valid record, so install again and it is discarded and rebuilt |
| installed, not active | validate it and switch `current` |
| missing or invalid `current` | report the damage and recreate the intended link; never guess |
| activation failure | retain or restore the previous `current` target |
| selected, convergence failed | keep the selected generation visible, report degraded host state, and rerun convergence after correcting the cause |

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

Local bake deployment derives a commit-bearing PEP 440 version and uses the same
generation builder and selection protocol as a release. Repeated bakes on one
release line therefore accumulate testable generations instead of overwriting a
shared environment.

The project owns installation, activation, migration, retention, and cleanup.
It does not own a custom launcher or a duplicate active-version manifest. The
clean-root readiness gate runs bootstrap twice on Windows and Linux with package
index resolution for Agents Live disabled, then verifies authenticated bytes,
the exact version, ownership, idempotency, and the resolved `current` target.