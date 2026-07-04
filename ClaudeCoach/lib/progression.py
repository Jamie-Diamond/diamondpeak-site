"""Long-run distance progression cap — rule 96 requires ≤10-15% progression/wk.

Shared by every script that quotes or pushes a long-run distance (morning-checkin,
night-before-brief) so the cap can't be silently skipped depending on which one runs.
Without a concrete number the model has drifted (e.g. 16km vs 15.2km cap on
2026-06-28, and a 19.5km recurrence when night-before-brief had no cap at all).

Baseline always comes from session-log.json (actual completed runs), never from
current-state.json — a stale status note there (e.g. a leftover "BLOCKED" entry)
must never override a real completed run as the progression baseline.
"""
import re

PROGRESSION_FACTOR = 1.125
_KM_RE = re.compile(r"~?\s*(\d+(?:\.\d+)?)\s*km", re.IGNORECASE)


def last_completed_long_run_km(session_log: list) -> float | None:
    """Most recent completed run/trail-run ≥10km from session-log.json, or None."""
    for s in session_log or []:
        if s.get("sport") in ("Run", "TrailRun") and float(s.get("distance_km") or 0) >= 10:
            return float(s["distance_km"])
    return None


def long_run_cap_km(events: list, session_log: list, classify_session_type) -> float | None:
    """Progression-capped distance ceiling for a run_long WORKOUT event, or None if
    there's no long-run event today/tomorrow or no prior qualifying run to base it on."""
    long_run_events = [
        e for e in events
        if (e.get("category") or "WORKOUT").upper() == "WORKOUT"
        and classify_session_type(e.get("type", ""), str(e.get("name") or "")) == "run_long"
    ]
    if not long_run_events:
        return None
    last_km = last_completed_long_run_km(session_log)
    if last_km is None:
        return None
    prog_cap = last_km * PROGRESSION_FACTOR
    m = _KM_RE.search(str(long_run_events[0].get("name") or ""))
    cal_km = float(m.group(1)) if m else None
    return min(prog_cap, cal_km) if cal_km else prog_cap
