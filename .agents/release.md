---
title: Releasing Agents Live
description: Required checks and commands for publishing agents-live releases
---

Checklist for cutting a release from this repository, the definitive
source since 2026-07-18.
Use [testing.md](testing.md) to validate source, target-version artifacts, and
the installed PyPI tool as separate execution modes.

## Changelog readiness

Invoke `/changelog-maintenance` before previewing a release. It compares every
commit since the latest tag with `Unreleased`, adds missing user-visible notes,
completes issue hygiene, and recommends the minimum semantic version bump.
Commit any resulting changelog update before continuing because preparation
requires a clean tree.

## Recent issue gate

Before the release preview, fetch open GitHub issues and review every issue
created or updated since the latest release tag. Also review older open issues
that match the code paths, platforms, or live-host operations changed or used
during release validation. Do not infer release readiness from commit history
alone.

```bash
git log -1 --format=%cs "$(git describe --tags --abbrev=0)"

gh issue list --state open --search "updated:>=<release-date>" --limit 100 \
	--json number,title,createdAt,updatedAt,labels
```

GitHub issue date qualifiers are UTC. Do not add a local-calendar upper bound:
an issue created late today locally may already carry tomorrow's UTC date.

For each relevant issue, choose one outcome:

- Fix an obvious, bounded defect on the release branch, add executing coverage,
  and rerun the affected gates.
- Present the issue number, release impact, workaround, and deferral rationale
  to the developer and receive explicit approval to release without the fix.

An issue that describes the exact failure or workaround encountered during
release validation is relevant even when it is labeled as an enhancement. Do
not preview, prepare, or publish while a relevant recent issue has neither been
fixed nor explicitly accepted for deferral.

## Versioning

Semantic versioning; the version lives in `pyproject.toml`.

| Change | Bump |
|---|---|
| Breaking CLI, configuration, or frontmatter contract | Major |
| New commands, adapters, or compatible features | Minor |
| Fixes and documentation | Patch |

## Gates (all must pass)

```bash
uv run --script tools/pre-release-audit.py
uv run --with-editable . python -m unittest discover -s tests -v
uv run --with-editable . agents-live smoketest
uv build
```

After the build, run the built-wheel dashboard readiness check described in
[testing.md](testing.md), including `--dev`, `/api/agents`, and started/stopped
action assertions. `dashboard --help` is not a release gate by itself.
The dashboard itself is a foreground server. Readiness means the API and rows
answer, not that its command exits; leave process startup/cleanup to
`tools/dashboard-readiness.py` or candidate acceptance.

The audit must report no personal information, secrets, or nonportable
paths, and its adapter-resolution and doc-link checks must pass.
The framework smoketest must pass end to end: it exercises the real
trigger/run/status loop in this checkout, catching integration breaks
the unit suite cannot. It uses whichever agent CLI this host can launch,
preferring `copilot`, so the gate does not require a particular vendor's
CLI to be installed. `tools/release.py` runs all of these gates
itself during `--prepare` and `--publish`.

`uv build` resolves its build backend from PyPI, so on a network that
intercepts TLS it fails with `HandshakeFailure` while every other gate
passes. That is a local condition, not a release defect: the published
artifacts are built by `.github/workflows/publish.yml` on a GitHub
runner, which reaches PyPI normally. `uv build --offline` succeeds from
the local cache and is enough to confirm the package still builds, but
`release.py` deliberately offers no offline mode, because a release
cannot be cut from a host that cannot reach the index it publishes to.
Either run the release from a host with direct access, or dispatch the
publish workflow against the tag.
For machine-specific names that generic patterns cannot detect, create the
gitignored `.agents-live-machine-names` file at the repository root. Put one
literal machine name on each line; blank lines and lines beginning with `#`
are ignored. The names remain local, while the audit reports every
case-insensitive match in shipped text with its file and line number.
Inspect the wheel and sdist: `agents-live --help` reports the
documented commands, `agents-live init` installs the vendored skill,
and no private adapter or deployment-specific agent is present.

## Publish

Preview the selected release without changing files or remotes:

```bash
uv run --script tools/release.py --dry-run --bump patch
```

Prepare the release locally:

```bash
uv run --script tools/release.py --prepare --bump patch --yes
```

Replace `patch` with the bump recommended by changelog maintenance. The script
rejects an empty `Unreleased` section and any bump below the minimum implied by
`feat:`, conventional `type!:` or `BREAKING CHANGE:` notes. Every changelog
bullet must start with a standalone one-line summary; supporting detail belongs
on indented continuation lines. The script requires a clean `main`
synchronized with `origin/main`, creates an isolated
`release/v<version>-candidate` branch, updates all package, skill,
documentation-link, and changelog versions, runs every release gate, and
creates the release commit, annotated tag, and preparation receipt locally.
The receipt binds the exact gate list, commit, base commit, tag object, wheel,
source distribution, and artifact hashes. Preparation copies the wheel and
source distribution into Git-local immutable release storage and all later
bootstrap, acceptance, and publication steps use those copies. `dist/` may be
rebuilt for diagnostics without changing the candidate identity. Inspect the
receipt-bound artifacts and review the commit.

## Candidate acceptance

The local release commit, annotated tag, and artifacts are not public yet.
Install that exact wheel into the user-level tool through the supported local
artifact upgrade path, restore a healthy representative repository with at
least one started watcher, then run the mandatory acceptance command:

