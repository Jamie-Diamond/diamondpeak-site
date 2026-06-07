"""Regression guard for remediation-plan WS A (load-maths consolidation).

The forward plan-generation maths (compute_required_tss, compute_projected_ctl,
derive_phase_ctl_targets, compute_race_min_ctl) must have exactly ONE
implementation — in primitives/load.py. generate-plan.py previously carried an
inline copy; this test fails if that copy ever returns, preventing the "two
Banister implementations" drift this workstream removed.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]            # ClaudeCoach/
GENERATE_PLAN = REPO / "scripts" / "generate-plan.py"

CONSOLIDATED_FNS = [
    "compute_required_tss",
    "compute_projected_ctl",
    "derive_phase_ctl_targets",
    "compute_race_min_ctl",
]


@pytest.fixture(scope="module")
def source() -> str:
    return GENERATE_PLAN.read_text()


def test_generate_plan_exists():
    assert GENERATE_PLAN.exists(), f"missing {GENERATE_PLAN}"


def test_generate_plan_imports_from_primitives(source):
    assert "from primitives.load import" in source, (
        "generate-plan.py must import the load maths from the tested package"
    )


@pytest.mark.parametrize("fn", CONSOLIDATED_FNS)
def test_no_inline_redefinition(source, fn):
    # No `def <fn>(` anywhere in generate-plan.py — it must come from the import.
    assert not re.search(rf"^\s*def\s+{re.escape(fn)}\s*\(", source, re.MULTILINE), (
        f"generate-plan.py redefines {fn} inline — it must use primitives.load instead"
    )
