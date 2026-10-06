"""Walk an EXPLAIN (FORMAT JSON) plan and pull out the parts the rules read."""

from __future__ import annotations

import json
import math
import re
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

_STRING = re.compile(r"'(?:[^']|'')*'")
# A plain lowercase name, or a quoted one such as "UserId" ("" inside is a literal quote).
_NAME = r'(?:[a-z_][a-z0-9_$]*|"(?:[^"]|"")+")'
# A name that is not a function call.
_IDENT = re.compile(rf"(?<![\w$\"]){_NAME}(?![\w$])(?!\s*\()")
# The type in a cast, so '::date' or '::timestamp with time zone' is not read as a column.
_CAST_TYPE = re.compile(
    rf"::(?:{_NAME}\.)?(?:(?:timestamp|time) with(?:out)? time zone"
    rf"|character varying|double precision|bit varying|{_NAME})"
    r"(?:\([0-9, ]*\))?(?:\[\])*"
)
# The field in EXTRACT(year FROM col), which is a keyword, not a column.
_EXTRACT_FIELD = re.compile(r"\bextract\(\s*\w+\s+from\b", re.I)
# alias.column, but not schema.function( or ::schema.type.
_QUALIFIED = re.compile(rf"(?<!::)(?<![\w$\"])({_NAME})\.({_NAME})(?![\w$])(?!\s*\()")


@dataclass(frozen=True)
class PlanNode:
    """One node of the plan tree, with the raw JSON kept for anything not modelled here."""

    raw: dict[str, Any]
    depth: int

    @property
    def node_type(self) -> str:
        return self.raw.get("Node Type", "")

    @property
    def relation(self) -> str | None:
        return self.raw.get("Relation Name")

    @property
    def alias(self) -> str | None:
        return self.raw.get("Alias")

    @property
    def filter(self) -> str | None:
        return self.raw.get("Filter")

    @property
    def own_filter(self) -> str | None:
        """Filter with this table's alias dropped and other tables' columns hidden (localize)."""
        if self.filter is None:
            return None
        return localize(self.filter, {n for n in (self.alias, self.relation) if n})

    @property
    def loops(self) -> int:
        return int(self.raw.get("Actual Loops", 1)) or 1

    @property
    def actual_rows(self) -> int:
        """Rows returned across all loops (EXPLAIN reports a per-loop average)."""
        return round(float(self.raw.get("Actual Rows", 0)) * self.loops)

    @property
    def rows_removed_by_filter(self) -> int:
        return round(float(self.raw.get("Rows Removed by Filter", 0)) * self.loops)

    @property
    def total_time_ms(self) -> float:
        """Time spent in this node. Parallel workers run at the same time, so do not add them."""
        per_loop = float(self.raw.get("Actual Total Time", 0.0))
        return per_loop if self.raw.get("Parallel Aware") else per_loop * self.loops

    def summary(self) -> str:
        """Short one-line label, e.g. 'Seq Scan on attendance (Filter: (enrollment_id = 42))'."""
        label = self.node_type
        if self.relation:
            label += f" on {self.relation}"
            if self.alias and self.alias != self.relation:
                label += f" {self.alias}"
        if self.filter:
            label += f" (Filter: {self.filter})"
        return label


def walk(plan: dict[str, Any]) -> Iterator[PlanNode]:
    """Yield every node, top first. Accepts the EXPLAIN result or its "Plan" object."""
    root = plan.get("Plan", plan)
    stack: list[tuple[dict[str, Any], int]] = [(root, 0)]
    while stack:
        node, depth = stack.pop()
        yield PlanNode(raw=node, depth=depth)
        for child in reversed(node.get("Plans", [])):
            stack.append((child, depth + 1))


def relations(plan: dict[str, Any]) -> set[str]:
    """Names of the tables the plan reads."""
    return {n.relation for n in walk(plan) if n.relation}


HOW_TO_GET_A_PLAN = (
    'Get one with: psql -XqAt -c "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) <your query>" '
    "> plan.json"
)


def parse_explain(text: str | bytes) -> dict[str, Any]:
    """The EXPLAIN result from a shared plan file, in the shape explain_analyze() returns.

    Accepts psql's one-element array or the bare object. Bytes may be UTF-8, -16 or -32
    (json detects which), so files saved by PowerShell's `>` also read.
    """
    try:
        data = json.loads(text)
    except ValueError:
        raise ValueError(
            f"The plan is not JSON (text-format EXPLAIN is not supported). {HOW_TO_GET_A_PLAN}"
        ) from None
    if isinstance(data, list):
        if len(data) != 1:
            raise ValueError(f"Expected one plan, got {len(data)}. {HOW_TO_GET_A_PLAN}")
        data = data[0]
    if not isinstance(data, dict) or not isinstance(data.get("Plan"), dict):
        raise ValueError(f'The file has no "Plan" object. {HOW_TO_GET_A_PLAN}')
    if "Actual Rows" not in data["Plan"]:
        raise ValueError(
            "The plan has no actual row counts: it was made without ANALYZE, and the rules "
            f"need real row counts. {HOW_TO_GET_A_PLAN}"
        )
    problem = _type_problem(data, "the plan") or next(
        filter(None, (_node_problem(n) for n in _raw_nodes(data["Plan"]))), None
    )
    if problem:
        raise ValueError(f"The plan is not valid EXPLAIN output: {problem}. {HOW_TO_GET_A_PLAN}")
    return data


