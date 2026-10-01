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

4. EVERY EVENT HAS FOUR LEVELS (Jamie, 30 Sep 2026: "90k a week is a crazy target for
   most people ... make sure we work for a range of fitnesses"). config/event-levels.json
   holds, per event and level, the peak weekly volume, longest session, taper and the
   Fitness to carry into the taper. The level comes from the athlete's goal time, else
   the time their run threshold predicts, else their current Fitness (`athlete_level`).

Pure except `_levels()` (reads config/event-levels.json once) and `run_ctl`, which only
walks the activity list it is given.
"""
from __future__ import annotations

import json
import re
from datetime import date, timedelta
from functools import lru_cache
from pathlib import Path

LEVELS_FILE = Path(__file__).resolve().parent.parent / "config" / "event-levels.json"
DEFAULT_LEVEL = 3          # when nothing says otherwise: a steady, sustainable level

# Session-library event keys that are single-sport run races.
RUN_EVENTS = ("5k", "10k", "half_marathon", "marathon")

# Race distances (km) for the threshold-based time prediction of run races.
RUN_DISTANCE_KM = {"5k": 5.0, "10k": 10.0, "half_marathon": 21.0975, "marathon": 42.195}
# Entry to each phase as a fraction of the race-day range (same shape as the Ironman
# table in generate-blueprint CTL_TARGETS: each phase starts higher than the last).
PHASE_FRACTION = {"base": 0.70, "build": 0.80, "specific": 0.90, "peak": 0.95,
                  "taper": 1.00}



@lru_cache(maxsize=1)
def _levels() -> dict:
    return json.loads(LEVELS_FILE.read_text())["events"]


def events() -> list:
    """Every event key the level tables cover."""
    return list(_levels())


def _ev(event):
    """An event definition: a bespoke dict as-is, else the table entry for a key/name."""
    if isinstance(event, dict):
        return event
    return _levels().get(levels_key(event) or "")


def event_def(cfg: dict | None = None, event=None):
    """The athlete's event: their BESPOKE event (a blended temporary blueprint for a
    non-standard race, see `bespoke_event`) when one is set for the current race, else
    the standard table entry for `event`."""
    b = (cfg or {}).get("bespoke_event")
    if isinstance(b, dict) and b.get("levels") and (
            not b.get("race_name") or b["race_name"] == (cfg or {}).get("race_name")):
        return b
    return _ev(event)


def taper_days(event):
    """(min, max) A-race taper days for an event, or None."""
    ev = _ev(event)
    return tuple(ev["taper_days"]) if ev else None

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


def levels_key(event: str):
    """Any event name or session-library key -> an event-levels key, or None.
    'Brighton Marathon' -> marathon, '70.3 Emilia' -> 70_3, 'Gravel Race' -> gravel."""
    s = str(event or "").strip().lower()
    if s in _levels():
        return s
    if "70.3" in s or "70_3" in s or "half iron" in s or "half-iron" in s:
        return "70_3"
    if "ironman" in s or "140.6" in s or re.search(r"\bim\b", s) or ("full" in s and "iron" in s):
        return "ironman"
    if "olympic" in s or "standard distance" in s:
        return "olympic"
    if "sprint" in s:
        return "sprint"
    if "gravel" in s:
        return "gravel"
    if "sportive" in s or "fondo" in s or "cyclosportive" in s:
        return "sportive"
    return run_event_key(s)


def level_table(event) -> list:
    ev = _ev(event)
    return list(ev["levels"]) if ev else []


def event_kind(event):
    """'run' | 'tri' | 'bike' — which Fitness the level's numbers are in."""
    ev = _ev(event)
    return ev["kind"] if ev else None


def _race_km(event):
    """Run race distance in km (standard or bespoke), or None."""
    ev = _ev(event)
    if ev and ev.get("distance_km"):
        return float(ev["distance_km"])
    return RUN_DISTANCE_KM.get(levels_key(event) or "") if not isinstance(event, dict) else None


