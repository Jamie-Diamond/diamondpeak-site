"""Fuelling prescription — deterministic, never LLM arithmetic.

The old rule (nutrition_target = avg_g_hr + 10, capped 90, no floor) was the wrong
SHAPE: a flat +10 step is fine near the race target but useless from a low base —
it told Kathryn "30 g/hr" off a ~20 g/hr dip against a 70 g/hr 70.3 target, and
20→30 is no progress. This replaces it with a gap-closing ramp:

  * AGGRESSIVE below 60 g/hr — the deficit is harmful and the gut tolerates the
    jump; close most of the way to 60 fast (~+25/block).
  * CAREFUL at/above 60 g/hr — this is gut fine-tuning toward race pace; small
    steps (~+5/block) so tolerance adapts without GI distress.

Never exceeds the athlete's race target; never prescribes a uselessly low number.
"""
from __future__ import annotations

_AGGRESSIVE_STEP = 25   # g/hr per block while below the careful threshold
_CAREFUL_STEP = 5       # g/hr per block at/above it
_CAREFUL_FROM = 60      # g/hr — boundary between aggressive and careful ramp
_MIN_USEFUL = 40        # never prescribe below this for a >90-min session


def fuel_target(avg_g_hr, race_target_g_hr) -> int:
    """Prescribed carbs (g/hr) for >90-min sessions, gap-closing toward the race
    target. avg_g_hr is the athlete's recent average intake (None if no logs)."""
    rt = float(race_target_g_hr)
    if avg_g_hr is None:
        base = min(_CAREFUL_FROM, rt)
    elif avg_g_hr < _CAREFUL_FROM:
        base = min(float(_CAREFUL_FROM), avg_g_hr + _AGGRESSIVE_STEP)
    else:
        base = avg_g_hr + _CAREFUL_STEP
    target = round(base / 5) * 5                      # round to nearest 5
    return int(max(min(target, rt), min(_MIN_USEFUL, rt)))


def recent_avg_g_hr(session_log, n: int = 6):
    """Mean carbs-per-hour over the most recent `n` long (>=90 min) ride/brick
    sessions with a logged carb total. Returns None if there are none."""
    rated = []
    for e in session_log or []:
        sport = (e.get("sport") or "")
        dur = e.get("duration_min") or 0
        carb = e.get("nutrition_g_carb")
        if sport in ("Ride", "GravelRide", "VirtualRide", "Brick") and dur >= 90 and carb:
            rated.append((e.get("date") or "", carb / dur * 60))
    if not rated:
        return None
    rated.sort(key=lambda x: x[0], reverse=True)
    recent = [r for _, r in rated[:n]]
    return sum(recent) / len(recent)
