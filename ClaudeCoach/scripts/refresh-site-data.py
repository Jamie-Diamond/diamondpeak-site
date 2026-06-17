#!/usr/bin/env python3
"""
Pull live data from Intervals.icu and update training-data.json, then push to GitHub.
Run daily (e.g. 06:00 via launchd/cron). Requires git push credentials (SSH key or keychain).
"""
import json, subprocess, sys, time, math
from pathlib import Path
from datetime import datetime, date, timedelta
from collections import defaultdict

BASE             = Path(__file__).parent.parent          # ClaudeCoach/
OUT_FILE         = BASE / "athletes/jamie/training-data.json"  # full private copy (gitignored)
PUB_FILE         = BASE / "training-data.json"                 # public subset (committed to GitHub Pages)
PROJECT_DIR      = str(BASE.parent)                        # diamondpeak-site/
LOCK_FILE        = BASE / ".refresh_site_data.lock"
CLAUDE           = "/usr/bin/claude"
ATHLETES_CONFIG  = BASE / "config/athletes.json"

HEAT_LOG          = BASE / "athletes/jamie/heat-log.json"
DECOUPLING_LOG    = BASE / "athletes/jamie/decoupling-log.json"
STATE_JSON        = BASE / "athletes/jamie/current-state.json"
SESSION_LOG       = BASE / "athletes/jamie/session-log.json"
SWIM_LOG          = BASE / "athletes/jamie/swim-log.json"
FITNESS_PREV_CACHE = BASE / "athletes/jamie/fitness-prev-cache.json"

RACE_DATE = date(2026, 9, 19)
PLAN_START = date(2026, 4, 27)  # Week 1 Monday

_POWER_DURATIONS = [
    (5,"5s"),(10,"10s"),(30,"30s"),(60,"1m"),(120,"2m"),(300,"5m"),
    (600,"10m"),(1200,"20m"),(1800,"30m"),(3600,"60m"),(5400,"90m"),(7200,"2h"),
]


def _eftp_from_fitness(fitness_rows: list) -> int | None:
    """Extract eFTP (W) for Ride from the most recent fitness row's sportInfo. Returns None if unavailable."""
    row = fitness_rows[-1] if fitness_rows else {}
    for s in (row.get("sportInfo") or []):
        if s.get("type") == "Ride" and s.get("eftp"):
            return int(s["eftp"])
    return None


def _has_recent_ftp_test(session_log_path: Path, weeks: int = 10) -> bool:
    """Return True if a named FTP test appears in session-log.json within the last N weeks."""
    if not session_log_path.exists():
        return False
    cutoff = (date.today() - timedelta(weeks=weeks)).isoformat()
    keywords = ("ramp", "ftp test", "20 min", "20-min", "threshold test")
    try:
        for e in json.loads(session_log_path.read_text()):
            if e.get("date", "") >= cutoff and e.get("sport") in ("Ride", "VirtualRide"):
                if any(kw in (e.get("name") or "").lower() for kw in keywords):
                    return True
    except Exception:
        pass
    return False


def _resolve_ftp(profile_ftp: int | None, fitness_rows: list, session_log_path: Path) -> int:
    """Use profile FTP if a confirmed test exists in last 10 weeks, else ICU eFTP, else profile."""
    if not _has_recent_ftp_test(session_log_path):
        eftp = _eftp_from_fitness(fitness_rows)
        if eftp:
            return eftp
    return profile_ftp or 250


def fetch_fitness_prev(client):
    """Fetch 2025 CTL series once and cache it. Skips if cache already exists."""
    if FITNESS_PREV_CACHE.exists():
        return
    log("Fetching last-season fitness (one-time cache)...")
    try:
        rows = client.get_fitness(start_date="2025-01-01", end_date="2025-09-19")
        series = [[r["id"][:10], round(r.get("ctl") or 0, 1)] for r in rows if r.get("ctl")]
        FITNESS_PREV_CACHE.write_text(json.dumps(series))
        log(f"fitnessPrev cached: {len(series)} days")
    except Exception as e:
        log(f"fitnessPrev fetch failed (non-fatal): {e}")