```bash
agents-live upgrade --from <receipt-bound-wheel-path-printed-by-prepare>
uv run --script tools/release.py --accept-candidate \
  --repo <live-repository> --agent <safe-agent-identifier> \
  --cost-agent <safe-provider-agent-identifier> --yes
```

The first command bootstraps the candidate. The acceptance command then makes
the installed candidate upgrade itself from the same wheel. It captures every
registered repository's started and loadable state, requires a started watcher
in the selected representative repository, waits for any deferred Windows
helper without repeatedly launching the held executable, and requires:

- the same exact candidate version after replacement;
- unchanged started and loadable state across all registered repositories;
- healthy all-repository `doctor` results before and after;
- restoration of every started watcher; and
- correlated quiesce, plugin convergence, restoration, and terminal events on
  deferred Windows upgrades.

Acceptance preflights the selected agents, absence of a managed dashboard, and
a real headless browser launch before replacement. Every watcher counted in the
baseline must also have a resident `watch-loop` process; started intent alone is
not an acceptance baseline. If preflight names an intent-only watcher, cycle
that one agent through public `stop`/`start` and rerun preflight before any
replacement. It writes an
`upgrade-complete` checkpoint after replacement, plugin convergence, watcher
restoration, all-repository state comparison, and doctor health succeed. If a
later operational phase fails and cleanup restores that exact baseline, resume
without repeating replacement:

```bash
uv run --script tools/release.py --accept-candidate \
  --repo <live-repository> --agent <safe-agent-identifier> \
  --cost-agent <safe-provider-agent-identifier> --resume --yes
```

After replacement succeeds, acceptance runs a full operational pass through
the uv-managed candidate. Choose an agent whose immediate run is safe and
whose started state may be toggled temporarily. The pass exercises CLI
`status`, `doctor`, `run`, `start`, `stop`, and log queries. It then launches
the installed dashboard, drives its real browser UI, runs the dashboard health
check, and clicks Run, Start, and Stop on that agent. Dashboard action records,
healthy header state, and every state transition must be observed. The exact
all-repository baseline is checked again afterward, and cleanup restores the
agent's initial started state even when a probe fails.

Choose a distinct second safe provider-backed agent for `--cost-agent`.
Acceptance runs
it after the candidate is installed and requires a new successful log record
with a positive normalized `list_cost_usd` usage value. This proves the real
provider plugin, output parser, observability schema, and dashboard cost input
agree; a fake or handler-only agent cannot satisfy this check. This paid probe
runs last, after CLI and browser lifecycle checks.

Success writes an untracked receipt under the repository's Git metadata. The
receipt binds acceptance to the release commit, annotated tag, and wheel
SHA-256. `--publish` checks both preparation and acceptance receipts and refuses
a missing or stale receipt. Never use a source-only or isolated `uvx` check as
a substitute; those do not exercise replacement of the installed consumer tool.

Publish the prepared commit and tag:

```bash
uv run --script tools/release.py --publish --yes
```

Publication validates the two receipts instead of rerunning identical local
gates. It requires the tagged release commit to be exactly one commit ahead of
`origin/main`, atomically pushes candidate `HEAD` to `main` with the tag, and
creates the GitHub release. The release body starts with
one first-line summary per changelog entry and a link to the full changelog at
the release tag, followed by GitHub's generated notes (merged pull requests and
the compare link).

Publishing the GitHub release triggers `.github/workflows/publish.yml`,
which resolves the release tag to one commit, runs the Test workflow against
that exact commit on Ubuntu and Windows, then verifies and publishes the exact
wheel and source distribution that passed installed-candidate acceptance. The
release tool attaches those artifacts and their `SHA256SUMS` manifest while the
GitHub release is still a draft; the publish job downloads them only after both
test jobs pass and refuses any checksum mismatch before PyPI. Independent
Windows and Linux builds are not expected to be byte-identical because archive
line endings, executable modes, and build-backend metadata differ. Publication cannot start
unless both test jobs pass. Wait
for the workflow to succeed, verify both artifacts are attached, then follow
the two-stage PyPI and installed-tool checks in [testing.md](testing.md). In
an interactive terminal, `gh run watch <run-id> --exit-status` can wait for
the workflow. Automation should use noninteractive run-status APIs or
`GH_PAGER=cat gh run view <run-id>` after completion; `gh run watch` may take
over the terminal's alternate screen.

GitHub also records a SHA-256 digest and byte size for each uploaded asset in
its release API. The generation bootstrap accepts only the uniquely named wheel
from the official repository and fails closed when that metadata is absent,
invalid, or does not match the downloaded bytes. This is separate from the
manifest check above: the manifest protects publication, while the release API
metadata is the provenance contract consumed by the installer.

PyPI's versioned JSON endpoint can expose a release before the Simple API used
by package resolvers. A successful workflow and HTTP 200 from the versioned
JSON endpoint confirm publication. Exact-version `uvx` resolution separately
confirms consumer availability. If JSON succeeds while `uvx` reports that the
version does not exist, allow the Simple API to propagate and retry the exact
check. Do not republish or alter the tag.

If a failure or interruption occurs before the release commit, the script
restores every version file and clears its staged changes. A failure after the
commit remains visible for recovery. Rerun `--publish --yes` if GitHub release
creation fails after the atomic push; publication accepts the exact tagged
commit locally or on `origin/main` and skips a release that already exists. Do
not rewrite or delete a pushed release tag.
