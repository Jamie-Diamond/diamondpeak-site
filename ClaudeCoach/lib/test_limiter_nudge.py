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
BIKE = ("fitness", "climbing", "durability", "fuelling")
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


# ── Where the minutes are (levers) ────────────────────────────────────────────

def test_run_levers_for_a_3h24_marathon():
    run = {"predictions": {"marathon": {"now_s": 12266}}, "engine": {"vo2max": 59.3}}
    lv = {x["label"]: x["minutes"] for x in limiters.run_levers(42195, 10800, run)}
    assert lv["Threshold +3%"] == 6 and lv["Economy +2%"] == 4
    assert "VO2max +2" not in lv                       # past 10 km, threshold decides
    assert 1 <= lv["10 km more running a week"] <= 4
    five = {x["label"] for x in limiters.run_levers(5000, 1100, {"engine": {"vo2max": 55}})}
    assert "VO2max +2" in five


def test_bike_levers_on_the_mallorca_312():
    import course as cs
    c = cs.load(cs.RACES_DIR / "mallorca-312.json")
    lv = {x["label"]: x for x in limiters.bike_levers(c, {"ftp": 211, "weight": 93.0})}
    assert lv["FTP +10 W"]["minutes"] >= 5 and lv["3 kg lighter"]["minutes"] >= 3
    assert "km 97" in lv["FTP +10 W"]["why"]
    assert limiters.bike_levers(None, {"ftp": 211, "weight": 93}) == []


def test_levers_ride_on_every_card():
    d = limiters.diagnose(42195, 10800, "sub 3", {"weeklyKm": 40, "predictions": {"marathon": {"now_s": 12266}}},
                          {"now": 55, "lo": 70, "hi": 95}, 0.7)
    assert d["levers"] and d["levers"][0]["minutes"] >= d["levers"][-1]["minutes"]
    t = limiters.diagnose_tri("ironman", {"swim": 4020, "bike": 17040, "run": 12600, "t": 330},
                              {"swim": {"time_s": 4100, "levers": [{"label": "CSS 3 s/100 m faster", "minutes": 2.0}]},
                               "run": {"time_s": 13200, "levers": [{"label": "Run fitness +2%", "minutes": 4.3}]}},
                              None)
    assert [x["label"] for x in t["levers"]] == ["Run fitness +2%", "CSS 3 s/100 m faster"]
