"""One drift figure for every place Peak and the coach use it (Jamie, 17 Oct 2026: "make sure
drift uses a good drift metric, takes out hills, freewheeling etc").

Drift (aerobic decoupling) = how much output per heartbeat fell from the first half of the
steady part of a session to the second: positive = HR drifted up against output. Under 5% is
the usual "aerobically sound" mark.

What is taken out, and why:
  warm-up, cool-down  Intervals.icu's detected warm-up / cool-down, else the first WARMUP_S and
                      last COOLDOWN_S (lib/activity_stats): HR lags at the start.
  freewheeling        ride samples at 0 W are dropped: a descent at no power with HR easing
                      would otherwise read as "free" output.
  stops               run samples under STOP_MS are dropped (lights, a gate, a walk break).
  hills               rides: power against HR - a climb raises power and HR together, so it
                      doesn't read as drift. Runs: power against HR when the run carries power;
                      without it, GRADE-ADJUSTED speed (Minetti 2002 cost of the gradient), so
                      a hill doesn't read as slowing down.
  intervals           only steady sessions: pedalling VI (power while pedalling) above MAX_VI
                      means the two halves held different work, and the figure would describe
                      the plan rather than the athlete. Hilly endurance rides sit under it.
  bad heart rate      sessions the HR check (lib/hr_quality) failed give no figure.
Needs MIN_STEADY_S of steady samples after the cuts (lib/activity_stats.decoupling).
"""
from __future__ import annotations

import activity_stats as ast_

MAX_VI = 1.15
VERSION = 1                  # bump when the method changes: cached figures are worked out again
GRADE_WINDOW = 30            # seconds either side for the gradient on a run without power


def _streams(raw) -> dict:
    if isinstance(raw, dict):
        return raw
    return ast_._streams(raw)


def _grade_adjusted(vel: list, alt: list, dist: list) -> list:
    """Speed x cost(gradient) / cost(flat), the gradient over +-GRADE_WINDOW seconds."""
    n = min(len(vel), len(alt), len(dist))
    out = []
    for i in range(n):
        a, b = max(0, i - GRADE_WINDOW), min(n - 1, i + GRADE_WINDOW)
        dd = (dist[b] or 0) - (dist[a] or 0)
        g = ((alt[b] or 0) - (alt[a] or 0)) / dd if dd and dd > 5 else 0.0
        v = vel[i]
        out.append(v * ast_._minetti(g) / 3.6 if isinstance(v, (int, float)) else None)
    return out


def session_drift(act: dict, raw_streams, hr_trusted: bool = True) -> dict:
    """{"pct": float | None, "method": str | None, "reason": str | None} for one ride or run."""
    if not hr_trusted:
        return {"pct": None, "method": None, "reason": "heart rate failed the check"}
    st = _streams(raw_streams)
    sport = str(act.get("type") or "")
    ride, run = "Ride" in sport, "Run" in sport
    hr = st.get("heartrate") or []
    if not (ride or run) or not hr:
        return {"pct": None, "method": None, "reason": "no heart rate"}
    watts = [w for w in (st.get("watts") or [])]
    has_power = any(isinstance(w, (int, float)) and w > 0 for w in watts)
    if ride:
        if not has_power:
            return {"pct": None, "method": None, "reason": "no power"}
        np_ = act.get("icu_weighted_avg_watts")
        if not np_:                         # no NP from Intervals: work it out, so the check still holds
            import np_curve
            np_ = np_curve.np_for_ride(watts)
        ped = [w for w in watts if isinstance(w, (int, float)) and w > 0]
        vi = np_ / (sum(ped) / len(ped)) if np_ and ped else None
        if vi and vi > MAX_VI:
            return {"pct": None, "method": None, "reason": "intervals or surges (power too variable)"}
        pct = ast_.decoupling(watts, hr, act, True)
        method = "power:HR"
    elif has_power:
        pct = ast_.decoupling(watts, hr, act, True)       # ride=True: drop 0 W (stopped)
        method = "power:HR"
    else:
        vel = st.get("velocity_smooth") or []
        alt, dist = st.get("fixed_altitude") or st.get("altitude") or [], st.get("distance") or []
        out = _grade_adjusted(vel, alt, dist) if alt and dist else vel
        pct = ast_.decoupling(out, hr, act, False)
        method = "grade-adjusted pace:HR" if alt and dist else "pace:HR"
    if pct is None:
        return {"pct": None, "method": method, "reason": "under 30 minutes steady after the cuts"}
    return {"pct": round(pct, 1), "method": method, "reason": None}
