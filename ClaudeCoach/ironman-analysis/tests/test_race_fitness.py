"""Run-race fitness (hybrid floors), A/B/C race priority, and "fitter than the goal".

Jamie, 29 Sep 2026: the blueprint needs the fitness a marathon / half / 10k / 5k
requires; guidance on A vs B vs C races; and a way to handle being fitter than the goal
that ASKS the athlete. Answers: run races are hybrid ("both are floored on their own"),
the total floor is the athlete's own, and what to do with a surplus is up to them.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from datetime import date
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]            # ClaudeCoach/
sys.path.insert(0, str(REPO / "lib"))
import plan_tools as pt  # noqa: E402
import race_fitness as rf  # noqa: E402


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, REPO / rel)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


# A block whose base phase target (60) sits well under the athlete's CTL (80).
BLOCK = {
    "plan_start": "2026-10-05",
    "race_date": "2027-04-25",
    "race_name": "Spring Marathon",
    "phase_tss": {"base_end_week": 8, "build_end_week": 14, "peak_end_week": 19},
    "ctl_targets": {"phase_ctl": {"base": 60, "build": 65, "peak": 70},
                    "maintenance_ctl_band": [65, 75]},
    "max_ctl_ramp_per_week": 4.0,
}
IN_BASE = date(2026, 10, 19)


class TestTables:
    def test_run_race_ranges(self):
        assert rf.run_fitness_range("marathon") == (55, 72)          # level 2
        assert rf.run_fitness_range("5k", "base") == (19, 28)       # level 2 x0.70
        assert rf.run_fitness_range("ironman") is None

    def test_run_event_key(self):
        assert rf.run_event_key("Half Marathon") == "half_marathon"
        assert rf.run_event_key("London Marathon") == "marathon"
        assert rf.run_event_key("Parkrun 5k") == "5k"
        assert rf.run_event_key("Full Ironman") is None
        assert rf.run_event_key("5k swim") is None

    def test_total_floor_is_the_athletes_own(self):
        assert rf.total_floor({"ctl_targets": {"total_fitness_floor": 60}}) == 60
        assert rf.total_floor(BLOCK) == 65                     # falls back to band bottom
        assert rf.total_floor({"ctl_targets": {}}) is None      # none set: running floor only

    def test_running_status(self):
        assert rf.running_status("marathon", "taper", 32)["status"] == "under"
        assert rf.running_status("5k", "taper", 60)["status"] == "over"
        assert rf.running_status("5k", "taper", 40)["status"] == "in"


class TestEasyDates:
    def test_c_race_one_each_side_b_race_three(self):
        c = rf.easy_dates([{"date": "2026-11-14", "priority": "C", "name": "5k"}],
                          date(2026, 11, 9))
        assert list(c) == ["2026-11-13", "2026-11-15"]
        b = rf.easy_dates([{"date": "2027-03-07", "priority": "B", "name": "Half"}],
                          date(2027, 3, 1))
        assert list(b) == ["2027-03-04", "2027-03-05", "2027-03-06"]

    def test_recovery_after_carries_into_next_week(self):
        nxt = rf.easy_dates([{"date": "2027-03-07", "priority": "B", "name": "Half"}],
                            date(2027, 3, 8))
        assert list(nxt) == ["2027-03-08", "2027-03-09", "2027-03-10"]
        assert "after" in nxt["2027-03-08"]


class TestFitterThanTheGoal:
    def test_unanswered_holds_and_asks(self):
        r = pt.required_tss(BLOCK, 80.0, today=IN_BASE)
        assert r["fitness_surplus"]["phase_target_ctl"] == 60
        assert r["needs_surplus_choice"] is True
        assert r["recommended_weekly_tss"] == 7 * 80              # hold, nothing lost
        assert "ASK the athlete" in r["note"]

    def test_hold_shift(self):
        r = pt.required_tss(dict(BLOCK, fitness_surplus_choice="hold_shift"), 80.0, today=IN_BASE)
        assert r["recommended_weekly_tss"] == 7 * 80
        assert "needs_surplus_choice" not in r
        assert "shift the mix" in r["note"]

    def test_drift_comes_down_but_not_below_the_floor(self):
        r = pt.required_tss(dict(BLOCK, fitness_surplus_choice="drift"), 80.0, today=IN_BASE)
        assert r["recommended_weekly_tss"] < 7 * 80
        # drifts to the floor (65), not the lower phase target (60)
        assert r["recommended_weekly_tss"] >= pt.compute_required_tss(80.0, 65.0, 6)
        assert r["weekly_tss_floor"] == r["recommended_weekly_tss"]   # no contradicting floor

    def test_not_fitter_means_no_question(self):
        r = pt.required_tss(BLOCK, 50.0, today=IN_BASE)
        assert "fitness_surplus" not in r and "needs_surplus_choice" not in r

    def test_fitness_choice_cli_writes_and_clears(self, tmp_path):
        p = tmp_path / "athletes.json"
        p.write_text(json.dumps({"x": {"race_date": "2027-04-25"}}))
        out = pt.set_fitness_choice("x", "drift", path=p)
        assert out["fitness_surplus_choice"] == "drift"
        assert json.loads(p.read_text())["x"]["fitness_surplus_choice"] == "drift"
        pt.set_fitness_choice("x", clear=True, path=p)
        assert "fitness_surplus_choice" not in json.loads(p.read_text())["x"]

    def test_the_bot_is_told_how_to_record_it(self, tmp_path):
        p = tmp_path / "athletes.json"
        p.write_text(json.dumps({"x": {"fitness_surplus_choice": "hold_shift"}}))
        block = pt.fitness_choice_prompt_block("x", "Jamie", path=p)
        assert "fitness-choice --athlete x" in block and "Current answer: hold_shift" in block

    def test_the_sunday_message_asks(self):
        s1 = _load("stage1", "scripts/stage1-plan.py")
        line = s1._surplus_line({"needs_surplus_choice": True,
                                 "fitness_surplus": {"ctl": 80.0, "phase_target_ctl": 60}})
        assert "*1*" in line and "*2*" in line and "*3*" in line
        assert s1._surplus_line({}) == ""


class TestBRaces:
    def test_a_registry_b_race_makes_its_week_easy(self):
        cfg = dict(BLOCK, races=[{"name": "Half", "date": "2026-10-25", "priority": "B"}])
        r = pt.required_tss(cfg, 55.0, today=IN_BASE)
        assert r["week_type"] == "taper" and "B race" in r["easy_week_reason"]
        assert r["weekly_tss_floor"] == 0

    def test_c_race_does_not_change_the_week(self):
        cfg = dict(BLOCK, races=[{"name": "Parkrun", "date": "2026-10-24", "priority": "C"}])
        assert (pt.required_tss(cfg, 55.0, today=IN_BASE)["week_type"]
                == pt.required_tss(BLOCK, 55.0, today=IN_BASE)["week_type"])

    def test_an_offseason_b_booking_lightens_its_week(self):
        base = {"plan_start": "2026-05-04", "race_date": "2026-09-19",
                "phase_tss": {"peak_end_week": 17},
                "ctl_targets": {"phase_ctl": {"peak": 110}, "maintenance_ctl_band": [65, 75]},
                "max_ctl_ramp_per_week": 4.0}
        b = dict(base, offseason={"bookings": [
            {"date": "2027-03-07", "sport": "Run", "name": "Half PB", "match": "half",
             "priority": "B"}]})
        c = dict(base, offseason={"bookings": [
            {"date": "2027-03-07", "sport": "Run", "name": "Half PB", "match": "half",
             "priority": "C"}]})
        rb = pt.required_tss(b, 70.0, today=date(2027, 3, 1))
        rc = pt.required_tss(c, 70.0, today=date(2027, 3, 1))
        assert rb["recommended_weekly_tss"] == round(rc["recommended_weekly_tss"] * 0.65)
        assert rb["weekly_tss_floor"] == 0 and "B-RACE WEEK" in rb["note"]


class TestValidatorEasyDays:
    def test_hard_work_on_a_recovery_day_blocks(self):
        s1 = _load("stage1", "scripts/stage1-plan.py")
        prop = {"sessions": [{"sport": "Bike", "date": "2027-03-08", "name": "VO2",
                              "segments": [{"minutes": 20, "zone": "z2"},
                                           {"minutes": 20, "zone": "z5"}]}]}
        brief = {"booking_easy_dates": {"2027-03-08": "1 day(s) after Half"}}
        blocking, _ = s1.audit_built(brief, {"total_tss": 300, "sessions": []}, 300, prop)
        assert [b for b in blocking if b["code"] == "booking_recovery"]
        assert s1.unknown_blocker_codes(blocking) == []
        assert s1.safety_blockers(blocking) == []


class TestBlueprintGenerator:
    def test_awaiting_decision_offers_the_athletes_three_choices(self):
        gb = _load("gb", "scripts/generate-blueprint.py")
        phases = [{"name": "Base", "start": date(2026, 1, 1), "end": date(2030, 1, 1)}]
        note = gb.fitness_check("x", "Full Ironman", 120.0, phases, None, {})
        assert "AWAITING_DECISION" in note and "hold_shift" in note and "drift" in note
        done = gb.fitness_check("x", "Full Ironman", 120.0, phases, None,
                                {"fitness_surplus_choice": "drift"})
        assert "AWAITING_DECISION" not in done and "drift" in done

    def test_run_race_checks_total_against_the_athletes_floor(self):
        gb = _load("gb", "scripts/generate-blueprint.py")
        phases = [{"name": "Base", "start": date(2026, 1, 1), "end": date(2030, 1, 1)}]
        assert "under this athlete's floor" in gb.fitness_check(
            "x", "Marathon", 50.0, phases, None, BLOCK)
        assert gb.fitness_check("x", "Marathon", 90.0, phases, None, BLOCK) is None


class TestBookingsInAnyBlock:
    """Jamie, 29 Sep 2026 (2a): the PB attempts and FTP/CSS tests carry on into the
    Brighton marathon block, where the off-season branch no longer runs."""

    CFG = dict(BLOCK, bookings=[
        {"week_start": "2026-11-30", "sport": "Ride", "name": "FTP test", "match": "FTP test"},
        {"date": "2027-03-07", "sport": "Run", "name": "Half PB", "match": "half",
         "priority": "B"}])

    def test_top_level_bookings_are_read_with_offseason_ones(self):
        cfg = dict(self.CFG, offseason={"bookings": [
            {"date": "2026-11-14", "sport": "Run", "name": "5k", "match": "5k"}]})
        assert {b["name"] for b in pt.all_bookings(cfg)} == {"FTP test", "Half PB", "5k"}
        assert [b["name"] for b in pt.week_bookings(cfg, date(2026, 12, 2))] == ["FTP test"]

    def test_held_bookings_see_top_level_ones(self):
        held = pt.held_bookings(self.CFG, date(2026, 9, 19), date(2026, 12, 7))
        assert [b["name"] for b in held] == ["FTP test"]

    def test_the_brief_books_them_in_a_normal_block_week(self):
        src = (REPO / "lib" / "session_library.py").read_text()
        assert "pt.week_bookings(cfg, plan_start) if _training_week" in src
        assert '_wt == "taper" and bool(req.get("easy_week_reason"))' in src


def test_a_run_race_keeps_the_athletes_cross_training_sports():
    src = (REPO / "lib" / "session_library.py").read_text()
    assert "if ekey in _rf.RUN_EVENTS and not bespoke:" in src
    assert '("bike", "bike_days"), ("swim", "swim_days")' in src


def test_the_config_race_wins_over_a_stale_profile():
    import session_library as sl
    assert sl.event_key({"race_name": "Brighton Marathon"},
                        {"race_distance": "Full Ironman", "race_name": "IM Italy"}) == "marathon"
    assert sl.event_key({}, {"race_distance": "70.3"}) == "70_3"
