"""Rule 1: a sequential scan on a large table throws away most rows on a column with no index."""

from __future__ import annotations

from sqd.catalog import Catalog
from sqd.plan import PlanNode, function_calls, referenced_columns
from sqd.rules.base import LARGE_TABLE_ROWS, Finding

RULE_ID = "missing-index"

# Fire only when the filter keeps at most this share of the rows it reads.
MAX_KEPT_SHARE = 0.10


def is_selective_seq_scan(node: PlanNode, catalog: Catalog) -> bool:
    """Seq Scan with a filter on a large table that kept at most MAX_KEPT_SHARE of the rows."""
    if node.node_type != "Seq Scan" or not node.filter:
        return False
    table = catalog.get(node.relation)
    if table is None or table.rows < LARGE_TABLE_ROWS:
        return False
    scanned = node.actual_rows + node.rows_removed_by_filter
    return scanned > 0 and node.actual_rows / scanned <= MAX_KEPT_SHARE


def check(node: PlanNode, catalog: Catalog) -> list[Finding]:
    """Return a finding if a selective filter on a large table has no index to use."""
    if not is_selective_seq_scan(node, catalog):
        return []
    table = catalog.get(node.relation)
    assert table is not None and node.filter is not None

    # Columns inside a function call are rule 2's job: a plain index would not help them.
    wrapped = {
        col
        for _, args in function_calls(node.filter)
        for col in referenced_columns(args, table.columns)
    }
    unindexed = [
        col
        for col in referenced_columns(node.filter, table.columns)
        if col not in wrapped and not table.has_leading_index(col)
    ]
    if not unindexed:
        return []

    column = unindexed[0]
    scanned = node.actual_rows + node.rows_removed_by_filter
    kept_pct = 100 * node.actual_rows / scanned
    kept = "under 0.01%" if kept_pct < 0.01 else f"{kept_pct:.2f}%"
    return [
        Finding(
            rule_id=RULE_ID,
            title=f"No index on {table.name}.{column}",
            explanation=(
                f"PostgreSQL read every row of {table.name} (about {table.rows:,}) one by one "
                f"and kept {node.actual_rows:,} ({kept}). There is no index on {column}, "
                "so it has no shortcut to the matching rows."
            ),
            suggestion=f"CREATE INDEX ON {table.name} ({column});",
            node=node.summary(),
            time_ms=node.total_time_ms,
        )
    ]
