"""Rule 4: deep OFFSET paging makes PostgreSQL produce and throw away every skipped row."""

from __future__ import annotations

from sqd.catalog import Catalog
from sqd.plan import PlanNode
from sqd.rules.base import Finding

RULE_ID = "deep-offset"

# Fire when a Limit throws away at least this many rows from its input.
DEEP_OFFSET_ROWS = 10_000


def check(node: PlanNode, catalog: Catalog) -> list[Finding]:
    """Return a finding if a Limit node discarded many rows, which is what OFFSET does.

    The plan does not show the OFFSET value, but a Limit stops pulling rows once it has
    enough, so every row its input produced beyond what it returned was skipped.
    """
    children = node.raw.get("Plans", [])
    if node.node_type != "Limit" or not children:
        return []
    child = PlanNode(raw=children[0], depth=node.depth + 1)
    skipped = child.actual_rows - node.actual_rows
    if skipped < DEEP_OFFSET_ROWS:
        return []

    return [
        Finding(
            rule_id=RULE_ID,
            title=f"Deep OFFSET: read {child.actual_rows:,} rows to return {node.actual_rows:,}",
            explanation=(
                f"OFFSET does not jump ahead. PostgreSQL produced {child.actual_rows:,} rows in "
                f"order and threw away the first {skipped:,}. Every later page is slower than "
                "the one before it."
            ),
            suggestion=(
                "Use keyset paging: remember the sort key of the last row on the previous page "
                "and ask for the rows after it, for example "
                "WHERE id > :last_id ORDER BY id LIMIT 20. It needs an index on the sort columns."
            ),
            node=f"Limit over {child.summary()}",
            time_ms=node.total_time_ms,
        )
    ]
