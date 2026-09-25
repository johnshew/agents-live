---
title: Releasing Agents Live
description: Required checks and commands for publishing agents-live releases
---

Checklist for cutting a release from this repository, the definitive
source since 2026-07-18.
Use [testing.md](testing.md) to validate source, target-version artifacts, and
the installed PyPI tool as separate execution modes.

Before release review, generate a local cycle report using the policy and
command in [release-report.md](release-report.md). The report shows all cycles,
open work, selected RCs, retained attempts, approval and validation gaps, and
publication evidence. It is
gitignored and summarizes evidence without replacing any gate below.

## Numbered RC lifecycle

The process and business requirements are canonical in
[development-release-process.md](../docs/development-release-process.md).
The manifest selects RC5 source for `6.9.2` finalization
while `main` advances toward `6.9.3rc1`. Historical RC1-RC6 artifacts and their
different readiness states remain retained; none is silently relabeled accepted.

The numbered lifecycle is implemented by `tools/release.py`. Existing
`local-deploy.py --rc` prepares or reuses the canonical numbered attempt, then
activates its exact wheel. Historical local evaluation receipts support recovery,
not full operational acceptance. Never rebuild those consumed identities or
import their limited readiness evidence as full acceptance. The current release
still requires the issue dispositions, exact RC acceptance, publication approval,
and independent stable acceptance described by the generated report.

### Numbered attempt commands

Run preflight with the live repository and safe agent arguments before allocating
an attempt. From the clean, synchronized configured cycle branch, prepare the next RC:

```bash
uv run --script tools/release.py --prepare-rc <configured-next-rc> --yes
```

Allocation consumes an identity across manifest history, local evidence and refs,
and origin refs. It creates a dedicated `release/v<attempt>-candidate` worktree
and retains evidence under the common Git directory's
`agents-live-release/cycle-<target>/<attempt>/`. Preparation commits release
metadata, preserves `Unreleased` for RCs, builds once, and runs the source and
packaged gates. No tag is created. Run subsequent commands from the printed
retained worktree, using its script, not a different checkout's version.

```bash
agents-live upgrade --from <retained-wheel> --candidate
uv run --script tools/release.py --accept-candidate --attempt <rc> \
  --repo <live-repository> --agent <safe-agent-identifier> \
  --cost-agent <safe-provider-agent-identifier> --yes
```

After exact RC acceptance, obtain developer approval naming its original source
commit and record it in the cycle's approval fields. Do not include later runtime
changes. From the clean synchronized cycle branch:

```bash
uv run --script tools/release.py --prepare-final --from-rc <accepted-rc> --yes
```

The final attempt has package version `<target>` but a separate identity such
as `<target>-final-1`. Its preparation stamps the accumulated stable changelog,
builds new stable bytes, and reruns the required gates. Bootstrap and independently
accept those bytes using the same command above with the final attempt identifier.
RC acceptance can never substitute for this receipt. After explicit approval:

```bash
uv run --script tools/release.py --finalize --attempt <final-attempt> --yes
uv run --script tools/release.py --publish --attempt <final-attempt> --yes
```

Finalization creates the annotated stable tag and an immutable record binding it
to final preparation and acceptance. Publication pushes the exact commit and tag
atomically, uploads retained bytes and a privacy-safe evidence digest, and refuses
replacement of existing draft assets. Both automatic and manual PyPI workflow
paths require a canonical stable tag and matching public evidence. Publication
never rebuilds accepted assets. Verify GitHub and PyPI availability independently.

### Retry and rejection

Use `--prepare-attempt <attempt> --yes` in its retained worktree to retry unchanged
preparation. Completed builds are reused byte-for-byte; changed or unreceipted
bytes are refused. An interruption before the commit/build record is complete
may require a new identity after preserving the failed attempt for inspection.
Use `--accept-candidate --attempt <attempt> --resume` with all live arguments
and `--yes` only when a matching upgrade checkpoint exists. Cycle mutation and
allocation locks refuse concurrent operations; after a crash, verify no operation
is active before removing only the stale lock directory. Never delete receipts.

If a committed dashboard validator defect blocked an already recorded build,
run the following from clean, reviewed tooling that contains the correction:

```bash
uv run --script tools/release.py --requalify-attempt <attempt> --yes
uv run --script tools/local-deploy.py --repo <live-repository> --rc <rc> --requalified
```

