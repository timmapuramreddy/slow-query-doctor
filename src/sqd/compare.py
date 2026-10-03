"""Time two queries against each other: warm-up, N runs, median / min / max and speedup."""

from __future__ import annotations

import hashlib
import statistics
import time
from collections.abc import Sequence
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


def run_once(conn: psycopg.Connection, query: str) -> tuple[float, list[tuple[Any, ...]]]:
    """Run a checked SELECT in a read-only transaction, roll back, return (ms, rows)."""
    try:
        with conn.cursor() as cur:
            cur.execute("BEGIN READ ONLY")
            start = time.perf_counter()
            cur.execute(query)
            rows = cur.fetchall()
            elapsed_ms = (time.perf_counter() - start) * 1000
    finally:
        conn.rollback()
    return elapsed_ms, rows


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


def _ms(value: float) -> str:
    return f"{value:.1f} ms" if value >= 1 else f"{value:.2f} ms"


def format_comparison(before: Timing, after: Timing, warmup: int) -> str:
    """Plain-text table plus a one-line verdict for the terminal."""
    runs = len(before.runs_ms)
    width = max(len(before.label), len(after.label))
    lines = [
        f"Ran each query {runs} time(s) after {warmup} warm-up run(s), taking turns.",
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
