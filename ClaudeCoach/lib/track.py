"""A done session's GPS track for Peak's map, coloured by zone (Jamie, 14 Oct 2026: "add a
map ... colour the GPS view by power or HR or pace"; no privacy zone, his call).

Served on demand by the API (/api/track/<slug>) to the athlete and Jamie only, never
written into training-data: the published route stays the coordinate-free outline from
lib/route_shape.py. Built from Intervals.icu streams:

    {"pts": [[lat, lng], ...],                 at most MAX_PTS, evenly sampled
     "ch":  {"power": [zone, ...], "hr": [...], "pace": [...]}}   zone 1-7 per point,
                                                                  0 = no reading there

Zones are the athlete's own Intervals.icu zones (workout_profile._zone / _hr_zone), so the
map, the workout bars and Intervals.icu agree on colour. Each value is smoothed over
SMOOTH_S seconds first: raw 1 s power flickers through every zone at a junction.
"""
from __future__ import annotations

import json
from pathlib import Path

import workout_profile as wp

MAX_PTS = 800
SMOOTH_S = 30


def _smooth(vals: list, w: int) -> list:
    out, run, n, q = [], 0.0, 0, []
    for v in vals:
        q.append(v)
        if v is not None:
            run += v
            n += 1
        if len(q) > w:
            old = q.pop(0)
            if old is not None:
                run -= old
                n -= 1
        out.append(run / n if n else None)
    return out


def build(streams: list, ss: dict, sport: str, css_s: float | None = None) -> dict | None:
    st = {s.get("type"): s for s in streams or [] if isinstance(s, dict)}
    ll = st.get("latlng") or {}
    lat, lng = ll.get("data") or [], ll.get("data2") or []
    n = min(len(lat), len(lng))
    idx = [i for i in range(n) if isinstance(lat[i], (int, float)) and isinstance(lng[i], (int, float))
           and not (lat[i] == 0 and lng[i] == 0)]
    if len(idx) < 10:
        return None
    step = max(1, -(-len(idx) // MAX_PTS))          # ceil: never over MAX_PTS
    keep = idx[::step]
    pts = [[round(lat[i], 5), round(lng[i], 5)] for i in keep]

    def chan(key):
        d = (st.get(key) or {}).get("data") or []
        return _smooth([v if isinstance(v, (int, float)) and v > 0 else None for v in d], SMOOTH_S) if d else []

    ch = {}
    watts = chan("watts")
    if watts and ss.get("ftp") and "Ride" in sport:     # running power: placeholder run FTPs
        ftp = float(ss["ftp"])
        ch["power"] = [wp._zone(100 * watts[i] / ftp, ss.get("power_zones")) if i < len(watts) and watts[i] else 0
                       for i in keep]
    hr = chan("heartrate")
    if hr and ss.get("hr_zones"):
        ch["hr"] = [wp._hr_zone(hr[i], ss.get("hr_zones")) if i < len(hr) and hr[i] else 0 for i in keep]
    vel = chan("velocity_smooth")
    thr_v = None
    if ss.get("threshold_pace"):
        thr_v = float(ss["threshold_pace"])
    elif css_s and "Swim" in sport:
        thr_v = 100.0 / css_s
    if vel and thr_v and "Ride" not in sport:
        ch["pace"] = [wp._zone(100 * vel[i] / thr_v, ss.get("pace_zones")) if i < len(vel) and vel[i] else 0
                      for i in keep]
    for k in list(ch):
        if not any(ch[k]):
            del ch[k]
    return {"pts": pts, "ch": ch}


def find_activity(acts: list, date: str, sport: str, name: str | None) -> dict | None:
    """The activity the sheet is showing: same day, same sport family, by name if given."""
    fam = wp_family(sport)
    same = [a for a in acts or [] if str(a.get("start_date_local") or "")[:10] == date
            and wp_family(a.get("type")) == fam]
    if name:
        named = [a for a in same if (a.get("name") or "") == name]
        if named:
            return named[0]
    return same[0] if same else None


def wp_family(t) -> str:
    t = str(t or "")
    for k, f in (("Ride", "bike"), ("Bike", "bike"), ("Run", "run"), ("Swim", "swim")):
        if k in t:
            return f
    return t.lower()


def cached(base: Path, slug: str, aid: str):
    p = base / "athletes" / slug / "track-cache" / f"{aid}.json"
    try:
        return json.loads(p.read_text())
    except Exception:
        return None


def store(base: Path, slug: str, aid: str, data: dict) -> None:
    d = base / "athletes" / slug / "track-cache"
    try:
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{aid}.json").write_text(json.dumps(data))
    except OSError:
        pass
