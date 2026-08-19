---
title: Safer Markdown polisher examples
description: Build watched Markdown polishers with plan or pipeline mode
ms.date: 2026-08-14
ms.topic: tutorial
---

The quick-start polisher uses `write` mode so the model can update Markdown
directly. Use one of these variants when unattended changes need a
deterministic write boundary. Both variants watch `docs/`, receive the changed
paths from Agents Live, and refuse to write any path outside that event.

## Choose an execution mode

| Mode | Model authority | Validation boundary | Setup |
|---|---|---|---|
| `write` | Provider write tools | Provider policy and review | One definition |
| `plan` | Read-oriented provider tools | JSON Schema, output roots, and a post-processor | Definition, schema, post-processor |
| `pipeline` | Run-scoped `get` and `put` tools | Schema-checked MCP path and processors | Definition, pre-processor, post-processor |

`plan` is the smaller safer example. The model reads the changed files and
returns proposed full-file contents as JSON. Agents Live validates the JSON
and every `path` before starting the post-processor. The post-processor checks
the changed-file set again before writing.

`pipeline` gives the model no repository read or write tools. The
pre-processor publishes changed Markdown content and an output schema to the
run-scoped side channel. The model reads that input and publishes validated
results with `put`; the post-processor retrieves and applies them.

These controls are tool policy and deterministic mediation, not an operating
system sandbox. Every processor still runs with the local account's
permissions.

## Plan mode

Create this bundle:

```text
Agents/
`-- markdown-polisher-plan/
    |-- SKILL.md
    |-- schemas/
    |   `-- files.schema.json
    `-- scripts/
        `-- apply.py
```

Use this `Agents/markdown-polisher-plan/SKILL.md`:

```yaml
---
name: markdown-polisher-plan
description: Polish changed Markdown through a validated post-processor.
metadata:
  agents-live.schema-version: "2"
  agents-live.selector: "claude"
  agents-live.mode: "plan"
  agents-live.watch: "docs/** debounce 1s"
  agents-live.output-schema: "schemas/files.schema.json"
  agents-live.output-path-roots: '["docs"]'
  agents-live.output-provenance: "strict"
  agents-live.post-processor: "scripts/apply.py"
---

Correct spelling, grammar, punctuation, and Markdown formatting errors in the
files under `Files changed:`. Preserve meaning, links, code, and frontmatter.
Return one JSON object that matches the declared schema. Include only changed
Markdown files. Set each `content` value to the complete corrected file.
```

Use this `schemas/files.schema.json`:

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "type": "object",
  "additionalProperties": false,
  "required": ["files", "summary"],
  "properties": {
    "files": {
      "type": "array",
      "items": {
        "type": "object",
        "additionalProperties": false,
        "required": ["path", "content"],
        "properties": {
          "path": {
            "type": "string",
            "pattern": "^docs/.+\\.md$"
          },
          "content": {
            "type": "string"
          }
        }
      }
    },
    "summary": {
      "type": "string"
    }
  }
}
```

Use this `scripts/apply.py`:

```python
#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
from __future__ import annotations

import json
import os
import sys
from pathlib import Path, PurePosixPath


def normalized(value: str) -> str:
    return PurePosixPath(value.replace("\\", "/")).as_posix()


def main() -> int:
    payload = json.load(sys.stdin)
    changed = {
        normalized(item)
        for item in json.loads(os.environ.get("AGENTS_LIVE_CHANGED_FILES", "[]"))
        if isinstance(item, str)
    }
    root = Path.cwd().resolve()
    docs = (root / "docs").resolve()

    for entry in payload["files"]:
        relative = normalized(entry["path"])
        destination = (root / relative).resolve()
        if relative not in changed:
            raise RuntimeError(f"refusing unselected path: {relative}")
        if not destination.is_relative_to(docs) or destination.suffix.lower() != ".md":
            raise RuntimeError(f"refusing unsafe Markdown path: {relative}")
        if not destination.is_file():
            raise RuntimeError(f"changed file no longer exists: {relative}")
        destination.write_text(entry["content"], encoding="utf-8")
        print(f"[post] wrote {relative}", file=sys.stderr)

    print(payload["summary"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

Test the changed-file contract before enabling the watcher:

```bash
agents-live run markdown-polisher-plan --changed-files '["docs/example.md"]'
agents-live start markdown-polisher-plan
```

## Pipeline mode

Create this bundle:

```text
Agents/
`-- markdown-polisher-pipeline/
    |-- SKILL.md
    `-- scripts/
        |-- apply.py
        `-- prepare.py
```

Use this `Agents/markdown-polisher-pipeline/SKILL.md`:

```yaml
---
name: markdown-polisher-pipeline
description: Polish changed Markdown through the pipeline side channel.
metadata:
  agents-live.schema-version: "2"
  agents-live.selector: "claude"
  agents-live.mode: "pipeline"
  agents-live.watch: "docs/** debounce 1s"
  agents-live.pre-processor: "scripts/prepare.py"
  agents-live.post-processor: "scripts/apply.py"
---

Call `get("/input/files/manifest")`. For each file, call `get` on every path
in its `chunks` array and concatenate the returned strings in order. Call
`get("/output/files/$schema")` to read the required output schema. Correct
spelling, grammar, punctuation, and Markdown formatting while preserving
meaning, links, code, and frontmatter. Call `put("/output/files", value)` with
the complete result. Fix every validation error returned by `put` before you
finish. Do not publish any path that is absent from the manifest.
```

Use this `scripts/prepare.py`:

```python
#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["mcp<2"]
# ///
from __future__ import annotations

import asyncio
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path, PurePosixPath

CHUNK_SIZE = 4000

SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": False,
    "required": ["files", "summary"],
    "properties": {
        "files": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["path", "content"],
                "properties": {
                    "path": {"type": "string", "pattern": r"^docs/.+\.md$"},
                    "content": {"type": "string"},
                },
            },
        },
        "summary": {"type": "string"},
    },
}


