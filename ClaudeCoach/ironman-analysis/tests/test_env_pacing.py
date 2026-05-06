"""Tests for primitives/env_pacing.py."""
from __future__ import annotations

import math
import pytest

from primitives.env_pacing import (
    EnvAdjustment,
    heat_correction_fraction,
    humidity_correction_fraction,
    headwind_component,
    wind_time_tax_min,
    adjust_bike_if,
    adjust_run_pace,
    race_day_targets,
    format_pace,
    format_if,
    SEGMENT_HEADING_DEG,
    SEGMENT_DISTANCE_KM,
)


# ---------------------------------------------------------------------------
# heat_correction_fraction
# ---------------------------------------------------------------------------

class TestHeatCorrection:
    def test_below_threshold_returns_zero(self):
        assert heat_correction_fraction(17.9) == 0.0

    def test_at_threshold_returns_zero(self):
        assert heat_correction_fraction(18.0) == 0.0

    def test_5c_above_threshold(self):
        # 23°C → 1 × 0.01 = -0.01
        assert math.isclose(heat_correction_fraction(23.0), -0.01)

    def test_10c_above_threshold(self):
        # 28°C → 2 × 0.01 = -0.02
        assert math.isclose(heat_correction_fraction(28.0), -0.02)

    def test_interpolation(self):
        # 25.5°C → 7.5/5 × 0.01 = -0.015
        assert math.isclose(heat_correction_fraction(25.5), -0.015)

    def test_always_negative_above_threshold(self):
        for temp in [19.0, 24.0, 30.0, 35.0]:
            assert heat_correction_fraction(temp) < 0.0


# ---------------------------------------------------------------------------
# humidity_correction_fraction
# ---------------------------------------------------------------------------

class TestHumidityCorrection:
    def test_below_threshold_returns_zero(self):
        assert humidity_correction_fraction(14.9) == 0.0

    def test_at_threshold_returns_zero(self):
        assert humidity_correction_fraction(15.0) == 0.0

    def test_5c_above_threshold(self):
        # 20°C DP → 1 × 0.01 = -0.01
        assert math.isclose(humidity_correction_fraction(20.0), -0.01)

    def test_typical_cervia_dp(self):
        # Dew point 18°C (typical Cervia Sept) → 3/5 × 0.01 = -0.006
        assert math.isclose(humidity_correction_fraction(18.0), -0.006)

    def test_always_negative_above_threshold(self):
        for dp in [16.0, 18.0, 20.0, 22.0]:
            assert humidity_correction_fraction(dp) < 0.0


# ---------------------------------------------------------------------------
# headwind_component
# ---------------------------------------------------------------------------

class TestHeadwindComponent:
    def test_pure_headwind(self):
        # Wind FROM east (90°), athlete heading east (90°) → pure headwind
        hw = headwind_component(5.0, 90.0, 90.0)
        assert math.isclose(hw, 5.0, abs_tol=0.01)

    def test_pure_tailwind(self):
        # Wind FROM east (90°), athlete heading west (270°) → pure tailwind
        hw = headwind_component(5.0, 90.0, 270.0)
        assert math.isclose(hw, -5.0, abs_tol=0.01)

    def test_pure_crosswind(self):
        # Wind FROM north (0°), athlete heading east (90°) → cos(90°) = 0
        hw = headwind_component(5.0, 0.0, 90.0)
        assert math.isclose(hw, 0.0, abs_tol=0.01)

    def test_zero_wind_returns_zero(self):
        assert headwind_component(0.0, 180.0, 270.0) == 0.0

    def test_cervia_return_leg_with_sea_breeze(self):
        # Return leg heads ESE (100°). Adriatic sea breeze FROM ENE (~70°).
        # Angle = 100 - 70 = 30°, cos(30°) = 0.866 → mostly headwind.
        hw = headwind_component(4.0, 70.0, 100.0)
        assert hw > 0.0
        assert math.isclose(hw, 4.0 * math.cos(math.radians(30.0)), abs_tol=0.01)


