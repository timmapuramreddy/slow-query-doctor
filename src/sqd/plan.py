"""Walk an EXPLAIN (FORMAT JSON) plan and pull out the parts the rules read."""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from sqd.plan_text import parse_text_explain

# A "quoted name" or a 'string', read left to right so a ' inside "it's" starts no string.
_QUOTED = re.compile(r"\"(?:[^\"]|\"\")*\"|'(?:[^']|'')*'")
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
    # Times the nearest Gather above re-ran its parallel plan (a nested loop can re-run it).
    # A parallel-aware node's loops are these runs times the processes in each run.
    rescans: int = 1

    def child(self, raw: dict[str, Any]) -> PlanNode:
        """The node for one of this node's "Plans", carrying the Gather run count down."""
        gather = self.node_type in ("Gather", "Gather Merge")
        return PlanNode(
            raw=raw, depth=self.depth + 1, rescans=self.loops if gather else self.rescans
        )

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
    def estimated_rows(self) -> float | None:
        """Rows per loop PostgreSQL expected ("Plan Rows"), or None if the plan has no costs."""
        value = self.raw.get("Plan Rows")
        return None if value is None else float(value)

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
    stack = [PlanNode(raw=plan.get("Plan", plan), depth=0)]
    while stack:
        node = stack.pop()
        yield node
        stack.extend(node.child(raw) for raw in reversed(node.raw.get("Plans", [])))


def relations(plan: dict[str, Any]) -> set[str]:
    """Names of the tables the plan reads."""
    return {n.relation for n in walk(plan) if n.relation}


HOW_TO_GET_A_PLAN = (
    'Get one with: psql -XqAt -c "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) <your query>" '
    "> plan.json"
)


_TEXT_PLAN_TOO = (
    'Text output of psql -XqAt -c "EXPLAIN (ANALYZE, BUFFERS) <your query>" also works.'
)


def parse_explain(text: str | bytes) -> dict[str, Any]:
    """The EXPLAIN result from a shared plan file, in the shape explain_analyze() returns.

    Accepts FORMAT JSON output (psql's one-element array or the bare object) or text
    EXPLAIN (ANALYZE, BUFFERS) output, read by parse_text_explain into the same shape.
    Bytes may be UTF-8, -16 or -32 (json detects which), so files saved by PowerShell's `>`
    also read.
    """
    if isinstance(text, bytes):
        try:
            text = text.decode(json.detect_encoding(text))
        except UnicodeDecodeError:
            raise ValueError(
                f"The plan file is not UTF-8, -16 or -32 text. {HOW_TO_GET_A_PLAN}"
            ) from None
    if text.lstrip().startswith(("[", "{")):
        data = _parse_json_explain(text)
    else:
        try:
            data = parse_text_explain(text)
        except ValueError as exc:
            raise ValueError(
                f"The plan is neither FORMAT JSON nor text EXPLAIN output: {exc}. "
                f"{HOW_TO_GET_A_PLAN} {_TEXT_PLAN_TOO}"
            ) from None
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


def _parse_json_explain(text: str) -> dict[str, Any]:
    """The result object from FORMAT JSON output, before its fields are checked."""
    try:
        data = json.loads(text)
    except ValueError:
        raise ValueError(
            "The plan starts like FORMAT JSON output but is not valid JSON. "
            f"{HOW_TO_GET_A_PLAN} {_TEXT_PLAN_TOO}"
        ) from None
    if isinstance(data, list):
        if len(data) != 1:
            raise ValueError(f"Expected one plan, got {len(data)}. {HOW_TO_GET_A_PLAN}")
        data = data[0]
    if not isinstance(data, dict) or not isinstance(data.get("Plan"), dict):
        raise ValueError(f'The file has no "Plan" object. {HOW_TO_GET_A_PLAN}')
    return data


# Real counts and times are far below this; the cap keeps rows x loops finite.
MAX_PLAN_NUMBER = 1e15


def _is_number(value: object) -> bool:
    """True for an int or float from 0 to MAX_PLAN_NUMBER. Comparing (not converting) also
    rejects NaN, inf (json reads 1e400 as inf) and ints too big for a float."""
    return (
        isinstance(value, int | float)
        and not isinstance(value, bool)
        and 0 <= value <= MAX_PLAN_NUMBER
    )


