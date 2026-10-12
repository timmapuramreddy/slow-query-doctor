from dataclasses import replace

import pytest

from sqd.catalog import Catalog
from sqd.rules import run_rules
from sqd.rules.row_estimate import _closes
from sqd.rules.top_n_sort import _btree_keys


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
    plan = _seq_scan("(status = 'x'::text)", kept=3, removed=497)
    assert run_rules(plan, _catalog(rows=500)) == []


def test_rules_use_the_rows_a_scan_read_when_the_table_was_never_analyzed(fixture_loader):
    # reltuples = -1 reaches the catalog as 0 rows. The plan shows the table is big:
    # a parallel scan of 3 loops each read 333,333 rows.
    plan, catalog = fixture_loader("missing_index_slow")
    catalog.tables["attendance"] = replace(catalog.tables["attendance"], rows=0)
    findings = run_rules(plan, catalog)
    assert [f.rule_id for f in findings] == ["missing-index"]
    assert "about 999,999" in findings[0].explanation


def test_join_rule_uses_the_rows_a_scan_read_when_the_table_was_never_analyzed(fixture_loader):
    plan, catalog = fixture_loader("join_key_slow")
    catalog.tables["enrollments"] = replace(catalog.tables["enrollments"], rows=0)
    assert [f.rule_id for f in run_rules(plan, catalog)] == ["missing-index"]


def test_a_small_table_in_a_rerun_parallel_scan_is_still_small():
    # Shape seen on PostgreSQL 17: a Gather on the inner side of a nested loop runs 200
    # times; its parallel Seq Scan shows 600 loops (200 runs x 3 processes) of 167 rows.
    scan = _seq_scan("(status = 'x'::text)", kept=0, removed=167)["Plan"]
    scan.update({"Parallel Aware": True, "Actual Loops": 600})
    gather = {"Node Type": "Gather", "Actual Loops": 200, "Actual Rows": 0, "Plans": [scan]}
    outer = {"Node Type": "Function Scan", "Actual Loops": 1, "Actual Rows": 200}
    plan = {
        "Plan": {
            "Node Type": "Nested Loop",
            "Actual Loops": 1,
            "Actual Rows": 0,
            "Plans": [outer, gather],
        }
    }
    assert run_rules(plan, _catalog(rows=0)) == []


def test_a_small_table_scanned_once_per_outer_row_is_still_small():
    # Inside a nested loop, a 500-row table is read 1,000 times: 500,000 rows in total,
    # but one pass is 500 rows.
    plan = _seq_scan("(status = 'x'::text)", kept=0, removed=500)
    plan["Plan"]["Actual Loops"] = 1000
    assert run_rules(plan, _catalog(rows=0)) == []


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


def test_deep_offset_silent_for_fetch_with_ties(fixture_loader):
    # FETCH FIRST 5 ROWS WITH TIES returned 142,695 tied rows. The Limit kept all but the one
    # row that broke the tie, so nothing was skipped the way OFFSET skips rows.
    plan, catalog = fixture_loader("deep_offset_with_ties")
    assert plan["Plan"]["Actual Rows"] > 100_000
    assert [f.rule_id for f in run_rules(plan, catalog)] == []


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


# Rule 1 on joins: a join key with no index


def _join_catalog(student_id_indexed: bool = False) -> Catalog:
    indexes = (
        [{"name": "e_sid", "columns": ["student_id"], "definition": "(student_id)"}]
        if student_id_indexed
        else []
    )
    return Catalog.from_dict(
        {
            "enrollments": {
                "rows": 300_000,
                "columns": ["id", "student_id", "course_id"],
                "indexes": indexes,
            },
            "courses": {"rows": 500, "columns": ["id", "code"], "indexes": []},
        }
    )


def _hash_join(cond: str, joined: int = 600, other: int = 1) -> dict:
    return {
        "Plan": {
            "Node Type": "Hash Join",
            "Hash Cond": cond,
            "Actual Rows": joined,
            "Actual Loops": 1,
            "Actual Total Time": 30.0,
            "Plans": [
                {
                    "Node Type": "Seq Scan",
                    "Relation Name": "enrollments",
                    "Alias": "e",
                    "Actual Rows": 300_000,
                    "Actual Loops": 1,
                    "Actual Total Time": 25.0,
                },
                {
                    "Node Type": "Hash",
                    "Actual Rows": other,
                    "Actual Loops": 1,
                    "Plans": [
                        {
                            "Node Type": "Seq Scan",
                            "Relation Name": "courses",
                            "Alias": "c",
                            "Actual Rows": other,
                            "Actual Loops": 1,
                            "Actual Total Time": 0.1,
                        }
                    ],
                },
            ],
        }
    }