def _build_jamie_data(client) -> dict:
    """Fetch Jamie's training data via IcuClient — replaces the old Claude+MCP approach."""
    today         = date.today()
    fourteen_ago  = (today - timedelta(days=14)).isoformat()
    seven_ago     = (today - timedelta(days=7)).isoformat()
    twentyone_fwd = (today + timedelta(days=21)).isoformat()

    wellness_60, history_21, events_21, fitness_ytd = client.fetch_all(
        ("get_wellness", 60),
        ("get_training_history", 21),
        ("get_events", today.isoformat(), twentyone_fwd),
        ("get_fitness", (today - date(today.year, 1, 1)).days + 1),
    )

    # KPI
    kpi = {}
    if wellness_60:
        w      = wellness_60[-1]
        ctl    = round(w.get("ctl") or 0, 1)
        atl    = round(w.get("atl") or 0, 1)
        ramp7d = round(ctl - round(wellness_60[-8].get("ctl") or 0, 1), 1) if len(wellness_60) >= 8 else 0.0
        kpi    = {"ctl": ctl, "atl": atl, "tsb": round(ctl - atl, 1), "ramp7d": ramp7d,
                  "hrv": w.get("hrv"), "rhr": w.get("restingHR")}

    # fitnessThis
    fitness_this = [[w["id"][:10], round(w.get("ctl") or 0, 1)] for w in fitness_ytd if w.get("ctl")]

    # recent (last 14 days, newest first)
    recent = []
    for a in sorted([x for x in history_21 if x.get("start_date_local","")[:10] >= fourteen_ago],
                    key=lambda x: x.get("start_date_local",""), reverse=True):
        sport  = _sport_normalise(a.get("type","Other"))
        dist_m = a.get("distance") or 0
        dur_s  = a.get("moving_time") or 0
        avg_p  = a.get("average_watts")
        norm_p = a.get("icu_weighted_avg_watts")
        recent.append({
            "date":   a.get("start_date_local","")[:10],
            "sport":  sport,
            "name":   a.get("name",""),
            "dur":    round(dur_s / 60),
            "dist":   round(dist_m / 1000, 2) if dist_m else None,
            "pace":   _format_pace(sport, dist_m, dur_s),
            "hr":     int(a["average_heartrate"]) if a.get("average_heartrate") else None,
            "powAvg": int(avg_p)  if avg_p  else None,
            "powNp":  int(norm_p) if norm_p else None,
            "tss":    int(a.get("icu_training_load") or 0),
        })

    # weekCalendar (last 7 days completed + next 21 days planned)
    completed_by_date: dict = defaultdict(list)
    for a in history_21:
        d = a.get("start_date_local","")[:10]
        if d >= seven_ago:
            completed_by_date[d].append(a)

    week_calendar = []
    for a in sorted(history_21, key=lambda x: x.get("start_date_local","")):
        d = a.get("start_date_local","")[:10]
        if d < seven_ago:
            continue
        sport  = _sport_normalise(a.get("type","Other"))
        dist_m = a.get("distance") or 0
        dur_s  = a.get("moving_time") or 0
        tss    = int(a.get("icu_training_load") or 0)
        avg_p  = a.get("average_watts")
        norm_p = a.get("icu_weighted_avg_watts")
        if sport == "Ride":
            detail = " · ".join(filter(None, [
                f"NP {int(norm_p)}W" if norm_p else None,
                f"HR {int(a['average_heartrate'])}" if a.get("average_heartrate") else None,
                f"{dist_m/1000:.1f}km" if dist_m else None,
            ]))
        elif sport in ("Run","Swim"):
            detail = " · ".join(filter(None,[_format_pace(sport,dist_m,dur_s),
                                              f"{dist_m/1000:.1f}km" if dist_m else None]))
        else:
            detail = ""
        week_calendar.append({"date":d,"sport":sport,"name":a.get("name",""),
                               "tss":tss,"duration_min":round(dur_s/60),
                               "status":"completed","key":tss>=60,"detail":detail})

    completed_dates = set(completed_by_date.keys())
    for ev in events_21:
        ev_date = (ev.get("start_date_local") or "")[:10]
        if not ev_date or ev_date < today.isoformat():
            continue
        ev_sport = _sport_normalise(ev.get("type") or ev.get("sport_type") or "Other")
        if any(_sport_normalise(a.get("type","")) == ev_sport
               for a in completed_by_date.get(ev_date,[])):
            continue
        ev_tss = ev.get("load_target") or ev.get("icu_training_load") or ev.get("load")
        ev_dur = ev.get("moving_time") or ev.get("duration")
        week_calendar.append({"date":ev_date,"sport":ev_sport,"name":ev.get("name",""),
                               "tss":int(ev_tss) if ev_tss else None,
                               "duration_min":round(int(ev_dur)/60) if ev_dur else None,
                               "status":"planned","key":bool(ev_tss and int(ev_tss)>=60),"detail":""})
    week_calendar.sort(key=lambda x: x["date"])

    # loadChart (today−7 to today+7, 15 days)
    tsb_by_date = {w.get("id","")[:10]: round((w.get("ctl") or 0)-(w.get("atl") or 0),1)
                   for w in wellness_60 if w.get("id")}
    load_chart = []
    for i in range(-7, 8):
        d    = (today + timedelta(days=i)).isoformat()
        acts = [{"sport":_sport_normalise(a.get("type","Other")),
                 "tss":int(a.get("icu_training_load") or 0),
                 "dur":round((a.get("moving_time") or 0)/60),
                 "status":"completed"}
                for a in history_21 if a.get("start_date_local","")[:10]==d]
        if i >= 0:   # include TODAY's planned sessions, not just future days
            for ev in events_21:
                ev_d     = (ev.get("start_date_local") or "")[:10]
                ev_sport = _sport_normalise(ev.get("type") or ev.get("sport_type") or "Other")
                if ev_d != d or any(a["sport"]==ev_sport for a in acts):
                    continue
                # Planned TSS lives in load_target; icu_training_load/load are null on planned events.
                ev_tss = ev.get("load_target") or ev.get("icu_training_load") or ev.get("load")
                ev_dur = ev.get("moving_time") or ev.get("duration")
                acts.append({"sport":ev_sport,"tss":int(ev_tss) if ev_tss else None,
                              "dur":round(int(ev_dur)/60) if ev_dur else None,"status":"planned"})
        load_chart.append({"date":d,"tsb":tsb_by_date.get(d),"activities":acts})

    # weightTrend (last 30 days where weight not null)
    weight_trend = [{"date":w.get("id","")[:10],"kg":w["weight"]}
                    for w in wellness_60 if w.get("weight")]

    # power curve (90-day best efforts at standard durations)
    power_curve = []
    try:
        pc_raw = client.get_power_curves(sport="Ride", curves="90d")
        if pc_raw.get("list"):
            curve        = pc_raw["list"][0]
            secs_to_w    = dict(zip(curve.get("secs",[]), curve.get("values",[])))
            power_curve  = [{"t":t,"label":lbl,"w":secs_to_w.get(t),"wPrev":None}
                             for t, lbl in _POWER_DURATIONS]
    except Exception as e:
        log(f"Power curve fetch failed (non-fatal): {e}")

    # Resolve FTP here while raw fitness rows are in scope; post_process (which builds
    # the profile block) cannot see fitness_ytd, so it reads this stashed value instead.
    _prof_f = BASE / "athletes/jamie/profile.json"
    _prof_ftp = None
    if _prof_f.exists():
        try:
            _prof_ftp = json.loads(_prof_f.read_text()).get("ftp_watts")
        except Exception:
            pass
    resolved_ftp = _resolve_ftp(_prof_ftp, fitness_ytd, SESSION_LOG)

    return {
        "generated":    today.isoformat(),
        "kpi":          kpi,
        "fitnessThis":  fitness_this,
        "recent":       recent,
        "weekCalendar": week_calendar,
        "loadChart":    load_chart,
        "weightTrend":  weight_trend,
        "powerCurve":   power_curve,
        "resolvedFtp":  resolved_ftp,
    }


