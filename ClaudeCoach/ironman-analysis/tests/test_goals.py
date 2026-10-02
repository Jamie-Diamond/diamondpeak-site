"""Goals without a race (lib/goals.py; Jamie, 1 Oct 2026: "make sure if an athlete has the
ask to just maintain fitness, or improve FTP or something non race related you know what
to do"). Two testers with no race signed up the same day.

Pinned here: the 6-week block shape and its loads, the end-of-block test reaching the
week's bookings, a race taking over and the goal resuming after recovery, the off-season
block winning when configured, sign-up parsing, setup, and the brief / audit wiring.
"""
from __future__ import annotations

import json
import sys
from datetime import date, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]            # ClaudeCoach/
sys.path.insert(0, str(REPO / "lib"))
import goals  # noqa: E402
import plan_tools as pt  # noqa: E402

SET = date(2026, 10, 1)          # a Thursday: block 1 starts Monday 5 Oct
MON = date(2026, 10, 5)


def _cfg(goal="ftp", **kw):
    return {"goal": goals.make(goal, today=SET, sports=kw.pop("sports", ["bike", "run"]),
                               ctl=kw.pop("ctl", None)), **kw}


# ── block shape and load ───────────────────────────────────────────────────────

def test_block_starts_the_monday_after_the_goal_was_set():
    assert goals.block_start(_cfg()) == MON


def test_three_build_weeks_then_one_easier_week_then_repeats():
    # Jamie, 2 Oct 2026: "normally it's 3/4 weeks on, 1 off" - not the first cut's
    # load, load, easier, load, load, test.
    cfg = _cfg()
    kinds = [pt.required_tss(cfg, 45.0, today=MON + timedelta(weeks=i))["goal_week_kind"]
             for i in range(8)]
    assert kinds == ["load", "load", "load", "test", "load", "load", "load", "test"]


def test_a_five_week_block_is_four_build_weeks_then_one_easier():
    cfg = _cfg()
    cfg["goal"]["block_weeks"] = 5
    kinds = [pt.required_tss(cfg, 45.0, today=MON + timedelta(weeks=i))["goal_week_kind"]
             for i in range(5)]
    assert kinds == ["load", "load", "load", "load", "test"]


def test_load_week_adds_the_goal_ramp_and_never_floors_under_maintenance():
    r = pt.required_tss(_cfg(), 45.0, today=MON)
    assert r["week_type"] == "goal" and r["phase"] == "goal"
    assert r["weekly_tss_floor"] == 315                      # 7 x CTL
    assert r["recommended_weekly_tss"] > 315
    assert r["goal_ramp_per_week"] == 1.5


def test_athlete_ramp_cap_bounds_the_goal_ramp():
    r = pt.required_tss(_cfg("fitter", max_ctl_ramp_per_week=1.0), 45.0, today=MON)
    assert r["goal_ramp_per_week"] == 1.0


def test_easy_and_test_weeks_are_down_weeks_with_no_floor():
    for i in (3, 7):
        r = pt.required_tss(_cfg(), 45.0, today=MON + timedelta(weeks=i))
        assert r["week_type"] in pt.DOWN_WEEK_TYPES and r["goal_easy"]
        assert r["weekly_tss_floor"] == 0
        assert r["recommended_weekly_tss"] == round(315 * 0.70)


def test_test_week_books_the_goal_test():
    r = pt.required_tss(_cfg(), 45.0, today=MON + timedelta(weeks=3))
    names = [b["name"] for b in r["bookings"]]
    assert names == ["FTP test (end of block 1)"]
    assert "FTP test" in r["note"]
    assert pt.required_tss(_cfg(), 45.0, today=MON)["bookings"] == []


def test_goals_without_a_test_book_none():
    for g in ("maintain", "fitter"):
        assert pt.goal_test_bookings(_cfg(g), today=MON) == []


def test_a_goal_sport_outside_the_plan_books_no_test():
    assert pt.goal_test_bookings(_cfg("swim", sports=["bike", "run"]), today=MON) == []
    r = pt.required_tss(_cfg("swim", sports=["bike", "run"]), 45.0, today=MON + timedelta(weeks=3))
    assert r["bookings"] == []