def test_join_key_fires_on_real_slow_plan(fixture_loader):
    plan, catalog = fixture_loader("join_key_slow")
    findings = run_rules(plan, catalog)
    assert [f.rule_id for f in findings] == ["missing-index"]
    assert findings[0].title == "No index on enrollments.course_id (join key)"
    assert findings[0].suggestion == "CREATE INDEX ON enrollments (course_id);"


def test_join_key_silent_after_index_is_added(fixture_loader):
    plan, catalog = fixture_loader("join_key_after_fix")
    assert run_rules(plan, catalog) == []


def test_join_key_silent_when_join_keeps_most_rows():
    assert run_rules(_hash_join("(e.course_id = c.id)", joined=250_000), _join_catalog()) == []


def test_join_key_silent_when_other_side_is_large():
    plan = _hash_join("(e.course_id = c.id)", other=100_000)
    assert run_rules(plan, _join_catalog()) == []


def test_join_key_silent_when_key_is_indexed():
    plan = _hash_join("(e.student_id = c.id)")
    assert run_rules(plan, _join_catalog(student_id_indexed=True)) == []


def test_join_key_reads_only_this_tables_side_of_the_condition():
    # c.student_id is the other table's column; enrollments.student_id is indexed.
    plan = _hash_join("(e.student_id = c.student_id)")
    assert run_rules(plan, _join_catalog(student_id_indexed=True)) == []


def test_join_key_fires_for_nested_loop_join_filter_over_materialize():
    plan = {
        "Plan": {
            "Node Type": "Nested Loop",
            "Join Filter": "(e.course_id = c.id)",
            "Actual Rows": 600,
            "Rows Removed by Join Filter": 299_400,
            "Actual Loops": 1,
            "Actual Total Time": 80.0,
            "Plans": [
                {
                    "Node Type": "Seq Scan",
                    "Relation Name": "courses",
                    "Alias": "c",
                    "Actual Rows": 1,
                    "Actual Loops": 1,
                },
                {
                    "Node Type": "Materialize",
                    "Actual Rows": 300_000,
                    "Actual Loops": 1,
                    "Plans": [
                        {
                            "Node Type": "Seq Scan",
                            "Relation Name": "enrollments",
                            "Alias": "e",
                            "Actual Rows": 300_000,
                            "Actual Loops": 1,
                            "Actual Total Time": 25.0,
                        }
                    ],
                },
            ],
        }
    }
    findings = run_rules(plan, _join_catalog())
    assert [f.title for f in findings] == ["No index on enrollments.course_id (join key)"]


# Rule 6: the index finds rows, the filter throws most away


def _bitmap_scan(filter_: str, kept: int, removed: int, index: str = "g_idx") -> dict:
    return {
        "Plan": {
            "Node Type": "Bitmap Heap Scan",
            "Relation Name": "grades",
            "Alias": "grades",
            "Filter": filter_,
            "Actual Rows": kept,
            "Rows Removed by Filter": removed,
            "Actual Loops": 1,
            "Actual Total Time": 30.0,
            "Plans": [
                {
                    "Node Type": "Bitmap Index Scan",
                    "Index Name": index,
                    "Actual Rows": kept + removed,
                    "Actual Loops": 1,
                }
            ],
        }
    }


def _grades_catalog() -> Catalog:
    return Catalog.from_dict(
        {
            "grades": {
                "rows": 600_000,
                "columns": ["id", "graded_at", "assessment", "score"],
                "indexes": [
                    {"name": "g_idx", "columns": ["graded_at"], "definition": "(graded_at)"}
                ],
            }
        }
    )


def test_filter_after_index_fires_on_real_slow_plan(fixture_loader):
    plan, catalog = fixture_loader("filter_after_index_slow")
    findings = run_rules(plan, catalog)
    assert [f.rule_id for f in findings] == ["filter-after-index"]
    assert findings[0].title == "grades_graded_at_idx found 26,347 rows, the filter kept 888"
    assert findings[0].suggestion.endswith("CREATE INDEX ON grades (assessment, graded_at, score);")


def test_filter_after_index_silent_after_combined_index(fixture_loader):
    plan, catalog = fixture_loader("filter_after_index_after_fix")
    assert run_rules(plan, catalog) == []


def test_filter_after_index_silent_when_filter_removes_few_rows():
    plan = _bitmap_scan("(assessment = 'final'::text)", kept=10, removed=900)
    assert run_rules(plan, _grades_catalog()) == []


def test_filter_after_index_silent_when_filter_keeps_most_rows():
    plan = _bitmap_scan("(score < '99'::numeric)", kept=50_000, removed=20_000)
    assert run_rules(plan, _grades_catalog()) == []


