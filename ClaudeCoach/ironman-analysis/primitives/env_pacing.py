"""env_pacing.py — environmental pacing adjustments for IM Cervia 2026.

Pure functions. No IO, no MCP coupling. Takes forecast conditions and
returns adjusted IF/pace targets with L2 reasoning trails.

Methodology — calibration status:
    * Heat: ~1% per 5°C above 18°C ambient (published default — Moran et al.).
    * Humidity: ~1% per 5°C dew-point above 15°C (published default).
    * Wind (bike): headwind component computed from course geometry; time tax
      estimated at 2 min/hr per 1 m/s net headwind. Power target held — accept
      slower speed rather than burning matches.
    * Run: same heat + humidity fractions; wind negligible at IM pace.

Re-calibration target: after each hot training ride in July 2026, fit
personal correction factors against observed pace/power vs planned. The
2025 Bertinoro Lap 2 data (i98218040) should be the primary reference once
re-analysed with concurrent weather data.
"""

from __future__ import annotations

import math
from dataclasses import dataclass


# ---------------------------------------------------------------------------
# Course geometry — Cervia 2026
# ---------------------------------------------------------------------------

# Approximate course headings in degrees (meteorological, clockwise from N).
# Outbound = toward Bertinoro (inland, broadly WNW from Adriatic coast).
# Return = toward Cervia (broadly ESE, toward sea).
SEGMENT_HEADING_DEG: dict[str, float] = {
    "bike_flat_out": 280,           # Cervia → Forlimpopoli
    "bike_flat_return": 100,        # Forlimpopoli → Cervia
    "bike_bertinoro_climb": 280,    # Forlimpopoli → Bertinoro (uphill)
    "bike_bertinoro_descent": 100,  # Bertinoro → Forlimpopoli (downhill)
    "run_loop": 0,                  # alternating N-S legs; treated as wind-neutral
}

# One-way distances per segment in km.
SEGMENT_DISTANCE_KM: dict[str, float] = {
    "bike_flat_out": 35.0,
    "bike_flat_return": 35.0,
    "bike_bertinoro_climb": 20.0,
    "bike_bertinoro_descent": 20.0,
    "run_loop": 10.55,
}

# Expected athlete speed at target intensity (m/s). Used only for wind time tax.
SEGMENT_SPEED_MS: dict[str, float] = {
    "bike_flat_out": 9.7,           # ~35 km/h on flat at IM pace
    "bike_flat_return": 10.0,       # slightly faster on return road surface
    "bike_bertinoro_climb": 5.6,    # ~20 km/h climbing
    "bike_bertinoro_descent": 13.9, # ~50 km/h descending
    "run_loop": 3.3,                # ~12 km/h = 5:00/km off-bike pace
}

# ---------------------------------------------------------------------------
# Correction constants (population defaults — see re-calibration note above)
# ---------------------------------------------------------------------------

_HEAT_THRESHOLD_C: float = 18.0
_HEAT_FRACTION_PER_5C: float = 0.01    # 1% reduction per 5°C above threshold

_DP_THRESHOLD_C: float = 15.0
_DP_FRACTION_PER_5C: float = 0.01      # 1% reduction per 5°C above threshold

# Wind time tax on bike: ~2 min extra per hour per 1 m/s of net headwind.
# Conservative default; update from personal data after July hot rides.
_WIND_MIN_PER_HR_PER_MS: float = 2.0


# ---------------------------------------------------------------------------
# Data class
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class EnvAdjustment:
    """Environmental adjustment for a single course segment."""
    segment: str
    heat_fraction: float            # negative = reduction; e.g. -0.02 at 28°C
    humidity_fraction: float        # negative = reduction; e.g. -0.006 at 18°C DP
    total_physio_fraction: float    # heat + humidity combined
    headwind_ms: float              # m/s; positive = into athlete's face
    wind_time_tax_min: float        # estimated extra minutes on segment from headwind
    adjusted_bike_if: float | None
    adjusted_run_pace_sec_per_km: float | None
    reasoning_trail: str            # L2 format: signal → rule → adjustment → effect


# ---------------------------------------------------------------------------
# Component functions
# ---------------------------------------------------------------------------

