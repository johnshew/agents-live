---
title: Release Cycle Reporting
description: Concurrent release targets, exact candidate evidence, and all in-flight work
---

# Release cycle reporting

Developer approval of the exact deployed RC is the publication decision.
Reports must recommend stable packaging, finalization, and publication using
that approval, not additional functional testing or stable acceptance.
Retained artifact identity, provenance, export audit, hashes, tags, and upload
availability remain verified. See
[release-requirements.md](../docs/release-requirements.md).

Choose a stable target, prepare and activate numbered local RCs, then accept and
publish the selected final packages. These steps form one release cycle. The
canonical business requirements and industry evidence are in
[development-release-process.md](../docs/development-release-process.md).

At the start of repository work, refresh authorized remote state and generate:

```bash
git fetch origin --prune
uv run --script tools/release-report.py
uv run --script tools/release-report.py --json
```

Markdown and JSON use the same calculation. The gitignored report is a
point-in-time observation, not permission to publish or a replacement for
package acceptance. Follow the repository's identity checks before remote
operations. Missing, stale or truncated evidence must be explicit.

## Routing and decisions

[.github/release-cycles.toml](../.github/release-cycles.toml) uses schema 2:

```toml
schema = 2
development_branch = "main"
default_cycle = "1.2.3"

[cycles."1.2.3"]
branch = "main"
next_rc = "1.2.3rc1"

[cycles."1.2.3".approval]
decision = "testing"
```

Develop on `main` by default. Configure a stabilization branch only when an
older source must remain separate from next-version development. Every cycle
keeps its own scope, selected RC, candidate under evaluation when applicable,
source identity, historical attempts, deployment observations and explicit issue
decisions. An older selected RC is not superseded merely because newer source or
a higher RC number exists.

There is no active bake branch, bake worktree, or bake-to-main promotion step.
Historical bake refs and records do not route development. Immutable numbered
attempt worktrees are retained release evidence, not another development channel.

Publication approval records `decision = "approved"`, the RC `attempt`, exact
full source `commit`, `wheel_sha256`, and `decided_on = "YYYY-MM-DD"`.
A different runtime commit requires
renewed validation and approval. An open issue can contain work delivered in an
RC but not yet released; show those states separately. A developer-approved
deferral is not a claim that the issue is fixed.

Published `6.9.4` retains developer-approved `6.9.4rc6`; no further RC
is planned for this completed release. Stable attempt `6.9.4-final-1` was
prepared once, finalized and published on 2026-10-09 without functional
retesting or runtime activation. GitHub package downloads and PyPI digests
matched retained package hashes; the package proxy did not yet list `6.9.4`.
Do not prepare or publish this version again. The developer explicitly approved shipping with
the logging and watcher observability gaps in #582, to be listed as known
deferred work in release notes. On 2026-10-09, #582 moved to 6.9.5 and #581
was closed and folded into its logging scope. RC1 was
prepared and consumed from `cd0be2e` on 2026-10-04; preserve its candidate commit,
immutable artifacts and retained receipts. RC2 was allocated and consumed from
`243ec7e` on the same date, but preparation failed at packaged dashboard
repositories readiness and it was not accepted. Preserve its attempt, commit,
build records and immutable artifacts independently. RC3 was prepared and consumed
from `8cda101` on 2026-10-04, passed isolated acceptance and was locally activated,
but is not publication-approved. Its retained source and artifact identities
remain recorded independently. RC4 was prepared and consumed from `dccdc4b` on
2026-10-05, passed isolated acceptance and is selected locally on the candidate
channel, but is not publication-approved. Its retained source and artifact
identities remain recorded independently. The planned issue list includes #556,
#538, #560, #561, #565, #572 and #574; issue closure and candidate delivery are
reported separately.
The 2026-10-04 user-directed reassignment of #530/#531 from 6.9.3 does not rewrite
that published cycle's retained attempts, deployment history or approval.
Published 6.9.2 and 6.9.3 have no next RC. Keep #530/#531 partial in 6.9.3,
not also deferred there; their outstanding work is planned in 6.9.4.
Do not reuse consumed RC1 through RC6. The developer assigned #565 to RC5 on
2026-10-08 for the main lock fix only after observing a maintenance run on RC4
hold the runtime launch gate for about 19 minutes and miss scheduled launches.
Task Scheduler query batching is a separate deferred follow-up tracked by #566.
RC5 was allocated and consumed from main with the #565 fix. Initial preparation
and a retry failed packaged dashboard readiness because the validator did not
drain the dashboard child's output pipe, blocking logging; the failure
reproduced at RC4 source and was not a product regression. PR #571 corrected
the validator, and requalification reused all four artifacts byte-exact,
passed all gates and passed isolated candidate acceptance. The first local
deployment was refused by the overly restrictive requalification allowlist,
which excludes instruction-only changes from PR #570 since RC5 source. The
orchestrator's tooling-only #572 correction merged in PR #573. A requalified
deployment then passed the package-input check, but activation was refused
while scheduled agent runs were in progress because activation refuses while
any run is active. The orchestrator removed a stale
repository registration for a missing folder to clear the candidate doctor
check. RC5 passed isolated acceptance but was never locally activated and is
superseded by RC6. On 2026-10-08, the developer directed that activation must
not wait for agent runs already in progress; the orchestrator filed #574 for
this package-changing product fix. RC6 included #565, the #571 readiness-
validator correction, #572 and #574; #572 is tooling-only and does not change
package bytes. RC6 was prepared from
`3733230d61b5f94e67cd33bf4d5482bb4fa38601`, passed packaged readiness and
isolated hello-world acceptance, and was locally activated on 2026-10-09.
The installed RC4 runtime first refused while an agent run was in flight. Later,
local-deploy refused because instruction-only commits advanced main after
preparation; #578 corrected this tooling gap in PR #579. Local-deploy reused
retained RC6 bytes and activated them at the first idle moment via a background
retry. The installed RC6 version, `doctor --all-repos` exit 0, and watchers
running from the RC6 generation were verified. On 2026-10-09, the developer
approved this exact RC6 source and retained wheel for 6.9.4 publication.
The manifest binds that approval to source
`3733230d61b5f94e67cd33bf4d5482bb4fa38601` and wheel digest
`1b623266e30e4d78f50ed489ad451adf2448aaa0e7da8e60aa5649abadf192ac`.
The stable tag binds commit `0f0bf55834ca3c37481d7127c80be825e2321ae6`
to retained final artifacts and RC6 approval. The report generator observes
GitHub publication independently, suppresses further release instructions,
and uses the manifest's frozen RC6 source rather than later development on main.
Issue #578 is tooling-only; the GitHub release notes include its correction
without changing approved RC package inputs. Its shipped changelog entry remains
deferred until the next package change. Retain the merged #556, #538, #560 and
#561 work and the dashboard responsiveness follow-up. Issues #565, #572, #574 and #578 are delivered;
#566, #567 and #582 remain deferred. RC5 is not publication-approved.
The additive #540 and #542 controls merit a semantic-version review without
automatically changing the requested 6.9.4 target.

