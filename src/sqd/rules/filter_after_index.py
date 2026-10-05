"""Rule 6: an index finds many rows, then a filter on other columns throws most of them away."""

from __future__ import annotations

import re

from sqd.catalog import Catalog, Index
from sqd.plan import PlanNode, referenced_columns
from sqd.rules.base import LARGE_TABLE_ROWS, Finding
from sqd.rules.missing_index import MAX_KEPT_SHARE, wrapped_columns

RULE_ID = "filter-after-index"

_INDEX_SCANS = frozenset({"Index Scan", "Index Only Scan", "Bitmap Heap Scan"})


def _index_used(node: PlanNode, catalog: Catalog) -> Index | None:
    """The one index the scan used. None for BitmapAnd / BitmapOr (several indexes)."""
    name = node.raw.get("Index Name")
    if node.node_type == "Bitmap Heap Scan":
        children = node.raw.get("Plans", [])
        if len(children) == 1 and children[0].get("Node Type") == "Bitmap Index Scan":
            name = children[0].get("Index Name")
    table = catalog.get(node.relation)
    if table is None or not name:
        return None
    return next((ix for ix in table.indexes if ix.name == name), None)


def _compared_with_equals(expression: str, column: str) -> bool:
    """True if the column is tested with = (or = ANY), which belongs first in an index."""
    return bool(re.search(rf"\b{column}\)?(?:::[a-z ]+?)?\s+=\s", expression))


def check(node: PlanNode, catalog: Catalog) -> list[Finding]:
    """Return a finding when a filter after an index scan drops most of the rows it found.

    Each dropped row was fetched from the table only to be thrown away. A combined index
    that also holds the filter columns lets PostgreSQL skip them inside the index.
    """
    if node.node_type not in _INDEX_SCANS or not node.filter:
        return []
    removed = node.rows_removed_by_filter
    found = node.actual_rows + removed
    if removed < LARGE_TABLE_ROWS or node.actual_rows > MAX_KEPT_SHARE * found:
        return []
    table = catalog.get(node.relation)
    index = _index_used(node, catalog)
    # Expression indexes (a None column) are left alone: rebuilding them is not a plain list.
    if table is None or index is None or None in index.columns:
        return []
    filter_ = node.own_filter
    assert filter_ is not None
    wrapped = wrapped_columns(filter_, table.columns)
    extra = [
        col
        for col in referenced_columns(filter_, table.columns)
        if col not in wrapped and col not in index.columns
    ]
    if not extra:
        return []

    # Equality columns first, then the index's own columns, then range columns.
    equal = [col for col in extra if _compared_with_equals(filter_, col)]
    ranges = [col for col in extra if col not in equal]
    columns = equal + [col for col in index.columns if col] + ranges
    kept_pct = 100 * node.actual_rows / found
    return [
        Finding(
            rule_id=RULE_ID,
            title=f"{index.name} found {found:,} rows, the filter kept {node.actual_rows:,}",
            explanation=(
                f"PostgreSQL used {index.name} to find {found:,} rows of {table.name}, read "
                f"each one from the table, then threw away {removed:,} because of the filter "
                f"on {', '.join(extra)} (kept {kept_pct:.1f}%). The index does not hold "
                f"{', '.join(extra)}, so it cannot skip those rows itself."
            ),
            suggestion=(
                "Add an index that holds the filter columns too, equality columns first: "
                f"CREATE INDEX ON {table.name} ({', '.join(columns)});"
            ),
            node=node.summary(),
            time_ms=node.total_time_ms,
        )
    ]
