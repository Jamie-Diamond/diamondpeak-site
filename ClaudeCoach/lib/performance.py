"""The athlete's running determinants, fetched, cached and tracked week by week (Jamie,
17 Oct 2026: "can we improve our tracking of these things to build a race predictor").

`build(slug, client, cfg, profile)` returns what refresh-site-data publishes as
`performance` in the athlete's training data:

    {"asOf": iso, "run": {
        "races":       race results (lib/run_model.detect_races), last two years
        "k":           {"k", "pairs", "own"} - how much they slow as races get longer
        "predictions": {"5k" | "10k" | "half_marathon" | "marathon": {now_s, model}}
        "weeklyKm":    mean running km a week, last 8 weeks (each race has its own
                       `km_before`, the same for the 8 weeks before it)
        "recentBreakDays": longest stretch without a run in the last 8 weeks
        "longestKm":   longest run, last 8 weeks
        "efficiency":  {"value", "n", "device", "prev"} - speed at heart rate as a VDOT,
                       now and six weeks ago (same watch only)
        "training":    best 3k / 5k / 10k / 15k / half / 30k inside any run in the last 90
                       days (pace curve)
        "criticalSpeed": {"mps", "pace_s_km"} - Intervals.icu's 90-day model, for reference
        "engine":      {"vo2max", "source", "n"} - Garmin's VO2max estimate (median, last 6
                       weeks), else the heart-rate one (run_model.hr_vdot)
        "threshold":   {"pace_s_km", "source", "lthr", "hr_top"} - pace at threshold HR from
                       the speed-on-heart-rate line of the last 6 weeks' runs, else the
                       threshold pace set in Intervals
        "durability":  {"drift_pct", "cost_fade_pct", "n", "longest_km", "source"} - median
                       drift of runs of 75+ min in the last 8 weeks and the longest of them.
                       With running power: power:HR drift and cost fade (watts per m/s,
                       last third against first) from the activity watcher's
                       run-durability-log.json; without, Intervals' pace:HR decoupling
        "power":       {"cp", "model", "best": {secs: watts}, "weight"} - running power
                       (Jamie, 17 Oct 2026: "when we have running power we should use that
                       where possible and pace as a fallback / way of communicating").
                       Critical power from Intervals' 90-day Run power curve
        "economy":     {"value", "prev", "n", "device"} - speed per W/kg on steady runs,
                       last 6 weeks and the 12 weeks before (same watch). Garmin power reads
                       differently from Stryd, so this is only compared with the athlete
    }}
lib/limiters.py turns engine / threshold / durability / weekly km into the Goals page's
"What's holding you back" card.

and appends one row per week to athletes/<slug>/performance-history.json, so the trend
of each determinant is kept for the coach and for the "where to improve" work next.

COST. The race history needs two years of activities, which is one large call. It is
fetched in full at most weekly (FULL_EVERY_DAYS) and topped up with the last
OVERLAP_DAYS otherwise, into athletes/<slug>/run-history.json, keeping only the fields
the model reads. The pace curve is one small call per build. Every failure returns
None and the Goals page keeps its threshold-pace estimate.
"""
from __future__ import annotations

import json
import statistics
from datetime import date, timedelta
from pathlib import Path

import run_model as rm

BASE = Path(__file__).resolve().parent.parent
HISTORY_DAYS = rm.K_WINDOW_DAYS + 60  # race window plus the efficiency window before it
FULL_EVERY_DAYS = 7
OVERLAP_DAYS = 7
KEEP_WEEKS = 156
RUN_TYPES = ("Run", "TrailRun", "VirtualRun", "Treadmill")
SLIM = ("id", "start_date_local", "type", "name", "distance", "moving_time", "elapsed_time",
        "average_heartrate", "athlete_max_hr", "icu_resting_hr", "icu_ignore_hr", "gap",
        "average_speed", "device_name", "race", "VO2MaxGarmin", "lthr", "decoupling",
        "threshold_pace", "icu_average_watts", "icu_weight", "carbs_ingested",
        "icu_weighted_avg_watts", "icu_rolling_ftp")
