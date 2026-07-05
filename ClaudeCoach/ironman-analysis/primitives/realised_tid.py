"""Realised (executed) intensity distribution — methodology audit P1-1.

The plan validator classifies sessions by NAME on the plan; nothing ever
checked what the athlete actually DID. This measures the realised distribution
from completed activities, session-level and coarse (the audit\'s own method):
whole-activity power IF when present, else average HR relative to LTHR. Both
failure directions matter: excess quality (grey-zone drift — easy sessions
executed too hard) and missing quality (a build week collapsing to all-easy).
"""
from __future__ import annotations

# Session-level classification bounds (whole-session effective intensity).
IF_LOW, IF_HIGH = 0.75, 0.88          # power IF thresholds
HR_LOW, HR_HIGH = 0.85, 0.95          # avg HR / LTHR thresholds

SKIP_TYPES = {"WeightTraining", "Workout", "Yoga", "Pilates"}   # not TID-relevant


def classify_activity(a: dict, lthr: float | None = None) -> str | None:
    """low / moderate / high, or None when the activity carries no usable signal."""
    t = float(a.get("moving_time") or 0)
    if t <= 0 or (a.get("type") or "") in SKIP_TYPES:
        return None
    if_ = a.get("icu_intensity")
    if if_:
        if_ = float(if_)
        if if_ > 3:                     # ICU reports percent on some endpoints
            if_ /= 100.0
        return "low" if if_ < IF_LOW else ("moderate" if if_ < IF_HIGH else "high")
    hr = a.get("average_heartrate")
    if hr and lthr:
        r = float(hr) / float(lthr)
        return "low" if r < HR_LOW else ("moderate" if r < HR_HIGH else "high")
    return None


def realised_tid(activities: list, lthr: float | None = None) -> dict | None:
    """% of classified moving time in low/moderate/high. None if nothing classifiable."""
    time_in = {"low": 0.0, "moderate": 0.0, "high": 0.0}
    skipped = 0
    for a in activities or []:
        c = classify_activity(a, lthr)
        if c is None:
            skipped += 1
            continue
        time_in[c] += float(a.get("moving_time") or 0)
    total = sum(time_in.values())
    if total <= 0:
        return None
    pct = {k: round(v / total * 100) for k, v in time_in.items()}
    return {"low_pct": pct["low"], "moderate_pct": pct["moderate"],
            "high_pct": pct["high"], "classified_hours": round(total / 3600, 1),
            "unclassified_sessions": skipped}


def tid_verdict(realised: dict, target_low_mod_high, low_tolerance_pp: float = 12.0) -> dict:
    """Compare realised vs the phase TID target [low, mod, high] — BOTH directions."""
    lo_t, mid_t, hi_t = target_low_mod_high
    lo = realised["low_pct"]
    out = {"target": list(target_low_mod_high), "breach": None}
    if lo < lo_t - low_tolerance_pp:
        out["breach"] = ("excess_quality",
                         f"realised easy share {lo}% vs target {lo_t}% — grey-zone "
                         f"drift: easy sessions are being executed too hard")
    elif (mid_t + hi_t) >= 15 and (realised["moderate_pct"] + realised["high_pct"]) == 0 \
            and realised["classified_hours"] >= 3:
        out["breach"] = ("missing_quality",
                         f"no moderate/high time recorded vs target {mid_t + hi_t}% — "
                         f"the week collapsed to all-easy")
    return out
