"""Inspect one resolved run step without executing it."""
from __future__ import annotations

import argparse
import json
import os
import sys

from ... import agent, paths
from .. import resolve
from .run import _instructions, _options


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--name", required=True)
    parser.add_argument("--role", choices=("pre", "agent", "post"))
    parser.add_argument("--changed-files")
    parser.add_argument("-p", "--prompt")
    parser.add_argument("--prompt-file")
    parser.add_argument("-o", "--option", action="append", default=[])
    args = parser.parse_args(argv)
    try:
        changed = tuple(json.loads(args.changed_files)) if args.changed_files else ()
        if not all(isinstance(item, str) for item in changed):
            raise ValueError("--changed-files must be a JSON string array")
        request = agent.Request(
            text=_instructions(args),
            changed_files=changed,
            options=_options(args.option),
        )
        overflow = (
            agent.changed_files_overflow(request.changed_files)
            or agent.instructions_overflow(request.text)
            or agent.options_overflow(request.options)
        )
        if overflow is not None:
            raise ValueError(overflow)
        root = paths.resolve_root()
        resolution = resolve.resolve(args.name, root=root, action="inspect context for")
        spec = resolution.spec
        shape = agent.shape(spec)
        role = args.role or ("pre" if shape.has_pre else "agent")
        step = agent.Step(role)
        if role == "pre" and not shape.has_pre:
            raise ValueError("pre processor is not declared")
        if role == "agent" and not shape.has_agent:
            raise ValueError("agent step is not declared")
        if role == "post" and not shape.has_post:
            raise ValueError("post processor is not declared")
        if shape.needs_mcp:
            raise ValueError(
                "pipeline context requires a live run-scoped MCP server")
        scratch = paths.repo_state_dir(resolution.root) / "runs" / spec.name / "context-preview"
        launch = agent.prepare(
            spec,
            step,
            agent.StepContext(
                request,
                run_id="context-preview",
                origin="manual",
                scratch=scratch,
            ),
        )
    except (agent.DefinitionError, json.JSONDecodeError, OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1

    payload = {
        "ok": True,
        "operation": "context",
        "agent": spec.identifier,
        "role": role,
        "argv": list(launch.argv),
        "environment": dict(launch.env),
        "cwd": launch.cwd,
        "stdin": launch.input_text,
        "stdin_available": role != "post",
        "ephemeral_paths_materialized": False,
    }
    if os.environ.get("AGENTS_LIVE_JSON") == "1":
        print(json.dumps(payload))
    else:
        print(f"agent: {payload['agent']}")
        print(f"role: {role}")
        print(f"cwd: {launch.cwd}")
        print("argv:")
        for value in launch.argv:
            print(f"  {value}")
        print("environment:")
        for name, value in sorted(launch.env):
            print(f"  {name}={value}")
        print("ephemeral paths: named, not materialized")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())