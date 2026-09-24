"""Off-season block — a training block that keeps Fitness inside a band.

Jamie, 24 Sep 2026: "keep fitness between 65-75 in the off season, whilst focusing on
power and speed", with PB attempts and FTP/CSS tests booked in. Before this, week 4
onward after the race was a maintenance HOLD typed as a down-week ("one quality touch
a week at most, no progression"), which rules out exactly the work he asked for.

Pins: the band (target at its midpoint, floor at its bottom edge); the new week type
and that it is NOT a down-week; that athletes without an `offseason` block are
untouched; the bookings, and the validator/injector treatment of a booked session.
"""
from __future__ import annotations

import importlib.util
import sys
from datetime import date, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]            # ClaudeCoach/
sys.path.insert(0, str(REPO / "lib"))
import plan_tools as pt  # noqa: E402
import quality_inject as qi  # noqa: E402

RACE = "2026-09-19"
CFG = {
    "plan_start": "2026-05-04",
    "race_date": RACE,
    "race_name": "IM Italy",
    "phase_tss": {"base_end_week": 6, "build_end_week": 12, "peak_end_week": 17},
    "ctl_targets": {"phase_ctl": {"base": 80, "build": 95, "peak": 110},
                    "maintenance_ctl_band": [65, 75]},
    "max_ctl_ramp_per_week": 4.0,
}
OFF = dict(CFG, offseason={
    "focus": "power and speed",
    "bookings": [
        {"week_start": "2026-10-19", "sport": "Ride", "name": "FTP test", "match": "FTP"},
        {"date": "2026-11-14", "sport": "Run", "name": "5k PB attempt (parkrun)",
         "match": "5k"},
    ]})
WEEK1_OFF = date(2026, 10, 12)        # first Monday that is week 4+ after the race


