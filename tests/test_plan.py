from sqd.plan import (
    PlanNode,
    date_wrapped_columns,
    function_calls,
    leading_wildcard_columns,
    localize,
    quote_name,
    referenced_columns,
    walk,
)


def test_walk_visits_every_node_top_first():
    plan = {
        "Plan": {
            "Node Type": "Sort",
            "Plans": [
                {"Node Type": "Gather", "Plans": [{"Node Type": "Seq Scan", "Relation Name": "t"}]}
            ],
        }
    }
    assert [(n.node_type, n.depth) for n in walk(plan)] == [
        ("Sort", 0),
        ("Gather", 1),
        ("Seq Scan", 2),
    ]


def test_referenced_columns_ignores_string_literals():
    expr = "((status = 'enrollment_id'::text) AND (class_date > '2024-01-01'::date))"
    assert referenced_columns(expr, ["enrollment_id", "status", "class_date"]) == [
        "status",
        "class_date",
    ]


def test_function_calls_returns_name_and_arguments():
    expr = "(lower((email)::text) = 'Student1@example.edu'::text)"
    assert function_calls(expr) == [("lower", "(email)::text")]


def test_function_calls_handles_nesting():
    assert function_calls("(upper(trim(s.last_name)) = ''::text)") == [
        ("upper", "trim(s.last_name)"),
        ("trim", "s.last_name"),
    ]


def test_parallel_node_time_is_not_multiplied_by_workers():
    raw = {"Actual Total Time": 30.0, "Actual Loops": 3}
    assert PlanNode(raw={**raw, "Parallel Aware": True}, depth=0).total_time_ms == 30.0
    assert PlanNode(raw={**raw, "Parallel Aware": False}, depth=0).total_time_ms == 90.0


def test_leading_wildcard_columns_reads_like_and_ilike():
    expr = "(((email)::text ~~ '%42@x'::text) AND (last_name ~~* 'Sm%'::text))"
    assert leading_wildcard_columns(expr, ["email", "last_name"]) == [("email", "%42@x")]


def test_date_wrapped_columns_finds_functions_extract_and_casts():
    expr = (
        "((date_trunc('day'::text, graded_at) = '2024-03-15'::timestamp with time zone) "
        "AND (EXTRACT(year FROM g.created_on) = '2024'::numeric) AND ((due_at)::date = "
        "'2024-03-15'::date) AND ((email)::text = 'a'::text))"
    )
    cols = ["graded_at", "created_on", "due_at", "email"]
    assert date_wrapped_columns(expr, cols) == [
        ("graded_at", "date_trunc()", "date_trunc('day'::text, graded_at)"),
        ("created_on", "EXTRACT()", "EXTRACT(year FROM g.created_on)"),
        ("due_at", "::date", "(due_at)::date"),
    ]


def test_function_calls_keeps_string_arguments():
    assert function_calls("(date_trunc('day'::text, graded_at) = 'x')") == [
        ("date_trunc", "'day'::text, graded_at")
    ]


def test_referenced_columns_skips_type_and_function_names():
    # A column called "date" must not match the ::date cast or the date() function.
    expr = "(((graded_at)::date = date('2024-03-15'::text)) AND (EXTRACT(year FROM due_at) = '1'))"
    assert referenced_columns(expr, ["date", "year", "graded_at", "due_at"]) == [
        "graded_at",
        "due_at",
    ]


def test_localize_keeps_own_columns_and_hides_other_tables():
    assert localize("(a.enrollment_id = e.id)", {"a", "attendance"}) == "(enrollment_id = $0)"
    assert localize("(enrollment_id = s.id)", {"a", "attendance"}) == "(enrollment_id = $0)"


def test_localize_leaves_literals_and_schema_functions_alone():
    expr = "((email)::text = pg_catalog.lower('s.x@a.edu'::text))"
    assert localize(expr, {"students"}) == expr


