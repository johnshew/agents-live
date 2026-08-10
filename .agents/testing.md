---
title: Testing Agents Live Locally and as an Installed Tool
description: Runbook for separating source, wheel, and published-tool validation
---

## What deserves a test

The smoke suite is the backbone: it drives the full chain (create,
frontmatter, dispatch, watcher detect, stop) against temp
projects. Add a focused test beside it only when a failure would be
high-impact and either silent or combinatorial beyond what the smoke
suite can enumerate (flag matrices, format parsing, error
classification clear that bar).

Do not add unit tests against internal function signatures or for
small one-caller helpers - they freeze implementation details
(function names, call shapes, cache invariants) and catch nothing the
smoke suite doesn't. Delete such tests rather than porting them
through refactors. Every infrastructure change should name the
cheapest executable check that can falsify it before running the
broader suite.

## Test boundaries

Keep these execution modes distinct. A passing editable-source command does
not prove that the built wheel or installed PyPI tool works.

| Target | Command prefix | What it proves |
|---|---|---|
| Current checkout | `uv run --with-editable .` | Source code and package imports work from this tree |
| Built wheel | `uvx --from <wheel>` | The release artifact works without replacing the installed tool |
| Installed tool | bare `agents-live` | The user-level uv tool works as consumers run it |

Do not use `uv tool install --editable .` for routine source testing. It makes
bare `agents-live` follow the checkout and hides the distinction between source
and released behavior.

Interactive commands need a readiness assertion, not only `--help`. For the
dashboard, launch the built-wheel command against a temporary repository, wait
for `/api/agents`, assert the expected agent row and action flags, then stop the
process. A successful GET of `/` proves only that NiceGUI bound a port; the
websocket-rendered table is not present in that response. Run this check with
development reload enabled as well, because NiceGUI starts that worker as
`__mp_main__`.

Tests at translation boundaries must assert decisions as well as serialized
values. In particular, dashboard fixtures cover both `started` and `stopped`
rows and their Start/Stop availability. Multi-repository lifecycle fixtures
must include heterogeneous project configuration, including local and
registry ownership in the same collection. Release-tool diagnostics must be
tested for factual wording when an association cannot be inferred.

## Validate the current checkout

Run the portable suite and release gates:

```bash
uv run --with-editable . python -m unittest discover -s tests -v
uv run --with-editable . agents-live smoketest
uv run --script tools/pre-release-audit.py
uv build
```

CI runs the same suites one file at a time
(`uv run --with-editable . --script tests/<file>.py`), adding `--with duckdb`
so the query tool is started rather than skipped. Reproduce that form when a
failure appears only in CI.

The Test workflow runs both Ubuntu and Windows for pushes and pull requests.
Its manual dispatch accepts `all`, `ubuntu-latest`, or `windows-latest` when a
single host needs to be isolated. The publish workflow calls the same workflow
against the resolved release commit and cannot publish until both hosts pass.

A development host has an initialized global workspace, so tests that do
not build their own temp project still resolve a root here and fail only
in CI. Reproduce a bare host before pushing:

```bash
env -u AGENTS_LIVE_REPO XDG_DATA_HOME=/tmp/bare/data \
  XDG_STATE_HOME=/tmp/bare/state XDG_CONFIG_HOME=/tmp/bare/config \
  uv run --with-editable . python -m unittest discover -s tests -v
```

Exercise source behavior against a configured project by keeping `--repo`
explicit:

```bash
uv run --with-editable . agents-live --repo ~/repos/<target-project> doctor
uv run --with-editable . agents-live --repo ~/repos/<target-project> status
uv run --with-editable . agents-live --repo ~/repos/<target-project> dashboard --help
uv run --with-editable . agents-live repos list
uv run --with-editable . agents-live status --all-repos
uv run --with-editable . agents-live doctor --all-repos
uv run --with-editable . agents-live dashboard --all-repos --help
```

Use temporary projects for mutating smoke tests. Do not start, stop, migrate,
or initialize agents in `~/repos/<target-project>` unless that operational change is part
of the test.

## Validate the built wheel

Build, select the wheel for the current version, and run it in uv's isolated
tool environment:

```bash
uv build
version="$(uv version --short)"
wheel="dist/agents_live-${version}-py3-none-any.whl"
uvx --from "$wheel" agents-live --help
uvx --from "$wheel" agents-live --repo ~/repos/<target-project> doctor
uvx --from "$wheel" agents-live --repo ~/repos/<target-project> dashboard --help
uvx --from "$wheel" agents-live repos list
uvx --from "$wheel" agents-live status --all-repos
```

