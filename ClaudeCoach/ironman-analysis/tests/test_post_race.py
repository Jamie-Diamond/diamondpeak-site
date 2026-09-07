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
    def test_the_taper_proper_is_unchanged(self):
        # CFG's peak runs to week 12, which leaves a one-week taper that IS race week,
        # so the taper proper needs a config whose peak ends earlier.
        cfg = dict(CFG, phase_tss=dict(CFG["phase_tss"], peak_end_week=10))
        r = pt.required_tss(cfg, CTL, today=date(2026, 6, 24))   # 11 days out
        assert r["phase"] == "taper"
        assert r["week_type"] == "taper"
        assert r["weeks_to_race"] == 2
        assert "race_tss_estimate" not in r
        assert r["recommended_weekly_tss"] == round(MAINTENANCE * pt._TAPER_FACTORS[2])
        assert "TAPER" in r["note"]

    def test_final_week_is_race_week(self):
        r = _req("2026-07-01")
        assert r["phase"] == "taper"                # still the taper phase...
        assert r["week_type"] == "race"             # ...but the race is in it
        assert r["weeks_to_race"] == 1

    def test_race_day_itself_is_still_the_taper_phase(self):
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

    def test_works_toward_a_maintenance_target_once_the_plan_has_run_out(self):
        r = _req("2026-08-05")
        assert r["needs_next_race"] is True
        assert r["maintenance_ctl"] is not None
        assert "next race" in r["note"]

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


class TestOffSeasonMaintenance:
    """Week 4 onward, with no next race: work toward a maintenance CTL.

    The first cut held at a fixed 65% of 7 x CTL, recomputed weekly off the CURRENT
    CTL. 7 x CTL is exactly the load that keeps CTL level, so a fixed fraction of it
    prescribes less every week as CTL falls and CTL chases the target down — from 45
    it reaches 23 in twelve weeks with no floor at all. A maintenance setting must
    SETTLE somewhere.
    """

    PEAK = CFG["ctl_targets"]["phase_ctl"]["peak"]      # 65
    WEEK4 = "2026-08-05"

    def _converge(self, cfg, ctl0, weeks=30):
        """Run the prescription forward through CTL EMA mechanics and return the CTL
        it settles at (post-race weeks are driven off `today`, so the date moves too)."""
        ctl = float(ctl0)
        start = date.fromisoformat(RACE) + timedelta(days=1)
        for w in range(weeks):
            t = pt.required_tss(cfg, round(ctl, 1),
                                today=start + timedelta(days=7 * w))["recommended_weekly_tss"]
            for _ in range(7):
                ctl += (t / 7 - ctl) / 42
        return round(ctl, 1)

    def test_a_configured_target_is_used_as_given(self):
        cfg = dict(CFG, ctl_targets=dict(CFG["ctl_targets"], maintenance_ctl=38))
        r = pt.required_tss(cfg, CTL, today=date.fromisoformat(self.WEEK4))
        assert r["maintenance_ctl"] == 38
        assert r["maintenance_ctl_source"] == "configured"
        assert r["needs_maintenance_target"] is False

    def test_ctl_actually_settles_on_the_configured_target(self):
        cfg = dict(CFG, ctl_targets=dict(CFG["ctl_targets"], maintenance_ctl=38))
        assert abs(self._converge(cfg, CTL) - 38) < 1.5

    def test_it_comes_DOWN_to_maintenance_from_race_fitness(self):
        cfg = dict(CFG, ctl_targets=dict(CFG["ctl_targets"], maintenance_ctl=38))
        r = pt.required_tss(cfg, CTL, today=date.fromisoformat(self.WEEK4))
        assert r["recommended_weekly_tss"] < 7 * CTL      # below "hold current CTL"
        assert "come down to" in r["note"]

    def test_it_builds_BACK_UP_if_they_have_dropped_below_it(self):
        cfg = dict(CFG, ctl_targets=dict(CFG["ctl_targets"], maintenance_ctl=38))
        r = pt.required_tss(cfg, 20.0, today=date.fromisoformat(self.WEEK4))
        assert r["recommended_weekly_tss"] > 7 * 20
        assert "build back to" in r["note"]

    def test_the_rebuild_respects_the_ramp_cap(self):
        cfg = dict(CFG, ctl_targets=dict(CFG["ctl_targets"], maintenance_ctl=90))
        r = pt.required_tss(cfg, 20.0, today=date.fromisoformat(self.WEEK4))
        capped = pt.compute_required_tss(20.0, 20.0 + CFG["max_ctl_ramp_per_week"], 1)
        assert r["recommended_weekly_tss"] <= capped

    def test_unset_is_derived_from_their_own_peak_and_flagged_provisional(self):
        """Maintenance is obviously far below race fitness, so the fallback cannot be
        "hold where the recovery weeks left you" — for a CTL-115 athlete that is a hold
        at ~88 on ~615 TSS/wk, near race training with no race."""
        r = _req(self.WEEK4)
        assert r["maintenance_ctl"] == round(self.PEAK * pt._MAINTENANCE_FRACTION)
        assert r["maintenance_ctl_source"] == "derived"
        assert r["needs_maintenance_target"] is True     # coach still has to confirm it
        assert "PROVISIONAL" in r["note"]
        assert "ctl_targets.maintenance_ctl" in r["note"]

    def test_the_derived_target_is_well_below_race_fitness(self):
        assert _req(self.WEEK4)["maintenance_ctl"] < 0.7 * self.PEAK

    def test_no_basis_at_all_holds_current_ctl_rather_than_guessing(self):
        cfg = dict(CFG, ctl_targets={"phase_ctl": {"base": 40}})   # no peak, no race_min
        r = pt.required_tss(cfg, CTL, today=date.fromisoformat(self.WEEK4))
        assert r["maintenance_ctl"] is None
        assert r["recommended_weekly_tss"] == round(7 * CTL)       # a true hold, not a decay
        assert "no maintenance" in r["note"].lower()

    def test_the_old_fixed_fraction_decay_is_gone(self):
        """Regression: the target must not fall week after week under its own output."""
        cfg = dict(CFG, ctl_targets=dict(CFG["ctl_targets"], maintenance_ctl=38))
        assert self._converge(cfg, CTL, weeks=60) > 30             # settles, not spirals
        assert not hasattr(pt, "_TRANSITION_HOLD")

    def test_recovery_weeks_are_unaffected_by_the_maintenance_target(self):
        cfg = dict(CFG, ctl_targets=dict(CFG["ctl_targets"], maintenance_ctl=38))
        for day in ("2026-07-08", "2026-07-15", "2026-07-22"):
            a = pt.required_tss(cfg, CTL, today=date.fromisoformat(day))
            b = _req(day)
            assert a["recommended_weekly_tss"] == b["recommended_weekly_tss"]
            assert a["needs_maintenance_target"] is False   # not stale yet
