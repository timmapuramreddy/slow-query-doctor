"""Rule 5: a date function or cast around a date column turns a range lookup into a full scan."""

from __future__ import annotations

import re

from sqd.catalog import Catalog
from sqd.plan import PlanNode, date_wrapped_columns, quote_name
from sqd.rules.base import Finding
from sqd.rules.missing_index import is_selective_seq_scan, table_rows

RULE_ID = "non-sargable-date-filter"

_ZONE = re.compile(r"AT TIME ZONE '((?:[^']|'')*)'")


def check(node: PlanNode, catalog: Catalog) -> list[Finding]:
    """Return a finding for each date column hidden inside date_trunc(), EXTRACT() or a cast.

    Stays silent for a column when an expression index matches the exact wrapped expression.
    """
    if not is_selective_seq_scan(node, catalog):
        return []
    table = catalog.get(node.relation)
    filter_ = node.own_filter
    assert table is not None and filter_ is not None
    rows = table_rows(node, table)

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
        # With AT TIME ZONE the day starts at midnight in that zone, so the range must say so.
        zone = next(
            (m.group(1) for c, _, expr in wrapped if c == col for m in [_ZONE.search(expr)] if m),
            None,
        )
        at = f" 00:00 {zone}" if zone else ""
        name = quote_name(col)
        indexed = table.has_leading_index(col)
        index_note = (
            f"There is an index on {col}, but it stores {col} values, not {wrapper} results, "
            "so PostgreSQL"
            if indexed
            else "PostgreSQL"
        )
        add_index = (
            "" if indexed else f" Then add CREATE INDEX ON {quote_name(table.name)} ({name});"
        )
        findings.append(
            Finding(
                rule_id=RULE_ID,
                title=f"{wrapper} on {table.name}.{col} turns a date range into a full scan",
                explanation=(
                    f"The filter changes {col} with {wrapper} before comparing it. {index_note} "
                    f"computed it for every row of {table.name} (about {rows:,})."
                ),
                suggestion=(
                    f"Compare the bare column with a half-open range, for example "
                    f"{name} >= '2024-03-15{at}' AND {name} < '2024-03-16{at}' for one day."
                    f"{add_index}"
                ),
                node=node.summary(),
                time_ms=node.total_time_ms,
            )
        )
    return findings
