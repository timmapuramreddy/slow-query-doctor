"""Rule 5: a date function or cast around a date column turns a range lookup into a full scan."""

from __future__ import annotations

from sqd.catalog import Catalog
from sqd.plan import PlanNode, date_wrapped_columns
from sqd.rules.base import Finding
from sqd.rules.missing_index import is_selective_seq_scan

RULE_ID = "non-sargable-date-filter"


def check(node: PlanNode, catalog: Catalog) -> list[Finding]:
    """Return a finding for each date column hidden inside date_trunc(), EXTRACT() or a cast.

    Stays silent for a column when an expression index matches the exact wrapped expression.
    """
    if not is_selective_seq_scan(node, catalog):
        return []
    table = catalog.get(node.relation)
    filter_ = node.own_filter
    assert table is not None and filter_ is not None

    wrapped = date_wrapped_columns(filter_, table.columns)
    # Only the outermost expressions count: a cast inside date_trunc() is part of it.
    outer = [
        (col, expr)
        for col, _, expr in wrapped
        if not any(expr != other and expr in other for _, _, other in wrapped)
    ]
    seen = {
        col
        for col in {c for c, _ in outer}
        if all(table.has_expression_index(expr) for c, expr in outer if c == col)
    }
    findings: list[Finding] = []
    for col, wrapper, _ in wrapped:
        if col in seen:
            continue
        seen.add(col)
        indexed = table.has_leading_index(col)
        index_note = (
            f"There is an index on {col}, but it stores {col} values, not {wrapper} results, "
            "so PostgreSQL"
            if indexed
            else "PostgreSQL"
        )
        add_index = "" if indexed else f" Then add CREATE INDEX ON {table.name} ({col});"
        findings.append(
            Finding(
                rule_id=RULE_ID,
                title=f"{wrapper} on {table.name}.{col} turns a date range into a full scan",
                explanation=(
                    f"The filter changes {col} with {wrapper} before comparing it. {index_note} "
                    f"computed it for every row of {table.name} (about {table.rows:,})."
                ),
                suggestion=(
                    f"Compare the bare column with a half-open range, for example "
                    f"{col} >= '2024-03-15' AND {col} < '2024-03-16' for one day.{add_index}"
                ),
                node=node.summary(),
                time_ms=node.total_time_ms,
            )
        )
    return findings
