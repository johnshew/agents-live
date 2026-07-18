---
# Starter: scheduled agent with a deterministic post-processor.
# Copy into `.claude/agents/` (the default agent directory - Claude
# Code, Copilot, and VS Code all discover it) as <agent-name>.md, then
# `agents-live run <agent-name>` to test it once.
# Names are lowercase-hyphen; the filename is the agent name.
description: Scheduled report agent. Never delegate to this agent.
disable-model-invocation: true    # operational agent - interactive surfaces must not auto-invoke it
runtime: claude                   # claude | copilot | none
mode: plan                        # plan (read-only tools) | write
schedule: "0 6 * * *"             # cron expression
# Processors are repo-relative paths: agent directories hold no
# executables (bare names are only valid in a legacy task directory).
post-processor: Agents/handlers/my-handler.py
# Safe-output validations (recommended for any post-processor with
# side effects - see docs/approach.md, "Frontmatter fields"):
# output-schema:
#   type: object
#   required: [summary]
# output-provenance: strict
---

Describe the job. The agent runs headless: tell it exactly what JSON to
output for the post-processor, and nothing else.
