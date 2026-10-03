from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from sqd.catalog import Catalog

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> tuple[dict[str, Any], Catalog]:
    """Plan JSON and catalog captured from the demo database with `sqd` internals."""
    data = json.loads((FIXTURES / f"{name}.json").read_text())
    return data["plan"], Catalog.from_dict(data["catalog"])


@pytest.fixture
def fixture_loader():
    return load_fixture
