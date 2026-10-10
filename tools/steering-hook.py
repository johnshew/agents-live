#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Agent hooks that keep the CDL steering line from being forgotten.

Modes (hook payload JSON on stdin, hook output JSON on stdout):

- ``transform``: Copilot ``userPromptTransformed``. Appends the steering
  reminder to the model-facing content of every user message.
- ``stop``: Copilot ``agentStop``. Forces one correction turn when the first
  reply text after the latest user message lacks the steering line.
- ``claude-prompt``: Claude Code ``UserPromptSubmit``. Adds the reminder as
  additional context.

Every mode fails open: unreadable input or transcripts produce no output.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

MARKER = "<steering-reminder>"
REMINDER = (
    f"{MARKER}Repository protocol (AGENTS.md, Continuous Development and "
    "Learning): begin your reply to this message with one line, "
    "`Steering: <classification>; new goals: <list or none>; still open: "
    "<list or none>; continue until: <condition>.` Then act."
    "</steering-reminder>"
)
STEERING_LINE = re.compile(r"^[\s>*_`#-]*Steering:", re.IGNORECASE)


def transform(payload: dict) -> dict:
    content = payload.get("transformedPrompt")
    if not isinstance(content, str) or not content.strip() or MARKER in content:
        return {}
    return {"modifiedTransformedPrompt": f"{content}\n\n{REMINDER}"}


def first_reply_text(transcript: Path) -> str | None:
    """Return the first non-empty assistant text after the last user message."""
    events = []
    with transcript.open(encoding="utf-8") as stream:
        for line in stream:
            try:
                events.append(json.loads(line))
            except ValueError:
                continue
    last_user = None
    for index, event in enumerate(events):
        if isinstance(event, dict) and event.get("type") == "user.message":
            last_user = index
    if last_user is None:
        return None
    for event in events[last_user + 1:]:
        if not isinstance(event, dict) or event.get("type") != "assistant.message":
            continue
        data = event.get("data")
        content = data.get("content") if isinstance(data, dict) else None
        if isinstance(content, str) and content.strip():
            return content
    return None


def stop(payload: dict) -> dict:
    if payload.get("stop_hook_active") or payload.get("stopHookActive"):
        return {}
    path = payload.get("transcriptPath") or payload.get("transcript_path")
    if not isinstance(path, str) or not path:
        return {}
    try:
        text = first_reply_text(Path(path))
    except OSError:
        return {}
    if text is None:
        return {}
    first_line = next((line for line in text.splitlines() if line.strip()), "")
    if STEERING_LINE.match(first_line):
        return {}
    return {
        "decision": "block",
        "reason": (
            "Your reply to the latest user message did not begin with the "
            "required steering line. Reply now starting with `Steering: "
            "<classification>; new goals: <list or none>; still open: <list "
            "or none>; continue until: <condition>.` and correct any work "
            "that classification changes."
        ),
    }


def claude_prompt(payload: dict) -> dict:
    return {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": REMINDER,
        }
    }


MODES = {"transform": transform, "stop": stop, "claude-prompt": claude_prompt}


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[1] not in MODES:
        print(f"usage: steering-hook.py {{{','.join(MODES)}}}", file=sys.stderr)
        return 2
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        return 0
    if not isinstance(payload, dict):
        return 0
    output = MODES[argv[1]](payload)
    if output:
        print(json.dumps(output))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
