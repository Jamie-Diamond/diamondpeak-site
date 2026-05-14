#!/usr/bin/env python3
"""
Weekly training summary — fetches IcuSync data directly, calls Claude API, sends to Telegram.
Replaces the MCP-dependent weekly-summary.sh flow.
Run: python3 weekly-summary.py [--athlete jamie]
Also called from weekly-summary.sh for cron compatibility.
"""
import json, ssl, subprocess, sys, urllib.request, urllib.error
from datetime import date, timedelta
from pathlib import Path

BASE = Path(__file__).parent.parent
sys.path.insert(0, str(BASE / "lib"))

from icu_api import IcuClient
import recovery_score as rs

ATHLETES_CONFIG = BASE / "config/athletes.json"
TG_CONFIG       = BASE / "telegram/config.json"
CLAUDE          = "/usr/bin/claude"
PROJECT_DIR     = str(BASE.parent)
LOG_DIR         = Path.home() / "Library/Logs/ClaudeCoach"
TOOLS           = "Write,Bash"


def _load_client(slug: str):
    cfg = json.loads(ATHLETES_CONFIG.read_text())
    a = cfg[slug]
    return IcuClient(a["icu_athlete_id"], a["icu_api_key"]), a["chat_id"]


def _tg_send(chat_id: str, text: str):
    try:
        cfg = json.loads(TG_CONFIG.read_text())
        token = cfg["bot_token"]
        cafile = "/etc/ssl/cert.pem" if Path("/etc/ssl/cert.pem").exists() else None
        ctx = ssl.create_default_context(cafile=cafile)

        def _post(body: dict):
            req = urllib.request.Request(
                f"https://api.telegram.org/bot{token}/sendMessage",
                data=json.dumps(body).encode(),
                headers={"Content-Type": "application/json"},
            )
            urllib.request.urlopen(req, timeout=10, context=ctx)

        for chunk in [text[i:i+4096] for i in range(0, len(text), 4096)]:
            try:
                _post({"chat_id": chat_id, "text": chunk, "parse_mode": "Markdown"})
            except urllib.error.HTTPError as e:
                if e.code == 400:
                    _post({"chat_id": chat_id, "text": chunk})
                else:
                    raise
    except Exception as e:
        print(f"Telegram send failed: {e}", file=sys.stderr)


def _read_file(path: Path, default="(not found)") -> str:
    try:
        return path.read_text()
    except Exception:
        return default


def _read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text())
    except Exception:
        return default if default is not None else []


