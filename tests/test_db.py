import os
from pathlib import Path

import pytest

from sqd import db

EXAMPLES = Path(__file__).parent.parent / "examples"

# Example folder -> rule ids that `sqd check` should report for its slow.sql.
EXPECTED = {
    "01-missing-index": ["missing-index"],
    "02-function-on-indexed-column": ["function-on-indexed-column"],
    "03-leading-wildcard": ["leading-wildcard"],
    "04-deep-offset": ["deep-offset"],
    "05-non-sargable-date": ["non-sargable-date-filter"],
    "06-join-missing-index": ["missing-index"],
    "07-filter-after-index": ["filter-after-index"],
}


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1",
        "select * from students;",
        "-- comment\nWITH x AS (SELECT 1) SELECT * FROM x",
        "SELECT 'delete; drop' AS note",
    ],
)
def test_ensure_select_accepts_reads(sql):
    assert db.ensure_select(sql)


@pytest.mark.parametrize(
    "sql",
    [
        "",
        "-- only a comment",
        "DELETE FROM students",
        "UPDATE students SET status = 'x'",
        "SELECT 1; DROP TABLE students",
        "WITH gone AS (DELETE FROM students RETURNING *) SELECT * FROM gone",
    ],
)
def test_ensure_select_rejects_anything_else(sql):
    with pytest.raises(db.UnsafeQueryError):
        db.ensure_select(sql)


@pytest.mark.parametrize(
    "sql, expected",
    [
        ("SELECT 'a--b' AS x;", "SELECT 'a--b' AS x"),
        ("SELECT '/*' AS a, '*/' AS b", "SELECT '/*' AS a, '*/' AS b"),
        ("SELECT $$;$$ AS x -- trailing", "SELECT $$;$$ AS x"),
        ("SELECT E'it\\'s; fine' AS x", "SELECT E'it\\'s; fine' AS x"),
        ('SELECT "delete" FROM t /* drop */', 'SELECT "delete" FROM t'),
    ],
)
def test_ensure_select_keeps_comment_marks_inside_strings(sql, expected):
    assert db.ensure_select(sql) == expected


def test_ensure_setup_keeps_strings_whole_and_splits_only_real_semicolons():
    sql = (
        "CREATE INDEX a ON t (x) WHERE note = 'foo--';\n"
        "ANALYZE t;\n"
        "CREATE INDEX b ON t ((position($s$;$s$ in body)));"
    )
    assert db.ensure_setup(sql) == [
        "CREATE INDEX a ON t (x) WHERE note = 'foo--'",
        "ANALYZE t",
        "CREATE INDEX b ON t ((position($s$;$s$ in body)))",
    ]


@pytest.mark.parametrize(
    "sql, count",
    [
        ("CREATE INDEX ON attendance (enrollment_id);", 1),
        (
            "-- trigram\nCREATE EXTENSION IF NOT EXISTS pg_trgm;\n"
            "CREATE INDEX s_idx ON students USING gin (email gin_trgm_ops);",
            2,
        ),
        ("create unique index on t (lower(x)); analyze t", 2),
        ("CREATE INDEX ON t (x) WHERE note <> 'a;b'", 1),
    ],
)
def test_ensure_setup_accepts_index_extension_and_analyze(sql, count):
    assert len(db.ensure_setup(sql)) == count


@pytest.mark.parametrize(
    "sql",
    [
        "",
        "-- only a comment",
        "DROP INDEX attendance_pkey",
        "CREATE TABLE t (x int)",
        "CREATE INDEX CONCURRENTLY ON t (x)",
        "CREATE INDEX ON t (x); DELETE FROM t",
        "SELECT 1",
    ],
)
def test_ensure_setup_rejects_anything_else(sql):
    with pytest.raises(db.UnsafeQueryError):
        db.ensure_setup(sql)


@pytest.mark.integration
@pytest.mark.skipif(not os.environ.get(db.DSN_ENV), reason="needs SQD_DATABASE_URL")
def test_check_finds_each_rule_on_its_demo_example():
    import psycopg

    from sqd.plan import walk
    from sqd.rules import run_rules

    found = {}
    with psycopg.connect(db.get_dsn()) as conn:
        for name in EXPECTED:
            plan = db.explain_analyze(conn, (EXAMPLES / name / "slow.sql").read_text())
            catalog = db.load_catalog(conn, {n.relation for n in walk(plan) if n.relation})
            found[name] = [f.rule_id for f in run_rules(plan, catalog)]
    assert found == EXPECTED
