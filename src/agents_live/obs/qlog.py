#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["duckdb>=1.0", "pytz"]
# ///
"""Query Agents Live logs across current JSONL and archived records.

Default ordering is newest-first (ORDER BY ts DESC). Use --asc to reverse.
The `ts` column in the `log` view is typed TIMESTAMP WITH TIME ZONE, so
time math in --sql works: e.g. `WHERE ts > now() - INTERVAL 1 HOUR`.

Quick recipes
-------------
Last 20 entries for one agent (newest first):
    qlog.py taskflow-email-sync -n 20

Recent errors for one agent:
    qlog.py taskflow-email-sync --errors --since 1h

Errors across every log in the last hour (correlated):
    qlog.py --all --errors --since 1h

Slow runs (duration > 30s) in the last day:
    qlog.py --slow 30 --since 1d

Custom SQL (filters in WHERE; --sql is exclusive with filter flags):
    qlog.py --sql "SELECT agent_name, COUNT(*) FROM log
                   WHERE ts > now() - INTERVAL 1 HOUR
                   GROUP BY 1 ORDER BY 2 DESC"

--since/--until are independently optional and accept ISO-8601 or relative
forms: 30m, 2h, 1d, "1 hour ago", "2 days ago". ISO timestamps without an
offset use the Agents Live legacy UTC convention. qlog normalizes all accepted
forms to UTC before filtering and display.

Schema of the `log` view
------------------------
    ts             TIMESTAMP WITH TIME ZONE  -- parsed from the JSON `ts` field
    _src           VARCHAR                   -- source filename
    _jsonl         BOOLEAN                   -- source is JSONL (plaintext logs are FALSE)
    run_id         VARCHAR                   -- one run.py execution
    event_id       VARCHAR                   -- one physical JSONL event
    agent_name     VARCHAR
    phase          VARCHAR  (start|done|pre-processor|post-processor|activate|watcher|...)
    status         VARCHAR  (ok|error|skipped|start|...)
    trigger        VARCHAR  (cron|file-change|manual|...)
    duration_s     DOUBLE
    cost_usd       DOUBLE
    credits        DOUBLE
    premium_requests DOUBLE
    log_schema     INTEGER
    level          VARCHAR  (info|warning|error|...)
    message        VARCHAR
    error_category VARCHAR  (auto-injected into --columns when --errors is set)
    traceback      VARCHAR  -- printed separately under "-- Tracebacks --" in table mode
    _files         VARCHAR  -- basename list derived from changed_files (if present)

Other fields from the JSON (account, output, stderr, etc.) are exposed
as VARCHAR columns and addressable via --columns or --sql.
"""
from __future__ import annotations

import argparse
import glob as _glob
import sys
from pathlib import Path

import duckdb

try:
    from .. import preflight
    from ..paths import host_logs_dir, repo_state_dir, resolve_root
except ImportError:
    package_dir = Path(__file__).resolve().parent.parent
    if str(package_dir) not in sys.path:
        sys.path.insert(0, str(package_dir))
    import preflight
    from paths import host_logs_dir, repo_state_dir, resolve_root

import re
from datetime import datetime, timedelta, timezone

def logs_dir() -> Path:
    """This repo's log directory, resolved when it is asked for.

    Resolving at import time would make a missing project root a
    traceback from the import statement instead of this command's own
    sentence about what is wrong (#202).

    Read-only: the registry fallback is safe here (#192).
    """
    return repo_state_dir(resolve_root(allow_sole_registered=True)) / "logs"


def default_log() -> Path:
    return logs_dir() / "agents-live.log"


def archive_dir() -> Path:
    return logs_dir() / "archive"


def all_log_globs() -> list[str]:
    """This repo's logs unioned with the host-level logs.

    The host logs hold the built-in health-check loop and other
    repo-less operations; other repos are a --repo away.
    """
    return [
        str(logs_dir() / "*.jsonl"),
        str(logs_dir() / "*.log"),
        str(host_logs_dir() / "*.jsonl"),
        str(host_logs_dir() / "*.log"),
    ]

