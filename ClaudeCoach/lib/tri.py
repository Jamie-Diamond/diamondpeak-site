"""Triathlon: each leg measured from the athlete's own training and races, set against
the split their goal needs (Jamie, 17 Oct 2026: the "What's holding you back" card for
triathlon; validated on Jamie's and James's Cervia 2026).

  SWIM   CSS x the athlete's own open-water factor (race swim pace / CSS, median of their
         past triathlons; SWIM_DEFAULT when they have none - judgement).
  BIKE   No drafting in an age-group triathlon, but the wind still moves a bike split: a
         drag figure fitted on one Cervia ride missed the other by 9-13% (Jamie's 2025 vs
         2026). So the bike leg is anchored on the athlete's OWN past rides on the same
         course: time ~ power^(-1/3) (air drag dominates on the flat), averaged over the
         rides so one windy year doesn't decide it, and the spread is reported. A first
         time on a course: the course profile (reference/races, lib/course.py physics) with
         CDA_DEFAULT, the age-group TT figure (James's own Cervia 2026 ride fitted 0.25), and
         a wider "conditions" caveat. No profile either: no bike time is claimed.
         Power they can hold: FTP x HOLD_IF for the bike's length, in NP; average watts =
         NP / their own variability (VI) from past long races.
  RUN    Open-run potential for the run distance (lib/run_model.py) x the athlete's own
         off-the-bike factor (past tri run / open potential at that date, same distance
         class); RUN_DEFAULT by distance when they have none (judgement).

Tri days (`tri_races`): a day with a Transition logged (every multisport recording has
them). Pure: lib/performance.py gathers, lib/limiters.diagnose_tri judges.
"""
from __future__ import annotations

import math
import statistics


SWIM_DEFAULT = 1.05
SWIM_SANE = (0.90, 1.25)         # outside this the swim course was mis-measured, not the swimmer
CDA_DEFAULT = 0.25
BIKE_KG = 10.0
RUN_DEFAULT = {"sprint": 1.03, "olympic": 1.04, "70_3": 1.06, "ironman": 1.10}
VI_DEFAULT = 1.05
# NP as a share of FTP that can be held for the bike's length in a triathlon (the run
# still to come): standard long-course pacing guidance, mid-band.
HOLD_IF = ((1.5, 0.85), (3.0, 0.80), (5.0, 0.73), (7.0, 0.68), (99, 0.62))
DIST = {"sprint": (0.75, 20, 5), "olympic": (1.5, 40, 10), "70_3": (1.9, 90, 21.0975),
        "ironman": (3.8, 180, 42.195)}


def kind_for_bike_km(km: float) -> str:
    return "sprint" if km < 30 else "olympic" if km < 60 else "70_3" if km < 130 else "ironman"


def hold_if(hours: float) -> float:
    return next(v for h, v in HOLD_IF if hours <= h)


def tri_races(acts: list) -> list:
    """[{date, kind, swim: {dist_m, time_s}, bike: {id, dist_m, time_s, avg_w, np_w}, run: {dist_m,
    time_s}}] oldest first, for each day with a Transition. Duplicate rides (a race file and
    its copy) collapse to the one with power, else the longest."""
    by_day = {}
    for a in acts:
        by_day.setdefault(str(a.get("start_date_local"))[:10], []).append(a)
    out = []
    for day, xs in sorted(by_day.items()):
        if not any(a.get("type") == "Transition" for a in xs):
            continue
        swims = [a for a in xs if "Swim" in str(a.get("type"))]
        rides = [a for a in xs if "Ride" in str(a.get("type"))]
        runs = [a for a in xs if a.get("type") == "Run"]
        if not (swims and rides and runs):
            continue
        ride = max(rides, key=lambda a: (bool(a.get("icu_average_watts")), a.get("distance") or 0))
        swim = max(swims, key=lambda a: a.get("distance") or 0)
        run = max(runs, key=lambda a: a.get("distance") or 0)
        kind = kind_for_bike_km((ride.get("distance") or 0) / 1000)
        t_s = sum((a.get("elapsed_time") or a.get("moving_time") or 0) for a in xs if a.get("type") == "Transition")
        out.append({"date": day, "kind": kind, "t_s": t_s or None,
                    "swim": {"dist_m": DIST[kind][0] * 1000, "time_s": swim.get("moving_time") or 0},
                    "bike": {"id": ride.get("id"), "dist_m": ride.get("distance") or 0,
                             "time_s": ride.get("moving_time") or 0, "avg_w": ride.get("icu_average_watts"),
                             "np_w": ride.get("icu_weighted_avg_watts")},
                    "run": {"dist_m": DIST[kind][2] * 1000, "time_s": run.get("moving_time") or 0}})
    return out