CACHE_VERSION = 6                    # bump when SLIM grows: forces a full refetch
RIDE_TYPES = ("Ride", "VirtualRide", "GravelRide", "MountainBikeRide")
OUTDOOR_RIDES = ("Ride", "GravelRide", "MountainBikeRide")
LONG_RIDE_S = 3 * 3600
POWER_SECS = (600, 1200, 1800, 3600, 5400, 7200, 10800)
POWER_MODEL_ORDER = ("MORTON_3P", "ECP", "FFT_CURVES", "MS_2P")
ENGINE_DAYS = 42
THRESHOLD_MIN_RUNS = 6
THRESHOLD_MAX_REACH = 30             # bpm the line may be extended past the hardest run
LONG_RUN_S = 75 * 60
TRAINING_EVENTS = {"3k": 3000.0, "5k": 5000.0, "10k": 10000.0, "15k": 15000.0,
                   "half_marathon": 21097.5, "30k": 30000.0}   # long ones: limiters endurance


def _athlete_dir(slug: str) -> Path:
    return BASE / "athletes" / slug


def _slim(a: dict) -> dict:
    return {k: a.get(k) for k in SLIM if a.get(k) is not None}


def _load(p: Path, default):
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return default


def activities(slug: str, client, today: date) -> list:
    """Two years of slim activities, from the cache topped up with recent ones."""
    path = _athlete_dir(slug) / "run-history.json"
    cache = _load(path, {})
    full = rm._d(cache.get("full"))
    if (not cache.get("acts") or not full or (today - full).days >= FULL_EVERY_DAYS
            or cache.get("v") != CACHE_VERSION):
        acts = [_slim(a) for a in client.get_training_history(days=HISTORY_DAYS) or []]
        cache = {"full": today.isoformat(), "acts": acts, "v": CACHE_VERSION}
    else:
        fetched = rm._d(cache.get("fetched")) or full
        days = (today - fetched).days + OVERLAP_DAYS
        fresh = {str(a.get("id")): _slim(a) for a in client.get_training_history(days=days) or []}
        cutoff = (today - timedelta(days=days)).isoformat()
        keep = [a for a in cache["acts"] if str(a.get("id")) not in fresh
                and not (str(a.get("start_date_local"))[:10] >= cutoff)]
        oldest = (today - timedelta(days=HISTORY_DAYS)).isoformat()
        cache["acts"] = [a for a in keep if str(a.get("start_date_local"))[:10] >= oldest] + list(fresh.values())
    cache["fetched"] = today.isoformat()
    try:
        path.write_text(json.dumps(cache, separators=(",", ":")))
    except OSError:
        pass
    return cache["acts"]


def training_efforts(pace_curve: dict | None) -> tuple:
    """([{event, dist_m, time_s, date, vdot}], critical_speed_mps) from a /pace-curves reply."""
    try:
        c = (pace_curve or {}).get("list", [])[0]
    except (IndexError, AttributeError):
        return [], None
    dists, secs = c.get("distance") or [], c.get("values") or []
    ids = c.get("activity_id") or []
    acts = (pace_curve or {}).get("activities") or {}
    out = []
    for ev, m in TRAINING_EVENTS.items():
        if m in dists:
            i = dists.index(m)
            t = secs[i] if i < len(secs) else None
            v = rm.vdot(m, t) if t else None
            if not v or v >= 85:          # a GPS glitch or a ride logged as a run
                continue
            aid = ids[i] if i < len(ids) else None
            when = str((acts.get(aid) or {}).get("start_date_local") or "")[:10]
            out.append({"event": ev, "dist_m": m, "time_s": int(t), "date": when, "vdot": round(v, 1)})
    cs = None
    for model in c.get("paceModels") or []:
        if model.get("type") == "CS" and model.get("criticalSpeed"):
            cs = float(model["criticalSpeed"])
    return out, cs


def _volume(acts: list, today: date, days: int = 56) -> tuple:
    km, longest = 0.0, 0.0
    for a in acts:
        d = rm._d(a.get("start_date_local"))
        if a.get("type") in RUN_TYPES and d and 0 <= (today - d).days < days:
            k = (a.get("distance") or 0) / 1000
            km += k
            longest = max(longest, k)
    return round(km / (days / 7), 1), round(longest, 1)


