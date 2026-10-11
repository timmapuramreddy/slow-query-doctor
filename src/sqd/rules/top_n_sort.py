"""Rule 9: ORDER BY ... LIMIT sorts every matching row of a large table to return a few."""

from __future__ import annotations

import re

from sqd.catalog import Catalog, Table
from sqd.plan import (
    PlanNode,
    localize,
    mask_literals,
    quote_name,
    referenced_columns,
    sort_key_column,
)
from sqd.rules.base import LARGE_TABLE_ROWS, Finding
from sqd.rules.deep_offset import DEEP_OFFSET_ROWS
from sqd.rules.missing_index import (
    MAX_KEPT_SHARE,
    is_selective_seq_scan,
    table_rows,
    wrapped_columns,
)

RULE_ID = "top-n-sort"

_SCANS = frozenset({"Seq Scan", "Index Scan", "Index Only Scan", "Bitmap Heap Scan"})


def _below(node: PlanNode, node_type: str) -> PlanNode | None:
    """The only child of this node, looking through a Gather Merge, if it has node_type."""
    children = node.raw.get("Plans", [])
    if len(children) != 1:
        return None
    child = node.child(children[0])
    if child.node_type == "Gather Merge":
        return _below(child, node_type)
    return child if child.node_type == node_type else None


def _scan_below(sort: PlanNode) -> PlanNode | None:
    """The scan of one table that feeds the sort, if that is all it sorts."""
    children = sort.raw.get("Plans", [])
    if len(children) != 1:
        return None
    scan = sort.child(children[0])
    return scan if scan.node_type in _SCANS and scan.relation else None


def _equality_columns(scan: PlanNode, table: Table) -> list[str]:
    """Bare columns the scan tests with = against one value, from its filter and conditions.

    An index on them first returns the matching rows already in sort order. Nothing is
    returned if a condition has OR, because then one index prefix does not cover the rows.
    """
    names = {n for n in (scan.alias, scan.relation) if n}
    parts = [scan.raw.get(key) for key in ("Index Cond", "Recheck Cond", "Filter")]
    conditions = [localize(p, names) for p in parts if p]
    if any(" OR " in mask_literals(c) for c in conditions):
        return []
    found: list[str] = []
    for condition in conditions:
        # Bare columns only: not inside a function, a date cast or a LIKE.
        wrapped = wrapped_columns(condition, table.columns)
        for column in referenced_columns(condition, table.columns):
            if (
                column not in wrapped
                and column not in found
                and _equals_one_value(condition, column)
            ):
                found.append(column)
    return found


def _equals_one_value(condition: str, column: str) -> bool:
    """True for "column = value", not "column = ANY (...)": a list of values loses the order."""
    name = re.escape(quote_name(column))
    pattern = rf"(?<![\w$.\"]){name}\)?(?:::[a-z ]+?(?:\[\])*)?\s+=\s+(?!ANY\b)"
    return bool(re.search(pattern, mask_literals(condition)))


def _has_index(table: Table, first: list[str], then: list[str]) -> bool:
    """True if an index starts with the columns of first (any order), then those of then."""
    width = len(first) + len(then)
    return any(
        set(ix.columns[: len(first)]) == set(first) and ix.columns[len(first) : width] == then
        for ix in table.indexes
    )


def check(node: PlanNode, catalog: Catalog) -> list[Finding]:
    """Return a finding if a Limit kept a few rows of a large sort of one table.

    PostgreSQL had to read and sort every matching row to know which come first. An index
    in the same order lets it read rows already sorted and stop after the first few.
    """
    if node.node_type != "Limit":
        return []
    sort = _below(node, "Sort")
    scan = _scan_below(sort) if sort else None
    if sort is None or scan is None:
        return []
    table = catalog.get(scan.relation)
    sorted_rows = scan.actual_rows
    if (
        table is None
        or table_rows(scan, table) < LARGE_TABLE_ROWS
        or sorted_rows < LARGE_TABLE_ROWS
        or node.actual_rows > MAX_KEPT_SHARE * sorted_rows
        # A selective filter is rule 1's; a deep OFFSET is rule 4's.
        or is_selective_seq_scan(scan, catalog)
        or node.child(node.raw["Plans"][0]).actual_rows - node.actual_rows >= DEEP_OFFSET_ROWS
    ):
        return []

    names = {n for n in (scan.alias, scan.relation) if n}
    keys = [sort_key_column(key, names) for key in sort.raw.get("Sort Key", [])]
    if not keys or None in keys or any(col not in table.columns for col, _ in keys):
        return []
    sort_columns = [col for col, _ in keys]
    first = [col for col in _equality_columns(scan, table) if col not in sort_columns]
    if _has_index(table, first, sort_columns):
        return []

    # A btree index can be read backwards, so one shared default order needs no ASC / DESC.
    orders = {order for _, order in keys}
    plain = orders == {""} or orders == {" DESC"}
    columns = [quote_name(col) for col in first] + [
        quote_name(col) + ("" if plain else order) for col, order in keys
    ]
    by = ", ".join(sort.raw["Sort Key"])
    return [
        Finding(
            rule_id=RULE_ID,
            title=f"Sorted {sorted_rows:,} rows of {table.name} to return {node.actual_rows:,}",
            explanation=(
                f"To return the first {node.actual_rows:,} rows by {by}, PostgreSQL sorted "
                f"all {sorted_rows:,} matching rows of {table.name}. No index holds the rows in "
                "that order, so it could not read the first few and stop."
            ),
            suggestion=(
                "Add an index in the query's order, filter columns tested with = first: "
                f"CREATE INDEX ON {quote_name(table.name)} ({', '.join(columns)});"
            ),
            node=f"Limit over Sort ({by}) over {scan.summary()}",
            time_ms=node.total_time_ms,
        )
    ]