def normalized(value: str) -> str:
    return PurePosixPath(value.replace("\\", "/")).as_posix()


def result_json(result) -> dict:
    text = "".join(item.text for item in result.content if hasattr(item, "text"))
    return json.loads(text)


@asynccontextmanager
async def pipeline():
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    url = os.environ["PIPELINE_MCP_URL"]
    token = os.environ["PIPELINE_MCP_TOKEN"]
    headers = {"Authorization": f"Bearer {token}"}
    async with streamablehttp_client(url, headers=headers) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            yield session


async def publish(manifest: list[dict[str, object]], values: dict[str, str]) -> None:
    async with pipeline() as session:
        puts: list[tuple[str, object]] = [
            ("/output/files/$schema", SCHEMA),
            ("/input/files/manifest", manifest),
            *values.items(),
        ]
        for path, value in puts:
            result = await session.call_tool("put", {"path": path, "value": value})
            payload = result_json(result)
            if not payload.get("ok"):
                raise RuntimeError(payload)


def main() -> int:
    root = Path.cwd().resolve()
    docs = (root / "docs").resolve()
    manifest: list[dict[str, object]] = []
    values: dict[str, str] = {}
    changed = json.loads(os.environ.get("AGENTS_LIVE_CHANGED_FILES", "[]"))
    for item in changed:
        if not isinstance(item, str):
            continue
        relative = normalized(item)
        source = (root / relative).resolve()
        if source.is_relative_to(docs) and source.suffix.lower() == ".md" and source.is_file():
            content = source.read_text(encoding="utf-8")
            index = len(manifest)
            chunks = [
                content[offset:offset + CHUNK_SIZE]
                for offset in range(0, len(content), CHUNK_SIZE)
            ]
            paths = [f"/input/files/{index}/{part}" for part in range(len(chunks))]
            manifest.append({"path": relative, "chunks": paths})
            values.update(zip(paths, chunks, strict=True))
    if not manifest:
        Path(os.environ["AGENTS_LIVE_CONTROL"]).write_text(
            json.dumps({"skip": True, "message": "no Markdown changed"}),
            encoding="utf-8",
        )
        return 0
    asyncio.run(publish(manifest, values))
    print(json.dumps({"published": [item["path"] for item in manifest]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

Use this `scripts/apply.py`:

```python
#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["mcp<2"]
# ///
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path, PurePosixPath


def normalized(value: str) -> str:
    return PurePosixPath(value.replace("\\", "/")).as_posix()


def result_json(result) -> dict:
    text = "".join(item.text for item in result.content if hasattr(item, "text"))
    return json.loads(text)


async def fetch() -> dict:
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    url = os.environ["PIPELINE_MCP_URL"]
    token = os.environ["PIPELINE_MCP_TOKEN"]
    headers = {"Authorization": f"Bearer {token}"}
    async with streamablehttp_client(url, headers=headers) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool("get", {"path": "/output/files"})
    payload = result_json(result)
    if not payload.get("ok") or "value" not in payload:
        raise RuntimeError(f"agent did not publish /output/files: {payload}")
    return payload["value"]


def main() -> int:
    payload = asyncio.run(fetch())
    changed = {
        normalized(item)
        for item in json.loads(os.environ.get("AGENTS_LIVE_CHANGED_FILES", "[]"))
        if isinstance(item, str)
    }
    root = Path.cwd().resolve()
    docs = (root / "docs").resolve()
    for entry in payload["files"]:
        relative = normalized(entry["path"])
        destination = (root / relative).resolve()
        if relative not in changed:
            raise RuntimeError(f"refusing unselected path: {relative}")
        if not destination.is_relative_to(docs) or destination.suffix.lower() != ".md":
            raise RuntimeError(f"refusing unsafe Markdown path: {relative}")
        if not destination.is_file():
            raise RuntimeError(f"changed file no longer exists: {relative}")
        destination.write_text(entry["content"], encoding="utf-8")
        print(f"[post] wrote {relative}", file=sys.stderr)
    print(payload["summary"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

Test the side channel before enabling the watcher:

```bash
agents-live run markdown-polisher-pipeline --changed-files '["docs/example.md"]'
agents-live start markdown-polisher-pipeline
```

Inspect either variant through the supported observability commands:

```bash
agents-live status
agents-live logs timeline markdown-polisher-plan --last 20
agents-live logs timeline markdown-polisher-pipeline --last 20
```

Stop the variant you started with `agents-live stop <name>`.