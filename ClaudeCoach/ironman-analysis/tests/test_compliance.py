"""Tests for primitives/compliance.py."""
from __future__ import annotations

import pytest

from primitives.compliance import (
    ComplianceRecord,
    classify_gap,
    compliance_recommendations,
    forward_correction_factor,
    rolling_compliance,
    tss_gap_series,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _record(
    planned=100.0,
    actual=95.0,
    planned_dur=90.0,
    actual_dur=87.0,
    rpe=6,
    classification="completed",
) -> ComplianceRecord:
    return ComplianceRecord(
        session_date="2026-04-01",
        sport="bike",
        session_name="Threshold",
        planned_tss=planned,
        actual_tss=actual,
        planned_duration_min=planned_dur,
        actual_duration_min=actual_dur,
        rpe=rpe,
        gap_classification=classification,
        gap_pct=(actual - planned) / planned if planned else 0.0,
    )


# ---------------------------------------------------------------------------
# classify_gap
# ---------------------------------------------------------------------------

class TestClassifyGap:
    def test_skipped_zero_duration(self):
        assert classify_gap(100, 0, 90, 0, None) == "skipped"

    def test_skipped_zero_tss(self):
        assert classify_gap(100, 0, 90, 85, None) == "skipped"

    def test_duration_short_below_threshold(self):
        # 70 min actual vs 90 min planned = 77.8% < 80%
        assert classify_gap(100, 60, 90, 70, 6) == "duration_short"

    def test_duration_exactly_at_threshold_not_short(self):
        # Exactly 80% (72/90) — not < 0.80, so falls through to TSS check
        result = classify_gap(100, 90, 90, 72, 6)
        assert result != "duration_short"

    def test_intensity_short_fatigued_high_rpe(self):
        assert classify_gap(100, 80, 90, 88, 8) == "intensity_short_fatigued"

    def test_intensity_short_fatigued_rpe_at_boundary(self):
        # RPE exactly 7 = fatigued threshold
        assert classify_gap(100, 80, 90, 88, 7) == "intensity_short_fatigued"

    def test_intensity_short_soft_low_rpe(self):
        assert classify_gap(100, 80, 90, 88, 6) == "intensity_short_soft"

    def test_intensity_short_soft_rpe_zero(self):
        assert classify_gap(100, 80, 90, 88, 0) == "intensity_short_soft"

    def test_intensity_short_unknown_no_rpe(self):
        assert classify_gap(100, 80, 90, 88, None) == "intensity_short_unknown"

    def test_completed_within_12_pct(self):
        assert classify_gap(100, 92, 90, 88, 6) == "completed"

    def test_completed_at_tss_boundary(self):
        # Exactly 88% is not < 0.88, so completed
        assert classify_gap(100, 88, 90, 88, 6) == "completed"

    def test_just_below_tss_boundary(self):
        assert classify_gap(100, 87, 90, 88, 5) == "intensity_short_soft"

    def test_over_target_still_completed(self):
        assert classify_gap(100, 120, 90, 100, 8) == "completed"

    def test_zero_planned_duration_no_crash(self):
        # planned_duration_min = 0 — duration_ratio defaults to 1.0
        result = classify_gap(100, 80, 0, 0, None)
        assert result == "skipped"  # actual_duration_min = 0


# ---------------------------------------------------------------------------
# tss_gap_series
# ---------------------------------------------------------------------------

class TestTssGapSeries:
    def _planned(self, date="2026-04-01", tss=100.0, dur=90.0, name="Threshold", sport="Ride"):
        return {"date": date, "name": name, "type": sport, "planned_tss": tss, "planned_duration_min": dur}

    def _actual(self, date="2026-04-01", tss=95.0, dur_min=87.0, sport="Ride"):
        return {"date": date, "type": sport, "tss": tss, "duration_minutes": dur_min}

    def _log_entry(self, date="2026-04-01", sport="Ride", rpe=7):
        return {"date": date, "sport": sport, "rpe": rpe}

    def test_matched_completed(self):
        records = tss_gap_series([self._planned()], [self._actual()], [self._log_entry(rpe=6)])
        assert len(records) == 1
        assert records[0].gap_classification == "completed"
        assert records[0].rpe == 6

    def test_unmatched_is_skipped(self):
        records = tss_gap_series([self._planned()], [], [])
        assert len(records) == 1
        assert records[0].gap_classification == "skipped"
        assert records[0].actual_tss == 0.0

    def test_rpe_lookup_fatigued(self):
        records = tss_gap_series(
            [self._planned(tss=100)],
            [self._actual(tss=80, dur_min=88)],
            [self._log_entry(rpe=8)],
        )
        assert records[0].gap_classification == "intensity_short_fatigued"

    def test_rpe_lookup_soft(self):
        records = tss_gap_series(
            [self._planned(tss=100)],
            [self._actual(tss=80, dur_min=88)],
            [self._log_entry(rpe=5)],
        )
        assert records[0].gap_classification == "intensity_short_soft"

    def test_no_rpe_gives_unknown(self):
        records = tss_gap_series(
            [self._planned(tss=100)],
            [self._actual(tss=80, dur_min=88)],
            [],
        )
        assert records[0].gap_classification == "intensity_short_unknown"

    def test_event_without_planned_tss_skipped(self):
        event = {"date": "2026-04-01", "name": "Easy Ride", "type": "Ride", "planned_tss": 0}
        records = tss_gap_series([event], [self._actual()], [])
        assert len(records) == 0

    def test_event_with_none_planned_tss_skipped(self):
        event = {"date": "2026-04-01", "name": "Easy Ride", "type": "Ride"}
        records = tss_gap_series([event], [self._actual()], [])
        assert len(records) == 0

    def test_sport_category_mapping_virtual_ride(self):
        planned = self._planned(sport="VirtualRide")
        actual = self._actual(sport="VirtualRide")
        records = tss_gap_series([planned], [actual], [])
        assert records[0].sport == "bike"

    def test_sport_category_mapping_run(self):
        planned = self._planned(sport="Run")
        actual = self._actual(sport="Run")
        records = tss_gap_series([planned], [actual], [])
        assert records[0].sport == "run"

    def test_gap_pct_negative_when_under(self):
        records = tss_gap_series(
            [self._planned(tss=100)],
            [self._actual(tss=90)],
            [],
        )
        assert records[0].gap_pct == pytest.approx(-0.10, abs=0.01)

    def test_gap_pct_positive_when_over(self):
        records = tss_gap_series(
            [self._planned(tss=100)],
            [self._actual(tss=110)],
            [],
        )
        assert records[0].gap_pct == pytest.approx(0.10, abs=0.01)

    def test_moving_time_preferred_over_duration_minutes(self):
        # moving_time in seconds: 5400 = 90 min
        actual = {"date": "2026-04-01", "type": "Ride", "tss": 95.0, "moving_time": 5400}
        records = tss_gap_series([self._planned(dur=90.0)], [actual], [])
        assert records[0].actual_duration_min == pytest.approx(90.0)

    def test_multiple_activities_takes_highest_tss(self):
        actuals = [
            self._actual(tss=60.0),
            self._actual(tss=95.0),  # this should win
        ]
        records = tss_gap_series([self._planned(tss=100)], actuals, [])
        assert records[0].actual_tss == 95.0

    def test_multiple_events_different_dates(self):
        planned = [
            self._planned(date="2026-04-01", tss=100),
            self._planned(date="2026-04-03", tss=80),
        ]
        actual = [
            self._actual(date="2026-04-01", tss=95),
            self._actual(date="2026-04-03", tss=75),
        ]
        records = tss_gap_series(planned, actual, [])
        assert len(records) == 2
        assert records[0].session_date == "2026-04-01"
        assert records[1].session_date == "2026-04-03"

    def test_iso_datetime_date_stripped(self):
        actual = {"date": "2026-04-01T06:30:00+01:00", "type": "Ride", "tss": 95.0, "duration_minutes": 87.0}
        records = tss_gap_series([self._planned()], [actual], [])
        assert len(records) == 1
        assert records[0].gap_classification == "completed"


# ---------------------------------------------------------------------------
# rolling_compliance
# ---------------------------------------------------------------------------

class TestRollingCompliance:
    def test_empty_returns_defaults(self):
        r = rolling_compliance([])
        assert r["compliance_rate"] == 1.0
        assert r["session_count"] == 0
        assert r["dominant_gap_type"] is None

    def test_full_compliance(self):
        records = [_record(100, 100, classification="completed") for _ in range(5)]
        r = rolling_compliance(records)
        assert r["compliance_rate"] == pytest.approx(1.0)
        assert r["completion_rate"] == pytest.approx(1.0)
        assert r["dominant_gap_type"] is None

    def test_partial_compliance_rate(self):
        records = [
            _record(100, 90, classification="intensity_short_soft"),
            _record(100, 90, classification="intensity_short_soft"),
            _record(100, 100, classification="completed"),
            _record(100, 100, classification="completed"),
        ]
        r = rolling_compliance(records)
        assert r["compliance_rate"] == pytest.approx(0.95)

    def test_dominant_gap_type_selected(self):
        records = [
            _record(classification="intensity_short_soft"),
            _record(classification="intensity_short_soft"),
            _record(classification="skipped"),
            _record(classification="completed"),
        ]
        r = rolling_compliance(records)
        assert r["dominant_gap_type"] == "intensity_short_soft"

    def test_skipped_sessions_reduce_compliance_to_zero(self):
        records = [
            _record(100, 0, classification="skipped"),
            _record(100, 0, classification="skipped"),
        ]
        r = rolling_compliance(records)
        assert r["compliance_rate"] == pytest.approx(0.0)

    def test_classification_counts_accurate(self):
        records = [
            _record(classification="completed"),
            _record(classification="completed"),
            _record(classification="skipped"),
        ]
        r = rolling_compliance(records)
        assert r["classification_counts"]["completed"] == 2
        assert r["classification_counts"]["skipped"] == 1

    def test_completion_rate(self):
        records = [
            _record(classification="completed"),
            _record(classification="skipped"),
            _record(classification="skipped"),
            _record(classification="completed"),
        ]
        r = rolling_compliance(records)
        assert r["completion_rate"] == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# forward_correction_factor
# ---------------------------------------------------------------------------

class TestForwardCorrectionFactor:
    def test_fully_compliant_no_correction(self):
        assert forward_correction_factor(1.0) == 1.0

    def test_above_threshold_no_correction(self):
        assert forward_correction_factor(0.98) == 1.0

    def test_at_boundary_no_correction(self):
        # 97% is the boundary — not < 0.97
        assert forward_correction_factor(0.97) == 1.0

    def test_just_below_threshold_corrects(self):
        f = forward_correction_factor(0.96)
        assert f == pytest.approx(1.0 / 0.96, abs=0.01)

    def test_90_pct_compliance(self):
        f = forward_correction_factor(0.90)
        assert f == pytest.approx(1.0 / 0.90, abs=0.01)

    def test_80_pct_compliance_capped(self):
        # 1/0.80 = 1.25, but cap is 1.20
        f = forward_correction_factor(0.80)
        assert f == 1.20

    def test_capped_at_1_20(self):
        assert forward_correction_factor(0.75) == 1.20

    def test_below_floor_no_correction(self):
        assert forward_correction_factor(0.65) == 1.0
        assert forward_correction_factor(0.50) == 1.0
        assert forward_correction_factor(0.0) == 1.0

    def test_exactly_at_floor_applies_correction(self):
        # 70% is not < 0.70, so correction applies
        f = forward_correction_factor(0.70)
        assert f == 1.20  # 1/0.70 = 1.4286, capped at 1.20


# ---------------------------------------------------------------------------
# compliance_recommendations
# ---------------------------------------------------------------------------

class TestComplianceRecommendations:
    def _metrics(self, rate, dominant, count=10):
        return {
            "compliance_rate": rate,
            "dominant_gap_type": dominant,
            "session_count": count,
        }

    def test_fully_compliant_empty_recs(self):
        recs = compliance_recommendations(self._metrics(1.0, None))
        assert recs == []

    def test_insufficient_data(self):
        recs = compliance_recommendations(self._metrics(0.80, "skipped", count=3))
        assert len(recs) == 1
        assert "not enough data" in recs[0]

    def test_skipped_dominant(self):
        recs = compliance_recommendations(self._metrics(0.85, "skipped"))
        assert len(recs) == 1
        assert "adherence" in recs[0]
        assert "Scaling" in recs[0]

    def test_fatigued_dominant(self):
        recs = compliance_recommendations(self._metrics(0.88, "intensity_short_fatigued"))
        assert len(recs) == 1
        assert "ambitious" in recs[0] or "recovery" in recs[0]

    def test_soft_dominant_includes_factor(self):
        recs = compliance_recommendations(self._metrics(0.90, "intensity_short_soft"))
        assert "×" in recs[0]  # includes correction factor
        assert "1.11" in recs[0]

    def test_duration_short_dominant(self):
        recs = compliance_recommendations(self._metrics(0.82, "duration_short"))
        assert "scheduling" in recs[0].lower() or "time" in recs[0].lower()

    def test_unknown_dominant_asks_for_rpe(self):
        recs = compliance_recommendations(self._metrics(0.85, "intensity_short_unknown"))
        assert "RPE" in recs[0]
