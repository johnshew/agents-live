---
title: Developing Agents Live
description: Build, test, and run agents-live from source without replacing the installed tool
---

How to build, test, and change the code in this repository.

## Layout

- `src/agents_live/` - the Python package. Canonical behavior is grouped
  under `agent/`, `runtime/`, `state/`, `obs/`, `pipeline/`, and `cli/`;
  `dispatch.py` is the handoff between runtime firings and agent execution.
  `legacy/` contains one-major-cycle 5.x migration and cleanup code only.
  The vendored skill payload remains under `skill/`.
- `tests/test_smoke.py` - export-safe smoke suite. Runs against temp
  projects only; never touches this checkout's `Agents/` directory.
- `tests/test_seams.py` - architecture, process-policy, migration, and
  correctness invariants over memory hosts and temporary repositories.
- `tools/pre-release-audit.py` - scans for personal information,
  secrets, and nonportable paths. Must pass before any release.
- `Agents/` - local runtime directory (handlers, logs) used when
  agents-live is exercised in this checkout; not package source.
- `docs/` - design documents and the high-level backlog for the project
  itself; not part of the installed skill payload.
  See [docs/README.md](../docs/README.md).

## Commands

Always go through `uv` - never plain `python3` (system interpreters are
often too old; the package requires Python 3.12+).

On Microsoft-managed native Windows or WSL hosts where the public Python
artifact endpoint fails TLS negotiation, configure the approved package proxy
for that runtime before running these commands. Follow
[diagnostics.md](../src/agents_live/skill/docs/diagnostics.md), including its
exact-version check; the proxy may lag the public release.

```bash
# run every portable smoke and seam test
uv run --with-editable . python -m unittest discover -s tests -v

# run the public framework smoketest
uv run --with-editable . agents-live smoketest

# run the CLI from source
uv run --with-editable . agents-live --help

# audit the tree for release-blocking content
uv run --script tools/pre-release-audit.py
```

The `dev` dependency group supplies test-only dependencies such as DuckDB to
all `uv run` commands in this checkout. Do not add per-command `--with` flags
for dependencies already declared there; doing so creates separate ephemeral
environments and lengthens the edit loop.

## Source checkout and installed tool

See [testing.md](testing.md) for the full source, wheel, and installed-tool
validation matrix.

Use the editable project environment when testing code in this repository:

```bash
uv run --with-editable . agents-live --repo ~/repos/<target-project> doctor
uv run --with-editable . agents-live --repo ~/repos/<target-project> dashboard --dev
```

These commands execute the current checkout without replacing the user-level
tool. From another repository, bare `agents-live` executes the self-managed
version selected by the stable `current` path:

```bash
agents-live --repo ~/repos/<target-project> doctor
agents-live --version
agents-live upgrade
```

`agents-live --version` reports the selected stable release or numbered RC.
Self-managed installation retains complete versions side by side under
`versions/`; `current` selects one. `agents-live upgrade` installs and selects
the latest stable generation, includes plugin wheels declared by registered
repositories before sealing it, and converges host integrations through the
selected version. Local RC deployment uses a new numbered package version for
each changed candidate, so successive candidates do not collide.

Use `agents-live --repo <project> upgrade` to upgrade the runtime and refresh
that project's installed skill payload. `agents-live --repo <project> doctor`
reports any package and skill payload version mismatch.

## Conventions

- Standalone scripts carry PEP 723 headers and run via
  `uv run --script <path>`.
- Docs under `src/agents_live/skill/docs/` carry frontmatter
  (`title`, `description`, `ms.date`, `ms.topic`); update `ms.date`
  when you materially change a doc.
- Keep `README.md` and the skill docs consistent - the README's
  feature claims and Documentation links mirror
  `src/agents_live/skill/docs/overview.md`.
- Minimal diffs; match the style of the surrounding code and docs.

## Subagent models and effort

Follow [subagent-models.md](subagent-models.md) when selecting development and
review models. Disclose unavailable model or effort controls rather than
silently substituting. Small trivial edits and short reads may stay with the
supervisor under CDL. For large coordinated work, use one reusable integration
tester per RC that returns the tested head, base, scope and result. One agent
owns live local runtime mutation and restoration during RC deployment; serialize
integration. Do not rerun a gate already passed on the same head and environment.

## Commit hygiene

Refresh `origin` at meaningful checkpoints: before branching, after a long
implementation or test phase, and before publication. Rebase unshared work
onto the latest intended target, then inspect overlapping imports, call sites,
and tests even when Git reports no conflict. Do not rebase a published branch
without approval, and do not add synchronization merge commits. Fast-forward a
clean primary checkout after verifying the merged commit is reachable from
`origin/main`.

