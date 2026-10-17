"""Bike courses from a GPX: the profile, the climbs, and what the course demands of a rider
(Jamie, 17 Oct 2026: outdoor ride times can't be predicted - "wind and drafting" - "but
the needs of the race can be worked out").

PREDICTABLE vs NOT. On a climb (CLIMB_GRADE or steeper) speed is set by power, weight and
gravity; drafting and wind barely matter, so the time a climb takes at a given power is
physics. Flats and descents depend on the group and the wind, so they are never
predicted: `needs` says what average SPEED they would need in the time the cut-offs leave.

Physics (the standard road-cycling power balance, Martin et al. 1998):
    P x DRIVETRAIN = v x m g (sin a + Crr cos a) + 0.5 rho CdA v^3
with CdA 0.36 (hoods), Crr 0.005, bike + kit KIT_KG. Assumptions, said wherever a number
is shown. Everything here is pure; course files live in reference/races/ (public facts
only) and riders' own GPX files in athletes/<slug>/courses/.
"""
from __future__ import annotations

import json
import math
import re
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
RACES_DIR = BASE / "reference" / "races"
STEP_M = 100
CLIMB_GRADE = 0.03              # a 100 m segment this steep or more is "climbing"
CLIMB_MIN_GAIN = 80             # a named climb gains at least this much
CDA, CRR, RHO, DRIVETRAIN, G = 0.36, 0.005, 1.2, 0.97, 9.81
KIT_KG = 11.0
VMAX = 50 / 3.6                 # descents: nobody is timed faster than this
STOP_MIN_PER_FEED = 4           # minutes at each feed station passed


# ── GPX -> profile ────────────────────────────────────────────────────────────

def parse_gpx(text: str) -> list:
    """[(lat, lon, ele)] from track (or route) points that carry an elevation."""
    pts = re.findall(r'<(?:trkpt|rtept)\s+lat="([-\d.]+)"\s+lon="([-\d.]+)"[^>]*>\s*<ele>([-\d.]+)</ele>', text)
    if not pts:   # lon before lat
        pts = [(a, b, c) for b, a, c in re.findall(
            r'<(?:trkpt|rtept)\s+lon="([-\d.]+)"\s+lat="([-\d.]+)"[^>]*>\s*<ele>([-\d.]+)</ele>', text)]
    return [(float(a), float(b), float(c)) for a, b, c in pts]


def _hav(a, b) -> float:
    r = 6371000.0
    p1, p2 = math.radians(a[0]), math.radians(b[0])
    dp, dl = p2 - p1, math.radians(b[1] - a[1])
    return 2 * r * math.asin(math.sqrt(math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2))


def profile(points: list, step_m: int = STEP_M) -> list:
    """Elevation every step_m along the track, lightly smoothed (3 points) so GPS/DEM
    jitter doesn't read as climbing."""
    if len(points) < 2:
        return []
    d = [0.0]
    for i in range(1, len(points)):
        d.append(d[-1] + _hav(points[i - 1], points[i]))
    raw, j = [], 0
    for k in range(0, int(d[-1]) + 1, step_m):
        while j < len(d) - 2 and d[j + 1] < k:
            j += 1
        f = (k - d[j]) / max(1e-9, d[j + 1] - d[j])
        raw.append(points[j][2] + f * (points[j + 1][2] - points[j][2]))
    return [round(sum(raw[max(0, i - 1):i + 2]) / len(raw[max(0, i - 1):i + 2]), 1) for i in range(len(raw))]


def ascent(ele: list) -> int:
    return int(round(sum(max(0.0, ele[i] - ele[i - 1]) for i in range(1, len(ele)))))


def _grades(ele: list, step_m: int = STEP_M) -> list:
    return [(ele[i + 1] - ele[i]) / step_m for i in range(len(ele) - 1)]


def climbs(ele: list, summits: list | None = None, step_m: int = STEP_M) -> list:
    """[{name, start_km, top_km, length_km, gain_m, avg_pct, top_m}]: stretches climbing
    at CLIMB_GRADE or more (500 m average), through dips of up to 300 m, gaining at least
    CLIMB_MIN_GAIN. Named after the nearest summit in `summits` ([{name, km}])."""
    g = _grades(ele, step_m)
    sm = [sum(g[max(0, i - 2):i + 3]) / len(g[max(0, i - 2):i + 3]) for i in range(len(g))]
    runs, i = [], 0
    while i < len(sm):
        if sm[i] < CLIMB_GRADE:
            i += 1
            continue
        s, e, gap = i, i, 0
        while i < len(sm) and gap <= 3:
            if sm[i] >= CLIMB_GRADE:
                e, gap = i, 0
            else:
                gap += 1
            i += 1
        runs.append((s, e + 1))
    out = []
    for s, e in runs:
        top = max(range(s, e + 1), key=lambda k: ele[k])
        start = min(range(s, top + 1), key=lambda k: ele[k])
        gain = ele[top] - ele[start]
        if gain < CLIMB_MIN_GAIN:
            continue
        length = (top - start) * step_m / 1000
        c = {"name": None, "start_km": round(start * step_m / 1000, 1), "top_km": round(top * step_m / 1000, 1),
             "length_km": round(length, 1), "gain_m": int(round(gain)),
             "avg_pct": round(gain / (length * 10), 1) if length else 0.0, "top_m": int(round(ele[top]))}
        near = min(summits or [], key=lambda x: abs(x["km"] - c["top_km"]), default=None)
        if near and abs(near["km"] - c["top_km"]) <= 3:
            c["name"] = near["name"]
        out.append(c)
    return out