def _longest_break(acts: list, today: date, days: int = 56) -> int:
    """Longest run of days with no running in the last `days`, up to today."""
    start = today - timedelta(days=days)
    dates = sorted({d for a in acts if a.get("type") in RUN_TYPES
                    for d in [rm._d(a.get("start_date_local"))] if d and start < d <= today})
    edges = [start] + dates + [today + timedelta(days=1)]
    return max((b - a).days - 1 for a, b in zip(edges, edges[1:]))


def _recent_runs(acts: list, today: date, days: int) -> list:
    multi = rm._multisport_days(acts)
    return [a for a in acts if a.get("type") == "Run" and not a.get("race")
            and str(a.get("start_date_local"))[:10] not in multi
            and (d := rm._d(a.get("start_date_local"))) and 0 <= (today - d).days < days]


def engine(acts: list, today: date) -> dict | None:
    runs = _recent_runs(acts, today, ENGINE_DAYS)
    garmin = [a["VO2MaxGarmin"] for a in runs if a.get("VO2MaxGarmin")]
    if garmin:
        return {"vo2max": round(statistics.median(garmin), 1), "source": "Garmin", "n": len(garmin)}
    eff = rm.efficiency(acts, today + timedelta(days=1), ENGINE_DAYS)
    if eff:
        return {"vo2max": eff["value"], "source": "heart rate", "n": eff["n"]}
    return None


def threshold(acts: list, today: date) -> dict | None:
    """Pace at threshold HR from the speed-on-HR line of recent runs (20-120 min), if the
    hardest of them reach within THRESHOLD_MAX_REACH bpm of it; else Intervals' setting."""
    runs = [a for a in _recent_runs(acts, today, ENGINE_DAYS)
            if 1200 <= (a.get("moving_time") or 0) <= 7200 and a.get("average_heartrate")
            and not a.get("icu_ignore_hr") and (a.get("gap") or a.get("average_speed"))]
    lthrs = [a["lthr"] for a in runs if a.get("lthr")]
    lthr = statistics.median(lthrs) if lthrs else None
    if lthr and len(runs) >= THRESHOLD_MIN_RUNS:
        pts = [(a["average_heartrate"], a.get("gap") or a["average_speed"]) for a in runs]
        top = max(h for h, _ in pts)
        mx, my = statistics.mean(h for h, _ in pts), statistics.mean(v for _, v in pts)
        sxx = sum((h - mx) ** 2 for h, _ in pts)
        if sxx > 0 and lthr - top <= THRESHOLD_MAX_REACH:
            b = sum((h - mx) * (v - my) for h, v in pts) / sxx
            v = my + b * (lthr - mx)
            if b > 0 and v > 0:
                return {"pace_s_km": round(1000 / v), "source": "heart rate", "lthr": round(lthr),
                        "hr_top": round(top)}
    set_ = sorted((str(a.get("start_date_local")), a["threshold_pace"]) for a in acts
                  if a.get("type") == "Run" and a.get("threshold_pace"))
    if set_:
        return {"pace_s_km": round(1000 / set_[-1][1]), "source": "Intervals setting",
                "lthr": round(lthr) if lthr else None, "hr_top": None}
    return None