def heat_correction_fraction(temp_c: float) -> float:
    """Fractional power/pace reduction due to ambient heat.

    Returns 0 at or below 18°C. Returns a negative fraction above that:
    -0.01 at 23°C, -0.02 at 28°C, -0.03 at 33°C.
    """
    if temp_c <= _HEAT_THRESHOLD_C:
        return 0.0
    return -round(((temp_c - _HEAT_THRESHOLD_C) / 5.0) * _HEAT_FRACTION_PER_5C, 6)


def humidity_correction_fraction(dew_point_c: float) -> float:
    """Fractional power/pace reduction due to humidity (dew-point proxy).

    Returns 0 at or below 15°C dew-point. Returns a negative fraction above:
    -0.002 at 16°C, -0.006 at 18°C, -0.01 at 20°C.
    """
    if dew_point_c <= _DP_THRESHOLD_C:
        return 0.0
    return -round(((dew_point_c - _DP_THRESHOLD_C) / 5.0) * _DP_FRACTION_PER_5C, 6)


def headwind_component(
    wind_speed_ms: float,
    wind_from_deg: float,
    course_heading_deg: float,
) -> float:
    """Net headwind component in m/s for a course heading vs wind direction.

    Args:
        wind_speed_ms: Total wind speed (m/s).
        wind_from_deg: Direction the wind is blowing FROM (meteorological, 0=N).
        course_heading_deg: Direction the athlete is travelling (0=N).

    Returns:
        Signed m/s: positive = headwind, negative = tailwind, 0 = crosswind.
    """
    angle_rad = math.radians(course_heading_deg - wind_from_deg)
    return round(wind_speed_ms * math.cos(angle_rad), 2)


def wind_time_tax_min(
    headwind_ms: float,
    segment: str,
) -> float:
    """Estimated extra minutes on a segment due to headwind at constant power.

    Tailwinds are not credited (conservative; athlete holds IF, takes the gift).
    Returns 0 if headwind <= 0.
    """
    if headwind_ms <= 0.0:
        return 0.0
    speed_ms = SEGMENT_SPEED_MS.get(segment, 9.0)
    dist_km = SEGMENT_DISTANCE_KM.get(segment, 0.0)
    segment_time_hr = (dist_km * 1000.0 / speed_ms) / 3600.0
    return round(_WIND_MIN_PER_HR_PER_MS * headwind_ms * segment_time_hr, 1)


# ---------------------------------------------------------------------------
# Main adjustment functions
# ---------------------------------------------------------------------------

def adjust_bike_if(
    target_if: float,
    temp_c: float,
    dew_point_c: float,
    segment: str = "bike_flat_out",
    wind_speed_ms: float = 0.0,
    wind_from_deg: float | None = None,
) -> EnvAdjustment:
    """Adjusted IF target for a bike segment given forecast conditions.

    Physiological corrections (heat + humidity) reduce the IF target.
    Wind is informational — athlete holds IF and accepts slower speed.

    Args:
        target_if: Planned intensity factor (e.g. 0.71 for IM race pace).
        temp_c: Ambient temperature (°C).
        dew_point_c: Dew-point temperature (°C, humidity proxy).
        segment: Course segment key from SEGMENT_HEADING_DEG.
        wind_speed_ms: Wind speed (m/s). 0 if unknown.
        wind_from_deg: Meteorological wind direction (FROM, degrees). None to skip wind.

    Returns:
        EnvAdjustment with adjusted_bike_if and L2 reasoning_trail.
    """
    heat_f = heat_correction_fraction(temp_c)
    humidity_f = humidity_correction_fraction(dew_point_c)
    total_f = round(heat_f + humidity_f, 6)

    hw = 0.0
    if wind_from_deg is not None and wind_speed_ms > 0.0:
        hw = headwind_component(
            wind_speed_ms, wind_from_deg, SEGMENT_HEADING_DEG.get(segment, 0.0)
        )
    tax = wind_time_tax_min(hw, segment)

    adjusted_if = round(target_if * (1.0 + total_f), 4)
    trail = _build_bike_trail(
        segment, temp_c, dew_point_c, heat_f, humidity_f, total_f,
        hw, tax, target_if, adjusted_if,
    )

    return EnvAdjustment(
        segment=segment,
        heat_fraction=heat_f,
        humidity_fraction=humidity_f,
        total_physio_fraction=total_f,
        headwind_ms=hw,
        wind_time_tax_min=tax,
        adjusted_bike_if=adjusted_if,
        adjusted_run_pace_sec_per_km=None,
        reasoning_trail=trail,
    )


