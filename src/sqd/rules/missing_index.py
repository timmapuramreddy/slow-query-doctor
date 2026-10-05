"""Rule 1: a large table is read in full because a filter or join column has no index."""

from __future__ import annotations

from sqd.catalog import Catalog, Table
from sqd.plan import (
    PlanNode,
    date_wrapped_columns,
    function_calls,
    leading_wildcard_columns,
    localize,
    referenced_columns,
)
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


def wrapped_columns(expression: str, columns: list[str]) -> set[str]:
    """Columns inside a function call, a date cast or a leading-wildcard LIKE.

    Those belong to rules 2, 3 and 5: a plain index on the column would not help them.
    """
    wrapped = {
        col for _, args in function_calls(expression) for col in referenced_columns(args, columns)
    }
    wrapped |= {col for col, *_ in date_wrapped_columns(expression, columns)}
    wrapped |= {col for col, _ in leading_wildcard_columns(expression, columns)}
    return wrapped


def unindexed_columns(expression: str, table: Table) -> list[str]:
    """Bare columns of the table in the expression that no index starts with."""
    wrapped = wrapped_columns(expression, table.columns)
    return [
        col
        for col in referenced_columns(expression, table.columns)
        if col not in wrapped and not table.has_leading_index(col)
    ]


def check(node: PlanNode, catalog: Catalog) -> list[Finding]:
    """Return a finding if a selective filter on a large table has no index to use."""
    if not is_selective_seq_scan(node, catalog):
        return []
    table = catalog.get(node.relation)
    filter_ = node.own_filter
    assert table is not None and filter_ is not None

    unindexed = unindexed_columns(filter_, table)
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


# Join node type -> the plan key holding its join condition.
JOIN_CONDITIONS = {
    "Hash Join": "Hash Cond",
    "Merge Join": "Merge Cond",
    "Nested Loop": "Join Filter",
}
# Nodes that sit between a join and the scan feeding it without changing which rows flow.
_PASS_THROUGH = frozenset({"Hash", "Sort", "Materialize", "Gather", "Gather Merge", "Memoize"})


def _scan_below(node: PlanNode) -> PlanNode | None:
    """The Seq Scan that feeds a join input, looking through Hash, Sort, Materialize, Gather."""
    while node.node_type in _PASS_THROUGH and node.raw.get("Plans"):
        node = PlanNode(raw=node.raw["Plans"][0], depth=node.depth + 1)
    return node if node.node_type == "Seq Scan" else None


def check_join(node: PlanNode, catalog: Catalog) -> list[Finding]:
    """Return a finding if a join reads a whole large table to keep few rows of it.

    That happens when the table's join key has no index: PostgreSQL cannot look up the
    matching rows for each row of the other side, so it reads them all. Fires only when the
    join keeps at most MAX_KEPT_SHARE of the rows read, and the other side is that small too,
    so an index lookup per row of the other side would be cheaper.
    """
    condition = node.raw.get(JOIN_CONDITIONS.get(node.node_type, ""))
    children = node.raw.get("Plans", [])
    if not condition or len(children) != 2:
        return []
    sides = [PlanNode(raw=child, depth=node.depth + 1) for child in children]

    findings: list[Finding] = []
    for i, side in enumerate(sides):
        scan = _scan_below(side)
        # A selective filter on the scan is rule 1's plain check; leave it there.
        if scan is None or is_selective_seq_scan(scan, catalog):
            continue
        table = catalog.get(scan.relation)
        read = scan.actual_rows
        if table is None or table.rows < LARGE_TABLE_ROWS or read == 0:
            continue
        if max(node.actual_rows, sides[1 - i].actual_rows) > MAX_KEPT_SHARE * read:
            continue
        own = localize(condition, {n for n in (scan.alias, scan.relation) if n})
        unindexed = unindexed_columns(own, table)
        if not unindexed:
            continue
        column = unindexed[0]
        findings.append(
            Finding(
                rule_id=RULE_ID,
                title=f"No index on {table.name}.{column} (join key)",
                explanation=(
                    f"PostgreSQL read all {read:,} rows of {table.name} to join on {column}, "
                    f"and the join kept {node.actual_rows:,}. There is no index on {column}, "
                    "so it cannot look up only the matching rows."
                ),
                suggestion=f"CREATE INDEX ON {table.name} ({column});",
                node=f"{node.node_type} on {condition} over {scan.summary()}",
                time_ms=scan.total_time_ms,
            )
        )
    return findings
