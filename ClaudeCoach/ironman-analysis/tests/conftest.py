"""Test fixtures."""
import json
from datetime import date
from pathlib import Path

import pytest

FIXTURE_DIR = Path(__file__).parent.parent / "fixtures"


@pytest.fixture
def april_14_duplicate():
    return json.loads((FIXTURE_DIR / "april_14_duplicate.json").read_text())


@pytest.fixture
def fitness_90d():
    return json.loads(
        (FIXTURE_DIR / "fitness_2026_01_25_to_2026_04_25.json").read_text()
    )["rows"]


@pytest.fixture
def constant_tss_42d():
    """42 consecutive days of TSS=100 starting 2026-01-01."""
    return {date(2026, 1, 1) + __import__("datetime").timedelta(days=i): 100.0 for i in range(42)}
