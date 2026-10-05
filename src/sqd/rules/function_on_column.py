"""Rule 2: a function wrapped around an indexed column stops PostgreSQL from using the index."""

from __future__ import annotations

import re

from sqd.catalog import Catalog
from sqd.plan import DATE_FUNCTIONS, PlanNode, function_calls, referenced_columns
from sqd.rules.base import Finding
from sqd.rules.missing_index import is_selective_seq_scan

RULE_ID = "function-on-indexed-column"

_CAST = re.compile(r"::[a-z_ ]+(\([0-9, ]*\))?")


def _clean_args(args: str) -> str:
    """Drop casts and extra brackets: '(email)::text' -> 'email'."""
    text = _CAST.sub("", args).strip()
    while text.startswith("(") and text.endswith(")"):
        text = text[1:-1].strip()
    return text


def check(node: PlanNode, catalog: Catalog) -> list[Finding]:
    """Return a finding for each function call that hides an indexed column from the planner."""
    if not is_selective_seq_scan(node, catalog):
        return []
    table = catalog.get(node.relation)
    filter_ = node.own_filter
    assert table is not None and filter_ is not None

    findings: list[Finding] = []
    for func, args in function_calls(filter_):
        # Date functions get their own rule (non-sargable date filters).
        if func in DATE_FUNCTIONS or table.has_expression_index(f"{func}({args})"):
            continue
        for col in referenced_columns(args, table.columns):
            if not table.has_leading_index(col):
                continue
            expr = f"{func}({_clean_args(args)})"
            findings.append(
                Finding(
                    rule_id=RULE_ID,
                    title=f"{func}() hides the index on {table.name}.{col}",
                    explanation=(
                        f"There is an index on {col}, but the filter compares {expr}, not {col}. "
                        f"The index stores {col} values, not {expr} values, so PostgreSQL read "
                        f"every row of {table.name} (about {table.rows:,}) instead."
                    ),
                    suggestion=(
                        f"Compare the bare column if you can (store the value already in the "
                        f"form you search for), or add an index on the expression: "
                        f"CREATE INDEX ON {table.name} ({expr});"
                    ),
                    node=node.summary(),
                    time_ms=node.total_time_ms,
                )
            )
    return findings
