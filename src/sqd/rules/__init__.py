"""Rules engine: run every rule over every plan node."""

from __future__ import annotations

from typing import Any

from sqd.catalog import Catalog
from sqd.plan import walk
from sqd.rules import (
    date_filter,
    deep_offset,
    function_on_column,
    leading_wildcard,
    missing_index,
)
from sqd.rules.base import Finding, Rule

RULES: list[Rule] = [
    missing_index.check,
    function_on_column.check,
    leading_wildcard.check,
    deep_offset.check,
    date_filter.check,
]


def run_rules(plan: dict[str, Any], catalog: Catalog) -> list[Finding]:
    """All findings for a plan, slowest node first."""
    findings = [f for node in walk(plan) for rule in RULES for f in rule(node, catalog)]
    return sorted(findings, key=lambda f: f.time_ms, reverse=True)


__all__ = ["Finding", "RULES", "run_rules"]
