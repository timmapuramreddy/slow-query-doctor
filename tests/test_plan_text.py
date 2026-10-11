import json
from pathlib import Path

import pytest

from sqd import cli
from sqd.catalog import Catalog
from sqd.plan import parse_explain, walk
from sqd.rules import run_rules

FIXTURES = Path(__file__).parent / "fixtures"
TEXT = FIXTURES / "text"
SLOW_FIXTURES = sorted(p.stem for p in FIXTURES.glob("*_slow.json"))
# The keys the rules and the report read, compared node for node with the JSON plan.
COMPARED_KEYS = (
    "Node Type", "Relation Name", "Alias", "Index Name", "Filter", "Hash Cond", "Merge Cond",
    "Join Filter", "Index Cond", "Sort Key", "Parent Relationship", "Parallel Aware",
    "Actual Rows", "Actual Loops", "Rows Removed by Filter",
)  # fmt: skip

SUBPLAN = """\
Bitmap Heap Scan on courses c (actual time=30.422..45.768 rows=2 loops=1)
  Recheck Cond: (id < 3)
  Heap Blocks: exact=1
  Buffers: shared hit=4415
  ->  Bitmap Index Scan on courses_pkey (actual time=0.004..0.005 rows=2 loops=1)
        Index Cond: (id < 3)
  SubPlan 1
    ->  Aggregate (actual time=22.861..22.861 rows=1 loops=2)
          ->  Seq Scan on enrollments e (actual time=0.079..22.768 rows=608 loops=2)
                Filter: (course_id = c.id)
                Rows Removed by Filter: 299392
Planning Time: 1.774 ms
Execution Time: 45.953 ms
"""

INITPLAN_PG17 = """\
Bitmap Heap Scan on courses (actual time=0.103..0.104 rows=0 loops=1)
  Recheck Cond: (id < 3)
  Filter: (credits = (InitPlan 1).col1)
  InitPlan 1
    ->  Aggregate (actual time=0.095..0.095 rows=1 loops=1)
          ->  Seq Scan on courses courses_1 (actual time=0.004..0.059 rows=500 loops=1)
  ->  Bitmap Index Scan on courses_pkey (actual time=0.003..0.003 rows=2 loops=1)
        Index Cond: (id < 3)
Execution Time: 0.131 ms
"""
INITPLAN_PG16 = INITPLAN_PG17.replace("(InitPlan 1).col1", "$0").replace(
    "  InitPlan 1\n", "  InitPlan 1 (returns $0)\n"
)


def _text(name: str) -> str:
    return (TEXT / f"{name}.txt").read_text()


def _json_plan(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text())["plan"]


def _nodes(text: str) -> list[dict]:
    return [n.raw for n in walk(parse_explain(text))]


@pytest.mark.parametrize("name", SLOW_FIXTURES)
def test_text_plan_reads_like_the_json_plan_of_the_same_query(name):
    text_nodes = list(walk(parse_explain(_text(name))))
    json_nodes = list(walk(_json_plan(name)))
    assert [n.depth for n in text_nodes] == [n.depth for n in json_nodes]
    for text_node, json_node in zip(text_nodes, json_nodes, strict=True):
        for key in COMPARED_KEYS:
            assert text_node.raw.get(key) == json_node.raw.get(key), (name, key)


@pytest.mark.parametrize("name", SLOW_FIXTURES)
def test_text_plan_gets_the_same_findings_as_the_json_plan(name):
    catalog = Catalog.from_dict(json.loads((FIXTURES / f"{name}.json").read_text())["catalog"])
    from_text = run_rules(parse_explain(_text(name)), catalog)
    from_json = run_rules(_json_plan(name), catalog)
    assert from_json
    assert [(f.rule_id, f.suggestion) for f in from_text] == [
        (f.rule_id, f.suggestion) for f in from_json
    ]


