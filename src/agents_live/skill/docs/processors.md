---
title: Writing a pre-processor or post-processor
description: The child process contract for Agents Live processors, and what changes with execution mode
ms.date: 2026-08-18
ms.topic: reference
---

# Processors

A processor is an ordinary program that Agents Live runs as a child process
around the provider call:

```text
pre-processor -> agent -> post-processor
```

Any of the three may be absent. A definition with no selector and both
processors is a deterministic pipeline with no model in it.

Processors are not a Python API. Everything a processor needs arrives as
environment variables and stdin, and everything it returns leaves through
stdout and its exit code, so `.py`, `.js`, `.ts`, `.ps1`, and `.sh` are equally
first-class.

This page records the contract as it ships today, not a proposal. Anything not
stated here is not part of it.

## Declaring one

```yaml
metadata:
  agents-live.pre-processor: "scripts/collect.py"
  agents-live.post-processor: "scripts/apply.py"
```

Both paths are relative to the skill directory. A path that does not resolve to
a file fails the run at launch with `pre-processor not found` or
`post-processor not found`.

## How it is launched

The interpreter is chosen by file extension:

| Extension | Command |
|---|---|
| `.py` | `uv run <path>` |
| `.js`, `.ts` | `node <path>` |
| `.ps1` | `pwsh -NoProfile -File <path>` |
| `.sh`, anything else | `<path>` executed directly |

Two consequences are worth knowing before you write the file.

**No arguments are passed.** A processor never receives argv from Agents Live.
Input arrives through the environment and stdin.

**`.py` runs under `uv run`.** The script executes in an isolated environment,
so a processor that imports anything outside the standard library needs an
inline script metadata header:

```python
# /// script
# requires-python = ">=3.12"
# dependencies = ["httpx"]
# ///
```

## What every processor receives

| | Value |
|---|---|
| Working directory | The repository root, not the skill directory |
| Timeout | `agents-live.timeout` if declared, otherwise 120 seconds, applied to each step separately |
| Exit code | 0 means success. Anything else fails the run |

The working directory is the most common surprise. A processor that resolves a
file next to itself must use its own location, for example
`Path(__file__).parent`, rather than a relative path.

### Environment

| Variable | Present when | Holds |
|---|---|---|
| `AGENTS_LIVE_AGENT_NAME` | Always | The definition's name |
| `AGENTS_LIVE_AGENT_ID` | Always | The stable identifier used in logs and state |
| `AGENTS_LIVE_LOG_FILE` | Always | Path to this agent's JSONL event log |
| `AGENTS_LIVE_CHANGED_FILES` | The firing carried changed paths | JSON array of repository-relative paths |
| `AGENTS_LIVE_PROJECT_MCP_CONFIG` | `agents-live.mcps` is declared, outside pipeline mode | Path to a generated MCP configuration file |
| `PIPELINE_MCP_URL`, `PIPELINE_MCP_TOKEN` | `mode: pipeline` only | The run-scoped side channel and its bearer token |

Anything in `agents-live.env` is added as well. The `AGENTS_LIVE_` variables are
written last, so a definition cannot override them. Everything else the parent
process had is inherited.

### Logging

`AGENTS_LIVE_LOG_FILE` names an append-only JSONL file that `agents-live logs`
reads. A processor may append its own records, one JSON object per line:

```json
{"log_schema": 5, "ts": "2026-08-18T09:12:04Z", "agent_name": "email-agent", "phase": "collect", "message": "42 threads"}
```

`log_schema`, `ts` with a UTC offset, and `agent_name` are the required fields.
`phase`, `status`, `message`, `duration_s`, and `level` are conventional and
used by the default views. Nothing needs importing to write this file.

## Pre-processor

**Input.** Stdin is closed. The pre-processor works from the environment, the
repository, and whatever it can reach itself.

**Output.** Stdout is the payload, not a log. Where it goes depends on the
shape of the run:

- with an agent step, it is appended to the prompt under a
  `Pre-processor context:` heading;
- with selector `none`, it becomes the post-processor's stdin;
- in pipeline mode, publish through `put` instead and treat stdout as
  incidental.

Because stdout reaches the model, progress chatter belongs on stderr or in the
log file. Print one value, usually a single JSON object.

**Skipping the run.** A stdout document that parses as a JSON object with a
truthy `skip` stops the firing before the provider is launched:

```python
print(json.dumps({"skip": True}))
```

The run ends with status `skipped`, no model is invoked, and no post-processor
runs. This is the right answer for a watcher that fires on a change with
nothing to do.

## Post-processor

**When it runs.** After the agent step, and only if nothing earlier failed and
the pre-processor did not skip.

**Input.** Stdin depends on the execution mode, and this is the difference that
catches people:

- Outside pipeline mode it receives the agent's output. If a structured value
  was extracted, it receives that value re-serialized as JSON, not the prose
  the model wrapped around it. If no value was extracted, it receives the raw
  text. With no agent step, it receives the pre-processor's stdout.
- In pipeline mode it receives nothing at all. Stdin is closed, and the
  post-processor is expected to `get` what it needs from the side channel.

**Output.** Stdout becomes the run's result text, which is what `status`,
`logs`, and `run --json` report.

## What execution mode changes

| | `plan` and `write` | `pipeline` |
|---|---|---|
| Post-processor stdin | Extracted JSON value, else raw text | Closed; use `get` |
| `PIPELINE_MCP_URL`, `PIPELINE_MCP_TOKEN` | Not set | Set for all three steps |
| Agent output with a post-processor declared | Must contain an extractable JSON value, otherwise the run fails with `output_parse_error` | No such requirement |
| `agents-live.mcps` | Allowed | Rejected when the definition loads |
| `agents-live.result-path` | Rejected when the definition loads | Allowed; the snapshot is returned with the outcome |
| Fenced `put` blocks in the body | Not used | Seeded before the pre-processor runs, then read-only for the run |

`plan` and `write` differ in what the model is allowed to do, not in what a
processor receives.

Output validation is independent of mode. `agents-live.output-schema`,
`agents-live.output-path-roots`, and `agents-live.output-provenance` are all
checked against the agent's output before the post-processor starts, so a
post-processor never has to defend against a shape the definition already
declared.

## Failure

| Condition | Category | Effect |
|---|---|---|
| Non-zero exit | `pre_processor_crash` or `post_processor_crash` | Run fails; stderr becomes the recorded message |
| Exceeded the timeout | `timeout` | Run fails; processors are not retried |
| Pre-processor emitted `{"skip": true}` | none | Run ends `skipped`, successfully |

Processors are never retried. Only the agent step retries, once on timeout and
twice on empty provider output.

On success, a processor's stderr is kept as that step's message, so a warning
written there is visible without failing the run.

## Worked examples

[markdown-polisher.md](markdown-polisher.md) builds the same watched agent
twice, once in `plan` mode with a validating post-processor and once in
`pipeline` mode with both processors and the side channel. It is the best place
to see the two stdin contracts side by side.
