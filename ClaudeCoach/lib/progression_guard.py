"""Double-increase progression guard (bike interval work).

Flags a proposed interval session that raises BOTH the intensity band and the
per-rep duration beyond what the athlete has actually completed. Either one alone
is normal progression; both at once is the step that produced Kathryn's 30 Jul
"Threshold 2x20 @ 95-102%" — a 20-minute rep at a band 5pp above her demonstrated
ceiling, when her longest rep anywhere near that band was 10 minutes.

ADVISORY ONLY. This returns a warning string for the prescription card and the
readiness log; it never rewrites a plan. Per the standing rule that warnings are
not hard rules, only safety ceilings block.

Rep structure comes from the ICU planned-event description ("- 20m 95-102%"),
filtered to events with a `paired_activity_id` (i.e. actually completed) — a
prescribed-but-missed session is not evidence of demonstrated capacity.
"""

import re

WINDOW_DAYS = 56          # ~8 weeks: long enough to hold a full build block
WORK_REP_FLOOR_PCT = 80   # below this a segment is warm-up/recovery, not work
BAND_TOLERANCE_PCT = 10   # "comparable band" = within this many points below

# Margins added 2026-07-30 after the guard's only real-world positive turned out to be
# a FALSE positive. It flagged Kathryn's 30 Jul 2x20 @ 95-102% as a double increase; she
# completed both reps at 94-97% FTP. Bare "any increase on both axes" fires on ambitious
# progressions as well as reckless ones, which is not useful — a coach who is told off
# for a step the athlete then nails will stop reading the warnings. A step must now
# CLEAR these margins, so 15min@94% -> 20min@102% (band +5, duration 1.33x) passes,
# while 10min@90% -> 25min@105% (band +15, duration 2.5x) still fires.
BAND_JUMP_PCT = 5             # band must exceed the demonstrated ceiling by MORE than this
DURATION_JUMP_FACTOR = 1.34   # ...and rep duration exceed the comparable best by this much

# "- 20m 95-102%" → (minutes, low, high). Negative lookahead drops pace targets,
# which are percentages of threshold PACE (higher = slower) and not comparable.
_REP_RE = re.compile(r"(\d+)\s*m\s+(\d{2,3})\s*-\s*(\d{2,3})\s*%(?!\s*pace)",
                     re.IGNORECASE)


def parse_work_reps(description: str) -> list[tuple[int, int]]:
    """[(minutes, top_of_band_pct)] for work segments only."""
    out = []
    for mins, _lo, hi in _REP_RE.findall(description or ""):
        if int(hi) >= WORK_REP_FLOOR_PCT:
            out.append((int(mins), int(hi)))
    return out


def _is_bike(event: dict) -> bool:
    return "ride" in (event.get("type") or "").lower()


def completed_history(events: list) -> list[tuple[int, int]]:
    """Work reps from COMPLETED bike events. Unpaired events are not evidence."""
    hist = []
    for e in events or []:
        if _is_bike(e) and e.get("paired_activity_id"):
            hist.extend(parse_work_reps(e.get("description")))
    return hist


def check(proposed_reps: list[tuple[int, int]],
          history: list[tuple[int, int]]) -> str | None:
    """Warning string if the proposal is a double increase, else None.

    Fires only when BOTH hold:
      1. the proposed band exceeds every band completed in the window, AND
      2. the proposed rep is longer than the longest rep completed at a
         comparable band (within BAND_TOLERANCE_PCT below the proposal).
    """
    if not proposed_reps or not history:
        return None

    prop_min, prop_band = max(proposed_reps, key=lambda r: (r[1], r[0]))
    hist_max_band = max(band for _m, band in history)
    if prop_band <= hist_max_band + BAND_JUMP_PCT:
        return None                     # same band, or a step within normal progression

    floor = prop_band - BAND_TOLERANCE_PCT
    comparable = [m for m, band in history if band >= floor]
    longest_comparable = max(comparable) if comparable else 0
    if prop_min <= longest_comparable * DURATION_JUMP_FACTOR:
        return None                     # duration step is within normal progression

    return (
        f"PROGRESSION FLAG (advisory): proposed {prop_min}min reps at {prop_band}% FTP "
        f"raise intensity AND rep duration WELL beyond demonstrated capacity in one step. "
        f"Demonstrated ceiling in the "
        f"last {WINDOW_DAYS} days is {hist_max_band}% FTP, and the longest completed rep "
        f"at {floor}% or above is {longest_comparable}min. Advance one variable at a "
        f"time: either {prop_min}min at {hist_max_band}%, or "
        f"{longest_comparable or prop_min}min at {prop_band}%."
    )


def check_event(proposed_event: dict, history_events: list) -> str | None:
    """Convenience wrapper over raw ICU event dicts."""
    if not _is_bike(proposed_event):
        return None
    prop_id = proposed_event.get("id")
    history = completed_history(
        [e for e in (history_events or []) if e.get("id") != prop_id]
    )
    return check(parse_work_reps(proposed_event.get("description")), history)