def parse_goal_s(text, event: str):
    """A goal like 'Sub 3:15', '3:15:00', 'sub 3', 'sub 40' or '19:30' -> seconds, or None.

    One bare number is HOURS for long events and MINUTES for a 5k/10k ('sub 40'). Two
    colon parts are MM:SS for a 5k/10k and H:MM for everything else.
    """
    s = str(text or "").lower()
    if not s or "finish" in s:
        return None
    m = re.search(r"(\d{1,2})(?::(\d{2}))?(?::(\d{2}))?", s)
    if not m:
        return None
    a, b, c = m.group(1), m.group(2), m.group(3)
    km = _race_km(event)
    short = km is not None and km < 15          # a 5k/10k goal is in minutes
    if c is not None:
        return int(a) * 3600 + int(b) * 60 + int(c)
    if b is not None:
        return int(a) * 60 + int(b) if short else int(a) * 3600 + int(b) * 60
    return int(a) * 60 if short else int(a) * 3600


def predicted_run_time_s(event: str, threshold_pace):
    """Race time a run threshold pace predicts (Riegel, exponent 1.06), or None.

    Threshold pace ~ the pace held for an hour, so the hour's distance is the anchor."""
    km = _race_km(event)
    try:
        mm, ss = str(threshold_pace).split(":")[:2]
        pace_s = int(mm) * 60 + int(ss)
    except (ValueError, AttributeError):
        return None
    if not km or pace_s <= 0:
        return None
    hour_km = 3600.0 / pace_s
    return int(round(3600.0 * (km / hour_km) ** 1.06))


def level_for_time(event: str, seconds):
    """The level whose goal band contains `seconds`, or None."""
    rows = level_table(event)
    if seconds is None or not rows or all(r["goal_max_s"] is None for r in rows):
        return None
    for r in rows:
        if r["goal_max_s"] is None or seconds <= r["goal_max_s"]:
            return r
    return rows[-1]


def level_for_fitness(event: str, fitness):
    """The level whose taper Fitness band sits closest to `fitness`, or None."""
    rows = level_table(event)
    if fitness is None or not rows:
        return None
    return min(rows, key=lambda r: abs((r["fitness_at_taper"][0] + r["fitness_at_taper"][1])
                                       / 2 - float(fitness)))


def athlete_goal(cfg: dict, profile: dict | None = None):
    """The goal for the athlete's CURRENT race: athletes.json `race_goal`, else the
    A-race registry entry's `goal`, else profile a_goal ONLY if the profile is about the
    same race (a stale 'Sub 9:30' Ironman goal must not level a marathon)."""
    cfg, profile = cfg or {}, profile or {}
    if cfg.get("race_goal"):
        return cfg["race_goal"]
    a = next((r for r in (cfg.get("races") or [])
              if str(r.get("priority") or "").upper() == "A"
              and r.get("date") == cfg.get("race_date")), None)
    if a and a.get("goal"):
        return a["goal"]
    same = (str(profile.get("race_name") or "").strip().lower()
            == str(cfg.get("race_name") or "").strip().lower())
    return profile.get("a_goal") if same else None


def athlete_level(cfg: dict, profile: dict | None, event, fitness=None) -> dict | None:
    """{level row..., "source"} for this athlete and event.

    Goal time first; then an explicitly stated `event_level` (1-4, how sportive / gravel
    riders say it); then the time their run threshold predicts (run races); then the
    level nearest their current Fitness (running Fitness for run races, total otherwise);
    else DEFAULT_LEVEL. `source` says which, so a guessed level is never presented as
    the athlete's own."""
    event = event_def(cfg, event)
    rows = level_table(event)
    if not rows:
        return None
    goal = athlete_goal(cfg, profile)
    r = level_for_time(event, parse_goal_s(goal, event))
    if r:
        return dict(r, source="goal", goal=goal)
    stated = (cfg or {}).get("event_level")
    if stated in (1, 2, 3, 4):
        return dict(rows[stated - 1], source="stated")
    if event_kind(event) == "run":
        t = predicted_run_time_s(event, (profile or {}).get("run_threshold_pace_per_km"))
        r = level_for_time(event, t)
        if r:
            return dict(r, source="threshold", predicted_s=t)
    r = level_for_fitness(event, fitness)
    if r:
        return dict(r, source="current_fitness")
    return dict(rows[DEFAULT_LEVEL - 1], source="default")


def fitness_range(event, level: int, phase: str = "taper"):
    """(lo, hi) Fitness to ENTER `phase` at this level: running Fitness for a run race,
    total for a triathlon or bike event (`event_kind`). None if unknown."""
    rows = level_table(event)
    f = PHASE_FRACTION.get((phase or "taper").lower())
    if not rows or f is None or not (1 <= int(level) <= len(rows)):
        return None
    lo, hi = rows[int(level) - 1]["fitness_at_taper"]
    return (round(lo * f), round(hi * f))


