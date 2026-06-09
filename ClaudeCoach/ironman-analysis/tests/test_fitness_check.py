"""Tests for the mid-plan fitness check in scripts/generate-blueprint.py.

The check must compare current CTL against the entry range of the phase
containing TODAY, not the plan's first phase (a mid-plan regen used to demand
a coaching decision for CTL 80 vs Base 55–70 nine weeks into the plan).
"""
from __future__ import annotations

import importlib.util
from datetime import date, timedelta
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]            # ClaudeCoach/
GB = REPO / "scripts" / "generate-blueprint.py"


@pytest.fixture(scope="module")
def gb():
    spec = importlib.util.spec_from_file_location("generate_blueprint", GB)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _phases(today):
    """Base finished 3 weeks ago; Build contains today; Taper later."""
    return [
        {"name": "Base",  "start": (today - timedelta(weeks=9)).isoformat(),
         "end": (today - timedelta(weeks=3, days=1)).isoformat()},
        {"name": "Build", "start": (today - timedelta(weeks=3)).isoformat(),
         "end": (today + timedelta(weeks=3)).isoformat()},
        {"name": "Taper", "start": (today + timedelta(weeks=3, days=1)).isoformat(),
         "end": (today + timedelta(weeks=5)).isoformat()},
    ]


class TestFitnessCheck:
    def test_mid_plan_checks_current_phase_not_first(self, gb):
        # CTL 80 is over Base (55–70) but inside Build (70–85): no decision needed
        assert gb.fitness_check("x", "Full Ironman", 80.0, _phases(date.today()), None) is None

    def test_mid_plan_overfit_for_current_phase_still_fires(self, gb):
        # 1.10 × Build high (85) = 93.5 — CTL 95 exceeds it
        note = gb.fitness_check("x", "Full Ironman", 95.0, _phases(date.today()), None)
        assert note and "AWAITING_DECISION" in note and "Build" in note

    def test_before_plan_start_uses_first_phase(self, gb):
        future = [{"name": p["name"],
                   "start": (date.fromisoformat(p["start"]) + timedelta(weeks=12)).isoformat(),
                   "end": (date.fromisoformat(p["end"]) + timedelta(weeks=12)).isoformat()}
                  for p in _phases(date.today())]
        note = gb.fitness_check("x", "Full Ironman", 80.0, future, None)
        assert note and "Base" in note  # original pre-plan behaviour preserved

    def test_in_taper_never_flags_overfitness(self, gb):
        today = date.today()
        taper_now = [{"name": "Taper", "start": (today - timedelta(days=3)).isoformat(),
                      "end": (today + timedelta(weeks=2)).isoformat()}]
        assert gb.fitness_check("x", "Full Ironman", 120.0, taper_now, None) is None
