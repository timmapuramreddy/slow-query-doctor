"""Database access: connect, load the demo data, run EXPLAIN safely, read index info."""

from __future__ import annotations

import os
import re
from importlib import resources
from typing import Any

import psycopg

from sqd.catalog import Catalog

DSN_ENV = "SQD_DATABASE_URL"

_WRITE_WORDS = re.compile(r"\b(insert|update|delete|merge|truncate|alter|drop|create)\b", re.I)
_DOLLAR_TAG = re.compile(r"\$(?:[a-z_][a-z0-9_]*)?\$", re.I)


class UnsafeQueryError(ValueError):
    """Raised when a query is not a single read-only SELECT."""


def get_dsn(dsn: str | None = None) -> str:
    """Return the DSN passed in, or the one from SQD_DATABASE_URL."""
    value = dsn or os.environ.get(DSN_ENV)
    if not value:
        raise RuntimeError(f"Set {DSN_ENV} (see .env.example) or pass --dsn.")
    return value


def _quoted_end(sql: str, start: int, quote: str, backslash: bool) -> int:
    """Index just past the quote that closes the one at `start` ('' or "" inside is escaped)."""
    pos = start + 1
    while pos < len(sql):
        # Skip a backslash escape (E'...' only) or a doubled quote.
        if (backslash and sql[pos] == "\\") or sql.startswith(quote * 2, pos):
            pos += 2
        elif sql[pos] == quote:
            return pos + 1
        else:
            pos += 1
    raise UnsafeQueryError(f"Unclosed {quote} quote.")


def mask_sql(sql: str) -> str:
    """Same-length copy of the SQL with comments blanked and the inside of quotes blanked.

    Checks and statement splitting read this copy, so '--', '/*', ';' or a keyword inside a
    string, a quoted name or a $$ block is ignored, while positions still match the original.
    """
    out = list(sql)
    pos = 0
    while pos < len(sql):
        char = sql[pos]
        if sql.startswith("--", pos):
            end = sql.find("\n", pos)
            end = len(sql) if end < 0 else end
            out[pos:end] = " " * (end - pos)
        elif sql.startswith("/*", pos):
            depth, end = 1, pos + 2  # PostgreSQL block comments nest.
            while end < len(sql) and depth:
                if sql.startswith("/*", end):
                    depth, end = depth + 1, end + 2
                elif sql.startswith("*/", end):
                    depth, end = depth - 1, end + 2
                else:
                    end += 1
            if depth:
                raise UnsafeQueryError("Unclosed /* comment.")
            out[pos:end] = " " * (end - pos)
        elif char in "'\"":
            prev = sql[pos - 2 : pos]
            escape = char == "'" and prev[-1:] in ("e", "E") and not prev[:-1].isalnum()
            end = _quoted_end(sql, pos, char, escape)
            out[pos + 1 : end - 1] = " " * (end - pos - 2)
        elif (
            char == "$"
            and (tag := _DOLLAR_TAG.match(sql, pos))
            and not (pos and (sql[pos - 1].isalnum() or sql[pos - 1] == "_"))
        ):
            close = sql.find(tag.group(), tag.end())
            if close < 0:
                raise UnsafeQueryError("Unclosed $$ string.")
            out[tag.end() : close] = " " * (close - tag.end())
            end = close + len(tag.group())
        else:
            end = pos + 1
        pos = end
    return "".join(out)


def _trim(sql: str, masked: str, start: int, end: int) -> tuple[str, str]:
    """(original, masked) text of sql[start:end] without the blank or comment edges."""
    while start < end and masked[start].isspace():
        start += 1
    while end > start and masked[end - 1].isspace():
        end -= 1
    return sql[start:end], masked[start:end]


def ensure_select(sql: str) -> str:
    """Return the query without a trailing semicolon, or raise if it is not one SELECT.

    EXPLAIN ANALYZE executes the query, so v1 only accepts SELECT / WITH ... SELECT.
    The read-only transaction in explain_analyze() is the second guard.
    """
    masked = mask_sql(sql)
    query, check = _trim(sql, masked, 0, len(sql))
    if check.endswith(";"):
        query, check = _trim(query, check, 0, len(check) - 1)
    if not check:
        raise UnsafeQueryError("The file has no SQL in it.")
    if ";" in check:
        raise UnsafeQueryError("Only one statement per file is supported.")
    first = check.split(None, 1)[0].lower()
    if first not in ("select", "with"):
        raise UnsafeQueryError(f"Only SELECT queries are allowed in v1, got {first.upper()}.")
    if _WRITE_WORDS.search(check):
        raise UnsafeQueryError("The query contains a write statement; only reads are allowed.")
    return query