def swim_factor(races: list, css_s: float | None) -> tuple:
    """(factor, n): race swim pace per 100 m / CSS, median over their triathlons. Official
    distances, not the GPS (a sighting line wanders)."""
    if not css_s:
        return SWIM_DEFAULT, 0
    fs = [r["swim"]["time_s"] / (r["swim"]["dist_m"] / 100) / css_s for r in races if r["swim"]["time_s"]]
    fs = [f for f in fs if SWIM_SANE[0] <= f <= SWIM_SANE[1]]
    return (round(statistics.median(fs), 3), len(fs)) if fs else (SWIM_DEFAULT, 0)


def variability(races: list) -> float:
    vs = [r["bike"]["np_w"] / r["bike"]["avg_w"] for r in races
          if r["bike"].get("np_w") and r["bike"].get("avg_w") and r["bike"]["time_s"] >= 2 * 3600]
    return round(statistics.median(vs), 3) if vs else VI_DEFAULT


def bike_anchors(races: list, dates: list) -> list:
    """(avg_w, time_s) for past race rides on the same course: the dates of earlier
    editions of the race (from the athlete's race list)."""
    keep = set(dates)
    return [(r["bike"]["avg_w"], r["bike"]["time_s"]) for r in races
            if r["date"] in keep and r["bike"].get("avg_w") and r["bike"]["time_s"]]


def bike_time(avg_w: float, anchors: list) -> float | None:
    """Seconds at avg_w on the course: each anchor scaled by t ~ P^(-1/3), geometric mean."""
    if not anchors or not avg_w:
        return None
    logs = [math.log(t * (p / avg_w) ** (1 / 3)) for p, t in anchors]
    return math.exp(sum(logs) / len(logs))


def bike_time_physics(avg_w: float, ele: list, rider_kg: float, cda: float = CDA_DEFAULT) -> float | None:
    """Seconds at a steady avg_w over a 100 m profile, solo (lib/course.speed)."""
    import course as cs
    if not ele or not avg_w:
        return None
    return sum(100.0 / cs.speed(avg_w, (ele[i + 1] - ele[i]) / 100.0, rider_kg + BIKE_KG, cda)
               for i in range(len(ele) - 1))


def bike_power_physics(time_s: float, ele: list, rider_kg: float, cda: float = CDA_DEFAULT) -> float | None:
    lo, hi = 50.0, 600.0
    if not ele or not time_s:
        return None
    for _ in range(40):
        p = (lo + hi) / 2
        if bike_time_physics(p, ele, rider_kg, cda) > time_s:
            lo = p
        else:
            hi = p
    return hi


def bike_power_for(time_s: float, anchors: list) -> float | None:
    """Average watts the course needs for a bike split of time_s (inverse of bike_time)."""
    if not anchors or not time_s:
        return None
    logs = [math.log(p * (t / time_s) ** 3) for p, t in anchors]
    return math.exp(sum(logs) / len(logs))