NORMALIZED_COLUMN_TYPES = {
    "duration_s": "DOUBLE",
    "cost_usd": "DOUBLE",
    "credits": "DOUBLE",
    "premium_requests": "DOUBLE",
    "log_schema": "INTEGER",
}

_RELATIVE_COMPACT = re.compile(r"^(\d+)\s*([mhd])$")
_RELATIVE_WORDS = re.compile(
    r"^(\d+)\s*(min|mins|minute|minutes|h|hr|hrs|hour|hours|d|day|days)"
    r"(?:\s+ago)?$",
    re.IGNORECASE,
)
_UNIT_TO_DELTA = {
    "m": "m", "min": "m", "mins": "m", "minute": "m", "minutes": "m",
    "h": "h", "hr": "h", "hrs": "h", "hour": "h", "hours": "h",
    "d": "d", "day": "d", "days": "d",
}


def _resolve_ts(value: str | None) -> str | None:
    """Normalize a relative or ISO-8601 bound to an aware UTC timestamp."""
    if value is None:
        return None
    text = value.strip()
    match = _RELATIVE_COMPACT.match(text) or _RELATIVE_WORDS.match(text)
    if match:
        count = int(match.group(1))
        unit = _UNIT_TO_DELTA[match.group(2).lower()]
        delta = {
            "m": timedelta(minutes=count),
            "h": timedelta(hours=count),
            "d": timedelta(days=count),
        }[unit]
        parsed = datetime.now(timezone.utc) - delta
    else:
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(
                f"invalid timestamp {value!r}; expected ISO-8601 or a relative "
                "duration such as 30m, 2h, or '1 day ago'"
            ) from exc
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _expand(patterns: list[str]) -> list[str]:
    files: list[str] = []
    for p in patterns:
        files.extend(_glob.glob(p) if any(c in p for c in "*?[") else [p])
    return sorted(f for f in files if Path(f).is_file())


def _is_jsonl(path: str) -> bool:
    """True when the file's first non-blank line looks like a JSON object.

    Plaintext logs (heartbeat, spawn stderr, transcript dumps) share the
    .log suffix; read_json_auto(ignore_errors=true) loads their lines as
    all-NULL rows. Each source is tagged so schema validation can scope
    itself to JSONL sources while plaintext stays queryable for
    diagnostics.
    """
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                stripped = line.strip()
                if stripped:
                    return stripped.startswith("{")
    except OSError:
        pass
    return False


