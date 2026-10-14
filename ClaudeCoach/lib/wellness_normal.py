"""An athlete's normal HRV and resting HR, and when they are off it (Jamie, 14 Oct 2026:
"should it not be based on standard deviation, what does the literature say?" -> "switch
both to SD-based").

Following the HRV-monitoring literature (AIS HRV best-practice guidelines; Plews et al.
2012; Vesterinen's HRV-guided training): judge each athlete against their OWN baseline
mean and spread, on the 7-day rolling average rather than single mornings, with HRV on a
log scale (ln rMSSD; it is skewed, and the log makes the spread comparable up and down).

    baseline   the BASELINE_DAYS before the day (needs MIN_BASELINE readings)
    band       baseline mean +/- BAND_SD x SD          (the "normal range")
    trend off  7-day average outside the band on the under-recovered side
               (HRV below, resting HR above)
    day off    a single morning beyond DAY_SD x SD on that side

The 0.5 SD band is common practice (a smallest-worthwhile-change band) rather than a
figure confirmed in a primary paper; it is one constant here if it needs tuning.

Used by the morning card's "Off your normal" line (scripts/morning-checkin.py), the
consistency line's lifestyle check (lib/consistency.py), and mirrored in Peak's HRV &
resting HR card (coach/app.js wellnessCard) so the three always agree.
"""
from __future__ import annotations

import math
from datetime import date, timedelta

BASELINE_DAYS = 28
MIN_BASELINE = 14
BAND_SD = 0.5
DAY_SD = 1.0
ROLL_DAYS = 7
BAD = {"hrv": -1, "restingHR": 1}           # which side means under-recovered
# Floor on the spread, so an unusually steady month can't flag a 1-beat wobble:
# ln-HRV 0.03 (~3%), resting HR 1 bpm.
MIN_SD = {"hrv": 0.03, "restingHR": 1.0}


def _f(key):
    return math.log if key == "hrv" else (lambda v: v)


def _finv(key):
    return math.exp if key == "hrv" else (lambda v: v)


def _by_date(rows) -> dict:
    return {str(r.get("id") or r.get("date") or "")[:10]: r for r in rows or []}


def _vals(by: dict, key: str, start: date, end: date) -> list:
    """Transformed readings for dates in [start, end)."""
    out, d = [], start
    while d < end:
        v = (by.get(d.isoformat()) or {}).get(key)
        if isinstance(v, (int, float)) and v > 0:
            out.append(_f(key)(v))
        d += timedelta(days=1)
    return out


def baseline(rows, key: str, day: date):
    """(mean, sd) on the transformed scale over the BASELINE_DAYS before `day`, or None."""
    v = _vals(_by_date(rows), key, day - timedelta(days=BASELINE_DAYS), day)
    if len(v) < MIN_BASELINE:
        return None
    m = sum(v) / len(v)
    sd = math.sqrt(sum((x - m) ** 2 for x in v) / (len(v) - 1))
    return m, max(sd, MIN_SD[key])


def assess(rows, key: str, day: date) -> dict | None:
    """{"usual", "today", "avg7", "lo", "hi", "trend_off", "day_off"} in natural units."""
    b = baseline(rows, key, day)
    if not b:
        return None
    m, sd = b
    by = _by_date(rows)
    inv, f = _finv(key), _f(key)
    t = (by.get(day.isoformat()) or {}).get(key)
    roll = _vals(by, key, day - timedelta(days=ROLL_DAYS - 1), day + timedelta(days=1))
    avg = sum(roll) / len(roll) if len(roll) >= 3 else None
    bad = BAD[key]
    lo, hi = m - BAND_SD * sd, m + BAND_SD * sd
    trend_off = avg is not None and ((avg < lo) if bad < 0 else (avg > hi))
    day_off = isinstance(t, (int, float)) and t > 0 and (
        (f(t) < m - DAY_SD * sd) if bad < 0 else (f(t) > m + DAY_SD * sd))
    return {"usual": inv(m), "today": t, "avg7": inv(avg) if avg is not None else None,
            "lo": inv(lo), "hi": inv(hi), "trend_off": bool(trend_off), "day_off": bool(day_off)}


def off_any(rows, day: date) -> bool:
    """HRV or resting HR off normal (trend or that morning) on `day`."""
    for key in ("hrv", "restingHR"):
        a = assess(rows, key, day)
        if a and (a["trend_off"] or a["day_off"]):
            return True
    return False
