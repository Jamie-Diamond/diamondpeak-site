"""Tests for primitives/load.py.

Coverage:
    - dedupe (id-based, secondary-key for Garmin duplicates)
    - daily_tss (local-date bucketing, the real 14 April case)
    - banister_series numerical correctness against Banister theory
    - banister handling of missing days, future projections
    - weekly_ramp arithmetic
    - atl_ctl_gap_streak counter behaviour
    - flag_conditions for ramp + sustained gap
    - trajectory_check for past + future targets
    - separate_actual_projection split
    - real-data fixture coherence (TSB = CTL - ATL etc.)
"""
from __future__ import annotations

import math
from datetime import date, datetime, timedelta

import pytest

from primitives.load import (
    BUILD_TABLE,
    LoadPoint,
    Flag,
    atl_ctl_gap_streak,
    banister_series,
    daily_tss,
    dedupe_activities,
    fitness_rows_to_loadpoints,
    flag_conditions,
    separate_actual_projection,
    trajectory_check,
    weekly_ramp,
    compute_required_tss,
    compute_projected_ctl,
    derive_phase_ctl_targets,
    compute_race_min_ctl,
    phase_ctl_band_targets,
)


# ---------------------------------------------------------------------------
# dedupe_activities
# ---------------------------------------------------------------------------

class TestDedup:
    def test_april_14_duplicate_dropped(self, april_14_duplicate):
        """The two distinct-id VirtualRide rows on 14 Apr collapse to one."""
        deduped = dedupe_activities(april_14_duplicate["activities"])
        assert len(deduped) == april_14_duplicate["expected_unique_activity_count"]
        # The dropped id should not be in output
        ids_out = {a["id"] for a in deduped}
        # Exactly one of the two virtual ride ids survives
        virtual_ride_ids = {"i139909103", "i139908846"}
        assert len(ids_out & virtual_ride_ids) == 1

    def test_id_collision_keeps_one(self):
        """If ids collide, only one row survives (last write wins)."""
        a = [
            {"id": "x1", "date": "2026-04-01T08:00:00", "duration_minutes": 60, "tss": 50},
            {"id": "x1", "date": "2026-04-01T08:00:00", "duration_minutes": 60, "tss": 99},
        ]
        out = dedupe_activities(a)
        assert len(out) == 1
        assert out[0]["tss"] == 99

    def test_distinct_metrics_preserved(self):
        """Two activities at different times — both kept."""
        a = [
            {"id": "x1", "date": "2026-04-01T07:00:00", "duration_minutes": 30, "normalized_power": 200, "tss": 25},
            {"id": "x2", "date": "2026-04-01T18:00:00", "duration_minutes": 30, "normalized_power": 200, "tss": 25},
        ]
        out = dedupe_activities(a)
        assert len(out) == 2

    def test_null_metrics_do_not_collide(self):
        """Two activities both with null power/duration null — kept separate."""
        a = [
            {"id": "x1", "date": "2026-04-01T07:00:00", "duration_minutes": None, "normalized_power": None, "tss": None},
            {"id": "x2", "date": "2026-04-01T07:00:00", "duration_minutes": None, "normalized_power": None, "tss": None},
        ]
        out = dedupe_activities(a)
        assert len(out) == 2  # secondary key inactive when nulls dominate

    def test_missing_id_skipped(self):
        a = [{"date": "2026-04-01T07:00:00", "tss": 10}]
        assert dedupe_activities(a) == []


# ---------------------------------------------------------------------------
# daily_tss
# ---------------------------------------------------------------------------