def test_check_reads_a_text_plan_file(tmp_path, capsys, no_db):
    data = json.loads((FIXTURES / "join_key_slow.json").read_text())
    (tmp_path / "catalog.json").write_text(json.dumps(data["catalog"]))
    args = ["--plan", str(TEXT / "join_key_slow.txt"), "--catalog", str(tmp_path / "catalog.json")]
    assert cli.main(["check", *args]) == 0
    out, err = capsys.readouterr()
    plan = parse_explain(_text("join_key_slow"))
    assert out.strip() == cli.format_findings(
        run_rules(plan, Catalog.from_dict(data["catalog"])), plan["Execution Time"]
    )
    assert "Found" in out
    assert err == ""


def test_catalog_sql_reads_a_text_plan_file(capsys, no_db):
    assert cli.main(["catalog-sql", str(TEXT / "join_key_slow.txt")]) == 0
    assert "ARRAY[$n0$courses$n0$, $n0$enrollments$n0$]::text[]" in capsys.readouterr().out


def test_psql_aligned_output_reads_like_plain_output():
    aligned = _text("join_key_slow_aligned")
    rows = aligned.rstrip().splitlines()
    assert rows[0].strip() == "QUERY PLAN" and rows[-1].startswith("(")
    plain = "\n".join(row[1:] for row in rows[2:-1])
    assert parse_explain(aligned) == parse_explain(plain)
    assert parse_explain(aligned)["Execution Time"] == 22.84


def test_timing_off_and_costs_off_give_counts_without_times_or_costs():
    root = parse_explain("Seq Scan on t (actual rows=3.00 loops=1)\n  Filter: (a > 1)")["Plan"]
    assert root == {
        "Node Type": "Seq Scan",
        "Relation Name": "t",
        "Alias": "t",
        "Parallel Aware": False,
        "Async Capable": False,
        "Actual Rows": 3.0,
        "Actual Loops": 1,
        "Filter": "(a > 1)",
    }


def test_costs_are_read_when_printed():
    line = "Limit  (cost=0.42..1.50 rows=20 width=19) (actual time=0.1..0.2 rows=20 loops=1)"
    root = parse_explain(line)["Plan"]
    assert (root["Startup Cost"], root["Total Cost"], root["Plan Rows"], root["Plan Width"]) == (
        0.42,
        1.5,
        20,
        19,
    )
    assert (root["Actual Startup Time"], root["Actual Total Time"]) == (0.1, 0.2)


def test_never_executed_node_has_zero_loops_and_times_like_json():
    nodes = _nodes(
        "Nested Loop (actual time=0.010..0.011 rows=0 loops=1)\n"
        "  ->  Seq Scan on courses c (actual time=0.009..0.009 rows=0 loops=1)\n"
        "        Filter: (id < 0)\n"
        "  ->  Index Scan using enrollments_pkey on enrollments e (never executed)\n"
        "        Index Cond: (id = c.id)\n"
    )
    never = nodes[2]
    assert never["Index Cond"] == "(id = c.id)"
    assert [never[k] for k in ("Actual Rows", "Actual Loops", "Actual Total Time")] == [0, 0, 0]


@pytest.mark.parametrize(
    "label, fields",
    [
        (
            'Seq Scan on public."UserAccounts" ua',
            {"Schema": "public", "Relation Name": "UserAccounts", "Alias": "ua"},
        ),
        (
            'Index Scan Backward using "Idx ""x""" on "My.Table"',
            {
                "Index Name": 'Idx "x"',
                "Relation Name": "My.Table",
                "Alias": "My.Table",
                "Scan Direction": "Backward",
            },
        ),
        ("Bitmap Index Scan on courses_pkey", {"Index Name": "courses_pkey"}),
        ("Parallel Index Only Scan using i on t", {"Parallel Aware": True, "Index Name": "i"}),
        ("Hash Anti Join", {"Node Type": "Hash Join", "Join Type": "Anti"}),
        ("Merge Right Semi Join", {"Node Type": "Merge Join", "Join Type": "Right Semi"}),
        ("Nested Loop", {"Node Type": "Nested Loop", "Join Type": "Inner"}),
        ("Nested Loop Left Join", {"Node Type": "Nested Loop", "Join Type": "Left"}),
        (
            "Finalize HashAggregate",
            {"Node Type": "Aggregate", "Strategy": "Hashed", "Partial Mode": "Finalize"},
        ),
        ("HashSetOp Except All", {"Node Type": "SetOp", "Strategy": "Hashed"}),
        ('Values Scan on "*VALUES*"', {"Node Type": "Values Scan", "Alias": "*VALUES*"}),
        ("CTE Scan on x a", {"CTE Name": "x", "Alias": "a"}),
        ("Function Scan on generate_series g", {"Function Name": "generate_series"}),
        ("Custom Scan (ChunkAppend) on m", {"Custom Plan Provider": "ChunkAppend"}),
        ("Async Foreign Scan on f", {"Async Capable": True, "Relation Name": "f"}),
        ("Update on t", {"Node Type": "ModifyTable", "Operation": "Update"}),
    ],
)
def test_node_labels_give_the_json_node_fields(label, fields):
    root = parse_explain(f"{label}  (actual rows=1 loops=1)")["Plan"]
    assert {k: root.get(k) for k in fields} == fields


