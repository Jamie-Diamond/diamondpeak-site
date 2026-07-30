"""RPE in the context of the session that produced it.

A bare RPE is not a signal. RPE 8 after a VO2 set is the session working; RPE 8
after an easy spin is an alarm. Modulation R5 fired on the raw value (`rpe >= 8`),
so it reacted to hard sessions feeling hard and was blind to easy sessions feeling
terrible — backwards in both directions.

## Where the expected value comes from

A COLD-START PRIOR (the agreed table below) that each athlete's own history
progressively overrides. Confirmed with Jamie 2026-07-30: "use the agreed table for
new athletes then re-calibrate over time."

The prior is keyed off MEASURED INTENSITY (IF derived from Load and duration), not
session type. Session type comes from `classify_session_type`, which reads the
session NAME, and names lie: Kathryn's 6x3min Z4 interval set is called "Wandsworth
Running" and classified `run_easy`; Calum's 372min Alpine ride is `bike_z2`; a
threshold session was typed `brick` because "brick" appeared in a time-cap note.
Load and duration are measured, so they cannot lie the same way.

## Why re-calibration is necessary

Median logged RPE by measured intensity, 2026-07-30, shows RPE scales are personal:

    IF band          jamie      kathryn    calum
    easy   <0.70     5.0 (n=5)  5.0 (n=4)  7.5 (n=4)
    steady .70-.80   5.0 (n=8)  5.0 (n=18) 9.0 (n=1)
    tempo  .80-.90   7.0 (n=7)  6.2 (n=12) -

Calum (beginner) runs ~2.5 points above the others at identical measured intensity.
Strength is further apart still: Jamie logs 1,1,1 where Kathryn logs 5,6,7,8. And
no athlete logs the agreed 3-4 for easy work — all sit at 5+. A fixed table applied
to everyone flagged 28% of all logged RPEs, i.e. it would nag.

## Response asymmetry (confirmed with Jamie 2026-07-30)

DETECTION is symmetric — an unexpectedly easy session is as interesting as an
unexpectedly hard one. RESPONSE is not: harder-than-expected eases load;
easier-than-expected raises an advisory note only. One RPE point is thin evidence
for ADDING load, and the under-training floor already covers volume.

Any out-of-band value is CONFIRMED WITH THE ATHLETE before it moves load, so a
fat-fingered 8 instead of 3 cannot silently trim a training week.
"""

import math
import statistics

# --- cold-start prior (the agreed table), keyed on measured intensity ----------
# (lo, hi) inclusive. Bands, not points, because the same intensity legitimately
# spans a couple of RPE points depending on the day.
#
# IF-from-Load is NOT comparable across sports: on the current corpus the median
# is bike 0.72, run 0.81, swim 0.87, because rTSS and sTSS are scaled differently
# from cycling TSS. Cycling-calibrated edges therefore label an easy run "tempo".
# Edges below are the per-sport quartiles of the actual logged corpus (n=85 bike,
# 42 run, 31 swim, 2026-07-30), so each band means the same thing WITHIN a sport.
# Revisit if the corpus grows a lot or an athlete's training mix changes.
IF_EDGES = {            # (q25, median, q75) — below q25 = easy, above q75 = hard
    "bike": (0.68, 0.72, 0.78),
    "run":  (0.78, 0.81, 0.84),
    "swim": (0.79, 0.87, 0.92),
}
SPORT_FAMILY = {
    "Ride": "bike", "VirtualRide": "bike", "GravelRide": "bike",
    "MountainBikeRide": "bike", "TrackRide": "bike", "Cyclocross": "bike",
    "Run": "run", "TrailRun": "run", "VirtualRun": "run",
    "Swim": "swim", "OpenWaterSwim": "swim",
}
# Band index -> the agreed prior. Index 0 = easy ... 3 = hard.
PRIOR_BY_BAND = [(3, 4), (5, 6), (6, 7), (7, 8)]

