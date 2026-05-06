"""Tests for primitives/debrief.py."""
from __future__ import annotations

import pytest

from primitives.debrief import (
    DebriefResult,
    LapMetrics,
    POWER_ZONES,
    build_debrief,
    hr_power_decoupling,
    lap_drift,
    power_zone_distribution,
    session_quality_label,
    _parse_laps,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _lap(duration_s=600.0, avg_watts=None, avg_hr=None, avg_pace=None) -> dict:
    """Raw lap dict as returned by IcuSync get_activity_detail."""
    d: dict = {"moving_time": duration_s}
    if avg_watts is not None:
        d["avg_watts"] = avg_watts
    if avg_hr is not None:
        d["avg_hr"] = avg_hr
    if avg_pace is not None:
        d["avg_pace"] = avg_pace  # s/m
    return d


def _activity(sport="Ride", tss=95.0, name="Threshold") -> dict:
    return {"type": sport, "tss": tss, "name": name}


# ---------------------------------------------------------------------------
# _parse_laps
# ---------------------------------------------------------------------------

class TestParseLaps:
    def test_basic_parsing(self):
        raw = [_lap(600, avg_watts=250, avg_hr=145)]
        laps = _parse_laps(raw)
        assert len(laps) == 1
        assert laps[0].avg_watts == 250.0
        assert laps[0].avg_hr == 145.0
        assert laps[0].duration_s == 600.0

    def test_zero_duration_lap_dropped(self):
        raw = [_lap(0, avg_watts=250), _lap(600, avg_watts=240)]
        laps = _parse_laps(raw)
        assert len(laps) == 1
        assert laps[0].avg_watts == 240.0

    def test_pace_converted_to_s_per_km(self):
        raw = [_lap(600, avg_pace=0.28)]  # 0.28 s/m = 280 s/km
        laps = _parse_laps(raw)
        assert laps[0].avg_pace_s_per_km == pytest.approx(280.0)

    def test_alternate_field_names(self):
        raw = [{"elapsed_time": 500, "average_watts": 260, "average_hr": 150}]
        laps = _parse_laps(raw)
        assert len(laps) == 1
        assert laps[0].avg_watts == 260.0
        assert laps[0].avg_hr == 150.0

    def test_missing_optional_fields_give_none(self):
        raw = [_lap(600)]
        laps = _parse_laps(raw)
        assert laps[0].avg_watts is None
        assert laps[0].avg_hr is None
        assert laps[0].avg_pace_s_per_km is None

    def test_lap_numbers_sequential(self):
        raw = [_lap(300), _lap(300), _lap(300)]
        laps = _parse_laps(raw)
        assert [l.lap_number for l in laps] == [1, 2, 3]

    def test_empty_input(self):
        assert _parse_laps([]) == []


# ---------------------------------------------------------------------------
# lap_drift
# ---------------------------------------------------------------------------

class TestLapDrift:
    def _laps(self, first_watts, last_watts):
        return [
            LapMetrics(1, 600, first_watts, 140, None),
            LapMetrics(2, 600, last_watts, 145, None),
        ]

    def test_power_dropped(self):
        laps = self._laps(250, 225)
        d = lap_drift(laps, "avg_watts")
        assert d == pytest.approx(-10.0, abs=0.1)

    def test_hr_rose(self):
        laps = [
            LapMetrics(1, 600, 250, 140, None),
            LapMetrics(2, 600, 250, 154, None),
        ]
        d = lap_drift(laps, "avg_hr")
        assert d == pytest.approx(10.0, abs=0.1)

    def test_no_change(self):
        laps = self._laps(250, 250)
        assert lap_drift(laps, "avg_watts") == pytest.approx(0.0)

    def test_single_lap_returns_none(self):
        laps = [LapMetrics(1, 600, 250, 140, None)]
        assert lap_drift(laps, "avg_watts") is None

    def test_empty_laps_returns_none(self):
        assert lap_drift([], "avg_watts") is None

    def test_missing_field_first_lap_returns_none(self):
        laps = [
            LapMetrics(1, 600, None, 140, None),
            LapMetrics(2, 600, 240, 145, None),
        ]
        assert lap_drift(laps, "avg_watts") is None

    def test_missing_field_last_lap_returns_none(self):
        laps = [
            LapMetrics(1, 600, 250, 140, None),
            LapMetrics(2, 600, None, 145, None),
        ]
        assert lap_drift(laps, "avg_watts") is None

    def test_pace_drift_positive_means_slower(self):
        laps = [
            LapMetrics(1, 600, None, 140, 300.0),   # 5:00/km
            LapMetrics(2, 600, None, 148, 315.0),   # 5:15/km — slower
        ]
        d = lap_drift(laps, "avg_pace_s_per_km")
        assert d is not None
        assert d > 0  # pace increased in seconds = got slower


# ---------------------------------------------------------------------------
# hr_power_decoupling
# ---------------------------------------------------------------------------

class TestHrPowerDecoupling:
    def test_stable_ratio_returns_low_decoupling(self):
        # HR/power ratio stays constant → 0% decoupling
        laps = [
            LapMetrics(i + 1, 600, 250, 140, None)
            for i in range(4)
        ]
        d = hr_power_decoupling(laps)
        assert d == pytest.approx(0.0, abs=0.1)

    def test_hr_rises_power_stable_positive_decoupling(self):
        # First 2 laps: HR=140, power=250. Last 2: HR=154, power=250.
        laps = [
            LapMetrics(1, 600, 250, 140, None),
            LapMetrics(2, 600, 250, 140, None),
            LapMetrics(3, 600, 250, 154, None),
            LapMetrics(4, 600, 250, 154, None),
        ]
        d = hr_power_decoupling(laps)
        assert d is not None
        assert d > 0  # HR:power ratio increased

    def test_high_decoupling_above_5_pct(self):
        laps = [
            LapMetrics(1, 600, 250, 130, None),
            LapMetrics(2, 600, 250, 130, None),
            LapMetrics(3, 600, 250, 142, None),
            LapMetrics(4, 600, 250, 142, None),
        ]
        d = hr_power_decoupling(laps)
        assert d is not None
        assert d > 5.0

    def test_single_lap_returns_none(self):
        laps = [LapMetrics(1, 600, 250, 140, None)]
        assert hr_power_decoupling(laps) is None

    def test_missing_hr_returns_none(self):
        laps = [
            LapMetrics(1, 600, 250, None, None),
            LapMetrics(2, 600, 250, None, None),
        ]
        assert hr_power_decoupling(laps) is None

    def test_missing_power_returns_none(self):
        laps = [
            LapMetrics(1, 600, None, 140, None),
            LapMetrics(2, 600, None, 145, None),
        ]
        assert hr_power_decoupling(laps) is None

    def test_odd_lap_count_splits_correctly(self):
        # 3 laps: mid = 1, first_half = [0], second_half = [1, 2]
        laps = [
            LapMetrics(1, 600, 250, 140, None),
            LapMetrics(2, 600, 250, 140, None),
            LapMetrics(3, 600, 250, 140, None),
        ]
        d = hr_power_decoupling(laps)
        assert d is not None  # shouldn't crash; ratio is stable so ~0

    def test_weighted_by_duration(self):
        # Long first lap at low HR, short second lap at high HR
        # weighted mean should reflect duration
        laps = [
            LapMetrics(1, 1200, 250, 130, None),  # 20 min
            LapMetrics(2, 1200, 250, 130, None),
            LapMetrics(3, 600,  250, 150, None),   # 10 min — high HR
            LapMetrics(4, 600,  250, 150, None),
        ]
        d = hr_power_decoupling(laps)
        assert d is not None
        assert d > 0


# ---------------------------------------------------------------------------
# power_zone_distribution
# ---------------------------------------------------------------------------

class TestPowerZoneDistribution:
    def test_z2_lap(self):
        laps = [LapMetrics(1, 3600, 200, 140, None)]  # 200W at FTP 300 = 0.667 = Z2
        dist = power_zone_distribution(laps, ftp=300)
        assert dist["Z2"] == pytest.approx(3600.0)
        assert dist["Z3"] == 0.0

    def test_z4_lap(self):
        laps = [LapMetrics(1, 600, 280, 155, None)]  # 280/300 = 0.933 = Z4
        dist = power_zone_distribution(laps, ftp=300)
        assert dist["Z4"] == pytest.approx(600.0)

    def test_z5_at_ftp(self):
        laps = [LapMetrics(1, 600, 300, 160, None)]  # exactly FTP = Z5 lower bound
        dist = power_zone_distribution(laps, ftp=300)
        assert dist["Z5"] == pytest.approx(600.0)

    def test_z6_above_ftp(self):
        laps = [LapMetrics(1, 300, 330, 170, None)]  # 110% FTP = Z6
        dist = power_zone_distribution(laps, ftp=300)
        assert dist["Z6"] == pytest.approx(300.0)

    def test_no_power_data_returns_zeros(self):
        laps = [LapMetrics(1, 600, None, 140, None)]
        dist = power_zone_distribution(laps, ftp=300)
        assert all(v == 0.0 for v in dist.values())

    def test_zero_ftp_returns_empty(self):
        laps = [LapMetrics(1, 600, 250, 140, None)]
        assert power_zone_distribution(laps, ftp=0) == {}

    def test_all_zones_present_in_output(self):
        laps = [LapMetrics(1, 600, 250, 140, None)]
        dist = power_zone_distribution(laps, ftp=300)
        assert set(dist.keys()) == set(POWER_ZONES.keys())

    def test_multiple_laps_accumulate(self):
        laps = [
            LapMetrics(1, 1800, 200, 140, None),  # Z2
            LapMetrics(2, 1800, 200, 142, None),  # Z2 again
        ]
        dist = power_zone_distribution(laps, ftp=300)
        assert dist["Z2"] == pytest.approx(3600.0)


# ---------------------------------------------------------------------------
# session_quality_label
# ---------------------------------------------------------------------------

class TestSessionQualityLabel:
    def test_no_planned_tss_adequate(self):
        assert session_quality_label(None, None) == "adequate"

    def test_executed_well(self):
        assert session_quality_label(1.0, 2.0) == "executed_well"

    def test_executed_well_at_boundary(self):
        assert session_quality_label(0.97, 1.0) == "executed_well"

    def test_overdone_with_high_decoupling(self):
        assert session_quality_label(1.05, 9.0) == "overdone"

    def test_executed_well_with_moderate_decoupling(self):
        assert session_quality_label(1.0, 4.0) == "executed_well"

    def test_adequate(self):
        assert session_quality_label(0.92, 3.0) == "adequate"

    def test_adequate_at_boundary(self):
        assert session_quality_label(0.88, None) == "adequate"

    def test_undercooked(self):
        assert session_quality_label(0.75, None) == "undercooked"

    def test_undercooked_at_zero(self):
        assert session_quality_label(0.0, None) == "undercooked"


# ---------------------------------------------------------------------------
# build_debrief — integration
# ---------------------------------------------------------------------------

class TestBuildDebrief:
    def _laps(self):
        return [
            _lap(1800, avg_watts=250, avg_hr=145),
            _lap(1800, avg_watts=248, avg_hr=147),
            _lap(1800, avg_watts=245, avg_hr=150),
            _lap(1800, avg_watts=242, avg_hr=152),
        ]

    def test_basic_bike_debrief(self):
        result = build_debrief(_activity("Ride", tss=95), self._laps(), ftp=316, planned_tss=100)
        assert result.sport == "bike"
        assert result.actual_tss == 95.0
        assert result.planned_tss == 100.0
        assert result.execution_pct == pytest.approx(0.95)

    def test_zone_distribution_populated_for_bike(self):
        result = build_debrief(_activity("Ride", tss=95), self._laps(), ftp=316)
        assert isinstance(result.power_zone_distribution, dict)
        assert sum(result.power_zone_distribution.values()) > 0

    def test_zone_distribution_empty_for_run(self):
        laps = [_lap(600, avg_hr=145, avg_pace=0.28)]
        result = build_debrief(_activity("Run", tss=60), [laps[0]], ftp=316)
        assert result.power_zone_distribution == {}

    def test_quality_label_computed(self):
        result = build_debrief(_activity("Ride", tss=98), self._laps(), ftp=316, planned_tss=100)
        assert result.quality_label in {"executed_well", "adequate", "undercooked", "overdone"}

    def test_no_planned_tss_execution_pct_none(self):
        result = build_debrief(_activity("Ride", tss=95), self._laps(), ftp=316)
        assert result.execution_pct is None
        assert result.quality_label == "adequate"

    def test_flags_populated_on_power_drop(self):
        laps = [
            _lap(1800, avg_watts=280, avg_hr=145),
            _lap(1800, avg_watts=280, avg_hr=145),
            _lap(1800, avg_watts=240, avg_hr=145),
            _lap(1800, avg_watts=240, avg_hr=145),
        ]
        result = build_debrief(_activity("Ride", tss=95), laps, ftp=316)
        assert any("Power fell" in f for f in result.flags)

    def test_flag_for_large_execution_gap(self):
        result = build_debrief(_activity("Ride", tss=60), self._laps(), ftp=316, planned_tss=100)
        assert any("underdelivery" in f for f in result.flags)

    def test_decoupling_flag_on_high_drift(self):
        laps = [
            _lap(1800, avg_watts=250, avg_hr=130),
            _lap(1800, avg_watts=250, avg_hr=130),
            _lap(1800, avg_watts=250, avg_hr=146),
            _lap(1800, avg_watts=250, avg_hr=146),
        ]
        result = build_debrief(_activity("Ride", tss=95), laps, ftp=316)
        assert any("decoupling" in f.lower() for f in result.flags)

    def test_virtual_ride_mapped_to_bike(self):
        result = build_debrief(_activity("VirtualRide", tss=80), self._laps(), ftp=316)
        assert result.sport == "bike"

    def test_empty_laps_no_crash(self):
        result = build_debrief(_activity("Ride", tss=90), [], ftp=316, planned_tss=100)
        assert result.sport == "bike"
        assert sum(result.power_zone_distribution.values()) == 0.0
        assert result.decoupling_pct is None

    def test_session_name_extracted(self):
        result = build_debrief(
            {"type": "Ride", "tss": 90, "name": "4×10 FTP"}, self._laps(), ftp=316
        )
        assert result.session_name == "4×10 FTP"

    def test_workout_name_fallback(self):
        result = build_debrief(
            {"type": "Ride", "tss": 90, "workout_name": "Z2 90min"}, self._laps(), ftp=316
        )
        assert result.session_name == "Z2 90min"