def test_filter_after_index_puts_range_column_after_index_columns():
    plan = _bitmap_scan("(score < '50'::numeric)", kept=500, removed=25_000)
    findings = run_rules(plan, _grades_catalog())
    assert findings[0].suggestion.endswith("CREATE INDEX ON grades (graded_at, score);")


def test_filter_after_index_ignores_equals_inside_a_string():
    plan = _bitmap_scan(
        "((score > '10'::numeric) AND (assessment = 'score = 5'::text))", kept=500, removed=25_000
    )
    findings = run_rules(plan, _grades_catalog())
    assert findings[0].suggestion.endswith("CREATE INDEX ON grades (assessment, graded_at, score);")


def test_filter_after_index_treats_equals_any_on_array_cast_as_equality():
    plan = _bitmap_scan(
        "((score < '50'::numeric) AND ((assessment)::text[] = ANY ('{a,b}'::text[])))",
        kept=500,
        removed=25_000,
    )
    findings = run_rules(plan, _grades_catalog())
    assert findings[0].suggestion.endswith("CREATE INDEX ON grades (assessment, graded_at, score);")


def test_date_filter_fires_on_cast_after_at_time_zone(fixture_loader):
    plan, catalog = fixture_loader("date_filter_at_time_zone")
    findings = run_rules(plan, catalog)
    assert [f.title for f in findings] == [
        "::date on grades.graded_at turns a date range into a full scan"
    ]
    # The range keeps the zone, or the day would start at the session's midnight instead.
    assert (
        "graded_at >= '2024-03-15 00:00 UTC' AND graded_at < '2024-03-16 00:00 UTC'"
        in findings[0].suggestion
    )


def test_date_filter_silent_when_at_time_zone_expression_is_indexed():
    catalog = _expression_index_catalog(
        "CREATE INDEX x ON public.students USING btree "
        "((((graded_at AT TIME ZONE 'UTC'::text))::date))"
    )
    plan = _seq_scan("(((graded_at AT TIME ZONE 'UTC'::text))::date = '2024-03-15'::date)")
    assert run_rules(plan, catalog) == []


# Names that need quotes in SQL: mixed case or a keyword


def _orders_scan(filter_: str) -> dict:
    return {
        "Plan": {
            "Node Type": "Seq Scan",
            "Relation Name": "Orders",
            "Alias": "Orders",
            "Filter": filter_,
            "Actual Rows": 3,
            "Rows Removed by Filter": 999_997,
            "Actual Loops": 1,
            "Actual Total Time": 40.0,
        }
    }


def _orders_catalog() -> Catalog:
    return Catalog.from_dict(
        {
            "Orders": {
                "rows": 1_000_000,
                "columns": ["id", "UserId", "order", "Email", "PlacedAt"],
                "indexes": [{"name": "o_pkey", "columns": ["id"], "definition": "(id)"}],
            }
        }
    )


def test_missing_index_quotes_mixed_case_names():
    findings = run_rules(_orders_scan('("UserId" = 42)'), _orders_catalog())
    assert [f.suggestion for f in findings] == ['CREATE INDEX ON "Orders" ("UserId");']


def test_missing_index_quotes_keyword_column():
    findings = run_rules(_orders_scan('("order" = 7)'), _orders_catalog())
    assert [f.suggestion for f in findings] == ['CREATE INDEX ON "Orders" ("order");']


def test_leading_wildcard_reads_and_quotes_mixed_case_column():
    findings = run_rules(_orders_scan("((\"Email\")::text ~~ '%@x.edu'::text)"), _orders_catalog())
    assert [f.rule_id for f in findings] == ["leading-wildcard"]
    assert 'CREATE INDEX ON "Orders" USING gin ("Email" gin_trgm_ops);' in findings[0].suggestion


def test_date_filter_reads_and_quotes_mixed_case_column():
    findings = run_rules(
        _orders_scan("((\"PlacedAt\")::date = '2024-03-15'::date)"), _orders_catalog()
    )
    assert [f.rule_id for f in findings] == ["non-sargable-date-filter"]
    assert "\"PlacedAt\" >= '2024-03-15'" in findings[0].suggestion
    assert findings[0].suggestion.endswith('CREATE INDEX ON "Orders" ("PlacedAt");')


# Rule 8: subquery run once per row


def _subplan(loops: int, rows: float, name: str = "SubPlan 1") -> dict:
    """A scan of students whose filter runs a subplan reading `rows` rows per run."""
    return {
        "Node Type": "Materialize",
        "Parent Relationship": "SubPlan",
        "Subplan Name": name,
        "Actual Rows": rows,
        "Actual Loops": loops,
        "Actual Total Time": 3.0,
    }