# ── physics ───────────────────────────────────────────────────────────────────

def speed(power_w: float, grade: float, mass_kg: float, cda: float = CDA) -> float:
    """m/s at a steady power on a grade (solo). Descents capped at VMAX."""
    a = math.atan(grade)
    f = mass_kg * G * (math.sin(a) + CRR * math.cos(a))
    p = power_w * DRIVETRAIN
    if f * VMAX + 0.5 * RHO * cda * VMAX ** 3 <= p:
        return VMAX
    lo, hi = 0.3, VMAX
    for _ in range(50):
        v = (lo + hi) / 2
        if f * v + 0.5 * RHO * cda * v ** 3 > p:
            hi = v
        else:
            lo = v
    return lo


def climbing_split(ele: list, a_km: float, b_km: float, power_w: float, mass_kg: float,
                   step_m: int = STEP_M) -> tuple:
    """(seconds on climbing segments at power_w, km of climbing, km of the rest) between
    two points. Climbing = a 100 m segment at CLIMB_GRADE or steeper."""
    g = _grades(ele, step_m)
    secs = climb_km = rest_km = 0.0
    for i in range(int(a_km * 1000 / step_m), min(int(b_km * 1000 / step_m), len(g))):
        if g[i] >= CLIMB_GRADE:
            secs += step_m / speed(power_w, g[i], mass_kg)
            climb_km += step_m / 1000
        else:
            rest_km += step_m / 1000
    return secs, climb_km, rest_km


def _clock_min(hhmm: str) -> int:
    h, m = str(hhmm).split(":")[:2]
    return int(h) * 60 + int(m)


def needs(course: dict, power_w: float, rider_kg: float) -> list:
    """For each cut-off: the climbing time to it at power_w (physics), the feed stops
    passed, and the average speed everything else would need to make it.
    [{label, km, clock, budget_min, climb_min, climb_km, rest_km, stops_min, rest_kmh}]"""
    ele, mass = course["ele"], rider_kg + KIT_KG
    if not course.get("start"):
        return []                      # a bare GPX: no clock, so no cut-off maths
    start = _clock_min(course["start"])
    out = []
    for cp in course.get("checkpoints") or []:
        if cp.get("type") not in ("cutoff", "finish"):
            continue
        secs, ckm, rkm = climbing_split(ele, 0, cp["km"], power_w, mass, course.get("step_m", STEP_M))
        stops = STOP_MIN_PER_FEED * sum(1 for f in course.get("feeds") or [] if f["km"] < cp["km"])
        budget = _clock_min(cp["clock"]) - start
        left_h = (budget - stops - secs / 60) / 60
        out.append({"label": cp.get("label") or f"km {cp['km']:g}", "km": cp["km"], "clock": cp["clock"],
                    "on_miss": cp.get("on_miss"),
                    "budget_min": budget, "climb_min": round(secs / 60), "climb_km": round(ckm, 1),
                    "rest_km": round(rkm, 1), "stops_min": stops,
                    "rest_kmh": round(rkm / left_h, 1) if left_h > 0 else None})
    return out


def climb_power_for(course: dict, rider_kg: float, rest_kmh: float) -> float | None:
    """The steady climbing power at which every cut-off leaves at most rest_kmh to find
    elsewhere (the binding cut-off decides)."""
    if not needs(course, 200.0, rider_kg):
        return None                    # nothing timed to make
    lo, hi = 50.0, 800.0
    for _ in range(40):
        p = (lo + hi) / 2
        worst = max((n["rest_kmh"] if n["rest_kmh"] is not None else 999) for n in needs(course, p, rider_kg))
        if worst > rest_kmh:
            lo = p
        else:
            hi = p
    return hi if hi < 799 else None


# ── course files ──────────────────────────────────────────────────────────────

def load(path) -> dict | None:
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None


def _norm(s) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(s or "").lower()).strip()


def for_race(name: str, slug: str | None = None) -> dict | None:
    """The course for a race name: the athlete's own upload first, then reference/races.
    Names compare with punctuation folded ("70.3" = "70 3"); a course with no name or
    match words never matches."""
    key = _norm(name)
    if not key:
        return None
    dirs = ([BASE / "athletes" / slug / "courses"] if slug else []) + [RACES_DIR]
    for d in dirs:
        for f in sorted(d.glob("*.json")) if d.exists() else []:
            c = load(f)
            words = [w for w in (_norm(m) for m in (c or {}).get("match") or [(c or {}).get("name")]) if w]
            if c and any(w in key for w in words):
                return c
    return None


def from_gpx(text: str, name: str, **facts) -> dict:
    """A course dict from GPX text plus any facts (start, checkpoints, feeds, summits)."""
    ele = profile(parse_gpx(text))
    c = {"name": name, "match": [_norm(name)],
         "distance_km": round((len(ele) - 1) * STEP_M / 1000, 1), "ascent_m": ascent(ele),
         "step_m": STEP_M, **facts, "ele": ele}
    c["climbs"] = climbs(ele, c.get("summits"))
    return c