class TestDailyTss:
    def test_april_14_real_case(self, april_14_duplicate):
        """14 April real activities: 5 unique → daily TSS 93, not 123."""
        tss = daily_tss(april_14_duplicate["activities"])
        assert tss[date(2026, 4, 14)] == pytest.approx(
            april_14_duplicate["expected_daily_tss_deduped"]
        )

    def test_late_evening_buckets_to_local_date(self):
        """An activity at 23:36 local time on day X buckets to day X, not day X+1."""
        a = [{"id": "z", "date": "2026-04-14T23:36:47", "duration_minutes": 30, "tss": 30}]
        tss = daily_tss(a)
        assert list(tss.keys()) == [date(2026, 4, 14)]

    def test_missing_tss_skipped(self):
        a = [{"id": "z", "date": "2026-04-14T08:00:00", "duration_minutes": 30, "tss": None}]
        assert daily_tss(a) == {}

    def test_multiple_activities_summed(self):
        a = [
            {"id": "a", "date": "2026-04-14T08:00:00", "duration_minutes": 30, "tss": 30},
            {"id": "b", "date": "2026-04-14T18:00:00", "duration_minutes": 60, "tss": 50},
        ]
        tss = daily_tss(a)
        assert tss[date(2026, 4, 14)] == 80


# ---------------------------------------------------------------------------
# banister_series
# ---------------------------------------------------------------------------

class TestBanister:
    def test_constant_tss_42d_known_answer(self, constant_tss_42d):
        """After 42 days of TSS=100 from zero seed, CTL ≈ 100*(1-(41/42)^42)."""
        start = date(2026, 1, 1)
        end = date(2026, 2, 11)  # day 42 (inclusive)
        series = banister_series(constant_tss_42d, start, end)

        assert len(series) == 42
        last = series[-1]

        # Banister analytic value: 100 * (1 - (1 - 1/42)^42)
        expected_ctl = 100.0 * (1.0 - (1.0 - 1.0 / 42.0) ** 42)
        expected_atl = 100.0 * (1.0 - (1.0 - 1.0 / 7.0) ** 42)

        assert last.ctl == pytest.approx(expected_ctl, abs=0.05)
        assert last.atl == pytest.approx(expected_atl, abs=0.05)
        assert last.tsb == pytest.approx(last.ctl - last.atl, abs=0.05)

    def test_atl_after_7d_known_answer(self, constant_tss_42d):
        """ATL after 7 days of TSS=100 ≈ 100*(1-(6/7)^7) ≈ 66.0."""
        start = date(2026, 1, 1)
        end = date(2026, 1, 7)  # day 7 (inclusive)
        series = banister_series(constant_tss_42d, start, end)
        expected = 100.0 * (1.0 - (1.0 - 1.0 / 7.0) ** 7)
        assert series[-1].atl == pytest.approx(expected, abs=0.05)
        assert series[-1].atl == pytest.approx(66.0, abs=0.5)

    def test_monotone_for_constant_input(self, constant_tss_42d):
        start = date(2026, 1, 1)
        end = date(2026, 2, 11)
        series = banister_series(constant_tss_42d, start, end)
        ctls = [p.ctl for p in series]
        assert ctls == sorted(ctls), "CTL should be monotone non-decreasing"

    def test_long_horizon_approaches_input(self):
        """After 5x time constant, CTL approaches steady-state TSS."""
        daily = {date(2026, 1, 1) + timedelta(days=i): 100.0 for i in range(300)}
        end = date(2026, 1, 1) + timedelta(days=210)
        series = banister_series(daily, date(2026, 1, 1), end)
        # 5 * 42 = 210 days → ~99.3% of asymptote
        assert series[-1].ctl == pytest.approx(100.0, abs=1.0)

    def test_missing_days_treated_as_rest(self):
        """A day not in `daily` is TSS=0 (true rest)."""
        daily = {date(2026, 1, 1): 100.0}  # only one entry
        series = banister_series(daily, date(2026, 1, 1), date(2026, 1, 5))
        assert len(series) == 5
        # CTL must drop after day 1
        assert series[1].ctl < series[0].ctl

    def test_future_dates_flagged_projection(self):
        daily = {date(2026, 1, 1): 100.0}
        series = banister_series(
            daily,
            start=date(2026, 1, 1),
            end=date(2026, 1, 10),
            today=date(2026, 1, 1),
        )
        assert series[0].is_projection is False
        assert all(p.is_projection for p in series[1:])

    def test_seeds_carried_forward(self):
        """Seed values shift the whole series."""
        daily = {}
        s1 = banister_series(daily, date(2026, 1, 1), date(2026, 1, 1), seed_ctl=0, seed_atl=0)
        s2 = banister_series(daily, date(2026, 1, 1), date(2026, 1, 1), seed_ctl=80, seed_atl=80)
        assert s2[0].ctl > s1[0].ctl


