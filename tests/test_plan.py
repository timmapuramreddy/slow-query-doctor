from sqd.plan import (
    PlanNode,
    date_wrapped_columns,
    function_calls,
    leading_wildcard_columns,
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
        ("graded_at", "date_trunc()"),
        ("created_on", "EXTRACT()"),
        ("due_at", "::date"),
    ]
