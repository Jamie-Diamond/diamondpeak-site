"""Tests for primitives/validate_plan.py — the deterministic planner backstop (WS E).

Each hard constraint has: a clean week that passes, and a breach that is caught.
Every check is opt-in (only fires when its input is supplied).
"""
from __future__ import annotations

from datetime import date

from primitives.validate_plan import (
    validate_week, validate_plan, Violation, WeekReport,
)

WEEK = date(2026, 6, 15)   # a Monday

# jamie's real day-rules: swim Tue/Thu only, bike Fri/Sat/Sun only.
DAY_RULES = {"swim_days": ["Tue", "Thu"], "bike_days": ["Fri", "Sat", "Sun"]}


def _ev(day_iso: str, sport: str, load: float = 50, category: str = "WORKOUT") -> dict:
    return {"start_date_local": f"{day_iso}T00:00:00", "type": sport,
            "load_target": load, "category": category}


def _clean_week() -> list[dict]:
    # Mon=15 … Sun=21. Swims Tue/Thu, rides Fri/Sat/Sun, runs/strength anywhere.
    return [
        _ev("2026-06-15", "Run", 40),
        _ev("2026-06-16", "Swim", 35),
        _ev("2026-06-16", "Run", 45),
        _ev("2026-06-18", "Swim", 50),
        _ev("2026-06-19", "Ride", 120),
        _ev("2026-06-20", "Ride", 90),
        _ev("2026-06-21", "Ride", 80),
    ]


class TestCleanWeek:
    def test_passes_all_checks(self):
        r = validate_week(_clean_week(), WEEK, day_rules=DAY_RULES,
                          weekly_tss_cap=600, ctl_today=80, ramp_cap=5)
        assert r.ok, [str(v) for v in r.violations]
        assert r.total_tss == 460
        assert isinstance(r, WeekReport)


class TestDayRules:
    def test_swim_on_forbidden_day_caught(self):
        evs = _clean_week() + [_ev("2026-06-15", "Swim", 40)]   # swim on Monday
        r = validate_week(evs, WEEK, day_rules=DAY_RULES)
        assert any(v.code == "swim_forbidden_day" and v.severity == "hard"
                   for v in r.violations)

    def test_ride_on_forbidden_day_caught(self):
        evs = _clean_week() + [_ev("2026-06-16", "Ride", 60)]   # ride on Tuesday
        r = validate_week(evs, WEEK, day_rules=DAY_RULES)
        assert any(v.code == "ride_forbidden_day" for v in r.violations)

    def test_gravelride_maps_to_bike_rule(self):
        evs = [_ev("2026-06-15", "GravelRide", 60)]             # bike on Monday
        r = validate_week(evs, WEEK, day_rules=DAY_RULES)
        assert any(v.code == "gravelride_forbidden_day" for v in r.violations)

    def test_unrestricted_sport_never_flagged(self):
        # Run/WeightTraining have no rule → any day is fine.
        evs = [_ev("2026-06-15", "Run", 40), _ev("2026-06-15", "WeightTraining", 20)]
        r = validate_week(evs, WEEK, day_rules=DAY_RULES)
        assert r.ok

    def test_no_day_rules_means_no_day_check(self):
        # Opt-in: without day_rules a Monday swim is not flagged.
        evs = [_ev("2026-06-15", "Swim", 40)]
        r = validate_week(evs, WEEK)
        assert not any("forbidden_day" in v.code for v in r.violations)


class TestWeeklyTssCap:
    def test_over_cap_caught(self):
        r = validate_week(_clean_week(), WEEK, weekly_tss_cap=300)  # 460 > 300+10%
        assert any(v.code == "weekly_tss_cap" for v in r.violations)

    def test_within_tolerance_passes(self):
        # 460 vs cap 430 → ceiling 473 → passes.
        r = validate_week(_clean_week(), WEEK, weekly_tss_cap=430, tss_tolerance=0.10)
        assert not any(v.code == "weekly_tss_cap" for v in r.violations)

    def test_no_cap_means_no_tss_check(self):
        r = validate_week(_clean_week(), WEEK)
        assert not any(v.code == "weekly_tss_cap" for v in r.violations)


class TestCtlRamp:
    def test_huge_week_breaches_ramp(self):
        big = [_ev("2026-06-15", "Ride", 600), _ev("2026-06-17", "Ride", 600)]
        r = validate_week(big, WEEK, ctl_today=20, ramp_cap=5)
        assert any(v.code == "ctl_ramp" for v in r.violations)

    def test_modest_week_within_ramp(self):
        r = validate_week(_clean_week(), WEEK, ctl_today=80, ramp_cap=5)
        assert not any(v.code == "ctl_ramp" for v in r.violations)

    def test_no_ctl_means_no_ramp_check(self):
        r = validate_week(_clean_week(), WEEK, ramp_cap=5)   # ctl_today absent
        assert not any(v.code == "ctl_ramp" for v in r.violations)


class TestWindowingAndCategory:
    def test_events_outside_week_ignored(self):
        evs = _clean_week() + [_ev("2026-06-08", "Swim", 40)]  # prior week, Mon
        r = validate_week(evs, WEEK, day_rules=DAY_RULES)
        assert r.ok                                            # not in this week

    def test_non_workout_ignored(self):
        evs = _clean_week() + [_ev("2026-06-15", "Swim", 40, category="NOTE")]
        r = validate_week(evs, WEEK, day_rules=DAY_RULES, weekly_tss_cap=600)
        assert r.ok
        assert r.total_tss == 460   # the note's load doesn't count

    def test_missing_load_target_counts_zero(self):
        evs = [{"start_date_local": "2026-06-15T00:00:00", "type": "Run",
                "category": "WORKOUT"}]   # no load_target
        r = validate_week(evs, WEEK)
        assert r.total_tss == 0