def run_fitness_range(event, phase: str = "taper", level: int = 2):
    """(lo, hi) RUNNING Fitness to enter `phase` for a run race at `level`, or None."""
    if event_kind(event) != "run":
        return None
    return fitness_range(event, level, phase)


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


# CYCLING COUNTS, A LITTLE (Jamie, 30 Sep 2026: "cycling is a great way to boost
# fitness and reduce intensity and deserves some benefit"). Cycling builds the engine but
# not running economy (cross-training studies: 70-90% of the aerobic effect, no economy
# gain), so HALF of cycling Fitness counts toward a run race's running Fitness, and at
# least half of the range's floor must come from running itself. Raised from 1/3 and 3/4
# on 1 Oct 2026, calibrated on Jamie's IM Italy run: 3:37 after a 178 km bike (~2:55-3:05
# open) on running 40 / cycling 58 at peak; 1/3 + 3/4 called that short of even 3:00-3:30.
# One athlete: re-check once testers race.
BIKE_CREDIT = 1 / 2
MIN_RUN_SHARE = 0.5


def running_status(event, phase: str, run_ctl, level: int = 2, bike_ctl=None):
    """{"ctl", "bike_credit", "effective", "min_running", "range", "status"} for a run
    race. effective = running + BIKE_CREDIT x cycling Fitness; status is `under` when
    effective is below the range OR running alone is under MIN_RUN_SHARE of its floor,
    `over` above the range, else `in`."""
    rng = run_fitness_range(event, phase, level)
    if rng is None or run_ctl is None:
        return None
    credit = round(float(bike_ctl or 0) * BIKE_CREDIT, 1)
    eff = round(float(run_ctl) + credit, 1)
    min_run = round(rng[0] * MIN_RUN_SHARE, 1)
    s = ("under" if eff < rng[0] or float(run_ctl) < min_run
         else ("over" if eff > rng[1] else "in"))
    return {"ctl": round(float(run_ctl), 1), "bike_credit": credit, "effective": eff,
            "min_running": min_run, "range": list(rng), "status": s}


def sport_ctl(activities, today: date, sport: str = "run", days: int = 180) -> float:
    """One sport's Fitness: 42-day EWMA of that sport's load ('run' or 'ride' matched in
    the activity type), the same form as the dashboard's per-sport series."""
    daily = {}
    for a in activities or []:
        if sport not in str(a.get("type") or "").lower():
            continue
        d = str(a.get("start_date_local") or "")[:10]
        if d:
            daily[d] = daily.get(d, 0.0) + float(a.get("icu_training_load") or 0)
    ctl, cur = 0.0, today - timedelta(days=days)
    while cur <= today:
        ctl += (daily.get(cur.isoformat(), 0.0) - ctl) / 42.0
        cur += timedelta(days=1)
    return round(ctl, 1)


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


# ── BESPOKE EVENTS (Jamie, 30 Sep 2026) ──────────────────────────────────────────────
# "If I say I have a 30k race you can just go: that's halfway between marathon and HM,
# so I'll blend it. Or a triathlon with a 4k swim and a 10k bike: adapt and create a
# temp blueprint." A bespoke event is built by BLENDING the two nearest standard events:
#   - a run race by distance (log scale) between 5k / 10k / half / marathon;
#   - a multisport race by its estimated duration between sprint / olympic / 70.3 /
#     Ironman for volume, Fitness and taper, and each sport's intensity split by THAT
#     leg's distance against the same leg of each standard triathlon.
# Beyond the tables (an ultra, a sub-5k) the nearest table is used and the result says so.
# The blend is stored per athlete as `bespoke_event` (plan_tools.py bespoke-event) and
# read wherever the standard table would be (event_def).

TRI_LEGS_KM = {"sprint": (0.75, 20.0, 5.0), "olympic": (1.5, 40.0, 10.0),
               "70_3": (1.9, 90.0, 21.0975), "ironman": (3.8, 180.0, 42.195)}
# Mid-level pace, used ONLY to size a multisport event against the standard ones.
_REF_MIN_PER_KM = {"swim": 20.0, "bike": 2.0, "run": 5.5}
_DIST_NAME = {"5k": "5k", "10k": "10k", "half_marathon": "Half Marathon",
              "marathon": "Marathon", "sprint": "Sprint", "olympic": "Olympic",
              "70_3": "70.3", "ironman": "Full Ironman", "sportive": "Sportive",
              "gravel": "Sportive"}