Requalification retains a byte-exact copy of the committed validator, records its
commit and hash, reruns all gates in the exact candidate checkout and reuses the
four recorded build artifacts without rebuilding. The preparation receipt names
the actual validator command. A missing build or changed artifact is refused;
an existing preparation is validated, not replaced. Deployment requires validator
ancestry and permits only the named release tools, release guide, changelog,
tests and design documentation to differ from the retained source. It does not
bypass provider health. Use the corrected tooling for subsequent explicit
`--attempt` acceptance, finalization and publication; it selects and verifies
the retained checkout before acting. Operational acceptance remains separate.

To reject, deliberately restore a retained version through `versions activate`,
verify all-repository doctor, agent state, and representative watchers, then run
`--reject-attempt <attempt> --reason <reason> --yes`. The tool verifies the retained
state baseline when acceptance has started and records rejection without deleting
artifacts. Advance the configured RC for source fixes. A failed final attempt
does not consume a stable patch: after restoration, remove only the inactive
failed installed version through `versions remove`, then allocate a new final
attempt. The installed wheel digest must match; sealed versions are never
overwritten. Runtime removal does not remove the Git-local evidence store.

The existing rejected local stable tag needs explicit migration before finalization:
`--migrate-legacy-tag v<target> --yes`. This verifies the original receipt and
artifacts, preserves the exact annotated object under an archive ref, and removes
only the matching unpublished local canonical ref. It refuses remote tags and
conflicting identities. It does not install or publish anything.

`--cycle-status` reports local attempt evidence without exposing receipt paths.
Legacy preview/prepare and implicit acceptance/publication remain blocked in
numbered cycles; the later legacy examples apply only to non-numbered cycles.
Do not use an older checkout to bypass the guard.

The adopted policy keeps a stable target while advancing RC numbers on
rejection. Keep immutable artifacts and receipts for every attempt. An
accepted RC permits preparation of final stable bytes, not their publication.
Final stable artifacts must pass independent required acceptance. Create the
stable tag only afterward and publish exactly those accepted bytes, without
rebuilding them. A failed final build needs a distinct retained attempt identity
and safe installed-version recovery, not another stable patch number.

Stage RCs beside the existing stable installation and select one deliberately
for testing. Verify rollback and watcher restoration without duplicate native
automation. Optional GitHub RC releases must be prereleases and never latest;
normal upgrades and the PyPI publishing workflow remain stable-only, including
manual workflow dispatch. Existing immutable tags and evidence are not rewritten.

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

The commands below describe the gates, not a sequence to run before
`--prepare`. Preparation owns their execution and records the results. Do not
manually repeat a passing gate for unchanged inputs, or run the entire list
and then immediately ask preparation to run it again. Use focused checks while
developing; use receipt-bound preparation once the release version is stamped.
Publication consumes those receipts instead of rerunning local tests.

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
CLI to be installed. `tools/release.py` runs these gates during `--prepare`;
`--publish` verifies the preparation and installed-acceptance receipts.

`uv build` resolves its build backend from PyPI, so on a network that
intercepts TLS it fails with `HandshakeFailure` while every other gate
passes. That is a local condition, not permission to replace accepted bytes.
Use an environment that reaches the approved package source for preparation.
An offline diagnostic build is not a preparation receipt. The publish workflow
downloads and verifies the accepted release assets rather than rebuilding them;
manual dispatch cannot bypass missing finalization or artifact evidence.
For machine-specific names that generic patterns cannot detect, create the
gitignored `.agents-live-machine-names` file at the repository root. Put one
literal machine name on each line; blank lines and lines beginning with `#`
are ignored. The names remain local, while the audit reports every
case-insensitive match in shipped text with its file and line number.
Inspect the wheel, sdist, `install.ps1`, and `install.sh`. The `SHA256SUMS`
manifest and GitHub release asset
metadata cover every file consumed by bootstrap. `agents-live --help` reports the
documented commands, `agents-live init` installs the vendored skill,
and no private adapter or deployment-specific agent is present.

## Publish

Preview the selected release without changing files or remotes:

```bash
uv run --script tools/release.py --dry-run --bump patch
```

Before preparation, reject live-host acceptance blockers without building or
changing the checkout:

```bash
uv run --script tools/release.py --candidate-preflight \
  --repo <live-repository> --agent <safe-agent-identifier> \
  --cost-agent <safe-provider-agent-identifier>
```

This runs the same selected-agent, browser, managed-dashboard, all-repository
doctor, ownership, watcher-residency, and watched-path checks used by candidate
acceptance. It does not replace installed candidate acceptance; it moves its
read-only prerequisites ahead of the expensive preparation gates.

Prepare the release locally:

```bash
uv run --script tools/release.py --prepare-rc <next-numbered-rc> --yes
```

