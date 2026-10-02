"""The empty-week fallback names its reason in the athlete's terms (Calum, 6 Sep 2026:
"There was no target for this week, how can I be off load?"). "load 29.4% off target"
was the planner's own aim, never agreed, and hid that the week was 30% too BIG."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]            # ClaudeCoach/
sys.path.insert(0, str(REPO / "lib"))
sys.path.insert(0, str(REPO / "ironman-analysis"))
_spec = importlib.util.spec_from_file_location("stage1_plan", REPO / "scripts" / "stage1-plan.py")
S1 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(S1)


def test_over_and_under_in_plain_words():
    assert S1._athlete_reason("load 29.4% off target") == \
        "it came out about 30% bigger than I meant to give you"
    assert S1._athlete_reason("load -18.0% off target") == \
        "it came out about 20% lighter than I meant to give you"


def test_never_says_target():
    for w in ("load 29.4% off target", "load -3% off target", "load 120% off target"):
        assert "target" not in S1._athlete_reason(w)


def test_other_reasons_pass_through():
    assert S1._athlete_reason("rule(hard): long ride missing") == "long ride missing"
    assert S1._athlete_reason("") == "it does not hold together the way I want it to"
