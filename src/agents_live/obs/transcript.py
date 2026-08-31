#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.12"
# ///
"""Retrieve and normalize recorded provider conversations."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PACKAGE_PARENT = Path(__file__).resolve().parents[2]
if str(PACKAGE_PARENT) not in sys.path:
    sys.path.append(str(PACKAGE_PARENT))

from agents_live import preflight  # noqa: E402
from agents_live.obs import query  # noqa: E402
from agents_live.paths import repo_state_dir, resolve_root  # noqa: E402

SUMMARY_TEXT_LIMIT = 6000
SUMMARY_TOOL_LIMIT = 100


def _logs_dir() -> Path:
    return repo_state_dir(resolve_root(allow_sole_registered=True)) / "logs"


def _runs_dir() -> Path:
    return repo_state_dir(resolve_root(allow_sole_registered=True)) / "runs"


def _select(
    run_id: str | None,
    agent: str | None,
    since: str | None,
    errors: bool,
    last: int,
) -> list[dict[str, object]]:
    records = [
        record for record in query.load(query.files(_logs_dir()), since=since)
        if record.get("phase") == "done"
        and (run_id is None or record.get("run_id") == run_id)
        and (agent is None or agent.casefold() in str(
            record.get("agent_name", "")).casefold())
        and (not errors or record.get("status") == "error")
    ]
    records.sort(key=lambda record: str(record.get("ts", "")), reverse=True)
    return records[:last]


def _tool_call(value: object) -> dict[str, object] | None:
    if not isinstance(value, dict):
        return None
    name = value.get("name") or value.get("toolName") or value.get("tool_name")
    if not isinstance(name, str) or not name:
        return None
    result: dict[str, object] = {"name": name}
    arguments = value.get("arguments", value.get("input", value.get("parameters")))
    if arguments is not None:
        result["arguments"] = arguments
    return result


def _copilot_turns(stdout: str) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    turns: list[dict[str, object]] = []
    tools: list[dict[str, object]] = []
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict) or not isinstance(event.get("data"), dict):
            continue
        event_type = event.get("type")
        data = event["data"]
        if event_type in {"assistant.message", "user.message"}:
            content = data.get("content")
            requests = data.get("toolRequests")
            calls = [
                call for call in (_tool_call(item) for item in requests)
                if call is not None
            ] if isinstance(requests, list) else []
            tools.extend(calls)
            if isinstance(content, str) and content.strip() or calls:
                turn: dict[str, object] = {
                    "role": "assistant" if event_type == "assistant.message" else "user",
                    "text": content.strip() if isinstance(content, str) else "",
                }
                if calls:
                    turn["tool_calls"] = calls
                turns.append(turn)
    return turns, tools


def _provider(envelope: dict[str, object]) -> str:
    declared = envelope.get("provider")
    if isinstance(declared, str) and declared:
        return declared
    argv = envelope.get("argv")
    if isinstance(argv, list):
        for token in argv[:6]:
            if not isinstance(token, str):
                continue
            name = token.replace("\\", "/").rsplit("/", 1)[-1].casefold()
            name = name.removesuffix(".exe")
            if name == "copilot" or name.endswith("-copilot"):
                return "copilot"
            if name == "claude" or name.endswith("-claude"):
                return "claude"
    return "unknown"


def _legacy_prompt(envelope: dict[str, object], provider: str) -> str | None:
    prompt = envelope.get("prompt")
    if isinstance(prompt, str):
        return prompt
    argv = envelope.get("argv")
    if provider == "copilot" and isinstance(argv, list) and "-p" in argv:
        index = argv.index("-p")
        if index + 1 < len(argv) and isinstance(argv[index + 1], str):
            return argv[index + 1]
    return None


def _normalize(record: dict[str, object]) -> tuple[dict[str, object], str | None]:
    path_value = record.get("transcript")
    declared_state = record.get("transcript_state")
    state = declared_state if isinstance(declared_state, str) else "unknown"
    base: dict[str, object] = {
        "run_id": record.get("run_id"),
        "agent": record.get("agent_name"),
        "timestamp": str(record.get("ts", "")),
        "status": record.get("status"),
        "transcript_state": state,
        "provider": None,
        "prompt": None,
        "final": None,
        "tool_calls": [],
        "turns": [],
    }
    if not isinstance(path_value, str) or not path_value:
        return base, None
    path = Path(path_value)
    try:
        managed = _runs_dir().resolve()
        resolved = path.resolve()
    except OSError:
        base["transcript_state"] = "invalid_path"
        return base, None
    if not path.is_absolute() or not resolved.is_relative_to(managed):
        base["transcript_state"] = "invalid_path"
        return base, None
    if not path.is_file():
        base["transcript_state"] = "missing"
        return base, None
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
        envelope = json.loads(raw)
    except (OSError, json.JSONDecodeError):
        base["transcript_state"] = "corrupt"
        return base, None
    if not isinstance(envelope, dict):
        base["transcript_state"] = "corrupt"
        return base, raw

    provider = _provider(envelope)
    prompt = _legacy_prompt(envelope, provider)
    stdout = envelope.get("stdout")
    stdout = stdout if isinstance(stdout, str) else ""
    turns: list[dict[str, object]] = []
    tools: list[dict[str, object]] = []
    if prompt:
        turns.append({"role": "user", "text": prompt})
    final: object = None
    structured: object = None
    if "claude" in provider:
        try:
            payload = json.loads(stdout)
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, dict):
            final = payload.get("result")
            structured = payload.get("structured_output")
        if not isinstance(final, str):
            final = stdout.strip() or None
    elif "copilot" in provider:
        provider_turns, tools = _copilot_turns(stdout)
        turns.extend(provider_turns)
        final = next(
            (turn.get("text") for turn in reversed(provider_turns)
             if turn.get("role") == "assistant" and turn.get("text")),
            None,
        )
    else:
        try:
            payload = json.loads(stdout)
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, dict):
            final = payload.get("text") or payload.get("result")
            structured = payload.get("structured")
        if not isinstance(final, str):
            final = stdout.strip() or None
    if isinstance(final, str) and not any(
            turn.get("role") == "assistant" and turn.get("text") == final
            for turn in turns):
        turns.append({"role": "assistant", "text": final})
    base.update({
        "transcript_state": "available",
        "provider": provider,
        "prompt": prompt,
        "final": final,
        "tool_calls": tools,
        "turns": turns,
    })
    if structured is not None:
        base["structured"] = structured
    return base, raw


def _clip(value: object) -> object:
    if not isinstance(value, str) or len(value) <= SUMMARY_TEXT_LIMIT:
        return value
    omitted = len(value) - SUMMARY_TEXT_LIMIT
    return f"{value[:SUMMARY_TEXT_LIMIT]}\n...[{omitted} characters omitted]"


def _summary(item: dict[str, object]) -> dict[str, object]:
    return {
        key: value for key, value in item.items()
        if key not in {"turns", "structured"}
    } | {
        "prompt": _clip(item.get("prompt")),
        "final": _clip(item.get("final")),
        "tool_calls": [
            {"name": call.get("name")}
            for call in item.get("tool_calls", [])[:SUMMARY_TOOL_LIMIT]
            if isinstance(call, dict)
        ],
    }


def _render(item: dict[str, object], summary: bool) -> None:
    print(f"Run {item['run_id']} ({item['agent']})")
    print(f"Status: {item['status']} | Transcript: {item['transcript_state']}")
    if item["transcript_state"] != "available":
        return
    if summary:
        sections = (("Prompt", _clip(item.get("prompt"))),
                    ("Final", _clip(item.get("final"))))
        for heading, value in sections:
            if value:
                print(f"\n{heading}:\n{value}")
        tools = item.get("tool_calls", [])
        if tools:
            print("\nTools:")
            for call in tools[:SUMMARY_TOOL_LIMIT]:
                if isinstance(call, dict):
                    print(f"- {call.get('name', 'unknown')}")
        return
    for turn in item.get("turns", []):
        if not isinstance(turn, dict):
            continue
        print(f"\n{str(turn.get('role', 'unknown')).upper()}:")
        if turn.get("text"):
            print(turn["text"])
        for call in turn.get("tool_calls", []):
            if isinstance(call, dict):
                print(f"[tool] {call.get('name', 'unknown')}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_id", nargs="?")
    parser.add_argument("--agent")
    parser.add_argument("--last", type=int, default=1)
    parser.add_argument("--since")
    parser.add_argument("--errors", action="store_true")
    parser.add_argument("--summary", action="store_true")
    parser.add_argument("--raw", action="store_true")
    parser.add_argument("--format", choices=("readable", "json", "raw"),
                        default="readable")
    args = parser.parse_args(argv)
    if bool(args.run_id) == bool(args.agent):
        parser.error("provide exactly one of RUN_ID or --agent NAME")
    if args.last < 1:
        parser.error("--last must be a positive integer")
    if args.run_id and args.last != 1:
        parser.error("--last is available only with --agent")
    output_format = "raw" if args.raw else args.format
    if args.raw and args.format != "readable":
        parser.error("--raw cannot be combined with --json or --format")
    if output_format == "raw" and (args.agent or args.summary):
        parser.error("--raw requires one RUN_ID and cannot be combined with --summary")
    try:
        records = _select(
            args.run_id, args.agent, args.since, args.errors, args.last)
    except ValueError as exc:
        preflight.emit_failure("logs transcript", str(exc), code="usage_error")
        return 2
    if not records:
        preflight.emit_failure(
            "logs transcript", "no matching terminal run", code="not_found")
        return 1
    normalized = [_normalize(record) for record in records]
    if output_format == "raw":
        raw = normalized[0][1]
        if raw is None:
            preflight.emit_failure(
                "logs transcript",
                f"transcript is {normalized[0][0]['transcript_state']}",
                code="transcript_unavailable",
            )
            return 1
        print(raw)
    elif output_format == "json":
        items = [item for item, _ in normalized]
        if args.summary:
            items = [_summary(item) for item in items]
        print(json.dumps({"transcripts": items}, ensure_ascii=False))
    else:
        for index, (item, _) in enumerate(normalized):
            if index:
                print("\n" + "=" * 72 + "\n")
            _render(item, args.summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())