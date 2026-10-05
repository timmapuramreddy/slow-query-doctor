from sqd.catalog import Catalog
from sqd.rules import run_rules


def _seq_scan(filter_: str, kept: int = 3, removed: int = 999_997, alias: str = "students") -> dict:
    return {
        "Plan": {
            "Node Type": "Seq Scan",
            "Relation Name": "students",
            "Alias": alias,
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


def test_missing_index_ignores_outer_column_with_the_same_name():
    # Correlated subquery: o.first_name belongs to the outer query, not to this scan.
    plan = _seq_scan("((email)::text = o.first_name)", alias="s")
    assert run_rules(plan, _catalog()) == []


def test_missing_index_does_not_read_a_cast_as_a_column_named_date():
    catalog = Catalog.from_dict(
        {
            "students": {
                "rows": 600_000,
                "columns": ["id", "graded_at", "date"],
                "indexes": [{"name": "g", "columns": ["graded_at"], "definition": "(graded_at)"}],
            }
        }
    )
    plan = _seq_scan("((graded_at)::date = '2024-03-15'::date)")
    assert [f.rule_id for f in run_rules(plan, catalog)] == ["non-sargable-date-filter"]


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
    assert [f.rule_id for f in run_rules(plan, catalog)] == ["non-sargable-date-filter"]


def test_function_on_literal_is_not_flagged():
    assert run_rules(_seq_scan("((email)::text = lower('A@B.EDU'::text))"), _catalog()) == []


# Rule 3: leading wildcard


def test_leading_wildcard_fires_on_real_slow_plan(fixture_loader):
    plan, catalog = fixture_loader("leading_wildcard_slow")
    findings = run_rules(plan, catalog)
    assert [f.rule_id for f in findings] == ["leading-wildcard"]
    assert findings[0].title == "Leading wildcard '%4242@example.edu' on students.email"
    assert "USING gin (email gin_trgm_ops)" in findings[0].suggestion


def test_leading_wildcard_silent_after_trigram_index(fixture_loader):
    plan, catalog = fixture_loader("leading_wildcard_after_fix")
    assert run_rules(plan, catalog) == []


def test_leading_wildcard_silent_when_seq_scan_already_has_trigram_index():
    indexes = [
        {
            "name": "students_first_name_trgm_idx",
            "columns": ["first_name"],
            "definition": "CREATE INDEX students_first_name_trgm_idx ON students "
            "USING gin (first_name gin_trgm_ops)",
        }
    ]
    plan = _seq_scan("(first_name ~~* '%sha'::text)")
    assert run_rules(plan, _catalog(indexes=indexes)) == []


def test_leading_wildcard_silent_when_column_is_second_in_a_trigram_index():
    indexes = [
        {
            "name": "students_names_trgm_idx",
            "columns": ["email", "first_name"],
            "definition": "CREATE INDEX students_names_trgm_idx ON students "
            "USING gin (email gin_trgm_ops, first_name gin_trgm_ops)",
        }
    ]
    plan = _seq_scan("(first_name ~~* '%sha'::text)")
    assert run_rules(plan, _catalog(indexes=indexes)) == []


def test_leading_wildcard_fires_when_only_another_column_has_trigram_ops():
    indexes = [
        {
            "name": "students_mixed_idx",
            "columns": ["email", "first_name"],
            "definition": "CREATE INDEX students_mixed_idx ON students "
            "USING gist (email gist_trgm_ops, first_name)",
        }
    ]
    plan = _seq_scan("(first_name ~~* '%sha'::text)")
    assert [f.rule_id for f in run_rules(plan, _catalog(indexes=indexes))] == ["leading-wildcard"]


def test_leading_wildcard_on_unindexed_column_does_not_suggest_btree_index():
    findings = run_rules(_seq_scan("(first_name ~~* '_sha%'::text)"), _catalog())
    assert [f.rule_id for f in findings] == ["leading-wildcard"]


def test_trailing_wildcard_is_not_flagged():
    findings = run_rules(_seq_scan("(first_name ~~ 'Ash%'::text)"), _catalog())
    assert [f.rule_id for f in findings] == ["missing-index"]


# Rule 4: deep OFFSET


def _limit(returned: int, produced: int) -> dict:
    return {
        "Plan": {
            "Node Type": "Limit",
            "Actual Rows": returned,
            "Actual Loops": 1,
            "Actual Total Time": 90.0,
            "Plans": [
                {
                    "Node Type": "Sort",
                    "Actual Rows": produced,
                    "Actual Loops": 1,
                    "Actual Total Time": 80.0,
                }
            ],
        }
    }


def test_deep_offset_fires_on_real_slow_plan(fixture_loader):
    plan, catalog = fixture_loader("deep_offset_slow")
    findings = run_rules(plan, catalog)
    assert [f.rule_id for f in findings] == ["deep-offset"]
    assert findings[0].title == "Deep OFFSET: read 500,020 rows to return 20"


def test_deep_offset_silent_for_keyset_rewrite(fixture_loader):
    plan, catalog = fixture_loader("deep_offset_fixed")
    assert run_rules(plan, catalog) == []


def test_deep_offset_silent_for_shallow_offset():
    assert run_rules(_limit(returned=20, produced=1_020), _catalog()) == []


def test_deep_offset_fires_over_a_sort():
    findings = run_rules(_limit(returned=50, produced=200_050), _catalog())
    assert [f.rule_id for f in findings] == ["deep-offset"]
    assert findings[0].node == "Limit over Sort"


# Rule 5: non-sargable date filter


def _date_column_catalog(indexed: bool = True) -> Catalog:
    indexes = [{"name": "g", "columns": ["graded_at"], "definition": "(graded_at)"}]
    return Catalog.from_dict(
        {
            "students": {
                "rows": 600_000,
                "columns": ["id", "graded_at"],
                "indexes": indexes if indexed else [],
            }
        }
    )


def test_date_filter_fires_on_real_slow_plan(fixture_loader):
    plan, catalog = fixture_loader("date_filter_slow")
    findings = run_rules(plan, catalog)
    assert [f.rule_id for f in findings] == ["non-sargable-date-filter"]
    assert findings[0].title.startswith("date_trunc() on grades.graded_at")


def test_date_filter_silent_for_range_rewrite(fixture_loader):
    plan, catalog = fixture_loader("date_filter_fixed")
    assert run_rules(plan, catalog) == []


def test_date_filter_fires_on_cast_to_date():
    plan = _seq_scan("((graded_at)::date = '2024-03-15'::date)")
    findings = run_rules(plan, _date_column_catalog())
    assert [f.title for f in findings] == [
        "::date on students.graded_at turns a date range into a full scan"
    ]


def test_date_filter_fires_on_extract():
    plan = _seq_scan("(EXTRACT(year FROM graded_at) = '2024'::numeric)")
    assert [f.rule_id for f in run_rules(plan, _date_column_catalog())] == [
        "non-sargable-date-filter"
    ]


def test_date_filter_on_unindexed_column_suggests_index_and_skips_missing_index_rule():
    plan = _seq_scan("((graded_at)::date = '2024-03-15'::date)")
    findings = run_rules(plan, _date_column_catalog(indexed=False))
    assert [f.rule_id for f in findings] == ["non-sargable-date-filter"]
    assert findings[0].suggestion.endswith("CREATE INDEX ON students (graded_at);")


def _expression_index_catalog(definition: str) -> Catalog:
    return Catalog.from_dict(
        {
            "students": {
                "rows": 600_000,
                "columns": ["id", "graded_at"],
                "indexes": [
                    {"name": "g", "columns": ["graded_at"], "definition": "(graded_at)"},
                    {"name": "x", "columns": [None], "definition": definition},
                ],
            }
        }
    )


def test_date_filter_silent_when_matching_expression_index_exists():
    catalog = _expression_index_catalog(
        "CREATE INDEX x ON public.students USING btree (EXTRACT(year FROM graded_at))"
    )
    plan = _seq_scan("(EXTRACT(year FROM graded_at) = '2024'::numeric)")
    assert run_rules(plan, catalog) == []


def test_date_filter_fires_when_expression_index_uses_another_precision():
    # An index on date_trunc('month', ...) cannot serve date_trunc('day', ...).
    catalog = _expression_index_catalog(
        "CREATE INDEX x ON public.students USING btree "
        "(date_trunc('month'::text, (graded_at)::timestamp without time zone))"
    )
    plan = _seq_scan(
        "(date_trunc('day'::text, (graded_at)::timestamp without time zone) = "
        "'2024-03-15 00:00:00'::timestamp without time zone)"
    )
    assert [f.rule_id for f in run_rules(plan, catalog)] == ["non-sargable-date-filter"]