def durability(acts: list, today: date, dur_log: list | None = None, drift: dict | None = None) -> dict | None:
    longs = [a for a in _recent_runs(acts, today, 56) if (a.get("moving_time") or 0) >= LONG_RUN_S]
    if not longs:
        return None
    longest = round(max((a.get("distance") or 0) for a in longs) / 1000, 1)
    ids = {str(a.get("id")) for a in longs}
    got = [(drift or {}).get(i) for i in ids]
    got = [g for g in got if g and g.get("pct") is not None]
    if got:
        methods = {g.get("method") for g in got}
        cost = [e["cost_fade_pct"] for e in dur_log or [] if str(e.get("activity_id")) in ids
                and e.get("cost_fade_pct") is not None]
        return {"drift_pct": _median(g["pct"] for g in got),
                "cost_fade_pct": round(statistics.median(cost), 1) if cost else None,
                "n": len(got), "longest_km": longest,
                "source": "power" if methods == {"power:HR"} else "pace"}
    logged = [e for e in dur_log or [] if str(e.get("activity_id")) in ids
              and e.get("decoupling_pct") is not None]
    if logged:
        cost = [e["cost_fade_pct"] for e in logged if e.get("cost_fade_pct") is not None]
        return {"drift_pct": round(statistics.median(e["decoupling_pct"] for e in logged), 1),
                "cost_fade_pct": round(statistics.median(cost), 1) if cost else None,
                "n": len(logged), "longest_km": longest, "source": "power"}
    drift = [a["decoupling"] for a in longs if a.get("decoupling") is not None]
    return {"drift_pct": round(statistics.median(drift), 1) if drift else None, "cost_fade_pct": None,
            "n": len(longs), "longest_km": longest, "source": "pace"}


def run_power(reply: dict | None, weight: float | None = None) -> dict | None:
    """{"cp", "model", "best", "weight"} from a /power-curves?type=Run reply."""
    try:
        c = (reply or {}).get("list", [])[0]
    except (IndexError, AttributeError):
        return None
    models = {m.get("type"): m for m in c.get("powerModels") or [] if m.get("criticalPower")}
    name = next((n for n in POWER_MODEL_ORDER if n in models), next(iter(models), None))
    if not name:
        return None
    secs, watts = c.get("secs") or [], c.get("watts") or []
    best = {s: watts[secs.index(s)] for s in POWER_SECS if s in secs and secs.index(s) < len(watts)
            and watts[secs.index(s)]}
    return {"cp": int(round(models[name]["criticalPower"])), "model": name, "best": best,
            "weight": c.get("weight") or weight}


def economy(acts: list, today: date) -> dict | None:
    """Speed per W/kg (m/s per W/kg) on steady 20-120 min runs: last 6 weeks, and the 12
    weeks before it on the same watch for the trend."""
    def window(lo, hi):
        rows = []
        for a in acts:
            d = rm._d(a.get("start_date_local"))
            w, kg = a.get("icu_average_watts"), a.get("icu_weight")
            v = a.get("gap") or a.get("average_speed")
            if (a.get("type") == "Run" and not a.get("race") and d and lo <= (today - d).days < hi
                    and w and kg and v and 1200 <= (a.get("moving_time") or 0) <= 7200):
                rows.append((rm.device_key(a.get("device_name")), v / (w / kg)))
        return rows
    now = window(0, ENGINE_DAYS)
    if len(now) < 5:
        return None
    dev = statistics.mode(r[0] for r in now)
    vals = [e for k, e in now if k == dev]
    prev = [e for k, e in window(ENGINE_DAYS, ENGINE_DAYS + 84) if k == dev]
    return {"value": round(statistics.median(vals), 3), "n": len(vals), "device": dev,
            "prev": round(statistics.median(prev), 3) if len(prev) >= 5 else None}