def _strip_private(data):
    """Remove personal health data before writing to the public file."""
    pub = {k: v for k, v in data.items()}
    pub.pop("sessionLog", None)
    pub.pop("weightTrend", None)
    if "currentState" in pub:
        cs = {k: v for k, v in pub["currentState"].items()}
        cs.pop("ankle_pain_during", None)
        cs.pop("ankle_pain_next_morning", None)
        cs.pop("weight_readings", None)
        pub["currentState"] = cs
    return pub


def log(msg):
    print(f"{datetime.now().strftime('%H:%M:%S')} {msg}")


def _ctl_project(start_ctl, daily_tss_fn, days):
    """Project CTL forward using exponential decay: CTL_new = CTL + (TSS - CTL) / 42."""
    ctl = start_ctl
    series = []
    today = date.today()
    for i in range(days):
        d = today + timedelta(days=i)
        tss = daily_tss_fn(d)
        ctl = ctl + (tss - ctl) / 42.0
        series.append({"date": d.isoformat(), "ctl": round(ctl, 1)})
    return series


def _ctl_target_milestones(athlete_cfg, current_ctl, today):
    """Phase CTL milestones the planner is aiming for — the SINGLE SOURCE shared by
    the Jamie (post_process) and generic athlete paths, so the site target line can
    never drift from the plan. Uses configured ctl_targets.phase_ctl when present
    (e.g. Jamie), else derives from race_min (mirrors plan_tools exactly, e.g.
    Kathryn/Calum). Returns a list of {date,ctl,label} or None if no CTL basis."""
    ct = athlete_cfg.get("ctl_targets") or {}
    pt = athlete_cfg.get("phase_tss") or {}
    plan_start_str = athlete_cfg.get("plan_start")
    race_str = athlete_cfg.get("race_date")
    if not (plan_start_str and race_str and (ct.get("phase_ctl") or ct.get("race_min"))):
        return None
    plan_start = date.fromisoformat(plan_start_str)
    race_dt    = date.fromisoformat(race_str)
    ends = {"base": pt.get("base_end_week", 6), "build": pt.get("build_end_week", 10),
            "specific": pt.get("specific_end_week", 14), "peak": pt.get("peak_end_week", 17)}
    phase_ctl = ct.get("phase_ctl")
    if phase_ctl:
        derived = {k: phase_ctl.get(k) for k in ("base", "build", "specific", "peak")}
    else:
        sys.path.insert(0, str(BASE / "ironman-analysis"))
        from primitives.load import derive_phase_ctl_targets
        derived = derive_phase_ctl_targets(
            current_ctl, int(ct["race_min"]), plan_start,
            ends["base"], ends["build"], ends["specific"], ends["peak"],
            float(athlete_cfg.get("max_ctl_ramp_per_week", 5.0)),
            float(athlete_cfg.get("taper_overshoot", 1.15)), today=today)
    ms = {}
    for label, key in (("End Base", "base"), ("End Build", "build"),
                       ("Specific", "specific"), ("Peak", "peak")):
        if derived.get(key) is None:
            continue
        md = plan_start + timedelta(weeks=ends[key])
        # First-write wins so collapsed phases keep the earlier, clearer label.
        if today <= md <= race_dt and md.isoformat() not in ms:
            ms[md.isoformat()] = {"date": md.isoformat(), "ctl": derived[key], "label": label}
    race_ctl = ct.get("race_min") or derived.get("peak")
    if race_ctl is not None:
        ms[race_dt.isoformat()] = {"date": race_dt.isoformat(),
                                   "ctl": int(race_ctl), "label": "Race day"}
    return sorted(ms.values(), key=lambda m: m["date"]) or None


def _parse_hm(s):
    """'4:55' -> 295 min (4h55m); '1:09' -> 69; '3:52' -> 232."""
    try:
        h, m = str(s).split(":"); return int(h) * 60 + int(m)
    except Exception:
        return None


def _parse_pace_s(s):
    """'4:02' -> 242 (seconds per km)."""
    try:
        m, sec = str(s).split(":"); return int(m) * 60 + int(sec)
    except Exception:
        return None


def _race_predictor(profile, current_ctl):
    """3-scenario IM race predictor.

    Science (the athlete's own framing): fitness = CTL = the capacity to absorb TSS;
    race TSS = hours x IF^2 x 100, so for a FIXED-distance event the sustainable
    intensity factor scales as IF ∝ √CTL. FTP and run threshold are held FIXED — the
    only lever between "now", "race day" and "target" is CTL (→ IF). Anchored entirely
    to the athlete's previous race (real IF, CTL, power, splits); bike speed scales as
    v ∝ NP^(1/3) (aero-dominated, same course). Returns None if inputs are missing."""
    import math
    pr  = profile.get("prev_race") or {}
    cfg = profile.get("race_predictor") or {}
    ftp = profile.get("ftp_watts")
    thr = _parse_pace_s(profile.get("run_threshold_pace_per_km"))
    anchor_if  = pr.get("bike_if")
    anchor_ctl = cfg.get("anchor_ctl")
    anchor_np  = pr.get("bike_np_watts")
    bike_km    = cfg.get("bike_km", 180.0)
    bike_anchor_min = _parse_hm(pr.get("bike_time"))
    swim_min   = _parse_hm(pr.get("swim_time"))
    run_anchor_min = _parse_hm(pr.get("run_time"))
    t12 = cfg.get("t1t2_min", 10)
    if not all([ftp, thr, anchor_if, anchor_ctl, anchor_np, bike_km,
                bike_anchor_min, swim_min, current_ctl]):
        return None
    v_ref = bike_km / (bike_anchor_min / 60.0)   # km/h at anchor NP
    scenarios = [
        ("If I did it now", float(current_ctl)),
        ("Race day",        float(cfg.get("raceday_ctl", anchor_ctl))),
        ("Target",          float(cfg.get("target_ctl", anchor_ctl))),
    ]
    rows = []
    for label, ctl in scenarios:
        IF   = anchor_if * math.sqrt(ctl / anchor_ctl)
        npw  = round(ftp * IF)
        v    = v_ref * (npw / anchor_np) ** (1 / 3.0)
        bmin = bike_km / v * 60
        rmin = 42.2 * (thr / IF) / 60
        rows.append({"label": label, "ctl": round(ctl), "if": round(IF, 3),
                     "bike_w": npw, "bike_min": round(bmin), "run_min": round(rmin),
                     "swim_min": round(swim_min), "t12_min": t12,
                     "total_min": round(bmin + rmin + swim_min + t12)})
    return {"rows": rows, "anchor": {
        "name": pr.get("name", "Last year"), "ctl": round(anchor_ctl),
        "if": anchor_if, "bike_w": anchor_np, "bike_min": round(bike_anchor_min),
        "run_min": run_anchor_min, "swim_min": round(swim_min), "t12_min": t12,
        "total_min": round(swim_min + bike_anchor_min + (run_anchor_min or 0) + t12)}}