def run_summary(slug: str = "jamie") -> str:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_file = LOG_DIR / "weekly-summary.log"

    adir   = BASE / "athletes" / slug
    pfile  = adir / "profile.json"
    profile = _read_json(pfile, {})

    client, chat_id = _load_client(slug)

    today      = date.today()
    week_start = today - timedelta(days=today.weekday())  # Monday
    week_end   = week_start + timedelta(days=6)           # Sunday

    # ── Fetch IcuSync data ────────────────────────────────────────────────────
    wellness_14d    = client.get_wellness(14)
    activities_7d   = client.get_training_history(7)
    events_this_wk  = client.get_events(week_start.isoformat(), week_end.isoformat())
    athlete_profile = client.get_athlete_profile()
    outlook_end     = (today + timedelta(days=28)).isoformat()
    fitness_outlook = client.get_fitness(7, newest=outlook_end)
    events_4wk      = client.get_events((today + timedelta(days=1)).isoformat(), outlook_end)

    # ── Read local files ──────────────────────────────────────────────────────
    current_state = _read_file(adir / "current-state.md")
    session_log   = _read_json(adir / "session-log.json")
    heat_log      = _read_json(adir / "heat-log.json")
    blueprint     = _read_file(adir / "reference/training-blueprint.md")

    # Upcoming training plan files (start date > today)
    plan_texts = []
    for p in sorted(adir.glob("training-plan-*.md")):
        # filename: training-plan-YYYY-MM-DD_to_YYYY-MM-DD.md
        parts = p.stem.replace("training-plan-", "").split("_to_")
        if parts and parts[0] >= today.isoformat():
            plan_texts.append(p.read_text())
    upcoming_plans = "\n\n---\n\n".join(plan_texts) if plan_texts else "(none)"

    # Filter logs to this week
    week_sessions = [
        s for s in session_log
        if week_start.isoformat() <= s.get("date", "") <= week_end.isoformat()
    ]
    week_heat = [
        h for h in heat_log
        if week_start.isoformat() <= h.get("date", "") <= week_end.isoformat()
    ]

    # Nutrition history: rides/bricks >90 min with carb data, last 6 weeks
    six_weeks_ago = (today - timedelta(weeks=6)).isoformat()
    nutrition_history = [
        {"date": s["date"], "name": s.get("name",""), "duration_min": s.get("duration_min",0),
         "nutrition_g_carb": s["nutrition_g_carb"],
         "g_per_hr": round(s["nutrition_g_carb"] / s["duration_min"] * 60, 1)}
        for s in session_log
        if s.get("date","") >= six_weeks_ago
        and s.get("sport","") in ("Ride","VirtualRide","GravelRide","Brick")
        and s.get("duration_min", 0) >= 90
        and s.get("nutrition_g_carb") is not None
    ]

    race_date    = date.fromisoformat(profile.get("race_date", "2026-09-19"))
    days_to_race = (race_date - today).days
    race_name    = profile.get("race_name", "race")
    first_name   = profile.get("name", slug).split()[0]
    ftp          = profile.get("ftp_watts", "unknown")

    # ── Athlete-specific nutrition consequence (for T7 trigger) ──────────────
    prev_race = profile.get("prev_race", {})
    prev_race_notes = prev_race.get("notes", "")
    prev_race_name  = prev_race.get("name", "")
    if prev_race_notes:
        nutrition_consequence = (
            f"{prev_race_name} post-race note: \"{prev_race_notes}\" — "
            f"underfuelling in training means the gut never adapts to high carb flux under load. "
            f"Every long ride below 75g/hr is a missed adaptation."
        )
    else:
        nutrition_consequence = (
            "Underfuelling in training means the gut never adapts to high carb flux under load. "
            "Race-day bonk risk rises sharply when training rides average below 75g/hr."
        )

    # ── Pre-compute recovery score ────────────────────────────────────────────
    recovery = None
    try:
        hrv_t, hrv_b, tsb_v, sleep_v = rs._parse_wellness(wellness_14d)
        pain = 0
        state_json = adir / "current-state.json"
        if state_json.exists():
            pain = json.loads(state_json.read_text()).get("ankle", {}).get("pain_during", 0) or 0
        recovery = rs.compute(hrv_t, hrv_b, tsb_v, sleep_v, pain)
    except Exception:
        pass

    recovery_block = ""
    if recovery:
        score  = recovery.get("score", "?")
        label  = recovery.get("label", "?")
        rec    = recovery.get("recommendation", "")
        sigs   = recovery.get("signals", {})
        hrv_r  = sigs.get("hrv",   {}).get("ratio")
        hrv_t_v = sigs.get("hrv",  {}).get("value")
        hrv_b_v = sigs.get("hrv",  {}).get("baseline")
        tsb_sv = sigs.get("tsb",   {}).get("value")
        slp_sv = sigs.get("sleep", {}).get("value")
        pain_v = sigs.get("pain",  {}).get("value")
        avail  = recovery.get("available_signals", [])
        missing = recovery.get("missing_signals", [])
        parts = []
        if hrv_r  is not None: parts.append(f"HRV ratio {hrv_r:.2f} (today {hrv_t_v}, baseline {hrv_b_v})")
        if tsb_sv is not None: parts.append(f"Form {tsb_sv:+.1f}")
        if slp_sv is not None: parts.append(f"sleep {slp_sv:.1f}h")
        if pain_v is not None and pain_v > 0: parts.append(f"pain {pain_v}/10")
        recovery_block = (
            f"\n## Pre-computed recovery score (end of week)\n"
            f"Score: {score}/100 — {label}. {rec}\n"
            f"Signals: {', '.join(parts) if parts else 'no data'}. "
            f"Available: {avail}. Missing: {missing}.\n"
            f"Use this for T1/T8 evaluation — it is already derived from the wellness data below.\n"
        )

    # ── Build prompt ──────────────────────────────────────────────────────────
    prompt = f"""You are generating the weekly training summary for {first_name}'s {race_name} coaching system.
All IcuSync data has been fetched and is embedded below. Do NOT call any fetch commands — work only from the data provided. Use Write and Bash only for the state-file update and git commit at the end.
{recovery_block}
---

## Context

Race: {race_name} | Days to race: {days_to_race} | FTP: {ftp} W
Week: {week_start} → {week_end}

## IcuSync — Wellness (14 days: CTL, ATL, TSB, HRV, sleep, weight, RHR)
{json.dumps(wellness_14d, indent=2)}

## IcuSync — Activities this week (7 days)
{json.dumps(activities_7d, indent=2)}

## IcuSync — Planned events this week ({week_start} → {week_end})
{json.dumps(events_this_wk, indent=2)}

## IcuSync — Athlete profile
{json.dumps(athlete_profile, indent=2)}

## Local — current-state.md
{current_state}

## Local — Session log (this week only)
{json.dumps(week_sessions, indent=2)}

## Local — Heat log (this week only)
{json.dumps(week_heat, indent=2)}

## Training Blueprint (phase structure and TSS targets)
{blueprint}

## IcuSync — Fitness projection (last 7 days + next 28 days)
{json.dumps(fitness_outlook, indent=2)}

## IcuSync — Events next 28 days ({today} → {outlook_end})
{json.dumps(events_4wk, indent=2)}

## Upcoming training plans
{upcoming_plans}

## Local — Nutrition history (rides/bricks >90 min, last 6 weeks — g/hr computed)
{json.dumps(nutrition_history, indent=2)}

---

## Step 1 — Compute week metrics

From the data above, extract:
- Total actual TSS (sum icu_training_load from activities this week)
- Total planned TSS (sum planned_tss from events — or estimate from event duration/IF if not explicit)
- Compliance % = actual / planned × 100
- CTL at start of week vs end of week (from wellness — first and last entries for the week range)
- ATL at end of week
- TSB at end of week (form field in wellness, or ATL - CTL)
- 4-week CTL ramp: (end CTL - CTL 28 days ago) / 4 — use wellness data window available
- Disciplines completed: count by sport_label from activities
- Sessions missed: planned events with no matching activity on same date and sport
- Heat sessions this week: count from heat-log above
- Average sleep: mean hrsSleep from wellness this week
- Fuelling logged this week: sessions with nutrition_g_carb set vs total rides
- Nutrition trend (from nutrition history above):
  - This week avg g/hr (rides >90 min): compute from nutrition_history entries this week
  - 4-week rolling avg g/hr: mean across all entries in nutrition_history
  - Trend direction: compare most recent 3 sessions vs previous 3 — improving / declining / flat
  - Gap to race target: 90 − this_week_avg (g/hr)
- Injury pain: ankle_pain_during scores from session-log this week

## Step 2 — Output the summary card

Output the card in Telegram Markdown. Rating = STRONG (≥95% compliance, no flags) / SOLID (80–95%, no major flags) / LIGHT (<80%) / MIXED (compliance ok but flags).

---
**Week ending {week_end} — [STRONG / SOLID / LIGHT / MIXED]**

| Metric | This week | Target/trend |
|---|---|---|
| Load | X (planned Y) | — |
| Compliance | X% | ≥90% |
| Fitness change | +X / −X | phase ramp target |
| Fatigue | X | — |
| Sleep avg | Xh | ≥7h |
| Heat sessions | N | — |
| Fuelling (rides >90 min) | Xg/hr this wk (4wk avg: Y) | 90g/hr race target — gap: Zg/hr |

**Completed:** [discipline summaries — e.g. "3 rides, 2 runs, 1 swim"]
**Missed:** [session names, or "none"]

**Key finding:** [one sentence — most important thing from this week]

**Monday focus:** [one sentence — single most important thing for next week's first session]

---

**4-week outlook**

| Week | Constraint | Proj. Fitness | Proj. Form |
|---|---|---|---|
| [Mon dd Mon] | [e.g. travel / full / race] | [Fitness from fitness_outlook] | [Form] |
| [Mon dd Mon] | … | … | … |
| [Mon dd Mon] | … | … | … |
| [Mon dd Mon] | … | … | … |

[For each significant event or constraint in events_4wk / upcoming plans / current-state.md, one line:]
📌 [Date]: [What — e.g. "Travel block begins (no bike)", "Dorney C-race TBC", "Heat protocol target 2×/wk"]

*Race trajectory: projected Fitness [X] on [date 8 weeks out] vs blueprint target [Y] — [on track / behind / ahead].*

---

## Step 3 — Decision triggers (⚡)

Evaluate each trigger using the computed metrics. Output only the ones that FIRE. If none fire, output the all-clear line.

**T1 RECOVERY** — fires if end-of-week TSB < −30:
⚡ *T1 RECOVERY*: Form at [X] — accumulated fatigue is high.
Options: A) 2-day recovery block (Mon–Tue easy only) | B) Continue as planned | C) Reduce Monday volume 40%

**T2 OVERREACH** — fires if 4-week CTL ramp > 7/wk:
⚡ *T2 OVERREACH*: 4-week Fitness ramp at [X]/wk — approaching overreach threshold.
Options: A) Cap next week at current Load | B) Insert recovery week now | C) Continue (accept fatigue risk)

**T3 UNDERLOAD** — fires if week TSS < 75% of current phase TSS ceiling (from blueprint):
⚡ *T3 UNDERLOAD*: Week Load [X] vs phase target [Y] ([Z]% of ceiling).
Availability issue or training fatigue? Reply to clarify and I'll adjust next week's plan.

**T4 FRESH** — fires if end-of-week TSB > 10 AND days to race > 42:
⚡ *T4 FRESH*: Form at [X] with {days_to_race} days to race — you're fresher than the phase requires.
Options: A) Add an extra session | B) Increase intensity on planned sessions | C) Hold (life/fatigue reason)

**T5 PHASE TRANSITION** — fires if current phase (from blueprint) ends within 7 days:
⚡ *T5 PHASE TRANSITION*: [phase name] ends [date] — entering [next phase] next week.
Readiness: [one line on whether athlete is prepared to step up]

**T6 INJURY** — fires if ankle pain avg > 3 this week OR last 3 pain scores are trending up:
⚡ *T6 INJURY*: Ankle pain avg [X]/10 this week [or: trending up — scores X→Y→Z].
Options: A) Drop all runs this week | B) Reduce run volume 50% | C) Continue protocol (accept risk)

**T7 NUTRITION** — fires if this-week avg g/hr < 75 on rides >90 min AND at least 1 such session was logged:
⚡ *T7 NUTRITION*: Avg fuelling [X]g/hr on long rides — [Y]g/hr short of the 90g/hr race target. Trend: [improving / declining / flat] over last 6 sessions.
Race consequence: {nutrition_consequence}
Fix: Eat at 15 min and every 25 min after. This week's long ride target: [blueprint phase rate, e.g. 90]g/hr. Use Maurten 320 + chews if GI allows.

**T8 HRV** — fires if the pre-computed recovery score HRV ratio < 0.90 OR 3+ consecutive days with HRV below the 7-day rolling average in the wellness data:
⚡ *T8 HRV*: HRV ratio [X] vs baseline — accumulated fatigue signal (recovery score: [score]/100 [label]).
Options: A) Flip tomorrow to easy | B) Prioritise sleep tonight | C) Continue (trust your Form)

If no triggers fire:
✅ No decision triggers this week.

---

## Step 4 — Open actions review

From the "Open actions" table in current-state.md, list any actions where status is NOT "done" and:
- Due date ≤ 14 days from today ({today}): flag as ⚠️ DUE SOON
- Due date has already passed: flag as 🔴 OVERDUE
- No due date but status is "pending" for 3+ weeks: flag as 📋 STALE

Format (append after the decision triggers, before the sign-off):

---
**Open actions**
[For each flagged item:]
[⚠️/🔴/📋] *[Action name]* — due [date] ([N days]) — [one-line nudge if overdue]

If no flagged actions: omit this section entirely.

---

## Step 5 — Update current-state.md

Using the Write tool, update ClaudeCoach/athletes/{slug}/current-state.md:
- Change "Last updated" line to today: {today}
- Update or add "Off-plan in last 7 days" with missed sessions (or "none")
- If heat sessions this week > 0: append a row to "Heat acclimation log" table
- If any body weight readings in wellness data: note the latest weight

Then using Bash:
  cd {PROJECT_DIR} && git add ClaudeCoach/athletes/{slug}/current-state.md && git commit -m "weekly: state update week ending {week_end}" && git pull --rebase origin main && git push origin main

## Output

Output ONLY the Telegram message (Steps 2 + 3 + 4 combined). No preamble, no sign-off, no tool-use commentary.
"""

    result = subprocess.run(
        [CLAUDE, "-p", prompt, "--allowedTools", TOOLS, "--model", "claude-sonnet-4-6"],
        capture_output=True, text=True, cwd=PROJECT_DIR, timeout=600,
    )

    if result.stderr:
        with open(log_file, "a") as f:
            f.write(result.stderr + "\n")

    output = result.stdout.strip()
    if output:
        _tg_send(chat_id, output)

    # Regenerate trend aggregates in the background (feeds dashboard chart)
    try:
        trend_script = BASE / "scripts/weekly-trend.py"
        if trend_script.exists():
            subprocess.Popen(
                [sys.executable, str(trend_script), "--athlete", slug],
                cwd=PROJECT_DIR,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
    except Exception:
        pass

    return output


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--athlete", default="jamie")
    args = p.parse_args()
    out = run_summary(args.athlete)
    print(out)
