"""What the rules need to know about tables: row counts, columns and indexes."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Index:
    """One index. A None entry in columns is an expression (for example lower(email))."""

    name: str
    columns: list[str | None]
    definition: str

    @property
    def leading_column(self) -> str | None:
        return self.columns[0] if self.columns else None


@dataclass(frozen=True)
class Table:
    name: str
    rows: int
    columns: list[str]
    indexes: list[Index] = field(default_factory=list)

    def has_leading_index(self, column: str) -> bool:
        """True if some index starts with this plain column, so a filter on it can use it."""
        return any(ix.leading_column == column for ix in self.indexes)

    def has_expression_index(self, expression: str) -> bool:
        """True if an expression index definition contains this text, e.g. 'lower(email)'."""
        needle = expression.replace(" ", "").lower()
        return any(
            ix.leading_column is None and needle in ix.definition.replace(" ", "").lower()
            for ix in self.indexes
        )


@dataclass(frozen=True)
class Catalog:
    tables: dict[str, Table]

    def get(self, name: str | None) -> Table | None:
        return self.tables.get(name) if name else None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Catalog:
        """Build from the JSON shape used in test fixtures."""
        return cls(
            tables={
                name: Table(
                    name=name,
                    rows=t["rows"],
                    columns=t["columns"],
                    indexes=[
                        Index(name=i["name"], columns=i["columns"], definition=i["definition"])
                        for i in t.get("indexes", [])
                    ],
                )
                for name, t in data.items()
            }
        )

    @classmethod
    def from_json(cls, text: str | bytes) -> Catalog:
        """Build from a catalog file made by running `sqd catalog-sql`, with plain errors."""
        how = "Make it with `sqd catalog-sql plan.json > catalog.sql` and "
        how += "`psql -XqAt -f catalog.sql -o catalog.json`."
        try:
            data = json.loads(text)
        except ValueError:
            raise ValueError(f"The catalog file is not JSON. {how}") from None
        try:
            return cls.from_dict(data)
        except (AttributeError, KeyError, TypeError):
            raise ValueError(f"The catalog file is not in the expected shape. {how}") from None
