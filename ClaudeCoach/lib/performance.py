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
        "training":    best 3k / 5k / 10k inside any run in the last 90 days (pace curve)
        "criticalSpeed": {"mps", "pace_s_km"} - Intervals.icu's 90-day model, for reference
    }}

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
        "average_speed", "device_name", "race")
TRAINING_EVENTS = {"3k": 3000.0, "5k": 5000.0, "10k": 10000.0}


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
    if not cache.get("acts") or not full or (today - full).days >= FULL_EVERY_DAYS:
        acts = [_slim(a) for a in client.get_training_history(days=HISTORY_DAYS) or []]
        cache = {"full": today.isoformat(), "acts": acts}
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


def determinants(acts: list, today: date, profile: dict | None = None,
                 training: list | None = None, cs_mps: float | None = None) -> dict:
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
    out = {"races": races, "k": rm.personal_k(races, today), "predictions": preds,
           "weeklyKm": weekly_km, "longestKm": longest, "recentBreakDays": brk, "efficiency": eff,
           "training": training or []}
    if cs_mps:
        out["criticalSpeed"] = {"mps": round(cs_mps, 3), "pace_s_km": round(1000 / cs_mps)}
    return out


def record_week(slug: str, run: dict, today: date) -> None:
    """One row per ISO week (the latest build of the week wins), newest last."""
    path = _athlete_dir(slug) / "performance-history.json"
    rows = _load(path, [])
    week = (today - timedelta(days=today.weekday())).isoformat()
    recent = [r for r in run.get("races") or [] if (today - rm._d(r["date"])).days <= 365]
    row = {"week": week, "date": today.isoformat(),
           "pred": {ev: p["now_s"] for ev, p in (run.get("predictions") or {}).items()},
           "k": (run.get("k") or {}).get("k"), "weeklyKm": run.get("weeklyKm"),
           "longestKm": run.get("longestKm"),
           "efficiency": (run.get("efficiency") or {}).get("value"),
           "bestRaceVdot": max((r["vdot"] for r in recent), default=None),
           "races": len(run.get("races") or [])}
    rows = [r for r in rows if r.get("week") != week] + [row]
    try:
        path.write_text(json.dumps(rows[-KEEP_WEEKS:], indent=1))
    except OSError:
        pass


def build(slug: str, client, cfg: dict | None = None, profile: dict | None = None,
          today: date | None = None) -> dict | None:
    today = today or date.today()
    acts = activities(slug, client, today)
    try:
        pc = client._get("pace-curves", {"type": "Run", "curves": "90d"})
    except Exception:
        pc = None
    training, cs = training_efforts(pc)
    run = determinants(acts, today, profile, training, cs)
    record_week(slug, run, today)
    return {"asOf": today.isoformat(), "run": run}