Configure the stable target and next RC after changelog review. The script
rejects an empty `Unreleased` section and any bump below the minimum implied by
`feat:`, conventional `type!:` or `BREAKING CHANGE:` notes. Every changelog
bullet must start with a standalone one-line summary; supporting detail belongs
on indented continuation lines. The script requires a clean `main`
synchronized with `origin/main`, creates an isolated
`release/v<version>-candidate` branch, updates all package, skill,
documentation-link, and changelog versions, runs every release gate, and
creates the candidate commit and preparation receipt locally. RCs are not tagged.
The receipt binds the exact gate list, commit, base commit, tag object, wheel,
source distribution, installer scripts, and artifact hashes.
Preparation copies the complete set into Git-local immutable release storage and all later
bootstrap, acceptance, and publication steps use those copies. `dist/` may be
rebuilt for diagnostics without changing the candidate identity. Inspect the
receipt-bound artifacts and review the commit.

### Preparation recovery

If tag creation, artifact preservation, or receipt writing fails after the
candidate commit, retain the candidate branch, tag, and Git-local artifacts.
From the clean candidate checkout, run:

```bash
uv run --script tools/release.py --prepare-attempt <retained-attempt> --yes
```

Resume does not allocate another identity. It requires the retained candidate
source and release-file commit to remain unchanged. An exact valid preparation
receipt is reused. Otherwise all release
gates, including artifact build and packaged readiness, must pass again before
a new receipt can be written. Interrupted copies do not replace the preserved
artifact set. Successfully replaced copies remain in Git-local `retained-*`
directories for inspection. Acceptance evidence still has to match the new
preparation identity; recovery never authorizes publication by itself.

Do not delete candidate evidence to silence a diagnostic or overwrite a
published tag. If the candidate cannot be resumed, retain its evidence and
prepare a different version from clean, synchronized `main`.

## Candidate acceptance

The local candidate commit and artifacts are not public yet.
Install that exact wheel into the user-level tool through the supported local
artifact upgrade path, restore a healthy representative repository with at
least one started watcher, then run the mandatory acceptance command:

```bash
agents-live upgrade --from <receipt-bound-wheel-path-printed-by-prepare>
uv run --script tools/release.py --accept-candidate --attempt <rc-or-final-attempt> \
  --repo <live-repository> --agent <safe-agent-identifier> \
  --cost-agent <safe-provider-agent-identifier> --yes
```

The first command bootstraps the candidate. The acceptance command then makes
the installed candidate upgrade itself from the same wheel. It captures every
registered repository's started and loadable state, requires a started watcher
in the selected representative repository, and requires:

- the same exact candidate version after replacement;
- unchanged started and loadable state across all registered repositories;
- healthy all-repository `doctor` results before and after;
- restoration of every started watcher.

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
uv run --script tools/release.py --accept-candidate --attempt <rc-or-final-attempt> \
  --repo <live-repository> --agent <safe-agent-identifier> \
  --cost-agent <safe-provider-agent-identifier> --resume --yes
```

After replacement succeeds, acceptance runs a full operational pass through
the self-managed candidate. Choose an agent whose immediate run is safe and
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

Prepare and independently accept final-version bytes, then finalize and publish:

```bash
uv run --script tools/release.py --prepare-final --from-rc <accepted-rc> --yes
uv run --script tools/release.py --accept-candidate --attempt <final-attempt> --repo <live-repository> --agent <safe-agent> --cost-agent <safe-provider-agent> --yes
uv run --script tools/release.py --finalize --attempt <final-attempt> --yes
uv run --script tools/release.py --publish --attempt <final-attempt> --yes
```

Publication validates the two receipts instead of rerunning identical local
gates. It requires the tagged release commit to be exactly one commit ahead of
`origin/main`, atomically pushes candidate `HEAD` to `main` with the tag, and
creates the GitHub release. The release body includes version-specific Linux,
WSL, and Windows quick-install commands, one first-line summary per changelog
entry, and a link to the full changelog at the release tag, followed by GitHub's
generated notes (merged pull requests and the compare link).

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

The exact release commit's reusable Test workflow runs clean-root bootstrap
acceptance on Windows and Linux with package indexes disabled before publish.
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

## Historical Evidence

Retain original development-build versions, tags, hashes, receipts, and rejected
attempts. They describe historical bytes, not a second active release channel.
Use the explicit legacy-tag migration command only for the recorded unpublished
conflict. Never relabel historical deployment observations as package acceptance
or overwrite a published tag. Remote testing distribution requires separate
authorization and does not confer stable-publication approval.
