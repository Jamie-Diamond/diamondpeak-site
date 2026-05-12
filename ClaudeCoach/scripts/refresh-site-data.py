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

TOOLS = ",".join([
    "mcp__claude_ai_icusync__get_athlete_profile",
    "mcp__claude_ai_icusync__get_fitness",
    "mcp__claude_ai_icusync__get_training_history",
    "mcp__claude_ai_icusync__get_events",
    "mcp__claude_ai_icusync__get_power_curves",
    "mcp__claude_ai_icusync__get_wellness",
])

PROMPT = """Fetch live training data from Intervals.icu and output it as JSON to stdout.
Do NOT use any Write tool — output the JSON directly as your response.

Steps:
1. get_athlete_profile → note current_date_local (today) and FTP
2. get_fitness(start_date="2026-01-01", end_date=today) → daily CTL/ATL/TSB series (this season)
3. get_training_history(start_date=<14 days ago>, end_date=today) → recent activities
4. get_power_curves → best power efforts for standard durations
5. get_wellness(start_date=<30 days ago>, end_date=today) → HRV, RHR, body_weight per day
6. get_events(start_date=<today>, end_date=<today+14 days>) → upcoming planned events

Then output ONLY a single JSON object — no other text before or after:

{
  "generated": "<today YYYY-MM-DD>",
  "kpi": {
    "ctl": <today CTL, 1dp float>,
    "atl": <today ATL, 1dp float>,
    "tsb": <today TSB, 1dp float — negative means fatigued>,
    "ramp7d": <CTL today minus CTL 7 days ago, 1dp float>,
    "hrv": <latest HRV integer or null>,
    "rhr": <latest RHR integer or null>
  },
  "fitnessThis": [
    ["YYYY-MM-DD", <ctl float>],
    ... one entry per day from 2026-01-01 to today inclusive
  ],
  "recent": [
    {
      "date": "YYYY-MM-DD",
      "sport": "<Ride|Run|Swim|Strength|GravelRide|VirtualRide|Other>",
      "name": "<activity name>",
      "dur": <duration in whole minutes>,
      "dist": <distance in km, 2dp float, or null>,
      "pace": "<formatted string: '31.7 kph' for rides, '5:02/km' for runs, '1:39/100m' for swims>",
      "hr": <average HR integer or null>,
      "powAvg": <average power watts integer or null — cycling only>,
      "powNp": <normalised power watts integer or null — cycling only>,
      "tss": <TSS integer>
    },
    ... all activities from the last 14 days, most recent first
  ],
  "powerCurve": [
    {"t": <seconds>, "label": "<e.g. 5s>", "w": <best watts integer>, "wPrev": <last year same window or null>},
    ... include durations: 5s(5), 10s(10), 30s(30), 1m(60), 2m(120), 5m(300), 10m(600), 20m(1200), 30m(1800), 60m(3600), 90m(5400), 2h(7200)
  ],
  "weekCalendar": [
    ... flat array of weekCalendar entries (see below) ordered by date ascending
  ],
  "loadChart": [
    ... 15 entries covering today-7 to today+7 (see below) ordered by date ascending
  ],
  "weightTrend": [
    {"date": "YYYY-MM-DD", "kg": <float>},
    ... all days from get_wellness where body_weight is not null, last 30 days, date ascending
  ]
}

weekCalendar: covers last 7 days (from training_history) + next 14 days (from get_events). Each entry:
{"date":"YYYY-MM-DD","sport":"Ride|Run|Swim|Strength|Other","name":"<name>","tss":<int or null>,"duration_min":<int or null>,"status":"completed"|"planned","key":<bool>,"detail":"<metric string>"}
Rules:
- training_history activities → status "completed"
- get_events with NO matching completed activity for that date+sport → status "planned"
- Normalise sport: Ride (VirtualRide/GravelRide), Run, Swim, Strength
- detail completed Ride: "NP <powNp>W · HR <hr> · <dist>km" (omit nulls)
- detail completed Run: "<pace> · <dist>km"
- detail completed Swim: "<pace> · <dist>m"
- detail planned: event description or ""

loadChart: 15 days from today-7 to today+7 inclusive, each:
{"date":"YYYY-MM-DD","tsb":<float or null>,"activities":[{"sport":"...","tss":<int or null>,"dur":<min or null>,"status":"completed"|"planned"}]}
- TSB from get_fitness rows; completed from training_history; planned from get_events (no matching completed)
- Include every day even if activities is empty
"""

PROMPT_FITNESS_PREV = """Fetch last season's CTL data from Intervals.icu and write it to a cache file.

Call get_fitness(start_date="2025-01-01", end_date="2025-09-19").

Then use the Write tool to write ClaudeCoach/athletes/jamie/fitness-prev-cache.json as a JSON array:
[
  ["YYYY-MM-DD", <ctl float>],
  ... one entry per day from 2025-01-01 to 2025-09-19 inclusive
]

Output one line: "Done: <N> days"
"""

TOOLS_FITNESS_PREV = ",".join([
    "Write",
    "mcp__claude_ai_icusync__get_fitness",
])


