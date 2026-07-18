"""Pipeline MCP - schema-validated side channel for agents-live pipelines.

In-process MCP server that exposes a tiny path-addressed surface:
``put(path, value)`` and ``get(path)``. All three pipeline
phases (pre-processor, agent, post-processor) connect to the same server
over localhost HTTP and exchange JSON values keyed by path.

Reserved ``$``-suffixed sub-paths attach metadata to the parent path:

  * ``/<path>/$schema`` - binds a JSON Schema to ``<path>``. Value must
    be a JSON object. Discriminator:

      * If ``$ref`` is a key, the object must be exactly ``{"$ref":
        "/path/to/schema"}`` - a one-hop reference to another stored
        schema document. The target must itself be a direct schema
        (no chained ``$ref``).
      * Otherwise the object IS the schema (meta-validated against the
        Draft 2020-12 meta-schema). The ``$schema`` JSON-Schema dialect
        keyword inside it is optional.

    Subsequent ``put`` calls to ``<path>`` validate the value against
    the (resolved) bound schema; failures return errors plus the raw
    ``$schema`` binding so the agent can fix and retry within the same
    session.

    *Rule of thumb for agents:* "If ``$ref`` is in the object, follow
    it; otherwise the object is the schema."

Only ``$schema`` is recognized today; any other ``$<key>`` segment is
rejected so the namespace stays clean for future reserved keys.

Liveness is checked by putting and getting a value on the conventional
``/ping`` path; there is no dedicated ``pipeline_ping`` tool.

See ``Agents/docs/proposal-pipeline-side-channel.md`` for full design.

Usage (host side, e.g. ``run.py``)::

    from .pipeline_mcp import PipelineMcp
    mcp = PipelineMcp(agent_log=Path("Agents/logs/foo.log"))
    mcp.start()  # binds 127.0.0.1:<random port>, daemon thread
    os.environ["PIPELINE_MCP_URL"] = mcp.url
    os.environ["PIPELINE_MCP_TOKEN"] = mcp.token
    try:
        ...  # run pipeline phases
    finally:
        mcp.shutdown()
"""
from __future__ import annotations

import json
import logging
import secrets
import socket
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# The `mcp` SDK is required.  Imported lazily inside PipelineMcp so that
# importing this module does not crash on hosts that have not yet
# installed the SDK (e.g. lint-only runs).
_DEFAULT_HOST = "127.0.0.1"

# Reserved ``$<key>`` segments that may appear at the leaf of a path.
# Anything else is rejected so the namespace stays clean for future
# extensions (``$meta``, ``$type``, ``$ttl`` are plausible candidates).
_RESERVED_KEYS: frozenset[str] = frozenset({"schema"})

