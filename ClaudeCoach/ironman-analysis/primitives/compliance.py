"""Session compliance analytics.

Compares planned TSS against actual TSS, classifies the root cause of
gaps, and derives a forward-correction factor for future planning.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Optional


_SPORT_MAP: dict[str, str] = {
    "Ride": "bike",
    "VirtualRide": "bike",
    "MountainBikeRide": "bike",
    "GravelRide": "bike",
    "Run": "run",
    "VirtualRun": "run",
    "Swim": "swim",
    "WeightTraining": "strength",
    "Workout": "strength",
}

_DURATION_SHORT_THRESHOLD = 0.80   # < 80% planned duration → session was cut
_TSS_GAP_THRESHOLD = 0.88          # < 88% planned TSS at full duration → intensity gap
_RPE_FATIGUED = 7                  # RPE ≥ 7 with intensity gap → fatigued, not soft


@dataclass
class ComplianceRecord:
    session_date: str            # YYYY-MM-DD
    sport: str                   # bike | run | swim | strength
    session_name: str
    planned_tss: float
    actual_tss: float
    planned_duration_min: float
    actual_duration_min: float
    rpe: Optional[int]           # from session-log.json; None if not logged
    gap_classification: str      # see classify_gap() docstring
    gap_pct: float               # (actual - planned) / planned; negative = missed


def _sport_category(raw_type: str) -> str:
    return _SPORT_MAP.get(raw_type, raw_type.lower())


def classify_gap(
    planned_tss: float,
    actual_tss: float,
    planned_duration_min: float,
    actual_duration_min: float,
    rpe: Optional[int],
) -> str:
    """Classify why a session fell short of its planned TSS.

    Returns one of:
        completed              — within 12% of planned TSS
        skipped                — no activity logged
        duration_short         — session was cut to < 80% of planned duration
        intensity_short_fatigued  — full duration, low TSS, RPE ≥ 7 (working hard, can't hit target)
        intensity_short_soft      — full duration, low TSS, RPE < 7 (didn't push)
        intensity_short_unknown   — full duration, low TSS, no RPE logged
    """
    if actual_duration_min == 0 or actual_tss == 0:
        return "skipped"

    duration_ratio = (
        actual_duration_min / planned_duration_min if planned_duration_min > 0 else 1.0
    )

    if duration_ratio < _DURATION_SHORT_THRESHOLD:
        return "duration_short"

    tss_ratio = actual_tss / planned_tss if planned_tss > 0 else 1.0

    if tss_ratio < _TSS_GAP_THRESHOLD:
        if rpe is None:
            return "intensity_short_unknown"
        return "intensity_short_fatigued" if rpe >= _RPE_FATIGUED else "intensity_short_soft"

    return "completed"


def tss_gap_series(
    planned_events: list[dict],
    actual_activities: list[dict],
    session_log: list[dict],
) -> list[ComplianceRecord]:
    """Match planned sessions to actual activities and build compliance records.

    planned_events: list of dicts from get_events — expected keys:
        date (YYYY-MM-DD), name, type or sport, planned_tss (optional),
        planned_duration_min (optional)

    actual_activities: list of dicts from get_training_history — expected keys:
        date (ISO or YYYY-MM-DD), type, tss,
        moving_time (seconds) or duration_minutes

    session_log: list of dicts from session-log.json — expected keys:
        date, sport, rpe

    Events without planned_tss are skipped — gap analysis requires a target.
    """
    # Index actual activities by (date, sport_category)
    actual_index: dict[tuple[str, str], list[dict]] = {}
    for act in actual_activities:
        act_date = str(act.get("date", ""))[:10]
        sport = _sport_category(act.get("type", ""))
        actual_index.setdefault((act_date, sport), []).append(act)

    # Index RPE from session log by (date, sport_category)
    rpe_index: dict[tuple[str, str], int] = {}
    for entry in session_log:
        rpe_date = str(entry.get("date", ""))[:10]
        rpe_sport = _sport_category(entry.get("sport", ""))
        rpe_val = entry.get("rpe")
        if rpe_val is not None:
            rpe_index[(rpe_date, rpe_sport)] = int(rpe_val)

    records: list[ComplianceRecord] = []

    for event in planned_events:
        ev_date = str(event.get("date", ""))[:10]
        sport = _sport_category(event.get("type", event.get("sport", "")))
        name = event.get("name", "")
        planned_tss = float(event.get("planned_tss") or 0.0)
        planned_dur = float(event.get("planned_duration_min") or 0.0)

        if planned_tss == 0:
            continue  # can't do compliance analysis without a target

        key = (ev_date, sport)
        matched = actual_index.get(key, [])

        if not matched:
            records.append(ComplianceRecord(
                session_date=ev_date,
                sport=sport,
                session_name=name,
                planned_tss=planned_tss,
                actual_tss=0.0,
                planned_duration_min=planned_dur,
                actual_duration_min=0.0,
                rpe=None,
                gap_classification="skipped",
                gap_pct=-1.0,
            ))
            continue

        # Take the activity with the highest TSS on the same day/sport
        act = max(matched, key=lambda a: float(a.get("tss") or 0))
        actual_tss = float(act.get("tss") or 0.0)

        # Prefer moving_time (seconds → minutes) over duration_minutes
        if "moving_time" in act:
            actual_dur = float(act["moving_time"]) / 60.0
        else:
            actual_dur = float(act.get("duration_minutes") or 0.0)

        rpe = rpe_index.get(key)
        gap_pct = (actual_tss - planned_tss) / planned_tss if planned_tss > 0 else 0.0
        classification = classify_gap(planned_tss, actual_tss, planned_dur, actual_dur, rpe)

        records.append(ComplianceRecord(
            session_date=ev_date,
            sport=sport,
            session_name=name,
            planned_tss=planned_tss,
            actual_tss=actual_tss,
            planned_duration_min=planned_dur,
            actual_duration_min=actual_dur,
            rpe=rpe,
            gap_classification=classification,
            gap_pct=gap_pct,
        ))

    return records


def rolling_compliance(records: list[ComplianceRecord]) -> dict:
    """Aggregate compliance metrics over the supplied records.

    Caller is responsible for date-filtering before passing records.

    Returns:
        compliance_rate       — actual TSS / planned TSS (overall)
        completion_rate       — fraction of sessions classified as "completed"
        classification_counts — breakdown by gap_classification
        dominant_gap_type     — most common non-completed classification (None if all complete)
        session_count         — total sessions analysed
    """
    if not records:
        return {
            "compliance_rate": 1.0,
            "completion_rate": 1.0,
            "classification_counts": {},
            "dominant_gap_type": None,
            "session_count": 0,
        }

    total_planned = sum(r.planned_tss for r in records)
    total_actual = sum(r.actual_tss for r in records)
    compliance_rate = total_actual / total_planned if total_planned > 0 else 1.0

    counts = Counter(r.gap_classification for r in records)
    completion_rate = counts.get("completed", 0) / len(records)

    incomplete = {k: v for k, v in counts.items() if k != "completed"}
    dominant = max(incomplete, key=incomplete.get) if incomplete else None

    return {
        "compliance_rate": round(compliance_rate, 4),
        "completion_rate": round(completion_rate, 4),
        "classification_counts": dict(counts),
        "dominant_gap_type": dominant,
        "session_count": len(records),
    }


def forward_correction_factor(compliance_rate: float) -> float:
    """Multiplier for planned TSS targets so actual TSS lands on the goal.

    Only valid when dominant_gap_type is "intensity_short_soft". Scaling
    targets when the gap is fatigue or skipped sessions is the wrong lever
    — the prompt layer must check before calling this.

    Bands:
        ≥ 97%  : already compliant → 1.0 (no correction)
        70–97% : apply correction, capped at ×1.20 to protect the ramp
        < 70%  : structural problem → 1.0 (scaling won't fix it)
    """
    if compliance_rate >= 0.97 or compliance_rate < 0.70:
        return 1.0
    return min(round(1.0 / compliance_rate, 4), 1.20)


def compliance_recommendations(metrics: dict) -> list[str]:
    """Translate rolling compliance metrics into actionable coaching recommendations.

    Returns a list of strings — one per relevant finding. Empty if fully compliant.
    """
    compliance_rate = metrics.get("compliance_rate", 1.0)
    dominant = metrics.get("dominant_gap_type")
    session_count = metrics.get("session_count", 0)

    if session_count < 4:
        return ["Fewer than 4 sessions with planned TSS — not enough data for reliable compliance analysis."]

    if compliance_rate >= 0.97:
        return []

    gap_pct = round((1 - compliance_rate) * 100, 1)
    recs: list[str] = []

    if dominant == "skipped":
        recs.append(
            f"Missing {gap_pct}% of planned TSS through skipped sessions. "
            "Root cause is adherence, not fatigue — protect session time in calendar. "
            "Scaling targets upward won't help."
        )
    elif dominant == "intensity_short_fatigued":
        recs.append(
            f"Missing {gap_pct}% of planned TSS — full duration, high RPE, can't hit intensity. "
            "Load is too ambitious for current fitness. "
            "Reduce planned quality-session TSS by "
            f"{gap_pct:.0f}% or add one recovery day per week."
        )
    elif dominant == "intensity_short_soft":
        factor = forward_correction_factor(compliance_rate)
        recs.append(
            f"Missing {gap_pct}% of planned TSS — full duration, low RPE, not pushing to target. "
            "Execution gap: add power alerts or explicit interval targets. "
            f"Forward correction factor: ×{factor:.2f} applied to quality session targets."
        )
    elif dominant == "duration_short":
        recs.append(
            f"Missing {gap_pct}% of planned TSS through shortened sessions. "
            "Check scheduling — sessions are being cut by external time pressure."
        )
    elif dominant == "intensity_short_unknown":
        recs.append(
            f"Missing {gap_pct}% of planned TSS — root cause unclear (no RPE logged). "
            "Log RPE after sessions to enable fatigue vs execution diagnosis."
        )

    return recs
