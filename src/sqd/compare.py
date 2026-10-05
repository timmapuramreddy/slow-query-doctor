"""Time two queries against each other: warm-up, N runs, median / min / max and speedup."""

from __future__ import annotations

import hashlib
import statistics
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

import psycopg

from sqd import db


@dataclass(frozen=True)
class Timing:
    """Wall-clock times of one query (execute + fetch all rows) and a fingerprint of its rows."""

    label: str
    runs_ms: list[float]
    row_count: int
    rows_digest: str

    @property
    def median_ms(self) -> float:
        return statistics.median(self.runs_ms)

    @property
    def min_ms(self) -> float:
        return min(self.runs_ms)

    @property
    def max_ms(self) -> float:
        return max(self.runs_ms)


def rows_digest(rows: Sequence[tuple[Any, ...]]) -> str:
    """Order-independent fingerprint, so two queries can be checked for the same result."""
    text = "\n".join(sorted(repr(row) for row in rows))
    return hashlib.sha256(text.encode()).hexdigest()


# How long --setup may wait for a table lock, so it never queues behind (and blocks) others.
SETUP_LOCK_TIMEOUT = "5s"


def _timed(cur: psycopg.Cursor, query: str) -> tuple[float, list[tuple[Any, ...]]]:
    """Execute and fetch every row, return (ms, rows)."""
    start = time.perf_counter()
    cur.execute(query)
    rows = cur.fetchall()
    return (time.perf_counter() - start) * 1000, rows


def run_once(conn: psycopg.Connection, query: str) -> tuple[float, list[tuple[Any, ...]]]:
    """Run a checked SELECT in a read-only transaction, roll back, return (ms, rows)."""
    try:
        with conn.cursor() as cur:
            cur.execute("BEGIN READ ONLY")
            return _timed(cur, query)
    finally:
        conn.rollback()


def _timing(
    label: str, run: Callable[[], tuple[float, list[tuple[Any, ...]]]], runs: int, warmup: int
) -> Timing:
    """Call run() warmup + runs times in a row, keep the timed runs and the first result."""
    times: list[float] = []
    first: list[tuple[Any, ...]] = []
    for n in range(warmup + runs):
        elapsed_ms, rows = run()
        if n == 0:
            first = rows
        if n >= warmup:
            times.append(elapsed_ms)
    return Timing(label=label, runs_ms=times, row_count=len(first), rows_digest=rows_digest(first))


def compare_queries(
    conn: psycopg.Connection,
    queries: list[tuple[str, str]],
    runs: int = 5,
    warmup: int = 1,
) -> list[Timing]:
    """Time each (label, sql) query `runs` times after `warmup` untimed runs.

    Runs alternate between the queries so cache warm-up or background load hits both alike.
    """
    if runs < 1 or warmup < 0:
        raise ValueError("runs must be at least 1 and warmup at least 0.")
    checked = [db.ensure_select(sql) for _, sql in queries]
    times: list[list[float]] = [[] for _ in checked]
    results: list[list[tuple[Any, ...]]] = [[] for _ in checked]

    for n in range(warmup + runs):
        for i, query in enumerate(checked):
            elapsed_ms, rows = run_once(conn, query)
            if n == 0:
                results[i] = rows
            if n >= warmup:
                times[i].append(elapsed_ms)

    return [
        Timing(
            label=label,
            runs_ms=times[i],
            row_count=len(results[i]),
            rows_digest=rows_digest(results[i]),
        )
        for i, (label, _) in enumerate(queries)
    ]


def compare_with_setup(
    conn: psycopg.Connection,
    before: tuple[str, str],
    after: tuple[str, str],
    setup_sql: str,
    runs: int = 5,
    warmup: int = 1,
) -> tuple[Timing, Timing, float]:
    """Time `before` on the database as it is, then `after` with the setup applied.

    The setup (CREATE INDEX ...) runs in one transaction that is always rolled back, so the
    database ends as it started. The timed queries still run read-only. Runs cannot take
    turns, because the index exists only inside that transaction: all `before` runs come first.
    While it runs, CREATE INDEX blocks writes to the table. Returns (before, after, setup ms).
    """
    if runs < 1 or warmup < 0:
        raise ValueError("runs must be at least 1 and warmup at least 0.")
    before_sql, after_sql = db.ensure_select(before[1]), db.ensure_select(after[1])
    setup = db.ensure_setup(setup_sql)

    before_timing = _timing(before[0], lambda: run_once(conn, before_sql), runs, warmup)
    try:
        with conn.cursor() as cur:
            cur.execute(f"SET LOCAL lock_timeout = '{SETUP_LOCK_TIMEOUT}'")
            start = time.perf_counter()
            for statement in setup:
                cur.execute(statement)
            setup_ms = (time.perf_counter() - start) * 1000
            cur.execute("SET LOCAL transaction_read_only = on")
            after_timing = _timing(after[0], lambda: _timed(cur, after_sql), runs, warmup)
    finally:
        conn.rollback()
    return before_timing, after_timing, setup_ms


def _ms(value: float) -> str:
    return f"{value:.1f} ms" if value >= 1 else f"{value:.2f} ms"


def format_comparison(
    before: Timing, after: Timing, warmup: int, setup: tuple[str, float] | None = None
) -> str:
    """Plain-text table plus a one-line verdict for the terminal.

    `setup` is (setup file label, ms it took) when compare_with_setup produced the timings.
    """
    runs = len(before.runs_ms)
    width = max(len(before.label), len(after.label))
    if setup is None:
        lines = [f"Ran each query {runs} time(s) after {warmup} warm-up run(s), taking turns."]
    else:
        lines = [
            f"Ran each query {runs} time(s) after {warmup} warm-up run(s): "
            f"first without {setup[0]}, then with it.",
            f"{setup[0]} took {setup[1] / 1000:.1f} s to apply (not timed) and was rolled back.",
        ]
    lines += [
        "",
        f"{'':<{width}}  {'median':>10}  {'min':>10}  {'max':>10}  rows",
    ]
    for t in (before, after):
        lines.append(
            f"{t.label:<{width}}  {_ms(t.median_ms):>10}  {_ms(t.min_ms):>10}  "
            f"{_ms(t.max_ms):>10}  {t.row_count:,}"
        )
    lines.append("")
    if after.median_ms > 0 and before.median_ms >= after.median_ms:
        lines.append(
            f"{after.label} is {before.median_ms / after.median_ms:,.1f}x faster "
            f"(median {_ms(before.median_ms)} -> {_ms(after.median_ms)})."
        )
    elif before.median_ms > 0:
        lines.append(
            f"{after.label} is {after.median_ms / before.median_ms:,.1f}x slower "
            f"(median {_ms(before.median_ms)} -> {_ms(after.median_ms)})."
        )
    if before.rows_digest == after.rows_digest:
        lines.append(f"Both queries returned the same {after.row_count:,} row(s).")
    else:
        lines.append(
            "Warning: the queries returned different rows, so this is not a like-for-like fix."
        )
    return "\n".join(lines)