def test_own_filter_uses_alias_and_relation_name():
    node = PlanNode(
        raw={"Relation Name": "attendance", "Alias": "a", "Filter": "(a.status = s.status)"},
        depth=0,
    )
    assert node.own_filter == "(status = $0)"


def test_row_counts_multiply_fractional_per_loop_averages():
    # PostgreSQL 18 prints per-loop averages with decimals.
    raw = {"Actual Rows": 0.4, "Rows Removed by Filter": 0.9, "Actual Loops": 20_000}
    node = PlanNode(raw=raw, depth=0)
    assert (node.actual_rows, node.rows_removed_by_filter) == (8_000, 18_000)


def test_localize_reads_quoted_names():
    expr = '(("Order".user_id)::integer = ("User".id)::integer)'
    assert localize(expr, {"Order"}) == "((user_id)::integer = ($0)::integer)"


def test_referenced_columns_matches_quoted_mixed_case_columns():
    assert referenced_columns('(("UserId" = 5) AND (id > 1))', ["UserId", "id"]) == [
        "UserId",
        "id",
    ]


def test_date_wrapped_columns_reads_at_time_zone_cast():
    expr = "(((graded_at AT TIME ZONE 'UTC'::text))::date = '2024-03-15'::date)"
    assert date_wrapped_columns(expr, ["graded_at"]) == [
        ("graded_at", "::date", "((graded_at AT TIME ZONE 'UTC'::text))::date")
    ]


def test_referenced_columns_reads_a_quote_inside_a_quoted_name():
    expr = "((\"it's\" = 'x'::text) AND (id > 1))"
    assert referenced_columns(expr, ["it's", "id"]) == ["it's", "id"]


def test_function_calls_skip_brackets_and_quotes_inside_quoted_names():
    assert function_calls("(lower(\"it's\") = 'a'::text)") == [("lower", '"it\'s"')]
    assert function_calls("(lower(\"a)b\") = 'a'::text)") == [("lower", '"a)b"')]
    assert function_calls("(\"lower(x)\" = 'a'::text)") == []


def test_date_wrapped_columns_reads_quote_and_bracket_inside_quoted_names():
    cols = ["it's day", "a)b day"]
    cases = [
        (
            "((\"it's day\")::date = '2024-01-01'::date)",
            ("it's day", "::date", '("it\'s day")::date'),
        ),
        (
            "(date_trunc('day'::text, \"it's day\") = '2024-01-01 00:00:00'::timestamp)",
            ("it's day", "date_trunc()", "date_trunc('day'::text, \"it's day\")"),
        ),
        (
            "(date_trunc('day'::text, \"a)b day\") = '2024-01-01 00:00:00'::timestamp)",
            ("a)b day", "date_trunc()", "date_trunc('day'::text, \"a)b day\")"),
        ),
        (
            "(EXTRACT(year FROM \"a)b day\") = '1990'::numeric)",
            ("a)b day", "EXTRACT()", 'EXTRACT(year FROM "a)b day")'),
        ),
    ]
    for expr, found in cases:
        assert date_wrapped_columns(expr, cols) == [found], expr


def test_date_wrapped_columns_reads_extract_with_brackets_inside():
    expr = "(EXTRACT(year FROM (due_at + '1 day'::interval)) = '2024'::numeric)"
    assert date_wrapped_columns(expr, ["due_at"]) == [
        ("due_at", "EXTRACT()", "EXTRACT(year FROM (due_at + '1 day'::interval))")
    ]


def test_date_wrapped_columns_skips_a_cast_inside_a_quoted_name():
    assert date_wrapped_columns("(\"x (due_at)::date z\" = 'a'::text)", ["due_at"]) == []


def test_quote_name_matches_postgres_quote_ident():
    assert [quote_name(n) for n in ["name", "order", "UserId", "time", 'a"b']] == [
        "name",
        '"order"',
        '"UserId"',
        '"time"',
        '"a""b"',
    ]