# ---------------------------------------------------------------------------
# weekly_ramp
# ---------------------------------------------------------------------------

class TestWeeklyRamp:
    def test_ramp_is_7d_delta_ctl(self, constant_tss_42d):
        series = banister_series(
            constant_tss_42d, date(2026, 1, 1), date(2026, 2, 11)
        )
        ramps = weekly_ramp(series)
        # First ramp should be at day 8 (index 7 has a partner at index 0)
        assert len(ramps) == len(series) - 7
        # All ramps positive (CTL rising under constant load)
        assert all(r > 0 for _, r in ramps)


# ---------------------------------------------------------------------------
# atl_ctl_gap_streak
# ---------------------------------------------------------------------------

class TestGapStreak:
    def _make_points(self, gaps: list[float]) -> list[LoadPoint]:
        """Build a synthetic series with controlled (atl - ctl) gaps."""
        pts = []
        d = date(2026, 1, 1)
        for g in gaps:
            pts.append(
                LoadPoint(
                    date=d,
                    tss=0,
                    ctl=50.0,
                    atl=50.0 + g,
                    tsb=-g,
                    tsb_pct=-g / 50.0 * 100,
                    is_projection=False,
                )
            )
            d += timedelta(days=1)
        return pts

    def test_no_breach(self):
        pts = self._make_points([0, 5, 10, 24, 24])
        s = atl_ctl_gap_streak(pts)
        assert s["current_streak_days"] == 0
        assert s["longest_streak_days"] == 0
        assert s["in_breach_today"] is False

    def test_current_streak(self):
        pts = self._make_points([0, 26, 27, 28])
        s = atl_ctl_gap_streak(pts)
        assert s["current_streak_days"] == 3
        assert s["in_breach_today"] is True

    def test_longest_recorded(self):
        pts = self._make_points([26, 26, 26, 26, 0, 26])
        s = atl_ctl_gap_streak(pts)
        assert s["longest_streak_days"] == 4
        assert s["current_streak_days"] == 1

    def test_threshold_is_strict_greater(self):
        # gap == threshold (25.0) is NOT a breach
        pts = self._make_points([25.0, 25.0])
        s = atl_ctl_gap_streak(pts)
        assert s["current_streak_days"] == 0


# ---------------------------------------------------------------------------
# flag_conditions
# ---------------------------------------------------------------------------

