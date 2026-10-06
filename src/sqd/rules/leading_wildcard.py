"""Rule 3: LIKE '%x' starts with a wildcard, so a normal B-tree index cannot be used."""

from __future__ import annotations

from sqd.catalog import Catalog, Table
from sqd.plan import PlanNode, leading_wildcard_columns, quote_name
from sqd.rules.base import Finding
from sqd.rules.missing_index import is_selective_seq_scan, table_rows

RULE_ID = "leading-wildcard"


def _index_items(definition: str) -> list[str]:
    """Key items of an index: 'USING gin (a gin_trgm_ops, b)' -> ['a gin_trgm_ops', 'b']."""
    start = definition.find("(", definition.find(" USING "))
    items: list[str] = []
    depth = 0
    current = ""
    for char in definition[start + 1 :]:
        if char == "(":
            depth += 1
        elif char == ")":
            if depth == 0:
                break
            depth -= 1
        elif char == "," and depth == 0:
            items.append(current.strip())
            current = ""
            continue
        current += char
    items.append(current.strip())
    return items


def _has_trigram_index(table: Table, column: str) -> bool:
    """True if a pg_trgm index covers the column in any position (it can serve LIKE '%x')."""
    return any(
        name == column and "_trgm_ops" in item
        for ix in table.indexes
        if "_trgm_ops" in ix.definition
        for name, item in zip(ix.columns, _index_items(ix.definition), strict=False)
    )


def check(node: PlanNode, catalog: Catalog) -> list[Finding]:
    """Return a finding for each LIKE / ILIKE pattern that starts with % or _."""
    if not is_selective_seq_scan(node, catalog):
        return []
    table = catalog.get(node.relation)
    filter_ = node.own_filter
    assert table is not None and filter_ is not None
    rows = table_rows(node, table)

    findings: list[Finding] = []
    for col, pattern in leading_wildcard_columns(filter_, table.columns):
        if _has_trigram_index(table, col):
            continue
        index_note = (
            f"Even the index on {col} cannot help: "
            if table.has_leading_index(col)
            else "A normal index would not help either: "
        )
        findings.append(
            Finding(
                rule_id=RULE_ID,
                title=f"Leading wildcard '{pattern}' on {table.name}.{col}",
                explanation=(
                    f"The pattern starts with a wildcard, so a match can begin anywhere in "
                    f"{col}. {index_note}a B-tree index is sorted from the first character, "
                    f"and the first character is unknown. PostgreSQL read every row of "
                    f"{table.name} (about {rows:,}) and tested each one."
                ),
                suggestion=(
                    f"If you know how the value starts, drop the leading wildcard. For "
                    f"'contains' or 'ends with' search, add a trigram index: "
                    f"CREATE EXTENSION IF NOT EXISTS pg_trgm; "
                    f"CREATE INDEX ON {quote_name(table.name)} "
                    f"USING gin ({quote_name(col)} gin_trgm_ops);"
                ),
                node=node.summary(),
                time_ms=node.total_time_ms,
            )
        )
    return findings