Use the primary checkout only when clean and already on the intended branch;
otherwise use an isolated worktree. Verify ancestry before committing or pushing.
Remove task worktrees after delivery, preserving retained attempt worktrees and
their immutable receipts. Never discard another writer's changes.

## Evidence and report contents

The report and each summary lead with readiness, the blocker and the next action;
reference detail such as hashes and other identities follows the decision-oriented
summary.

- Show every configured release cycle, its full source commit, selected and next
  RC, approval, decisions, blockers, recommendation and concrete next action.
- Show retained candidate and final attempts, including prepared but unselected,
  rejected, finalized, missing and invalid evidence. Do not convert local
  readiness or historical observations into operational acceptance.
- Report the observed local runtime separately from recorded deployment history.
  Selection is scoped to the queried environment, not universal across hosts.
- Include all open PRs, review/check state and target branches, plus merged
  unreleased work. Do not silently hide work outside the default cycle.
- Include delivered, planned, partial, deferred and decision-needed issues, and
  all unassigned open issues. Keep issue closure separate from package delivery.
- Report latest stable GitHub release and each cycle's publication state.
  PyPI and proxy availability need independent verification; an index delay
  must not cause another publication of the same version.
- Include generation time and the provenance or limitation of every observation.

Git refs identify source. GitHub supplies issues, PRs and public release state.
The manifest records decisions those APIs cannot infer. Retained receipts bind
source, package hashes, environment and checks. Public CLI observations identify
the current local runtime. None of these sources alone proves all the others.

## Local testing and publication

From a clean synchronized configured source branch:

```bash
uv run --script tools/local-deploy.py --repo <live-repository> --rc <configured-next-rc>
```

Activation, state preservation, dashboard/watcher restoration and rollback are
official parts of the RC loop. Source changes require another RC number; never
overwrite consumed package identities. Developer approval of the exact RC
authorizes stable packaging, finalization and publication without stable
operational acceptance or functional retesting. Final preparation runs identity,
provenance, export audit, and build checks only; it does not repeat functional
verification or require stable operational acceptance. Use
[release.md](release.md) for those operations and [testing.md](testing.md) for
source, artifact and installed-state evidence boundaries.

When GitHub reports a stable target published, suppress preparation/publication
instructions for that target and direct development to a later cycle. Keep the
published cycle's historical evidence visible. Do not infer PyPI availability
from GitHub publication.

Keep this guide, `AGENTS.md`, the canonical process, manifest and generator
consistent. Write conclusions and next actions in ordinary release vocabulary,
with explicit uncertainty instead of an optimistic readiness label.