class TestFlags:
    def test_ramp_flag_when_ankle(self):
        """Big ramp + ankle-in-rehab → ramp flag."""
        # Build a series where CTL rises 5 in 7 days
        pts = []
        for i in range(8):
            pts.append(
                LoadPoint(
                    date=date(2026, 1, 1) + timedelta(days=i),
                    tss=100,
                    ctl=70.0 + i * 0.8,  # rises 5.6 in 7 days
                    atl=70.0,
                    tsb=0.0,
                    tsb_pct=0.0,
                    is_projection=False,
                )
            )
        flags = flag_conditions(pts, ankle_in_rehab=True)
        codes = {f.code for f in flags}
        assert "ramp_too_hot_ankle" in codes

    def test_ramp_no_flag_when_not_in_rehab(self):
        pts = []
        for i in range(8):
            pts.append(
                LoadPoint(
                    date=date(2026, 1, 1) + timedelta(days=i),
                    tss=100, ctl=70.0 + i * 0.8, atl=70.0,
                    tsb=0.0, tsb_pct=0.0, is_projection=False,
                )
            )
        flags = flag_conditions(pts, ankle_in_rehab=False)
        codes = {f.code for f in flags}
        assert "ramp_too_hot_ankle" not in codes

    def test_sustained_gap_flag(self):
        pts = []
        for i in range(7):
            pts.append(
                LoadPoint(
                    date=date(2026, 1, 1) + timedelta(days=i),
                    tss=200, ctl=70.0, atl=100.0,  # gap = 30
                    tsb=-30.0, tsb_pct=-42.9, is_projection=False,
                )
            )
        flags = flag_conditions(pts, ankle_in_rehab=False)
        codes = {f.code for f in flags}
        assert "atl_ctl_gap_sustained" in codes


# ---------------------------------------------------------------------------
# trajectory_check
# ---------------------------------------------------------------------------

class TestTrajectoryCheck:
    def test_past_target_uses_actual(self):
        # Build a series ending today with CTL above the past target
        target_date = date(2026, 5, 31)
        end = target_date + timedelta(days=10)
        pts = []
        for i, d_ in enumerate(
            (date(2026, 5, 25) + timedelta(days=i) for i in range(20))
        ):
            pts.append(
                LoadPoint(
                    date=d_, tss=80, ctl=85.0, atl=85.0,
                    tsb=0.0, tsb_pct=0.0, is_projection=False,
                )
            )
        out = trajectory_check(pts)
        end_base = next(t for t in out if t["label"] == "End base")
        assert end_base["is_projected"] is False
        assert end_base["status"] == "on_track"
        assert end_base["ctl_on_target_date"] == 85.0

    def test_future_target_projects(self):
        # Series of 14 days, all today's CTL = 70, no ramp
        pts = []
        for i in range(14):
            pts.append(
                LoadPoint(
                    date=date(2026, 4, 12) + timedelta(days=i),
                    tss=50, ctl=70.0, atl=70.0,
                    tsb=0.0, tsb_pct=0.0, is_projection=False,
                )
            )
        out = trajectory_check(pts)
        end_base = next(t for t in out if t["label"] == "End base")
        # No ramp → projected CTL = 70 → below 82
        assert end_base["is_projected"] is True
        assert end_base["status"] == "below"
        assert end_base["delta_low"] < 0


# ---------------------------------------------------------------------------
# separate_actual_projection
# ---------------------------------------------------------------------------

def test_separate_split_by_today():
    today = date(2026, 4, 25)
    pts = [
        LoadPoint(date=today - timedelta(days=1), tss=0, ctl=70, atl=70, tsb=0, tsb_pct=0, is_projection=False),
        LoadPoint(date=today, tss=100, ctl=72, atl=85, tsb=-13, tsb_pct=-18, is_projection=False),
        LoadPoint(date=today + timedelta(days=1), tss=0, ctl=72, atl=72, tsb=0, tsb_pct=0, is_projection=True),
    ]
    actual, proj = separate_actual_projection(pts, today)
    assert len(actual) == 2
    assert len(proj) == 1


# ---------------------------------------------------------------------------
# Real-data fixture coherence checks
# ---------------------------------------------------------------------------

