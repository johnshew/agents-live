---
title: Releasing Agents Live
description: Required checks and commands for publishing agents-live releases
---

Checklist for cutting a release from this repository, the definitive
source since 2026-07-18.
Use [testing.md](testing.md) to validate source, target-version artifacts, and
the installed PyPI tool as separate execution modes.

Before release review, generate a local channel report using the policy and
command in [release-report.md](release-report.md). The report shows what has
reached bake, what remains deferred or needs a promotion decision, whether the
deployed artifact matches the channel tip, and the next promotion target. It is
gitignored and summarizes evidence without replacing any gate below.

## Numbered RC migration

The active `6.9.2` cycle adopts numbered RCs under
[#511](https://github.com/johnshew/agents-live/issues/511). The migration record
reserves `6.9.2rc1` for a legacy rejected attempt actually packaged as `6.9.2`;
its original preparation evidence was recovered in the preparing checkout,
not converted into a numbered RC package or accepted release. RC2 has historical
deployment evidence. RC3 failed packaged readiness (#516); RC4 passed the
unchanged Windows gate on its preparing environment but is not installed-accepted.
The manifest records RC4 as prepared and reserves RC5 for new bytes. Explicit
RC4 recovery requires its retained wheel and readiness receipt (#522).

The numbered lifecycle is implemented by `tools/release.py`. Existing
`local-deploy.py --rc` receipts support historical local evaluation and recovery,
not full operational acceptance. Never rebuild those consumed identities or
import their limited readiness evidence as full acceptance. The current release
still requires the issue dispositions, retained RC evidence, and developer
approval of that exact RC. No functional verification is required from RC
promotion through publication.

### Numbered attempt commands

Numbered acceptance uses an isolated temporary repository and the exact wheel.
It does not require a consumer repository or a local version switch. From clean,
synchronized bake, prepare the configured next RC:

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
uv run --script tools/release.py --accept-candidate --attempt <rc> --yes
```

When an agency plugin source is available, add `--agency-plugin <source>`.
The source is copied into the temporary repository and loaded by the candidate
runtime. It must provide `agency-copilot`; plugin loading, authentication, and
execution failures are errors, not reasons for silent fallback. Without that
option, the probe uses built-in Copilot. No domain-membership heuristic is used.
Both paths require an exact hello-world reply, a successful correlated terminal
event, and finite positive reported cost. No mail, Teams, calendar, consumer
agents, schedules, or local runtime activation are required.

The receipt records the provider, run ID, cost, wheel hash, preparation hash,
and committed validator identity. Current clean committed tooling may accept a
retained attempt without editing or rebuilding its immutable checkout or bytes.
The existing live `--repo`/`--agent`/`--cost-agent` path is optional integration
diagnostics, not a numbered-release prerequisite. The legacy live examples
later in this document do not override this numbered workflow.

Deploy the prepared RC for in-situ evaluation. All source, platform, packaged,
bootstrap, dashboard, provider, and operational checks belong to the RC process.
Once the developer accepts the deployed RC, record approval in the cycle
manifest with its `attempt`, original source `commit`, `wheel_sha256`, and
`decided_on`. Approval attests completion of the RC evaluation; publication
must not rerun it or require a new acceptance receipt. From the clean synchronized
configured cycle branch:

```bash
uv run --script tools/release.py --prepare-final --from-rc <accepted-rc> --yes
```

The final attempt has package version `<target>` but a separate identity such
as `<target>-final-1`. Its preparation stamps the accumulated stable changelog,
builds stable artifacts from the same runtime source, and retains the RC approval.
Version and release metadata may change; runtime changes require a new RC and
new developer acceptance. There is no stable acceptance stage and no functional
test execution during final preparation, finalization, or publication:

```bash
uv run --script tools/release.py --finalize --attempt <final-attempt> --yes
uv run --script tools/release.py --publish --attempt <final-attempt> --yes
```

Finalization creates the annotated stable tag and an immutable record binding it
to final preparation and the exact RC approval. Publication pushes the exact commit and tag
atomically, uploads retained bytes and a privacy-safe evidence digest, and refuses
replacement of existing draft assets. Both automatic and manual PyPI workflow
paths require a canonical stable tag and matching public evidence. Neither calls
the Test workflow or any functional gate. Publication never rebuilds retained
assets. Verify GitHub and PyPI availability and hashes independently; do not run
the installed tool as an additional release acceptance check.

### Retry and rejection

Use `--prepare-attempt <attempt> --yes` in its retained worktree to retry unchanged
preparation. Completed builds are reused byte-for-byte; changed or unreceipted
bytes are refused. An interruption before the commit/build record is complete
may require a new identity after preserving the failed attempt for inspection.
Use `--accept-candidate --attempt <attempt> --resume` with all live arguments
and `--yes` only when a matching upgrade checkpoint exists. Cycle mutation and
allocation locks refuse concurrent operations; after a crash, verify no operation
is active before removing only the stale lock directory. Never delete receipts.

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
developer-accepted RC authorizes stable packaging and publication without further
functional testing. Create the stable tag after packaging and provenance checks,
then publish exactly those retained bytes without rebuilding them.
A failed final build needs a distinct retained attempt identity
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
uv run --script tools/release.py --build-artifacts
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

## Legacy Non-Numbered Workflow

The following preparation and live-acceptance commands document the historical
non-numbered workflow. They are not publication prerequisites for an approved
numbered RC. Use the numbered attempt commands above for the current cycle.

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
uv run --script tools/release.py --prepare --resume --yes
```

Resume does not bump the version again. It requires the candidate to remain
one release commit ahead of current `origin/main`, rejects remote tags and
local tags that are unannotated or point elsewhere, and checks the release
file set. An exact valid preparation receipt is reused. Otherwise all release
gates, including artifact build and packaged readiness, must pass again before
a new receipt can be written. Interrupted copies do not replace the preserved
artifact set. Successfully replaced copies remain in Git-local `retained-*`
directories for inspection. Acceptance evidence still has to match the new
preparation identity; recovery never authorizes publication by itself.

Do not delete candidate evidence to silence a diagnostic or overwrite a
published tag. If the candidate cannot be resumed, retain its evidence and
prepare a different version from clean, synchronized `main`.

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
uv run --script tools/release.py --accept-candidate \
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

Publish the prepared commit and tag:

```bash
uv run --script tools/release.py --publish --yes
```

Publication validates the two receipts instead of rerunning identical local
gates. It requires the tagged release commit to be exactly one commit ahead of
`origin/main`, atomically pushes candidate `HEAD` to `main` with the tag, and
creates the GitHub release. The release body includes version-specific Linux,
WSL, and Windows quick-install commands, one first-line summary per changelog
entry, and a link to the full changelog at the release tag, followed by GitHub's
generated notes (merged pull requests and the compare link).

Publishing the GitHub release triggers `.github/workflows/publish.yml`,
which resolves the stable tag, verifies release identity and evidence, downloads
the retained wheel and source distribution, checks hashes, and uploads them to
PyPI. It does not run the Test workflow, functional tests, or installed-tool
acceptance. The release tool attaches artifacts and their `SHA256SUMS` manifest
while the GitHub release is still a draft. Wait for publication to succeed and
verify public artifact availability and hashes. In
an interactive terminal, `gh run watch <run-id> --exit-status` can wait for
the workflow. Automation should use noninteractive run-status APIs or
`GH_PAGER=cat gh run view <run-id>` after completion; `gh run watch` may take
over the terminal's alternate screen.

The RC process owns clean-root bootstrap acceptance on Windows and Linux with
package indexes disabled. Publication does not repeat those checks.
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

## Publish a bake for remote testing

Use a GitHub prerelease only when another machine must install the exact bake
that has already passed local deployment validation. This is distribution for
testing, not stable promotion. It does not replace changelog review, candidate
acceptance, or `tools/release.py` for the eventual stable release.

The public bake identity is the complete commit-qualified PEP 440 version,
such as `6.7.0.dev0+g<commit>`. Before publication, require all of the
following:

- the work is merged to a clean `main` synchronized with `origin/main`;
- the version's `g<commit>` suffix names that exact commit;
- source tests, smoketest, audit, build, dashboard readiness, and bootstrap
  readiness passed for that commit;
- the commit-qualified wheel was installed and accepted on a live host;
- the annotated tag is `v<complete-version>` and targets that commit; and
- the asset directory contains exactly one wheel, one source archive,
  `install.ps1`, `install.sh`, and `SHA256SUMS-<complete-version>`.

Build the source archive and copy the installers from a Git archive of the
same commit used for the validated wheel. Stamp that archive with the same
complete bake version; never edit tracked version files in the shared checkout
to manufacture prerelease assets. Hash all four executable/package assets into
the versioned manifest. Do not replace an existing tag or asset: a correction
gets a new commit-qualified version and tag.

After checking every manifest entry against its file, create a draft with the
complete set so consumers cannot observe a release missing bootstrap assets:

```powershell
$repo = "johnshew/agents-live"
$version = "<complete-commit-qualified-version>"
$tag = "v$version"
$commit = git rev-parse HEAD
$assets = @(
    "<artifact-directory>\agents_live-$version-py3-none-any.whl",
    "<artifact-directory>\agents_live-$version.tar.gz",
    "<artifact-directory>\install.ps1",
    "<artifact-directory>\install.sh",
    "<artifact-directory>\SHA256SUMS-$version"
)

git tag -a $tag $commit -m "agents-live $version bake"
git push origin $tag
gh release create $tag @assets --repo $repo --verify-tag `
    --title "agents-live $version" --notes-file <notes-file> `
  --prerelease --draft
```

The release notes must say that this is a bake, is not on PyPI, and give the
exact-version installation command. If `gh release create` fails after the tag
push, diagnose and resume against that immutable tag; do not delete or rewrite
it. While the release is still a draft, verify all five asset names, positive
sizes, GitHub digests, and manifest hashes. Publish only after those checks:

```powershell
gh release view $tag --repo $repo --json isDraft,isPrerelease,assets
gh release edit $tag --repo $repo `
  --draft=false --prerelease --latest=false
```

Then verify the final state:

```powershell
gh release view $tag --repo $repo `
    --json tagName,isDraft,isPrerelease,name,publishedAt,url,assets
gh api "repos/$repo/releases/tags/$tag"
```

Require `isDraft: false`, `isPrerelease: true`, and GitHub digest and positive
size metadata for every asset. Confirm the stable release remains latest and
that PyPI still reports the prior stable version. GitHub renders `+` as `%2B`
in direct asset URLs; this is expected, and bootstrap compares the decoded URL
to the exact tag identity.

If a failure or interruption occurs before the release commit, the script
restores every version file and clears its staged changes. A failure after the
commit remains visible for recovery. Rerun `--publish --yes` if GitHub release
creation fails after the atomic push; publication accepts the exact tagged
commit locally or on `origin/main` and skips a release that already exists. Do
not rewrite or delete a pushed release tag.