def test_maintain_converges_on_the_band_from_either_side():
    cfg = _cfg("maintain", ctl=50)
    assert cfg["goal"]["ctl_band"] == [47.0, 53.0]
    low = pt.required_tss(cfg, 40.0, today=MON)
    mid = pt.required_tss(cfg, 50.0, today=MON)
    high = pt.required_tss(cfg, 58.0, today=MON)
    assert low["recommended_weekly_tss"] > 280               # build back up
    assert abs(mid["recommended_weekly_tss"] - 350) <= 10     # hold
    assert high["recommended_weekly_tss"] < 406               # come down
    assert mid["in_band"] and not low["in_band"]


def test_a_collapsed_week_turns_the_next_load_week_easier():
    r = pt.required_tss(_cfg(), 45.0, today=MON + timedelta(weeks=1), last_week_tss=100)
    assert r["goal_week_kind"] == "easy" and r["weekly_tss_floor"] == 0


def test_return_to_load_steps_up_at_most_thirty_percent():
    r = pt.required_tss(_cfg("fitter"), 60.0, today=MON + timedelta(weeks=1),
                        last_week_tss=300)
    assert r["recommended_weekly_tss"] == 420               # max(7 x 60, 300 x 1.3)
    assert r["return_step_cap"] == 420


def test_no_fitness_yet_gives_no_target_but_a_note():
    r = pt.required_tss(_cfg(), None, today=MON)
    assert r["recommended_weekly_tss"] is None and "No Fitness" in r["note"]


# ── race takes over, goal resumes ──────────────────────────────────────────────

def test_an_a_race_ahead_takes_over_from_the_goal():
    cfg = _cfg(race_date="2027-04-04", race_name="Marathon")
    assert not pt.goal_active(cfg, MON)
    assert pt.required_tss(cfg, 45.0, today=MON).get("week_type") != "goal"
    # No goal test inside the race block or its recovery; the first is the end of the
    # goal's block 1 after it (race 4 Apr, recovery to 25 Apr, block 1 from 26 Apr).
    tests = pt.goal_test_bookings(cfg, today=MON)
    assert tests and all(t["week_start"] > "2027-04-25" for t in tests)
    assert tests[0] == {"week_start": "2027-05-17", "sport": "Ride", "goal_test": True,
                        "name": "FTP test (end of block 1)", "match": "FTP test"}


def test_goal_restarts_at_block_one_after_the_race_recovery_weeks():
    cfg = _cfg(race_date="2026-11-01", race_name="Some race")     # a Sunday, mid-block 1
    assert not pt.goal_active(cfg, date(2026, 11, 2))              # recovery week 1
    back = date(2026, 11, 23)                                      # week 4 after the race
    assert pt.goal_active(cfg, back)
    r = pt.required_tss(cfg, 45.0, today=back)
    assert (r["goal_block"], r["goal_week"], r["goal_week_kind"]) == (1, 1, "load")


def test_an_off_season_block_wins_after_a_race():
    cfg = _cfg(race_date="2026-09-01", race_name="Old race", offseason={"focus": "speed"})
    assert not pt.goal_active(cfg, MON)


# ── sign-up parsing and setup ──────────────────────────────────────────────────

def test_no_race_answers():
    for t in ("none", "None.", "no race", "Not yet", "n/a", "just training", "nothing booked"):
        assert goals.is_no_race(t), t
    for t in ("Hampton half marathon in march", "IM Frankfurt, 2027-06-29", "London 10k"):
        assert not goals.is_no_race(t), t


def test_goal_answers_by_number_button_or_words():
    assert [goals.parse_goal(str(i)) for i in range(1, 6)] == list(goals.GOAL_ORDER)
    assert [goals.parse_goal(k) for k in goals.GOAL_ORDER] == list(goals.GOAL_ORDER)
    assert goals.parse_goal("I want more watts") == "ftp"
    assert goals.parse_goal("faster parkrun") == "run"
    assert goals.parse_goal("lose a bit of weight") == "fitter"
    assert goals.parse_goal("banana") is None


def test_sports_answers():
    assert goals.parse_sports("2 3") == ["bike", "run"]
    assert goals.parse_sports("swim and bike") == ["swim", "bike"]
    assert goals.parse_sports("?") == []