def adjust_run_pace(
    target_pace_sec_per_km: float,
    temp_c: float,
    dew_point_c: float,
    segment: str = "run_loop",
    wind_speed_ms: float = 0.0,
    wind_from_deg: float | None = None,
) -> EnvAdjustment:
    """Adjusted run pace target given forecast conditions.

    Heat + humidity slow the athlete; same fractional corrections as bike.
    Pace in sec/km increases (slower) by 1 / (1 + total_f).

    Args:
        target_pace_sec_per_km: Planned pace (seconds per km, e.g. 298 = 4:58/km).
        temp_c: Ambient temperature (°C). At IM run start time (~14:00) expect
                +2–3°C above morning forecast.
        dew_point_c: Dew-point temperature (°C).
        segment: Course segment key (default: run_loop).
        wind_speed_ms: Wind speed (m/s).
        wind_from_deg: Meteorological wind direction (FROM, degrees).

    Returns:
        EnvAdjustment with adjusted_run_pace_sec_per_km and L2 reasoning_trail.
    """
    heat_f = heat_correction_fraction(temp_c)
    humidity_f = humidity_correction_fraction(dew_point_c)
    total_f = round(heat_f + humidity_f, 6)

    hw = 0.0
    if wind_from_deg is not None and wind_speed_ms > 0.0:
        hw = headwind_component(
            wind_speed_ms, wind_from_deg, SEGMENT_HEADING_DEG.get(segment, 0.0)
        )
    tax = wind_time_tax_min(hw, segment)

    # total_f is negative → speed decreases → pace (sec/km) increases
    adjusted_pace = round(target_pace_sec_per_km / (1.0 + total_f), 1)
    trail = _build_run_trail(
        segment, temp_c, dew_point_c, heat_f, humidity_f, total_f,
        hw, tax, target_pace_sec_per_km, adjusted_pace,
    )

    return EnvAdjustment(
        segment=segment,
        heat_fraction=heat_f,
        humidity_fraction=humidity_f,
        total_physio_fraction=total_f,
        headwind_ms=hw,
        wind_time_tax_min=tax,
        adjusted_bike_if=None,
        adjusted_run_pace_sec_per_km=adjusted_pace,
        reasoning_trail=trail,
    )