def bike(acts: list, today: date, power: dict | None, ftp: dict | None,
         dur_log: list | None = None, drift: dict | None = None) -> dict | None:
    """The bike block (lib/limiters.diagnose_bike): FTP, outdoor power coverage, longest
    ride, durability (power:HR drift on 3h+ rides, from the activity watcher's
    decoupling-log.json, so only rides with power) and fuelling logged on long rides."""
    rides = [a for a in acts if a.get("type") in RIDE_TYPES
             and (d := rm._d(a.get("start_date_local"))) and 0 <= (today - d).days < 90]
    if not rides and not power:
        return None
    outdoor = [a for a in rides if a.get("type") in OUTDOOR_RIDES and (a.get("moving_time") or 0) >= 3600]
    longs = [a for a in rides if (a.get("moving_time") or 0) >= LONG_RIDE_S]
    ids = {str(a.get("id")) for a in longs}
    if drift:
        logged = [{"decoupling_pct": drift[i]["pct"]} for i in ids if drift.get(i) and drift[i].get("pct") is not None]
    else:
        logged = [e for e in dur_log or [] if str(e.get("activity_id")) in ids and e.get("decoupling_pct") is not None]
    fuel = [(a["carbs_ingested"], a["moving_time"] / 3600) for a in longs if a.get("carbs_ingested")]
    weight = (power or {}).get("weight") or next((a.get("icu_weight") for a in rides if a.get("icu_weight")), None)
    return {"ftp": (ftp or {}).get("watts"), "ftp_source": (ftp or {}).get("source"),
            "weight": weight, "best": (power or {}).get("best") or {},
            "outdoor": {"rides": len(outdoor), "with_power": sum(1 for a in outdoor if a.get("icu_average_watts"))},
            "longest_h": round(max(((a.get("moving_time") or 0) for a in rides), default=0) / 3600, 1),
            "durability": ({"drift_pct": round(statistics.median(e["decoupling_pct"] for e in logged), 1),
                            "n": len(logged)} if logged else None),
            "fuel": ({"g_per_h": round(sum(c for c, _ in fuel) / sum(h for _, h in fuel)), "n": len(fuel)}
                     if fuel else None)}


def determinants(acts: list, today: date, profile: dict | None = None,
                 training: list | None = None, cs_mps: float | None = None,
                 power: dict | None = None, dur_log: list | None = None, drift: dict | None = None) -> dict:
    """The run block of `build`, from data already fetched. Pure."""
    profile = profile or {}
    races = rm.detect_races(acts, profile.get("race_results"), profile.get("race_results_exclude"))
    races = [r for r in races if (today - rm._d(r["date"])).days <= rm.K_WINDOW_DAYS]
    for r in races:
        r["km_before"] = _volume(acts, rm._d(r["date"]))[0]
    weekly_km, longest = _volume(acts, today)
    brk = _longest_break(acts, today)
    preds = {}
    for ev in ("5k", "10k", "half_marathon", "marathon"):
        p = rm.predict(rm.STANDARD_M[ev], races, today, weekly_km, training, brk)
        if p:
            preds[ev] = {"now_s": p["now_s"], "model": p["model"], "method": p["method"],
                         "stale": p.get("stale")}
    eff = rm.efficiency(acts, today + timedelta(days=1))
    if eff:
        prev = rm.efficiency(acts, today - timedelta(days=41))
        eff["prev"] = prev["value"] if prev and prev["device"] == eff["device"] else None
    out = {"engine": engine(acts, today), "threshold": threshold(acts, today),
           "durability": durability(acts, today, dur_log, drift),
           "power": power, "economy": economy(acts, today) if power else None,
           "races": races, "k": rm.personal_k(races, today), "predictions": preds,
           "weeklyKm": weekly_km, "longestKm": longest, "recentBreakDays": brk, "efficiency": eff,
           "training": training or []}
    if cs_mps:
        out["criticalSpeed"] = {"mps": round(cs_mps, 3), "pace_s_km": round(1000 / cs_mps)}
    return out


HISTORY_V = 2                         # rows carry the bike and fitness fields; older rows get backfilled
BACKFILL_WEEKS = 52


def _median(xs):
    xs = [x for x in xs if x is not None]
    return round(statistics.median(xs), 1) if xs else None


def _log_drift(log, as_of: date, min_minutes: int, days: int = 56, sport: str | None = None):
    """Median drift (%) of sessions of min_minutes+ in the `days` before as_of. `log` is the
    drift cache ({id: entry}, lib/drift.py, one method everywhere) or a list of log entries."""
    entries = log.values() if isinstance(log, dict) else (log or [])
    return _median(e.get("pct", e.get("decoupling_pct")) for e in entries
                   if (e.get("duration_min") or 0) >= min_minutes
                   and (sport is None or e.get("sport") == sport)
                   and (d := rm._d(e.get("date"))) and 0 < (as_of - d).days <= days)


DRIFT_RUN_MIN, DRIFT_RIDE_MIN = 75, 180
DRIFT_DAYS = 400
DRIFT_NEW_PER_BUILD = 30              # stream fetches per build: a year fills over a few builds