def build_view(con: duckdb.DuckDBPyConnection, patterns: list[str],
               archives: Path | None = None) -> None:
    """Create a `log` view over the given files plus Parquet archives.

    Each file is read separately (read_json_auto infers schema per file)
    and the results are unioned by name so schema drift across files
    doesn't collapse rows into a raw JSON column.

    `archives` is the directory holding monthly Parquet archives; the
    caller passes it because it knows which repo the patterns came from.

    Adds `_src` (filename) for provenance.
    """
    # Agents Live JSONL timestamps are UTC. DuckDB infers homogeneous ISO
    # strings as naive TIMESTAMP values in its session timezone, so pin the
    # session to UTC before ingestion to preserve aware instants and give
    # offset-free legacy rows a deterministic UTC interpretation.
    con.sql("SET TimeZone='UTC'")
    files = _expand(patterns)
    if not files:
        raise SystemExit(f"no log files matched: {patterns}")
    selects = []
    for f in files:
        read_expr = (
            f"read_json_auto('{f}', format='newline_delimited', "
            f"ignore_errors=true, maximum_object_size=16777216)"
        )
        # DuckDB infers all-null columns as JSON. After UNION ALL BY NAME
        # that JSON typing leaks into other files where the same column
        # holds bare strings (e.g. "start"), and `rel.show()` then raises
        # `Malformed JSON at byte 0`. Cast any per-file JSON column to
        # VARCHAR at the source so the union sees consistent text.
        per_file_cols = con.sql(f"DESCRIBE SELECT * FROM {read_expr}").fetchall()
        projection_parts: list[str] = []
        for name, dtype, *_ in per_file_cols:
            if dtype == "JSON":
                projection_parts.append(f'CAST("{name}" AS VARCHAR) AS "{name}"')
            else:
                projection_parts.append(f'"{name}"')
        cols_sql = ", ".join(projection_parts) if projection_parts else "*"
        jsonl_sql = "TRUE" if _is_jsonl(f) else "FALSE"
        selects.append(
            f"SELECT {cols_sql}, '{Path(f).name}' AS _src, "
            f"{jsonl_sql} AS _jsonl FROM {read_expr}"
        )
    # Include current unified monthly Parquet archives if any exist.
    # Archives are produced from JSONL sources only, so they are always
    # in scope for schema validation.
    if archives is not None and archives.is_dir():
        unified_files = sorted(archives.glob("*.parquet"))
        if unified_files:
            paths_csv = ", ".join(f"'{p}'" for p in unified_files)
            selects.append(
                f"SELECT *, TRUE AS _jsonl "
                f"FROM read_parquet([{paths_csv}], union_by_name=true)"
            )
    union = " UNION ALL BY NAME ".join(selects)
    con.sql(f"CREATE VIEW _log_raw AS {union}")

    # DuckDB's read_json_auto types all-null columns as JSON. When the
    # union spans heterogeneous logs (some with values, some all-null),
    # querying rows whose value is a bare string (e.g. "parse-tracking")
    # raises `Malformed JSON at byte 0`. Cast every inferred JSON column
    # to VARCHAR so SELECTs are safe.
    raw_cols = con.sql("DESCRIBE _log_raw").fetchall()
    raw_names = {name for name, *_ in raw_cols}
    canonical = {"ts", "agent_name", "phase", "status", "trigger", "log_schema"}
    projections: list[str] = []
    for name, dtype, *_ in raw_cols:
        if name in canonical:
            continue
        if name in NORMALIZED_COLUMN_TYPES:
            target_type = NORMALIZED_COLUMN_TYPES[name]
            projections.append(
                f'TRY_CAST("{name}" AS {target_type}) AS "{name}"'
            )
        elif dtype == "JSON":
            projections.append(f'CAST("{name}" AS VARCHAR) AS "{name}"')
        else:
            projections.append(f'"{name}"')

    def source(*names: str, fallback: str = "NULL") -> str:
        available = [f'CAST("{name}" AS VARCHAR)' for name in names
                     if name in raw_names]
        if not available:
            return fallback
        return available[0] if len(available) == 1 else (
            f"COALESCE({', '.join(available)})")

    timestamp = source("ts", "timestamp")
    projections.extend((
        "TRY_CAST(regexp_replace("
        f"{timestamp}, ' UTCZ$', 'Z') AS TIMESTAMP WITH TIME ZONE) AS \"ts\"",
        f'{source("agent_name", "agent")} AS "agent_name"',
        "CASE WHEN " + source("event", fallback="''") + " = 'run' THEN 'done' "
        f'ELSE {source("phase", "event")} END AS "phase"',
        "CASE " + source("status", fallback="''") + " WHEN 'success' THEN 'ok' "
        "WHEN 'failed' THEN 'error' ELSE " + source("status") + " END AS \"status\"",
        f'{source("trigger", "origin")} AS "trigger"',
        "TRY_CAST(" + source("log_schema", "spec")
        + ' AS INTEGER) AS "log_schema"',
    ))

    # Optional derived column: render `changed_files` as a compact
    # basename list when the inferred type is actually a LIST. (Skipped
    # if the column is absent or scalar - the lambda would otherwise
    # fail to bind.)
    changed_files_type = next(
        (dtype for name, dtype, *_ in raw_cols if name == "changed_files"),
        None,
    )
    if changed_files_type and changed_files_type.endswith("[]"):
        projections.append(
            "CASE WHEN changed_files IS NOT NULL "
            "THEN array_to_string("
            "list_transform(changed_files, x -> regexp_replace(x, '^.*/', '')), "
            "', ') ELSE NULL END AS _files"
        )
    else:
        projections.append("NULL AS _files")

    # Backfill the standard columns the CLI references (default --columns
    # and the --errors / --slow queries). When the unioned logs happen to
    # contain none of a given field (e.g. no file has `status` or
    # `message`), that column is simply absent from _log_raw and any query
    # naming it raises a Binder Error. Project a typed NULL for each
    # standard column not already present so queries always bind.
    present = set(raw_names)
    present.add("_files")  # derived above
    STANDARD_COLUMNS = (
        "ts", "agent_name", "phase", "status", "trigger", "level",
        "message", "error_category", "traceback", "duration_s",
    )
    for col in STANDARD_COLUMNS:
        if col not in present:
            target_type = NORMALIZED_COLUMN_TYPES.get(col, "VARCHAR")
            projections.append(f'CAST(NULL AS {target_type}) AS "{col}"')

    con.sql(f"CREATE VIEW log AS SELECT {', '.join(projections)} FROM _log_raw")


