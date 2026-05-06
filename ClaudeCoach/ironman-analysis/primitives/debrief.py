"""Post-session debrief analytics.

Pure functions that take IcuSync activity and lap data and return
structured metrics. Claude handles the natural-language coaching output;
this module supplies the numbers consistently and testably.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


# Coggan 6-zone power model (fraction of FTP)
POWER_ZONES: dict[str, tuple[float, float]] = {
    "Z1": (0.0,  0.55),
    "Z2": (0.55, 0.75),
    "Z3": (0.75, 0.87),
    "Z4": (0.87, 0.95),
    "Z5": (0.95, 1.05),
    "Z6": (1.05, float("inf")),
}

_DECOUPLING_FLAG_PCT = 5.0    # > 5% HR:power drift = aerobic stress
_HR_DRIFT_FLAG_PCT = 10.0     # > 10% HR rise first→last lap = cardiovascular drift
_POWER_DROP_FLAG_PCT = -10.0  # > 10% power drop first→last lap = pacing issue
_EXECUTION_CONCERN_PCT = 0.80 # < 80% of planned TSS = significant underdelivery


@dataclass
class LapMetrics:
    lap_number: int
    duration_s: float
    avg_watts: Optional[float]
    avg_hr: Optional[float]
    avg_pace_s_per_km: Optional[float]  # converted from s/m


@dataclass
class DebriefResult:
    session_name: str
    sport: str                                    # bike | run | swim
    actual_tss: float
    planned_tss: Optional[float]
    execution_pct: Optional[float]                # actual / planned TSS
    hr_drift_pct: Optional[float]                 # first→last lap HR %
    power_drift_pct: Optional[float]              # first→last lap power %
    pace_drift_pct: Optional[float]               # first→last lap pace % (+ = slower)
    decoupling_pct: Optional[float]               # HR:power ratio drift first→second half %
    power_zone_distribution: dict[str, float]     # zone: seconds
    quality_label: str                            # executed_well | adequate | undercooked | overdone
    flags: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _first_float(d: dict, keys: list[str]) -> Optional[float]:
    for k in keys:
        v = d.get(k)
        if v is not None:
            try:
                f = float(v)
                return f if f > 0 else None
            except (TypeError, ValueError):
                continue
    return None


def _parse_laps(raw_laps: list[dict]) -> list[LapMetrics]:
    """Normalise IcuSync lap objects, handling common field-name variants.

    Pace is stored as seconds/metre in Intervals.icu — converted to s/km.
    Laps with zero duration are dropped.
    """
    laps: list[LapMetrics] = []
    for i, lap in enumerate(raw_laps):
        duration_s = float(
            lap.get("moving_time") or lap.get("elapsed_time") or 0.0
        )
        if duration_s <= 0:
            continue

        avg_watts = _first_float(lap, ["avg_watts", "average_watts", "normalized_power"])
        avg_hr = _first_float(lap, ["avg_hr", "average_hr"])

        pace_s_per_m = _first_float(lap, ["avg_pace", "average_pace"])
        avg_pace_s_per_km = pace_s_per_m * 1000.0 if pace_s_per_m else None

        laps.append(LapMetrics(
            lap_number=i + 1,
            duration_s=duration_s,
            avg_watts=avg_watts,
            avg_hr=avg_hr,
            avg_pace_s_per_km=avg_pace_s_per_km,
        ))

    return laps


def _weighted_mean(laps: list[LapMetrics], attr: str) -> Optional[float]:
    total_dur = sum(l.duration_s for l in laps if getattr(l, attr) is not None)
    if total_dur == 0:
        return None
    total_val = sum(
        (getattr(l, attr) or 0.0) * l.duration_s
        for l in laps
        if getattr(l, attr) is not None
    )
    return total_val / total_dur


# ---------------------------------------------------------------------------
# Public analytics functions
# ---------------------------------------------------------------------------

def lap_drift(laps: list[LapMetrics], attr: str) -> Optional[float]:
    """Percentage change from first to last lap for a given LapMetrics attribute.

    attr: "avg_watts" | "avg_hr" | "avg_pace_s_per_km"
    Returns: positive = increased (e.g. HR rose), negative = decreased (e.g. power fell).
    Returns None if fewer than 2 laps or the attribute is missing on either end.
    """
    if len(laps) < 2:
        return None

    first_val = getattr(laps[0], attr)
    last_val = getattr(laps[-1], attr)

    if first_val is None or last_val is None or first_val == 0:
        return None

    return round((last_val - first_val) / first_val * 100, 2)


def hr_power_decoupling(laps: list[LapMetrics]) -> Optional[float]:
    """Aerobic decoupling: % change in HR:power ratio from first half to second half.

    > 5% suggests aerobic stress — the cardiovascular system worked harder in the
    second half to sustain the same power output. Relevant for Z2 and race-pace work.

    Returns None if fewer than 2 laps or HR/power data are missing.
    """
    if len(laps) < 2:
        return None

    mid = len(laps) // 2
    first_half = laps[:mid]
    second_half = laps[mid:]

    hr1 = _weighted_mean(first_half, "avg_hr")
    hr2 = _weighted_mean(second_half, "avg_hr")
    pwr1 = _weighted_mean(first_half, "avg_watts")
    pwr2 = _weighted_mean(second_half, "avg_watts")

    if any(v is None or v == 0 for v in [hr1, hr2, pwr1, pwr2]):
        return None

    ratio1 = hr1 / pwr1  # type: ignore[operator]
    ratio2 = hr2 / pwr2  # type: ignore[operator]

    return round((ratio2 - ratio1) / ratio1 * 100, 2)


def power_zone_distribution(laps: list[LapMetrics], ftp: float) -> dict[str, float]:
    """Estimate power zone distribution from lap averages × lap duration.

    Returns dict of zone_name → seconds. Laps without power data are excluded.

    Note: this is an approximation using lap-average power. True zone distribution
    requires the per-second power stream from get_extended_metrics.
    """
    if ftp <= 0:
        return {}

    distribution: dict[str, float] = {z: 0.0 for z in POWER_ZONES}

    for lap in laps:
        if lap.avg_watts is None:
            continue
        fraction = lap.avg_watts / ftp
        for zone, (lo, hi) in POWER_ZONES.items():
            if lo <= fraction < hi:
                distribution[zone] += lap.duration_s
                break

    return {k: round(v, 1) for k, v in distribution.items()}


def session_quality_label(
    execution_pct: Optional[float],
    decoupling_pct: Optional[float],
) -> str:
    """Simple quality classification for the session.

    Returns: "executed_well" | "adequate" | "undercooked" | "overdone"
    """
    if execution_pct is None:
        return "adequate"

    if execution_pct >= 0.97:
        if decoupling_pct is not None and decoupling_pct > 8.0:
            return "overdone"
        return "executed_well"

    if execution_pct >= 0.88:
        return "adequate"

    return "undercooked"


def build_debrief(
    activity: dict,
    raw_laps: list[dict],
    ftp: float,
    planned_tss: Optional[float] = None,
) -> DebriefResult:
    """Build a DebriefResult from IcuSync activity and lap data.

    activity: dict from get_activity_detail — needs type, tss, name.
    raw_laps: laps list from the activity (may be empty for swims).
    ftp: current FTP from get_athlete_profile.
    planned_tss: from get_events for today (optional — set when available).
    """
    sport_raw = (activity.get("type") or "").lower()
    if "ride" in sport_raw:
        sport = "bike"
    elif "run" in sport_raw:
        sport = "run"
    else:
        sport = "swim"

    name = activity.get("name") or activity.get("workout_name") or "Session"
    actual_tss = float(activity.get("tss") or 0.0)
    execution_pct = (
        actual_tss / planned_tss if planned_tss and planned_tss > 0 else None
    )

    laps = _parse_laps(raw_laps)

    hr_drift = lap_drift(laps, "avg_hr")
    power_drift = lap_drift(laps, "avg_watts")
    pace_drift = lap_drift(laps, "avg_pace_s_per_km")
    decoupling = hr_power_decoupling(laps)

    zone_dist = power_zone_distribution(laps, ftp) if sport == "bike" else {}

    ql = session_quality_label(execution_pct, decoupling)

    flags: list[str] = []
    if decoupling is not None and decoupling > _DECOUPLING_FLAG_PCT:
        flags.append(
            f"Aerobic decoupling {decoupling:.1f}% (>{_DECOUPLING_FLAG_PCT:.0f}%) "
            "— pacing too hard or aerobic fitness limiting"
        )
    if hr_drift is not None and hr_drift > _HR_DRIFT_FLAG_PCT:
        flags.append(
            f"HR drifted +{hr_drift:.1f}% first→last lap "
            "— cardiovascular drift; check hydration/heat"
        )
    if power_drift is not None and power_drift < _POWER_DROP_FLAG_PCT:
        flags.append(
            f"Power fell {abs(power_drift):.1f}% first→last lap "
            "— went out too hard or fatigued mid-session"
        )
    if execution_pct is not None and execution_pct < _EXECUTION_CONCERN_PCT:
        flags.append(
            f"Executed {execution_pct:.0%} of planned TSS "
            "— significant underdelivery vs target"
        )

    return DebriefResult(
        session_name=name,
        sport=sport,
        actual_tss=actual_tss,
        planned_tss=planned_tss,
        execution_pct=execution_pct,
        hr_drift_pct=hr_drift,
        power_drift_pct=power_drift,
        pace_drift_pct=pace_drift,
        decoupling_pct=decoupling,
        power_zone_distribution=zone_dist,
        quality_label=ql,
        flags=flags,
    )
