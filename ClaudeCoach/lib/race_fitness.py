#!/usr/bin/env python3
"""Race fitness requirements and race-priority rules — one source (Jamie, 29 Sep 2026).

Three things the blueprint (blueprints/blueprint.md §2.1, §4.4, §4.5) states and the
planner applies, kept here so the document and the code cannot drift:

1. RUN RACE FITNESS is HYBRID. A run race is judged on two floors, each on its own:
   RUNNING Fitness (the 42-day EWMA of run load only) against the race's range, and
   TOTAL Fitness against the athlete's own total floor. A triathlete can be far above
   a 5k's needs on total and still under it on running (Jamie, 29 Sep: total 95,
   running 32), so neither number alone answers "is this athlete ready".

2. A / B / C races. A = the block is built around it and gets the event's full taper;
   B = a few easier days either side and a lighter race week; C = train through, an
   easy day either side.

3. FITTER THAN THE GOAL is the ATHLETE's call. When current Fitness sits above what
   the goal needs, the coach asks and records one of SURPLUS_CHOICES; nothing is
   decided for them. Until they answer, the plan holds.

Pure: no IO except `run_ctl`, which only walks the activity list it is given.
"""
from __future__ import annotations

from datetime import date, timedelta

# Session-library event keys that are single-sport run races.
RUN_EVENTS = ("5k", "10k", "half_marathon", "marathon")

# RUNNING Fitness on race day (taper entry) for a strong amateur. Estimates, not
# physiology constants: 5k/10k are speed-limited, the marathon is volume-limited.
RUN_FITNESS_RACE_DAY = {
    "5k": (35, 50),
    "10k": (40, 55),
    "half_marathon": (45, 60),
    "marathon": (55, 75),
}
# Entry to each phase as a fraction of the race-day range (same shape as the Ironman
# table in generate-blueprint CTL_TARGETS: each phase starts higher than the last).
PHASE_FRACTION = {"base": 0.70, "build": 0.80, "specific": 0.90, "peak": 0.95,
                  "taper": 1.00}

# A-race taper, in days, per event (run events; triathlon tapers live in §4.1/4.2).
TAPER_DAYS = {"5k": (5, 7), "10k": (5, 7), "half_marathon": (7, 10),
              "marathon": (14, 21)}

# Easy days AFTER an A-race, per event.
A_RECOVERY_DAYS = {"ironman": 21, "70_3": 10, "marathon": 10, "half_marathon": 7,
                   "10k": 3, "5k": 3}

# B and C races. Easy days are days with no hard work (nothing >10 min at >= 0.90 IF).
PRIORITY_RULES = {
    "B": {"race_week_factor": 0.65, "easy_days_before": 3, "easy_days_after": 3},
    "C": {"race_week_factor": None, "easy_days_before": 1, "easy_days_after": 1},
}

# How the athlete wants to handle being fitter than the goal needs.
SURPLUS_CHOICES = {
    "hold_shift": "hold total Fitness and shift the mix toward the goal sport "
                  "(e.g. more running, let swimming fade)",
    "drift": "let total Fitness drift down to their floor and put the freed time "
             "into speed",
    "raise_goal": "raise the goal",
}


def run_event_key(text: str):
    """'Marathon', 'Half Marathon', '10k', '5k' (free text) -> a RUN_EVENTS key, or None.
    Same keyword order as session_library.event_key: half before marathon, 10k before 5k."""
    s = str(text or "").lower()
    if "iron" in s or "70.3" in s or "tri" in s:
        return None
    if "half mara" in s or "half-mara" in s or "21.1" in s:
        return "half_marathon"
    if "marathon" in s:
        return "marathon"
    if "10k" in s or "10 k" in s:
        return "10k"
    if ("5k" in s or "5 k" in s) and "swim" not in s:
        return "5k"
    return None


def run_fitness_range(event: str, phase: str = "taper"):
    """(lo, hi) RUNNING Fitness to enter `phase` for a run race, or None."""
    rng = RUN_FITNESS_RACE_DAY.get(event)
    f = PHASE_FRACTION.get((phase or "taper").lower())
    if not rng or f is None:
        return None
    return (round(rng[0] * f), round(rng[1] * f))


def total_floor(cfg: dict):
    """The athlete's own TOTAL Fitness floor, or None.

    Set per athlete (ctl_targets.total_fitness_floor). Falls back to the bottom of
    their maintenance band, the number they already said they want to stay above.
    """
    ct = (cfg or {}).get("ctl_targets") or {}
    v = ct.get("total_fitness_floor")
    if v is None:
        band = ct.get("maintenance_ctl_band")
        try:
            v = band[0] if band and float(band[0]) < float(band[1]) else None
        except (TypeError, ValueError, IndexError):
            v = None
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def surplus_choice(cfg: dict):
    """The athlete's recorded answer to "you're fitter than the goal needs", or None."""
    c = (cfg or {}).get("fitness_surplus_choice")
    return c if c in SURPLUS_CHOICES else None


def running_status(event: str, phase: str, run_ctl):
    """{"ctl", "range", "status"} for a run race; status is under / in / over."""
    rng = run_fitness_range(event, phase)
    if rng is None or run_ctl is None:
        return None
    s = "under" if run_ctl < rng[0] else ("over" if run_ctl > rng[1] else "in")
    return {"ctl": round(float(run_ctl), 1), "range": list(rng), "status": s}


def run_ctl(activities, today: date, days: int = 180) -> float:
    """RUNNING Fitness: 42-day EWMA of run load, the same form as the dashboard's
    per-sport series (refresh-site-data._compute_per_sport_ctl)."""
    daily = {}
    for a in activities or []:
        if "run" not in str(a.get("type") or "").lower():
            continue
        d = str(a.get("start_date_local") or "")[:10]
        if d:
            daily[d] = daily.get(d, 0.0) + float(a.get("icu_training_load") or 0)
    ctl, cur = 0.0, today - timedelta(days=days)
    while cur <= today:
        ctl += (daily.get(cur.isoformat(), 0.0) - ctl) / 42.0
        cur += timedelta(days=1)
    return round(ctl, 1)


def easy_dates(races, week_start: date) -> dict:
    """{iso date: reason} for the days in the week of `week_start` that must carry no
    hard work because of a B or C race (booked PB attempt or registry race) nearby.

    `races` = [{"date", "priority", "name"}]. Races in the neighbouring weeks count,
    so the recovery days after a Saturday race land in the next week's plan.
    """
    ws = week_start - timedelta(days=week_start.weekday())
    we = ws + timedelta(days=6)
    out = {}
    for r in races or []:
        rule = PRIORITY_RULES.get(str(r.get("priority") or "C").upper())
        try:
            d = date.fromisoformat(str(r.get("date"))[:10])
        except ValueError:
            continue
        if not rule:
            continue
        name = r.get("name") or "race"
        for n in range(1, rule["easy_days_before"] + 1):
            x = d - timedelta(days=n)
            if ws <= x <= we:
                out.setdefault(x.isoformat(), f"{n} day(s) before {name}")
        for n in range(1, rule["easy_days_after"] + 1):
            x = d + timedelta(days=n)
            if ws <= x <= we:
                out.setdefault(x.isoformat(), f"{n} day(s) after {name}")
    return dict(sorted(out.items()))
