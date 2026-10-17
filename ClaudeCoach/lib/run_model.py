"""Run race prediction from the athlete's own races (Jamie, 17 Oct 2026: "build a better
predictor ... look at the races I have done"; running goals first, triathlon later).

What sets the time for a run race of a given length, and where each one comes from:

  CEILING x ECONOMY   How fast the engine is. Read from real race results, so economy is
                      already in it. VDOT (Daniels & Gilbert 1979) puts races of different
                      lengths on one scale for tracking.
  ENDURANCE           How much of that speed is held as the race gets longer: the athlete's
                      own Riegel exponent k (time2 = time1 x (dist2/dist1)^k) from pairs of
                      their races at different distances run close together. Riegel's
                      population value is 1.06; Vickers & Vertosick (2016) found 1.07 well
                      calibrated up to the half marathon. Higher k = fades more.
  VOLUME (marathon)   Vickers & Vertosick 2016, Model 2 (two races + weekly mileage) or
                      Model 1 (one race + mileage). Riegel alone gave half of runners a
                      marathon at least 10 min too fast; these models cut the squared error
                      from 381 to 208-228. Coefficients from the paper's Additional file 1.
  FITNESS NOW         Races are weighted by how recent they are (RECENCY_HALF_LIFE_DAYS)
                      and how near the target distance. Aerobic efficiency (speed at a given
                      heart rate, `efficiency`) is TRACKED but does not move the prediction:
                      backtested on Jamie's races it made the error worse, and a new watch or
                      strap shifts it (his 2025 switch moved it ~15% with no change in race
                      times).

OUT OF DATE (Jamie, 17 Oct 2026: consistent athletes are fine on a race a year old,
"for a lot of athletes they wouldn't be as flat"). An old race is NOT corrected for a
change in fitness: every correction tried (training volume and pace, volume alone, heart-
rate efficiency) made the backtest worse or no better. Instead the prediction says it is
out of date, and that a parkrun or 5k time trial will update it, when the athlete runs
under STALE_VOLUME_FRAC of the weekly km they ran in the 8 weeks before those races, or
has had STALE_BREAK_DAYS or more without running in the last 8 weeks. Both thresholds
are judgement, not tested. Races carry `km_before`; `recent_break_days` comes from
lib/performance.py.

Race evidence (`detect_races`): a road run that covers a standard distance where the
athlete was racing: Intervals.icu's race flag; or an average HR at racing level for the
duration (HR_RACE_FRAC) or a parkrun (Saturday / Christmas Day / New Year's Day, 9am, 5k)
at a hard HR, AND clearly faster than their usual running (RACE_PACE_FASTER than the
median of the 90 days before: heat or a wrist sensor can push HR up on an easy run, and
did - a hot 1:52 long run and a "solid z2" 42 km both read as racing on HR alone). Over
HR_MAX_MINUTES only the race flag or the coach can say it was a race. Plus the profile's
`race_results` (official times the coach records, which beat the watch) less
`race_results_exclude`. Trail runs, treadmill and multisport legs never count: a 10k off
the bike is a different event.

Pure: no IO. lib/performance.py fetches and caches; lib/season_view.py and plan_tools
`run-predict` call `predict`, so the Goals page and the coach quote the same numbers.
"""
from __future__ import annotations

import math
import re
import statistics
from datetime import date, datetime

# Distances a race result can count as (metres). Predictions use any distance.
STANDARD_M = {"5k": 5000.0, "10k": 10000.0, "15k": 15000.0, "10mile": 16093.4,
              "half_marathon": 21097.5, "marathon": 42195.0}
LABEL = {"5k": "5k", "10k": "10k", "15k": "15k", "10mile": "10 miles",
         "half_marathon": "half", "marathon": "marathon"}
DIST_TOL = (0.97, 1.06)          # GPS distance / standard: short courses out, long GPS in
ROAD_TYPES = ("Run",)            # TrailRun, VirtualRun, Treadmill never count

