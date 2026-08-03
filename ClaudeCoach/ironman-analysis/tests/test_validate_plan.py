"""Tests for primitives/validate_plan.py — the deterministic planner backstop (WS E).

Each hard constraint has: a clean week that passes, and a breach that is caught.
Every check is opt-in (only fires when its input is supplied).
"""
from __future__ import annotations

from datetime import date

from primitives.validate_plan import (
    validate_week, validate_plan, Violation, WeekReport, escalate_repeats,
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


# -- Distance/duration internal consistency (check 6) --------------------------

class TestDistanceDurationMismatch:
    def _ev_walkrun(self, day_iso, name, notes=""):
        return {"start_date_local": f"{day_iso}T00:00:00", "type": "Run",
                "name": name, "description_raw": notes,
                "load_target": 40, "category": "WORKOUT"}

    def test_5k_label_with_50min_walkrun_flagged(self):
        # 5x9:1 @ conservative paces implies ~7.3km, not the labelled 5k
        # (the real 11 May 2026 incident: labelled 5k, 50 min 5x9:1 walk-run).
        evs = [self._ev_walkrun("2026-06-15", "Easy run / walk-run 5k — 50 min 5x9:1")]
        rep = validate_week(evs, WEEK)
        hits = [v for v in rep.violations if v.code == "distance_duration_mismatch"]
        assert len(hits) == 1 and hits[0].severity == "hard"
        assert "5k" in hits[0].detail and "7.3km" in hits[0].detail

    def test_consistent_label_passes(self):
        # 5x5:1 implies ~4.4km — close enough to the labelled 4k.
        evs = [self._ev_walkrun("2026-06-15", "Easy run / walk-run 4k — 30 min 5x5:1")]
        rep = validate_week(evs, WEEK)
        assert not [v for v in rep.violations if v.code == "distance_duration_mismatch"]

    def test_no_distance_label_not_checked(self):
        evs = [self._ev_walkrun("2026-06-15", "Easy run / walk-run — 50 min 5x9:1")]
        rep = validate_week(evs, WEEK)
        assert not [v for v in rep.violations if v.code == "distance_duration_mismatch"]

    def test_no_walkrun_pattern_not_checked(self):
        evs = [self._ev_walkrun("2026-06-15", "Easy run 5k")]
        rep = validate_week(evs, WEEK)
        assert not [v for v in rep.violations if v.code == "distance_duration_mismatch"]

    def test_non_run_sport_not_checked(self):
        evs = [{"start_date_local": "2026-06-15T00:00:00", "type": "Ride",
                "name": "Ride 5k — 50 min 5x9:1", "load_target": 40,
                "category": "WORKOUT"}]
        rep = validate_week(evs, WEEK)
        assert not [v for v in rep.violations if v.code == "distance_duration_mismatch"]


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

# -- Segment-minute intensity distribution (2026-07-28 rewrite) ----------------
# The old check bucketed WHOLE SESSIONS by name, so "Easy run + 10min tempo" counted
# 40 quality minutes instead of 10. These fix the behaviour in place: the classes above
# deliberately use events with NO workout_doc, which is the name-based fallback path, so
# they still assert the legacy behaviour where the legacy evidence is all there is.

def _step(mins, lo, hi, kind="pace"):
    return {kind: {"start": lo, "end": hi, "units": "%pace" if kind == "pace" else "%ftp"},
            "duration": int(mins * 60)}


def _structured(day_iso, sport, name, steps):
    mins = sum(s["duration"] for s in steps) / 60
    e = _named(day_iso, sport, name, mins)
    e["workout_doc"] = {"steps": steps}
    return e


# planned_tss._ZONE_BAND tuples, spelled out so a change to that table breaks these
# tests loudly rather than silently re-bucketing real athletes' weeks.
RUN_EASY, RUN_Z3, RUN_THR = (78, 88), (80, 86), (95, 101)
BIKE_Z2, BIKE_Z3, BIKE_SS, BIKE_VO2 = (60, 70), (76, 84), (88, 94), (105, 118)


class TestSegmentDistribution:
    def test_mostly_easy_session_with_a_short_quality_block_is_not_all_quality(self):
        """THE false positive: Jamie's real week of 2026-07-27 read 60% easy and flagged;
        by segment minutes it is 87% easy and on spec."""
        evs = [
            _structured("2026-06-15", "Run", "Long run", [_step(130, *RUN_EASY)]),
            _structured("2026-06-16", "Run", "Easy run + 10min tempo",
                        [_step(15, *RUN_EASY), _step(10, *RUN_Z3), _step(15, *RUN_EASY)]),
            _structured("2026-06-17", "Run", "Easy run + sweetspot",
                        [_step(27, *RUN_EASY), _step(18, *RUN_Z3)]),
        ]
        r = validate_week(evs, WEEK, distribution=DIST)
        assert not [v for v in r.violations if v.code.startswith("intensity_distribution")]

    def test_the_same_week_flags_under_the_old_name_based_path(self):
        """Strip the steps and the identical week flags — proving the fix is the
        measurement, not a loosened threshold."""
        evs = [_named("2026-06-15", "Run", "Long run", 130),
               _named("2026-06-16", "Run", "Easy run + 10min tempo", 40),
               _named("2026-06-17", "Run", "Easy run + sweetspot", 45)]
        r = validate_week(evs, WEEK, distribution=DIST)
        assert [v for v in r.violations if v.code == "intensity_distribution"]

    def test_genuine_excess_quality_still_flags_by_segment(self):
        """NEGATIVE CONTROL — the fix must not make a real excess-quality week clean."""
        evs = [
            _structured("2026-06-15", "Ride", "Sweetspot 4x20",
                        [_step(15, *BIKE_Z2, kind="power"), _step(80, *BIKE_SS, kind="power"),
                         _step(10, *BIKE_Z2, kind="power")]),
            _structured("2026-06-16", "Ride", "VO2 5x5",
                        [_step(15, *BIKE_Z2, kind="power"), _step(25, *BIKE_VO2, kind="power"),
                         _step(20, *BIKE_Z3, kind="power")]),
        ]
        r = validate_week(evs, WEEK, distribution=DIST)
        hits = [v for v in r.violations if v.code == "intensity_distribution"]
        assert len(hits) == 1 and "segment minutes" in hits[0].detail

    def test_vo2_swapped_for_z3_keeps_the_easy_share_but_trips_the_zone_ceiling(self):
        """The hole the easy-share check alone leaves: identical easy minutes, quality
        moved from Z3 into Z4-5. Caught by the per-zone ceiling limb."""
        evs = [
            _structured("2026-06-15", "Ride", "Endurance", [_step(150, *BIKE_Z2, kind="power")]),
            _structured("2026-06-16", "Ride", "VO2 block",
                        [_step(30, *BIKE_Z2, kind="power"), _step(30, *BIKE_VO2, kind="power")]),
        ]
        r = validate_week(evs, WEEK, distribution=DIST)
        assert [v for v in r.violations if v.code == "intensity_distribution_vo2_high"]
        # easy share is 180/210 = 86% — the easy-share limb alone would say nothing.
        assert not [v for v in r.violations if v.code == "intensity_distribution"]

    def test_partially_stepped_session_falls_back_rather_than_shrinking_the_denominator(self):
        """Steps covering only the main set must not be trusted: 20 min of stated steps on
        a 90-min event would otherwise read 100% quality of a 20-min week."""
        from primitives.validate_plan import step_bucket_minutes
        e = _structured("2026-06-15", "Ride", "Threshold 2x10",
                        [_step(20, *BIKE_SS, kind="power")])
        e["moving_time"] = 90 * 60          # stated duration, steps cover 20 min
        assert step_bucket_minutes("Ride", e) is None

    def test_unstructured_session_keeps_the_loose_tolerance_and_says_so(self):
        """Kathryn's real "Sweetspot 3x15" carries no steps at all. That sport stays on the
        legacy measurement AND the report records why it was judged loosely."""
        evs = [
            _named("2026-06-15", "Ride", "Sweetspot 3x15", 90),
            _structured("2026-06-16", "Ride", "Long ride",
                        [_step(150, *BIKE_Z2, kind="power"), _step(30, *BIKE_Z3, kind="power")]),
        ]
        r = validate_week(evs, WEEK, distribution=DIST)
        assert any("measured loosely" in s and "no usable structured steps" in s
                   for s in r.skipped)
        assert not [v for v in r.violations
                    if v.code.startswith("intensity_distribution_")]   # no per-zone limb

    def test_bricks_still_excluded_even_when_structured(self):
        evs = [
            _structured("2026-06-15", "Run", "Brick run — tempo off the bike",
                        [_step(25, *RUN_EASY), _step(25, *RUN_Z3)]),
            _structured("2026-06-16", "Run", "Easy run", [_step(50, *RUN_EASY)]),
            _structured("2026-06-17", "Run", "Long run", [_step(100, *RUN_EASY)]),
        ]
        r = validate_week(evs, WEEK, distribution=DIST)
        assert not [v for v in r.violations if v.code.startswith("intensity_distribution")]

    def test_step_band_reverse_index_separates_run_easy_from_run_z3(self):
        """The two bands OVERLAP (78-88 vs 80-86) and share a midpoint of 83, so only the
        exact tuple can tell them apart. This is the whole basis of the fix."""
        from primitives.validate_plan import bucket_for_step
        assert bucket_for_step("Run", _step(10, *RUN_EASY)) == "easy"
        assert bucket_for_step("Run", _step(10, *RUN_Z3)) == "z3"
        assert bucket_for_step("Run", _step(10, *RUN_THR)) == "z45"

    def test_off_table_band_is_counted_as_guessed_not_measured(self):
        """Calum's real "VO2 touch" renders 64-72% FTP — the planner's intensity fallback,
        absent from _ZONE_BAND. It must be flagged as a guess, not silently trusted."""
        from primitives.validate_plan import step_bucket_minutes
        e = _structured("2026-06-15", "Ride", "VO2 touch",
                        [_step(15, 60, 70, kind="power"), _step(12, 64, 72, kind="power")])
        got = step_bucket_minutes("Ride", e)
        assert got["guessed_min"] == 12 and got["easy"] == 27


class TestEscalateRepeats:
    def _v(self, code="intensity_distribution"):
        return Violation(code=code, severity="soft", detail="x")

    def test_first_occurrence_does_not_escalate(self):
        out, streaks = escalate_repeats([self._v()], {})
        assert len(out) == 1 and streaks == {"intensity_distribution": 1}

    def test_third_consecutive_run_escalates(self):
        out, streaks = escalate_repeats([self._v()], {"intensity_distribution": 2})
        codes = [v.code for v in out]
        assert "intensity_distribution_persistent" in codes
        assert streaks == {"intensity_distribution": 3}
        # loud, never blocking
        assert all(v.severity == "soft" for v in out)

    def test_a_clean_run_breaks_the_streak(self):
        out, streaks = escalate_repeats([], {"intensity_distribution": 5})
        assert out == [] and streaks == {}

    def test_hard_violations_are_not_streak_tracked(self):
        out, streaks = escalate_repeats(
            [Violation(code="wrong_day", severity="hard", detail="x")], {})
        assert streaks == {} and len(out) == 1


class TestDirectedDeviations:
    """day_rules are GUIDELINES: a coach-DIRECTED off-pattern session is a soft
    advisory, an UNDIRECTED one is still a hard breach, and overrides cannot be used
    to move the pattern by stealth."""

    WED_SWIM = [_ev("2026-06-17", "Swim", 35)]                 # Wed, not in swim_days
    DIRECTED = {"swim:2026-06-17": "Coach-directed in Telegram, 2026-06-15."}

    def test_undirected_deviation_is_still_hard(self):
        r = validate_week(self.WED_SWIM, WEEK, day_rules=DAY_RULES)
        assert [(v.code, v.severity) for v in r.violations] == [("swim_forbidden_day", "hard")]

    def test_directed_deviation_downgrades_but_stays_visible(self):
        r = validate_week(self.WED_SWIM, WEEK, day_rules=DAY_RULES,
                          day_overrides=self.DIRECTED)
        assert [(v.code, v.severity) for v in r.violations] == [("swim_directed_day", "soft")]
        # the reason it was allowed must be ON the finding, not just in a register
        assert "COACH-DIRECTED" in r.violations[0].detail
        assert "Telegram" in r.violations[0].detail

    def test_an_override_is_dated_so_it_cannot_excuse_another_day(self):
        # Same sport, a different Wednesday: the register entry must not travel.
        r = validate_week([_ev("2026-06-24", "Swim", 35)], date(2026, 6, 22),
                          day_rules=DAY_RULES, day_overrides=self.DIRECTED)
        assert [v.code for v in r.violations] == ["swim_forbidden_day"]

    def test_an_override_does_not_excuse_a_different_sport_that_day(self):
        r = validate_week(self.WED_SWIM + [_ev("2026-06-17", "Ride", 90)], WEEK,
                          day_rules=DAY_RULES, day_overrides=self.DIRECTED)
        assert sorted(v.code for v in r.violations) == ["ride_forbidden_day",
                                                       "swim_directed_day"]

    def test_ride_family_shares_one_override_key(self):
        # bike_days covers Ride/VirtualRide/GravelRide, so the register family is "bike".
        r = validate_week([_ev("2026-06-17", "GravelRide", 90)], WEEK, day_rules=DAY_RULES,
                          day_overrides={"bike:2026-06-17": "Coach-directed."})
        assert [v.code for v in r.violations] == ["gravelride_directed_day"]

    def test_a_malformed_register_grants_nothing(self):
        # Fails CLOSED: no key can be silenced by a broken or empty entry.
        for bad in ({"swim:2026-06-17": ""}, {"swim:2026-06-17": True},
                    {"swim-2026-06-17": "x"}, {"badsport:2026-06-17": "x"},
                    {"swim:17/06/2026": "x"}, None):
            r = validate_week(self.WED_SWIM, WEEK, day_rules=DAY_RULES, day_overrides=bad)
            assert [v.code for v in r.violations] == ["swim_forbidden_day"], bad

    def test_repeated_overrides_on_one_weekday_hard_fail_as_drift(self):
        # Three directed Wednesday swims inside the window is the PATTERN, not an
        # exception — the audit must not be able to go quiet on the whole category.
        reg = {f"swim:{d}": "Coach-directed."
               for d in ("2026-06-03", "2026-06-10", "2026-06-17")}
        r = validate_week(self.WED_SWIM, WEEK, day_rules=DAY_RULES, day_overrides=reg)
        drift = [v for v in r.violations if v.code == "day_rules_drifted"]
        assert len(drift) == 1 and drift[0].severity == "hard"
        assert "swim_days" in drift[0].detail          # names the remedy
        assert "Wed" in drift[0].detail

    def test_two_overrides_is_not_yet_drift(self):
        reg = {f"swim:{d}": "Coach-directed." for d in ("2026-06-10", "2026-06-17")}
        r = validate_week(self.WED_SWIM, WEEK, day_rules=DAY_RULES, day_overrides=reg)
        assert not any(v.code == "day_rules_drifted" for v in r.violations)

    def test_drift_ages_out_of_the_window(self):
        # Same three, but long enough ago that the streak has lapsed.
        reg = {f"swim:{d}": "Coach-directed."
               for d in ("2026-04-01", "2026-04-08", "2026-04-15")}
        r = validate_week(self.WED_SWIM, WEEK, day_rules=DAY_RULES, day_overrides=reg)
        assert not any(v.code == "day_rules_drifted" for v in r.violations)


class TestDatedDayRuleExceptions:
    """`<key>_expires` — a time-boxed day permission that reverts on its own."""

    CALUM = {"bike_days": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat"],
             "bike_days_expires": {"Sat": "2026-09-05"}}
    SAT_RIDE = [_ev("2026-06-20", "Ride", 120)]                # Sat inside the window

    def test_a_live_dated_exception_permits_the_day(self):
        r = validate_week(self.SAT_RIDE, WEEK, day_rules=self.CALUM)
        assert not any("_day" in v.code for v in r.violations)

    def test_it_reverts_by_itself_once_the_date_passes(self):
        dr = dict(self.CALUM, bike_days_expires={"Sat": "2026-06-19"})
        r = validate_week(self.SAT_RIDE, WEEK, day_rules=dr)
        assert [(v.code, v.severity) for v in r.violations] == [("ride_forbidden_day", "hard")]
        # the message must name the mechanism, not claim Sat was never permitted
        assert "bike_days_expires" in r.violations[0].detail
        assert "EXPIRED" in r.violations[0].detail

    def test_expiry_is_judged_per_event_date_not_per_run_date(self):
        # A week that starts before the expiry and ends after it: only the late
        # session breaches, so future weeks audit correctly.
        dr = dict(self.CALUM, bike_days_expires={"Sat": "2026-06-20"})
        r = validate_week([_ev("2026-06-20", "Ride", 90), _ev("2026-06-27", "Ride", 90)],
                          WEEK, day_rules=dr)
        assert [v.code for v in r.violations] == []           # 27th is outside the week
        r2 = validate_week([_ev("2026-06-27", "Ride", 90)], date(2026, 6, 22), day_rules=dr)
        assert [v.code for v in r2.violations] == ["ride_forbidden_day"]

    def test_the_sidecar_is_inert_for_a_parser_that_ignores_it(self):
        # config/athletes.json is hand-edited on the live box and is NOT deployed with
        # the code, so the encoding had to be a no-op for the running parser: a
        # dict-valued key is skipped by _normalise_day_rules.
        from primitives.validate_plan import _normalise_day_rules
        assert _normalise_day_rules(self.CALUM) == {"bike_days": {0, 1, 2, 3, 4, 5}}

    def test_a_garbled_expiry_leaves_the_day_permitted(self):
        # Never invent a NEW failure out of a typo: drop the entry, keep today's
        # behaviour.
        for bad in ({"Sat": "not-a-date"}, {"Sat": None}, {"Caturday": "2026-09-05"}, []):
            dr = dict(self.CALUM, bike_days_expires=bad)
            r = validate_week(self.SAT_RIDE, WEEK, day_rules=dr)
            assert not any("_day" in v.code for v in r.violations), bad



class TestRestDay:
    """Every week needs one full rest day (Jamie, 3 Aug 2026).

    Deliberately NOT opt-in: the events alone answer it, and a rest day is a
    requirement for every athlete. The two cases worth guarding are the false
    fires - a part-planned week, and a zero-load mobility day.
    """

    def _seven_days(self, load: float = 40) -> list[dict]:
        return [_ev(f"2026-06-{15 + i}", "Run", load) for i in range(7)]

    def _rest_codes(self, rep) -> list[tuple[str, str]]:
        return [(v.code, v.severity) for v in rep.violations if "rest" in v.code]

    def test_clean_week_has_a_rest_day(self):
        # _clean_week() leaves Wed 17 empty, which is the point.
        r = validate_week(_clean_week(), WEEK)
        assert self._rest_codes(r) == []

    def test_seven_loaded_days_is_hard(self):
        r = validate_week(self._seven_days(), WEEK)
        assert self._rest_codes(r) == [("no_rest_day", "hard")]
        assert not r.ok

    def test_waiver_downgrades_to_soft_and_records_the_reason(self):
        r = validate_week(self._seven_days(), WEEK,
                          rest_day_waiver="race week — every day is a 20min opener")
        assert self._rest_codes(r) == [("no_rest_day_waived", "soft")]
        v = next(v for v in r.violations if "rest" in v.code)
        assert "20min opener" in v.detail
        # WeekReport.ok is `not violations` (soft included), so the thing that
        # matters for a waiver is that nothing HARD was raised.
        assert not r.hard

    def test_zero_load_mobility_still_counts_as_rest(self):
        # Mobility is logged as a zero-TSS workout; it must not cost the rest day.
        week = self._seven_days()[:6] + [_ev("2026-06-21", "Workout", 0)]
        assert self._rest_codes(validate_week(week, WEEK)) == []

    def test_part_planned_week_does_not_false_fire(self):
        week = [_ev("2026-06-15", "Run", 40), _ev("2026-06-16", "Swim", 35)]
        assert self._rest_codes(validate_week(week, WEEK)) == []

    def test_empty_week_does_not_false_fire(self):
        assert self._rest_codes(validate_week([], WEEK)) == []

    def test_two_rest_days_can_be_required(self):
        week = self._seven_days()[:6]          # one rest day only
        r = validate_week(week, WEEK, rest_days_min=2)
        assert self._rest_codes(r) == [("no_rest_day", "hard")]

    def test_can_be_disabled(self):
        r = validate_week(self._seven_days(), WEEK, rest_days_min=0)
        assert self._rest_codes(r) == []

    def test_multiple_sessions_on_one_day_still_one_loaded_day(self):
        # Six days loaded, one of them twice -> still a rest day on the seventh.
        week = self._seven_days()[:6] + [_ev("2026-06-15", "Swim", 30)]
        assert self._rest_codes(validate_week(week, WEEK)) == []