def test_subplan_children_get_the_parent_relationship_json_gives():
    root, bitmap, aggregate, scan = _nodes(SUBPLAN)
    assert bitmap["Parent Relationship"] == "Outer"
    assert bitmap["Index Name"] == "courses_pkey"
    assert (aggregate["Parent Relationship"], aggregate["Subplan Name"]) == ("SubPlan", "SubPlan 1")
    assert scan["Filter"] == "(course_id = c.id)"
    assert scan["Rows Removed by Filter"] == 299392


@pytest.mark.parametrize(
    "text, name", [(INITPLAN_PG17, "InitPlan 1"), (INITPLAN_PG16, "InitPlan 1 (returns $0)")]
)
def test_initplan_children_get_the_parent_relationship_json_gives(text, name):
    root, aggregate, scan, bitmap = _nodes(text)
    assert (aggregate["Parent Relationship"], aggregate["Subplan Name"]) == ("InitPlan", name)
    assert scan["Alias"] == "courses_1"
    assert bitmap["Parent Relationship"] == "Outer"


def test_cte_header_is_an_initplan():
    text = (
        "Hash Join (actual rows=9 loops=1)\n"
        "  Hash Cond: (a.id = b.id)\n"
        "  CTE x\n"
        "    ->  Index Only Scan using courses_pkey on courses (actual rows=9 loops=1)\n"
        "  ->  CTE Scan on x a (actual rows=9 loops=1)\n"
        "  ->  Hash (actual rows=9 loops=1)\n"
        "        ->  CTE Scan on x b (actual rows=9 loops=1)\n"
    )
    root, cte, outer, hashed, inner = _nodes(text)
    assert (cte["Parent Relationship"], cte["Subplan Name"]) == ("InitPlan", "CTE x")
    assert (outer["Parent Relationship"], hashed["Parent Relationship"]) == ("Outer", "Inner")


def test_sort_key_splits_only_on_top_level_commas():
    text = (
        "Sort (actual rows=1 loops=1)\n"
        "  Sort Key: (lower(title)), round(credits, 2) DESC, ', '::text\n"
        "  Sort Method: top-N heapsort  Memory: 25kB\n"
    )
    root = parse_explain(text)["Plan"]
    assert root["Sort Key"] == ["(lower(title))", "round(credits, 2) DESC", "', '::text"]
    assert (root["Sort Method"], root["Sort Space Used"], root["Sort Space Type"]) == (
        "top-N heapsort",
        25,
        "Memory",
    )


def test_grouping_sets_leave_out_group_and_sort_keys_like_json():
    text = (
        "GroupAggregate (actual rows=3 loops=1)\n"
        "  Group Key: a, b\n"
        "  Group Key: a\n"
        "  Sort Key: b\n"
        "    Group Key: b\n"
        "  ->  Sort (actual rows=3 loops=1)\n"
        "        Sort Key: a, b\n"
        "        ->  Seq Scan on t (actual rows=3 loops=1)\n"
    )
    aggregate, sort, _ = _nodes(text)
    assert "Group Key" not in aggregate and "Sort Key" not in aggregate
    assert sort["Sort Key"] == ["a", "b"]