# Average HR as a fraction of max that says "racing", by duration (minutes, frac).
HR_RACE_FRAC = ((25, 0.88), (50, 0.87), (100, 0.86))
HR_MAX_MINUTES = 100             # longer than this, HR drift makes "racing" unreadable
PARKRUN_HR_FRAC = 0.84
RACE_PACE_FASTER = 0.08          # a race is at least 8% quicker than usual running
SUBMAX_SLACK = 1.05              # a same-event result 5% slower than one within
SUBMAX_DAYS = 180                # 180 days of it was not a max effort

DEFAULT_K = 1.07                 # Vickers & Vertosick 2016
K_PRIOR_WEIGHT = 0.3             # shrink a personal k towards DEFAULT_K on thin evidence
K_BOUNDS = (1.01, 1.20)
K_PAIR_MIN_RATIO = 1.8           # distances must differ by this much to read a k
K_PAIR_GAP_DAYS = 120            # e-folding gap between the two races of a pair

# Nothing older than two years predicts (Jamie, 17 Oct 2026: "you can't use races from
# 2024 to predict 2027, but it's useful for validating the model"). Older races are for
# backtesting only (the 2023-25 races are how the 2.5% error was measured).
RACE_WINDOW_DAYS = 730           # sets "now"
K_WINDOW_DAYS = RACE_WINDOW_DAYS  # sets the fade with distance
RECENCY_HALF_LIFE_DAYS = 270
DIST_SIGMA = 0.8                 # nearness of a race to the target, in ln(distance)

STALE_VOLUME_FRAC = 0.6
STALE_BREAK_DAYS = 21

EFF_WINDOW_DAYS = 42
EFF_MIN_RUNS = 5
KM_PER_MILE = 1.609344


# ── maths ─────────────────────────────────────────────────────────────────────

def vdot(dist_m: float, t_s: float) -> float | None:
    """Daniels & Gilbert VDOT for a race of dist_m metres in t_s seconds."""
    if not dist_m or not t_s or t_s <= 0:
        return None
    tm = t_s / 60.0
    v = dist_m / tm
    vo2 = -4.60 + 0.182258 * v + 0.000104 * v * v
    pct = 0.8 + 0.1894393 * math.exp(-0.012778 * tm) + 0.2989558 * math.exp(-0.1932605 * tm)
    return vo2 / pct


def riegel(t_s: float, d_from: float, d_to: float, k: float) -> float:
    return t_s * (d_to / d_from) ** k


def vickers_model2(t2_s: float, d2_m: float, k_r2r1: float, miles_per_week: float) -> float:
    """Marathon seconds from the longer of two races and the k between them."""
    k_m = 1.4510756 - 0.23797948 * k_r2r1 - 0.01410023 * (miles_per_week / 10.0)
    return t2_s * (42195.0 / d2_m) ** k_m


def vickers_model1(t1_s: float, d1_m: float, miles_per_week: float) -> float:
    """Marathon seconds from one race and weekly mileage."""
    v_riegel = 42195.0 / (t1_s * (42195.0 / d1_m) ** 1.07)
    v = 0.16018617 + 0.83076202 * v_riegel + 0.06423826 * (miles_per_week / 10.0)
    return 42195.0 / v


# ── race evidence ─────────────────────────────────────────────────────────────

def _d(v) -> date | None:
    try:
        return date.fromisoformat(str(v)[:10])
    except (TypeError, ValueError):
        return None


def device_key(name) -> str:
    """'GARMIN FENIX8' and 'Garmin fenix 8' are the same watch."""
    return re.sub(r"[^a-z0-9]", "", str(name or "").lower())


def std_event(dist_m) -> str | None:
    for key, m in STANDARD_M.items():
        if dist_m and DIST_TOL[0] <= dist_m / m <= DIST_TOL[1]:
            return key
    return None


def _race_time_s(a: dict) -> int:
    """Gun-to-line time: elapsed (stops count in a race) unless the watch was left running
    at the end. Not scaled by the GPS distance: a measured course reading 1% short on the
    watch is still the distance (Jamie's Dec 2023 parkrun: 4.96 km, 19:38 elapsed)."""
    moving, elapsed = a.get("moving_time") or 0, a.get("elapsed_time") or 0
    return int(elapsed if elapsed and elapsed <= moving * 1.05 else moving)


