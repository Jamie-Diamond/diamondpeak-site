"""Tests for the plan nudge (lib/limiters.nudge) and its place in the weekly brief.
Run: python3 -m pytest ClaudeCoach/lib/test_limiter_nudge.py"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import limiters  # noqa: E402


def card(limiter, status, keys):
    return {"limiter": limiter, "headline": "x is the issue",
            "rows": [{"key": k, "status": status if k == limiter else "green"} for k in keys]}


RUN = ("fitness", "engine", "threshold", "efficiency", "endurance", "durability")
BIKE = ("fitness", "climbing", "day", "durability", "fuelling")
TRI = ("fitness", "swim", "bike", "run", "durability", "fuelling")


def test_each_card_type_gets_its_own_nudge():
    assert limiters.nudge(card("threshold", "amber", RUN))["nudge"].startswith("Run quality: give the Z3 share")
    assert limiters.nudge(card("climbing", "red", BIKE))["nudge"].startswith("Bike quality: threshold work as seated climbing")
    assert limiters.nudge(card("run", "red", TRI))["nudge"].startswith("Run: a short run straight off the long ride")
    assert limiters.nudge(card("endurance", "unshown", RUN))["area"] == "endurance"


def test_no_nudge_when_nothing_flagged_or_fitness_or_no_card():
    assert limiters.nudge(None) is None
    assert limiters.nudge({"limiter": None, "rows": []}) is None
    assert limiters.nudge(card("threshold", "green", RUN)) is None
    assert limiters.nudge(card("fitness", "red", RUN)) is None        # volume is the blueprint's job


def test_the_planner_is_told_it_never_changes_volume_or_targets():
    text = (Path(__file__).parent.parent / "scripts" / "stage1-plan.py").read_text()
    rule = text[text.index("- LIMITER:"):text.index("- MENSTRUAL:")]
    for must in ("never adds volume or TSS", "never takes a zone above", "never changes the phase"):
        assert must in rule
