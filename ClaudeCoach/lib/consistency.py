"""Consistency over big one-off days (Jamie, 14 Oct 2026: "for someone like Fred at the
moment we need to remind them consistent is the key. Going over power or a longer session
but skipping the next one isn't how to build fitness ... subtly and politely"; and "it's
also important if it's tiredness from lifestyle/sleep or from training, as alcohol, stress
and tiredness can prevent recovery").

Code decides when a line fires and which one, so the model can neither nag nor guess:

  overshoot()   a done session well over its plan (Load or duration). The debrief adds
                one warm line about letting the next session land (activity-watcher).
  assess()      a planned session missed in the last 3 days. The morning card gets ONE
                line, at most once a week, picked by the likely cause:
                  training   an overshoot in the days before the miss
                  lifestyle  no overshoot, but sleep / HRV / resting HR off normal
                  ask        neither: one open question, the athlete's answer goes in notes

Never a list of misses, never numbers about the overshoot, never a telling-off. Alcohol
is only ever part of a general list.
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

LOOKBACK_MISS = 3          # days before today a missed session counts
LOOKBACK_OVER = 4          # days before a miss an overshoot counts as its likely cause
WEEK = 7                   # at most one morning line per athlete per this many days
OVER_LOAD = 1.20           # Load this much over plan...
OVER_LOAD_MIN = 10         # ...and at least this many points
OVER_TIME = 1.30           # or duration this much over plan...
OVER_TIME_MIN_S = 15 * 60  # ...and at least this many seconds

LINES = {
    "training": "Steady weeks build more fitness than big one-off days: hitting each "
                "session as planned is what adds up fastest.",
    "lifestyle": "Recovery comes from more than training: sleep, stress and alcohol all "
                 "affect it, so look after those this week and the sessions will feel better.",
    "ask": "Tired from training, or has the week been busy outside it?",
}

_FAMILY = (("Ride", "ride"), ("Run", "run"), ("Swim", "swim"),
           ("WeightTraining", "strength"), ("Workout", "strength"))


def family(t) -> str:
    t = str(t or "")
    for k, f in _FAMILY:
        if k in t:
            return f
    return t.lower()


def overshoot(plan_ev: dict, act: dict) -> bool:
    """True when a done ride, run or swim ran well over its planned Load or duration.
    Strength running long is not the pattern (Fred's physio sessions often do)."""
    if family(act.get("type")) == "strength":
        return False
    pl, al = plan_ev.get("icu_training_load") or 0, act.get("icu_training_load") or 0
    if pl and al >= pl * OVER_LOAD and al - pl >= OVER_LOAD_MIN:
        return True
    pm, am = plan_ev.get("moving_time") or 0, act.get("moving_time") or 0
    return bool(pm and am >= pm * OVER_TIME and am - pm >= OVER_TIME_MIN_S)


def _workouts(events) -> list:
    return [e for e in events or [] if (e.get("category") or "WORKOUT") == "WORKOUT"
            and e.get("type")]


def _day(x) -> str:
    return str(x.get("start_date_local") or "")[:10]


def matched(ev: dict, acts: list):
    """The activity that did this planned session (same day, same sport family), or None."""
    fam = family(ev.get("type"))
    for a in acts or []:
        if _day(a) == _day(ev) and family(a.get("type")) == fam:
            return a
    return None


def _wellness_off(rows, start: date, end: date, baseline_days: int = 28) -> bool:
    """Any night in [start, end] with sleep 1h+ short and under 7h, or HRV / resting HR
    off the athlete's own normal (lib/wellness_normal.py, as the morning card)."""
    by = {str(r.get("id") or "")[:10]: r for r in rows or []}
    d = start
    while d <= end:
        t = by.get(d.isoformat())
        prev = [by[k] for k in sorted(by) if k < d.isoformat()][-baseline_days:]

        def base(key):
            v = [r[key] for r in prev if r.get(key) is not None]
            return sum(v) / len(v) if len(v) >= baseline_days // 2 else None
        if t:
            sb = base("sleepSecs")
            import wellness_normal as WN            # SD-based, as the morning card
            if WN.off_any(rows, d):
                return True
            if t.get("sleepSecs") is not None and sb:
                sl, s0 = t["sleepSecs"] / 3600, sb / 3600
                if sl <= s0 - 1 and sl < 7:
                    return True
        d += timedelta(days=1)
    return False


def assess(events, acts, wellness_rows, today: date) -> dict | None:
    """{"case", "line", "missed"} when a planned session was missed in the last
    LOOKBACK_MISS days, else None. `events` and `acts` must cover the LOOKBACK_MISS +
    LOOKBACK_OVER days before today."""
    lo = (today - timedelta(days=LOOKBACK_MISS)).isoformat()
    misses = [e for e in _workouts(events)
              if lo <= _day(e) < today.isoformat() and not matched(e, acts)]
    if not misses:
        return None
    first = min(_day(e) for e in misses)
    f = date.fromisoformat(first)
    over_lo = (f - timedelta(days=LOOKBACK_OVER)).isoformat()
    over = any(overshoot(e, a) for e in _workouts(events)
               if over_lo <= _day(e) <= first and (a := matched(e, acts)))   # BEFORE the miss
    if over:
        case = "training"
    elif _wellness_off(wellness_rows, f - timedelta(days=1), today):
        case = "lifestyle"
    else:
        case = "ask"
    return {"case": case, "line": LINES[case], "missed": [e.get("name") for e in misses]}


# ── once a week ─────────────────────────────────────────────────────────────────
def _state_path(adir: Path) -> Path:
    return adir / "consistency-state.json"


def due(adir: Path, today: date) -> bool:
    try:
        last = date.fromisoformat(json.loads(_state_path(adir).read_text())["last"])
        return (today - last).days >= WEEK
    except Exception:
        return True


def mark(adir: Path, today: date, case: str) -> None:
    try:
        _state_path(adir).write_text(json.dumps({"last": today.isoformat(), "case": case}))
    except OSError:
        pass