_BAND = re.compile(r"(\d+(?:\.\d+)?)\s*%\s*(Z\s*[1-7](?:\s*[-\u2010-\u2015]\s*[1-7])?)")


@lru_cache(maxsize=1)
def _distributions() -> dict:
    """blueprint.md §3.2, parsed by generate-blueprint's own parser (one source)."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "_gb_dist", Path(__file__).resolve().parent.parent / "scripts" / "generate-blueprint.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m.DISTRIBUTION


def _neighbours(x: float, anchors: list):
    """[(key, value)] sorted by value -> (key_a, key_b, w) on a log scale, clamped."""
    anchors = sorted(anchors, key=lambda a: a[1])
    if x <= anchors[0][1]:
        return anchors[0][0], anchors[0][0], 0.0
    if x >= anchors[-1][1]:
        return anchors[-1][0], anchors[-1][0], 0.0
    import math
    for (ka, a), (kb, b) in zip(anchors, anchors[1:]):
        if a <= x <= b:
            return ka, kb, (math.log(x) - math.log(a)) / (math.log(b) - math.log(a))
    return anchors[-1][0], anchors[-1][0], 0.0


def _mix(a, b, w, nd=0):
    v = a + (b - a) * w
    return round(v, nd) if nd else int(round(v))


def _mix_range(ra, rb, w, nd=0):
    return [_mix(ra[0], rb[0], w, nd), _mix(ra[1], rb[1], w, nd)]


def _mix_row(ra: str, rb: str, w: float) -> str:
    """Weighted blend of two '62% Z1-2 / 15% Z3 / 23% Z4-5' rows (same band labels)."""
    pa, pb = _BAND.findall(ra or ""), _BAND.findall(rb or "")
    if not pa or len(pa) != len(pb):
        return ra or rb
    vals = [int(round(float(x[0]) + (float(y[0]) - float(x[0])) * w)) for x, y in zip(pa, pb)]
    vals[vals.index(max(vals))] += 100 - sum(vals)
    return " / ".join(f"{v}% {lbl}" for v, (_, lbl) in zip(vals, pa))


def _fmt_s(sec: int) -> str:
    h, m = divmod(int(round(sec / 60)), 60)
    return f"{h}:{m:02d}" if h else f"{m} min"


def _labels(goals: list) -> list:
    out, prev = [], None
    for g in goals:
        out.append(f"sub {_fmt_s(g)}" if prev is None and g else
                   (f"{_fmt_s(prev)}–{_fmt_s(g)}" if g else f"{_fmt_s(prev)}+"))
        prev = g
    return out


def _blend_levels(ea: dict, eb: dict, w: float, goal_scale_a: float, goal_scale_b: float,
                  keep: tuple) -> list:
    rows = []
    for la, lb in zip(ea["levels"], eb["levels"]):
        g = None
        if la["goal_max_s"] and lb["goal_max_s"]:
            g = _mix(la["goal_max_s"] * goal_scale_a, lb["goal_max_s"] * goal_scale_b, w)
        row = {"level": la["level"], "goal_max_s": g,
               "fitness_at_taper": _mix_range(la["fitness_at_taper"], lb["fitness_at_taper"], w)}
        for k in keep:
            if k in la and k in lb:
                row[k] = _mix_range(la[k], lb[k], w, nd=2 if k.endswith("_h") or k == "hours" else 0)
        rows.append(row)
    for row, lbl in zip(rows, _labels([r["goal_max_s"] for r in rows])):
        row["label"] = lbl
    return rows


def _blend_distribution(pairs: dict) -> dict:
    """{sport: (dist_key_a, dist_key_b, w)} -> {phase: {Sport: row}} from §3.2."""
    d = _distributions()
    out = {}
    for sport, (ka, kb, w) in pairs.items():
        da, db = d.get(_DIST_NAME[ka], {}), d.get(_DIST_NAME[kb], {})
        for phase in ("base", "build", "specific", "peak", "taper"):
            ra, rb = (da.get(phase) or {}).get(sport), (db.get(phase) or {}).get(sport)
            if ra or rb:
                out.setdefault(phase, {})[sport] = _mix_row(ra, rb, w) if ra and rb else (ra or rb)
    return out


def bespoke_event(name: str, swim_km: float = 0, bike_km: float = 0, run_km: float = 0) -> dict:
    """A temporary blueprint for a non-standard race, blended from the nearest standard
    events. Same shape as an event-levels entry, plus distribution, sports, provenance."""
    swim_km, bike_km, run_km = float(swim_km or 0), float(bike_km or 0), float(run_km or 0)
    legs = {k: v for k, v in (("swim", swim_km), ("bike", bike_km), ("run", run_km)) if v > 0}
    if not legs:
        raise ValueError("a bespoke event needs at least one leg distance")
    L = _levels()
    notes = []
    if list(legs) == ["run"]:
        ka, kb, w = _neighbours(run_km, [(k, RUN_DISTANCE_KM[k]) for k in RUN_EVENTS])
        if ka == kb:
            notes.append(f"{run_km:g} km is outside 5k-marathon: {L[ka]['label']} numbers used, "
                         "goal bands scaled by distance. Check them with the athlete.")
        sa = (run_km / RUN_DISTANCE_KM[ka]) ** 1.06
        sb = (run_km / RUN_DISTANCE_KM[kb]) ** 1.06
        ev = {"label": name, "kind": "run", "distance_km": run_km, "sports": ["run"],
              "taper_days": _mix_range(L[ka]["taper_days"], L[kb]["taper_days"], w),
              "levels": _blend_levels(L[ka], L[kb], w, sa, sb, ("run_km", "long_run_km")),
              "distribution": _blend_distribution({"Run": (ka, kb, w), "Swim": (ka, kb, w),
                                                   "Bike": (ka, kb, w)}),
              "library_event": ka if w < 0.5 else kb}
    elif list(legs) == ["bike"]:
        base = L["sportive"]
        notes.append("bike-only: the sportive levels apply as they are")
        ka, kb, w = "sportive", "sportive", 0.0
        ev = dict(base, label=name, sports=["bike"], distance_km=bike_km,
                  distribution=_blend_distribution({"Bike": ("sportive", "sportive", 0.0)}),
                  library_event="sportive")
    elif list(legs) == ["swim"]:
        raise ValueError("swim-only races are not covered yet")
    else:
        def minutes(sw, bk, rn):
            return sw * _REF_MIN_PER_KM["swim"] + bk * _REF_MIN_PER_KM["bike"] + rn * _REF_MIN_PER_KM["run"]
        t = minutes(swim_km, bike_km, run_km)
        tt = {k: minutes(*v) for k, v in TRI_LEGS_KM.items()}
        ka, kb, w = _neighbours(t, list(tt.items()))
        if ka == kb:
            notes.append(f"about {t / 60:.1f} h at a mid-level pace, outside sprint-Ironman: "
                         f"{L[ka]['label']} numbers used. Check them with the athlete.")
        idx = {"swim": 0, "bike": 1, "run": 2}
        pairs = {}
        for sport, km in legs.items():
            pairs[sport.title()] = _neighbours(km, [(k, v[idx[sport]]) for k, v in TRI_LEGS_KM.items()])
        keep = ("hours",) + (("long_ride_h",) if "bike" in legs else ()) + (
            ("long_run_km",) if "run" in legs else ())
        ev = {"label": name, "kind": "tri", "sports": list(legs),
              "legs_km": {k: v for k, v in legs.items()}, "est_minutes": int(round(t)),
              "taper_days": _mix_range(L[ka]["taper_days"], L[kb]["taper_days"], w),
              "levels": _blend_levels(L[ka], L[kb], w, t / tt[ka], t / tt[kb], keep),
              "distribution": _blend_distribution(pairs),
              "library_event": ka if w < 0.5 else kb}
        if "swim" in legs:
            ev["long_swim_m"] = int(round(swim_km * 1150, -2))      # ~15% overdistance
    ev["blended_from"] = {"a": ka, "b": kb, "weight_b": round(w, 2)}
    ev["notes"] = notes
    return ev


def describe_blend(ev: dict) -> str:
    """One line for the athlete: what the temporary blueprint was blended from."""
    b = ev.get("blended_from") or {}
    L = _levels()
    if not b or b.get("a") == b.get("b"):
        return f"{ev.get('label')}: based on {L.get(b.get('a'), {}).get('label', 'the nearest event')}"
    pct = int(round((b.get("weight_b") or 0) * 100))
    return (f"{ev.get('label')}: a blend of {L[b['a']]['label']} ({100 - pct}%) and "
            f"{L[b['b']]['label']} ({pct}%)")