class TestValidatePlanMultiWeek:
    def test_one_report_per_week(self):
        w2 = [
            {"start_date_local": "2026-06-23T00:00:00", "type": "Swim",
             "load_target": 40, "category": "WORKOUT"},   # Tue wk2 — fine
        ]
        reports = validate_plan(_clean_week() + w2,
                                [date(2026, 6, 15), date(2026, 6, 22)],
                                day_rules=DAY_RULES)
        assert len(reports) == 2
        assert all(r.ok for r in reports)


class TestStrengthCap:
    """≤N strength sessions/week (composition quality). Soft severity — logged in
    warn mode, never block-worthy on its own."""

    def _ev_named(self, day_iso, name, sport="WeightTraining"):
        return {"start_date_local": f"{day_iso}T00:00:00", "type": sport,
                "name": name, "load_target": 20, "category": "WORKOUT"}

    def test_over_cap_flags_soft(self):
        evs = [self._ev_named("2026-06-15", "Strength & conditioning"),
               self._ev_named("2026-06-17", "Kettlebell circuit"),
               self._ev_named("2026-06-19", "S&C lower body", sport="Workout")]  # typed Workout
        rep = validate_week(evs, WEEK, strength_max=2)
        hits = [v for v in rep.violations if v.code == "strength_over_cap"]
        assert len(hits) == 1 and hits[0].severity == "soft"
        assert "3 strength" in hits[0].detail

    def test_at_cap_is_clean(self):
        evs = [self._ev_named("2026-06-15", "Strength & conditioning"),
               self._ev_named("2026-06-17", "Kettlebell circuit")]
        rep = validate_week(evs, WEEK, strength_max=2)
        assert not [v for v in rep.violations if v.code == "strength_over_cap"]

    def test_not_checked_when_unset(self):
        evs = [self._ev_named("2026-06-15", "Strength"),
               self._ev_named("2026-06-16", "Strength"),
               self._ev_named("2026-06-17", "Strength")]
        rep = validate_week(evs, WEEK)        # no strength_max → no check
        assert not [v for v in rep.violations if v.code == "strength_over_cap"]

    def test_strength_max_key_in_day_rules_does_not_crash_day_parsing(self):
        # day_rules now carries scalar strength_max alongside the *_days lists.
        dr = {"swim_days": ["Tue", "Thu"], "run_days": ["Tue", "Wed", "Sat", "Sun"],
              "strength_max": 2}
        evs = [_ev("2026-06-16", "Swim", 35), _ev("2026-06-17", "Run", 45)]
        rep = validate_week(evs, WEEK, day_rules=dr, strength_max=2)  # must not raise
        assert isinstance(rep, WeekReport)


# -- Intensity-distribution drift (check 5) ------------------------------------

DIST = {"Bike": "75% Z1–2 / 15% Z3 / 10% Z4–5",
        "Run":  "80% Z1–2 / 12% Z3 / 8% Z4–5"}


def _named(day_iso, sport, name, mins):
    return {"start_date_local": f"{day_iso}T00:00:00", "type": sport, "name": name,
            "moving_time": mins * 60, "load_target": 50, "category": "WORKOUT"}


class TestDistributionDrift:
    def test_easy_dominant_week_passes(self):
        evs = [
            _named("2026-06-19", "Ride", "Long Z2 ride", 240),
            _named("2026-06-20", "Ride", "Threshold ride (3x10)", 75),
            _named("2026-06-15", "Run", "Easy run", 50),
            _named("2026-06-16", "Run", "Long run", 100),
        ]
        r = validate_week(evs, WEEK, distribution=DIST)
        assert not [v for v in r.violations if v.code == "intensity_distribution"]

    def test_quality_heavy_bike_week_flagged_soft(self):
        evs = [
            _named("2026-06-19", "Ride", "VO2max intervals (5x4)", 75),
            _named("2026-06-20", "Ride", "Threshold ride (3x15 sweet spot)", 90),
            _named("2026-06-21", "Ride", "Z2 spin", 60),
        ]
        r = validate_week(evs, WEEK, distribution=DIST)
        hits = [v for v in r.violations if v.code == "intensity_distribution"]
        assert len(hits) == 1 and hits[0].severity == "soft" and "Bike" in hits[0].detail

    def test_single_session_never_judged(self):
        evs = [_named("2026-06-19", "Ride", "VO2max intervals", 150)]
        r = validate_week(evs, WEEK, distribution=DIST)
        assert not [v for v in r.violations if v.code == "intensity_distribution"]

    def test_no_distribution_supplied_check_inert(self):
        evs = [
            _named("2026-06-19", "Ride", "VO2max intervals (5x4)", 75),
            _named("2026-06-20", "Ride", "Threshold ride", 90),
        ]
        r = validate_week(evs, WEEK)
        assert not [v for v in r.violations if v.code == "intensity_distribution"]

    def test_swims_and_bricks_excluded(self):
        evs = [
            _named("2026-06-16", "Swim", "CSS test set", 60),
            _named("2026-06-18", "Swim", "CSS intervals", 60),
            _named("2026-06-20", "Ride", "Brick: 90min Z3 + 20min run", 110),
        ]
        r = validate_week(evs, WEEK, distribution=DIST)
        assert not [v for v in r.violations if v.code == "intensity_distribution"]
