import os
from pathlib import Path

import pytest

from sqd import db
from sqd.compare import (
    Timing,
    compare_queries,
    compare_with_setup,
    format_comparison,
    rows_digest,
)

EXAMPLES = Path(__file__).parent.parent / "examples"


def _timing(label: str, runs_ms: list[float], rows: list[tuple] | None = None) -> Timing:
    rows = rows if rows is not None else [(1, "a")]
    return Timing(label=label, runs_ms=runs_ms, row_count=len(rows), rows_digest=rows_digest(rows))


def test_timing_reports_median_min_max():
    t = _timing("slow.sql", [30.0, 10.0, 50.0, 20.0, 40.0])
    assert (t.median_ms, t.min_ms, t.max_ms) == (30.0, 10.0, 50.0)


def test_rows_digest_ignores_row_order():
    assert rows_digest([(1, "a"), (2, "b")]) == rows_digest([(2, "b"), (1, "a")])
    assert rows_digest([(1, "a")]) != rows_digest([(1, "b")])


def test_format_comparison_reports_speedup_from_medians():
    text = format_comparison(
        _timing("slow.sql", [90.0, 100.0, 400.0]), _timing("fixed.sql", [1.0, 2.0, 3.0]), warmup=1
    )
    assert "fixed.sql is 50.0x faster (median 100.0 ms -> 2.0 ms)." in text
    assert "Both queries returned the same 1 row(s)." in text


def test_format_comparison_warns_when_rows_differ():
    text = format_comparison(
        _timing("slow.sql", [10.0], rows=[(1,)]), _timing("fixed.sql", [1.0], rows=[(2,)]), 1
    )
    assert "returned different rows" in text


def test_format_comparison_says_slower_when_the_fix_is_slower():
    text = format_comparison(_timing("a.sql", [1.0]), _timing("b.sql", [4.0]), warmup=0)
    assert "b.sql is 4.0x slower" in text


def test_compare_refuses_non_select_before_touching_the_database():
    with pytest.raises(db.UnsafeQueryError):
        compare_queries(None, [("a", "SELECT 1"), ("b", "DELETE FROM students")])  # type: ignore[arg-type]


def test_format_comparison_with_setup_says_runs_did_not_take_turns():
    text = format_comparison(
        _timing("slow.sql", [40.0]),
        _timing("slow.sql + fix.sql", [0.2]),
        warmup=1,
        setup=("fix.sql", 1234.0),
    )
    assert "first without fix.sql, then with it" in text
    assert "fix.sql took 1.2 s to apply (not timed) and was rolled back." in text


def test_compare_with_setup_refuses_unsafe_setup_before_touching_the_database():
    with pytest.raises(db.UnsafeQueryError):
        compare_with_setup(None, ("a", "SELECT 1"), ("a", "SELECT 1"), "DROP TABLE students")  # type: ignore[arg-type]


@pytest.mark.integration
@pytest.mark.skipif(not os.environ.get(db.DSN_ENV), reason="needs SQD_DATABASE_URL")
def test_compare_times_both_examples_and_matches_rows():
    import psycopg

    folder = EXAMPLES / "04-deep-offset"
    queries = [(name, (folder / name).read_text()) for name in ("slow.sql", "fixed.sql")]
    with psycopg.connect(db.get_dsn()) as conn:
        before, after = compare_queries(conn, queries, runs=2, warmup=1)
    assert len(before.runs_ms) == len(after.runs_ms) == 2
    assert before.row_count == after.row_count == 20
    assert before.rows_digest == after.rows_digest


@pytest.mark.integration
@pytest.mark.skipif(not os.environ.get(db.DSN_ENV), reason="needs SQD_DATABASE_URL")
def test_compare_with_setup_times_the_index_fix_and_rolls_it_back():
    import psycopg

    folder = EXAMPLES / "01-missing-index"
    slow = ("slow.sql", (folder / "slow.sql").read_text())
    with psycopg.connect(db.get_dsn()) as conn:
        before, after, setup_ms = compare_with_setup(
            conn, slow, slow, (folder / "fix.sql").read_text(), runs=2, warmup=1
        )
        catalog = db.load_catalog(conn, {"attendance"})
    assert after.median_ms < before.median_ms
    assert before.rows_digest == after.rows_digest
    assert setup_ms > 0
    assert not catalog.tables["attendance"].has_leading_index("enrollment_id")