def _hr_frac(a: dict) -> float | None:
    hr, mx = a.get("average_heartrate"), a.get("athlete_max_hr")
    if not hr or not mx or a.get("icu_ignore_hr"):
        return None
    return hr / mx


def _racing_hr(a: dict) -> bool:
    f = _hr_frac(a)
    if f is None:
        return False
    mins = (a.get("moving_time") or 0) / 60
    if mins > HR_MAX_MINUTES:
        return False
    return f >= next(frac for lim, frac in HR_RACE_FRAC if mins <= lim)


def _pace(a: dict) -> float | None:
    d, t = a.get("distance") or 0, a.get("moving_time") or 0
    return t / d if d > 0 and t > 0 else None


def _quicker_than_usual(a: dict, runs: list) -> bool:
    """Pace at least RACE_PACE_FASTER quicker than the median run of the 90 days before."""
    day = _d(a.get("start_date_local"))
    p = _pace(a)
    if not day or not p:
        return False
    usual = [q for r in runs if r is not a
             for q in [_pace(r)] if q and 0 < (day - _d(r.get("start_date_local"))).days <= 90]
    if len(usual) < 5:
        return False
    return p <= statistics.median(usual) * (1 - RACE_PACE_FASTER)


def _parkrun(a: dict) -> bool:
    try:
        dt = datetime.fromisoformat(str(a.get("start_date_local")))
    except (TypeError, ValueError):
        return False
    day_ok = dt.weekday() == 5 or (dt.month, dt.day) in ((12, 25), (1, 1))
    on_time = (dt.hour == 8 and dt.minute >= 55) or (dt.hour == 9 and dt.minute <= 15)
    f = _hr_frac(a)
    return (day_ok and on_time and std_event(a.get("distance")) == "5k"
            and f is not None and f >= PARKRUN_HR_FRAC)


def _multisport_days(activities: list) -> set:
    """Race days of a triathlon / duathlon: a Transition logged, or a swim and a ride on
    the same day. NOT any day with a ride: a triathlete rides and runs on most days, and
    that excluded half their runs (and any parkrun with a ride later)."""
    by_day = {}
    for a in activities:
        by_day.setdefault(str(a.get("start_date_local"))[:10], set()).add(str(a.get("type") or ""))
    return {d for d, ts in by_day.items()
            if "Transition" in ts or (any("Swim" in t for t in ts) and ts & {"Ride", "VirtualRide"})}


def _parse_time(v) -> int | None:
    parts = str(v or "").strip().split(":")
    try:
        nums = [int(float(p)) for p in parts]
    except ValueError:
        return None
    if len(nums) == 2:
        return nums[0] * 60 + nums[1]
    if len(nums) == 3:
        return nums[0] * 3600 + nums[1] * 60 + nums[2]
    return None


def _manual(entries: list) -> list:
    out = []
    for e in entries or []:
        ev = e.get("event")
        if ev not in STANDARD_M and e.get("distance_km"):
            ev = std_event(float(e["distance_km"]) * 1000)
        t, when = _parse_time(e.get("time")), _d(e.get("date"))
        if ev in STANDARD_M and t and when:
            out.append({"date": when.isoformat(), "event": ev, "dist_m": STANDARD_M[ev],
                        "time_s": t, "name": e.get("name") or "", "source": "manual",
                        "id": e.get("activity_id")})
    return out


def _drop_submax(races: list) -> list:
    keep = []
    for r in races:
        rd = _d(r["date"])
        beaten = any(o is not r and o["event"] == r["event"]
                     and abs((_d(o["date"]) - rd).days) <= SUBMAX_DAYS
                     and r["time_s"] > o["time_s"] * SUBMAX_SLACK for o in races)
        if not beaten:
            keep.append(r)
    return keep