def drift_cache(slug: str, client, acts: list, today: date, trusted=None) -> dict:
    """{activity_id: {"date", "sport", "duration_min", "pct", "method", "reason"}} for long runs
    and rides of the last DRIFT_DAYS, worked out once from each one's streams (lib/drift.py)
    and kept in athletes/<slug>/drift-cache.json."""
    import drift as _drift
    path = _athlete_dir(slug) / "drift-cache.json"
    cache = _load(path, {})
    todo = []
    for a in acts:
        d = rm._d(a.get("start_date_local"))
        t, mins = str(a.get("type") or ""), (a.get("moving_time") or 0) / 60
        sport = "Run" if t == "Run" else "Ride" if t in RIDE_TYPES else None
        if not sport or not d or (today - d).days > DRIFT_DAYS:
            continue
        if (cache.get(str(a.get("id"))) or {}).get("v") == _drift.VERSION:
            continue
        if mins >= (DRIFT_RUN_MIN if sport == "Run" else DRIFT_RIDE_MIN) and not a.get("race"):
            todo.append((a, sport, d, mins))
    todo.sort(key=lambda x: x[2], reverse=True)          # newest first
    for a, sport, d, mins in todo[:DRIFT_NEW_PER_BUILD]:
        try:
            raw = client.get_activity_streams(a["id"])
        except Exception:
            continue
        ok = trusted(a["id"]) if trusted else True
        r = _drift.session_drift(a, raw, ok)
        cache[str(a["id"])] = {"date": d.isoformat(), "sport": sport, "duration_min": round(mins),
                               "v": _drift.VERSION, **r}
    try:
        path.write_text(json.dumps(cache, separators=(",", ":")))
    except OSError:
        pass
    return cache


def _curve_history(client, sport: str, ends: list) -> dict:
    """{end_iso: run_power(...)} for the 90 days before each date, from Intervals' power
    curves over custom windows ("r.START.END", 13 per call)."""
    out = {}
    for i in range(0, len(ends), 13):
        chunk = ends[i:i + 13]
        ids = ",".join(f"r.{(e - timedelta(days=90)).isoformat()}.{(e - timedelta(days=1)).isoformat()}"
                       for e in chunk)
        try:
            reply = client._get("power-curves", {"type": sport, "curves": ids})
        except Exception:
            continue
        for c in (reply or {}).get("list") or []:
            end = rm._d(str(c.get("id") or "").split(".")[-1])
            if end:
                out[(end + timedelta(days=1)).isoformat()] = run_power({"list": [c]})
    return out


def history_row(as_of: date, acts: list, run_cp=None, bike_cp=None, ctl=None,
                run_log=None, ride_log=None, races=None) -> dict:
    """The factors as they stood on `as_of` (activities before it only): what Trends draws."""
    day = as_of - timedelta(days=1)
    eng = engine(acts, day) or {}
    eco = economy(acts, day) or {}
    km, longest = _volume(acts, day)
    rides = [a for a in acts if a.get("type") in RIDE_TYPES and (d := rm._d(a.get("start_date_local")))
             and 0 <= (day - d).days < 90]
    ftps = sorted(((a["start_date_local"], a["icu_rolling_ftp"]) for a in rides if a.get("icu_rolling_ftp")))
    row = {"week": (as_of - timedelta(days=as_of.weekday())).isoformat(), "date": as_of.isoformat(),
           "v": HISTORY_V, "vo2max": eng.get("vo2max"), "vo2max_source": eng.get("source"),
           "cp": run_cp, "economy": eco.get("value"), "weeklyKm": km, "longestKm": longest,
           "drift": _log_drift(run_log, as_of, DRIFT_RUN_MIN, sport="Run" if isinstance(run_log, dict) else None),
           "bike_cp": bike_cp,
           "bike_ftp": ftps[-1][1] if ftps else None,
           "bike_drift": _log_drift(ride_log, as_of, DRIFT_RIDE_MIN, sport="Ride" if isinstance(ride_log, dict) else None),
           "longest_ride_h": round(max(((a.get("moving_time") or 0) for a in rides), default=0) / 3600, 1),
           "ctl": ctl}
    if races is not None:
        ok = [r for r in races if r["date"] < as_of.isoformat()]
        row["pred"] = {ev: p["now_s"] for ev in ("5k", "10k", "half_marathon", "marathon")
                       if (p := rm.predict(rm.STANDARD_M[ev], ok, day, km))}
    return row


