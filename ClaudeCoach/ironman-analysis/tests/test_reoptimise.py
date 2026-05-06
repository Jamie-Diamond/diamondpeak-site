"""Tests for primitives/reoptimise.py."""
from __future__ import annotations

import pytest

from primitives.reoptimise import (
    WeekDebt,
    assess_week_debt,
    apply_compliance_correction,
    quality_session_spacing_ok,
    ramp_headroom,
    _MAX_RAMP_CTL_PER_WEEK_REHAB,
    _MAX_RAMP_CTL_PER_WEEK_NORMAL,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _planned(date: str, tss: float = 100.0) -> dict:
    return {"date": date, "planned_tss": tss}


def _actual(date: str, tss: float = 90.0) -> dict:
    return {"date": date, "tss": tss}


# ---------------------------------------------------------------------------
# assess_week_debt
# ---------------------------------------------------------------------------

class TestAssessWeekDebt:
    def test_no_debt_all_completed(self):
        # Wednesday mid-week. Mon/Tue planned and completed.
        debt = assess_week_debt(
            planned_sessions=[_planned("2026-04-27", 100), _planned("2026-04-28", 80)],
            actual_sessions=[_actual("2026-04-27", 100), _actual("2026-04-28", 80)],
            today="2026-04-29",  # Wednesday
        )
        assert debt.debt_tss == pytest.approx(0.0)
        assert debt.redistributable is True

    def test_partial_miss_is_redistributable(self):
        # Missed Tuesday (30 TSS), Monday completed (100 TSS). Today = Wednesday.
        # Debt = 30 / 130 total = 23% — under 40% threshold → redistributable.
        debt = assess_week_debt(
            planned_sessions=[_planned("2026-04-27", 100), _planned("2026-04-28", 30)],
            actual_sessions=[_actual("2026-04-27", 100)],  # Tuesday skipped
            today="2026-04-29",
        )
        assert debt.debt_tss == pytest.approx(30.0)
        assert debt.days_missed == 1
        assert debt.redistributable is True

    def test_too_many_days_missed_not_redistributable(self):
        # 4 days planned, none completed. Today = Friday.
        planned = [
            _planned("2026-04-27", 100),
            _planned("2026-04-28", 80),
            _planned("2026-04-29", 120),
            _planned("2026-04-30", 90),
        ]
        debt = assess_week_debt(planned, [], today="2026-05-01")
        assert not debt.redistributable
        assert "4 days missed" in debt.reason

    def test_debt_pct_too_high_not_redistributable(self):
        # 300 planned, only 100 completed = 67% debt. Today = Thursday.
        planned = [
            _planned("2026-04-27", 150),
            _planned("2026-04-28", 150),
        ]
        actual = [_actual("2026-04-27", 50)]  # massive undershoot
        debt = assess_week_debt(planned, actual, today="2026-04-29")
        assert not debt.redistributable
        assert "too large" in debt.reason

    def test_end_of_week_not_redistributable(self):
        # Today is Sunday (last day)
        debt = assess_week_debt(
            planned_sessions=[_planned("2026-04-27", 100)],
            actual_sessions=[],
            today="2026-05-03",  # Sunday
        )
        assert not debt.redistributable
        assert "tomorrow" in debt.reason

    def test_week_start_is_monday(self):
        # Verify week_start is always Monday
        debt = assess_week_debt([], [], today="2026-04-29")  # Wednesday
        assert debt.week_start == "2026-04-27"  # Monday

    def test_days_remaining_includes_today(self):
        debt = assess_week_debt([], [], today="2026-04-29")  # Wednesday
        # Wed → Sun = 5 days remaining (Wed, Thu, Fri, Sat, Sun)
        assert debt.days_remaining == 5

    def test_days_elapsed_from_monday(self):
        debt = assess_week_debt([], [], today="2026-04-29")  # Wednesday
        assert debt.days_elapsed == 2  # Mon=0, Tue=1, Wed=2

    def test_future_planned_sessions_excluded_from_debt(self):
        # Plan has Mon (past) and Thu (future). Only Mon counts for debt calc.
        planned = [_planned("2026-04-27", 100), _planned("2026-04-30", 100)]
        debt = assess_week_debt(planned, [], today="2026-04-29")  # Wednesday
        assert debt.debt_tss == pytest.approx(100.0)  # only Monday's missed TSS

    def test_planned_total_includes_all_week_sessions(self):
        # Total planned TSS should include future sessions (for debt_pct denominator)
        planned = [_planned("2026-04-27", 100), _planned("2026-04-30", 100)]
        debt = assess_week_debt(planned, [], today="2026-04-29")
        assert debt.planned_tss == pytest.approx(200.0)

    def test_debt_pct_of_planned_total(self):
        # 100 missed out of 200 total planned = 50% → not redistributable
        planned = [_planned("2026-04-27", 100), _planned("2026-04-30", 100)]
        debt = assess_week_debt(planned, [], today="2026-04-29")
        assert debt.debt_pct == pytest.approx(0.50)
        assert not debt.redistributable

    def test_events_with_no_tss_excluded(self):
        planned = [
            _planned("2026-04-27", 100),
            {"date": "2026-04-28", "planned_tss": 0},   # no TSS — skip
            {"date": "2026-04-28"},                      # missing key — skip
        ]
        debt = assess_week_debt(planned, [_actual("2026-04-27", 100)], today="2026-04-29")
        assert debt.planned_tss == pytest.approx(100.0)
        assert debt.debt_tss == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# ramp_headroom
# ---------------------------------------------------------------------------

class TestRampHeadroom:
    def test_rehab_headroom_at_typical_ctl(self):
        # CTL=100, rehab ramp cap=4/wk. Max weekly TSS = 100×7 + 4×42 = 868
        # Planned=700. Headroom = 168
        h = ramp_headroom(current_ctl=100.0, weekly_planned_tss=700.0, ankle_in_rehab=True)
        assert h == pytest.approx(168.0)

    def test_normal_headroom_higher(self):
        # Normal ramp cap=6. Max = 100×7 + 6×42 = 952. Headroom = 252
        h = ramp_headroom(current_ctl=100.0, weekly_planned_tss=700.0, ankle_in_rehab=False)
        assert h == pytest.approx(252.0)

    def test_at_cap_returns_zero(self):
        # CTL=100, cap=4. Max=868. If already planning 868 TSS, headroom=0.
        h = ramp_headroom(current_ctl=100.0, weekly_planned_tss=868.0, ankle_in_rehab=True)
        assert h == pytest.approx(0.0)

    def test_above_cap_clamped_to_zero(self):
        h = ramp_headroom(current_ctl=100.0, weekly_planned_tss=900.0, ankle_in_rehab=True)
        assert h == 0.0

    def test_low_ctl_builds_headroom(self):
        # Fresh athlete, CTL=50. Max rehab = 50×7 + 4×42 = 518. Planned=400. Headroom=118.
        h = ramp_headroom(current_ctl=50.0, weekly_planned_tss=400.0, ankle_in_rehab=True)
        assert h == pytest.approx(118.0)

    def test_headroom_uses_correct_ramp_constant(self):
        rehab = ramp_headroom(100.0, 700.0, ankle_in_rehab=True)
        normal = ramp_headroom(100.0, 700.0, ankle_in_rehab=False)
        diff = normal - rehab
        # Diff should be (6-4)×42 = 84
        assert diff == pytest.approx(84.0)


# ---------------------------------------------------------------------------
# apply_compliance_correction
# ---------------------------------------------------------------------------

class TestApplyComplianceCorrection:
    def _session(self, stype="bike_threshold", planned_tss=100.0):
        return {"session_type": stype, "planned_tss": planned_tss, "target_intensity": 1.0}

    def test_no_correction_factor_unchanged(self):
        sessions = [self._session()]
        result = apply_compliance_correction(sessions, 1.0)
        assert result[0]["planned_tss"] == pytest.approx(100.0)

    def test_quality_session_corrected(self):
        sessions = [self._session("bike_threshold", 100.0)]
        result = apply_compliance_correction(sessions, 1.10)
        assert result[0]["planned_tss"] == pytest.approx(110.0)

    def test_z2_session_not_corrected(self):
        sessions = [self._session("bike_z2", 100.0)]
        result = apply_compliance_correction(sessions, 1.10)
        assert result[0]["planned_tss"] == pytest.approx(100.0)

    def test_swim_session_not_corrected(self):
        sessions = [self._session("swim", 60.0)]
        result = apply_compliance_correction(sessions, 1.15)
        assert result[0]["planned_tss"] == pytest.approx(60.0)

    def test_target_intensity_unchanged(self):
        sessions = [self._session("bike_threshold", 100.0)]
        result = apply_compliance_correction(sessions, 1.10)
        assert result[0]["target_intensity"] == pytest.approx(1.0)

    def test_originals_not_mutated(self):
        sessions = [self._session("bike_threshold", 100.0)]
        apply_compliance_correction(sessions, 1.10)
        assert sessions[0]["planned_tss"] == pytest.approx(100.0)

    def test_all_correctable_types(self):
        correctable = [
            "bike_threshold", "bike_vo2", "bike_race_pace",
            "run_quality", "run_long", "brick",
        ]
        sessions = [self._session(t, 100.0) for t in correctable]
        result = apply_compliance_correction(sessions, 1.10)
        for r in result:
            assert r["planned_tss"] == pytest.approx(110.0)

    def test_session_without_planned_tss_not_crashed(self):
        sessions = [{"session_type": "bike_threshold"}]
        result = apply_compliance_correction(sessions, 1.10)
        assert "planned_tss" not in result[0]

    def test_run_long_corrected(self):
        sessions = [self._session("run_long", 120.0)]
        result = apply_compliance_correction(sessions, 1.10)
        assert result[0]["planned_tss"] == pytest.approx(132.0)


# ---------------------------------------------------------------------------
# quality_session_spacing_ok
# ---------------------------------------------------------------------------

class TestQualitySessionSpacingOk:
    def _q(self, date: str, stype="bike_threshold"):
        return {"date": date, "session_type": stype}

    def test_no_existing_sessions_ok(self):
        assert quality_session_spacing_ok("2026-04-29", []) is True

    def test_z2_adjacent_ok(self):
        existing = [self._q("2026-04-28", "bike_z2")]
        assert quality_session_spacing_ok("2026-04-29", existing) is True

    def test_quality_day_before_blocked(self):
        existing = [self._q("2026-04-28", "bike_threshold")]
        assert quality_session_spacing_ok("2026-04-29", existing) is False

    def test_quality_day_after_blocked(self):
        existing = [self._q("2026-04-30", "run_quality")]
        assert quality_session_spacing_ok("2026-04-29", existing) is False

    def test_quality_two_days_apart_ok(self):
        existing = [self._q("2026-04-27", "bike_threshold")]
        assert quality_session_spacing_ok("2026-04-29", existing) is True

    def test_same_day_blocked(self):
        existing = [self._q("2026-04-29", "bike_threshold")]
        assert quality_session_spacing_ok("2026-04-29", existing) is False

    def test_brick_counts_as_quality(self):
        existing = [self._q("2026-04-28", "brick")]
        assert quality_session_spacing_ok("2026-04-29", existing) is False

    def test_strength_does_not_block(self):
        existing = [self._q("2026-04-28", "strength")]
        assert quality_session_spacing_ok("2026-04-29", existing) is True

    def test_swim_does_not_block(self):
        existing = [self._q("2026-04-28", "swim")]
        assert quality_session_spacing_ok("2026-04-29", existing) is True