def test_goal_setup_preview_then_apply(tmp_path):
    p = tmp_path / "athletes.json"
    p.write_text(json.dumps({"tess": {"name": "Tess", "race_date": "", "race_name": ""}}))
    pv = pt.goal_setup("tess", "run", path=p, today=SET, ctl=40.0, sports="run")
    assert not pv["applied"] and pv["block_1"] == ["2026-10-05", "2026-11-01"]
    assert pv["tests"][0].startswith("2026-10-26: 5k time trial")
    assert pv["week_shape"] == ["load", "load", "load", "test"]
    assert "goal" not in json.loads(p.read_text())["tess"]
    pt.goal_setup("tess", "run", path=p, today=SET, ctl=40.0, sports="run", apply=True)
    g = json.loads(p.read_text())["tess"]["goal"]
    assert g["type"] == "run" and g["start"] == "2026-10-05" and g["sports"] == ["run"]
    pt.goal_setup("tess", path=p, today=SET, clear=True, apply=True)
    assert "goal" not in json.loads(p.read_text())["tess"]


def test_pin_start_fixes_block_one_and_the_maintain_band(tmp_path):
    p = tmp_path / "athletes.json"
    g = goals.make("maintain", today=SET)
    p.write_text(json.dumps({"tess": {"goal": g}}))
    out = goals.pin_start("tess", config_path=p, ctl=42.0, today=SET)
    assert out["start"] == "2026-10-05" and out["ctl_band"] == [39.0, 45.0]
    assert goals.pin_start("tess", config_path=p, ctl=50.0) is None     # already pinned


# ── what the athlete and the coach see ─────────────────────────────────────────

def test_view_and_progress():
    cfg = _cfg()
    cfg["goal"]["start_values"] = {"ftp": 231, "ctl": 47}
    v = goals.view(cfg, {"ftp_watts": 238}, today=MON + timedelta(weeks=2), ctl_now=49.2)
    assert (v["label"], v["block"], v["week"], v["kind"]) == ("Raise my FTP", 1, 3, "load")
    assert v["blockWeeks"] == 4 and v["shape"] == ["load", "load", "load", "test"]
    assert v["nextTest"] == {"name": "FTP test", "week_start": "2026-10-26"}
    assert [(r["label"], r["better"]) for r in v["progress"]] == [("FTP", True), ("Fitness", True)]


def test_run_progress_reads_pace_lower_as_better():
    cfg = _cfg("run")
    cfg["goal"]["start_values"] = {"run_threshold": "4:40"}
    rows = goals.progress(cfg, {"run_threshold_pace_per_km": "4:31"})
    assert rows[0]["better"] is True


def test_prompt_block_names_the_goal_and_the_setup_command(tmp_path):
    p = tmp_path / "athletes.json"
    p.write_text(json.dumps({"tess": _cfg()}))
    txt = goals.prompt_block("tess", "Tess", path=p, today=MON + timedelta(weeks=3))
    assert "Raise my FTP" in txt and "week 4" in txt and "goal-setup" in txt
    assert "4-week goal blocks (3 build weeks, then one easier week" in txt
    assert "countdown" in txt
    p.write_text(json.dumps({"tess": {}}))
    assert "GOAL WITHOUT A RACE" not in goals.prompt_block("tess", "Tess", path=p)


def test_distribution_puts_the_top_end_in_the_goal_sport():
    d = goals.distribution(_cfg("ftp", sports=["bike", "run"]))
    assert d == {"Bike": "70% Z1–2 / 12% Z3 / 18% Z4–5", "Run": "88% Z1–2 / 7% Z3 / 5% Z4–5"}


def test_sports_fall_back_to_the_standing_day_rules():
    cfg = {"goal": goals.make("maintain", today=SET),
           "day_rules": {"swim_days": ["Tue"], "run_days": ["Sat"], "bike_days": []}}
    assert goals.sports_for(cfg) == ["swim", "run"]


def test_preview_with_a_race_ahead_starts_after_its_recovery(tmp_path):
    p = tmp_path / "athletes.json"
    p.write_text(json.dumps({"tess": {"race_date": "2026-11-01", "race_name": "Race"}}))
    pv = pt.goal_setup("tess", "ftp", path=p, today=SET, ctl=40.0, sports="bike")
    assert pv["block_1"][0] == "2026-11-23" and "race block runs first" in pv["note"]
    assert pv["tests"][0].startswith("2026-12-14: FTP test (end of block 1)")


def test_preview_says_when_an_off_season_block_wins(tmp_path):
    p = tmp_path / "athletes.json"
    p.write_text(json.dumps({"tess": {"race_date": "2026-11-01", "race_name": "Race",
                                      "offseason": {"focus": "speed"}}}))
    pv = pt.goal_setup("tess", "ftp", path=p, today=SET, ctl=40.0, sports="bike")
    assert "block_1" not in pv and "off-season block" in pv["note"]