def race_day_targets(
    bike_target_if: float,
    run_target_pace_sec_per_km: float,
    temp_c: float,
    dew_point_c: float,
    wind_speed_ms: float = 0.0,
    wind_from_deg: float | None = None,
) -> dict:
    """Adjusted targets across all race segments for a single forecast snapshot.

    Typical usage: call at T-3 with the actual race-day forecast.

    Returns dict with:
        "segments": {segment_name: EnvAdjustment}
        "summary": {
            "conservative_bike_if": float  — lowest adjusted IF across bike segments
            "adjusted_run_pace_sec_per_km": float
            "adjusted_run_pace_formatted": str  — e.g. "5:06/km"
            "total_physio_correction_pct": float  — e.g. -2.6
        }
    """
    bike_segs = ["bike_flat_out", "bike_flat_return", "bike_bertinoro_climb"]
    segments: dict[str, EnvAdjustment] = {}

    for seg in bike_segs:
        segments[seg] = adjust_bike_if(
            bike_target_if, temp_c, dew_point_c, seg, wind_speed_ms, wind_from_deg
        )

    segments["run_loop"] = adjust_run_pace(
        run_target_pace_sec_per_km, temp_c, dew_point_c,
        "run_loop", wind_speed_ms, wind_from_deg,
    )

    bike_ifs = [v.adjusted_bike_if for v in segments.values() if v.adjusted_bike_if is not None]
    run_paces = [v.adjusted_run_pace_sec_per_km for v in segments.values()
                 if v.adjusted_run_pace_sec_per_km is not None]

    run_adj = max(run_paces) if run_paces else run_target_pace_sec_per_km
    total_pct = round(segments["bike_flat_out"].total_physio_fraction * 100, 2)

    return {
        "segments": segments,
        "summary": {
            "conservative_bike_if": min(bike_ifs) if bike_ifs else bike_target_if,
            "adjusted_run_pace_sec_per_km": run_adj,
            "adjusted_run_pace_formatted": format_pace(run_adj),
            "total_physio_correction_pct": total_pct,
        },
    }


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def format_pace(sec_per_km: float) -> str:
    """Format seconds-per-km as M:SS/km string. e.g. 306.0 → '5:06/km'."""
    minutes = int(sec_per_km // 60)
    seconds = int(round(sec_per_km % 60))
    return f"{minutes}:{seconds:02d}/km"


def format_if(intensity_factor: float) -> str:
    """Format IF to 3 decimal places. e.g. 0.6923 → '0.692'."""
    return f"{intensity_factor:.3f}"


# ---------------------------------------------------------------------------
# L2 reasoning trail builders (internal)
# ---------------------------------------------------------------------------

def _build_bike_trail(
    segment: str,
    temp_c: float,
    dew_point_c: float,
    heat_f: float,
    humidity_f: float,
    total_f: float,
    headwind_ms: float,
    wind_tax: float,
    original_if: float,
    adjusted_if: float,
) -> str:
    signals = []
    if heat_f != 0.0:
        signals.append(
            f"Temp {temp_c}°C (>{_HEAT_THRESHOLD_C}°C) "
            f"→ heat rule ({abs(heat_f) * 100:.2g}% per 5°C above {_HEAT_THRESHOLD_C}°C)"
        )
    if humidity_f != 0.0:
        signals.append(
            f"Dew point {dew_point_c}°C (>{_DP_THRESHOLD_C}°C) "
            f"→ humidity rule ({abs(humidity_f) * 100:.2g}% per 5°C above {_DP_THRESHOLD_C}°C)"
        )
    if headwind_ms > 0.0:
        signals.append(
            f"Headwind {headwind_ms:.1f} m/s on {segment} "
            f"→ hold IF, accept slower speed (informational)"
        )
    elif headwind_ms < 0.0:
        signals.append(
            f"Tailwind {abs(headwind_ms):.1f} m/s on {segment} → no IF adjustment (take the gift)"
        )

    signal_str = " | ".join(signals) if signals else "No corrections (conditions within thresholds)"
    effect = (
        f"→ IF {format_if(original_if)} → {format_if(adjusted_if)} "
        f"({total_f * 100:+.2g}% physiological)"
    )
    if wind_tax > 0.0:
        effect += f"; expect +{wind_tax:.0f} min on {segment} from headwind (hold power, accept time)"
    return f"{signal_str} {effect}"


def _build_run_trail(
    segment: str,
    temp_c: float,
    dew_point_c: float,
    heat_f: float,
    humidity_f: float,
    total_f: float,
    headwind_ms: float,
    wind_tax: float,
    original_pace: float,
    adjusted_pace: float,
) -> str:
    signals = []
    if heat_f != 0.0:
        signals.append(
            f"Temp {temp_c}°C (>{_HEAT_THRESHOLD_C}°C) "
            f"→ heat rule ({abs(heat_f) * 100:.2g}% per 5°C above {_HEAT_THRESHOLD_C}°C)"
        )
    if humidity_f != 0.0:
        signals.append(
            f"Dew point {dew_point_c}°C (>{_DP_THRESHOLD_C}°C) "
            f"→ humidity rule ({abs(humidity_f) * 100:.2g}% per 5°C above {_DP_THRESHOLD_C}°C)"
        )
    if headwind_ms > 0.0:
        signals.append(
            f"Headwind {headwind_ms:.1f} m/s → negligible at run pace (noted)"
        )

    signal_str = " | ".join(signals) if signals else "No corrections (conditions within thresholds)"
    effect = (
        f"→ pace {format_pace(original_pace)} → {format_pace(adjusted_pace)} "
        f"({total_f * 100:+.2g}% physiological)"
    )
    return f"{signal_str} {effect}"