class TestRealDataCoherence:
    def test_tsb_equals_ctl_minus_atl(self, fitness_90d):
        """API invariant: TSB = CTL - ATL within rounding.

        Intervals.icu stores each of CTL/ATL/TSB rounded independently to 1 dp,
        so the identity holds only within ±0.15 in the worst case (3× ±0.05).
        Tolerance of 0.2 keeps the test honest while accepting documented drift.
        """
        for row in fitness_90d:
            assert row["tsb"] == pytest.approx(row["ctl"] - row["atl"], abs=0.2)

    def test_no_negative_ctl(self, fitness_90d):
        assert all(r["ctl"] >= 0 for r in fitness_90d)

    def test_load_field_always_null(self, fitness_90d):
        """Documented gotcha — load is null in API. If this fails, schema changed."""
        assert all(r.get("load") is None for r in fitness_90d)

    def test_fitness_rows_to_loadpoints_round_trip(self, fitness_90d):
        today = date(2026, 4, 25)
        pts = fitness_rows_to_loadpoints(fitness_90d, today=today)
        assert len(pts) == len(fitness_90d)
        # No future rows in this fixture, so no projections
        assert all(not p.is_projection for p in pts)
        # Match API values (allow rounding noise)
        api_by_date = {row["date"]: row for row in fitness_90d}
        for p in pts:
            row = api_by_date[p.date.isoformat()]
            assert p.ctl == pytest.approx(row["ctl"], abs=0.05)
            assert p.atl == pytest.approx(row["atl"], abs=0.05)
            assert p.tsb == pytest.approx(row["tsb"], abs=0.05)


# ---------------------------------------------------------------------------
# Forward plan-generation maths (consolidated from generate-plan.py)
# ---------------------------------------------------------------------------

# Jamie config/profile snapshot — golden anchors captured 2026-06-07 from the
# pre-consolidation inline implementation. These pin the refactor as a no-op.
JAMIE_CFG = {
    "ctl_targets": {"phase_ctl": {"base": 85, "build": 95, "specific": 105, "peak": 112},
                    "race_min": 97},
    "phase_tss": {"base_end_week": 5, "build_end_week": 10,
                  "specific_end_week": 14, "peak_end_week": 17},
    "plan_start": "2026-04-27",
    "max_ctl_ramp_per_week": 4.0,
    "race_target_splits": {"swim_min": 67, "bike_min": 284, "run_min": 210,
                           "bike_np_target_watts": 225, "t1t2_min": 5.5},
}
JAMIE_PROFILE = {
    "ftp_watts": 316, "race_distance": "Full Ironman",
    "run_threshold_pace_per_km": "4:02", "race_date": "2026-09-19",
}


class TestComputeRequiredTss:
    def test_golden_anchors(self):
        # Pinned from the inline implementation pre-refactor.
        assert compute_required_tss(79, 95, 5) == 749
        assert compute_required_tss(79, 112, 17) == 797

    def test_zero_or_negative_horizon(self):
        # N <= 0 falls back to target * 7.
        assert compute_required_tss(80, 90, 0) == 90 * 7

    def test_clamps_at_zero_when_already_above(self):
        # Target below current decayed CTL must never demand negative TSS.
        assert compute_required_tss(120, 60, 8) >= 0

    def test_higher_target_needs_more_tss(self):
        assert compute_required_tss(79, 100, 8) > compute_required_tss(79, 90, 8)


class TestComputeProjectedCtl:
    def test_golden_anchor(self):
        assert compute_projected_ctl(79, 650, 5) == pytest.approx(86.8953, abs=1e-4)

    def test_holding_at_equilibrium_is_stable(self):
        # weekly_tss == ctl*7 is the daily-CTL fixed point: CTL should barely move.
        assert compute_projected_ctl(80, 80 * 7, 6) == pytest.approx(80.0, abs=0.5)

    def test_round_trip_required_then_projected_reaches_target(self):
        # The core self-consistency: TSS required to hit a target, when applied,
        # lands within rounding error of that target.
        ctl0, target, weeks = 79.0, 100.0, 10
        req = compute_required_tss(ctl0, target, weeks)
        landed = compute_projected_ctl(ctl0, req, weeks)
        assert landed == pytest.approx(target, abs=2.0)