def _phase_daily_tss(d):
    """Return planned daily TSS based on phase (week number from PLAN_START).
    Calibrated to 2025 actuals (spring ~110/day, peak ~133/day).
    Projects peak CTL ~123, race-day CTL ~105 from current ~79 — exceeding 2025.
    - Base (wk 1-6):    105/day = 735/wk
    - Build (wk 7-10):  112/day = 784/wk
    - Specific (wk 11-14): 122/day = 854/wk
    - Peak (wk 15-18):  135/day = 945/wk  (4 weeks, ends ~Aug 30)
    - Taper (wk 19+):    75/day = 525/wk  (matches 2025 actual ~76/day)"""
    week = max(1, math.ceil((d - PLAN_START).days / 7))
    if week <= 6:    return 105   # Base: ~735/wk
    if week <= 10:   return 112   # Build: ~784/wk
    if week <= 14:   return 122   # Specific: ~854/wk
    if week <= 18:   return 135   # Peak: ~945/wk
    return 75                     # Taper: ~525/wk — matches 2025 actual


def post_process(data):
    """Add heat, decoupling, and CTL projection fields to the training-data dict."""
    # Heat protocol
    heat_entries = json.loads(HEAT_LOG.read_text()) if HEAT_LOG.exists() else []
    today = date.today()
    week_start = today - timedelta(days=today.weekday())
    this_week = [e for e in heat_entries if e.get("date", "") >= week_start.isoformat()]
    last_date = max((e["date"] for e in heat_entries), default=None)
    data["heatProtocol"] = {
        "sessions_cumulative": len(heat_entries),
        "sessions_this_week": len(this_week),
        "last_session_date": last_date,
        "protocol_start_date": "2026-05-15",
        "target_min": 14,
        "target_max": 20,
    }

    # Last-season CTL overlay (cached once — 2025 data never changes)
    if FITNESS_PREV_CACHE.exists():
        try:
            data["fitnessPrev"] = json.loads(FITNESS_PREV_CACHE.read_text())
        except Exception:
            pass

    fitness_2023_cache = BASE / "athletes/jamie/fitness-2023-cache.json"
    if fitness_2023_cache.exists():
        try:
            data["fitnessPrev2"] = json.loads(fitness_2023_cache.read_text())
        except Exception:
            pass

    # Decoupling trend
    dcoup = json.loads(DECOUPLING_LOG.read_text()) if DECOUPLING_LOG.exists() else []
    data["decouplingTrend"] = sorted(dcoup, key=lambda e: e.get("date", ""))

    # CTL projection
    current_ctl = data["kpi"]["ctl"]
    ramp7d = data["kpi"]["ramp7d"]
    days_to_race = (RACE_DATE - today).days + 1

    def current_trend_tss(d):
        return max(0, current_ctl + ramp7d / 7)  # extend current ramp

    # planned_sessions: use actual planned event TSS from weekCalendar for the
    # next 14 days, then fall back to phase averages beyond the known window
    planned_tss_by_date = {}
    completed_dates = set()
    for e in data.get("weekCalendar", []):
        d_str = e.get("date", "")
        if e.get("status") == "completed":
            completed_dates.add(d_str)
        elif e.get("status") == "planned":
            planned_tss_by_date[d_str] = planned_tss_by_date.get(d_str, 0) + (e.get("tss") or 0)
    known_window_end = today + timedelta(days=14)

    def planned_sessions_tss(d):
        d_str = d.isoformat()
        # If there's already a completed activity on this date, current_ctl
        # already reflects it — don't add planned TSS on top.
        if d_str in completed_dates:
            return 0
        # Within known window: use actual planned session TSS (0 = rest day)
        # Beyond known window: 0 (nothing booked yet — CTL decays honestly)
        if d <= known_window_end:
            return planned_tss_by_date.get(d_str, 0)
        return 0

    sick_week_num = 10
    def sick_week_tss(d):
        week = max(1, math.ceil((d - PLAN_START).days / 7))
        return 0 if week == sick_week_num else _phase_daily_tss(d)

    # Jamie's config (phase_ctl) drives the Target CTL line — same helper as every
    # other athlete, so the displayed target tracks athletes.json automatically.
    try:
        _jamie_cfg = json.loads((BASE / "config/athletes.json").read_text()).get("jamie", {})
    except Exception:
        _jamie_cfg = {}
    data["ctlProjection"] = {
        "current_trend":    _ctl_project(current_ctl, current_trend_tss, days_to_race),
        "planned_build":    _ctl_project(current_ctl, _phase_daily_tss, days_to_race),
        "planned_sessions": _ctl_project(current_ctl, planned_sessions_tss, days_to_race),
        "sick_week":        _ctl_project(current_ctl, sick_week_tss, days_to_race),
        "target_milestones": _ctl_target_milestones(_jamie_cfg, current_ctl, today),
        "race_date": RACE_DATE.isoformat(),
        "target_ctl_min": 105,
        "target_ctl_max": 115,
    }

    # Race predictor — Now / Race day / Target, from IF ∝ √CTL (see _race_predictor).
    try:
        _prof = json.loads((BASE / "athletes/jamie/profile.json").read_text())
        _rp = _race_predictor(_prof, current_ctl)
        if _rp:
            data["racePredictor"] = _rp
    except Exception as exc:
        log(f"[jamie] racePredictor skipped: {exc}")

    # Profile fields needed by the dashboard (goals, thresholds)
    profile_f = BASE / "athletes/jamie/profile.json"
    if profile_f.exists():
        try:
            prof = json.loads(profile_f.read_text())
            data["profile"] = {
                "a_goal":                    prof.get("a_goal"),
                "b_goal":                    prof.get("b_goal"),
                "swim_css_per_100m":         prof.get("swim_css_per_100m"),
                "run_threshold_pace_per_km": prof.get("run_threshold_pace_per_km"),
                "lthr":                      prof.get("lthr"),
                "ftp_watts":                 data.get("resolvedFtp") or prof.get("ftp_watts"),
                "weight_kg":                 prof.get("weight_kg"),
                "race_distance":             prof.get("race_distance"),
                "race_date":                 prof.get("race_date"),
                "race_name":                 prof.get("race_name"),
                "prev_race":                 prof.get("prev_race"),
                "prev_race_date":            prof.get("prev_race_date"),
                "prev2_race_date":           prof.get("prev2_race_date"),
                "prev2_race_name":           prof.get("prev2_race_name"),
                "race_targets":              prof.get("race_targets"),
            }
        except Exception as e:
            # Loud, not silent: an empty profile makes the whole zones/race-scenario
            # panel vanish from the athlete page (regression: 2026-06-07).
            log(f"PROFILE BUILD FAILED — athlete page zones/race panel will be empty: {e}")

    # Current state snapshot (ankle, watchdog flags, open actions)
    if STATE_JSON.exists():
        try:
            cs = json.loads(STATE_JSON.read_text())
            data["currentState"] = {
                "ankle_pain_during": cs.get("ankle", {}).get("pain_during"),
                "ankle_pain_next_morning": cs.get("ankle", {}).get("pain_next_morning"),
                "bike_ftp": cs.get("bike_ftp"),
                "watchdog_flags": cs.get("watchdog_flags", []),
                "open_actions": cs.get("open_actions", []),
                "weight_readings": cs.get("weight_readings", [])[-5:],
            }
        except Exception:
            pass

    # Weekly discipline breakdown (from athlete-summary.json if available)
    athlete_summary_f = BASE / "athletes/jamie/athlete-summary.json"
    if athlete_summary_f.exists():
        try:
            summary = json.loads(athlete_summary_f.read_text())
            data["weeklyBreakdown"] = summary.get("weeks", [])
            data["swimProgression"] = summary.get("swim_progression", [])
        except Exception:
            pass

    # Session log — last 10 confirmed (non-stub) entries
    if SESSION_LOG.exists():
        try:
            all_entries = json.loads(SESSION_LOG.read_text())
            confirmed = [e for e in all_entries if not e.get("stub", True)]
            data["sessionLog"] = confirmed[-10:]
        except Exception:
            pass

    # Swim log — full history for progression chart
    if SWIM_LOG.exists():
        try:
            data["swimLog"] = json.loads(SWIM_LOG.read_text())
        except Exception:
            pass

    # Progress charts — cycling NP/VI, run pace/EF, fuelling g/hr
    if SESSION_LOG.exists():
        try:
            all_s = json.loads(SESSION_LOG.read_text())
            # Drop double-uploaded activities (same date+sport+distance+duration under
            # different ICU ids) — keep whichever entry has the most fields filled in.
            best = {}
            for s in all_s:
                k = (s.get("date"), s.get("sport"),
                     round(float(s.get("distance_km") or 0), 1),
                     int(s.get("duration_min") or 0))
                cur = best.get(k)
                if cur is None or sum(v is not None for v in s.values()) > sum(v is not None for v in cur.values()):
                    best[k] = s
            all_s = list(best.values())
            long_rides = sorted(
                [s for s in all_s if s.get("sport") == "Ride"
                 and s.get("norm_power") and s.get("avg_power")
                 and int(s.get("duration_min") or 0) >= 150],   # rides > 2.5 h only
                key=lambda x: x["date"]
            )
            hr_runs = sorted(
                [s for s in all_s if s.get("sport") == "Run"
                 and s.get("avg_hr") and s.get("distance_km")
                 and int(s.get("duration_min") or 0) >= 60],     # runs > 60 min only
                key=lambda x: x["date"]
            )
            carb_s = sorted(
                [s for s in all_s if s.get("nutrition_g_carb") and s.get("duration_min")],
                key=lambda x: x["date"]
            )
            ftp = (data.get("profile") or {}).get("ftp_watts") or 316
            data["progressData"] = {
                "ftp": ftp,
                "rides": [
                    {"date": s["date"], "np": s["norm_power"],
                     "vi": round(s["norm_power"] / s["avg_power"], 3),
                     "hr": s.get("avg_hr"), "dur": s.get("duration_min"),
                     "name": (s.get("name") or "")[:40]}
                    for s in long_rides
                ],
                "runs": [
                    {"date": s["date"],
                     "pace": round(float(s["duration_min"]) / float(s["distance_km"]), 3),
                     "ef": round(float(s["distance_km"]) * 1000 / float(s["duration_min"]) / float(s["avg_hr"]), 4),
                     "hr": s.get("avg_hr"), "dist": round(float(s["distance_km"]), 1),
                     "name": (s.get("name") or "")[:40]}
                    for s in hr_runs
                ],
                "carb": [
                    {"date": s["date"],
                     "g_per_hr": round(float(s["nutrition_g_carb"]) / float(s["duration_min"]) * 60, 1),
                     "sport": s.get("sport"), "dur": s.get("duration_min"),
                     "name": (s.get("name") or "")[:40]}
                    for s in carb_s
                ],
            }
        except Exception:
            pass

    # Plan vs actual — last 6 weeks, grouped by week
    # Actual TSS from session-log.json; planned from phase daily TSS * 7
    if SESSION_LOG.exists():
        try:
            all_entries = json.loads(SESSION_LOG.read_text())
            weekly_actual = defaultdict(float)
            seen_ids: set = set()
            for e in all_entries:
                aid = e.get("activity_id")
                if aid:
                    if aid in seen_ids:
                        continue
                    seen_ids.add(aid)
                d_str = e.get("date", "")
                if not d_str:
                    continue
                dt = date.fromisoformat(d_str)
                wk_start = dt - timedelta(days=dt.weekday())
                weekly_actual[wk_start.isoformat()] += e.get("tss") or 0

            plan_actual = []
            for i in range(5, -1, -1):
                wk_start = today - timedelta(days=today.weekday()) - timedelta(weeks=i)
                wk_num = max(1, math.ceil((wk_start - PLAN_START).days / 7))
                planned_tss = _phase_daily_tss(wk_start) * 7
                plan_actual.append({
                    "week_start": wk_start.isoformat(),
                    "week_num": wk_num,
                    "actual_tss": round(weekly_actual.get(wk_start.isoformat(), 0)),
                    "planned_tss": round(planned_tss),
                })
            data["planVsActual"] = plan_actual
        except Exception:
            pass

    return data