def _with_subplans(filter_: str, *subplans: dict) -> dict:
    return {
        "Plan": {
            "Node Type": "Seq Scan",
            "Relation Name": "students",
            "Alias": "s",
            "Filter": filter_,
            "Actual Rows": 1,
            "Actual Loops": 1,
            "Actual Total Time": 700.0,
            "Plans": list(subplans),
        }
    }


def test_subquery_per_row_fires_on_real_slow_plan(fixture_loader):
    plan, catalog = fixture_loader("subquery_per_row_slow")
    findings = run_rules(plan, catalog)
    assert [f.rule_id for f in findings] == ["subquery-per-row"]
    assert findings[0].title == "NOT IN subquery ran 200 times"
    assert findings[0].suggestion.startswith("Rewrite NOT IN (SELECT x FROM ...) as NOT EXISTS")


def test_subquery_per_row_silent_for_not_exists_rewrite(fixture_loader):
    plan, catalog = fixture_loader("subquery_per_row_fixed")
    assert run_rules(plan, catalog) == []


def test_subquery_per_row_silent_for_a_cheap_lookup_per_row():
    # 8,274 runs of an index lookup returning 6 rows: 17 ms, faster than a JOIN rewrite.
    plan = _with_subplans("(status = 'x'::text)", _subplan(loops=8_274, rows=6))
    assert run_rules(plan, _catalog(rows=500)) == []


def test_subquery_per_row_silent_for_a_hashed_subplan():
    plan = _with_subplans(
        "(NOT (ANY (id = (hashed SubPlan 1).col1)))", _subplan(loops=1, rows=300_000)
    )
    assert run_rules(plan, _catalog(rows=500)) == []


def test_subquery_per_row_suggests_a_join_for_a_correlated_subquery():
    plan = _with_subplans("((SubPlan 1) > 3)", _subplan(loops=500, rows=10_000))
    findings = run_rules(plan, _catalog(rows=500))
    assert [f.title for f in findings] == ["Subquery ran 500 times"]
    assert findings[0].suggestion.startswith("Rewrite the subquery as a JOIN")


def test_subquery_per_row_reads_the_postgres_16_not_in_filter():
    plan = _with_subplans("(NOT (SubPlan 1))", _subplan(loops=500, rows=10_000))
    findings = run_rules(plan, _catalog(rows=500))
    assert [f.title for f in findings] == ["NOT IN subquery ran 500 times"]


def test_subquery_per_row_matches_not_in_to_its_own_subplan():
    # SubPlan 1 is a plain IN, SubPlan 2 the NOT IN; only SubPlan 2 gets the NOT EXISTS fix.
    plan = _with_subplans(
        "((NOT (ANY (id = (SubPlan 2).col1))) AND (ANY (email = (SubPlan 1).col1)))",
        _subplan(loops=500, rows=10_000, name="SubPlan 1"),
        _subplan(loops=500, rows=10_000, name="SubPlan 2"),
    )
    titles = sorted(f.title for f in run_rules(plan, _catalog(rows=500)))
    assert titles == ["NOT IN subquery ran 500 times", "Subquery ran 500 times"]
    by_title = {f.title: f.node for f in run_rules(plan, _catalog(rows=500))}
    assert by_title["NOT IN subquery ran 500 times"].startswith("SubPlan 2 ")


# Rule 9: ORDER BY ... LIMIT sorts the whole table


def _top_n(filter_: str | None, keys: list[str], kept: int = 150_000, returned: int = 20) -> dict:
    """Limit > Gather Merge > Sort > parallel Seq Scan of students, as in the real plan."""
    scan = {
        "Node Type": "Seq Scan",
        "Parallel Aware": True,
        "Relation Name": "students",
        "Alias": "s",
        "Actual Rows": kept / 3,
        "Rows Removed by Filter": (1_000_000 - kept) / 3,
        "Actual Loops": 3,
        "Actual Total Time": 40.0,
    }
    if filter_:
        scan["Filter"] = filter_
    sort = {"Node Type": "Sort", "Sort Key": keys, "Actual Rows": returned, "Actual Loops": 3}
    sort["Plans"] = [scan]
    gather = {"Node Type": "Gather Merge", "Actual Rows": returned, "Actual Loops": 1}
    gather["Plans"] = [sort]
    return {
        "Plan": {
            "Node Type": "Limit",
            "Actual Rows": returned,
            "Actual Loops": 1,
            "Actual Total Time": 50.0,
            "Plans": [gather],
        }
    }


def _suggested_index(plan: dict, catalog: Catalog | None = None) -> list[str]:
    findings = run_rules(plan, catalog or _catalog())
    return [f.suggestion.split(": ", 1)[1] for f in findings if f.rule_id == "top-n-sort"]