def backfill(slug: str, client, acts: list, today: date, races: list, weeks: int = BACKFILL_WEEKS) -> list:
    """History rows for the past `weeks` Mondays, rebuilt from Intervals: the factors as
    they stood each week, so Trends has a line from day one (Jamie, 17 Oct 2026)."""
    monday = today - timedelta(days=today.weekday())
    ends = [monday - timedelta(weeks=w) for w in range(weeks, 0, -1)]
    run_c = _curve_history(client, "Run", ends)
    ride_c = _curve_history(client, "Ride", ends)
    try:
        wl = client.get_wellness(days=weeks * 7 + 7) or []
        ctl_by = {str(w.get("id"))[:10]: w.get("ctl") for w in wl if w.get("ctl") is not None}
    except Exception:
        ctl_by = {}
    run_log = _load(_athlete_dir(slug) / "run-durability-log.json", [])
    ride_log = _load(_athlete_dir(slug) / "decoupling-log.json", [])
    rows = []
    for e in ends:
        iso = e.isoformat()
        ctl = next((round(ctl_by[d], 1) for d in ((e - timedelta(days=i)).isoformat() for i in range(1, 8))
                    if d in ctl_by), None)
        rows.append(history_row(e, acts, (run_c.get(iso) or {}).get("cp"), (ride_c.get(iso) or {}).get("cp"),
                                ctl, run_log, ride_log, races))
    return rows


def record_week(slug: str, run: dict, today: date, extra: dict | None = None) -> list:
    """One row per ISO week (the latest build of the week wins), newest last. `extra`:
    the bike / fitness fields of history_row for this week. Returns the rows."""
    path = _athlete_dir(slug) / "performance-history.json"
    rows = _load(path, [])
    week = (today - timedelta(days=today.weekday())).isoformat()
    recent = [r for r in run.get("races") or [] if (today - rm._d(r["date"])).days <= 365]
    row = {"week": week, "date": today.isoformat(),
           "pred": {ev: p["now_s"] for ev, p in (run.get("predictions") or {}).items()},
           "k": (run.get("k") or {}).get("k"), "weeklyKm": run.get("weeklyKm"),
           "longestKm": run.get("longestKm"),
           "efficiency": (run.get("efficiency") or {}).get("value"),
           "vo2max": (run.get("engine") or {}).get("vo2max"),
           "cp": (run.get("power") or {}).get("cp"),
           "economy": (run.get("economy") or {}).get("value"),
           "drift": (run.get("durability") or {}).get("drift_pct"),
           "bestRaceVdot": max((r["vdot"] for r in recent), default=None),
           "races": len(run.get("races") or [])}
    for k, v in (extra or {}).items():
        row.setdefault(k, v)
    rows = [r for r in rows if r.get("week") != week] + [row]
    rows = sorted(rows, key=lambda r: r["week"])[-KEEP_WEEKS:]
    try:
        path.write_text(json.dumps(rows, indent=1))
    except OSError:
        pass
    return rows