def _type_problem(node: dict[str, Any], where: str) -> str | None:
    """What is wrong with the types of the fields sqd reads, or None.

    Text: names, filters and join conditions. Numbers: row estimates, actual counts and times.
    Lists of text: sort keys.
    """
    for key, value in node.items():
        number = key.startswith(("Actual ", "Rows Removed by ")) or key in (
            "Execution Time",
            "Plan Rows",
        )
        text = key in _TEXT_KEYS or key.endswith((" Cond", "Filter"))
        if number and not _is_number(value):
            return f'"{key}" should be a number from 0 to 1e15 in {where}'
        if text and not number and not isinstance(value, str):
            return f'"{key}" should be text in {where}'
        if key == "Sort Key" and not (
            isinstance(value, list) and all(isinstance(item, str) for item in value)
        ):
            return f'"{key}" should be a list of text in {where}'
    return None


_TEXT_KEYS = frozenset(
    {"Node Type", "Relation Name", "Alias", "Index Name", "Parent Relationship", "Subplan Name"}
)


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


def _blank(match: re.Match[str]) -> str:
    """The quoted text with its inside replaced by spaces, so positions stay the same."""
    text = match.group()
    return text[0] + " " * (len(text) - 2) + text[-1]


def strip_literals(expression: str) -> str:
    """Replace quoted strings with '' so their contents are not read as column names."""
    return _QUOTED.sub(lambda m: m.group() if m.group()[0] == '"' else "''", expression)


def mask_literals(expression: str) -> str:
    """Blank out the inside of quoted strings, keeping every character position the same."""
    return _QUOTED.sub(lambda m: m.group() if m.group()[0] == '"' else _blank(m), expression)


def mask_quoted(expression: str) -> str:
    """Blank out the inside of quoted strings and quoted names, keeping every position.

    For finding calls and brackets: '("lower(x)" = lower("a)b"))' -> '("        " = lower("   "))'.
    """
    return _QUOTED.sub(_blank, expression)


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


# One "Sort Key" entry that is a plain column: the name, then the order as EXPLAIN prints it.
_SORT_KEY = re.compile(rf"({_NAME})((?: DESC)?(?: NULLS (?:FIRST|LAST))?)")


def sort_key_column(key: str, names: set[str]) -> tuple[str, str] | None:
    """(column, order) for a "Sort Key" entry of one table, or None for an expression.

    names are the table's alias and name, dropped from the key as in localize.
    Example with names {"a"}: "a.class_date DESC" -> ("class_date", " DESC").
    """
    match = _SORT_KEY.fullmatch(localize(key, names))
    return (unquote(match.group(1)), match.group(2)) if match else None


_CALL = re.compile(r"\b([a-z_][a-z0-9_]*)\(")


def function_calls(expression: str) -> list[tuple[str, str]]:
    """(function name, argument text) for each call in an expression, outermost first.

    Example: "(lower((email)::text) = ''::text)" -> [("lower", "(email)::text")].
    String arguments are kept: "date_trunc('day'::text, x)" -> [("date_trunc", "'day'::text, x")].
    """
    text = mask_quoted(expression)
    return [
        (match.group(1), expression[match.end() : _closing(text, match.end()) - 1])
        for match in _CALL.finditer(text)
    ]


def _closing(text: str, start: int) -> int:
    """The position just after the ")" that closes a "(" ending at start (text is masked)."""
    depth = 1
    pos = start
    while pos < len(text) and depth:
        if text[pos] == "(":
            depth += 1
        elif text[pos] == ")":
            depth -= 1
        pos += 1
    return pos


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

_EXTRACT = re.compile(r"\bextract\(\s*\w+\s+from\s+", re.I)
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
    # Casts are read with quoted names kept, so ("Order Date")::date matches. A match that
    # starts on a blanked character lies inside a quoted name and is skipped.
    text = mask_literals(expression)
    hidden = mask_quoted(expression)
    found: list[tuple[str, str, str]] = []
    for func, args in function_calls(expression):
        if func in DATE_FUNCTIONS:
            found += [
                (col, f"{func}()", f"{func}({args})") for col in referenced_columns(args, columns)
            ]
    for match in _EXTRACT.finditer(hidden):
        end = _closing(hidden, match.start() + len("extract("))
        found += [
            (col, "EXTRACT()", expression[match.start() : end])
            for col in referenced_columns(expression[match.end() : end - 1], columns)
        ]
    for match in (m for pattern in _DATE_CASTS for m in pattern.finditer(text)):
        if hidden[match.start()] != text[match.start()]:
            continue
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
