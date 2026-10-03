"""Shared types for rules."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from sqd.catalog import Catalog
from sqd.plan import PlanNode

# Tables smaller than this are cheap to scan; rules about scans ignore them.
LARGE_TABLE_ROWS = 10_000


@dataclass(frozen=True)
class Finding:
    """One problem found in a plan, in plain words, with the node that triggered it."""

    rule_id: str
    title: str
    explanation: str
    suggestion: str
    node: str
    time_ms: float


Rule = Callable[[PlanNode, Catalog], list[Finding]]
