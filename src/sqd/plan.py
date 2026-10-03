"""Walk an EXPLAIN (FORMAT JSON) plan and pull out the parts the rules read."""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

_STRING = re.compile(r"'(?:[^']|'')*'")
_IDENT = re.compile(r"\b[a-z_][a-z0-9_]*\b")


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
    def loops(self) -> int:
        return int(self.raw.get("Actual Loops", 1)) or 1

    @property
    def actual_rows(self) -> int:
        """Rows returned across all loops (EXPLAIN reports a per-loop average)."""
        return int(self.raw.get("Actual Rows", 0)) * self.loops

    @property
    def rows_removed_by_filter(self) -> int:
        return int(self.raw.get("Rows Removed by Filter", 0)) * self.loops

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


def strip_literals(expression: str) -> str:
    """Replace quoted strings with '' so their contents are not read as column names."""
    return _STRING.sub("''", expression)


def referenced_columns(expression: str, columns: list[str]) -> list[str]:
    """Columns of a table that appear in a plan expression such as a Filter, in order."""
    known = set(columns)
    seen: list[str] = []
    for token in _IDENT.findall(strip_literals(expression)):
        if token in known and token not in seen:
            seen.append(token)
    return seen


_CALL = re.compile(r"\b([a-z_][a-z0-9_]*)\(")


def function_calls(expression: str) -> list[tuple[str, str]]:
    """(function name, argument text) for each call in an expression, outermost first.

    Example: "(lower((email)::text) = ''::text)" -> [("lower", "(email)::text")].
    """
    text = strip_literals(expression)
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
        calls.append((match.group(1), text[start : pos - 1]))
    return calls


# Column (optionally cast) followed by LIKE (~~) or ILIKE (~~*) and a pattern starting with % or _.
_LEADING_WILDCARD = re.compile(
    r"\b([a-z_][a-z0-9_]*)\)?(?:::[a-z ]+?)?\s+~~\*?\s+'([%_](?:[^']|'')*)'"
)


def leading_wildcard_columns(expression: str, columns: list[str]) -> list[tuple[str, str]]:
    """(column, pattern) for each LIKE / ILIKE whose pattern starts with a wildcard.

    Example: "((email)::text ~~ '%4242@example.edu'::text)" -> [("email", "%4242@example.edu")].
    """
    known = set(columns)
    return [
        (m.group(1), m.group(2))
        for m in _LEADING_WILDCARD.finditer(expression)
        if m.group(1) in known
    ]


# Date functions that hide a date/time column from its index. Shared by rules 2 and 5.
DATE_FUNCTIONS = frozenset({"date_trunc", "date_part", "date"})

_EXTRACT = re.compile(r"\bextract\(\s*\w+\s+from\s+([^)]*)\)", re.I)
_DATE_CAST = re.compile(
    r"\(?(?:[a-z_][a-z0-9_]*\.)?([a-z_][a-z0-9_]*)\)?"
    r"::(date|timestamp(?: with(?:out)? time zone)?)\b"
)


def date_wrapped_columns(expression: str, columns: list[str]) -> list[tuple[str, str]]:
    """(column, wrapper) for each column inside a date function, EXTRACT, or a cast to a date type.

    Example: "((graded_at)::date = '2024-03-15'::date)" -> [("graded_at", "::date")].
    """
    text = strip_literals(expression)
    found: list[tuple[str, str]] = []
    for func, args in function_calls(text):
        if func in DATE_FUNCTIONS:
            found += [(col, f"{func}()") for col in referenced_columns(args, columns)]
    for match in _EXTRACT.finditer(text):
        found += [(col, "EXTRACT()") for col in referenced_columns(match.group(1), columns)]
    for match in _DATE_CAST.finditer(text):
        if match.group(1) in columns:
            found.append((match.group(1), f"::{match.group(2)}"))
    unique: list[tuple[str, str]] = []
    for item in found:
        if item not in unique:
            unique.append(item)
    return unique
