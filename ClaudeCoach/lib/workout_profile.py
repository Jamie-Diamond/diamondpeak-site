"""Workout profiles for Peak: a session as zone-coloured blocks, planned or done (Jamie,
13 Oct 2026, from Intervals.icu's activity card: "Can our planned and completed workout
have something like this ... I want it to look modern!").

A profile is a compact list of blocks, [[seconds, percent, zone], ...]:
  seconds  how long the block lasts (its width)
  percent  intensity as % of threshold - FTP for power, threshold pace for run / swim
           pace, LTHR for heart rate (its height; 100 = threshold)
  zone     1-7 in the athlete's own Intervals.icu zones for that sport (its colour)

planned(doc, ss)   from an event's workout_doc steps (repeats expanded)
completed(...)     from an activity's streams: power for a ride with power, else speed for
                   a run or swim, else heart rate; binned to ~1 minute, at most MAX_BLOCKS
attach(...)        fills `prof` on recent[] rows (cached by activity id, like route shapes)

Pure apart from attach(); never raises out of attach().
"""
from __future__ import annotations

import json
import time
from pathlib import Path

MAX_BLOCKS = 90
MIN_BIN_S = 60
MAX_NEW_FETCHES = 25         # per refresh, so a first run can't stall the data job
FETCH_PAUSE_S = 0.3
PROFILE_DAYS = 60            # completed sessions this recent get a profile


def _zone(pct: float, bounds: list | None) -> int:
    """1-based zone for a % of threshold, from Intervals.icu % boundaries (power_zones /
    pace_zones are % of FTP / threshold pace, upper bounds)."""
    if not bounds:
        bounds = [55, 75, 90, 105, 120, 150, 999]
    for i, b in enumerate(bounds):
        if pct <= float(b):
            return i + 1
    return len(bounds)


def _hr_zone(bpm: float, hr_bounds: list | None) -> int:
    if not hr_bounds:
        return 1
    for i, b in enumerate(hr_bounds):
        if bpm <= float(b):
            return i + 1
    return len(hr_bounds) + 1


def settings_for(sport_settings: list, sport: str) -> dict:
    sport = sport or ""
    for ss in sport_settings or []:
        types = ss.get("types") or []
        if any(t == sport or t in sport or sport in t for t in types):
            return ss
    return {}


def _step_block(st: dict, ss: dict):
    for key in ("power", "pace", "hr"):
        t = st.get(key)
        if not isinstance(t, dict):
            continue
        lo = t.get("start", t.get("value"))
        hi = t.get("end", lo)
        if lo is None:
            continue
        pct = (float(lo) + float(hi)) / 2
        units = str(t.get("units") or "")
        if key == "power":
            z = _zone(pct, ss.get("power_zones"))
        elif key == "pace":
            z = _zone(pct, ss.get("pace_zones"))
        else:
            if "lthr" in units and ss.get("lthr"):
                z = _hr_zone(pct * float(ss["lthr"]) / 100, ss.get("hr_zones"))
            else:
                z = _zone(pct, [81, 89, 94, 100, 103, 106, 999])
        return [int(st.get("duration") or 0), round(pct), z]
    if st.get("duration"):
        return [int(st["duration"]), 50, 1]
    return None


def planned(doc: dict | None, ss: dict) -> list:
    """Blocks for a planned workout; [] without structure."""
    out = []

    def walk(steps, reps=1):
        for _ in range(max(1, reps)):
            for st in steps or []:
                if not isinstance(st, dict):
                    continue
                if isinstance(st.get("steps"), list):
                    walk(st["steps"], int(st.get("reps") or 1))
                else:
                    b = _step_block(st, ss)
                    if b and b[0] > 0:
                        out.append(b)
    walk((doc or {}).get("steps"))
    return out


