"""Constrained week re-optimiser (W1).

Given what was planned vs what was completed mid-week, determines whether
missed load can be redistributed into remaining days without violating ramp,
quality-session spacing, or weekly-hour constraints.

Claude handles the actual session scheduling — these functions supply the
constraint envelope so Claude's redistribution stays within safe bounds.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Optional


_MAX_RAMP_CTL_PER_WEEK_REHAB = 4.0    # CTL/week while ankle still in rehab
_MAX_RAMP_CTL_PER_WEEK_NORMAL = 6.0   # CTL/week post-clearance
_MAX_DEBT_PCT_TO_REDISTRIBUTE = 0.40  # > 40% of weekly planned TSS missed → absorb
_MAX_DAYS_MISSED = 3                   # > 3 days missed → absorb, don't pile up


@dataclass
class WeekDebt:
    week_start: str             # YYYY-MM-DD (Monday)
    planned_tss: float          # total planned TSS for the week
    actual_tss_to_date: float   # TSS accumulated so far this week
    debt_tss: float             # planned-to-date minus actual (positive = missed)
    debt_pct: float             # debt_tss / planned_tss
    days_elapsed: int           # days since Monday (0 = Monday)
    days_remaining: int         # days left including today
    days_missed: int            # days where TSS was planned but nothing logged
    redistributable: bool
    reason: str                 # empty when redistributable=True


def assess_week_debt(
    planned_sessions: list[dict],
    actual_sessions: list[dict],
    today: str,
) -> WeekDebt:
    """Assess how much load was missed so far and whether redistribution is viable.

    planned_sessions: from get_events for the current week — each needs
        date (YYYY-MM-DD) and planned_tss.
    actual_sessions: from get_training_history for the current week — each needs
        date (YYYY-MM-DD or ISO) and tss.
    today: YYYY-MM-DD — the current date (use get_athlete_profile current_date_local).
    """
    today_date = date.fromisoformat(today)
    week_start = today_date - timedelta(days=today_date.weekday())  # Monday
    week_end = week_start + timedelta(days=6)                        # Sunday
    yesterday = today_date - timedelta(days=1)

    # Planned TSS by date (only days that have already passed)
    planned_by_date: dict[str, float] = {}
    planned_total = 0.0
    for ev in planned_sessions:
        ev_date_str = str(ev.get("date", ""))[:10]
        ptss = float(ev.get("planned_tss") or 0.0)
        if not ev_date_str or ptss == 0:
            continue
        planned_total += ptss
        ev_date = date.fromisoformat(ev_date_str)
        if week_start <= ev_date <= yesterday:
            planned_by_date[ev_date_str] = planned_by_date.get(ev_date_str, 0.0) + ptss

    # Actual TSS by date (completed window only)
    actual_by_date: dict[str, float] = {}
    for act in actual_sessions:
        act_date_str = str(act.get("date", ""))[:10]
        atss = float(act.get("tss") or 0.0)
        if not act_date_str:
            continue
        act_date = date.fromisoformat(act_date_str)
        if week_start <= act_date <= yesterday:
            actual_by_date[act_date_str] = actual_by_date.get(act_date_str, 0.0) + atss

    actual_to_date = sum(actual_by_date.values())
    planned_to_date = sum(planned_by_date.values())
    debt_tss = max(0.0, planned_to_date - actual_to_date)
    debt_pct = debt_tss / planned_total if planned_total > 0 else 0.0

    days_elapsed = (today_date - week_start).days
    days_remaining = (week_end - today_date).days + 1  # today is available

    days_missed = sum(
        1
        for d, ptss in planned_by_date.items()
        if ptss > 0 and actual_by_date.get(d, 0.0) == 0
    )

    redistributable = True
    reason = ""

    if days_remaining <= 1:
        redistributable = False
        reason = "Week ends tomorrow — no safe redistribution window"
    elif days_missed > _MAX_DAYS_MISSED:
        redistributable = False
        reason = (
            f"{days_missed} days missed (limit {_MAX_DAYS_MISSED}) — "
            "absorb the gap and continue from current fitness"
        )
    elif debt_pct > _MAX_DEBT_PCT_TO_REDISTRIBUTE:
        redistributable = False
        reason = (
            f"{debt_pct:.0%} of planned TSS missed "
            f"(limit {_MAX_DEBT_PCT_TO_REDISTRIBUTE:.0%}) — "
            "too large to recover safely this week"
        )

    return WeekDebt(
        week_start=str(week_start),
        planned_tss=planned_total,
        actual_tss_to_date=actual_to_date,
        debt_tss=round(debt_tss, 1),
        debt_pct=round(debt_pct, 4),
        days_elapsed=days_elapsed,
        days_remaining=days_remaining,
        days_missed=days_missed,
        redistributable=redistributable,
        reason=reason,
    )


def ramp_headroom(
    current_ctl: float,
    weekly_planned_tss: float,
    ankle_in_rehab: bool = True,
) -> float:
    """Maximum additional TSS that can be added this week without breaching the ramp cap.

    Derivation (Banister, constant-load approximation):
        ΔCTL/week = (avg_daily_TSS - CTL) × (7/42)
        Solving for max_weekly_TSS at ΔCTL = max_ramp:
        max_weekly_TSS = (CTL + max_ramp × 6) × 7 = CTL × 7 + max_ramp × 42

    Returns 0 if already at or above the ramp cap.
    """
    max_ramp = (
        _MAX_RAMP_CTL_PER_WEEK_REHAB if ankle_in_rehab else _MAX_RAMP_CTL_PER_WEEK_NORMAL
    )
    max_weekly_tss = current_ctl * 7 + max_ramp * 42
    return max(0.0, round(max_weekly_tss - weekly_planned_tss, 1))


def apply_compliance_correction(
    sessions: list[dict],
    correction_factor: float,
) -> list[dict]:
    """Scale planned TSS targets in a session list by the correction factor.

    Only applicable when the dominant compliance gap is "intensity_short_soft"
    (athlete consistently completes sessions at lower intensity than prescribed
    with low RPE — execution gap, not fatigue). The caller must verify this
    before using this function.

    Only quality sessions are corrected — Z2, recovery, and swim sessions are
    typically completed as planned and should not have targets inflated.

    Returns a new list of dicts; originals are not mutated.
    """
    _CORRECTABLE = {
        "bike_threshold",
        "bike_vo2",
        "bike_race_pace",
        "run_quality",
        "run_long",
        "brick",
    }

    if correction_factor == 1.0:
        return [dict(s) for s in sessions]

    result: list[dict] = []
    for s in sessions:
        session = dict(s)
        if session.get("session_type") in _CORRECTABLE:
            if session.get("planned_tss"):
                session["planned_tss"] = round(
                    float(session["planned_tss"]) * correction_factor, 1
                )
        result.append(session)

    return result


def quality_session_spacing_ok(
    new_session_date: str,
    existing_sessions: list[dict],
) -> bool:
    """Check whether placing a quality session on new_session_date would create
    back-to-back quality days.

    existing_sessions: list of dicts with keys date (YYYY-MM-DD) and
        session_type (string). Only sessions already scheduled this week.

    Quality session types: bike_threshold, bike_vo2, bike_race_pace,
        run_quality, run_long, brick.
    """
    _QUALITY = {
        "bike_threshold", "bike_vo2", "bike_race_pace",
        "run_quality", "run_long", "brick",
    }
    target = date.fromisoformat(new_session_date)

    for s in existing_sessions:
        stype = s.get("session_type", "")
        if stype not in _QUALITY:
            continue
        s_date = date.fromisoformat(str(s.get("date", ""))[:10])
        if abs((target - s_date).days) <= 1:
            return False

    return True
