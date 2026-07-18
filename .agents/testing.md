---
title: Testing Agents Live Locally and as an Installed Tool
description: Runbook for separating source, wheel, and published-tool validation
---

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

## Validate the current checkout

Run the portable suite and release gates:

```bash
uv run --with-editable . --script tests/test_smoke.py
uv run --script tools/pre-release-audit.py
uv build
```

Exercise source behavior against a configured project by keeping `--repo`
explicit:

```bash
uv run --with-editable . agents-live --repo ~/repos/life doctor
uv run --with-editable . agents-live --repo ~/repos/life status
uv run --with-editable . agents-live --repo ~/repos/life dashboard --help
uv run --with-editable . agents-live repos list
uv run --with-editable . agents-live status --all-repos
uv run --with-editable . agents-live doctor --all-repos
uv run --with-editable . agents-live dashboard --all-repos --help
```

Use temporary projects for mutating smoke tests. Do not start, stop, migrate,
or initialize agents in `~/repos/life` unless that operational change is part
of the test.

## Validate the built wheel

Build, select the wheel for the current version, and run it in uv's isolated
tool environment:

```bash
uv build
version="$(uv version --short)"
wheel="dist/agents_live-${version}-py3-none-any.whl"
uvx --from "$wheel" agents-live --help
uvx --from "$wheel" agents-live --repo ~/repos/life doctor
uvx --from "$wheel" agents-live --repo ~/repos/life dashboard --help
uvx --from "$wheel" agents-live repos list
uvx --from "$wheel" agents-live status --all-repos
```

Inspect both artifacts before publication:

```bash
unzip -l "$wheel"
tar -tzf "dist/agents_live-${version}.tar.gz"
```

Confirm that package modules, the vendored skill payload, tests, and release
tools are present, and that deployment-specific agents, logs, data, and private
adapters are absent.

## Validate the installed tool

Show the installed version and run the same read-only checks consumers use:

```bash
uv tool list
agents-live --repo ~/repos/life doctor
agents-live --repo ~/repos/life status
agents-live --repo ~/repos/life dashboard --help
```

Check PyPI and upgrade when a newer version is available:

```bash
uv tool upgrade agents-live
uv tool list
agents-live --repo ~/repos/life init
agents-live --repo ~/repos/life doctor
```

`init` refreshes an installed optional skill payload. `doctor` reports a
package and payload version mismatch. GitHub repository notifications can
provide proactive release notices: select **Watch**, **Custom**, then
**Releases**.

If bare `agents-live` was installed editable from this checkout, restore the
normal PyPI tool before testing consumer behavior:

```bash
uv tool install --force agents-live
```

## Validate a release candidate

Preview and prepare the release locally:

```bash
uv run --script tools/release.py --dry-run
uv run --script tools/release.py --prepare --yes
```

Preparation bumps every version surface, runs the gates, builds the target
artifacts, and creates a local commit and annotated tag. Before publication:

1. Review the release commit and tag.
2. Inspect the target-version wheel and source distribution.
3. Run the built-wheel checks from this runbook.
4. Confirm bare `agents-live` still represents the previously published tool.

Publish only after those checks pass:

```bash
uv run --script tools/release.py --publish --yes
```

After the GitHub workflow succeeds, run `uv tool upgrade agents-live` and
repeat the installed-tool checks. This final pass proves the artifact that
PyPI consumers receive, not only the local wheel.

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