def fetch_fitness_prev():
    """Fetch 2025 CTL series once and cache it. Skips if cache already exists."""
    if FITNESS_PREV_CACHE.exists():
        return
    log("Fetching last-season fitness (one-time cache)...")
    result = subprocess.run(
        [CLAUDE, "-p", PROMPT_FITNESS_PREV, "--allowedTools", TOOLS_FITNESS_PREV],
        capture_output=True, text=True,
        cwd=PROJECT_DIR, timeout=120,
    )
    if result.returncode != 0 or not FITNESS_PREV_CACHE.exists():
        log(f"fitnessPrev fetch failed (non-fatal): {result.stderr[:120]}")
        return
    log(f"fitnessPrev cached: {result.stdout.strip()[:80]}")


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


def _phase_daily_tss(d):
    """Return planned daily TSS based on phase (week number from PLAN_START).
    Values reflect upper range of each phase at current fitness (~80 CTL):
    base holds CTL steady; build/specific/peak drive progressive gains."""
    week = max(1, math.ceil((d - PLAN_START).days / 7))
    if week <= 6:    return 80    # Base: ~560/wk — holds CTL near current level
    if week <= 10:   return 90    # Build: ~630/wk
    if week <= 14:   return 100   # Specific: ~700/wk
    if week <= 17:   return 114   # Peak: ~800/wk
    return 29                     # Taper: ~200/wk


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

    data["ctlProjection"] = {
        "current_trend":    _ctl_project(current_ctl, current_trend_tss, days_to_race),
        "planned_build":    _ctl_project(current_ctl, _phase_daily_tss, days_to_race),
        "planned_sessions": _ctl_project(current_ctl, planned_sessions_tss, days_to_race),
        "sick_week":        _ctl_project(current_ctl, sick_week_tss, days_to_race),
        "race_date": RACE_DATE.isoformat(),
        "target_ctl_min": 100,
        "target_ctl_max": 115,
    }

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
                "ftp_watts":                 prof.get("ftp_watts"),
                "race_date":                 prof.get("race_date"),
                "race_name":                 prof.get("race_name"),
            }
        except Exception:
            pass

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

    # ── kpi ──────────────────────────────────────────────────────────────────
    kpi = {}
    if wellness_60:
        w = wellness_60[-1]
        ctl = round(w.get("ctl") or 0, 1)
        atl = round(w.get("atl") or 0, 1)
        ramp7d = round(ctl - round(wellness_60[-8].get("ctl") or 0, 1), 1) if len(wellness_60) >= 8 else 0
        kpi = {"ctl": ctl, "atl": atl, "tsb": round(ctl - atl, 1), "ramp7d": ramp7d,
               "hrv": w.get("hrv"), "rhr": w.get("restingHR")}

    # ── fitnessThis ───────────────────────────────────────────────────────────
    fitness_this = [[w["id"][:10], round(w.get("ctl") or 0, 1)] for w in fitness_ytd if w.get("ctl")]

    # ── recent (last 14 days) ─────────────────────────────────────────────────
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

    # ── weekCalendar (last 7 days + next 14 days) ─────────────────────────────
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

    # ── loadChart (today−7 to today+7, 15 days) ───────────────────────────────
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

    # ── session log + swim log from local files ───────────────────────────────
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

    # Profile (goals + thresholds)
    profile_f = BASE / f"athletes/{slug}/profile.json"
    if profile_f.exists():
        try:
            prof = json.loads(profile_f.read_text())
            data["profile"] = {
                "a_goal":                    prof.get("a_goal"),
                "b_goal":                    prof.get("b_goal"),
                "swim_css_per_100m":         prof.get("swim_css_per_100m"),
                "run_threshold_pace_per_km": prof.get("run_threshold_pace_per_km"),
                "lthr":                      prof.get("lthr"),
                "ftp_watts":                 prof.get("ftp_watts"),
                "race_date":                 prof.get("race_date"),
                "race_name":                 prof.get("race_name"),
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
        fetch_fitness_prev()  # one-time cache of 2025 CTL — skips if already exists

        log("Fetching live data via Claude + IcuSync...")
        result = subprocess.run(
            [CLAUDE, "-p", PROMPT, "--allowedTools", TOOLS],
            capture_output=True, text=True,
            cwd=PROJECT_DIR, timeout=300,
        )

        if result.returncode != 0:
            log(f"Claude error: {result.stderr[:200]}")
            sys.exit(1)

        # Parse JSON from stdout (Claude outputs data directly — no Write tool)
        import re as _re
        m = _re.search(r'\{.*\}', result.stdout, _re.DOTALL)
        if not m:
            log(f"No JSON object in Claude output: {result.stdout[:200]}")
            sys.exit(1)

        try:
            data = json.loads(m.group(0))
            assert "kpi" in data and "fitnessThis" in data and "recent" in data and "weekCalendar" in data and "loadChart" in data
            log(f"JSON valid: CTL {data['kpi']['ctl']}, {len(data['recent'])} activities")
        except Exception as e:
            log(f"JSON parse/validation failed: {e} — aborting push")
            sys.exit(1)

        # Add locally-computed fields (heat, decoupling, CTL projection)
        try:
            data = post_process(data)
            OUT_FILE.write_text(json.dumps(data, separators=(",", ":")))
            log("Post-processing: heat, decoupling, CTL projection added")
        except Exception as e:
            log(f"Post-processing warning: {e} — continuing without extra fields")

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
