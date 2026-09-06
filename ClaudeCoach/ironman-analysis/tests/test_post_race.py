"""Post-race behaviour — the week AFTER the A-race.

Before this (6 Sep 2026) there was no "the race has happened" branch anywhere in
planning. `week_now` counted past peak_end_week, so every post-race week resolved
to the TAPER branch, whose days_to_race is clamped at 0 -> weeks_to_race 1. The
athlete-visible result was a race-week taper ("hold INTENSITY, keep race-pace
sharpness"), a note claiming the race was in 1 week when it was weeks behind
them, and — because current_phase clamped to the Taper phase and tss_ceiling
returns None in a taper — NO weekly load cap at all on the week after an
Ironman.

Pins the transition branch, and pins that nothing about the PRE-race taper moved.
"""
from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]            # ClaudeCoach/
sys.path.insert(0, str(REPO / "lib"))
import plan_tools as pt  # noqa: E402
from primitives.blueprint import current_phase, tss_ceiling  # noqa: E402

RACE = "2026-07-05"

CFG = {
    "plan_start": "2026-04-06",
    "race_date": RACE,
    "race_name": "A Race",
    "phase_tss": {"base_end_week": 6, "build_end_week": 10, "peak_end_week": 12},
    "ctl_targets": {"phase_ctl": {"base": 40, "build": 55, "specific": 60, "peak": 65}},
    "max_ctl_ramp_per_week": 5.0,
}
CTL = 45.0
MAINTENANCE = 7 * CTL          # 315


def _req(day: str, **kw):
    return pt.required_tss(CFG, CTL, today=date.fromisoformat(day), **kw)


class TestPreRaceUnchanged:
    def test_final_week_is_still_a_taper(self):
        r = _req("2026-07-01")
        assert r["phase"] == "taper"
        assert r["week_type"] == "taper"
        assert r["weeks_to_race"] == 1
        assert "TAPER" in r["note"]

    def test_race_day_itself_is_still_a_taper(self):
        r = _req(RACE)
        assert r["phase"] == "taper"


class TestPostRaceIsATransition:
    def test_week_after_the_race_is_not_a_taper(self):
        r = _req("2026-07-08")
        assert r["phase"] == "transition"
        assert r["week_type"] == "post_race"
        assert r["weeks_since_race"] == 1

    def test_never_claims_an_upcoming_race(self):
        """The complaint: "it tells you it's the next weekend as well"."""
        for day in ("2026-07-08", "2026-07-20", "2026-08-05", "2026-09-06"):
            note = _req(day)["note"]
            assert "weeks_to_race" not in _req(day)
            assert "in 1 wk" not in note
            assert "race in" not in note.lower()

    def test_volume_returns_gradually_and_is_bounded_by_maintenance(self):
        got = [_req(d)["recommended_weekly_tss"]
               for d in ("2026-07-08", "2026-07-15", "2026-07-22")]
        assert got == [round(MAINTENANCE * f) for f in (0.30, 0.45, 0.60)]
        assert got == sorted(got)                       # monotonic ramp back
        assert all(t < MAINTENANCE for t in got)        # never a build week

    def test_no_under_training_floor(self):
        # The floor exists to stop a light PHASE target detraining someone mid-block.
        # A transition is meant to be light, so the floor must be off — otherwise it
        # would drag the week straight back up to 7 x CTL.
        assert _req("2026-07-08")["weekly_tss_floor"] == 0

    def test_intensity_is_explicitly_off(self):
        note = _req("2026-07-08")["note"]
        assert "no VO2" in note and "no threshold" in note
        assert "Easy aerobic only" in note

    def test_holds_rather_than_builds_once_the_plan_has_run_out(self):
        r = _req("2026-08-05")
        assert r["needs_next_race"] is True
        assert r["recommended_weekly_tss"] == round(MAINTENANCE * pt._TRANSITION_HOLD)
        assert "next race" in r["note"]

    def test_hold_does_not_ratchet_upward_with_time(self):
        a = _req("2026-08-05")["recommended_weekly_tss"]
        b = _req("2026-11-05")["recommended_weekly_tss"]
        assert a == b

    def test_no_ctl_still_returns_a_transition_not_a_taper(self):
        r = pt.required_tss(CFG, 0, today=date(2026, 7, 8))
        assert r.get("phase") == "transition"
        assert r["recommended_weekly_tss"] is None
        assert "NOT a taper" in r["note"]

    def test_fires_even_if_the_phase_config_still_says_peak(self):
        """A stale plan_start can leave week_now inside 'peak' after race day."""
        cfg = dict(CFG, plan_start="2026-06-01")        # race lands in week 5 = base
        r = pt.required_tss(cfg, CTL, today=date(2026, 7, 8))
        assert r["phase"] == "transition"


class TestReturningToLoadAfterTheTransition:
    def test_a_post_race_week_is_not_read_as_a_missed_week(self):
        """The transition is light BY DESIGN. Executing it must not trip the
        miss-trigger (or the return-to-load step cap) and cascade a recovery week
        into the first real week of the next block."""
        cfg = dict(CFG, race_date="2026-06-14")         # race behind us, next block on
        light = 0.3 * MAINTENANCE
        r = pt.required_tss(cfg, CTL, today=date(2026, 6, 21), last_week_tss=light)
        assert r["week_type"] == "post_race"            # still transitioning
        assert "deload_reason" not in r


class TestNoUnboundedWeekAfterTheRace:
    def _bp(self):
        return {"phases": [
            {"name": "Peak",  "start": "2026-06-01", "end": "2026-06-21"},
            {"name": "Taper", "start": "2026-06-22", "end": RACE},
        ]}

    def test_the_week_after_the_race_has_a_load_ceiling(self):
        ph = current_phase(self._bp(), date.fromisoformat(RACE) + timedelta(days=3))
        assert ph["name"] == "Transition"
        cap = tss_ceiling(12.0, ph["name"])
        assert cap is not None and cap > 0

    def test_the_taper_itself_still_has_none(self):
        ph = current_phase(self._bp(), date(2026, 6, 30))
        assert ph["name"] == "Taper"
        assert tss_ceiling(12.0, ph["name"]) is None

    def test_transition_ceiling_is_below_base(self):
        assert tss_ceiling(12.0, "Transition") < tss_ceiling(12.0, "Base")