# TRIED AND REJECTED 2026-07-30: shifting the prior by `coaching_level`
# (beginner +2 / pro -2). It looked obvious — every cold-start flag for Jamie (pro)
# said "easier" and every one for Calum (beginner) said "harder" — but on replay it
# fixed Kathryn and Calum (both to zero asks) while making Jamie WORSE, 8 asks -> 14.
# Reason: ability is per-sport, not per-athlete. Jamie is strong on the bike and run
# but swimming is his weak discipline, where he logs 7-8; a single athlete-wide shift
# pushed his expected swim RPE down to ~4 and flagged nearly every swim. Left at zero
# deliberately — do not reintroduce without per-sport ability data.
LEVEL_SHIFT = {"beginner": 0, "mid": 0, "pro": 0}
PRIOR_STRENGTH = (6, 7)
PRIOR_SWIM = (5, 7)        # spans technique work to CSS sets

# Sports where Load/duration does not describe the demand, or that are not training.
STRENGTH_SPORTS = {"Strength", "Workout"}
NON_TRAINING_SPORTS = {"Golf", "Sailing", "Walk"}

LONG_SESSION_MIN = 180     # at/above this, a session feels harder than its IF implies
LONG_SESSION_SHIFT = 1     # ...so shift the prior band up by this much

MIN_N_FOR_OWN_DATA = 2     # below this, prior only — kept low because ALL
                           # residual false asks on replay were at n=0-1
TIGHT_SPREAD = 2           # own values this tightly clustered => trust them outright
PRIOR_WEIGHT = 2           # pseudo-count: own data outweighs the prior from n>2
CONFIRM_DELTA = 2.5        # how far from expected before we ask the athlete


def intensity_factor(tss, duration_min) -> float | None:
    """IF back-derived from Load and duration (Load = IF^2 * hours * 100)."""
    try:
        hours = float(duration_min) / 60.0
        if hours <= 0 or not tss:
            return None
        return math.sqrt((float(tss) / hours) / 100.0)
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def bucket(entry: dict) -> str | None:
    """Comparability bucket for an entry, or None if not assessable.

    Returns None rather than guessing — an unassessable session is skipped, not
    given a default band. Defaulting is what produces spurious asks.
    """
    sport = entry.get("sport") or ""
    if sport in NON_TRAINING_SPORTS:
        return None
    if sport in STRENGTH_SPORTS:
        return "strength"
    family = SPORT_FAMILY.get(sport)
    if family is None:
        return None
    IF = intensity_factor(entry.get("tss"), entry.get("duration_min"))
    if IF is None:
        return None
    q25, med, q75 = IF_EDGES[family]
    band = 0 if IF < q25 else 1 if IF < med else 2 if IF < q75 else 3
    long_tag = ""
    try:
        if float(entry.get("duration_min") or 0) >= LONG_SESSION_MIN:
            long_tag = "+long"
    except (TypeError, ValueError):
        pass
    # Sport family is part of the key so runs are never pooled with rides.
    return f"{family}:b{band}{long_tag}"


def prior_for(bucket_name: str, coaching_level: str = "mid") -> tuple[int, int] | None:
    lvl = LEVEL_SHIFT.get(coaching_level or "mid", 0)
    if bucket_name == "strength":
        return (PRIOR_STRENGTH[0] + lvl, PRIOR_STRENGTH[1] + lvl)
    if ":" not in bucket_name:
        return None
    family, tail = bucket_name.split(":", 1)
    if family == "swim":
        # Swim RPE tracks stroke/technique work more than load rate; one band.
        base = PRIOR_SWIM
    else:
        try:
            base = PRIOR_BY_BAND[int(tail.replace("+long", "")[1:])]
        except (ValueError, IndexError):
            return None
    shift = (LONG_SESSION_SHIFT if tail.endswith("+long") else 0) + lvl
    return (base[0] + shift, base[1] + shift)


