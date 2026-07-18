---
# Starter: handler-only automation - no agent, pure scripted pipeline.
# Copy into `.claude/agents/` as <automation-name>.md. The pre-processor's
# JSON output feeds the post-processor directly; `skip: true` in that
# output ends the run cleanly.
description: Scripted pipeline automation (no agent). Never delegate to this agent.
disable-model-invocation: true
runtime: none
mode: plan
schedule: "*/30 * * * *"
# Repo-relative paths: agent directories hold no executables.
pre-processor: Agents/handlers/my-prep.py     # optional; may request skip
post-processor: Agents/handlers/my-handler.py
---

Body is unused when runtime is none (kept for humans reading the automation).
Document what the pre-processor gathers and what the handler does.