def off_bike_factor(races: list, kind: str, open_s) -> tuple:
    """(factor, n): tri run time / open potential for that run distance on that date.
    `open_s(dist_m, date_iso)` gives the open-run potential (run_model) or None."""
    fs = []
    for r in races:
        if r["kind"] != kind or not r["run"]["time_s"]:
            continue
        o = open_s(r["run"]["dist_m"], r["date"])
        if o:
            fs.append(r["run"]["time_s"] / o)
    fs = [f for f in fs if 0.95 <= f <= 1.6]
    return (round(statistics.median(fs), 3), len(fs)) if fs else (RUN_DEFAULT.get(kind, 1.08), 0)


def goal_splits(goal_s: int | None, target: dict | None, last: dict | None) -> dict | None:
    """{swim, bike, run, t} seconds the goal needs: the athlete's own target splits when set,
    else the goal time shared out like their last race of the kind (transitions as raced)."""
    if target and all(target.get(k) for k in ("swim_min", "bike_min", "run_min")):
        return {"swim": target["swim_min"] * 60, "bike": target["bike_min"] * 60,
                "run": target["run_min"] * 60, "t": (target.get("t1t2_min") or 0) * 60}
    if not goal_s or not last:
        return None
    legs = {"swim": last["swim"]["time_s"], "bike": last["bike"]["time_s"], "run": last["run"]["time_s"]}
    t = last.get("t_s") or 600
    scale = (goal_s - t) / sum(legs.values())
    return {k: v * scale for k, v in legs.items()} | {"t": t}


def _hm(s) -> str:
    s = int(round(s))
    return f"{s // 3600}:{s % 3600 // 60:02d}"


def _mmss(s) -> str:
    s = int(round(s))
    return f"{s // 60}:{s % 60:02d}"


def legs(kind: str, tri_block: dict, run_block: dict | None, bike_block: dict | None,
         course_ele: list | None, anchor_dates: list, today, bike_hours: float | None = None) -> dict:
    """{"swim" | "bike" | "run": {"time_s", "you", "source"}} predicted for a race of `kind`
    from the athlete's measures (lib/performance.py blocks). A leg with nothing to go on is
    left out rather than guessed."""
    import run_model as rm
    out = {}
    swim_km, bike_km, run_km = DIST[kind]
    races = (tri_block or {}).get("races") or []
    css = (tri_block or {}).get("css_s")
    if css:
        f, n = (tri_block.get("swim_factor") or [SWIM_DEFAULT, 0])
        t = swim_km * 10 * css * f
        out["swim"] = {"time_s": t, "you": f"CSS {_mmss(css)} × {f:.2f} open water ≈ {_hm(t)}",
                       "source": f"{n} race swims" if n else "typical open-water gap (no race swims yet)"}
    ftp, kg = (bike_block or {}).get("ftp"), (bike_block or {}).get("weight")
    if ftp:
        hours = bike_hours or bike_km / 32.0
        i = hold_if(hours)
        np_w = ftp * i
        avg = np_w / ((tri_block or {}).get("vi") or VI_DEFAULT)
        anchors = bike_anchors(races, anchor_dates)
        t = bike_time(avg, anchors) if anchors else (bike_time_physics(avg, course_ele, kg) if course_ele and kg else None)
        if t:
            how = (f"your {len(anchors)} past ride{'s' if len(anchors) > 1 else ''} here" if anchors
                   else "the course and a typical age-group position")
            out["bike"] = {"time_s": t, "you": f"{ftp} W FTP → ~{np_w:.0f} W NP (IF {i:.2f}) ≈ {_hm(t)}",
                           "source": how}
    run = run_block or {}
    p = rm.predict(run_km * 1000, run.get("races") or [], today, run.get("weeklyKm") or 0.0,
                   run.get("training"), run.get("recentBreakDays"))
    if p:
        f, n = ((tri_block or {}).get("off_bike") or {}).get(kind) or [RUN_DEFAULT.get(kind, 1.08), 0]
        t = p["now_s"] * f
        out["run"] = {"time_s": t, "you": f"open {_hm(p['now_s'])} × {f:.2f} off the bike ≈ {_hm(t)}",
                      "source": f"{n} past {kind.replace('_', '.')} runs" if n else "typical off-the-bike slowing"}
    return out