def _own_values(history: list, bucket_name: str) -> list[float]:
    """That athlete's logged RPEs for comparable sessions."""
    out = []
    for e in history or []:
        if e.get("rpe") is None or e.get("stub"):
            continue
        if bucket(e) == bucket_name:
            try:
                out.append(float(e["rpe"]))
            except (TypeError, ValueError):
                pass
    return out


def expected(entry: dict, history: list, coaching_level: str = "mid") -> dict | None:
    """Expected RPE for this session, blending the prior with the athlete's own data.

    Returns None when not assessable at all.
    `source` is 'prior' | 'blend' | 'own' — worth surfacing so a flag can be read
    with the right amount of trust.
    """
    b = bucket(entry)
    if b is None:
        return None
    prior = prior_for(b, coaching_level)
    if prior is None:
        return None
    prior_centre = (prior[0] + prior[1]) / 2.0

    own = _own_values(history, b)
    # Exclude this very entry, so a session is never compared against itself.
    try:
        if entry.get("rpe") is not None and float(entry["rpe"]) in own:
            own.remove(float(entry["rpe"]))
    except (TypeError, ValueError):
        pass

    n = len(own)
    if n < MIN_N_FOR_OWN_DATA:
        return {"centre": prior_centre, "source": "prior", "n": n, "bucket": b}

    own_median = statistics.median(own)
    # Tightly-clustered own data beats the prior outright: Jamie logs strength as
    # 1,1,1 — that is his scale, not an anomaly, and shrinking it toward a 6.5
    # prior would flag every single strength session he ever does.
    if (max(own) - min(own)) <= TIGHT_SPREAD:
        return {"centre": own_median, "source": "own", "n": n, "bucket": b}

    centre = ((PRIOR_WEIGHT * prior_centre) + (n * own_median)) / (PRIOR_WEIGHT + n)
    return {"centre": centre, "source": "blend", "n": n, "bucket": b}


def assess(entry: dict, history: list, coaching_level: str = "mid") -> dict:
    """Full assessment of one logged RPE against its context."""
    blank = {"expected": None, "delta": None, "direction": None,
             "needs_confirm": False, "source": None, "n": 0, "bucket": None}
    rpe = entry.get("rpe")
    if rpe is None:
        return blank
    exp = expected(entry, history, coaching_level)
    if exp is None:
        return blank
    try:
        d = float(rpe) - exp["centre"]
    except (TypeError, ValueError):
        return blank
    return {
        "expected": round(exp["centre"], 1),
        "delta": round(d, 1),
        "direction": "as_expected" if abs(d) < CONFIRM_DELTA
                     else ("harder" if d > 0 else "easier"),
        "needs_confirm": abs(d) >= CONFIRM_DELTA,
        "source": exp["source"], "n": exp["n"], "bucket": exp["bucket"],
    }


def r5_verdict(entry: dict, history: list, confirmed: bool | None,
               coaching_level: str = "mid") -> tuple[bool, str]:
    """(should_ease, reason) for the readiness rule.

    Eases ONLY on a confirmed harder-than-expected session. In-band,
    easier-than-expected, and out-of-band-but-unconfirmed all move nothing.
    `confirmed`: True = athlete stood by it, False = asked and not yet answered,
    None = never asked.
    """
    a = assess(entry, history, coaching_level)
    if a["direction"] != "harder":
        return False, ""
    if confirmed is not True:
        return False, (
            f"RPE {entry['rpe']} is {a['delta']:+.1f} vs an expected ~{a['expected']} "
            f"for this athlete's comparable sessions ({a['source']}, n={a['n']}) — "
            f"awaiting their confirmation, not acting on it yet."
        )
    return True, (
        f"RPE {entry['rpe']} vs expected ~{a['expected']} for this athlete's "
        f"comparable sessions ({a['delta']:+.1f}, {a['source']}, n={a['n']}) → prior "
        f"session was harder than it should have been → ease today's intensity"
    )