Before GitHub writes, check the authenticated login and repository permission.
With multiple accounts, use an owner credential scoped to the command process;
never print tokens or switch the shared active account while other agents work.
Preserve and restore any existing credential environment variable in `finally`.
Use a full tested head SHA with guarded merges. Fetch and report a moved head
instead of merging a different revision silently.

Keep PowerShell validation scripts fail-fast: capture `$LASTEXITCODE` immediately
after the native command and propagate it before formatting or another command
can replace it. Prefer one check per invocation or the validation runner, which
records and returns each failing command's status. Summarize outcomes and the
next decision, not each successful tool invocation.

Review the branch history before its first push:

```bash
git fetch origin main
git log --oneline origin/main..HEAD
git diff --check origin/main...HEAD
git rev-list --no-merges origin/main..HEAD | while read -r commit; do
  if ! git diff-tree --root --no-commit-id --name-only -r "$commit" | grep -q .; then
    git show -s --format='%h %s' "$commit"
  fi
done
```

The final command prints empty non-merge commits. It should produce no output.
The full branch review must also confirm that:

- Every commit describes one meaningful repository state with an imperative,
  conventional-commit subject.
- Plans and progress notes remain in the session, issue, or PR description.
- Each implementation commit is understandable and testable on its own.
- A commit does not exist only to correct behavior or documentation introduced
  earlier on the same unshared branch.
- Implementation commits remain separate from the single follow-up changelog
  commit, and release preparation remains a distinct commit.
- The branch contains no incidental `Merge remote-tracking branch
  'origin/main'` synchronization commit.

Clean up superseded or empty commits while the branch is still local and
unshared. If review has started or the branch is on the remote, do not rewrite
it without explicit developer approval. Never rewrite commits reachable from
`main` or a release tag. If an unshared branch falls behind, rebase it onto
current `origin/main`; do not merge `origin/main` into it merely to synchronize.

## Backlog

Pending work is tracked as GitHub issues on this repo (`gh issue
list`). File bugs and design questions there; reference them from commit
messages (`Fixes #N`).

[docs/backlog.md](../docs/backlog.md) holds only the high-level themes
and links them to their issues. Keep acceptance criteria, repro steps,
and status in the issue, never in the backlog document.

## Source of truth

Since 2026-07-18 this repository is the definitive source for the
agents-live framework; the earlier flow that assembled releases from a
private source repository is retired. Consumer repositories receive the skill
payload through `agents-live init`/`upgrade`; never treat an installed payload
as a source checkout or propose back-porting changes into one.

<!-- glp-update:v1 id=c90f511b-cf45-4dac-a71d-bd556d82e313 -->
<a id="glp-c90f511b-cf45-4dac-a71d-bd556d82e313"></a>
### GLP Update: Select the repository owner account before each gh sequence

