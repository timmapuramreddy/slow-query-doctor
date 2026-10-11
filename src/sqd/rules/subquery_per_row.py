"""Rule 8: a subquery runs again for every row of the outer query and reads a lot each time."""

from __future__ import annotations

import re

from sqd.catalog import Catalog
from sqd.plan import PlanNode, mask_literals, walk
from sqd.rules.base import Finding

RULE_ID = "subquery-per-row"

# Fire when the subquery ran at least this many times and read at least this many rows in
# total. A cheap lookup per row (a few rows from an index) stays under the second limit.
MIN_LOOPS = 100
MIN_ROWS_READ = 1_000_000


def _rows_read(subplan: PlanNode) -> int:
    """Most rows any node of the subquery produced or filtered out, over all its runs."""
    return max(n.actual_rows + n.rows_removed_by_filter for n in walk(subplan.raw))


def _is_not_in(filter_: str | None, name: str) -> bool:
    """True if the filter tests this subplan with NOT IN, as PostgreSQL 17 or 16 prints it.

    17: "(NOT (ANY (id = (SubPlan 1).col1)))"; 16 and older: "(NOT (SubPlan 1))".
    A hashed subplan ("hashed SubPlan 1") runs once, so it is not this pattern.
    """
    if not filter_:
        return False
    text = mask_literals(filter_)
    name = re.escape(name)
    return bool(
        re.search(rf"NOT \(ANY \((?:(?!SubPlan).)*= \({name}\)\.col\d+\)\)", text)
        or re.search(rf"NOT \({name}\)", text)
    )


def check(node: PlanNode, catalog: Catalog) -> list[Finding]:
    """Return a finding for each subquery of this node that ran many times and read a lot.

    PostgreSQL runs a correlated subquery, or a NOT IN subquery too big to keep in memory
    (work_mem), once per outer row. A join or NOT EXISTS lets it work on all rows at once.
    """
    findings: list[Finding] = []
    for raw in node.raw.get("Plans", []):
        if raw.get("Parent Relationship") != "SubPlan":
            continue
        subplan = node.child(raw)
        read = _rows_read(subplan)
        if subplan.loops < MIN_LOOPS or read < MIN_ROWS_READ:
            continue
        name = raw.get("Subplan Name", "SubPlan")
        runs = (
            f"ran once for each of {subplan.loops:,} rows of the outer query and read about "
            f"{read:,} rows in total"
        )
        if _is_not_in(node.filter, name):
            title = f"NOT IN subquery ran {subplan.loops:,} times"
            explanation = (
                f"The NOT IN subquery ({name}) {runs}. Its result was too big to keep in "
                "memory (work_mem), so PostgreSQL searched it again for every row."
            )
            suggestion = (
                "Rewrite NOT IN (SELECT x FROM ...) as NOT EXISTS (SELECT 1 FROM ... WHERE "
                "x = <outer column>). PostgreSQL runs NOT EXISTS as one anti join. The two "
                "differ only if the subquery can return NULL: then NOT IN returns no rows."
            )
        else:
            title = f"Subquery ran {subplan.loops:,} times"
            explanation = (
                f"The subquery ({name}) {runs}. PostgreSQL runs it again for every row "
                "instead of once for all of them."
            )
            suggestion = (
                "Rewrite the subquery as a JOIN (for a value per row, join to a subquery "
                "grouped by the join key) or as EXISTS (SELECT 1 ...), so PostgreSQL can "
                "work on all rows at once."
            )
        findings.append(
            Finding(
                rule_id=RULE_ID,
                title=title,
                explanation=explanation,
                suggestion=suggestion,
                node=f"{name} ({subplan.node_type}, {subplan.loops:,} runs) in {node.summary()}",
                time_ms=subplan.total_time_ms,
            )
        )
    return findings
