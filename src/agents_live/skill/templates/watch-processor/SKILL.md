---
name: watch-processor
description: Processes repository files after matching changes settle. Use when creating file-watch automation.
metadata:
  agents-live.schema-version: "2"
  agents-live.selector: "claude"
  agents-live.mode: "plan"
  agents-live.watch: "path/to/watch/** !**/*.tmp debounce 30s"
  agents-live.post-processor: "scripts/process.py"
---

Describe what to do when watched files change. Changed paths are prepended to
this prompt as a Files changed list.
