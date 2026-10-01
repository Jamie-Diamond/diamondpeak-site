"""Tests for lib/season_view.py. Run: python3 -m pytest ClaudeCoach/lib/test_season_view.py"""
from datetime import date

import season_view as sv


def test_hold_target_by_duration():
    assert sv.hold_target(10, 250) == {"if_lo": 0.62, "if_hi": 0.67, "hours": 10, "w_lo": 155, "w_hi": 170}
    assert sv.hold_target(3, None) == {"if_lo": 0.80, "if_hi": 0.85, "hours": 3}


def test_season_races_goals_and_predictions(monkeypatch):
    import planning_pause
    import races
    monkeypatch.setattr(planning_pause, "is_paused", lambda slug, cfg=None: False)
    monkeypatch.setattr(races, "load_races", lambda slug, config=None: [
        {"name": "Old IM", "date": "2025-09-20", "distance": "Full Ironman"},
        {"name": "Brighton Marathon", "date": "2027-04-04", "priority": "A", "distance": "Marathon"},
        {"name": "Half PB", "date": "2027-03-07", "priority": "B", "distance": "Half Marathon", "goal": "1:25"},
        {"name": "Gravel day", "date": "2027-02-01", "priority": "C", "distance": "Gravel 120 km"}])
    cfg = {"race_goal": "sub 3", "plan_start": "2026-11-02", "race_date": "2027-04-04",
           "phase_tss": {"base_end_week": 6, "build_end_week": 14}}
    p = sv.payload("x", cfg, {"run_threshold_pace_per_km": "4:02/km - tested"},
                   {"kpi": {"ctl": 60}, "resolvedFtp": 300}, today=date(2026, 10, 1))
    names = [r["name"] for r in p["races"]]
    assert names == ["Gravel day", "Half PB", "Brighton Marathon"]           # past race dropped
    a = p["aRace"]
    assert a["name"] == "Brighton Marathon" and a["goal_s"] == 10800 and 10700 < a["now_s"] < 11000
    half = p["races"][1]
    assert half["priority"] == "B" and half["goal_s"] == 5100 and half["now_s"]
    grav = p["races"][0]
    assert grav["kind"] == "bike" and grav["hold"]["w_lo"] and "now_s" not in grav
    assert [x["name"] for x in p["phases"]][:2] == ["Base", "Build"] and p["phases"][-1]["name"] == "Taper"


def test_tanda_and_race_day_predictions(monkeypatch):
    import planning_pause
    import races
    # By hand: 17.1 + 140 * e^(-0.318) + 0.55 * 300 = 283.96 s/km x 42.195 km = 3:19:42
    assert sv.tanda_marathon_s(60, 300) == 11982
    monkeypatch.setattr(planning_pause, "is_paused", lambda slug, cfg=None: False)
    monkeypatch.setattr(races, "load_races", lambda slug, config=None: [
        {"name": "Parkrun", "date": "2026-11-14", "priority": "C", "distance": "5k"},
        {"name": "Brighton Marathon", "date": "2027-04-04", "priority": "A", "distance": "Marathon"}])
    recent = [{"date": f"2026-09-{d:02d}", "sport": "Run", "dist": 10.0, "dur": 50} for d in range(3, 30, 3)]
    p = sv.payload("x", {"race_goal": "sub 3"}, {"run_threshold_pace_per_km": "4:02"},
                   {"recent": recent}, today=date(2026, 10, 1))
    c, a = p["races"]
    assert a["method"] == "tanda" and a["raceday_s"] < a["now_s"]          # builds to 95 km/wk
    assert a["volume"]["k_now"] == 11.2 and a["volume"]["pace_s_km"] == 300
    assert c["method"] == "riegel" and c["raceday_s"] <= c["now_s"]
    assert c["now_s"] - c["raceday_s"] < (a["now_s"] - a["raceday_s"]) * 0.2   # a 5k gains little
