"""Rule 10: PostgreSQL guessed far too few rows, so it chose a nested loop that ran many times."""

from __future__ import annotations

import re

from sqd.catalog import Catalog
from sqd.plan import (
    PlanNode,
    date_wrapped_columns,
    function_calls,
    mask_literals,
    quote_name,
    referenced_columns,
    walk,
)
from sqd.rules.base import Finding

RULE_ID = "bad-row-estimate"

# Fire when the outer side returned at least this many times the rows PostgreSQL expected,
# and the inner side then ran at least MIN_RUNS times. On the demo data each inner run of an
# in-memory index lookup costs about 1 microsecond more than a hash join would, so 50,000
# runs cost about 50 ms extra; fewer is not worth a finding. Rows on disk cost far more.
MIN_RATIO = 10
MIN_RUNS = 50_000

# PostgreSQL allows at most 8 columns and expressions in one statistics object.
MAX_STATISTICS_ITEMS = 8

# What follows a column compared directly, as EXPLAIN prints it: "status = ".
_COMPARISON = re.compile(r"\s+(?:=|<>|<=|>=|<|>)\s")


def _ratio(node: PlanNode) -> float | None:
    """Actual rows per loop over the estimate, or None if the plan has no estimate."""
    estimate = node.estimated_rows
    if estimate is None:
        return None
    return node.actual_rows / node.loops / max(estimate, 1.0)


def _child(node: PlanNode, relationship: str) -> PlanNode | None:
    for raw in node.raw.get("Plans", []):
        if raw.get("Parent Relationship") == relationship:
            return node.child(raw)
    return None


def _ran(inner: PlanNode) -> PlanNode:
    """The node that really ran for each outer row. Below a Memoize, only cache misses run."""
    while inner.node_type == "Memoize" and inner.raw.get("Plans"):
        inner = inner.child(inner.raw["Plans"][0])
    return inner


def _source(outer: PlanNode) -> PlanNode | None:
    """The node where the bad guess starts: the first node, top down, that is off by
    MIN_RATIO or more while none of its children is."""
    for node in walk(outer.raw):
        ratio = _ratio(node)
        if ratio is None or ratio < MIN_RATIO:
            continue
        children = [node.child(raw) for raw in node.raw.get("Plans", [])]
        if not any((_ratio(c) or 0) >= MIN_RATIO for c in children):
            return node
    return None


def _bad_loop(node: PlanNode) -> tuple[PlanNode, PlanNode, PlanNode] | None:
    """(outer, inner, source) if this is a nested loop the bad guess made slow, else None."""
    if node.node_type != "Nested Loop":
        return None
    outer, inner = _child(node, "Outer"), _child(node, "Inner")
    if outer is None or inner is None:
        return None
    ratio = _ratio(outer)
    if ratio is None or ratio < MIN_RATIO or _ran(inner).loops < MIN_RUNS:
        return None
    source = _source(outer)
    return (outer, inner, source) if source is not None else None


def _wrapped_expressions(filter_: str, columns: list[str]) -> list[str]:
    """Function calls, EXTRACTs and date casts around this table's columns, outermost only.

    Example: "(lower(status) = ''::text)" -> ["lower(status)"].
    """
    found = [
        f"{func}({args})"
        for func, args in function_calls(filter_)
        if referenced_columns(args, columns)
    ]
    found += [expr for _, _, expr in date_wrapped_columns(filter_, columns)]
    unique = list(dict.fromkeys(found))
    return [e for e in unique if not any(e != other and e in other for other in unique)]


def _outside_quotes(text: str) -> list[bool] | None:
    """For each character, True if it is outside every 'string' and "quoted name".

    None if one does not close. Doubled quotes inside are escapes. Unlike mask_literals, a '
    inside a quoted name such as "a'b" does not start a string.
    """
    outside: list[bool] = []
    quote, pos = "", 0
    while pos < len(text):
        char = text[pos]
        if quote and text.startswith(quote * 2, pos):
            outside += [False, False]
            pos += 2
            continue
        if quote:
            quote = "" if char == quote else quote
            outside.append(False)
        elif char in "'\"":
            quote = char
            outside.append(False)
        else:
            outside.append(True)
        pos += 1
    return None if quote else outside


def _closes(expression: str) -> bool:
    """True if every quote in the expression closes and so does every bracket outside them."""
    outside = _outside_quotes(expression)
    if outside is None:
        return False
    depth = 0
    for char, out in zip(expression, outside, strict=True):
        if out and char in "()":
            depth += 1 if char == "(" else -1
            if depth < 0:
                return False
    return depth == 0


def _starts(expression: str, filter_: str) -> list[int]:
    """Where the expression appears in the filter outside quotes. The helpers above also find
    function-like text inside a quoted name such as "a f(x) b"."""
    outside = _outside_quotes(filter_) or []
    found: list[int] = []
    start = filter_.find(expression)
    while start >= 0:
        if start < len(outside) and outside[start]:
            found.append(start)
        start = filter_.find(expression, start + 1)
    return found


def _compared(column: str, filter_: str) -> bool:
    """True if every use of the column in the filter compares it directly (=, <, >= ...).

    Not after an operator or a cast ("payload ->> 'k'", "(payload)::text"): sqd does not know
    the column's type, and statistics on a json column itself would fail.
    """
    name = re.escape(quote_name(column))
    uses = list(re.finditer(rf'(?<![\w$."]){name}(?![\w$"])', filter_))
    return bool(uses) and all(_COMPARISON.match(filter_, use.end()) for use in uses)