def completed(streams: dict, ss: dict, sport: str, threshold_pace_s=None) -> list:
    """Blocks for a done session from its streams ({type: data}); [] if nothing usable."""
    watts, vel, hr = streams.get("watts"), streams.get("velocity_smooth"), streams.get("heartrate")
    t = streams.get("time")
    n = max(len(watts or []), len(vel or []), len(hr or []))
    if n < 120:
        return []
    kind = None
    has_power = bool(watts and ss.get("ftp") and sum(1 for w in watts if w) > n * 0.3)
    has_pace = bool(vel and "Ride" not in sport and (threshold_pace_s or ss.get("threshold_pace")))
    # Rides by power; runs and swims by pace (running power against a placeholder run FTP
    # put Jamie's easy run in Z3 when Intervals.icu has it in Z1-Z2 by pace).
    if has_power and ("Ride" in sport or not has_pace):
        kind, src, ref = "power", watts, float(ss["ftp"])
    elif has_pace:
        thr_v = (float(ss["threshold_pace"]) if ss.get("threshold_pace")
                 else (100.0 / threshold_pace_s if "Swim" in sport else 1000.0 / threshold_pace_s))
        kind, src, ref = "pace", vel, thr_v
    elif hr and ss.get("lthr"):
        kind, src, ref = "hr", hr, float(ss["lthr"])
    if not kind:
        return []
    total = (t[-1] - t[0]) if t and len(t) == n else n
    bin_s = max(MIN_BIN_S, int(total / MAX_BLOCKS) + 1)
    out, i = [], 0
    times = t if t and len(t) == n else list(range(n))
    while i < n:
        j = i
        while j < n and times[j] - times[i] < bin_s:
            j += 1
        vals = [float(v) for v in src[i:j] if v is not None]
        if vals:
            m = sum(vals) / len(vals)
            pct = 100.0 * m / ref
            if kind == "power":
                z = _zone(pct, ss.get("power_zones"))
            elif kind == "pace":
                z = _zone(pct, ss.get("pace_zones"))
            else:
                z = _hr_zone(m, ss.get("hr_zones"))
            out.append([int(times[min(j, n - 1)] - times[i]) or bin_s, round(pct), z])
        i = max(j, i + 1)
    return out


def _cache_path(base: Path, slug: str) -> Path:
    return base / "athletes" / slug / "profile-cache.json"


def attach(client, base: Path, slug: str, recent: list, sport_settings: list,
           since: str, id_key: str = "_aid", profile: dict | None = None, log=None) -> int:
    """Fill `prof` on recent[] rows dated `since` or later. Reads row[id_key] but does NOT
    remove it (route_shape.attach_shapes does that afterwards). Returns new fetches."""
    p = _cache_path(base, slug)
    try:
        cache = json.loads(p.read_text())
    except Exception:
        cache = {}
    css_s = None
    try:
        import re
        m = re.match(r"(\d+):(\d{2})", str((profile or {}).get("swim_css_per_100m") or ""))
        css_s = int(m.group(1)) * 60 + int(m.group(2)) if m else None
    except Exception:
        pass
    fetched = 0
    for row in recent:
        aid = row.get(id_key)
        if not aid or str(row.get("date") or "") < since:
            continue
        if aid in cache:
            if cache[aid]:
                row["prof"] = cache[aid]
            continue
        if fetched >= MAX_NEW_FETCHES:
            continue
        try:
            if fetched:
                time.sleep(FETCH_PAUSE_S)
            raw = client.get_activity_streams(aid) or []
            st = {s.get("type"): s.get("data") for s in raw
                  if isinstance(s, dict) and isinstance(s.get("data"), list)}
            sport = str(row.get("sport") or "")
            icu_sport = {"Bike": "Ride", "Run": "Run", "Swim": "Swim"}.get(sport, sport)
            ss = settings_for(sport_settings, icu_sport)
            prof = completed(st, ss, icu_sport, css_s if "Swim" in icu_sport else None)
            cache[aid] = prof or None
            fetched += 1
            if prof:
                row["prof"] = prof
        except Exception as e:
            if log:
                log(f"[{slug}] workout profile {aid} failed (non-fatal): {e}")
    try:
        p.write_text(json.dumps(cache))
    except OSError:
        pass
    return fetched