def _sport_normalise(raw):
    return {"VirtualRide": "Ride", "GravelRide": "Ride", "VirtualRun": "Run", "TrailRun": "Run"}.get(raw, raw)


def _format_pace(sport, dist_m, duration_s):
    if sport == "Ride" and dist_m and duration_s:
        return f"{dist_m / 1000 / (duration_s / 3600):.1f} kph"
    if sport == "Run" and dist_m and duration_s:
        spm = duration_s / (dist_m / 1000)
        return f"{int(spm)//60}:{int(spm)%60:02d}/km"
    if sport == "Swim" and dist_m and duration_s:
        spc = duration_s / (dist_m / 100)
        return f"{int(spc)//60}:{int(spc)%60:02d}/100m"
    return None


def _build_athlete_training_data(slug, athlete_cfg):
    """Build training-data-{slug}.json using IcuClient (Python only — no Claude call)."""
    sys.path.insert(0, str(BASE / "lib"))
    from icu_api import IcuClient

    today = date.today()
    client = IcuClient(athlete_cfg["icu_athlete_id"], athlete_cfg["icu_api_key"])

    seven_ago  = (today - timedelta(days=7)).isoformat()
    fourteen_ago = (today - timedelta(days=14)).isoformat()
    seven_fwd  = (today + timedelta(days=7)).isoformat()
    twentyone_fwd = (today + timedelta(days=21)).isoformat()
    year_start = f"{today.year}-01-01"

    # Parallel fetch
    wellness_60, history_21, events_21, fitness_ytd = client.fetch_all(
        ("get_wellness", 60),
        ("get_training_history", 21),
        ("get_events", today.isoformat(), twentyone_fwd),
        ("get_fitness", (today - date(today.year, 1, 1)).days + 1),
    )

    # -- kpi ------------------------------------------------------------------
    kpi = {}
    if wellness_60:
        w = wellness_60[-1]
        ctl = round(w.get("ctl") or 0, 1)
        atl = round(w.get("atl") or 0, 1)
        ramp7d = round(ctl - round(wellness_60[-8].get("ctl") or 0, 1), 1) if len(wellness_60) >= 8 else 0
        kpi = {"ctl": ctl, "atl": atl, "tsb": round(ctl - atl, 1), "ramp7d": ramp7d,
               "hrv": w.get("hrv"), "rhr": w.get("restingHR")}

    # -- fitnessThis -----------------------------------------------------------
    fitness_this = [[w["id"][:10], round(w.get("ctl") or 0, 1)] for w in fitness_ytd if w.get("ctl")]

    # -- recent (last 14 days) -------------------------------------------------
    recent = []
    for a in sorted([x for x in history_21 if x.get("start_date_local", "")[:10] >= fourteen_ago],
                    key=lambda x: x.get("start_date_local", ""), reverse=True):
        sport = _sport_normalise(a.get("type", "Other"))
        dist_m = a.get("distance") or 0
        dur_s  = a.get("moving_time") or 0
        dur    = round(dur_s / 60)
        avg_p  = a.get("average_watts")
        norm_p = a.get("icu_weighted_avg_watts")
        recent.append({
            "date":   a.get("start_date_local", "")[:10],
            "sport":  sport,
            "name":   a.get("name", ""),
            "dur":    dur,
            "dist":   round(dist_m / 1000, 2) if dist_m else None,
            "pace":   _format_pace(sport, dist_m, dur_s),
            "hr":     int(a["average_heartrate"]) if a.get("average_heartrate") else None,
            "powAvg": int(avg_p) if avg_p else None,
            "powNp":  int(norm_p) if norm_p else None,
            "tss":    int(a.get("icu_training_load") or 0),
        })

    # -- weekCalendar (last 7 days + next 14 days) -----------------------------
    completed_by_date: dict[str, list] = defaultdict(list)
    for a in history_21:
        d = a.get("start_date_local", "")[:10]
        if d >= seven_ago:
            completed_by_date[d].append(a)

    week_calendar = []
    for a in sorted(history_21, key=lambda x: x.get("start_date_local", "")):
        d = a.get("start_date_local", "")[:10]
        if d < seven_ago:
            continue
        sport = _sport_normalise(a.get("type", "Other"))
        dist_m = a.get("distance") or 0
        dur_s  = a.get("moving_time") or 0
        tss    = int(a.get("icu_training_load") or 0)
        avg_p  = a.get("average_watts")
        norm_p = a.get("icu_weighted_avg_watts")
        if sport == "Ride":
            detail = " · ".join(filter(None, [
                f"NP {int(norm_p)}W" if norm_p else None,
                f"HR {int(a['average_heartrate'])}" if a.get("average_heartrate") else None,
                f"{dist_m/1000:.1f}km" if dist_m else None,
            ]))
        elif sport in ("Run", "Swim"):
            detail = " · ".join(filter(None, [
                _format_pace(sport, dist_m, dur_s),
                f"{dist_m/1000:.1f}km" if dist_m else None,
            ]))
        else:
            detail = ""
        week_calendar.append({
            "date": d, "sport": sport, "name": a.get("name", ""),
            "tss": tss, "duration_min": round(dur_s / 60),
            "status": "completed", "key": tss >= 60, "detail": detail,
        })

    completed_dates = set(completed_by_date.keys())
    for ev in events_21:
        ev_date = (ev.get("start_date_local") or "")[:10]
        if not ev_date or ev_date < today.isoformat():
            continue
        ev_sport = _sport_normalise(ev.get("type") or ev.get("sport_type") or "Other")
        # Skip if there's already a completed activity of same sport on that date
        if any(_sport_normalise(a.get("type", "")) == ev_sport
               for a in completed_by_date.get(ev_date, [])):
            continue
        ev_tss = ev.get("icu_training_load") or ev.get("load")
        ev_dur = ev.get("moving_time") or ev.get("duration")
        week_calendar.append({
            "date": ev_date, "sport": ev_sport, "name": ev.get("name", ""),
            "tss": int(ev_tss) if ev_tss else None,
            "duration_min": round(int(ev_dur) / 60) if ev_dur else None,
            "status": "planned", "key": bool(ev_tss and int(ev_tss) >= 60), "detail": "",
        })
    week_calendar.sort(key=lambda x: x["date"])

    # -- loadChart (today−7 to today+7, 15 days) -------------------------------
    tsb_by_date = {}
    for w in wellness_60:
        d = w.get("id", "")[:10]
        ctl = w.get("ctl") or 0
        atl = w.get("atl") or 0
        if d:
            tsb_by_date[d] = round(ctl - atl, 1)

    load_chart = []
    for i in range(-7, 8):
        d = (today + timedelta(days=i)).isoformat()
        acts = []
        for a in history_21:
            if a.get("start_date_local", "")[:10] == d:
                acts.append({
                    "sport": _sport_normalise(a.get("type", "Other")),
                    "tss":   int(a.get("icu_training_load") or 0),
                    "dur":   round((a.get("moving_time") or 0) / 60),
                    "status": "completed",
                })
        if i > 0:
            for ev in events_21:
                ev_d = (ev.get("start_date_local") or "")[:10]
                if ev_d != d:
                    continue
                ev_sport = _sport_normalise(ev.get("type") or ev.get("sport_type") or "Other")
                if any(a["sport"] == ev_sport for a in acts):
                    continue
                ev_tss = ev.get("icu_training_load") or ev.get("load")
                ev_dur = ev.get("moving_time") or ev.get("duration")
                acts.append({
                    "sport": ev_sport,
                    "tss":   int(ev_tss) if ev_tss else None,
                    "dur":   round(int(ev_dur) / 60) if ev_dur else None,
                    "status": "planned",
                })
        load_chart.append({"date": d, "tsb": tsb_by_date.get(d), "activities": acts})

    # -- session log + swim log from local files -------------------------------
    session_log = []
    sl_file = BASE / f"athletes/{slug}/session-log.json"
    if sl_file.exists():
        try:
            all_e = json.loads(sl_file.read_text())
            session_log = [e for e in all_e if not e.get("stub", True)][-10:]
        except Exception:
            pass

    swim_log = []
    sw_file = BASE / f"athletes/{slug}/swim-log.json"
    if sw_file.exists():
        try:
            swim_log = json.loads(sw_file.read_text())
        except Exception:
            pass

    data = {
        "generated":    today.isoformat(),
        "kpi":          kpi,
        "fitnessThis":  fitness_this,
        "recent":       recent,
        "weekCalendar": week_calendar,
        "loadChart":    load_chart,
        "sessionLog":   session_log,
        "swimLog":      swim_log,
    }

    # Previous season CTL overlay (if cache exists for this athlete)
    prev_cache = BASE / f"athletes/{slug}/fitness-prev-cache.json"
    if prev_cache.exists():
        try:
            data["fitnessPrev"] = json.loads(prev_cache.read_text())
        except Exception:
            pass

    prev2_cache = BASE / f"athletes/{slug}/fitness-2023-cache.json"
    if prev2_cache.exists():
        try:
            data["fitnessPrev2"] = json.loads(prev2_cache.read_text())
        except Exception:
            pass

    # Profile (goals + thresholds)
    profile_f = BASE / f"athletes/{slug}/profile.json"
    if profile_f.exists():
        try:
            prof = json.loads(profile_f.read_text())
            session_log_f = BASE / f"athletes/{slug}/session-log.json"
            data["profile"] = {
                "a_goal":                    prof.get("a_goal"),
                "b_goal":                    prof.get("b_goal"),
                "swim_css_per_100m":         prof.get("swim_css_per_100m"),
                "run_threshold_pace_per_km": prof.get("run_threshold_pace_per_km"),
                "lthr":                      prof.get("lthr"),
                "ftp_watts":                 _resolve_ftp(prof.get("ftp_watts"), fitness_ytd, session_log_f),
                "weight_kg":                 prof.get("weight_kg"),
                "race_distance":             prof.get("race_distance"),
                "race_date":                 prof.get("race_date"),
                "race_name":                 prof.get("race_name"),
                "prev_race":                 prof.get("prev_race"),
                "prev_race_date":            prof.get("prev_race_date"),
                "prev2_race_date":           prof.get("prev2_race_date"),
                "prev2_race_name":           prof.get("prev2_race_name"),
                "race_targets":              prof.get("race_targets"),
            }
        except Exception:
            pass

    # Weekly discipline breakdown (from athlete-summary.json)
    summary_f = BASE / f"athletes/{slug}/athlete-summary.json"
    if summary_f.exists():
        try:
            summary = json.loads(summary_f.read_text())
            data["weeklyBreakdown"] = summary.get("weeks", [])
            data["swimProgression"] = summary.get("swim_progression", [])
        except Exception:
            pass

    # CTL projection — SINGLE SOURCE OF TRUTH. The target CTL milestones come from
    # the SAME planner maths (derive_phase_ctl_targets / compute_required_tss in
    # primitives.load) that stage1-plan uses, driven by ctl_targets.race_min in
    # athletes.json. The site chart plots whatever lands in ctlProjection — there
    # are NO hardcoded targets — so changing an athlete's race_min moves both the
    # plan and the website together. (Was: a stale phase_tss-defaults projection
    # that ignored ctl_targets and drifted from the plan.)
    try:
        sys.path.insert(0, str(BASE / "ironman-analysis"))
        from primitives.load import derive_phase_ctl_targets, compute_required_tss
        phase_cfg      = athlete_cfg.get("phase_tss", {})
        ctl_targets    = athlete_cfg.get("ctl_targets", {})
        race_min       = ctl_targets.get("race_min")
        plan_start_str = athlete_cfg.get("plan_start")
        race_dt        = date.fromisoformat(athlete_cfg["race_date"])
        if race_min and plan_start_str and kpi.get("ctl"):
            plan_start_dt = date.fromisoformat(plan_start_str)
            current_ctl   = kpi["ctl"]
            # Defaults MUST mirror plan_tools.required_tss exactly, or the site
            # target and the plan target drift apart again.
            ends = {
                "base":     phase_cfg.get("base_end_week", 6),
                "build":    phase_cfg.get("build_end_week", 10),
                "specific": phase_cfg.get("specific_end_week", 14),
                "peak":     phase_cfg.get("peak_end_week", 17),
            }
            max_ramp        = float(athlete_cfg.get("max_ctl_ramp_per_week", 5.0))
            taper_overshoot = float(athlete_cfg.get("taper_overshoot", 1.15))
            derived = derive_phase_ctl_targets(
                current_ctl, int(race_min), plan_start_dt,
                ends["base"], ends["build"], ends["specific"], ends["peak"],
                max_ramp, taper_overshoot, today=today)

            # Target CTL milestones — shared single-source helper (handles configured
            # phase_ctl and race_min-derived identically for every athlete).
            target_milestones = _ctl_target_milestones(athlete_cfg, current_ctl, today)

            # Planned build: ramp to the peak target then taper, using the same
            # required-TSS maths the planner prescribes (not a static phase table).
            days_to_race  = (race_dt - today).days + 1
            peak_end_date = plan_start_dt + timedelta(weeks=ends["peak"])
            weeks_to_peak = max(1, math.ceil((peak_end_date - today).days / 7))
            build_daily   = compute_required_tss(current_ctl, derived["peak"], weeks_to_peak) / 7.0

            def _planned_build(d):
                return build_daily if d <= peak_end_date else build_daily * 0.6

            proj_build = _ctl_project(current_ctl, _planned_build, days_to_race)
            data["ctlProjection"] = {
                "planned_build":    proj_build,
                "target_milestones": target_milestones,
                "race_date":        race_dt.isoformat(),
                "target_ctl_min":   ctl_targets.get("race_min", 60),
                "target_ctl_max":   ctl_targets.get("race_max", 80),
            }
    except Exception as exc:
        log(f"[{slug}] ctlProjection skipped: {exc}")

    out = BASE / f"training-data-{slug}.json"
    out.write_text(json.dumps(data, separators=(",", ":")))
    log(f"[{slug}] training-data-{slug}.json: CTL {kpi.get('ctl')}, {len(recent)} activities")