def test_top_n_sort_fires_on_real_slow_plan(fixture_loader):
    plan, catalog = fixture_loader("top_n_sort_slow")
    findings = run_rules(plan, catalog)
    assert [f.rule_id for f in findings] == ["top-n-sort"]
    assert findings[0].title == "Sorted 142,695 rows of attendance to return 20"
    assert findings[0].suggestion.endswith("CREATE INDEX ON attendance (status, class_date, id);")


def test_top_n_sort_silent_after_index_is_added(fixture_loader):
    plan, catalog = fixture_loader("top_n_sort_after_fix")
    assert run_rules(plan, catalog) == []


def test_top_n_sort_without_filter_indexes_the_sort_key():
    plan = _top_n(None, ["s.first_name DESC"], kept=1_000_000)
    assert _suggested_index(plan) == ["CREATE INDEX ON students (first_name);"]


def test_top_n_sort_keeps_mixed_orders():
    plan = _top_n("(status = 'x'::text)", ["first_name", "id DESC"])
    assert _suggested_index(plan) == ["CREATE INDEX ON students (status, first_name, id DESC);"]


def test_top_n_sort_skips_equality_columns_under_or():
    plan = _top_n("((status = 'x'::text) OR (email = 'y'::text))", ["first_name"])
    assert _suggested_index(plan) == ["CREATE INDEX ON students (first_name);"]


def test_top_n_sort_does_not_put_an_any_list_first():
    plan = _top_n("(status = ANY ('{x,y}'::text[]))", ["first_name"])
    assert _suggested_index(plan) == ["CREATE INDEX ON students (first_name);"]


def test_top_n_sort_silent_for_an_expression_sort_key():
    assert _suggested_index(_top_n(None, ["(lower(s.first_name))"], kept=1_000_000)) == []


def test_top_n_sort_silent_when_limit_keeps_many_rows():
    assert _suggested_index(_top_n(None, ["first_name"], kept=100_000, returned=50_000)) == []


def test_top_n_sort_silent_for_a_small_sort():
    assert _suggested_index(_top_n("(status = 'x'::text)", ["first_name"], kept=5_000)) == []


def test_top_n_sort_silent_when_the_index_exists():
    catalog = _catalog(indexes=[_index("status, first_name", ["status", "first_name"])])
    assert _suggested_index(_top_n("(status = 'x'::text)", ["first_name"]), catalog) == []


def test_top_n_sort_leaves_a_selective_filter_to_the_missing_index_rule():
    plan = _top_n("(status = 'x'::text)", ["first_name"], kept=30_000)
    assert [f.rule_id for f in run_rules(plan, _catalog())] == ["missing-index"]


def _index(definition_columns: str, columns: list[str]) -> dict:
    definition = f"CREATE INDEX ix ON public.students USING btree ({definition_columns})"
    return {"name": "ix", "columns": columns, "definition": definition}


def test_top_n_sort_silent_when_the_index_can_be_read_backwards():
    catalog = _catalog(indexes=[_index("status, first_name", ["status", "first_name"])])
    plan = _top_n("(status = 'x'::text)", ["first_name DESC"])
    assert _suggested_index(plan, catalog) == []


def test_top_n_sort_fires_when_the_index_has_another_nulls_order():
    # Read backwards, (first_name) gives DESC NULLS FIRST, not ASC NULLS FIRST.
    catalog = _catalog(indexes=[_index("first_name", ["first_name"])])
    plan = _top_n(None, ["first_name NULLS FIRST"], kept=1_000_000)
    assert _suggested_index(plan, catalog) == ["CREATE INDEX ON students (first_name NULLS FIRST);"]


def test_top_n_sort_fires_when_the_index_has_another_direction():
    catalog = _catalog(indexes=[_index("status, first_name DESC", ["status", "first_name"])])
    plan = _top_n("(status = 'x'::text)", ["first_name", "id"])
    assert _suggested_index(plan, catalog) == ["CREATE INDEX ON students (status, first_name, id);"]
    plan = _top_n("(status = 'x'::text)", ["first_name"])
    catalog = _catalog(
        indexes=[_index("status, first_name DESC NULLS LAST", ["status", "first_name"])]
    )
    assert _suggested_index(plan, catalog) == ["CREATE INDEX ON students (status, first_name);"]


def test_top_n_sort_reads_an_equality_on_a_cast_with_a_length():
    plan = _top_n("((status)::character varying(20) = 'x'::text)", ["first_name"])
    assert _suggested_index(plan) == ["CREATE INDEX ON students (status, first_name);"]


def test_top_n_sort_does_not_read_a_cast_type_as_an_equality_column():
    # "::status =" casts email; the status column itself is only compared with >.
    plan = _top_n("(((email)::status = 'x'::status) AND (status > 'a'::text))", ["first_name"])
    assert _suggested_index(plan) == ["CREATE INDEX ON students (email, first_name);"]