_SCHEMA_SUFFIX = "/$schema"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _pick_free_port(host: str = _DEFAULT_HOST) -> int:
    """Return a free TCP port on ``host``.

    Uses the kernel's ephemeral-port allocator (bind to port 0) and
    releases the socket immediately.  There is a benign race window
    between this call and the server actually binding; in practice the
    in-process MCP starts within milliseconds and the window is harmless.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return sock.getsockname()[1]


class PipelineMcp:
    """In-process pipeline-MCP server.

    Surface:
      * ``put(path, value)`` - store ``value`` (any JSON-serialisable
        type) at ``path`` (must start with ``/``). Latest write wins.
      * ``get(path)`` - read the latest value at ``path``. Returns
        ``{"ok": False, "error": "missing"}`` if no value has been put.

    Liveness convention: write and read ``/ping`` (e.g.
    ``put("/ping", "<token>")`` then ``get("/ping")``).

    Transport:
      * Localhost HTTP on a random free port.
      * Bearer token (``secrets.token_urlsafe(16)``) on by default.
    * JSONL logging of every tool call to ``agent_log`` (or stderr if no
        log path is supplied), plus a single ``op:"final-state"`` line on
        shutdown with put/get counters and the set of paths written.

    Schema binding is supported via ``/<path>/$schema`` paths; values written
    with ``put`` are validated against the bound schema using
    ``jsonschema`` Draft 2020-12. The public surface
    (``start``/``shutdown``/``url``/``token``/``store``) is stable.
    """

    def __init__(
        self,
        *,
        agent_log: Path | None = None,
        host: str = _DEFAULT_HOST,
        port: int | None = None,
        token: str | None = None,
        require_token: bool = True,
        run_id: str | None = None,
    ) -> None:
        self._host = host
        self._port = port if port is not None else _pick_free_port(host)
        self._token = token if token is not None else secrets.token_urlsafe(16)
        self._require_token = require_token
        self._agent_log = agent_log
        self._run_id = run_id
        self._lock = threading.Lock()
        self._store: dict[str, Any] = {}
        # Paths written by the trusted host via seed(). Agent-facing put
        # may never overwrite them: a rebound $schema (or a replaced
        # referenced schema document) would let the agent validate its
        # output against a schema of its own choosing (PKG-001).
        self._frozen: set[str] = set()
        self._puts = 0
        self._gets = 0
        self._thread: threading.Thread | None = None
        self._server: Any = None  # uvicorn.Server, set in start()
        self._started = threading.Event()

    # ------------------------------------------------------------------ public

    @property
    def url(self) -> str:
        # FastMCP serves streamable HTTP under ``/mcp`` by default.
        return f"http://{self._host}:{self._port}/mcp"

    @property
    def token(self) -> str:
        return self._token

    @property
    def port(self) -> int:
        return self._port

    @property
    def store(self) -> dict[str, Any]:
        """Return a copy of the current store (test/diagnostic hook)."""
        with self._lock:
            return dict(self._store)

    def seed(self, items: list[tuple[str, Any]]) -> None:
        """Pre-populate the store with ``(path, value)`` pairs from a
        trusted host (e.g. fenced ``put`` blocks parsed from an agent definition).

        Items are stored verbatim without schema validation -- the host
        is trusted to supply schemas before content, and the agent author
        is responsible for ordering. Every seeded path is frozen: the
        agent-facing ``put`` can never overwrite it, so seeded schema
        bindings (and the schema documents they reference) stay exactly
        as the host wrote them. Reserved-segment rules still apply
        (``$schema`` allowed, other ``$<key>`` rejected) so a typo in
        the agent definition surfaces immediately instead of silently masking a tool
        call.

        Raises ``ValueError`` on the first invalid entry.
        """
        normalized_entries: list[tuple[str, Any]] = []
        for path, value in items:
            if not isinstance(path, str) or not path.startswith("/"):
                raise ValueError(f"seed path must start with '/': {path!r}")
            segments = [s for s in path.split("/") if s]
            for i, seg in enumerate(segments):
                if not seg.startswith("$"):
                    continue
                if i != len(segments) - 1:
                    raise ValueError(
                        f"seed path {path!r}: reserved segment '${seg[1:]}' only allowed at leaf"
                    )
                key = seg[1:]
                if key not in _RESERVED_KEYS:
                    raise ValueError(
                        f"seed path {path!r}: unknown reserved segment '${key}'"
                    )
            normalized_entries.append((path, value))
        with self._lock:
            for path, value in normalized_entries:
                self._store[path] = value
                self._frozen.add(path)
                self._puts += 1
        for path, _value in normalized_entries:
            self._log_event(op="seed", path=path, ok=True)

    def start(self, *, timeout: float = 5.0) -> None:
        """Start the HTTP server in a daemon thread and wait for readiness."""
        if self._thread is not None:
            raise RuntimeError("PipelineMcp.start() called twice")
        self._thread = threading.Thread(
            target=self._serve_forever,
            name=f"pipeline-mcp:{self._port}",
            daemon=True,
        )
        self._thread.start()
        if not self._started.wait(timeout=timeout):
            raise RuntimeError(
                f"pipeline-mcp failed to start within {timeout}s on {self.url}"
            )

    def shutdown(self) -> None:
        """Signal the server to stop and dump final state to the log."""
        server = self._server
        if server is not None:
            server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=5.0)
        with self._lock:
            paths = sorted(self._store.keys())
            puts = self._puts
            gets = self._gets
        self._log_event(op="final-state", puts=puts, gets=gets, paths=paths)

    # ----------------------------------------------------------------- internal

    def _build_app(self) -> Any:
        """Build the FastMCP app exposing ``put`` / ``get``.

        Imports the SDK lazily so importing this module does not require
        ``mcp`` to be installed.
        """
        from mcp.server.fastmcp import FastMCP
        from mcp.types import ToolAnnotations

        fastmcp = FastMCP(
            name="pipeline-mcp",
            instructions=(
                "Pipeline side-channel MCP. Path-addressed key/value store: "
                "put(path, value) writes, get(path) reads. "
                "Liveness convention: put and get '/ping'."
            ),
            host=self._host,
            port=self._port,
            stateless_http=True,
            json_response=True,
            log_level="WARNING",
        )

        def _classify_path(path: Any) -> tuple[str | None, str | None, str | None]:
            """Return ``(normalized, error, reserved_key)``.

            ``normalized`` is the canonical path string when valid.
            ``reserved_key`` is the trailing reserved ``$<key>`` if any
            (e.g. ``"schema"`` for ``/foo/$schema``). User content paths
            (no reserved suffix) have ``reserved_key=None``.

            Errors:
              * non-string / does not start with ``/`` -> ``invalid-path``
              * trailing ``$<key>`` not in ``_RESERVED_KEYS`` -> ``reserved-key``
            """
            if not isinstance(path, str) or not path.startswith("/"):
                return None, "path must start with '/'", None
            # Inspect each segment; a leading ``$`` is only legal at the
            # leaf and only for recognized reserved keys.
            segments = [s for s in path.split("/") if s]
            for i, seg in enumerate(segments):
                if not seg.startswith("$"):
                    continue
                if i != len(segments) - 1:
                    return None, f"reserved segment '${seg[1:]}' only allowed at leaf", None
                key = seg[1:]
                if key not in _RESERVED_KEYS:
                    return None, (
                        f"unknown reserved segment '${key}'; "
                        f"only {sorted('$' + k for k in _RESERVED_KEYS)} are recognized"
                    ), None
                return path, None, key
            return path, None, None

        def _resolve_schema(
            raw_schema: Any, *, binding_frozen: bool = False,
        ) -> tuple[dict[str, Any] | None, str | None]:
            """Return ``(schema_doc, error)`` for a ``$schema`` binding.

            ``raw_schema`` is always a JSON object (enforced at put-time).
            If ``$ref`` is a key, follow it exactly once; the target must
            be a direct schema (no chained ``$ref``). Otherwise the
            object IS the schema. A host-seeded (frozen) binding may only
            reference a host-seeded document - otherwise an agent could
            supply the forward-declared target itself and validate
            against a schema of its own choosing (PKG-001).
            """
            if not isinstance(raw_schema, dict):
                # Defensive: put-time validation should have rejected this.
                return None, "$schema binding is not a JSON object"
            if "$ref" in raw_schema:
                ref = raw_schema["$ref"]
                with self._lock:
                    if ref not in self._store:
                        return None, f"$schema reference {ref!r} has no content"
                    referenced = self._store[ref]
                    ref_frozen = ref in self._frozen
                if binding_frozen and not ref_frozen:
                    return None, (
                        f"$schema reference {ref!r} is not host-seeded; "
                        "a seeded binding may only reference seeded schemas")
                if not isinstance(referenced, dict):
                    return None, f"$schema reference {ref!r} is not a JSON object"
                if "$ref" in referenced:
                    return None, f"$schema reference {ref!r} is itself a $ref; chains are not allowed"
                return referenced, None
            return raw_schema, None

        def _validate_inline_schema(schema_obj: Any) -> str | None:
            """Meta-validate an inline schema document. Returns error or None."""
            if not isinstance(schema_obj, dict):
                return "inline $schema must be a JSON object"
            try:
                from jsonschema.validators import Draft202012Validator
            except ImportError:
                return "jsonschema library is not installed"
            try:
                Draft202012Validator.check_schema(schema_obj)
            except Exception as exc:  # SchemaError + anything weird
                return f"inline $schema is not a valid Draft 2020-12 schema: {exc}"
            return None

        def _format_errors(errors: list[Any]) -> list[dict[str, str]]:
            formatted: list[dict[str, str]] = []
            for e in sorted(errors, key=lambda e: list(e.absolute_path)):
                path_str = "$." + ".".join(str(p) for p in e.absolute_path) if e.absolute_path else "$"
                formatted.append({"path": path_str, "message": e.message})
            return formatted

        @fastmcp.tool(
            name="put",
            description=(
                "Store a JSON-serialisable value at the given path on the "
                "pipeline-MCP side channel. Path must start with '/'. "
                "Latest write wins. If a JSON Schema has been bound to "
                "the path via '<path>/$schema', the value is validated "
                "against it; failures return the errors plus the raw "
                "$schema reference so you can fix and retry."
            ),
            annotations=ToolAnnotations(
                title="Pipeline put",
                readOnlyHint=False,
                destructiveHint=False,
                idempotentHint=True,
                openWorldHint=False,
            ),
        )
        def put(path: str, value: Any) -> dict[str, Any]:
            normalized, err, reserved_key = _classify_path(path)
            if normalized is None:
                self._log_event(op="put", path=str(path), ok=False, error=err)
                return {"ok": False, "error": err}

            # Host-seeded paths (schema bindings and the documents they
            # reference) are immutable to agents: latest-write-wins must
            # never let the validated party replace its own validator.
            with self._lock:
                frozen = normalized in self._frozen
            if frozen:
                msg = "path is host-seeded and read-only"
                self._log_event(op="put", path=normalized, ok=False, error=msg)
                return {"ok": False, "path": normalized, "error": msg}

            # 1. Writes to /<path>/$schema: value MUST be an object.
            #    Discriminator: '$ref' present -> reference (exactly one
            #    key, string value starting with '/'); otherwise object
            #    is the schema itself (meta-validated).
            if reserved_key == "schema":
                if not isinstance(value, dict):
                    msg = "$schema must be a JSON object"
                    self._log_event(op="put", path=normalized, ok=False, error=msg)
                    return {"ok": False, "path": normalized, "error": msg}
                if "$ref" in value:
                    if set(value.keys()) != {"$ref"}:
                        msg = "$ref reference must be the only key in the $schema object"
                        self._log_event(op="put", path=normalized, ok=False, error=msg)
                        return {"ok": False, "path": normalized, "error": msg}
                    ref = value["$ref"]
                    if not isinstance(ref, str) or not ref.startswith("/"):
                        msg = "$ref value must be an MCP path starting with '/'"
                        self._log_event(op="put", path=normalized, ok=False, error=msg)
                        return {"ok": False, "path": normalized, "error": msg}
                    schema_kind = "ref"
                    # Forward declaration is allowed: the target may not
                    # exist yet; resolution happens at validation time.
                else:
                    meta_err = _validate_inline_schema(value)
                    if meta_err:
                        self._log_event(op="put", path=normalized, ok=False, error=meta_err)
                        return {"ok": False, "path": normalized, "error": meta_err}
                    schema_kind = "direct"
                with self._lock:
                    self._store[normalized] = value
                    self._puts += 1
                self._log_event(op="put", path=normalized, schema_kind=schema_kind, ok=True)
                return {"ok": True, "path": normalized}

            # 2. Writes to /schemas/<...> (or any plain content path): if a
            # parent ``/<path>/$schema`` exists, validate against it.
            schema_path = normalized + _SCHEMA_SUFFIX
            with self._lock:
                raw_schema = self._store.get(schema_path)
                schema_frozen = schema_path in self._frozen
            if raw_schema is not None:
                schema_doc, resolve_err = _resolve_schema(
                    raw_schema, binding_frozen=schema_frozen)
                if resolve_err:
                    self._log_event(op="put", path=normalized, ok=False,
                                    error=f"schema-resolve: {resolve_err}")
                    return {
                        "ok": False,
                        "path": normalized,
                        "error": resolve_err,
                        "$schema": raw_schema,
                    }
                try:
                    from jsonschema.exceptions import SchemaError
                    from jsonschema.validators import Draft202012Validator
                except ImportError:
                    msg = "jsonschema library is not installed"
                    self._log_event(op="put", path=normalized, ok=False, error=msg)
                    return {"ok": False, "path": normalized, "error": msg, "$schema": raw_schema}
                try:
                    validator = Draft202012Validator(schema_doc)
                except SchemaError as exc:
                    msg = f"schema-invalid: {exc.message}"
                    self._log_event(op="put", path=normalized, ok=False, error=msg)
                    return {"ok": False, "path": normalized, "error": msg, "$schema": raw_schema}
                errors = list(validator.iter_errors(value))
                if errors:
                    formatted = _format_errors(errors)
                    self._log_event(op="put", path=normalized, ok=False,
                                    error="validation",
                                    error_count=len(formatted))
                    return {
                        "ok": False,
                        "path": normalized,
                        "errors": formatted,
                        "$schema": raw_schema,
                    }

            with self._lock:
                self._store[normalized] = value
                self._puts += 1
            self._log_event(op="put", path=normalized, value=value, ok=True)
            return {"ok": True, "path": normalized}

        @fastmcp.tool(
            name="get",
            description=(
                "Read the latest value at the given path on the pipeline-MCP "
                "side channel. Path must start with '/'. Returns "
                "{ok: false, error: 'missing'} if nothing has been written. "
                "Use get('<path>/$schema') first to discover the schema "
                "bound to a content path."
            ),
            annotations=ToolAnnotations(
                title="Pipeline get",
                readOnlyHint=True,
                destructiveHint=False,
                idempotentHint=True,
                openWorldHint=False,
            ),
        )
        def get(path: str) -> dict[str, Any]:
            normalized, err, _reserved = _classify_path(path)
            if normalized is None:
                self._log_event(op="get", path=str(path), ok=False, error=err)
                return {"ok": False, "error": err}
            with self._lock:
                present = normalized in self._store
                value = self._store.get(normalized)
                self._gets += 1
            self._log_event(op="get", path=normalized, present=present, ok=True)
            if not present:
                return {"ok": False, "path": normalized, "error": "missing"}
            return {"ok": True, "path": normalized, "value": value}

        return fastmcp

    def _serve_forever(self) -> None:
        """Run the streamable-HTTP server.  Called inside the daemon thread."""
        try:
            import uvicorn
        except ImportError as exc:  # pragma: no cover - mcp pulls in uvicorn
            raise RuntimeError("pipeline-mcp requires uvicorn (installed via `mcp`)") from exc

        fastmcp = self._build_app()
        app = fastmcp.streamable_http_app()
        if self._require_token:
            app = _BearerTokenMiddleware(app, self._token)

        # Silence uvicorn's per-request access log -- we already JSONL-log
        # every tool call ourselves, and uvicorn noise pollutes agent logs.
        logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
        config = uvicorn.Config(
            app,
            host=self._host,
            port=self._port,
            log_level="warning",
            access_log=False,
            lifespan="on",
        )
        self._server = uvicorn.Server(config)

        # Bridge uvicorn's "started" signal to our threading.Event so
        # callers of start() block until the listener is actually up.
        orig_startup = self._server.startup

        async def _startup(*args: Any, **kwargs: Any) -> Any:
            result = await orig_startup(*args, **kwargs)
            self._started.set()
            return result

        self._server.startup = _startup  # type: ignore[assignment]

        try:
            self._server.run()
        except Exception as exc:  # pragma: no cover - last-resort guard
            self._log_event(op="server-error", ok=False, error=repr(exc))
            self._started.set()  # unblock start()
            raise
        finally:
            self._log_event(op="server-stopped", ok=True)

    def _log_event(self, **fields: Any) -> None:
        """Append a single JSONL line to the agent log (or stderr fallback)."""
        entry = {
            "ts": _utc_now(),
            "log_schema": 5,
            "event_id": uuid.uuid4().hex,
            "component": "pipeline-mcp",
            **({"agent_name": self._agent_log.stem} if self._agent_log else {}),
            **({"run_id": self._run_id} if self._run_id else {}),
            **fields,
        }
        try:
            line = json.dumps(entry, separators=(",", ":"), default=str)
        except (TypeError, ValueError):
            line = json.dumps({"ts": entry["ts"], "component": "pipeline-mcp",
                               "op": fields.get("op"), "error": "unserialisable"})
        if self._agent_log is None:
            # No agent log: write to stderr so the developer can still see the trail
            # during interactive testing.
            import sys
            print(line, file=sys.stderr, flush=True)
            return
        try:
            self._agent_log.parent.mkdir(parents=True, exist_ok=True)
            with self._agent_log.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        except OSError:
            # Never let logging crash the server.
            pass


class _BearerTokenMiddleware:
    """Minimal ASGI middleware that enforces a static bearer token.

    The MCP SDK's built-in ``token_verifier`` requires a full
    ``AuthSettings`` block tied to an OAuth issuer URL, which is overkill
    for an in-process side-channel.  This middleware checks the
    ``Authorization: Bearer <token>`` header on every HTTP request and
    rejects mismatches with 401.  Constant-time compare avoids trivial
    timing oracles even on a localhost endpoint.
    """

    def __init__(self, app: Any, token: str) -> None:
        if not token or len(token) < 8:
            raise ValueError("pipeline-mcp bearer token must be at least 8 chars")
        self._app = app
        self._token = token

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self._app(scope, receive, send)
            return
        headers = dict(scope.get("headers") or [])
        auth = headers.get(b"authorization", b"").decode("latin-1", errors="replace")
        ok = False
        if auth.lower().startswith("bearer "):
            ok = secrets.compare_digest(auth[7:], self._token)
        if not ok:
            await send({
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"www-authenticate", b'Bearer realm="pipeline-mcp"'),
                ],
            })
            await send({
                "type": "http.response.body",
                "body": b'{"error":"unauthorized"}',
            })
            return
        await self._app(scope, receive, send)



__all__ = ["PipelineMcp"]
