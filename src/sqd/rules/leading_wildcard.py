"""Rule 3: LIKE '%x' starts with a wildcard, so a normal B-tree index cannot be used."""

from __future__ import annotations

from sqd.catalog import Catalog, Table
from sqd.plan import PlanNode, leading_wildcard_columns
from sqd.rules.base import Finding
from sqd.rules.missing_index import is_selective_seq_scan

RULE_ID = "leading-wildcard"


def _has_trigram_index(table: Table, column: str) -> bool:
    """True if a pg_trgm index covers the column (it can serve LIKE '%x')."""
    return any(
        "_trgm_ops" in ix.definition and f"({column} " in ix.definition for ix in table.indexes
    )


def check(node: PlanNode, catalog: Catalog) -> list[Finding]:
    """Return a finding for each LIKE / ILIKE pattern that starts with % or _."""
    if not is_selective_seq_scan(node, catalog):
        return []
    table = catalog.get(node.relation)
    assert table is not None and node.filter is not None

    findings: list[Finding] = []
    for col, pattern in leading_wildcard_columns(node.filter, table.columns):
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
                    f"{table.name} (about {table.rows:,}) and tested each one."
                ),
                suggestion=(
                    f"If you know how the value starts, drop the leading wildcard. For "
                    f"'contains' or 'ends with' search, add a trigram index: "
                    f"CREATE EXTENSION IF NOT EXISTS pg_trgm; "
                    f"CREATE INDEX ON {table.name} USING gin ({col} gin_trgm_ops);"
                ),
                node=node.summary(),
                time_ms=node.total_time_ms,
            )
        )
    return findings
