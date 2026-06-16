"""Tests for the strength programme (two-stage engine + watchdog), opt-in via
profile `strength_programme` — signed off by Jamie 2026-06-10, including the
EVERY-WEEK equipment ask. Ported from the retired generate-plan.py to stage1-plan.py
(15 Jun): the brief carries the strength block, the proposer is instructed to place
the sessions, and the weekly message carries the equipment ask."""
from __future__ import annotations

import importlib.util
from datetime import date
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]            # ClaudeCoach/


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, REPO / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def s1():
    return _load("s1_strength", "scripts/stage1-plan.py")


@pytest.fixture(scope="module")
def wd():
    return _load("wd_strength", "scripts/watchdog.py")


class TestPlannerStrengthBlock:
    def test_prompt_carries_strength_rule(self, s1):
        # The proposer must always be told how to place strength when the brief flags it.
        prompt = s1.build_prompt(
            "x", {"weekly_tss_target": 400, "strength_programme": {"sessions_per_week": 2}},
            date(2026, 6, 22))
        assert "STRENGTH" in prompt and "strength_programme" in prompt

    def test_week_message_has_equipment_ask_when_flagged(self, s1):
        built = {"week_start": "2026-06-22", "total_tss": 400, "sessions": []}
        msg = s1._week_message(
            {"phase": "build", "strength_programme": {"sessions_per_week": 2}}, built)
        assert "equipment" in msg.lower()        # the EVERY-WEEK ask, never silently dropped

    def test_no_equipment_ask_when_unflagged(self, s1):
        built = {"week_start": "2026-06-22", "total_tss": 400, "sessions": []}
        msg = s1._week_message({"phase": "build", "strength_programme": None}, built)
        assert "equipment" not in msg.lower()


class TestWatchdogT11:
    def test_t11_present_with_target(self, wd):
        prompt = wd.build_prompt("x", "X", "Race", "2026-09-19", "1", strength_target=2)
        assert "T11" in prompt and "target 2/week" in prompt

    def test_t11_absent_without_target(self, wd):
        prompt = wd.build_prompt("x", "X", "Race", "2026-09-19", "1")
        assert "T11" not in prompt
