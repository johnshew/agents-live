---
title: Generation-Based Deployment Decision
description: Why an installation owns a generation root, a data pointer, and a stable launcher, and what each partial failure recovers to
ms.date: 2026-08-23
ms.topic: concept
---

# Generation-based deployment

## Status

Foundation accepted. The layout, pointer, ownership model, lifecycle planning,
and a generation builder now exist and are tested. The builder stages a new
environment, validates it, promotes it without touching the active generation,
and can activate it with the single pointer write. A hidden
`install-generation` seam composes that API with `uv`, but no public install,
upgrade, or uninstall path calls it. Installation stays uv-managed,
`agents-live upgrade` still runs `uv tool upgrade`, and the Windows
deferred-handoff machinery is untouched.

This record supplies the vocabulary, failure semantics, and ownership rules
that [#369](https://github.com/johnshew/agents-live/issues/369) owes
[#334](https://github.com/johnshew/agents-live/issues/334), and marks step 1
of #334's three-step staging as begun.

## Context

`uv tool upgrade` deletes and rebuilds the tool environment in place. On
Windows the process running the upgrade executes from inside that directory,
and the host refuses to remove a running image, so an upgrade can finish
half-way: packages gone, launcher retained, installation on neither version
([#231](https://github.com/johnshew/agents-live/issues/231)). Watchers and the
dashboard run from the same environment and are the product, so they cannot
simply be forbidden while an upgrade runs.

Everything Windows-only in the current upgrade path - the external handoff, the
deferral loop that polls the process table, the durable claim and reconcile
store, the quiesce and restore handshake, the held-installation refusals -
exists to survive or explain that one in-place rewrite. None of it exists on
POSIX.

Comparable CLIs unpack a new version beside the old one and flip a pointer.
A running process keeps executing the version it started with, and nothing
tries to delete it.

## Decision

An installation owns a root, and a version never moves once it is written.

```text
<installation root>/
    versions/<generation>/   a complete environment, never rewritten
    versions/.staging-<id>/  an incomplete one, safe to delete
    bin/                     the stable launchers, on PATH
    current.json             the pointer naming the active generation
    owner.json               which channel owns this installation
```

The root is `%LOCALAPPDATA%\agents-live` on Windows and
`$XDG_DATA_HOME/agents-live` on POSIX, overridable with
`AGENTS_LIVE_INSTALL_ROOT`. It is machine-local because it holds executables
built for this machine, which must not follow a roaming profile elsewhere.

Four rules make the model work, and each is enforced in code rather than
described here only.

**The pointer is data, not an executable.** Activation writes `current.json`
and nothing else. If flipping the pointer meant rewriting `agents-live.exe`,
Windows would lock that file as soon as anything ran and #231 would return in
a new place.

**A generation name is validated before it becomes a path.** Names are
letters, digits, `.`, `_`, `+`, and `-`. A name that could climb out of
`versions/` is only observable afterwards, as a write outside the root.

**A damaged pointer is refused, never guessed.** Missing, unreadable,
malformed, and unsupported are distinct answers. Recovering by picking the
newest directory in `versions/` would look healthy while running a generation
nobody activated.

**The launcher carries the host's own executable suffix.** Scheduled and
crontab commands pin an absolute path, and a pinned path may not be a `.cmd`
or `.bat` shim - Windows re-parses a batch command line, so a prompt carrying
`&` would run as a command. A launcher that scheduled work cannot pin is not a
launcher. Which native trampoline provides it is still open in #334.

### Generation lifecycle

Nine steps, from #369, planned purely and executed behind narrow adapters:

| # | Step | Touches the active generation |
|---|---|---|
| 1 | `inspect` the installation, its owner, and its channel | no |
| 2 | `resolve` the target version, plugins, and provenance | no |
| 3 | `stage` a complete generation beside the active one | no |
| 4 | `validate` the staged generation with smoke checks | no |
| 5 | `quiesce` only what cannot survive the activation | no |
| 6 | `activate` with one atomic pointer write | yes |
| 7 | `restore` what quiesce stopped | no |
| 8 | `verify` health, rolling the pointer back on failure | no |
| 9 | `collect` generations nothing holds | yes |

Steps 1 through 4 cannot disturb what is running, so they may fail without an
operator noticing. Uninstall is not a step in this lifecycle: it removes owned
artifacts without touching user definitions or unrelated state, and is
designed and tested on its own. Initialization likewise stays separate,
explicit, and idempotent in every channel.

### Failure semantics

| Interrupted state | Recovery |
|---|---|
| `staging` | delete the `.staging-` directory and stage again; the active generation was never touched |
| `staged` | validate again; an unpointed generation is inert |
| `quiesced` | restart what was stopped and report the holders that refused; a partial quiesce is not a partial upgrade, because the pointer has not moved |
| `activated` | verify; a concurrent command sees the new generation, and a running process keeps the old one until it restarts |
| `unverified` | write the previous generation back into the pointer, which is a complete rollback because that generation was never modified |
| `pointer-unreadable` | report the damage and name the repair; guessing a generation is forbidden |
| `pointer-unsupported` | replace the launcher, which is the stale artifact; the pointer is never downgraded to suit it |
| `collecting` | resume; collection removes only generations that are neither active, retained, nor held |

An upgrade is not refusable because a watcher is running. Holders of the
active generation keep executing it and hand off at their next idle version
check ([#188](https://github.com/johnshew/agents-live/issues/188)). Holders of
the *target* directory do block, because staging into a directory that is
executing is the in-place rewrite this design removes.

Retention keeps exactly one superseded generation. It is the rollback, and
rollback is only free while it is on disk. Older generations are collected
once nothing executes from them.

### Installation ownership

Every installation has exactly one upgrade owner, read from evidence rather
than asserted.

| Owner | Evidence | Upgrades by |
|---|---|---|
| `uv` | a `uv-receipt.toml` beside the running image | `uv tool upgrade` |
| `agents-live` | the running image is inside `versions/<generation>/` | activating a new generation |
| `unmanaged` | neither: a checkout, an editable install, a `uvx` run, or a channel that records nothing | whatever installed it |

The running image decides, because that is the artifact an upgrade must
replace; a recorded owner that disagrees is reported as contested rather than
believed. Ownership is recorded beside the pointer so that an installation
that is copied, restored, or deleted takes the answer with it.

Detection uses the filesystem only. Asking uv where its tools live costs a
subprocess that can hang, and the report is needed exactly when a runtime is
being replaced.

A host where a generation layout is active *and* the running command belongs
to another channel is contested: two artifacts answer to `agents-live` on one
PATH and either could replace the runtime. `doctor` fails that check, because
the outcome of leaving it alone is the #231 failure arriving through a
different door.

## Alternatives

**Keep `uv tool`, delete the protocol** (#334 option B). Smaller, contained in
the upgrade command, but it keeps the in-place rewrite and removes the
machinery that detects and explains the failure. It also requires downtime for
every watcher and the dashboard.

**Status quo** (#334 option C). The mechanism works today, but each of the
handoff, quiescence, restore, reconcile, and refusal paths is Windows-only,
hard to test, and has to be kept correct as the runtime changes.

**Self-relocation on first run.** Whether `uv tool install agents-live`
relocates itself into the version root is still open. It is attractive because
it needs no explanation, and dangerous because a later `uv tool upgrade` by
the operator would rewrite a shim whose pointer Agents Live owns. The
detection rule above is the precondition for deciding it, not the decision.
The same installer decision owns an `al` collision opt-out: Python console
entry points are unconditional, so uv can report and refuse an existing
unrelated `al` executable but cannot offer a package-defined `--no-al-alias`
flag. A self-managed installer can offer that choice before it writes stable
launchers.

## Consequences

Landed now: the layout, the pointer format and its refusals, the ownership
classification, the lifecycle plan with its ordering invariant, the retention
and collection rule, a `doctor` check that names the upgrade owner, and the
tested staging, validation, promotion, and activation API. The hidden
`install-generation` command is an integration seam for exact local artifacts
or published versions; it is not the active installer or upgrader.

Deferred, and each is a breaking step that must land on its own:

- upgrade staging a new generation and flipping the pointer, with
  `plugins.py` converging into the version root rather than the uv tool
  environment (#334 step 2);
- deleting `cli/upgrade_handoff.py`, `defer_until_environment_exits`, the
  quiesce and restore protocol, the held-installation refusals, and the
  reconcile hook in `cli/main.py`, plus adding the collector (#334 step 3);
- pointing `cli_executable_path` and the pinned Task Scheduler and crontab
  command paths at the stable launcher, including the WSL liveness pin;
- migrating existing uv-managed installations, which must not break the
  executable paths already written into host artifacts;
- choosing the Windows launcher trampoline, and deciding self-relocation.

We own an installer, a launcher, and a collector once those land: new surface
with its own edge cases. The failure semantics table above is what that
surface is held to.
