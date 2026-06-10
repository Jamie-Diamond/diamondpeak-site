"""Tests for the strength programme blocks (planner + watchdog), opt-in via
profile `strength_programme` — signed off by Jamie 2026-06-10, including the
EVERY-WEEK equipment ask."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]            # ClaudeCoach/


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, REPO / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def gp():
    return _load("gp_strength", "scripts/generate-plan.py")


@pytest.fixture(scope="module")
def wd():
    return _load("wd_strength", "scripts/watchdog.py")


CFG = {"name": "Test Athlete", "race_name": "Race", "race_date": "2026-09-19",
       "day_rules": {"strength_max": 2}}


class TestPlannerStrengthBlock:
    def test_flagged_athlete_gets_programme_and_weekly_equipment_ask(self, gp):
        prompt = gp.build_prompt("x", CFG, {"strength_programme": True}, ctl_today=80.0)
        assert "STRENGTH PROGRAMME" in prompt
        assert "strength.md" in prompt
        assert "EQUIPMENT ASK" in prompt
        assert "Ask EVERY week" in prompt
        assert "Tier C" in prompt          # default content pushed, never empty slots

    def test_unflagged_athlete_unchanged(self, gp):
        prompt = gp.build_prompt("x", CFG, {}, ctl_today=80.0)
        assert "STRENGTH PROGRAMME" not in prompt


class TestWatchdogT11:
    def test_t11_present_with_target(self, wd):
        prompt = wd.build_prompt("x", "X", "Race", "2026-09-19", "1", strength_target=2)
        assert "T11" in prompt and "target 2/week" in prompt

    def test_t11_absent_without_target(self, wd):
        prompt = wd.build_prompt("x", "X", "Race", "2026-09-19", "1")
        assert "T11" not in prompt