def test_top_n_sort_looks_past_an_initplan_of_the_limit():
    plan = _top_n(None, ["first_name"], kept=1_000_000)
    initplan = {"Node Type": "Result", "Parent Relationship": "InitPlan", "Actual Rows": 1}
    plan["Plan"]["Plans"].insert(0, initplan)
    assert _suggested_index(plan) == ["CREATE INDEX ON students (first_name);"]


@pytest.mark.parametrize(
    "definition",
    [
        "CREATE INDEX ix ON public.students USING btree (status) INCLUDE (first_name)",
        "CREATE INDEX ix ON public.students USING gin (status, first_name)",
        "CREATE INDEX ix ON public.students USING btree (status, first_name text_pattern_ops)",
        'CREATE INDEX ix ON public.students USING btree (status, first_name COLLATE "C")',
        'CREATE INDEX ix ON public.students USING btree (status COLLATE "C", first_name)',
        "CREATE INDEX ix ON public.students USING btree (status, first_name) " "WHERE (id > 5)",
    ],
)
def test_top_n_sort_fires_when_the_index_cannot_give_the_order(definition):
    index = {"name": "ix", "columns": ["status", "first_name"], "definition": definition}
    plan = _top_n("(status = 'x'::text)", ["first_name"])
    assert _suggested_index(plan, _catalog(indexes=[index])) == [
        "CREATE INDEX ON students (status, first_name);"
    ]


def test_top_n_sort_silent_when_the_index_has_extra_include_columns():
    definition = "status, first_name) INCLUDE (id"
    catalog = _catalog(indexes=[_index(definition, ["status", "first_name", "id"])])
    assert _suggested_index(_top_n("(status = 'x'::text)", ["first_name"]), catalog) == []


def test_btree_keys_reads_quoted_names_as_names():
    definition = 'CREATE INDEX ix ON t USING btree ("desc", "a, b)" DESC, "x""y")'
    assert _btree_keys(definition) == [
        ("desc", (False, False)),
        ("a, b)", (True, True)),
        ('x"y', (False, False)),
    ]


def test_btree_keys_does_not_read_a_quoted_name_as_the_using_clause():
    definition = 'CREATE INDEX " USING btree (a, b)" ON t USING gin (c)'
    assert _btree_keys(definition) is None


# Rule 10: a far-too-low row estimate made a nested loop run many times


def _scan(relation: str, filter_: str | None, estimate: float | None, rows: float) -> dict:
    node = {
        "Node Type": "Seq Scan",
        "Parent Relationship": "Outer",
        "Relation Name": relation,
        "Alias": relation,
        "Actual Rows": rows,
        "Actual Loops": 1,
        "Actual Total Time": 100.0,
    }
    if filter_ is not None:
        node["Filter"] = filter_
    if estimate is not None:
        node["Plan Rows"] = estimate
    return node


def _lookup(loops: int) -> dict:
    return {
        "Node Type": "Index Scan",
        "Parent Relationship": "Inner",
        "Relation Name": "enrollments",
        "Alias": "e",
        "Index Name": "enrollments_pkey",
        "Plan Rows": 1,
        "Actual Rows": 1,
        "Actual Loops": loops,
        "Actual Total Time": 0.003,
    }


def _nested_loop(outer: dict, inner: dict) -> dict:
    return {
        "Plan": {
            "Node Type": "Nested Loop",
            "Plan Rows": outer.get("Plan Rows", 1),
            "Actual Rows": outer["Actual Rows"],
            "Actual Loops": 1,
            "Actual Total Time": 500.0,
            "Plans": [outer, inner],
        }
    }


def _attendance_catalog(table: str = "attendance") -> Catalog:
    return Catalog.from_dict(
        {table: {"rows": 1_000_000, "columns": ["id", "status", "class_date"], "indexes": []}}
    )


_WRAPPED = (
    "((lower(status) = 'present'::text) AND (EXTRACT(year FROM class_date) = '2024'::numeric))"
)


def _row_estimate(plan: dict, catalog: Catalog) -> list:
    return [f for f in run_rules(plan, catalog) if f.rule_id == "bad-row-estimate"]


def test_row_estimate_fires_once_on_real_slow_plan(fixture_loader):
    # Two stacked nested loops share one bad guess on attendance: one finding, on the lower one.
    plan, catalog = fixture_loader("row_estimate_slow")
    findings = run_rules(plan, catalog)
    assert [f.rule_id for f in findings] == ["bad-row-estimate"]
    assert findings[0].title.startswith("PostgreSQL expected 30 rows from attendance and got about")
    assert "ran 372,907 times" in findings[0].node
    assert findings[0].suggestion.endswith(
        "CREATE STATISTICS attendance_status_class_date_stats ON (lower(status)), "
        "(EXTRACT(year FROM class_date)) FROM attendance; ANALYZE attendance;"
    )