def detect_races(activities: list, overrides: list | None = None,
                 exclude: list | None = None) -> list:
    """Race results, oldest first: [{date, event, dist_m, time_s, name, source, id, device,
    vdot}]. `overrides` (profile race_results) win over a detected race on the same day and
    distance; `exclude` lists activity ids the coach has ruled out."""
    skip = {str(x) for x in exclude or []}
    multi = _multisport_days(activities)
    runs = [a for a in activities if a.get("type") in ROAD_TYPES and _d(a.get("start_date_local"))]
    found = []
    for a in activities:
        if a.get("type") not in ROAD_TYPES or str(a.get("id")) in skip:
            continue
        ev = std_event(a.get("distance"))
        day = str(a.get("start_date_local"))[:10]
        if not ev or day in multi:
            continue
        if a.get("race"):
            src = "race flag"
        elif _parkrun(a) and _quicker_than_usual(a, runs):
            src = "parkrun"
        elif _racing_hr(a) and _quicker_than_usual(a, runs):
            src = "racing heart rate"
        else:
            continue
        found.append({"date": day, "event": ev, "dist_m": STANDARD_M[ev],
                      "time_s": _race_time_s(a), "name": a.get("name") or "",
                      "source": src, "id": a.get("id"), "device": device_key(a.get("device_name"))})
    manual = _manual(overrides)
    manual_keys = {(m["date"], m["event"]) for m in manual}
    races = [r for r in found if (r["date"], r["event"]) not in manual_keys] + manual
    races = _drop_submax(races)
    for r in races:
        r["vdot"] = round(vdot(r["dist_m"], r["time_s"]), 1)
    return sorted(races, key=lambda r: r["date"])


# ── endurance: the athlete's own k ────────────────────────────────────────────

def personal_k(races: list, today: date) -> dict:
    """{"k", "pairs", "own"}: weighted k over pairs of races at clearly different distances,
    pairs run close together counting most, shrunk towards DEFAULT_K."""
    rs = [r for r in races if 0 <= (today - _d(r["date"])).days <= K_WINDOW_DAYS]
    num, den, n = 0.0, 0.0, 0
    for i, a in enumerate(rs):
        for b in rs[i + 1:]:
            short, long_ = (a, b) if a["dist_m"] < b["dist_m"] else (b, a)
            if long_["dist_m"] / short["dist_m"] < K_PAIR_MIN_RATIO:
                continue
            k = math.log(long_["time_s"] / short["time_s"]) / math.log(long_["dist_m"] / short["dist_m"])
            if not K_BOUNDS[0] <= k <= K_BOUNDS[1]:
                continue
            da, db = _d(a["date"]), _d(b["date"])
            age = (today - max(da, db)).days
            w = math.exp(-abs((da - db).days) / K_PAIR_GAP_DAYS) * 0.5 ** (age / 730)
            num, den, n = num + w * k, den + w, n + 1
    if not n:
        return {"k": DEFAULT_K, "pairs": 0, "own": False}
    k = (num + K_PRIOR_WEIGHT * DEFAULT_K) / (den + K_PRIOR_WEIGHT)
    return {"k": round(k, 3), "pairs": n, "own": True}


# ── fitness since the race: aerobic efficiency ────────────────────────────────

def hr_vdot(a: dict) -> float | None:
    """VDOT a steady run implies from its speed at its heart rate (Swain: %HR reserve ~
    %VO2 reserve; Daniels' oxygen cost of running speed). Noisy run to run; the trend is
    the point. None for anything not a steady 25-120 min road run with HR."""
    if a.get("type") not in ROAD_TYPES or a.get("race") or a.get("icu_ignore_hr"):
        return None
    t = a.get("moving_time") or 0
    hr, mx, rest = a.get("average_heartrate"), a.get("athlete_max_hr"), a.get("icu_resting_hr")
    speed = a.get("gap") or a.get("average_speed")
    if not (1500 <= t <= 7200 and hr and mx and rest and speed and mx > rest):
        return None
    p = (hr - rest) / (mx - rest)
    if not 0.60 <= p <= 0.88:
        return None
    v = speed * 60.0
    vo2 = -4.60 + 0.182258 * v + 0.000104 * v * v
    return 3.5 + (vo2 - 3.5) / p