def build(slug: str, client, cfg: dict | None = None, profile: dict | None = None,
          today: date | None = None) -> dict | None:
    today = today or date.today()
    acts = activities(slug, client, today)
    try:
        pc = client._get("pace-curves", {"type": "Run", "curves": "90d"})
    except Exception:
        pc = None
    training, cs = training_efforts(pc)
    try:
        power = run_power(client._get("power-curves", {"type": "Run", "curves": "90d"}))
    except Exception:
        power = None
    dur_log = _load(_athlete_dir(slug) / "run-durability-log.json", [])
    try:
        import hr_quality
        trusted = lambda aid: not hr_quality.is_untrusted(slug, aid)
    except Exception:
        trusted = None
    drift = drift_cache(slug, client, acts, today, trusted)
    run = determinants(acts, today, profile, training, cs, power, dur_log, drift)
    out = {"asOf": today.isoformat(), "run": run}
    try:
        ride_power = run_power(client._get("power-curves", {"type": "Ride", "curves": "90d"}))
    except Exception:
        ride_power = None
    try:
        from thresholds import get_thresholds
        t = get_thresholds(slug, cfg, client) or {}
        ftp = {"watts": t.get("ftp_watts"), "source": t.get("ftp_source")} if t.get("ftp_watts") else None
    except Exception:
        ftp = None
    out["bike"] = bike(acts, today, ride_power, ftp,
                       _load(_athlete_dir(slug) / "decoupling-log.json", []), drift)
    out["tri"] = tri_block(acts, run, profile, client)
    # Trends (Jamie, 17 Oct 2026): this week's row, and a year rebuilt once from Intervals.
    path = _athlete_dir(slug) / "performance-history.json"
    if not any(r.get("v") == HISTORY_V for r in _load(path, [])[:-1]):
        try:
            old = {r["week"]: r for r in _load(path, [])}
            for r in backfill(slug, client, acts, today, run.get("races") or []):
                old[r["week"]] = {**old.get(r["week"], {}), **r}
            path.write_text(json.dumps(sorted(old.values(), key=lambda r: r["week"])[-KEEP_WEEKS:], indent=1))
        except Exception:
            pass
    try:
        wl = client.get_wellness(days=2) or []
        ctl = round(float(wl[-1].get("ctl")), 1) if wl and wl[-1].get("ctl") is not None else None
    except Exception:
        ctl = None
    now = history_row(today + timedelta(days=1), acts, None, (ride_power or {}).get("cp"), ctl, drift, drift)
    extra = {k: now[k] for k in ("v", "bike_cp", "bike_ftp", "bike_drift", "longest_ride_h", "ctl", "vo2max_source")}
    rows = record_week(slug, run, today, extra)
    # Drift in every row from the one method (lib/drift.py), as the cache fills.
    for r in rows:
        r["drift"] = _log_drift(drift, rm._d(r["date"]), DRIFT_RUN_MIN, sport="Run")
        r["bike_drift"] = _log_drift(drift, rm._d(r["date"]), DRIFT_RIDE_MIN, sport="Ride")
    try:
        path.write_text(json.dumps(rows, indent=1))
    except OSError:
        pass
    out["history"] = rows[-BACKFILL_WEEKS:]
    return out


def _css_s(profile: dict | None, client=None) -> float | None:
    """CSS in seconds per 100 m: the profile's "1:41", else Intervals' Swim threshold pace."""
    v = str((profile or {}).get("swim_css_per_100m") or "")
    try:
        m, s = v.split(":")[:2]
        return int(m) * 60 + float(s)
    except ValueError:
        pass
    try:
        sw = client.get_sport_settings("Swim") if client else None
        sw = sw[0] if isinstance(sw, list) else sw
        mps = (sw or {}).get("threshold_pace")
        return round(100 / mps, 1) if mps else None
    except Exception:
        return None


def tri_block(acts: list, run: dict, profile: dict | None, client=None) -> dict | None:
    """Triathlon measures (lib/tri.py): past tri races, CSS and the open-water factor, bike
    variability, and the off-the-bike factor per distance against the open-run potential
    the athlete had on each race date."""
    import tri
    races = tri.tri_races(acts)
    css = _css_s(profile, client)
    if not races and not css:
        return None
    run_races = (run or {}).get("races") or []

    def open_s(dist_m, iso):
        d = rm._d(iso)
        p = rm.predict(dist_m, [r for r in run_races if r["date"] < iso], d, _volume(acts, d)[0])
        return p["now_s"] if p else None
    kinds = sorted({r["kind"] for r in races})
    return {"races": races, "css_s": css, "swim_factor": list(tri.swim_factor(races, css)),
            "vi": tri.variability(races),
            "off_bike": {k: list(tri.off_bike_factor(races, k, open_s)) for k in kinds}}