def acquire_lock():
    if LOCK_FILE.exists() and time.time() - LOCK_FILE.stat().st_mtime < 600:
        return False
    LOCK_FILE.touch()
    return True


def release_lock():
    try:
        LOCK_FILE.unlink()
    except FileNotFoundError:
        pass


def main():
    if not acquire_lock():
        log("Already running — skipping")
        sys.exit(0)

    try:
        sys.path.insert(0, str(BASE / "lib"))
        from icu_api import IcuClient
        athletes_map = json.loads(ATHLETES_CONFIG.read_text())
        jamie_cfg    = athletes_map.get("jamie", {})
        client       = IcuClient(jamie_cfg["icu_athlete_id"], jamie_cfg["icu_api_key"])

        fetch_fitness_prev(client)  # one-time cache of 2025 CTL — skips if already exists

        log("Fetching live data via IcuClient...")
        try:
            data = _build_jamie_data(client)
            log(f"Fetch ok: CTL {data['kpi'].get('ctl')}, {len(data['recent'])} activities")
        except Exception as e:
            log(f"IcuClient fetch failed: {e}")
            sys.exit(1)

        # Add locally-computed fields (heat, decoupling, CTL projection, session log…)
        try:
            data = post_process(data)
            log("Post-processing: heat, decoupling, CTL projection added")
        except Exception as e:
            log(f"Post-processing warning: {e} — continuing without extra fields")
        OUT_FILE.write_text(json.dumps(data, separators=(",", ":")))

        # Write public version (strips personal health data) to ClaudeCoach/ for GitHub Pages
        try:
            PUB_FILE.write_text(json.dumps(_strip_private(data), separators=(",", ":")))
            log(f"Wrote public training-data.json (sessionLog + health fields stripped)")
        except Exception as e:
            log(f"Public file write warning: {e}")

        # Refresh per-athlete training data for other athletes (using IcuClient directly)
        if ATHLETES_CONFIG.exists():
            try:
                athletes_map = json.loads(ATHLETES_CONFIG.read_text())
                for slug, acfg in athletes_map.items():
                    if slug == "jamie" or not acfg.get("active", True):
                        continue
                    try:
                        _build_athlete_training_data(slug, acfg)
                    except Exception as e:
                        log(f"[{slug}] training-data refresh failed (non-fatal): {e}")
            except Exception as e:
                log(f"athletes.json load error: {e}")

        # Commit and push — include all training-data*.json files
        today_str = datetime.now().strftime("%Y-%m-%d")
        pub_files = ["ClaudeCoach/training-data.json"] + [
            f"ClaudeCoach/training-data-{s}.json"
            for s, v in (json.loads(ATHLETES_CONFIG.read_text()).items() if ATHLETES_CONFIG.exists() else [])
            if s != "jamie" and v.get("active", True)
            and (BASE / f"training-data-{s}.json").exists()
        ]
        for cmd in [
            ["git", "add"] + pub_files,
            ["git", "commit", "-m", f"data: refresh training data {today_str}"],
            ["git", "fetch", "origin"],
            ["git", "rebase", "--autostash", "origin/main"],
            ["git", "push", "origin", "main"],
        ]:
            r = subprocess.run(cmd, cwd=PROJECT_DIR, capture_output=True, text=True)
            if r.returncode != 0 and "nothing to commit" not in r.stdout + r.stderr:
                log(f"git error ({' '.join(cmd[:2])}): {r.stderr[:120]}")
                break
            log(f"git {cmd[1]}: ok")

        log("Done.")

    finally:
        release_lock()


if __name__ == "__main__":
    main()
