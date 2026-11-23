#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Agent hooks that keep the CDL steering line from being forgotten.

Modes (hook payload JSON on stdin, hook output JSON on stdout):

- ``transform``: Copilot ``userPromptTransformed``. Appends the steering
  reminder to the model-facing content of every user message.
- ``stop``: Copilot ``agentStop``. Forces one correction turn when the final
  assistant message after the latest user message (the text after the last
  tool call, or the only message in a tool-free turn) lacks the steering
  line. Earlier text collapses with the tool calls and is hidden from the
  developer, so only the final message counts.
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
    "Learning): begin your final message for this reply (the text after "
    "your last tool call) with one line, `Steering: <classification>; new "
    "goals: <list or none>; still open: <list or none>; continue until: "
    "<condition>.` and put the answers in that final message; earlier text "
    "is hidden with the tool calls.</steering-reminder>"
)
STEERING_LINE = re.compile(r"^[\s>*_`#-]*Steering:", re.IGNORECASE)


def transform(payload: dict) -> dict:
    content = payload.get("transformedPrompt")
    if not isinstance(content, str) or not content.strip() or MARKER in content:
        return {}
    return {"modifiedTransformedPrompt": f"{content}\n\n{REMINDER}"}


def final_message(transcript: Path) -> str | None:
    """Return the final assistant text after the last user message.

    ``None`` means there is no final message to judge: no reply yet, or the
    reply ended on a tool call or with empty text.
    """
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
    final = None
    for event in events[last_user + 1:]:
        if isinstance(event, dict) and event.get("type") == "assistant.message":
            final = event.get("data")
    if not isinstance(final, dict) or final.get("toolRequests"):
        return None
    content = final.get("content")
    if not isinstance(content, str) or not content.strip():
        return None
    return content


def _opens_with_steering(text: str) -> bool:
    first_line = next((line for line in text.splitlines() if line.strip()), "")
    return bool(STEERING_LINE.match(first_line))


def stop(payload: dict) -> dict:
    if payload.get("stop_hook_active") or payload.get("stopHookActive"):
        return {}
    path = payload.get("transcriptPath") or payload.get("transcript_path")
    if not isinstance(path, str) or not path:
        return {}
    try:
        text = final_message(Path(path))
    except OSError:
        return {}
    if text is None or _opens_with_steering(text):
        return {}
    return {
        "decision": "block",
        "reason": (
            "Your final message to the latest user message did not begin "
            "with the required steering line. Text before your last tool "
            "call is hidden from the developer. Reply now with one message, "
            "without tool calls, starting with `Steering: <classification>; "
            "new goals: <list or none>; still open: <list or none>; continue "
            "until: <condition>.`, restate the answers there and correct any "
            "work that classification changes."
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
