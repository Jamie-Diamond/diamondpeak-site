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
