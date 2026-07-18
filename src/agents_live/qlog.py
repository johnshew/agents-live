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

--since/--until accept ISO-8601 or relative forms: 30m, 2h, 1d,
"1 hour ago", "2 days ago".

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
    traceback      VARCHAR  -- printed separately under "── Tracebacks ──" in table mode
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

import re
from datetime import datetime, timedelta, timezone

from paths import resolve_root

REPO = resolve_root()
DEFAULT_LOG = REPO / "Agents/logs/agents-live.log"
ARCHIVE_DIR = REPO / "Agents/logs/archive"
ALL_LOG_GLOBS = [
    str(REPO / "Agents/logs/*.log"),
    str(REPO / "Exercise/data/log/*.log"),
]

NORMALIZED_COLUMN_TYPES = {
    "duration_s": "DOUBLE",
    "cost_usd": "DOUBLE",
    "credits": "DOUBLE",
    "premium_requests": "DOUBLE",
    "log_schema": "INTEGER",
}


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


def build_view(con: duckdb.DuckDBPyConnection, patterns: list[str]) -> None:
    """Create a `log` view over the given files plus Parquet archives.

    Each file is read separately (read_json_auto infers schema per file)
    and the results are unioned by name so schema drift across files
    doesn't collapse rows into a raw JSON column.

    Adds `_src` (filename) for provenance.
    """
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
    if ARCHIVE_DIR.is_dir():
        unified_files = sorted(ARCHIVE_DIR.glob("*.parquet"))
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
    projections: list[str] = []
    for name, dtype, *_ in raw_cols:
        if name == "ts":
            # Expose ts as TIMESTAMP WITH TIME ZONE so time math in --sql
            # works (e.g. `WHERE ts > now() - INTERVAL 1 HOUR`). String
            # comparisons against ISO-8601 literals still implicitly cast.
            # A since-fixed writer emitted "... UTCZ" timestamps
            # (2026-05..07 exercise-judgment rows, live and archived);
            # normalize that legacy suffix so those rows stay queryable.
            projections.append(
                "TRY_CAST(regexp_replace(CAST(\"ts\" AS VARCHAR), "
                "' UTCZ$', 'Z') AS TIMESTAMP WITH TIME ZONE) AS \"ts\""
            )
        elif name in NORMALIZED_COLUMN_TYPES:
            target_type = NORMALIZED_COLUMN_TYPES[name]
            projections.append(
                f'TRY_CAST("{name}" AS {target_type}) AS "{name}"'
            )
        elif dtype == "JSON":
            projections.append(f'CAST("{name}" AS VARCHAR) AS "{name}"')
        else:
            projections.append(f'"{name}"')

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
            "OR log_schema IS NULL OR log_schema <> 5)"
        ).fetchone()[0]
        if invalid_count:
            violations.append(
                f"{invalid_count} JSONL row(s) violate required schema v5 fields"
            )
    return violations


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("name", nargs="?",
                    help="agent name; resolves to Agents/logs/<name>.log if that "
                         "file exists, otherwise used as --agent substring filter")
    ap.add_argument("--log", default=None,
                    help=f"log file or glob (default: {DEFAULT_LOG.relative_to(REPO)})")
    ap.add_argument("--all", action="store_true",
                    help="union all Agents/logs/*.log + Exercise/data/log/*.log")
    ap.add_argument("--agent", help="filter by agent name (substring match)")
    ap.add_argument("--since", help="ts >= this (ISO-8601, UTC)")
    ap.add_argument("--until", help="ts < this (ISO-8601, UTC)")
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
    ap.add_argument("--format", choices=["table", "jsonl", "csv"], default="table")
    ap.add_argument("--check-schema", action="store_true",
                    help="validate normalized live-plus-archive column types")
    args = ap.parse_args()

    # Resolve positional `name`: if Agents/logs/<name>.log exists, point
    # --log at it; otherwise fall through to an --agent substring filter.
    if args.name and args.log is None and not args.all:
        candidate = DEFAULT_LOG.parent / f"{args.name}.log"
        if candidate.is_file():
            args.log = str(candidate)
        else:
            if not args.agent:
                args.agent = args.name
    if args.log is None:
        args.log = str(DEFAULT_LOG)

    con = duckdb.connect(":memory:")
    build_view(con, ALL_LOG_GLOBS if args.all else [args.log])

    # Resolve relative time specs to ISO timestamps. Accepts compact
    # forms (`30m`, `2h`, `1d`) and natural forms (`1 hour ago`,
    # `2 days ago`, `30 min ago`).
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
    def _resolve_ts(val: str | None) -> str | None:
        if val is None:
            return None
        s = val.strip()
        m = _RELATIVE_COMPACT.match(s) or _RELATIVE_WORDS.match(s)
        if m:
            n = int(m.group(1))
            unit = _UNIT_TO_DELTA[m.group(2).lower()]
            delta = {"m": timedelta(minutes=n),
                     "h": timedelta(hours=n),
                     "d": timedelta(days=n)}[unit]
            return (datetime.now(timezone.utc) - delta).strftime("%Y-%m-%dT%H:%M:%SZ")
        return s

    if args.check_schema:
        violations = check_schema(con)
        if violations:
            for violation in violations:
                print(f"schema error: {violation}", file=sys.stderr)
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
            ("--since", args.since, lambda v: f"ts >= '{_resolve_ts(v)}'"),
            ("--until", args.until, lambda v: f"ts < '{_resolve_ts(v)}'"),
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
            print(
                f"error: --sql is exclusive with filter flags ({flags}).\n"
                f"Move them into your SQL WHERE clause, e.g.:\n"
                f"    WHERE {where_frag}",
                file=sys.stderr,
            )
            return 2
        q = args.sql
    else:
        where = []
        if args.agent:   where.append(f"agent_name LIKE '%{args.agent}%'")
        if args.since:   where.append(f"ts >= '{_resolve_ts(args.since)}'")
        if args.until:   where.append(f"ts < '{_resolve_ts(args.until)}'")
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
    except duckdb.Error as e:
        print(f"query error: {e}", file=sys.stderr)
        print(f"  sql: {q}", file=sys.stderr)
        return 2

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
        # max_width=500 lets the table exceed terminal width (wraps naturally).
        # max_col_width=80 keeps individual columns readable.
        rel.show(max_col_width=80, max_width=500)

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
                print("\n── Tracebacks ──")
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