def check_schema(con: duckdb.DuckDBPyConnection) -> list[str]:
    """Return contract violations for normalized columns in the log view."""
    actual = {
        name: dtype
        for name, dtype, *_ in con.sql("DESCRIBE log").fetchall()
    }
    violations = []
    for required in ("ts", "agent_name", "log_schema"):
        if required not in actual:
            violations.append(f"missing required column: {required}")
    for name, expected_type in NORMALIZED_COLUMN_TYPES.items():
        actual_type = actual.get(name)
        if actual_type is not None and actual_type != expected_type:
            violations.append(
                f"{name}: expected {expected_type}, got {actual_type}"
            )
    if not violations:
        # Row-level contract applies to JSONL sources only; plaintext
        # logs (heartbeat, spawn stderr, transcripts) load as all-NULL
        # rows by design and are exempt.
        invalid_count = con.sql(
            "SELECT count(*) FROM log "
            "WHERE _jsonl AND (ts IS NULL OR agent_name IS NULL "
            "OR log_schema IS NULL OR log_schema NOT IN (1, 5))"
        ).fetchone()[0]
        if invalid_count:
            violations.append(
                f"{invalid_count} JSONL row(s) violate required schema v5 fields"
            )
    return violations


MAX_CELL_WIDTH = 80


def show(rel: duckdb.DuckDBPyRelation) -> None:
    """Print a result set as a table, in a form its reader can read.

    DuckDB draws its table in box-drawing characters. Written to a
    console those are decoded as the UTF-8 they are; captured into a
    pipe by a shell whose console codepage is the Windows default, they
    are decoded as OEM bytes and every line becomes noise. Nothing
    reports it, so the sanctioned way to read runtime state degrades
    silently in the one case - a pipe - where the reader is a program
    (#186). Off a terminal the same rows are printed with ASCII rules,
    which no codepage rewrites.
    """
    if sys.stdout.isatty():
        rel.show(max_col_width=MAX_CELL_WIDTH, max_width=500)
        return
    columns = list(rel.columns)
    rows = [[_cell(value) for value in row] for row in rel.fetchall()]
    widths = [
        max(len(column), *(len(row[index]) for row in rows)) if rows
        else len(column)
        for index, column in enumerate(columns)
    ]
    print(" | ".join(name.ljust(width)
                     for name, width in zip(columns, widths, strict=True)))
    print("-+-".join("-" * width for width in widths))
    for row in rows:
        print(" | ".join(cell.ljust(width)
                         for cell, width in zip(row, widths, strict=True)))
    print(f"({len(rows)} row{'' if len(rows) == 1 else 's'})")