class TestDerivePhaseCtlTargets:
    def test_golden_anchor_with_injected_today(self):
        d = derive_phase_ctl_targets(
            79, 100, date(2026, 4, 27), 5, 10, 14, 17, 4.0, 1.15,
            today=date(2026, 6, 7),
        )
        assert d == {"base": 80, "build": 87, "specific": 93, "peak": 96}

    def test_today_is_injectable_and_deterministic(self):
        a = derive_phase_ctl_targets(79, 100, date(2026, 4, 27), 5, 10, 14, 17, 4.0,
                                     today=date(2026, 6, 7))
        b = derive_phase_ctl_targets(79, 100, date(2026, 4, 27), 5, 10, 14, 17, 4.0,
                                     today=date(2026, 6, 7))
        assert a == b

    def test_targets_non_decreasing_across_phases(self):
        d = derive_phase_ctl_targets(79, 110, date(2026, 4, 27), 5, 10, 14, 17, 5.0,
                                     today=date(2026, 5, 1))
        assert d["base"] <= d["build"] <= d["specific"] <= d["peak"]

    def test_floor_above_current_ctl(self):
        # Every target must sit at least 1 above current CTL.
        d = derive_phase_ctl_targets(90, 100, date(2026, 4, 27), 5, 10, 14, 17, 4.0,
                                     today=date(2026, 6, 7))
        assert all(v >= round(90) + 1 for v in d.values())


class TestComputeRaceMinCtl:
    def test_jamie_golden(self):
        assert compute_race_min_ctl(JAMIE_CFG, JAMIE_PROFILE) == 100

    def test_none_when_splits_absent(self):
        assert compute_race_min_ctl({}, JAMIE_PROFILE) is None

    def test_none_when_splits_incomplete(self):
        cfg = {"race_target_splits": {"swim_min": 67, "bike_min": 0, "run_min": 210}}
        assert compute_race_min_ctl(cfg, JAMIE_PROFILE) is None

    def test_scales_with_duration(self):
        slow = {**JAMIE_CFG, "race_target_splits":
                {**JAMIE_CFG["race_target_splits"], "bike_min": 360, "run_min": 270}}
        assert compute_race_min_ctl(slow, JAMIE_PROFILE) > compute_race_min_ctl(JAMIE_CFG, JAMIE_PROFILE)

    def test_half_distance_branch(self):
        half_profile = {**JAMIE_PROFILE, "race_distance": "70.3"}
        half_cfg = {**JAMIE_CFG, "race_target_splits":
                    {"swim_min": 33, "bike_min": 150, "run_min": 100,
                     "bike_np_target_watts": 250}}
        assert compute_race_min_ctl(half_cfg, half_profile) is not None


class TestPhaseCtlBandTargets:
    def test_builds_one_milestone_per_configured_phase(self):
        out = phase_ctl_band_targets(JAMIE_CFG, date(2026, 4, 27))
        assert [label for *_, label in out] == ["End base", "End build", "End specific", "Peak"]

    def test_bands_centre_on_configured_ctl(self):
        out = phase_ctl_band_targets(JAMIE_CFG, date(2026, 4, 27), band=3.0)
        base = next(t for t in out if t[3] == "End base")
        _, lo, hi, _ = base
        assert (lo, hi) == (82.0, 88.0)  # 85 +/- 3

    def test_dates_match_phase_end_weeks(self):
        out = phase_ctl_band_targets(JAMIE_CFG, date(2026, 4, 27))
        base_date = next(t[0] for t in out if t[3] == "End base")
        assert base_date == date(2026, 4, 27) + timedelta(weeks=5)

    def test_race_day_appended_when_race_date_given(self):
        out = phase_ctl_band_targets(JAMIE_CFG, date(2026, 4, 27),
                                     race_date=date(2026, 9, 19))
        assert out[-1][3] == "Race day"
        assert out[-1][0] == date(2026, 9, 19)

    def test_empty_when_no_phase_ctl(self):
        assert phase_ctl_band_targets({"phase_tss": {}}, date(2026, 4, 27)) == []