def test_planning_jit_worker_and_summary_blocks_are_skipped():
    text = (
        "Aggregate (actual time=1.0..6.0 rows=1 loops=1)\n"
        "  Output: count(*)\n"
        "  ->  Gather (actual time=1.0..5.0 rows=0 loops=1)\n"
        "        Workers Planned: 2\n"
        "        Workers Launched: 2\n"
        "        ->  Parallel Seq Scan on public.enrollments (actual time=0.1..0.9 rows=0 loops=3)"
        "\n"
        "              Filter: (enrollments.term > 'a'::text)\n"
        "              Worker 0:  actual time=0.1..0.9 rows=0 loops=1\n"
        "                Buffers: shared hit=931\n"
        "              Buffers: shared hit=2206\n"
        "              I/O Timings: shared read=0.1\n"
        "Query Identifier: -123\n"
        "Planning:\n"
        "  Buffers: shared hit=3\n"
        "  Memory: used=10kB  allocated=16kB\n"
        "Planning Time: 0.039 ms\n"
        "Serialization: time=0.01 ms  output=1kB  format=text\n"
        "Settings: work_mem = '64MB'\n"
        "Trigger for constraint fk_x: time=0.1 calls=1\n"
        "JIT:\n"
        "  Functions: 4\n"
        "  Options: Inlining false, Optimization false, Expressions true, Deforming true\n"
        "  Timing: Generation 0.5 ms (Deform 0.1 ms), Total 4.8 ms\n"
        "Execution Time: 15.703 ms\n"
    )
    plan = parse_explain(text)
    assert (plan["Planning Time"], plan["Execution Time"]) == (0.039, 15.703)
    aggregate, gather, scan = (n.raw for n in walk(plan))
    assert gather["Workers Launched"] == 2
    assert (scan["Schema"], scan["Relation Name"], scan["Parallel Aware"]) == (
        "public",
        "enrollments",
        True,
    )


def test_utf16_text_plan_reads():
    text = _text("missing_index_slow")
    assert parse_explain(text.encode("utf-16")) == parse_explain(text)


ALIGNED_JSON = """\
              QUERY PLAN
--------------------------------------
 [                                   +
   {                                 +
     "Plan": {                       +
       "Node Type": "Result"         +
     }                               +
   }                                 +
 ]
(1 row)
"""


@pytest.mark.parametrize(
    "text, message",
    [
        ("", "the input is empty"),
        ("Result (actual rows=1 loops=1)\n  ->  Teleport (actual rows=1 loops=1)", "line 2: "),
        ("Result (actual rows=1 loops=1)\n   Filter: (a)", "line 2 is indented to a column"),
        ("Result (actual rows=1 loops=1)\n\nResult (actual rows=1 loops=1)", "line 3 starts a"),
        ('"Result (actual rows=1 loops=1)"', "line 1 is wrapped in double quotes"),
        ("Result (actual rows=1 loops=1)\n  this is not a plan", "line 2 is not a plan node"),
        ("hello world", 'line 1: "hello world" is not a plan node'),
        ("  ->  Result (actual rows=1 loops=1)", "line 1 should be the top plan node"),
        ("Result (actual rows=1 loops=1)\nBuffers: shared hit=1", "line 2 is not a plan summary"),
        ("Result (actual rows=1 loops=1)\nExecution Time: fast", '"Execution Time" should be'),
        (ALIGNED_JSON, "line 3 starts FORMAT JSON output that psql printed as a table"),
        (" QUERY PLAN\nResult (actual rows=1 loops=1)", "line 1 is a QUERY PLAN header"),
        (" QUERY PLAN\n----\nResult (actual rows=1 loops=1)", "line 3 does not start with"),
    ],
)
def test_text_plan_rejects_what_it_cannot_read(text, message):
    with pytest.raises(ValueError) as err:
        parse_explain(text)
    assert message in str(err.value)
    assert "FORMAT JSON" in str(err.value)
    assert "EXPLAIN (ANALYZE, BUFFERS)" in str(err.value)


def _aligned(plain: str) -> str:
    """The same plan as psql's default table output prints it."""
    rows = plain.splitlines()
    return "\n".join([" QUERY PLAN", "-" * 20, *(f" {row}" for row in rows), f"({len(rows)} rows)"])