@pytest.mark.parametrize("name", ["row_estimate_after_fix", "row_estimate_fixed"])
def test_row_estimate_silent_after_either_fix(fixture_loader, name):
    plan, catalog = fixture_loader(name)
    assert run_rules(plan, catalog) == []


def test_row_estimate_fix_in_the_example_matches_the_suggestion(fixture_loader):
    from pathlib import Path

    plan, catalog = fixture_loader("row_estimate_slow")
    fix = Path(__file__).parent.parent / "examples" / "10-bad-row-estimate" / "fix.sql"
    statements = " ".join(
        line for line in fix.read_text().splitlines() if not line.startswith("--")
    )
    assert " ".join(statements.split()) in run_rules(plan, catalog)[0].suggestion


@pytest.mark.parametrize(
    "estimate, rows, loops",
    [
        (10_000, 99_000, 99_000),  # off by 9.9x: close enough
        (10, 49_999, 49_999),  # off by 5,000x, but too few runs to cost much
        (None, 100_000, 100_000),  # COSTS OFF text plan: no estimate to compare
    ],
)
def test_row_estimate_silent_below_thresholds(estimate, rows, loops):
    plan = _nested_loop(_scan("attendance", _WRAPPED, estimate, rows), _lookup(loops))
    assert _row_estimate(plan, _attendance_catalog()) == []


def test_row_estimate_counts_cache_misses_under_memoize():
    def memoize(misses: int) -> dict:
        lookup = {**_lookup(misses), "Parent Relationship": "Outer"}
        return {
            "Node Type": "Memoize",
            "Parent Relationship": "Inner",
            "Actual Rows": 1,
            "Actual Loops": 100_000,
            "Actual Total Time": 0.001,
            "Plans": [lookup],
        }

    outer = _scan("attendance", _WRAPPED, 10, 100_000)
    assert _row_estimate(_nested_loop(outer, memoize(500)), _attendance_catalog()) == []
    [finding] = _row_estimate(_nested_loop(outer, memoize(60_000)), _attendance_catalog())
    assert finding.node == "Nested Loop (Index Scan ran 60,000 times)"
    assert "runs Index Scan on enrollments e once for each row not already in its cache" in (
        finding.explanation
    )


def test_row_estimate_suggests_statistics_on_related_plain_columns():
    filter_ = "((status = 'late'::text) AND (class_date = '2024-03-04'::date))"
    outer = _scan("attendance", filter_, 1, 60_000)
    [finding] = _row_estimate(_nested_loop(outer, _lookup(60_000)), _attendance_catalog())
    assert "If status, class_date depend on each other" in finding.suggestion
    assert finding.suggestion.endswith(
        "CREATE STATISTICS attendance_status_class_date_stats ON status, class_date "
        "FROM attendance; ANALYZE attendance;"
    )


@pytest.mark.parametrize(
    "filter_",
    [
        "(status = 'late'::text)",  # one plain column: no multi-column statistics to add
        None,
        "(lower(status) = (SubPlan 1))",  # a subplan cannot go into CREATE STATISTICS
        "(lower(status) = lower(e.status))",  # nor another table's column
    ],
)
def test_row_estimate_falls_back_to_analyze(filter_):
    outer = _scan("attendance", filter_, 1, 60_000)
    [finding] = _row_estimate(_nested_loop(outer, _lookup(60_000)), _attendance_catalog())
    assert finding.suggestion.startswith("Run ANALYZE attendance; so PostgreSQL has current")


def test_row_estimate_quotes_mixed_case_names():
    outer = _scan("Visits", "(lower(\"Status\") = 'x'::text)", 1, 60_000)
    catalog = Catalog.from_dict({"Visits": {"rows": 1_000_000, "columns": ["Status"]}})
    [finding] = _row_estimate(_nested_loop(outer, _lookup(60_000)), catalog)
    assert finding.suggestion.endswith(
        'CREATE STATISTICS "Visits_Status_stats" ON (lower("Status")) FROM "Visits"; '
        'ANALYZE "Visits";'
    )


def test_row_estimate_without_catalog_gives_general_advice():
    plan = _nested_loop(_scan("attendance", _WRAPPED, 10, 100_000), _lookup(100_000))
    [finding] = _row_estimate(plan, Catalog({}))
    assert finding.suggestion.startswith("Run ANALYZE attendance; to refresh its statistics.")
    assert finding.suggestion.endswith("Add --catalog for the exact SQL.")


