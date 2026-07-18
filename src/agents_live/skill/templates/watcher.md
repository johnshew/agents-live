---
# Starter: watch-triggered agent - runs when watched paths change.
# Copy into `.claude/agents/` as <agent-name>.md (lowercase-hyphen;
# the filename is the agent name).
description: File-watch processing agent. Never delegate to this agent.
disable-model-invocation: true
runtime: claude
mode: plan
watchPath:                        # file(s) or directory(ies), repo-relative
  - path/to/watch/
watchIgnore:                      # optional glob excludes
  - "*.tmp"
debounce: 30                      # seconds of quiet before dispatch
# Repo-relative path: agent directories hold no executables.
post-processor: Agents/handlers/my-handler.py
---

Describe what to do when the watched files change. The changed paths are
prepended to this prompt as a "Files changed:" list.