def _stage1():
    spec = importlib.util.spec_from_file_location("stage1", REPO / "scripts" / "stage1-plan.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class TestBand:
    def test_band_sets_the_maintenance_target_at_its_midpoint(self):
        assert pt.maintenance_band(CFG) == (65.0, 75.0)
        assert pt.maintenance_ctl(CFG) == (70.0, "configured")

    def test_a_malformed_band_is_ignored_not_guessed(self):
        for bad in ([75, 65], [70], "65-75", None, [0, 10]):
            cfg = dict(CFG, ctl_targets={"phase_ctl": {"peak": 110},
                                         "maintenance_ctl_band": bad})
            assert pt.maintenance_band(cfg) is None


class TestNoOffseasonBlockIsUnchanged:
    def test_week4_is_still_the_maintenance_hold(self):
        r = pt.required_tss(CFG, 72.0, today=WEEK1_OFF)
        assert r["week_type"] == "post_race"
        assert r["weekly_tss_floor"] == 0
        assert "OFF-SEASON MAINTENANCE" in r["note"]
        assert "bookings" not in r


class TestOffseasonWeek:
    def test_recovery_weeks_are_untouched(self):
        for d in (date(2026, 9, 21), date(2026, 9, 28), date(2026, 10, 5)):
            a = pt.required_tss(OFF, 90.0, today=d)
            b = pt.required_tss(CFG, 90.0, today=d)
            assert a["week_type"] == "post_race"
            assert a["recommended_weekly_tss"] == b["recommended_weekly_tss"]

    def test_week4_becomes_a_training_week(self):
        r = pt.required_tss(OFF, 72.0, today=WEEK1_OFF)
        assert r["week_type"] == "offseason"
        assert r["week_type"] not in pt.DOWN_WEEK_TYPES
        assert r["offseason_week"] == 1
        assert r["ctl_band"] == [65.0, 75.0]
        assert r["in_band"] is True
        assert "between 65 and 75" in r["note"]
        assert "power and speed" in r["note"]
        assert "countdown" in r["note"]

    def test_an_offseason_block_without_a_band_still_plans(self):
        cfg = dict(OFF, ctl_targets={"phase_ctl": {"base": 40}})   # no band, no basis
        r = pt.required_tss(cfg, 72.0, today=WEEK1_OFF)
        assert r["week_type"] == "offseason"
        assert r["weekly_tss_floor"] == 0 and r["ctl_band"] is None
        assert "near 72" in r["note"]

    def test_floor_is_the_band_bottom_and_sits_under_the_target(self):
        r = pt.required_tss(OFF, 72.0, today=WEEK1_OFF)
        assert 0 < r["weekly_tss_floor"] < r["recommended_weekly_tss"]
        assert r["weekly_tss_floor"] == pt.compute_required_tss(
            72.0, 65.0, pt._MAINTENANCE_CONVERGE_WEEKS)

    def test_above_the_band_it_comes_down(self):
        r = pt.required_tss(OFF, 80.0, today=WEEK1_OFF)
        assert r["in_band"] is False
        assert r["recommended_weekly_tss"] < 7 * 80

    def test_below_the_band_it_builds_within_the_ramp_cap(self):
        r = pt.required_tss(OFF, 60.0, today=WEEK1_OFF)
        assert r["recommended_weekly_tss"] > 7 * 60
        assert r["recommended_weekly_tss"] <= pt.compute_required_tss(60.0, 64.0, 1)

    def test_from_race_fitness_it_settles_inside_the_band(self):
        ctl = 98.2                            # Jamie's CTL five days after the race
        d = date(2026, 9, 21)
        for _ in range(30):
            t = pt.required_tss(OFF, round(ctl, 1), today=d)["recommended_weekly_tss"]
            for _ in range(7):
                ctl += (t / 7 - ctl) / 42
            d += timedelta(days=7)
        assert 65 <= ctl <= 75


class TestBookings:
    def test_week_and_date_bookings_land_in_their_own_weeks(self):
        assert [b["name"] for b in pt.offseason_bookings(OFF, date(2026, 10, 21))] == ["FTP test"]
        assert [b["name"] for b in pt.offseason_bookings(OFF, date(2026, 11, 9))] == [
            "5k PB attempt (parkrun)"]
        assert pt.offseason_bookings(OFF, date(2026, 10, 12)) == []
        assert pt.offseason_bookings(CFG, date(2026, 10, 19)) == []

    def test_required_tss_carries_the_weeks_bookings(self):
        r = pt.required_tss(OFF, 72.0, today=date(2026, 11, 9))
        assert [b["match"] for b in r["bookings"]] == ["5k"]
        assert "5k PB attempt" in r["note"]

    def test_booking_matches(self):
        b = {"date": "2026-11-14", "sport": "Run", "match": "5k"}
        assert pt.booking_matches(b, {"sport": "Run", "date": "2026-11-14", "name": "5k parkrun PB"})
        assert not pt.booking_matches(b, {"sport": "Run", "date": "2026-11-13", "name": "5k PB"})
        assert not pt.booking_matches(b, {"sport": "Run", "date": "2026-11-14", "name": "Easy run"})
        assert not pt.booking_matches(b, {"sport": "Bike", "date": "2026-11-14", "name": "5k"})
        w = {"week_start": "2026-10-19", "sport": "Ride", "match": "FTP"}
        assert pt.booking_matches(w, {"sport": "Bike", "date": "2026-10-22", "name": "FTP test 20min"})


class TestStage1Validator:
    BOOK = {"date": "2026-11-14", "sport": "Run", "name": "5k PB attempt", "match": "5k"}
    BUILT = {"total_tss": 400, "sessions": []}

    def test_a_missing_booking_blocks(self):
        blocking, _ = _stage1().audit_built(
            {"booked_sessions": [self.BOOK]}, self.BUILT, 400,
            {"sessions": [{"sport": "Run", "date": "2026-11-14", "name": "Easy run",
                           "segments": [{"minutes": 40, "zone": "z2"}]}]})
        assert [b for b in blocking if b["code"] == "booking_missing"]

    def test_hard_work_the_day_before_blocks(self):
        prop = {"sessions": [
            {"sport": "Run", "date": "2026-11-14", "name": "5k parkrun",
             "segments": [{"minutes": 15, "zone": "z2"}, {"minutes": 20, "zone": "z5"}]},
            {"sport": "Bike", "date": "2026-11-13", "name": "VO2 ride",
             "segments": [{"minutes": 20, "zone": "z2"}, {"minutes": 20, "zone": "z5"}]}]}
        blocking, _ = _stage1().audit_built({"booked_sessions": [self.BOOK]}, self.BUILT, 400, prop)
        codes = {b["code"] for b in blocking}
        assert "booking_not_fresh" in codes and "booking_missing" not in codes

    def test_an_easy_eve_passes(self):
        prop = {"sessions": [
            {"sport": "Run", "date": "2026-11-14", "name": "5k parkrun",
             "segments": [{"minutes": 15, "zone": "z2"}, {"minutes": 20, "zone": "z5"}]},
            {"sport": "Swim", "date": "2026-11-13", "name": "Easy swim",
             "segments": [{"minutes": 30, "zone": "z2"}]}]}
        blocking, _ = _stage1().audit_built({"booked_sessions": [self.BOOK]}, self.BUILT, 400, prop)
        assert not [b for b in blocking if b["code"].startswith("booking")]

    def test_the_new_codes_are_classified_and_not_safety(self):
        s1 = _stage1()
        blk = [{"code": "booking_missing", "msg": ""}, {"code": "booking_not_fresh", "msg": ""}]
        assert s1.unknown_blocker_codes(blk) == []
        assert s1.safety_blockers(blk) == []


class TestInjectorLeavesTheBookingAlone:
    def test_trim_skips_the_booked_session(self):
        tt = {"sport": "Run", "date": "2026-11-14", "name": "5k parkrun",
              "segments": [{"minutes": 10, "zone": "z2"}, {"minutes": 20, "zone": "z5"}]}
        reps = {"sport": "Run", "date": "2026-11-11", "name": "Run reps",
                "segments": [{"minutes": 20, "zone": "z2"}, {"minutes": 15, "zone": "z5"}]}
        book = {"date": "2026-11-14", "sport": "Run", "match": "5k"}
        seg_if = lambda sport, sg: {"z2": 0.75, "z5": 1.06}[sg["zone"]]  # noqa: E731
        out = qi._apply({"sessions": [tt, reps]}, "Run", "high", -20, seg_if,
                        protect=lambda s: pt.booking_matches(book, s))
        assert out["sessions"][0] == tt           # the PB attempt is untouched
        assert out["sessions"][1] != reps          # the trim came from the other run
