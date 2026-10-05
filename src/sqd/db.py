"""Database access: connect, load the demo data, run EXPLAIN safely, read index info."""

from __future__ import annotations

import os
import re
from importlib import resources
from typing import Any

import psycopg

from sqd.catalog import Catalog, Index, Table

DSN_ENV = "SQD_DATABASE_URL"

_LINE_COMMENT = re.compile(r"--[^\n]*")
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
_STRING = re.compile(r"'(?:[^']|'')*'")
_WRITE_WORDS = re.compile(r"\b(insert|update|delete|merge|truncate|alter|drop|create)\b", re.I)


class UnsafeQueryError(ValueError):
    """Raised when a query is not a single read-only SELECT."""


def get_dsn(dsn: str | None = None) -> str:
    """Return the DSN passed in, or the one from SQD_DATABASE_URL."""
    value = dsn or os.environ.get(DSN_ENV)
    if not value:
        raise RuntimeError(f"Set {DSN_ENV} (see .env.example) or pass --dsn.")
    return value


def ensure_select(sql: str) -> str:
    """Return the query without a trailing semicolon, or raise if it is not one SELECT.

    EXPLAIN ANALYZE executes the query, so v1 only accepts SELECT / WITH ... SELECT.
    The read-only transaction in explain_analyze() is the second guard.
    """
    stripped = _BLOCK_COMMENT.sub(" ", _LINE_COMMENT.sub(" ", sql)).strip().rstrip(";").strip()
    no_strings = _STRING.sub("''", stripped)
    if not stripped:
        raise UnsafeQueryError("The file has no SQL in it.")
    if ";" in no_strings:
        raise UnsafeQueryError("Only one statement per file is supported.")
    first = no_strings.split(None, 1)[0].lower()
    if first not in ("select", "with"):
        raise UnsafeQueryError(f"Only SELECT queries are allowed in v1, got {first.upper()}.")
    if _WRITE_WORDS.search(no_strings):
        raise UnsafeQueryError("The query contains a write statement; only reads are allowed.")
    return stripped


_SETUP_STATEMENT = re.compile(r"(create\s+(unique\s+)?index|create\s+extension|analyze)\b", re.I)
_CONCURRENTLY = re.compile(r"\bconcurrently\b", re.I)


def ensure_setup(sql: str) -> list[str]:
    """Split a `compare --setup` file into statements, or raise if one is not allowed.

    Only CREATE INDEX, CREATE EXTENSION and ANALYZE: what an index fix needs. They run in
    the comparison's transaction, which is always rolled back.
    """
    text = _BLOCK_COMMENT.sub(" ", _LINE_COMMENT.sub(" ", sql))
    # Blank out strings (same length) so a ';' inside one does not split the statement.
    masked = _STRING.sub(lambda m: "'" + " " * (len(m.group()) - 2) + "'", text)
    statements: list[str] = []
    start = 0
    for end in [i for i, char in enumerate(masked) if char == ";"] + [len(masked)]:
        statement = text[start:end].strip()
        start = end + 1
        if not statement:
            continue
        if not _SETUP_STATEMENT.match(statement):
            first = statement.split(None, 1)[0].upper()
            raise UnsafeQueryError(
                f"--setup only allows CREATE INDEX, CREATE EXTENSION and ANALYZE, got {first}."
            )
        if _CONCURRENTLY.search(_STRING.sub("''", statement)):
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


_CATALOG_SQL = """
SELECT c.relname,
       greatest(c.reltuples, 0)::bigint,
       array(SELECT a.attname::text FROM pg_attribute a
             WHERE a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped
             ORDER BY a.attnum),
       coalesce(json_agg(json_build_object(
           'name', ic.relname,
           'columns', array(SELECT coalesce(a.attname::text, '')
                            FROM unnest(i.indkey::int2[]) WITH ORDINALITY AS k(attnum, ord)
                            LEFT JOIN pg_attribute a
                              ON a.attrelid = c.oid AND a.attnum = k.attnum
                            ORDER BY k.ord),
           'definition', pg_get_indexdef(i.indexrelid)))
         FILTER (WHERE i.indexrelid IS NOT NULL), '[]'::json)
FROM pg_class c
LEFT JOIN pg_index i ON i.indrelid = c.oid
LEFT JOIN pg_class ic ON ic.oid = i.indexrelid
WHERE c.oid = to_regclass(%s)
GROUP BY c.oid, c.relname, c.reltuples
"""


def load_catalog(conn: psycopg.Connection, table_names: set[str]) -> Catalog:
    """Read row estimates, columns and indexes for the given tables."""
    tables: dict[str, Table] = {}
    with conn.cursor() as cur:
        for name in sorted(table_names):
            cur.execute(_CATALOG_SQL, (name,))
            row = cur.fetchone()
            if row is None:
                continue
            relname, rows, columns, indexes = row
            tables[relname] = Table(
                name=relname,
                rows=rows,
                columns=list(columns),
                # An empty column name means that index position is an expression.
                indexes=[
                    Index(
                        name=ix["name"],
                        columns=[c or None for c in ix["columns"]],
                        definition=ix["definition"],
                    )
                    for ix in indexes
                ],
            )
    conn.rollback()
    return Catalog(tables=tables)


def load_demo(conn: psycopg.Connection) -> None:
    """Create the school schema and fill it with synthetic data. Drops existing demo tables."""
    sql_dir = resources.files("sqd") / "sql"
    with conn.cursor() as cur:
        for name in ("schema.sql", "seed.sql"):
            cur.execute((sql_dir / name).read_text())
    conn.commit()
