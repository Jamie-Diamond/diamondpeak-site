"""modulation.py — soft session modulation engine (W2).

Pure functions. Takes a planned session and current readiness signals,
applies modulation rules in priority order, returns an adjusted prescription
with L2 reasoning trails.

Rule inventory (applied in order; hard rules fire first):
    R1  Ankle hard stop       — run quality blocked if pain >2/10 or not cleared
    R2  ATL swap to Z2        — (ATL − CTL) > 25 → quality → Z2
    R3  HRV intensity drop    — 7d HRV trend < −7% → −5% intensity, −1 interval
    R4  ATL moderate cap      — 15 < (ATL − CTL) ≤ 25 → cap 95% FTP, −1 interval
    R5  Prior RPE reduction   — last session RPE ≥ 8 → −5% intensity
    R6  Sleep swap (two-sig)  — sleep < 6h AND HRV < −5% → swap quality → Z2
    R7  Heat adjustment       — temp > 18°C → apply L1 env_pacing correction

Design rule: start with 7; add only when an observed failure isn't caught.
Sources: reference/rules.md, upgrade plan W2 spec, multi-signal corroboration rule.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from primitives.env_pacing import adjust_bike_if, adjust_run_pace, format_pace, format_if


# ---------------------------------------------------------------------------
# Constants — thresholds match reference/rules.md and watchdog W4 values
# ---------------------------------------------------------------------------

_ATL_SWAP_GAP: float = 25.0       # ATL − CTL > this → swap quality to Z2
_ATL_MODERATE_GAP: float = 15.0   # ATL − CTL > this → cap + reduce
_HRV_TREND_HARD: float = -7.0     # % (7d) below this → R3 fires
_HRV_TREND_SOFT: float = -5.0     # % (7d) below this → contributes to two-signal (R6)
_RPE_REDUCTION_THRESHOLD: int = 8  # prior RPE ≥ this → R5 fires
_SLEEP_SWAP_H: float = 6.0        # hours below this → two-signal swap candidate (R6)
_SLEEP_REDUCE_H: float = 7.0      # hours below this → single-signal reduce (checked in R6)
_ANKLE_PAIN_RUN_CAP: int = 2       # pain > this → no run quality
_INTENSITY_STEP: float = 0.05      # standard reduction increment


# Session types with run load — ankle rule applies
_RUN_LOAD_TYPES = {"run_quality", "run_long", "run_easy", "brick"}
# Quality session types that can be modulated
_QUALITY_TYPES = {"bike_threshold", "bike_vo2", "bike_race_pace", "run_quality", "brick"}
_Z2_TYPES = {"bike_z2", "run_easy"}


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class RuleResult:
    """One applied modulation rule."""
    code: str                   # e.g. "R3"
    fired: bool
    reasoning_trail: str        # L2 format


@dataclass
class SessionPrescription:
    """Modulated session output."""
    session_type: str
    go: bool                            # False = do not execute as quality
    swapped_to_z2: bool                 # quality → Z2 swap applied
    modified: bool                      # any rule changed the prescription
    target_intensity: float             # adjusted (0–1.5, relative to FTP/threshold)
    interval_count: int | None
    interval_duration_min: float | None
    recovery_min: float | None
    total_duration_min: int
    applied_rules: list[str]            # codes of rules that fired
    reasoning_trails: list[str]         # L2 trails for each fired rule
    summary: str                        # one-line human summary


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _is_quality(session_type: str) -> bool:
    return session_type in _QUALITY_TYPES


def _is_run(session_type: str) -> bool:
    return session_type in _RUN_LOAD_TYPES


def _fmt_gap(atl: float, ctl: float) -> str:
    return f"ATL {atl:.0f} vs CTL {ctl:.0f} (gap +{atl - ctl:.1f})"


# ---------------------------------------------------------------------------
# Rule functions — each returns a RuleResult
# ---------------------------------------------------------------------------

def _r1_ankle_hard_stop(
    planned: dict, readiness: dict
) -> RuleResult:
    """R1: Ankle pain >2/10 OR not cleared → no run quality."""
    if not _is_run(planned["session_type"]):
        return RuleResult("R1", False, "")

    pain = readiness.get("ankle_pain_score", 0)
    cleared = readiness.get("ankle_quality_cleared", False)

    if pain > _ANKLE_PAIN_RUN_CAP:
        trail = (
            f"Ankle pain {pain}/10 (>{_ANKLE_PAIN_RUN_CAP}/10 hard cap, rules.md) "
            f"→ R1 hard stop → no run quality today "
            f"→ swap to easy or rest; re-assess tomorrow"
        )
        return RuleResult("R1", True, trail)

    if not cleared and _is_quality(planned["session_type"]):
        trail = (
            f"Ankle quality not yet cleared (4 consecutive pain-free weeks not reached, rules.md) "
            f"→ R1 hard stop → run quality blocked "
            f"→ continue return-to-run protocol; no intervals until cleared"
        )
        return RuleResult("R1", True, trail)

    return RuleResult("R1", False, "")


def _r2_atl_swap(
    planned: dict, readiness: dict
) -> RuleResult:
    """R2: ATL − CTL > 25 → swap quality session to Z2."""
    if not _is_quality(planned["session_type"]):
        return RuleResult("R2", False, "")

    atl = readiness.get("atl", 0.0)
    ctl = readiness.get("ctl", 0.0)
    gap = atl - ctl

    if gap > _ATL_SWAP_GAP:
        trail = (
            f"{_fmt_gap(atl, ctl)} (>{_ATL_SWAP_GAP:.0f} swap threshold) "
            f"→ R2 ATL-swap rule → replace quality with Z2, duration unchanged "
            f"→ arrest fatigue accumulation; quality in 24–48h if gap < {_ATL_SWAP_GAP:.0f}"
        )
        return RuleResult("R2", True, trail)

    return RuleResult("R2", False, "")


def _r3_hrv_intensity_drop(
    planned: dict, readiness: dict
) -> RuleResult:
    """R3: 7d HRV trend < −7% → −5% intensity, −1 interval."""
    if not _is_quality(planned["session_type"]):
        return RuleResult("R3", False, "")

    hrv = readiness.get("hrv_trend_pct", 0.0)
    if hrv < _HRV_TREND_HARD:
        trail = (
            f"HRV {hrv:+.1f}% over 7d (<{_HRV_TREND_HARD}% threshold) "
            f"→ R3 HRV-reduction rule → intensity −{_INTENSITY_STEP * 100:.0f}%, intervals −1 "
            f"→ maintain stimulus, reduce autonomic strain ~15%"
        )
        return RuleResult("R3", True, trail)

    return RuleResult("R3", False, "")


def _r4_atl_moderate_cap(
    planned: dict, readiness: dict
) -> RuleResult:
    """R4: 15 < ATL − CTL ≤ 25 → cap 95% FTP, −1 interval."""
    if not _is_quality(planned["session_type"]):
        return RuleResult("R4", False, "")

    atl = readiness.get("atl", 0.0)
    ctl = readiness.get("ctl", 0.0)
    gap = atl - ctl

    if _ATL_MODERATE_GAP < gap <= _ATL_SWAP_GAP:
        trail = (
            f"{_fmt_gap(atl, ctl)} ({_ATL_MODERATE_GAP:.0f}–{_ATL_SWAP_GAP:.0f} moderate zone) "
            f"→ R4 load-management cap → cap intensity 95%, intervals −1 "
            f"→ aerobic stimulus preserved, acute load ceiling enforced"
        )
        return RuleResult("R4", True, trail)

    return RuleResult("R4", False, "")


def _r5_prior_rpe(
    planned: dict, readiness: dict
) -> RuleResult:
    """R5: Last session RPE ≥ 8 → −5% intensity."""
    if not _is_quality(planned["session_type"]):
        return RuleResult("R5", False, "")

    rpe = readiness.get("last_session_rpe")
    if rpe is not None and rpe >= _RPE_REDUCTION_THRESHOLD:
        trail = (
            f"Yesterday's RPE {rpe}/10 (≥{_RPE_REDUCTION_THRESHOLD}) "
            f"→ R5 prior-RPE rule → intensity −{_INTENSITY_STEP * 100:.0f}% "
            f"→ allow partial glycogen and CNS recovery before today's stimulus"
        )
        return RuleResult("R5", True, trail)

    return RuleResult("R5", False, "")


def _r6_sleep_two_signal(
    planned: dict, readiness: dict
) -> RuleResult:
    """R6: Sleep < 6h AND HRV < −5% → swap quality to Z2.

    Multi-signal corroboration required (rules.md). Single bad sleep alone
    does NOT trigger a swap — reduces interval count by 1 instead (handled
    in prescription assembly below).
    """
    if not _is_quality(planned["session_type"]):
        return RuleResult("R6", False, "")

    sleep = readiness.get("sleep_h_last_night")
    hrv = readiness.get("hrv_trend_pct", 0.0)

    if sleep is not None and sleep < _SLEEP_SWAP_H and hrv < _HRV_TREND_SOFT:
        trail = (
            f"Sleep {sleep:.1f}h (<{_SLEEP_SWAP_H}h) AND HRV {hrv:+.1f}% (<{_HRV_TREND_SOFT}%) "
            f"→ R6 two-signal swap (multi-signal corroboration rule, rules.md) "
            f"→ replace quality with Z2, duration unchanged "
            f"→ single compromised-recovery signal insufficient; both signals together = swap"
        )
        return RuleResult("R6", True, trail)

    return RuleResult("R6", False, "")


def _r7_heat(
    planned: dict, readiness: dict
) -> tuple[RuleResult, float]:
    """R7: Temp > 18°C → apply L1 env_pacing correction.

    Returns (RuleResult, adjusted_intensity_fraction).
    adjusted_intensity_fraction is 1.0 if rule did not fire.
    """
    temp = readiness.get("temp_c", 15.0)
    dp = readiness.get("dew_point_c", 10.0)
    stype = planned["session_type"]
    base = planned.get("target_intensity", 1.0)

    if temp <= 18.0:
        return RuleResult("R7", False, ""), 1.0

    if "bike" in stype or stype == "brick":
        adj = adjust_bike_if(base, temp, dp)
        factor = (1.0 + adj.total_physio_fraction)
        trail = f"L1 env pacing (bike): {adj.reasoning_trail}"
    else:
        # For run: convert intensity fraction to sec/km, adjust, convert back.
        # Approximate: target_intensity maps to a pace. Use 1.0 = threshold pace (242 sec/km = 4:02/km).
        # adjusted_pace / original_pace = 1 / (1 + total_f)
        from primitives.env_pacing import heat_correction_fraction, humidity_correction_fraction
        heat_f = heat_correction_fraction(temp)
        hum_f = humidity_correction_fraction(dp)
        total_f = heat_f + hum_f
        factor = 1.0 + total_f  # speed ratio (negative → slower)
        adj_obj = adjust_run_pace(242.0, temp, dp)  # 4:02/km as anchor
        trail = f"L1 env pacing (run): {adj_obj.reasoning_trail}"

    rule = RuleResult("R7", True, trail)
    return rule, factor


# ---------------------------------------------------------------------------
# Main modulation function
# ---------------------------------------------------------------------------

def modulate_session(
    planned: dict,
    readiness: dict,
) -> SessionPrescription:
    """Apply modulation rules to a planned session and return a prescription.

    Args:
        planned: {
            "session_type": str,          # bike_threshold|bike_z2|bike_vo2|bike_race_pace|
                                          # run_quality|run_easy|run_long|swim|strength|brick
            "target_intensity": float,    # 0–1.5; 1.0 = FTP/threshold
            "interval_count": int|None,
            "interval_duration_min": float|None,
            "recovery_min": float|None,
            "total_duration_min": int,
        }
        readiness: {
            "atl": float,
            "ctl": float,
            "hrv_trend_pct": float,         # 7d HRV trend (negative = declining)
            "sleep_h_last_night": float|None,
            "last_session_rpe": int|None,   # from session-log.json
            "ankle_pain_score": int,        # from current-state.md
            "ankle_quality_cleared": bool,
            "temp_c": float,
            "dew_point_c": float,
        }

    Returns:
        SessionPrescription with adjustments, reasoning trails, and summary.
    """
    stype = planned["session_type"]
    intensity = planned.get("target_intensity", 1.0)
    interval_count = planned.get("interval_count")
    interval_duration = planned.get("interval_duration_min")
    recovery = planned.get("recovery_min")
    duration = planned.get("total_duration_min", 60)

    applied: list[str] = []
    trails: list[str] = []
    go = True
    swapped = False

    # --- R1: ankle hard stop (must be first) ---
    r1 = _r1_ankle_hard_stop(planned, readiness)
    if r1.fired:
        applied.append(r1.code)
        trails.append(r1.reasoning_trail)
        go = False
        return SessionPrescription(
            session_type=stype,
            go=False,
            swapped_to_z2=False,
            modified=True,
            target_intensity=intensity,
            interval_count=interval_count,
            interval_duration_min=interval_duration,
            recovery_min=recovery,
            total_duration_min=duration,
            applied_rules=applied,
            reasoning_trails=trails,
            summary="BLOCKED by R1 (ankle). Swap to easy or rest.",
        )

    # --- R2 and R6: swap triggers (mutually exclusive; R2 fires first if both) ---
    r2 = _r2_atl_swap(planned, readiness)
    r6 = _r6_sleep_two_signal(planned, readiness)

    swap_rule = None
    if r2.fired:
        swap_rule = r2
    elif r6.fired:
        swap_rule = r6

    if swap_rule is not None:
        applied.append(swap_rule.code)
        trails.append(swap_rule.reasoning_trail)
        swapped = True
        new_type = "bike_z2" if "bike" in stype else "run_easy"
        return SessionPrescription(
            session_type=new_type,
            go=True,
            swapped_to_z2=True,
            modified=True,
            target_intensity=0.65,  # easy Z2 anchor
            interval_count=None,
            interval_duration_min=None,
            recovery_min=None,
            total_duration_min=duration,
            applied_rules=applied,
            reasoning_trails=trails,
            summary=f"SWAPPED to Z2 by {swap_rule.code}. Hold {duration} min easy.",
        )

    # --- Soft rules (stack; only for quality sessions) ---
    if _is_quality(stype):
        # R3: HRV
        r3 = _r3_hrv_intensity_drop(planned, readiness)
        if r3.fired:
            applied.append(r3.code)
            trails.append(r3.reasoning_trail)
            intensity = round(intensity - _INTENSITY_STEP, 4)
            if interval_count is not None and interval_count > 1:
                interval_count -= 1

        # R4: ATL moderate
        r4 = _r4_atl_moderate_cap(planned, readiness)
        if r4.fired:
            applied.append(r4.code)
            trails.append(r4.reasoning_trail)
            intensity = min(intensity, round(1.0 - _INTENSITY_STEP, 4))  # cap 95%
            if interval_count is not None and interval_count > 1 and "R3" not in applied:
                interval_count -= 1

        # R5: prior RPE
        r5 = _r5_prior_rpe(planned, readiness)
        if r5.fired:
            applied.append(r5.code)
            trails.append(r5.reasoning_trail)
            intensity = round(intensity - _INTENSITY_STEP, 4)

        # R6 partial: single sleep signal → reduce interval count only (no swap triggered above)
        sleep = readiness.get("sleep_h_last_night")
        if (
            sleep is not None
            and _SLEEP_SWAP_H <= sleep < _SLEEP_REDUCE_H
            and interval_count is not None
            and interval_count > 1
        ):
            applied.append("R6-single")
            trails.append(
                f"Sleep {sleep:.1f}h (<{_SLEEP_REDUCE_H}h, single signal) "
                f"→ R6 partial: multi-signal not met → intervals −1 only "
                f"→ reduce total stress without full swap (HRV corroboration absent)"
            )
            interval_count -= 1

    # --- R7: heat (always, adjusts intensity fraction) ---
    r7, heat_factor = _r7_heat(planned, readiness)
    if r7.fired:
        applied.append(r7.code)
        trails.append(r7.reasoning_trail)
        intensity = round(intensity * heat_factor, 4)

    # Floor: never prescribe below 65% FTP / threshold for quality → flag if so
    intensity = max(intensity, 0.65)

    modified = bool(applied)
    rules_str = "+".join(applied) if applied else "none"

    if not modified:
        summary = "No adjustments — execute as planned."
    elif swapped:
        summary = f"SWAPPED to Z2 ({rules_str})."
    else:
        parts = []
        if planned.get("target_intensity") and abs(intensity - planned["target_intensity"]) > 0.001:
            parts.append(
                f"intensity {format_if(planned['target_intensity'])} → {format_if(intensity)}"
            )
        orig_count = planned.get("interval_count")
        if orig_count is not None and interval_count != orig_count:
            parts.append(f"intervals {orig_count} → {interval_count}")
        summary = f"Modified ({rules_str}): {'; '.join(parts)}." if parts else f"Modified ({rules_str})."

    return SessionPrescription(
        session_type=stype,
        go=go,
        swapped_to_z2=swapped,
        modified=modified,
        target_intensity=intensity,
        interval_count=interval_count,
        interval_duration_min=interval_duration,
        recovery_min=recovery,
        total_duration_min=duration,
        applied_rules=applied,
        reasoning_trails=trails,
        summary=summary,
    )
