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


def _input(node: PlanNode) -> PlanNode | None:
    """The one child that feeds this node rows; InitPlans and SubPlans are not inputs."""
    children = [
        raw
        for raw in node.raw.get("Plans", [])
        if raw.get("Parent Relationship") not in ("InitPlan", "SubPlan")
    ]
    return node.child(children[0]) if len(children) == 1 else None


def _sort_below(limit: PlanNode) -> PlanNode | None:
    """The Sort that feeds the Limit, looking through a Gather Merge."""
    child = _input(limit)
    while child is not None and child.node_type == "Gather Merge":
        child = _input(child)
    return child if child is not None and child.node_type == "Sort" else None


def _scan_below(sort: PlanNode) -> PlanNode | None:
    """The scan of one table that feeds the sort, if that is all it sorts."""
    scan = _input(sort)
    return scan if scan is not None and scan.node_type in _SCANS and scan.relation else None


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
    # Not after "::" or ".", where the name is a type or another table's column.
    pattern = rf"(?<![\w$.:\"]){name}\)?(?:::[a-z ]+?(?:\([0-9, ]*\))?(?:\[\])*)?\s+=\s+(?!ANY\b)"
    return bool(re.search(pattern, mask_literals(condition)))


Order = tuple[bool, bool]  # (descending, nulls first)


def _order(text: str) -> Order:
    """Order of a sort key or index column from its ASC / DESC / NULLS words.

    Without NULLS, PostgreSQL puts nulls last going up and first going down.
    """
    words = text.upper()
    descending = bool(re.search(r"\bDESC\b", words))
    if "NULLS FIRST" in words:
        return descending, True
    return descending, descending and "NULLS LAST" not in words


def _index_orders(definition: str) -> list[Order]:
    """The order of each column in an index definition, as pg_get_indexdef prints it.

    Example: "CREATE INDEX ix ON t USING btree (a, b DESC)" -> [(False, False), (True, True)].
    """
    text = mask_literals(definition)
    using = re.search(r"\bUSING \w+ \(", text)
    pos = using.end() if using else text.find("(") + 1
    orders: list[Order] = []
    depth, start = 0, pos
    while 0 < pos <= len(text) - 1 and depth >= 0:
        char = text[pos]
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        if depth < 0 or (char == "," and depth == 0):
            orders.append(_order(text[start:pos]))
            start = pos + 1
        pos += 1
    return orders


def _has_index(table: Table, first: list[str], keys: list[tuple[str, str]]) -> bool:
    """True if an index starts with the columns of first (any order), then the sort keys.

    The sort keys must match in name and order, or all be reversed: an index can be read
    backwards.
    """
    wanted = [_order(order) for _, order in keys]
    reversed_ = [(not desc, not nulls_first) for desc, nulls_first in wanted]
    width = len(first) + len(keys)
    for ix in table.indexes:
        orders = _index_orders(ix.definition)
        orders += [(False, False)] * (len(ix.columns) - len(orders))
        if (
            set(ix.columns[: len(first)]) == set(first)
            and ix.columns[len(first) : width] == [col for col, _ in keys]
            and orders[len(first) : width] in (wanted, reversed_)
        ):
            return True
    return False


def check(node: PlanNode, catalog: Catalog) -> list[Finding]:
    """Return a finding if a Limit kept a few rows of a large sort of one table.

    PostgreSQL had to read and sort every matching row to know which come first. An index
    in the same order lets it read rows already sorted and stop after the first few.
    """
    if node.node_type != "Limit":
        return []
    sort = _sort_below(node)
    scan = _scan_below(sort) if sort else None
    child = _input(node)
    if sort is None or scan is None or child is None:
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
        or child.actual_rows - node.actual_rows >= DEEP_OFFSET_ROWS
    ):
        return []

    names = {n for n in (scan.alias, scan.relation) if n}
    keys = [sort_key_column(key, names) for key in sort.raw.get("Sort Key", [])]
    if not keys or None in keys or any(col not in table.columns for col, _ in keys):
        return []
    sort_columns = [col for col, _ in keys]
    first = [col for col in _equality_columns(scan, table) if col not in sort_columns]
    if _has_index(table, first, keys):
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