def _is_whole(expression: str, filter_: str) -> bool:
    """True if the expression can be copied into SQL as it is: nothing in it is cut short, no
    schema name comes before it, and it is compared directly, as _compared asks of a column.

    The helpers above read only a function's own name, and stop at the first ")" in EXTRACT or
    at one inside a quoted name such as "x)y". A cast after the expression would be dropped,
    and the uncast result may be a type statistics cannot be built on (json).
    """
    return _closes(expression) and all(
        not (start and (filter_[start - 1] in '."$_' or filter_[start - 1].isalnum()))
        and _COMPARISON.match(filter_, start + len(expression))
        for start in _starts(expression, filter_)
    )


def _suggestion(source: PlanNode, catalog: Catalog) -> str:
    """How to give PostgreSQL a better guess, as exact SQL when the filter allows it."""
    table = catalog.get(source.relation)
    if source.relation is None:
        return (
            f"The guess went wrong at {source.node_type}, not at a single table. Run ANALYZE "
            "on the tables it reads. If its join or filter compares columns that depend on "
            "each other, CREATE STATISTICS on those columns can help."
        )
    name = quote_name(source.relation)
    if table is None:
        return (
            f"Run ANALYZE {name}; to refresh its statistics. If the filter wraps columns in "
            "functions, compare the plain columns, or add CREATE STATISTICS on the "
            "expressions. If it filters on columns that depend on each other, add CREATE "
            "STATISTICS on those columns. Add --catalog for the exact SQL."
        )
    filter_ = source.own_filter or ""
    # Other tables' columns ($0), parameters and subplans cannot go into CREATE STATISTICS.
    if any(mark in mask_literals(filter_) for mark in ("$", "SubPlan", "InitPlan")):
        filter_ = ""
    columns = referenced_columns(filter_, table.columns)
    expressions = [e for e in _wrapped_expressions(filter_, table.columns) if _starts(e, filter_)]
    plain = [c for c in columns if not any(c in referenced_columns(e, [c]) for e in expressions)]
    items = [quote_name(c) for c in plain] + [f"({e})" for e in expressions]
    if not expressions and len(plain) < 2:
        return (
            f"Run ANALYZE {name}; so PostgreSQL has current statistics, then check the plan "
            "again."
        )
    exact = (
        len(items) <= MAX_STATISTICS_ITEMS
        and all(_is_whole(e, filter_) for e in expressions)
        and all(_compared(c, filter_) for c in plain)
    )
    stats = quote_name(f"{source.relation}_{'_'.join(columns)}_stats")
    create = f"CREATE STATISTICS {stats} ON {', '.join(items)} FROM {name}; ANALYZE {name};"
    if expressions:
        if not exact:
            create = (
                f"CREATE STATISTICS on up to {MAX_STATISTICS_ITEMS} of the expressions in the "
                f"filter, then ANALYZE {name};"
            )
        return (
            f"PostgreSQL keeps statistics on columns, not on {' or '.join(expressions)}, so "
            "it guessed. Best: filter on the plain columns instead (for example a date range "
            "instead of a date function), so their statistics apply. If you can't change the "
            f"query, give PostgreSQL statistics on the expressions: {create}"
        )
    if not exact:
        create = (
            f"CREATE STATISTICS on {MAX_STATISTICS_ITEMS} or fewer of those columns, then "
            f"ANALYZE {name};"
        )
    return (
        "PostgreSQL treats filters on different columns as unrelated and multiplies their "
        f"chances. If {', '.join(plain)} depend on each other, tell it: {create}"
    )


def check(node: PlanNode, catalog: Catalog) -> list[Finding]:
    """Return a finding if a far-too-low row estimate made this nested loop run many times.

    Expecting few rows, PostgreSQL runs the inner side once per outer row. When the guess is
    off by MIN_RATIO or more and that means MIN_RUNS or more runs, a hash join, which reads
    each side once, is usually faster. Stacked nested loops fed by the same bad guess give one
    finding, on the lowest loop. A qualifying loop on the inner side is not merged.
    """
    found = _bad_loop(node)
    if found is None:
        return []
    outer, inner, source = found
    for lower in walk(outer.raw):
        below = _bad_loop(lower)
        if below is not None and below[2].raw is source.raw:
            return []
    expected = round((source.estimated_rows or 0) * source.loops)
    what = source.relation or source.node_type
    ran = _ran(inner)
    each = "each row" if ran is inner else "each row not already in its cache (Memoize)"
    return [
        Finding(
            rule_id=RULE_ID,
            title=f"PostgreSQL expected {expected:,} rows from {what} and got about "
            f"{source.actual_rows:,}",
            explanation=(
                f"PostgreSQL guessed {source.summary()} would return about {expected:,} rows; "
                f"it returned about {source.actual_rows:,}. Expecting few rows, it chose a nested "
                f"loop, which runs {ran.summary()} once for {each}: {ran.loops:,} times. For "
                "this many rows a hash join, which reads each side once, is usually faster."
            ),
            suggestion=_suggestion(source, catalog),
            node=f"{node.summary()} ({ran.node_type} ran {ran.loops:,} times)",
            time_ms=node.total_time_ms,
        )
    ]
