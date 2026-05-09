#!/usr/bin/env python3
"""
Pull live data from Intervals.icu and update training-data.json, then push to GitHub.
Run daily (e.g. 06:00 via launchd/cron). Requires git push credentials (SSH key or keychain).
"""
import json, subprocess, sys, time, math
from pathlib import Path
from datetime import datetime, date, timedelta
from collections import defaultdict

BASE        = Path(__file__).parent.parent          # ClaudeCoach/
OUT_FILE    = BASE / "training-data.json"
PROJECT_DIR = str(BASE.parent)                       # diamondpeak-site/
LOCK_FILE   = BASE / ".refresh_site_data.lock"
CLAUDE      = "/usr/bin/claude"

HEAT_LOG          = BASE / "heat-log.json"
DECOUPLING_LOG    = BASE / "decoupling-log.json"
STATE_JSON        = BASE / "current-state.json"
SESSION_LOG       = BASE / "session-log.json"
SWIM_LOG          = BASE / "swim-log.json"
FITNESS_PREV_CACHE = BASE / "fitness-prev-cache.json"

RACE_DATE = date(2026, 9, 19)
PLAN_START = date(2026, 4, 27)  # Week 1 Monday

TOOLS = ",".join([
    "Write",
    "mcp__claude_ai_icusync__get_athlete_profile",
    "mcp__claude_ai_icusync__get_fitness",
    "mcp__claude_ai_icusync__get_training_history",
    "mcp__claude_ai_icusync__get_events",
    "mcp__claude_ai_icusync__get_power_curves",
    "mcp__claude_ai_icusync__get_wellness",
])

PROMPT = """Fetch live training data from Intervals.icu and write ClaudeCoach/training-data.json.

Steps:
1. get_athlete_profile → note current_date_local (today) and FTP
2. get_fitness(start_date="2026-01-01", end_date=today) → daily CTL/ATL/TSB series (this season)
3. get_training_history(start_date=<14 days ago>, end_date=today) → recent activities
4. get_power_curves → best power efforts for standard durations
5. get_wellness(start_date=<30 days ago>, end_date=today) → HRV, RHR, body_weight per day

Then use the Write tool to write ClaudeCoach/training-data.json with EXACTLY this schema
(no trailing text after the Write call):

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
    ... flat array of weekCalendar entries (see step 6) ordered by date ascending
  ],
  "loadChart": [
    ... 15 entries covering today-7 to today+7 (see step 7) ordered by date ascending
  ],
  "weightTrend": [
    {"date": "YYYY-MM-DD", "kg": <float>},
    ... all days from get_wellness where body_weight is not null, last 30 days, date ascending
  ]
}

6. get_events(start_date=<today>, end_date=<today+14 days>) → upcoming planned events

Build "weekCalendar": a flat array covering the last 7 days (from training_history) plus the next 14 days (from get_events). Each entry:
{
  "date": "YYYY-MM-DD",
  "sport": "Ride|Run|Swim|Strength|Other",
  "name": "<activity or event name>",
  "tss": <integer or null>,
  "duration_min": <integer or null>,
  "status": "completed" or "planned",
  "key": <true if the event is marked key/priority, else false>,
  "detail": "<brief metric string>"
}

Rules for weekCalendar:
- Activity in get_training_history → status "completed"
- Event in get_events with NO matching training_history entry for that date+sport → status "planned"
- Never mark a planned event "completed" based on the plan alone — only actual recorded activities count
- Normalise sport to: Ride (also for VirtualRide/GravelRide), Run, Swim, Strength
- detail for completed Ride: "NP <powNp>W · HR <hr> · <dist>km" (omit null fields)
- detail for completed Run: "<pace> · <dist>km"
- detail for completed Swim: "<pace> · <dist>m"
- detail for planned: event description or empty string

7. Build "loadChart": 15 day entries covering (today minus 7 days) through (today plus 7 days) inclusive, ordered date ascending.
   Each entry:
   {
     "date": "YYYY-MM-DD",
     "tsb": <TSB float from get_fitness for that date, or null if not in fitness data>,
     "activities": [
       {"sport": "Ride|Run|Swim|Strength|Other", "tss": <integer or null>, "dur": <minutes or null>, "status": "completed"|"planned"}
     ]
   }
   Rules:
   - TSB: use get_fitness rows (available for past dates and possibly a few future projection rows)
   - Completed: from get_training_history filtered to dates in the window
   - Planned: from get_events filtered to dates in the window that have NO matching completed activity
   - Normalise sport: Ride (also VirtualRide/GravelRide), Run, Swim, Strength
   - Include every day in the window even if activities is empty

After writing the file, output one line: "Done: CTL <value>, <N> activities"
"""

PROMPT_FITNESS_PREV = """Fetch last season's CTL data from Intervals.icu and write it to a cache file.

Call get_fitness(start_date="2025-01-01", end_date="2025-09-19").

Then use the Write tool to write ClaudeCoach/fitness-prev-cache.json as a JSON array:
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
    """Return planned daily TSS based on phase (week number from PLAN_START)."""
    week = max(1, math.ceil((d - PLAN_START).days / 7))
    if week <= 6:    return 57    # Base: ~400/wk
    if week <= 10:   return 79    # Build: ~550/wk
    if week <= 14:   return 97    # Specific: ~680/wk
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

    sick_week_num = 10
    def sick_week_tss(d):
        week = max(1, math.ceil((d - PLAN_START).days / 7))
        return 0 if week == sick_week_num else _phase_daily_tss(d)

    data["ctlProjection"] = {
        "current_trend": _ctl_project(current_ctl, current_trend_tss, days_to_race),
        "planned_build":  _ctl_project(current_ctl, _phase_daily_tss, days_to_race),
        "sick_week":      _ctl_project(current_ctl, sick_week_tss, days_to_race),
        "race_date": RACE_DATE.isoformat(),
        "target_ctl_min": 100,
        "target_ctl_max": 115,
    }

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
            for e in all_entries:
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

        log(f"Claude: {result.stdout.strip()[:120]}")

        if not OUT_FILE.exists():
            log("training-data.json was not written — aborting push")
            sys.exit(1)

        # Validate JSON before committing
        try:
            data = json.loads(OUT_FILE.read_text())
            assert "kpi" in data and "fitnessThis" in data and "recent" in data and "weekCalendar" in data and "loadChart" in data
            log(f"JSON valid: CTL {data['kpi']['ctl']}, {len(data['recent'])} activities")
        except Exception as e:
            log(f"JSON validation failed: {e} — aborting push")
            sys.exit(1)

        # Add locally-computed fields (heat, decoupling, CTL projection)
        try:
            data = post_process(data)
            OUT_FILE.write_text(json.dumps(data, separators=(",", ":")))
            log("Post-processing: heat, decoupling, CTL projection added")
        except Exception as e:
            log(f"Post-processing warning: {e} — continuing without extra fields")

        # Commit and push
        today = datetime.now().strftime("%Y-%m-%d")
        for cmd in [
            ["git", "add", "ClaudeCoach/training-data.json"],
            ["git", "commit", "-m", f"data: refresh training data {today}"],
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