# ---------------------------------------------------------------------------
# wind_time_tax_min
# ---------------------------------------------------------------------------

class TestWindTimeTax:
    def test_tailwind_returns_zero(self):
        assert wind_time_tax_min(-3.0, "bike_flat_return") == 0.0

    def test_zero_wind_returns_zero(self):
        assert wind_time_tax_min(0.0, "bike_flat_out") == 0.0

    def test_headwind_positive(self):
        tax = wind_time_tax_min(5.0, "bike_flat_out")
        assert tax > 0.0

    def test_flat_out_5ms_headwind(self):
        # bike_flat_out: 35 km at 9.7 m/s
        # segment_time_hr = 35000 / 9.7 / 3600 ≈ 1.002 hr
        # tax = 2.0 × 5.0 × 1.002 ≈ 10.0 min
        tax = wind_time_tax_min(5.0, "bike_flat_out")
        assert math.isclose(tax, 10.0, abs_tol=0.5)

    def test_climb_segment_explicit(self):
        # bike_bertinoro_climb: 20 km at 5.6 m/s
        # segment_time_hr = 20000 / 5.6 / 3600 = 0.992 hr
        # tax = 2.0 × 5.0 × 0.992 ≈ 9.9 min
        tax = wind_time_tax_min(5.0, "bike_bertinoro_climb")
        assert math.isclose(tax, 9.9, abs_tol=0.3)


# ---------------------------------------------------------------------------
# adjust_bike_if
# ---------------------------------------------------------------------------

class TestAdjustBikeIf:
    def test_cold_dry_no_correction(self):
        result = adjust_bike_if(0.71, 15.0, 10.0)
        assert result.adjusted_bike_if == 0.71
        assert result.heat_fraction == 0.0
        assert result.humidity_fraction == 0.0
        assert result.total_physio_fraction == 0.0
        assert result.adjusted_run_pace_sec_per_km is None

    def test_cervia_typical_conditions(self):
        # 28°C, dew point 18°C: heat -2%, humidity -0.6% → total -2.6%
        result = adjust_bike_if(0.71, 28.0, 18.0)
        assert result.heat_fraction == pytest.approx(-0.02, abs=1e-5)
        assert result.humidity_fraction == pytest.approx(-0.006, abs=1e-5)
        assert result.total_physio_fraction == pytest.approx(-0.026, abs=1e-5)
        expected_if = round(0.71 * (1 - 0.026), 4)
        assert result.adjusted_bike_if == expected_if

    def test_adjusted_if_lower_than_target_in_heat(self):
        result = adjust_bike_if(0.71, 30.0, 20.0)
        assert result.adjusted_bike_if < 0.71

    def test_wind_does_not_change_if(self):
        # Wind is informational only — IF unchanged regardless of wind
        no_wind = adjust_bike_if(0.71, 28.0, 18.0)
        with_headwind = adjust_bike_if(0.71, 28.0, 18.0, wind_speed_ms=5.0, wind_from_deg=100.0)
        assert no_wind.adjusted_bike_if == with_headwind.adjusted_bike_if

    def test_headwind_adds_time_tax(self):
        result = adjust_bike_if(
            0.71, 28.0, 18.0,
            segment="bike_flat_return",
            wind_speed_ms=5.0,
            wind_from_deg=70.0,  # sea breeze from ENE → headwind on return leg
        )
        assert result.headwind_ms > 0.0
        assert result.wind_time_tax_min > 0.0

    def test_tailwind_zero_tax(self):
        result = adjust_bike_if(
            0.71, 28.0, 18.0,
            segment="bike_flat_out",  # heading WNW (280°)
            wind_speed_ms=5.0,
            wind_from_deg=70.0,  # ENE wind → tailwind on outbound
        )
        assert result.wind_time_tax_min == 0.0

    def test_reasoning_trail_contains_key_values(self):
        result = adjust_bike_if(0.71, 28.0, 18.0)
        assert "28" in result.reasoning_trail  # temp cited
        assert "18" in result.reasoning_trail  # dew point cited
        assert "0.71" in result.reasoning_trail
        assert result.reasoning_trail.count("→") >= 1

    def test_no_wind_from_deg_skips_wind(self):
        result = adjust_bike_if(0.71, 28.0, 18.0, wind_speed_ms=10.0, wind_from_deg=None)
        assert result.headwind_ms == 0.0
        assert result.wind_time_tax_min == 0.0