def _is_number(value: object) -> bool:
    """True for a finite int or float; json reads 1e400 as inf and NaN as nan."""
    return isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)


def _type_problem(node: dict[str, Any], where: str) -> str | None:
    """What is wrong with the types of the fields sqd reads, or None.

    Text: names, filters and join conditions. Numbers: actual counts and times.
    """
    for key, value in node.items():
        number = key.startswith(("Actual ", "Rows Removed by ")) or key == "Execution Time"
        text = key in ("Node Type", "Relation Name", "Alias", "Index Name") or key.endswith(
            (" Cond", "Filter")
        )
        if number and not _is_number(value):
            return f'"{key}" should be a number in {where}'
        if text and not number and not isinstance(value, str):
            return f'"{key}" should be text in {where}'
    return None


def _node_problem(node: object) -> str | None:
    if not isinstance(node, dict):
        return f"{json.dumps(node)} is not a plan node"
    where = f"a {node['Node Type']} node" if isinstance(node.get("Node Type"), str) else "a node"
    if not isinstance(node.get("Plans", []), list):
        return f'"Plans" should be a list in {where}'
    return _type_problem(node, where)


def _raw_nodes(root: object) -> Iterator[object]:
    """Every node of an unchecked plan tree; stops below anything that is not a node."""
    stack = [root]
    while stack:
        node = stack.pop()
        yield node
        if isinstance(node, dict) and isinstance(node.get("Plans"), list):
            stack.extend(node["Plans"])


def strip_literals(expression: str) -> str:
    """Replace quoted strings with '' so their contents are not read as column names."""
    return _STRING.sub("''", expression)


def mask_literals(expression: str) -> str:
    """Blank out the inside of quoted strings, keeping every character position the same."""
    return _STRING.sub(lambda m: "'" + " " * (len(m.group()) - 2) + "'", expression)


def unquote(name: str) -> str:
    """'"UserId"' -> 'UserId'; plain names are returned as they are."""
    return name[1:-1].replace('""', '"') if name.startswith('"') else name


# Keywords PostgreSQL 17 quotes in names: pg_get_keywords() where catcode <> 'U'.
_KEYWORDS_TEXT = """
    all analyse analyze and any array as asc asymmetric authorization between bigint binary bit
    boolean both case cast char character check coalesce collate collation column concurrently
    constraint create cross current_catalog current_date current_role current_schema
    current_time current_timestamp current_user dec decimal default deferrable desc distinct do
    else end except exists extract false fetch float for foreign freeze from full grant greatest
    group grouping having ilike in initially inner inout int integer intersect interval into is
    isnull join json json_array json_arrayagg json_exists json_object json_objectagg json_query
    json_scalar json_serialize json_table json_value lateral leading least left like limit
    localtime localtimestamp merge_action national natural nchar none normalize not notnull null
    nullif numeric offset on only or order out outer overlaps overlay placing position precision
    primary real references returning right row select session_user setof similar smallint some
    substring symmetric system_user table tablesample then time timestamp to trailing treat trim
    true union unique user using values varchar variadic verbose when where window with
    xmlattributes xmlconcat xmlelement xmlexists xmlforest xmlnamespaces xmlparse xmlpi xmlroot
    xmlserialize xmltable
"""
_QUOTED_KEYWORDS = frozenset(_KEYWORDS_TEXT.split())
_PLAIN_NAME = re.compile(r"[a-z_][a-z0-9_$]*")


def quote_name(name: str) -> str:
    """Name as it must be written in SQL, like PostgreSQL's quote_ident: 'UserId' -> '"UserId"'."""
    if _PLAIN_NAME.fullmatch(name) and name not in _QUOTED_KEYWORDS:
        return name
    return '"' + name.replace('"', '""') + '"'