def test_row_estimate_names_a_join_as_the_source():
    # The scans below were guessed well; the join between them was not.
    join = {
        "Node Type": "Hash Join",
        "Parent Relationship": "Outer",
        "Plan Rows": 5,
        "Actual Rows": 80_000,
        "Actual Loops": 1,
        "Actual Total Time": 90.0,
        "Plans": [
            _scan("attendance", None, 1_000_000, 1_000_000),
            {**_scan("students", None, 50_000, 50_000), "Parent Relationship": "Inner"},
        ],
    }
    [finding] = _row_estimate(_nested_loop(join, _lookup(80_000)), _attendance_catalog())
    assert finding.title == "PostgreSQL expected 5 rows from Hash Join and got about 80,000"
    assert finding.suggestion.startswith("The guess went wrong at Hash Join")


@pytest.mark.parametrize(
    "filter_",
    [
        "(app.clean(status) = 'x'::text)",  # the helpers drop the schema name
        "(EXTRACT(year FROM (class_date + '1 day'::interval)) = '2024'::numeric)",  # cut short
        "(replace(status, \"x)y\", ''::text) = ''::text)",  # cut at the ")" in "x)y"
        # A ' inside the quoted name "a'b" must not hide the open quote of "x)y.
        '(concat(status, "a\'b", \'q\'::text, "x)y") = status)',
    ],
)
def test_row_estimate_gives_no_exact_sql_for_an_expression_it_cannot_copy_whole(filter_):
    outer = _scan("attendance", filter_, 1, 60_000)
    catalog = Catalog.from_dict(
        {"attendance": {"rows": 1_000_000, "columns": ["status", "class_date", "x)y", "a'b"]}}
    )
    [finding] = _row_estimate(_nested_loop(outer, _lookup(60_000)), catalog)
    assert finding.suggestion.endswith(
        "CREATE STATISTICS on up to 8 of the expressions in the filter, then ANALYZE attendance;"
    )


@pytest.mark.parametrize(
    "expression, whole",
    [
        ("lower(status)", True),
        ('lower("Status")', True),
        ("replace(status, '(', ''::text)", True),  # brackets inside a string do not count
        ('lower("a""b)c")', True),  # doubled quote inside a quoted name
        ("EXTRACT(year FROM (class_date + '1 day'::interval)", False),
        ('replace(status, "x)', False),
        ("lower(status))", False),
    ],
)
def test_row_estimate_closes_reads_strings_and_quoted_names(expression, whole):
    assert _closes(expression) is whole


def test_row_estimate_ignores_function_like_text_inside_a_quoted_name():
    outer = _scan("attendance", "(\"a nonexistent(status) z\" = 'x'::text)", 1, 60_000)
    catalog = Catalog.from_dict(
        {"attendance": {"rows": 1_000_000, "columns": ["status", "a nonexistent(status) z"]}}
    )
    [finding] = _row_estimate(_nested_loop(outer, _lookup(60_000)), catalog)
    assert finding.suggestion.startswith("Run ANALYZE attendance; so PostgreSQL has current")


def test_row_estimate_gives_no_exact_sql_for_more_than_eight_columns():
    columns = [f"c{n}" for n in range(9)]
    filter_ = "(" + " AND ".join(f"({c} = 1)" for c in columns) + ")"
    catalog = Catalog.from_dict({"attendance": {"rows": 1_000_000, "columns": columns}})
    [finding] = _row_estimate(
        _nested_loop(_scan("attendance", filter_, 1, 60_000), _lookup(60_000)), catalog
    )
    assert finding.suggestion.endswith(
        "CREATE STATISTICS on 8 or fewer of those columns, then ANALYZE attendance;"
    )


def test_row_estimate_gives_no_exact_sql_for_a_column_used_through_an_operator():
    # Statistics on the json column payload itself would fail: json has no ordering.
    filter_ = "(((payload ->> 'status'::text) = 'active'::text) AND (tenant_id = 42))"
    catalog = Catalog.from_dict(
        {"attendance": {"rows": 1_000_000, "columns": ["payload", "tenant_id"]}}
    )
    [finding] = _row_estimate(
        _nested_loop(_scan("attendance", filter_, 1, 60_000), _lookup(60_000)), catalog
    )
    assert "CREATE STATISTICS attendance" not in finding.suggestion


def test_row_estimate_reads_a_cast_column_as_compared_directly():
    filter_ = "(((status)::text = 'late'::text) AND (class_date >= '2024-01-01'::date))"
    [finding] = _row_estimate(
        _nested_loop(_scan("attendance", filter_, 1, 60_000), _lookup(60_000)),
        _attendance_catalog(),
    )
    assert finding.suggestion.endswith(
        "CREATE STATISTICS attendance_status_class_date_stats ON status, class_date "
        "FROM attendance; ANALYZE attendance;"
    )