The dashboard `--help` check is an import-only precursor. The release candidate
is not validated until the built-wheel dashboard reaches `/api/agents` and
returns the expected temporary-project rows and action flags in normal and
`--dev` modes.

On a Microsoft-managed host whose proxy does not yet expose the candidate,
install the local wheel by path and let the proxy resolve only its
dependencies. Follow the Windows and WSL procedure in
[diagnostics.md](../src/agents_live/skill/docs/diagnostics.md). Keep the test
repository explicit and temporary. In 6.0, the ownership module is
`agents_live.state.ownership`; the removed `agents_live.ownership` path is not
a wheel-integrity check.

Inspect both artifacts before publication:

```bash
uv run python -m zipfile --list "$wheel"
tar -tzf "dist/agents_live-${version}.tar.gz"
```

Confirm that package modules, the vendored skill payload, tests, and release
tools are present. The source distribution intentionally includes the generic
`Agents/handlers/write-files.py` fixture and `Agents/logs/.gitkeep`; no other
`Agents/` logs or data, deployment-specific agents, or private adapters should
be present. The wheel contains only the installable package and its metadata.

## Validate the installed tool

Show the installed version and run the same read-only checks consumers use:

```bash
uv tool list
agents-live --repo ~/repos/<target-project> doctor
agents-live --repo ~/repos/<target-project> status
agents-live --repo ~/repos/<target-project> dashboard --help
```

Check PyPI and upgrade when a newer version is available:

```bash
agents-live upgrade
uv tool list
agents-live --repo ~/repos/<target-project> doctor
```

`upgrade` reinstalls the latest stable uv-managed runtime, then refreshes
managed skill payloads in the current initialized project and every available
registered repository. An explicit `--repo` limits refresh to one project;
`--runtime-only` and `--skills-only` isolate either phase. `init` retains its
payload refresh behavior for first-time setup and compatibility. `doctor`
reports a package and payload version mismatch. GitHub repository notifications
can provide proactive release notices: select **Watch**, **Custom**, then
**Releases**.

If bare `agents-live` was installed editable from this checkout, restore the
normal PyPI tool before testing consumer behavior:

```bash
uv tool install --force agents-live
```

## Validate a release candidate

Preview and prepare the release locally:

```bash
uv run --script tools/release.py --dry-run --bump patch
uv run --script tools/release.py --prepare --bump patch --yes
```

Run `/changelog-maintenance` first and replace `patch` with its recommended
bump. Preparation rejects an empty changelog or an undersized bump, updates
every version surface, runs the gates, builds the target artifacts, and creates
a local commit and annotated tag. Before publication:

1. Review the release commit and tag.
2. Inspect the target-version wheel and source distribution.
3. Run the built-wheel checks from this runbook.
4. Confirm bare `agents-live` still represents the previously published tool.

Publish only after those checks pass:

```bash
uv run --script tools/release.py --publish --yes
```

After the GitHub workflow succeeds, verify the exact release from PyPI and
repeat the installed-tool checks:

```bash
version="$(uv version --short)"
curl -fsS "https://pypi.org/pypi/agents-live/${version}/json" >/dev/null
uvx --refresh --index-url https://pypi.org/simple \
	--from "agents-live==$version" agents-live --version
agents-live upgrade
uv tool list
agents-live --version
agents-live --repo ~/repos/<target-project> doctor
```

The versioned JSON request confirms that trusted publishing created the PyPI
release record. The isolated exact-version check bypasses uv's cache and
confirms that PyPI's Simple API can resolve the release without pinning the
user-level tool. These endpoints may propagate at different times: if JSON
succeeds while `uvx` reports no matching version, retry `uvx` after the Simple
API catches up rather than republishing. Run the global upgrade only after
exact resolution succeeds; it validates the normal consumer workflow. This
final pass proves the artifact that PyPI consumers receive, not only the local
wheel.

## Recover an editable tool install

Use these checks when source and installed behavior appear unexpectedly
identical:

```bash
command -v agents-live
uv tool list
uv tool install --force agents-live
```

Then compare bare `agents-live` with the explicit editable-source command. The
two should report different versions whenever the checkout is ahead of the
latest PyPI release.