_SETUP_STATEMENT = re.compile(r"(create\s+(unique\s+)?index|create\s+extension|analyze)\b", re.I)
_CONCURRENTLY = re.compile(r"\bconcurrently\b", re.I)


def ensure_setup(sql: str) -> list[str]:
    """Split a `compare --setup` file into statements, or raise if one is not allowed.

    Only CREATE INDEX, CREATE EXTENSION and ANALYZE: what an index fix needs. They run in
    the comparison's transaction, which is always rolled back.
    """
    masked = mask_sql(sql)
    statements: list[str] = []
    start = 0
    for end in [i for i, char in enumerate(masked) if char == ";"] + [len(masked)]:
        statement, check = _trim(sql, masked, start, end)
        start = end + 1
        if not check:
            continue
        if not _SETUP_STATEMENT.match(check):
            first = check.split(None, 1)[0].upper()
            raise UnsafeQueryError(
                f"--setup only allows CREATE INDEX, CREATE EXTENSION and ANALYZE, got {first}."
            )
        if _CONCURRENTLY.search(check):
            raise UnsafeQueryError(
                "CREATE INDEX CONCURRENTLY cannot run in a transaction; drop CONCURRENTLY."
            )
        statements.append(statement)
    if not statements:
        raise UnsafeQueryError("The --setup file has no SQL in it.")
    return statements


def explain_analyze(conn: psycopg.Connection, sql: str) -> dict[str, Any]:
    """Run EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) in a read-only transaction and roll back."""
    query = ensure_select(sql)
    try:
        with conn.cursor() as cur:
            cur.execute("BEGIN READ ONLY")
            cur.execute(f"EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) {query}")
            row = cur.fetchone()
    finally:
        conn.rollback()
    if row is None:
        raise RuntimeError("EXPLAIN returned no rows.")
    return row[0][0]


# One JSON object in the Catalog.from_dict shape: {table: {rows, columns, indexes}}.
# Names are plan "Relation Name" values (unquoted), so quote_ident keeps their case.
# A null index column is an expression. Read-only: it only reads pg_catalog.
_CATALOG_SQL = """
SELECT coalesce(json_object_agg(c.relname, json_build_object(
    'rows', greatest(c.reltuples, 0)::bigint,
    'columns', array(SELECT a.attname::text FROM pg_attribute a
                     WHERE a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped
                     ORDER BY a.attnum),
    'indexes', coalesce((
        SELECT json_agg(json_build_object(
            'name', ic.relname,
            'columns', array(SELECT a.attname::text
                             FROM unnest(i.indkey::int2[]) WITH ORDINALITY AS k(attnum, ord)
                             LEFT JOIN pg_attribute a
                               ON a.attrelid = c.oid AND a.attnum = k.attnum
                             ORDER BY k.ord),
            'definition', pg_get_indexdef(i.indexrelid)) ORDER BY ic.relname)
        FROM pg_index i JOIN pg_class ic ON ic.oid = i.indexrelid
        WHERE i.indrelid = c.oid), '[]'::json))), '{}'::json)
FROM unnest(%s::text[]) AS t(name)
JOIN pg_class c ON c.oid = to_regclass(quote_ident(t.name))
"""


def load_catalog(conn: psycopg.Connection, table_names: set[str]) -> Catalog:
    """Read row estimates, columns and indexes for the given tables."""
    with conn.cursor() as cur:
        cur.execute(_CATALOG_SQL, (sorted(table_names),))
        row = cur.fetchone()
    conn.rollback()
    return Catalog.from_dict(row[0] if row else {})


def catalog_sql(table_names: set[str]) -> str:
    """The catalog query with the table names filled in, for people to run on their own DB."""
    names = ", ".join("'" + name.replace("'", "''") + "'" for name in sorted(table_names))
    return (
        "-- Slow Query Doctor catalog query. Read-only: it reads row estimates, column names\n"
        "-- and index definitions from pg_catalog for the tables in your plan.\n"
        "-- Run it with: psql -XqAt -f catalog.sql -o catalog.json"
        + _CATALOG_SQL.replace("%s", f"ARRAY[{names}]")
    )


DEMO_ROWS_PER_SCALE = 1_950_000  # students, enrollments, attendance, grades
DEMO_FIXED_ROWS = 500  # courses


def load_demo(conn: psycopg.Connection, scale: int = 1) -> None:
    """Create the school schema and fill it with synthetic data. Drops existing demo tables.

    scale multiplies every table but courses; scale 1 is the data the tests expect.
    """
    sql_dir = resources.files("sqd") / "sql"
    with conn.cursor() as cur:
        cur.execute("SELECT set_config('sqd.scale', %s, false)", (str(scale),))
        for name in ("schema.sql", "seed.sql"):
            cur.execute((sql_dir / name).read_text())
    conn.commit()