- Update-ID: c90f511b-cf45-4dac-a71d-bd556d82e313
- Recorded-UTC: 2026-10-08T14:21:29Z
- Kind: correction
- Topics: github-cli, credentials, gh auth switch, GH_TOKEN, issue creation
- Workstream: 6.9.4rc5 (#565) orchestration
- Target: .agents/development.md, Commit hygiene; compared origin/main dccdc4b
- Source-Session: CDL session 2026-10-08 (local log only)
- Evidence-Basis: mixed
- Application: applied
- Consolidation: pending

#### Change
The developer directed that every `gh` command sequence start by switching to
the repository owner account (`gh auth switch --user <owner>`). A `GH_TOKEN`
(or `GITHUB_TOKEN`) environment
variable overrides the switch, so clear it in the command process first. When
the in-app issue tool returns 403 for an Enterprise Managed User, file the issue
with `gh` under the owner account instead of retrying the tool.

#### Evidence
- User direction, 2026-10-08: start each gh command sequence by switching to
  the right user, naming the repository owner account.
- Observed 2026-10-08: the issue tool and `gh issue create` returned "As an
  Enterprise Managed User, you cannot access this content"; `gh auth switch`
  then printed that the `GH_TOKEN` value was being used. After removing
  `GH_TOKEN` in the process and switching, `gh issue create` filed #566.

#### Previous Knowledge
Commit hygiene above says to use an owner credential scoped to the command
process and never switch the shared active account while other agents work.
This user direction supersedes the "never switch" clause for this repository.
Process-scoped token removal remains required; do not print tokens.

#### Verification and Limits
Verified once by filing #566. `gh auth switch` changes the shared active
account for every process on the host, so a concurrent agent that expects a
different account may be affected; the developer accepted this by directing
the switch. Whether the switch should be undone afterwards is unknown.

#### Follow-up
Fold into Commit hygiene at the next consolidation of this document.
<!-- /glp-update:v1 id=c90f511b-cf45-4dac-a71d-bd556d82e313 -->

<!-- glp-update:v1 id=7dcc21e7-7a86-4cd9-89e5-480ca755c02e -->
<a id="glp-7dcc21e7-7a86-4cd9-89e5-480ca755c02e"></a>
### GLP Update: Pin the owner token per process; the active gh account can be reverted

- Update-ID: 7dcc21e7-7a86-4cd9-89e5-480ca755c02e
- Recorded-UTC: 2026-10-08T23:35:44Z
- Kind: correction
- Topics: github-cli, credentials, gh auth switch, GH_TOKEN, git push, PowerShell quoting
- Workstream: 6.9.4rc5/rc6 release operations
- Target: .agents/development.md, Commit hygiene; compared origin/main 3733230
- Source-Session: CDL session 2026-10-08 (local log only)
- Evidence-Basis: mixed
- Application: applied
- Consolidation: pending

#### Change
`gh auth switch` changes shared host state and was observed to be reverted by a
concurrent process during 6.9.4rc5/rc6 work, so switching alone is unreliable.
In each command process, clear `GITHUB_TOKEN` and set `GH_TOKEN` from
`gh auth token --user <owner>` without printing the token. Push with the
process-scoped credential helper
`git -c credential.helper= -c "credential.helper=!gh auth git-credential" push ...`.
Pass issue and PR bodies through `--body-file` using a temporary file because
inline PowerShell quoting broke bodies.

#### Evidence
- Observed 2026-10-08: repeated EMU 403s after switching; pinning the token
  worked for PRs #570, #571, #573, #575 and #576, and issues #572 and #574.
- Observed 2026-10-08: inline PowerShell body quoting failed; using a temporary
  file with `--body-file` avoids that quoting path.

#### Previous Knowledge
The preceding [GLP update](#glp-c90f511b-cf45-4dac-a71d-bd556d82e313) says to
clear `GH_TOKEN` before switching. This corrects that method: avoid shared
account switching and pin the owner credential per process instead.

#### Verification and Limits
The process-scoped token approach succeeded for the listed GitHub writes.
Tokens were not printed. This evidence does not establish behavior for other
credential providers or shells.

#### Follow-up
Fold into Commit hygiene at the next consolidation of this document.
<!-- /glp-update:v1 id=7dcc21e7-7a86-4cd9-89e5-480ca755c02e -->

<!-- glp-update:v1 id=49387356-34c6-4342-aa9c-8354137ae10e -->
<a id="glp-49387356-34c6-4342-aa9c-8354137ae10e"></a>
### GLP Update: Merge with the full head SHA and guard branch cleanup on merge success

- Update-ID: 49387356-34c6-4342-aa9c-8354137ae10e
- Recorded-UTC: 2026-10-08T23:35:44Z
- Kind: lesson
- Topics: pull requests, merge, head SHA, branch cleanup
- Workstream: 6.9.4rc5/rc6 release operations
- Target: .agents/development.md, Commit hygiene; compared origin/main 3733230
- Source-Session: CDL session 2026-10-08 (local log only)
- Evidence-Basis: observed
- Application: applied
- Consolidation: pending

#### Change
`gh pr merge --match-head-commit` requires the full 40-character SHA. A short
SHA failed the merge, and chained cleanup then deleted the head branch and
closed PR #571; the branch had to be restored and the PR reopened. Check the
merge exit code, then verify the head commit is reachable from `origin/main`
with `git merge-base --is-ancestor` before deleting worktrees or branches. The
repository may already delete the remote head branch on merge, so a subsequent
failed remote delete is expected.

#### Evidence
- Observed during PR #571 on 2026-10-08: the short SHA merge failed and the
  chained cleanup deleted the branch; restoration and reopening were required.

#### Previous Knowledge
Commit hygiene already requires a full tested head SHA and guarded merges. This
adds the specific 40-character requirement, cleanup ordering, reachability
check, and expected remote-branch deletion behavior.

#### Verification and Limits
The PR #571 merge was completed after restoring the branch and reopening the
PR. The exact cleanup outcome can vary when the repository deletes remote
branches automatically.

#### Follow-up
Fold into Commit hygiene at the next consolidation of this document.
<!-- /glp-update:v1 id=49387356-34c6-4342-aa9c-8354137ae10e -->
