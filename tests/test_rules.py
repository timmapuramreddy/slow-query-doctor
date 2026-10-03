from sqd.catalog import Catalog
from sqd.rules import run_rules


def _seq_scan(filter_: str, kept: int = 3, removed: int = 999_997) -> dict:
    return {
        "Plan": {
            "Node Type": "Seq Scan",
            "Relation Name": "students",
            "Filter": filter_,
            "Actual Rows": kept,
            "Rows Removed by Filter": removed,
            "Actual Loops": 1,
            "Actual Total Time": 40.0,
        }
    }


def _catalog(rows: int = 1_000_000, indexes: list | None = None) -> Catalog:
    return Catalog.from_dict(
        {
            "students": {
                "rows": rows,
                "columns": ["id", "email", "first_name", "status"],
                "indexes": (
                    indexes
                    if indexes is not None
                    else [
                        {
                            "name": "students_pkey",
                            "columns": ["id"],
                            "definition": "CREATE UNIQUE INDEX students_pkey ON students "
                            "USING btree (id)",
                        },
                        {
                            "name": "students_email_idx",
                            "columns": ["email"],
                            "definition": "CREATE UNIQUE INDEX students_email_idx ON students "
                            "USING btree (email)",
                        },
                    ]
                ),
            }
        }
    )


# Rule 1: missing index


def test_missing_index_fires_on_real_slow_plan(fixture_loader):
    plan, catalog = fixture_loader("missing_index_slow")
    findings = run_rules(plan, catalog)
    assert [f.rule_id for f in findings] == ["missing-index"]
    assert findings[0].suggestion == "CREATE INDEX ON attendance (enrollment_id);"
    assert "Seq Scan on attendance" in findings[0].node


def test_missing_index_silent_after_index_is_added(fixture_loader):
    plan, catalog = fixture_loader("missing_index_after_fix")
    assert run_rules(plan, catalog) == []


def test_missing_index_silent_on_small_table():
    assert run_rules(_seq_scan("(status = 'x'::text)"), _catalog(rows=500)) == []


def test_missing_index_silent_when_filter_keeps_most_rows():
    plan = _seq_scan("(status = 'active'::text)", kept=600_000, removed=400_000)
    assert run_rules(plan, _catalog()) == []


def test_missing_index_silent_when_column_is_indexed():
    assert run_rules(_seq_scan("((email)::text = 'a'::text)"), _catalog()) == []


def test_missing_index_ignores_column_inside_function():
    # An index on first_name would not help lower(first_name); do not suggest one.
    plan = _seq_scan("(lower(first_name) = 'asha'::text)")
    assert [f.rule_id for f in run_rules(plan, _catalog())] == []


# Rule 2: function on indexed column


def test_function_on_column_fires_on_real_slow_plan(fixture_loader):
    plan, catalog = fixture_loader("function_on_column_slow")
    findings = run_rules(plan, catalog)
    assert [f.rule_id for f in findings] == ["function-on-indexed-column"]
    assert findings[0].title == "lower() hides the index on students.email"
    assert "CREATE INDEX ON students (lower(email));" in findings[0].suggestion


def test_function_on_column_silent_for_bare_column_rewrite(fixture_loader):
    plan, catalog = fixture_loader("function_on_column_fixed")
    assert run_rules(plan, catalog) == []


def test_function_on_column_silent_after_expression_index(fixture_loader):
    plan, catalog = fixture_loader("function_on_column_after_fix")
    assert run_rules(plan, catalog) == []


def test_function_on_column_silent_when_expression_index_exists_but_unused():
    indexes = [
        {
            "name": "students_lower_email_idx",
            "columns": [None],
            "definition": "CREATE INDEX students_lower_email_idx ON students "
            "USING btree (lower((email)::text))",
        }
    ]
    plan = _seq_scan("(lower((email)::text) = 'a'::text)")
    assert run_rules(plan, _catalog(indexes=indexes)) == []


def test_function_on_column_leaves_date_functions_to_their_own_rule():
    catalog = Catalog.from_dict(
        {
            "students": {
                "rows": 1_000_000,
                "columns": ["id", "enrolled_on"],
                "indexes": [
                    {"name": "i", "columns": ["enrolled_on"], "definition": "(enrolled_on)"}
                ],
            }
        }
    )
    plan = _seq_scan("(date_trunc('month'::text, (enrolled_on)::timestamp) = '2024-01-01')")
    assert run_rules(plan, catalog) == []


def test_function_on_literal_is_not_flagged():
    assert run_rules(_seq_scan("((email)::text = lower('A@B.EDU'::text))"), _catalog()) == []
