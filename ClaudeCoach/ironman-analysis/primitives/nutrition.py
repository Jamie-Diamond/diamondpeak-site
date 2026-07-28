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

WHICH SESSIONS FEED THE AVERAGE (28 Jul 2026)
---------------------------------------------
The bike average used to be taken over EVERY ride >=90 min. That silently mixed in
the sessions Jamie's own standing rule says must not be read as fuelling evidence:
`athletes/jamie/persistent-rules.md` — "Race fuelling = 90 g/hr race-day target
(NOT a training minimum ...). Bike capacity is proven (90-100 g/hr); only
occasionally program a ride near 90 g/hr for gut tolerance."

A 92-min easy/taper ride carrying one 25 g chew (16 g/hr, 19 Jul 2026) is not a
capacity signal — it is a short ride that needed almost no fuel. Averaged in, it
dragged his window to 58.6 g/hr and prescribed 60 on a bike already proven at
90-100. So the bike window is now scoped to LONG rides (>=150 min, the same
"long ride" threshold the site ETL already uses for its long-ride charts), where
intake genuinely tests the gut. Prescription still ATTACHES at >=90 min — only
the evidence window narrowed.

Runs were excluded from both the note and the average entirely, which is exactly
backwards: the same rule states "The open focus is RUN fuelling - prioritise
run-fuelling data and nudge run carbs toward ~60 g/hr." Runs now have their own
window (>=60 min — the duration at which a run needs fuel at all) and their own
target constant, so a run number can never be dragged by bike data or vice versa.

Nothing here compares training intake to a race target: run prescriptions cite
RUN_TARGET_G_HR only, and lib/races.py keeps its own suppression of the
training-vs-race comparison.
"""
from __future__ import annotations

_AGGRESSIVE_STEP = 25   # g/hr per block while below the careful threshold
_CAREFUL_STEP = 5       # g/hr per block at/above it
_CAREFUL_FROM = 60      # g/hr — boundary between aggressive and careful ramp
_MIN_USEFUL = 40        # never prescribe below this for a >90-min session

# Sport sets. Brick stays with the rides (its fuelling is bike fuelling carried
# into a short run) and is deliberately NOT counted as run-fuelling evidence.
RIDE_SPORTS = ("Ride", "GravelRide", "VirtualRide", "Brick")
RUN_SPORTS = ("Run", "TrailRun", "VirtualRun")

LONG_RIDE_MIN = 150     # min — a ride long enough for intake to test the gut
LONG_RUN_MIN = 60       # min — a run long enough to need fuel at all
_MIN_WINDOW_SAMPLE = 3  # below this the narrow window is too thin to average

# Run-fuelling target (g/hr). Jamie's persistent rule sets the run nudge at ~60
# g/hr and separately forbids treating the 90 g/hr RACE figure as a training
# number, so the run ramp tops out here rather than at the race target.
RUN_TARGET_G_HR = 60


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


def run_fuel_target(avg_g_hr, run_target_g_hr=None) -> int:
    """Prescribed carbs (g/hr) for long RUNS. Same ramp, but ceilinged at the run
    target (~60 g/hr) — never at the race-day bike figure.

    With NO run-fuelling data the ramp starts at the useful floor, not at the
    ceiling: 60 g/hr is the number to ramp TO, so handing it to an athlete with no
    logged run intake at all prescribes the destination as the first step."""
    rt = int(run_target_g_hr or RUN_TARGET_G_HR)
    if avg_g_hr is None:
        return min(_MIN_USEFUL, rt)
    return fuel_target(avg_g_hr, rt)


def _avg_g_hr(session_log, sports, min_duration_min, n):
    """Mean carbs/hr over the most recent `n` qualifying sessions. None if none."""
    rated = []
    for e in session_log or []:
        sport = (e.get("sport") or "")
        dur = e.get("duration_min") or 0
        carb = e.get("nutrition_g_carb")
        if sport in sports and dur >= min_duration_min and carb:
            rated.append((e.get("date") or "", carb / dur * 60))
    if not rated:
        return None, 0
    rated.sort(key=lambda x: x[0], reverse=True)
    recent = [r for _, r in rated[:n]]
    return sum(recent) / len(recent), len(rated)


def recent_avg_g_hr(session_log, n: int = 6, sports=RIDE_SPORTS,
                    min_duration_min: int = LONG_RIDE_MIN):
    """Mean carbs-per-hour over the most recent `n` LONG (>=150 min) ride/brick
    sessions with a logged carb total. Returns None if there are none.

    Long-only by default so short easy/taper rides carrying a token chew cannot
    depress the window (see module docstring). If fewer than 3 long rides are
    logged the window widens to >=90 min rather than returning None — for an
    athlete with almost no long-ride history that thin evidence is still better
    than jumping them to the no-data default of 60 g/hr.
    """
    avg, count = _avg_g_hr(session_log, sports, min_duration_min, n)
    if count >= _MIN_WINDOW_SAMPLE:
        return avg
    wide, wide_count = _avg_g_hr(session_log, sports, 90, n)
    return wide if wide_count else avg


def recent_run_avg_g_hr(session_log, n: int = 6):
    """Mean carbs-per-hour over the most recent `n` runs >=60 min with a logged
    carb total. Runs are Jamie's stated open fuelling focus, so they get their own
    window — never blended with bike data in either direction."""
    return _avg_g_hr(session_log, RUN_SPORTS, LONG_RUN_MIN, n)[0]