# A string literal with a newline splits the Filter; its second half looks like a property.
SPLIT_LITERAL = (
    "Seq Scan on t (actual rows=1 loops=1)\n" "  Filter: (s = 'x\n" "  Filter: y'::text)\n"
)


@pytest.mark.parametrize("text, line", [(SPLIT_LITERAL, 2), (_aligned(SPLIT_LITERAL), 4)])
def test_a_quoted_value_split_across_lines_is_rejected(text, line):
    with pytest.raises(ValueError, match=f"line {line}: a quoted value continues"):
        parse_explain(text)


def test_even_quotes_including_doubled_ones_are_read():
    text = "Seq Scan on t (actual rows=1 loops=1)\n  Filter: (s = 'it''s'::text)\n"
    assert parse_explain(text)["Plan"]["Filter"] == "(s = 'it''s'::text)"


@pytest.mark.parametrize(
    "text, message",
    [
        (
            "Result (actual rows=1 loops=1)\n"
            "  Unexpected:\n"
            "    ->  Seq Scan on t (actual rows=1 loops=1)\n",
            'line 3 is a plan node indented under "Unexpected:"',
        ),
        (
            "Seq Scan on t (actual rows=1 loops=1)\n"
            "  Worker 0:  actual rows=1 loops=1\n"
            "    SubPlan 1\n",
            'line 3 is a plan node indented under "Worker 0:  actual rows=1 loops=1"',
        ),
        (
            "Result (actual rows=1 loops=1)\n"
            "Planning:\n"
            "  ->  Seq Scan on t (actual rows=1 loops=1)\n",
            'line 3 is a plan node indented under "Planning:"',
        ),
        (
            "Sort (actual rows=1 loops=1)\n  Sort Key: a\n    Filter: (a > 0)\n",
            "line 3 is indented to a column that matches no plan node",
        ),
    ],
)
def test_lines_under_a_block_or_sort_key_cannot_hide_plan_lines(text, message):
    with pytest.raises(ValueError) as err:
        parse_explain(text)
    assert message in str(err.value)


SERIALIZE = (
    "Seq Scan on t (actual time=0.010..0.020 rows=1 loops=1)\n"
    "  Buffers: shared hit=1\n"
    "Planning Time: 0.050 ms\n"
    "Serialization: time=0.015 ms  output=1kB  format=text\n"
    "  Buffers: shared hit=2\n"
    "  I/O Timings: shared read=0.010\n"
    "Execution Time: 0.100 ms\n"
)
SERIALIZE_TIMING_OFF = (
    "Seq Scan on t (actual rows=1 loops=1)\n"
    "Serialization: output=1kB  format=text\n"
    "  Buffers: shared hit=2\n"
    "Execution Time: 0.100 ms\n"
)


@pytest.mark.parametrize(
    "text", [SERIALIZE, _aligned(SERIALIZE), SERIALIZE_TIMING_OFF, _aligned(SERIALIZE_TIMING_OFF)]
)
def test_lines_under_a_summary_line_such_as_serialization_are_skipped(text):
    plan = parse_explain(text)
    assert plan["Execution Time"] == 0.1
    assert plan["Plan"]["Relation Name"] == "t"


@pytest.mark.parametrize("count, relationship", [(1, "child"), (2, "children")])
def test_custom_scan_children_are_named_like_json(count, relationship):
    lines = ["Custom Scan (ChunkAppend) on m (actual rows=1 loops=1)"]
    lines += [f"  ->  Seq Scan on c{n} (actual rows=1 loops=1)" for n in range(count)]
    root, *children = _nodes("\n".join(lines))
    assert [c["Parent Relationship"] for c in children] == [relationship] * count


def test_modify_table_children_get_no_parent_relationship():
    root, child = _nodes(
        "Update on t (actual rows=0 loops=1)\n  ->  Seq Scan on t (actual rows=1 loops=1)"
    )
    assert "Parent Relationship" not in child


def test_text_plan_without_analyze_gets_the_without_analyze_message():
    with pytest.raises(ValueError, match="without ANALYZE"):
        parse_explain("Seq Scan on t  (cost=0.00..1.00 rows=1 width=4)\n  Filter: (a = 1)")