def efficiency(activities: list, on: date, days: int = EFF_WINDOW_DAYS) -> dict | None:
    """{"value", "n", "device"}: median hr_vdot of steady runs in the `days` before `on`,
    on the watch most of them used. None under EFF_MIN_RUNS."""
    rows = []
    for a in activities:
        d = _d(a.get("start_date_local"))
        if d and 0 < (on - d).days <= days:
            e = hr_vdot(a)
            if e:
                rows.append((device_key(a.get("device_name")), e))
    if not rows:
        return None
    dev = statistics.mode([r[0] for r in rows])
    vals = [e for k, e in rows if k == dev]
    if len(vals) < EFF_MIN_RUNS:
        return None
    return {"value": round(statistics.median(vals), 1), "n": len(vals), "device": dev}


# ── prediction ────────────────────────────────────────────────────────────────

def _weight(r: dict, d_to: float, today: date) -> float:
    age = (today - _d(r["date"])).days
    near = math.exp(-math.log(d_to / r["dist_m"]) ** 2 / (2 * DIST_SIGMA ** 2))
    return 0.5 ** (age / RECENCY_HALF_LIFE_DAYS) * near


def _blend(races: list, d_to: float, k: float, today: date) -> tuple:
    """(seconds, [(race, weight)]): each race moved to d_to by Riegel, weighted."""
    num = den = 0.0
    used = []
    for r in races:
        w = _weight(r, d_to, today)
        num += w * riegel(r["time_s"], r["dist_m"], d_to, k)
        den += w
        used.append((r, w))
    return (num / den if den else None), used


def fmt_time(s) -> str:
    s = int(round(s))
    h, m, x = s // 3600, s % 3600 // 60, s % 60
    return f"{h}:{m:02d}:{x:02d}" if h else f"{m}:{x:02d}"


def _when(iso: str) -> str:
    d = _d(iso)
    return d.strftime("%b %Y") if d else iso


def stale(used: list, weekly_km: float | None, recent_break_days: int | None) -> dict | None:
    """{"reason": "break" | "volume", ...} when the races behind a prediction no longer
    describe the athlete (see OUT OF DATE above), else None."""
    if recent_break_days is not None and recent_break_days >= STALE_BREAK_DAYS:
        return {"reason": "break", "days": int(recent_break_days)}
    pairs = [(r["km_before"], w) for r, w in used if r.get("km_before") is not None and w > 0]
    if weekly_km is None or not pairs:
        return None
    before = sum(k * w for k, w in pairs) / sum(w for _, w in pairs)
    if before > 0 and weekly_km < STALE_VOLUME_FRAC * before:
        return {"reason": "volume", "km_before": round(before, 1), "km_now": round(weekly_km, 1)}
    return None