def _cell(value: object) -> str:
    """One value as a single line, cut to the column cap."""
    text = "" if value is None else str(value).replace("\n", " ")
    return text if len(text) <= MAX_CELL_WIDTH else text[:MAX_CELL_WIDTH - 3] + "..."


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("name", nargs="?",
                    help="agent name; resolves to <name>.log in this repo's "
                         "log directory if that file exists, otherwise used "
                         "as --agent substring filter")
    ap.add_argument("--log", default=None,
                    help="log file or glob (default: this repo's "
                         "agents-live.log)")
    ap.add_argument("--all", action="store_true",
                    help="union this repo's logs with the host-level logs")
    ap.add_argument("--agent", help="filter by agent name (substring match)")
    ap.add_argument("--since", help="ts >= this (ISO-8601 or relative; UTC)")
    ap.add_argument("--until", help="ts < this (ISO-8601 or relative; UTC)")
    ap.add_argument("--phase", help="filter by phase (start|done|watcher|activate|...)")
    ap.add_argument("--status", help="filter by status (ok|error|skipped|...)")
    ap.add_argument("--trigger", help="filter by trigger (cron|file-change|...)")
    ap.add_argument("--slow", type=float, metavar="SEC",
                    help="only runs with duration_s > SEC")
    ap.add_argument("--errors", action="store_true",
                    help="only level=error OR status=error")
    ap.add_argument("-n", "--limit", "--tail", type=int, default=200,
                    dest="limit",
                    help="max rows (default 200; 0=unlimited). "
                         "Aliases: -n, --tail")
    ap.add_argument("--columns", default="ts,_src,agent_name,phase,status,trigger,duration_s,message",
                    help="comma-separated columns to show")
    ap.add_argument("--order-by", dest="order_by", default="ts",
                    help="column to order by (default: ts)")
    direction = ap.add_mutually_exclusive_group()
    direction.add_argument("--desc", dest="direction", action="store_const",
                           const="DESC", help="newest first (default)")
    direction.add_argument("--asc", dest="direction", action="store_const",
                           const="ASC", help="oldest first")
    ap.set_defaults(direction="DESC")
    ap.add_argument("--sql", help="run custom SQL against the `log` view. "
                    "Mutually exclusive with filter flags (--agent, --since, "
                    "--until, --phase, --status, --trigger, --slow, --errors); "
                    "put those conditions in your WHERE clause.")
    ap.add_argument("--format", choices=["table", "jsonl", "csv"], default="table",
                    help="table (default; ASCII rules when not a terminal), "
                         "or csv/jsonl, which are the forms to parse")
    ap.add_argument("--check-schema", action="store_true",
                    help="validate normalized live-plus-archive column types")
    args = ap.parse_args()

    try:
        since = _resolve_ts(args.since)
        until = _resolve_ts(args.until)
    except ValueError as exc:
        preflight.emit_failure("logs", str(exc), code="usage_error")
        return 2

    # Resolve positional `name`: if <name>.log exists in this repo's log
    # directory, point --log at it; otherwise fall through to an --agent
    # substring filter. With --all there is no single file to prefer, so
    # the name always narrows the union as an agent filter (#89).
    try:
        if args.name and args.log is None:
            if args.all:
                if not args.agent:
                    args.agent = args.name
            else:
                candidates = (
                    logs_dir() / f"{args.name}.jsonl",
                    logs_dir() / f"{args.name}.log",
                )
                candidate = next(
                    (path for path in candidates if path.is_file()), None)
                if candidate is not None:
                    args.log = str(candidate)
                elif not args.agent:
                    args.agent = args.name
        if args.log is None:
            args.log = str(default_log())
        patterns = all_log_globs() if args.all else [args.log]
        archives = archive_dir()
    except ValueError as exc:
        preflight.emit_failure("logs", str(exc), code="no_project_root")
        return 2

    con = duckdb.connect(":memory:")
    build_view(con, patterns, archives=archives)

    if args.check_schema:
        violations = check_schema(con)
        if violations:
            preflight.emit_failure(
                "logs", "; ".join(violations), code="schema_error")
            return 2
        print("schema OK")
        return 0

    if args.sql:
        # Hard-fail when filter flags are mixed with --sql. Silently
        # ignoring them produces deterministically wrong counts. Show
        # the equivalent WHERE fragment the user can paste into their
        # SQL.
        filter_map = [
            ("--agent", args.agent, lambda v: f"agent_name LIKE '%{v}%'"),
            ("--since", since, lambda v: f"ts >= '{v}'"),
            ("--until", until, lambda v: f"ts < '{v}'"),
            ("--phase", args.phase, lambda v: f"phase = '{v}'"),
            ("--status", args.status, lambda v: f"status = '{v}'"),
            ("--trigger", args.trigger, lambda v: f"trigger = '{v}'"),
            ("--slow", args.slow, lambda v: f"duration_s > {v}"),
            ("--errors", args.errors,
             lambda v: "(level='error' OR status='error')"),
        ]
        conflicting = [(flag, render(val)) for flag, val, render in filter_map if val]
        if conflicting:
            flags = ", ".join(flag for flag, _ in conflicting)
            where_frag = " AND ".join(frag for _, frag in conflicting)
            preflight.emit_failure(
                "logs",
                f"--sql is exclusive with filter flags ({flags}); move them "
                f"into the SQL WHERE clause, for example: WHERE {where_frag}",
                code="usage_error")
            return 2
        q = args.sql
    else:
        where = []
        if args.agent:   where.append(f"agent_name LIKE '%{args.agent}%'")
        if since:        where.append(f"ts >= '{since}'")
        if until:        where.append(f"ts < '{until}'")
        if args.phase:   where.append(f"phase = '{args.phase}'")
        if args.status:  where.append(f"status = '{args.status}'")
        if args.trigger: where.append(f"trigger = '{args.trigger}'")
        if args.slow:    where.append(f"duration_s > {args.slow}")
        if args.errors:  where.append("(level='error' OR status='error')")
        wsql = "WHERE " + " AND ".join(where) if where else ""
        # When showing errors, inject error_category after status if not already present
        col_list = args.columns.split(",")
        if args.errors and "error_category" not in col_list:
            idx = col_list.index("status") + 1 if "status" in col_list else len(col_list)
            col_list.insert(idx, "error_category")
        cols = ", ".join(col_list)
        lim = f"LIMIT {args.limit}" if args.limit else ""
        q = (f"SELECT {cols} FROM log {wsql} "
             f"ORDER BY {args.order_by} {args.direction} {lim}")

    try:
        rel = con.sql(q)
        if args.format == "jsonl":
            import json
            cols = rel.columns
            for row in rel.fetchall():
                print(json.dumps(dict(zip(cols, row, strict=True)), default=str))
        elif args.format == "csv":
            print(",".join(rel.columns))
            for row in rel.fetchall():
                print(",".join("" if v is None else str(v).replace(",", ";") for v in row))
        else:
            # Widen display so columns aren't hidden or truncated.
            # max_width=500 lets the table exceed terminal width (wraps
            # naturally) when there is a terminal to wrap in.
            show(rel)
    except duckdb.Error as exc:
        preflight.emit_failure(
            "logs", f"{exc}; sql: {q}", code="query_error")
        return 2

    # When --errors is active and format is table, show tracebacks separately
    if args.errors and args.format == "table" and not args.sql:
        try:
            tb_rel = con.sql(
                f"SELECT ts, agent_name, traceback "
                f"FROM log {wsql} AND traceback IS NOT NULL "
                f"ORDER BY {args.order_by} {args.direction} {lim}"
            )
            rows = tb_rel.fetchall()
            if rows:
                print("\n-- Tracebacks --")
                for ts, agent_name, tb in rows:
                    # Show last 20 lines of traceback
                    lines = tb.strip().splitlines()
                    tail = "\n".join(lines[-20:])
                    print(f"\n[{ts}] {agent_name}:")
                    print(tail)
        except duckdb.Error:
            pass  # traceback column may not exist in older logs

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