def localize(expression: str, names: set[str]) -> str:
    """One table's view of an expression: drop its own alias, hide other tables' columns.

    Join conditions and correlated subqueries name columns of other tables with their alias.
    Those become '$0' so a column of the same name is not mistaken for this table's.
    Example with names {"e"}: "(e.course_id = c.id)" -> "(course_id = $0)".
    """
    masked = mask_literals(expression)
    parts: list[str] = []
    last = 0
    for match in _QUALIFIED.finditer(masked):
        parts.append(expression[last : match.start()])
        parts.append(match.group(2) if unquote(match.group(1)) in names else "$0")
        last = match.end()
    parts.append(expression[last:])
    return "".join(parts)


def referenced_columns(expression: str, columns: list[str]) -> list[str]:
    """Columns of a table that appear in a plan expression such as a Filter, in order.

    Cast types, function names and EXTRACT fields are skipped, so a column called "date"
    is not found in '::date' or date(...).
    """
    known = set(columns)
    seen: list[str] = []
    text = _EXTRACT_FIELD.sub("extract(", _CAST_TYPE.sub("", strip_literals(expression)))
    for token in map(unquote, _IDENT.findall(text)):
        if token in known and token not in seen:
            seen.append(token)
    return seen


_CALL = re.compile(r"\b([a-z_][a-z0-9_]*)\(")


def function_calls(expression: str) -> list[tuple[str, str]]:
    """(function name, argument text) for each call in an expression, outermost first.

    Example: "(lower((email)::text) = ''::text)" -> [("lower", "(email)::text")].
    String arguments are kept: "date_trunc('day'::text, x)" -> [("date_trunc", "'day'::text, x")].
    """
    text = mask_literals(expression)
    calls: list[tuple[str, str]] = []
    for match in _CALL.finditer(text):
        start = match.end()
        depth = 1
        pos = start
        while pos < len(text) and depth:
            if text[pos] == "(":
                depth += 1
            elif text[pos] == ")":
                depth -= 1
            pos += 1
        calls.append((match.group(1), expression[start : pos - 1]))
    return calls


# Column (optionally cast) followed by LIKE (~~) or ILIKE (~~*) and a pattern starting with % or _.
_LEADING_WILDCARD = re.compile(
    rf"(?<![\w$\"])({_NAME})\)?(?:::[a-z ]+?)?\s+~~\*?\s+'([%_](?:[^']|'')*)'"
)


def leading_wildcard_columns(expression: str, columns: list[str]) -> list[tuple[str, str]]:
    """(column, pattern) for each LIKE / ILIKE whose pattern starts with a wildcard.

    Example: "((email)::text ~~ '%4242@example.edu'::text)" -> [("email", "%4242@example.edu")].
    """
    known = set(columns)
    return [
        (unquote(m.group(1)), m.group(2))
        for m in _LEADING_WILDCARD.finditer(expression)
        if unquote(m.group(1)) in known
    ]


# Date functions that hide a date/time column from its index. Shared by rules 2 and 5.
DATE_FUNCTIONS = frozenset({"date_trunc", "date_part", "date"})

_EXTRACT = re.compile(r"\bextract\(\s*\w+\s+from\s+([^)]*)\)", re.I)
_DATE_TYPE = r"::(date|timestamp(?: with(?:out)? time zone)?)\b"
_DATE_CASTS = (
    # (graded_at)::date
    re.compile(rf"\(?(?:{_NAME}\.)?({_NAME})\)?{_DATE_TYPE}"),
    # ((graded_at AT TIME ZONE 'UTC'::text))::date
    re.compile(rf"\(\((?:{_NAME}\.)?({_NAME}) AT TIME ZONE '[^']*'(?:::[a-z ]+?)?\)\){_DATE_TYPE}"),
)


def date_wrapped_columns(expression: str, columns: list[str]) -> list[tuple[str, str, str]]:
    """(column, wrapper, expression) for each column inside a date function, EXTRACT or cast.

    The expression is the exact text, to compare with expression index definitions.
    Example: "((graded_at)::date = '2024-03-15'::date)"
    -> [("graded_at", "::date", "(graded_at)::date")].
    """
    text = mask_literals(expression)
    found: list[tuple[str, str, str]] = []
    for func, args in function_calls(expression):
        if func in DATE_FUNCTIONS:
            found += [
                (col, f"{func}()", f"{func}({args})") for col in referenced_columns(args, columns)
            ]
    for match in _EXTRACT.finditer(text):
        found += [
            (col, "EXTRACT()", expression[match.start() : match.end()])
            for col in referenced_columns(match.group(1), columns)
        ]
    for match in (m for pattern in _DATE_CASTS for m in pattern.finditer(text)):
        if unquote(match.group(1)) in columns:
            found.append(
                (
                    unquote(match.group(1)),
                    f"::{match.group(2)}",
                    expression[match.start() : match.end()],
                )
            )
    unique: list[tuple[str, str, str]] = []
    for item in found:
        if item not in unique:
            unique.append(item)
    return unique