def predict(dist_m: float, races: list, today: date, weekly_km: float | None = None,
            training: list | None = None, recent_break_days: int | None = None) -> dict | None:
    """Race time today for a run of dist_m metres.

    {now_s, method: "races" | "training", model, k, k_pairs, evidence, stale, note}, or
    None when there is nothing to go on (no race in RACE_WINDOW_DAYS and no training
    effort): the caller then falls back to the threshold-pace estimate."""
    recent = [r for r in races if 0 <= (today - _d(r["date"])).days <= RACE_WINDOW_DAYS]
    if not recent:
        return _from_training(dist_m, training, weekly_km)
    kinfo = personal_k(races, today)
    k = kinfo["k"]
    is_marathon = abs(dist_m - 42195.0) < 500
    model = "riegel"
    shorter = [r for r in recent if r["event"] != "marathon"]
    if is_marathon and shorter and weekly_km is not None:
        d2 = max(r["dist_m"] for r in shorter)
        t2, _ = _blend(shorter, d2, k, today)
        miles = weekly_km / KM_PER_MILE
        if kinfo["own"]:
            vick, model = vickers_model2(t2, d2, k, miles), "vickers2"
        else:
            vick, model = vickers_model1(t2, d2, miles), "vickers1"
        marathons = [r for r in recent if r["event"] == "marathon"]
        if marathons:
            tm, _ = _blend(marathons, dist_m, k, today)
            wm = sum(_weight(r, dist_m, today) for r in marathons)
            wv = max(_weight(r, r["dist_m"], today) for r in shorter)
            vick = (tm * wm + vick * wv) / (wm + wv)
        now = vick
        _, used = _blend(recent, dist_m, k, today)
    else:
        now, used = _blend(recent, dist_m, k, today)
    used.sort(key=lambda x: -x[1])
    top = [r for r, _ in used[:3]]
    out = {"now_s": int(round(now)), "method": "races", "model": model, "k": k,
           "k_pairs": kinfo["pairs"],
           "evidence": [{"event": r["event"], "time_s": r["time_s"], "date": r["date"],
                         "name": r.get("name") or "", "source": r.get("source")} for r in top]}
    if model.startswith("vickers"):
        out["weekly_km"] = round(weekly_km, 1)
    out["stale"] = stale(used, weekly_km, recent_break_days)
    out["note"] = _note(out)
    return out


def _from_training(dist_m: float, training: list | None, weekly_km: float | None = None) -> dict | None:
    """No race: the best hard training effort (Run pace curve, last 90 days), by VDOT.
    A marathon still goes through Vickers Model 1, for the same reason as from a race."""
    best = None
    for e in training or []:
        v = vdot(e.get("dist_m"), e.get("time_s"))
        if v and v < 85 and (best is None or v > best[0]):
            best = (v, e)
    if not best:
        return None
    e = best[1]
    if abs(dist_m - 42195.0) < 500 and weekly_km is not None:
        now, model = vickers_model1(e["time_s"], e["dist_m"], weekly_km / KM_PER_MILE), "vickers1"
    else:
        now, model = riegel(e["time_s"], e["dist_m"], dist_m, DEFAULT_K), "riegel"
    out = {"now_s": int(round(now)), "method": "training", "model": model, "k": DEFAULT_K,
           "k_pairs": 0,
           "evidence": [{"event": e.get("event") or "", "time_s": e["time_s"],
                         "date": e.get("date") or "", "name": "", "source": "training"}]}
    out["note"] = (f"No recent race, so from your best hard training effort "
                   f"({_label(e)} {fmt_time(e['time_s'])}). A race would likely be a bit faster.")
    if model == "vickers1":
        out["weekly_km"] = round(weekly_km, 1)
        out["note"] += f" Marathon at your {weekly_km:.0f} km a week (Vickers 2016)."
    return out


def _label(e: dict) -> str:
    ev = e.get("event")
    if ev in LABEL:
        return LABEL[ev]
    km = (e.get("dist_m") or 0) / 1000
    return f"{km:g} km"


def _note(p: dict) -> str:
    ev = ", ".join(f"{_label(e)} {fmt_time(e['time_s'])} ({_when(e['date'])})" for e in p["evidence"][:2])
    k = p["k"]
    st = p.get("stale")
    s = ""
    if st and st["reason"] == "break":
        s = (f"Out of date: {st['days'] // 7} weeks without running recently. "
             f"A parkrun or 5k time trial will update this. ")
    elif st:
        s = (f"Out of date: you're running {st['km_now']:.0f} km a week, against "
             f"{st['km_before']:.0f} before these races. A parkrun or 5k time trial will update this. ")
    if p["k_pairs"]:
        fade = ("more than most" if k > 1.085 else "less than most" if k < 1.055 else "about average")
        s += f"From your races: {ev}. You slow {fade} as races get longer (k {k:.2f})."
    else:
        s += f"From your races: {ev}. Average slowing with distance assumed (k {k:.2f})."
    if p["model"].startswith("vickers"):
        s += f" Marathon at your {p['weekly_km']:.0f} km a week (Vickers 2016)."
    return s