# ---------------------------------------------------------------------------
# adjust_run_pace
# ---------------------------------------------------------------------------

class TestAdjustRunPace:
    # 4:58/km = 298 sec/km
    TARGET_PACE = 298.0

    def test_cold_dry_no_correction(self):
        result = adjust_run_pace(self.TARGET_PACE, 15.0, 10.0)
        assert result.adjusted_run_pace_sec_per_km == self.TARGET_PACE
        assert result.adjusted_bike_if is None

    def test_heat_slows_pace(self):
        result = adjust_run_pace(self.TARGET_PACE, 28.0, 18.0)
        # Heat -2%, humidity -0.6% → total -2.6% speed → pace increases
        assert result.adjusted_run_pace_sec_per_km > self.TARGET_PACE

    def test_cervia_conditions_quantified(self):
        # 28°C, dew point 18°C
        result = adjust_run_pace(self.TARGET_PACE, 28.0, 18.0)
        # Expected: 298 / (1 - 0.026) = 298 / 0.974 ≈ 306.0
        assert math.isclose(result.adjusted_run_pace_sec_per_km, 298.0 / 0.974, abs_tol=0.5)

    def test_reasoning_trail_shows_pace(self):
        result = adjust_run_pace(self.TARGET_PACE, 28.0, 18.0)
        assert "4:58" in result.reasoning_trail  # original pace
        assert "5:" in result.reasoning_trail    # adjusted pace starts with 5:


# ---------------------------------------------------------------------------
# race_day_targets
# ---------------------------------------------------------------------------

class TestRaceDayTargets:
    def test_returns_all_bike_segments_and_run(self):
        out = race_day_targets(0.71, 298.0, 28.0, 18.0)
        assert "bike_flat_out" in out["segments"]
        assert "bike_flat_return" in out["segments"]
        assert "bike_bertinoro_climb" in out["segments"]
        assert "run_loop" in out["segments"]

    def test_conservative_if_is_lowest(self):
        out = race_day_targets(0.71, 298.0, 28.0, 18.0)
        all_ifs = [
            v.adjusted_bike_if
            for v in out["segments"].values()
            if v.adjusted_bike_if is not None
        ]
        assert out["summary"]["conservative_bike_if"] == min(all_ifs)

    def test_cold_dry_no_correction_in_summary(self):
        out = race_day_targets(0.71, 298.0, 15.0, 10.0)
        assert out["summary"]["conservative_bike_if"] == 0.71
        assert out["summary"]["adjusted_run_pace_sec_per_km"] == 298.0
        assert out["summary"]["total_physio_correction_pct"] == 0.0

    def test_summary_pace_formatted(self):
        out = race_day_targets(0.71, 298.0, 28.0, 18.0)
        fmt = out["summary"]["adjusted_run_pace_formatted"]
        assert "/" in fmt  # "5:06/km" format
        assert "km" in fmt


# ---------------------------------------------------------------------------
# format_pace / format_if
# ---------------------------------------------------------------------------

class TestFormatters:
    def test_format_pace_298(self):
        assert format_pace(298.0) == "4:58/km"

    def test_format_pace_300(self):
        assert format_pace(300.0) == "5:00/km"

    def test_format_pace_306(self):
        assert format_pace(306.0) == "5:06/km"

    def test_format_pace_rounds_seconds(self):
        # 300.6 → 0.6 sec remainder → rounds to 1 → "5:01/km"
        assert format_pace(300.6) == "5:01/km"

    def test_format_if_three_decimals(self):
        assert format_if(0.71) == "0.710"
        assert format_if(0.6923) == "0.692"
