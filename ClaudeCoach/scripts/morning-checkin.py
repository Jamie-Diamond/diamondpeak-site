#!/usr/bin/env python3
"""Morning briefing — runs via VM crontab at 06:20 daily. Loops over all active athletes."""
import json, subprocess, sys
from datetime import datetime
from datetime import date, timedelta
from pathlib import Path

BASE            = Path(__file__).parent.parent  # ClaudeCoach/
PROJECT_DIR     = str(BASE.parent)
CLAUDE          = "/usr/bin/claude"
NOTIFY          = BASE / "telegram/notify.py"
ATHLETES_CONFIG = BASE / "config/athletes.json"
LOG_DIR         = Path.home() / "Library/Logs/ClaudeCoach"
LOG_DIR.mkdir(parents=True, exist_ok=True)
sys.path.insert(0, str(BASE / "lib"))

TOOLS = "Read,Bash"


def _build_prompt(slug, first_name, race_name, race_date, days_to_race, injuries, recovery=None):
    today = date.today().isoformat()
    tomorrow = (date.today() + timedelta(days=1)).isoformat()

    injury_question = ""
    if injuries:
        injury_question = (
            "- If a run is planned today AND the last injury pain score in current-state.json was >0: "
            "ask \"Injury pain score before heading out? (0-10)\"\n"
        )
    injury_question += "- Else if no weight reading in the last 3 days: ask \"Weight this morning?\""

    injuries_note = (
        "; ".join(
            f"{i.get('location','unknown')}: {i.get('description','')}"
            + (f" — {i.get('protocol','')}" if i.get("protocol") else "")
            for i in injuries
        )
        if injuries else "None"
    )

    recovery_block = ""
    if recovery:
        score  = recovery.get("score", "?")
        label  = recovery.get("label", "?")
        rec    = recovery.get("recommendation", "")
        sigs   = recovery.get("signals", {})
        hrv_r  = sigs.get("hrv",   {}).get("ratio")
        tsb_v  = sigs.get("tsb",   {}).get("value")
        slp_v  = sigs.get("sleep", {}).get("value")
        pain_v = sigs.get("pain",  {}).get("value")
        parts  = []
        if hrv_r  is not None: parts.append(f"HRV ratio {hrv_r:.2f}")
        if tsb_v  is not None: parts.append(f"Form {tsb_v:+.1f}")
        if slp_v  is not None: parts.append(f"sleep {slp_v:.1f}h")
        if pain_v is not None and pain_v > 0: parts.append(f"pain {pain_v}/10")
        recovery_block = (
            f"\n## Recovery score (pre-computed)\n"
            f"Score: {score}/100 — {label}. {rec}\n"
            f"Signals: {', '.join(parts) if parts else 'no data'}.\n"
            f"Use this to modulate session prescription: GREEN = train as planned; "
            f"AMBER = note and monitor; ORANGE = reduce intensity or volume; RED = flag for easy day.\n"
        )

    return f"""\
You are generating the morning briefing for {first_name}'s training day.
{recovery_block}
Step 1 — Fetch data via Bash:
  python3 ClaudeCoach/lib/icu_fetch.py --athlete {slug} --endpoint wellness --days 2
  python3 ClaudeCoach/lib/icu_fetch.py --athlete {slug} --endpoint events --start {today} --end {today}

Step 2 — Read:
- ClaudeCoach/athletes/{slug}/current-state.md (open actions, watchdog flags)
- ClaudeCoach/athletes/{slug}/current-state.json (weight_readings, injury pain scores)
- ClaudeCoach/athletes/{slug}/heat-log.json (count entries in current ISO week to get sessions_this_week)
- ClaudeCoach/athletes/{slug}/session-log.json — only if today's planned event is a Ride or Brick >90 min: extract the last 4 entries with sport Ride/GravelRide/Brick, duration_min ≥ 90, and nutrition_g_carb set. Compute each g_per_hr and the avg.

Step 3 — Determine ONE question to ask (or none):
{injury_question}
- Else: no question

Step 4 — Output the morning card in Telegram Markdown (no preamble, no sign-off):

Use the recovery score and signals ONLY to decide what to flag — do NOT show the score, label, HRV ratio, or any internal metric to the athlete. Write like a coach sending a morning text, not a dashboard.

*Good morning — [Day date, e.g. Sat 9 May]*

*Today:* [session name] — [duration] min[, ~[TSS] TSS if available]

[Form line — only include if notable:
  · Form < −20: ⚠️ Heavy load today — keep intensity in check
  · Form > +10: 🟢 Fresh legs — good day for quality work
  · Form −1 to −20: omit entirely, that's normal training]
[If recovery ORANGE or RED: ⚠️ [one plain-English sentence on what to do differently — no scores]]
[If watchdog flag active: ⚠️ [flag in plain English — one line]]
[If today's session is Ride or Brick >90 min: 🍌 Nutrition — target [min(avg+10,90)]g/hr · eat at 15 min then every 25 min]
[If any travel block, race, or constraint from current-state.md "Travel & training blocks" starts within 5 days: 📌 [constraint name] in [N] days — [one-line impact]]
[If open action is due within 3 days: 📌 [action] due [date]]
[If today ≥ 2026-05-15 AND sessions_this_week < 2 AND today is Wednesday or later: 🌡️ Heat bath due — [N] this week (target 2–3×)]

[Question if applicable — one line]

_{days_to_race} days to {race_name}_

Rules:
- Sleep/HRV/RHR: NEVER show these. At 06:20 the wearable has not yet synced last night's data — any value shown would be from the previous night and would be wrong. Leave them out entirely.
- If no planned session: say "Rest day" and skip the Today line.
- Omit any section that has nothing to say — do not pad with dashes or "N/A".
- Never ask for subjective mood, fatigue, or motivation scores.
- The countdown line appears exactly once, at the end.
Wrap your entire output in <telegram> and </telegram> tags. Output nothing outside those tags — no preamble, no reasoning, no tool commentary."""


def notify(msg, chat_id):
    try:
        subprocess.run(
            ["python3", str(NOTIFY), "--chat-id", str(chat_id), msg],
            cwd=PROJECT_DIR, timeout=15,
        )
    except Exception:
        pass


def run_athlete(slug, athlete_cfg):
    adir = BASE / f"athletes/{slug}"
    chat_id = athlete_cfg.get("chat_id", "")
    log_file = LOG_DIR / "morning-checkin.log"

    profile = {}
    if (adir / "profile.json").exists():
        try:
            profile = json.loads((adir / "profile.json").read_text())
        except Exception:
            pass

    first_name = profile.get("name", slug).split()[0]
    race_name = profile.get("race_name") or athlete_cfg.get("race_name", "your race")
    race_date_str = profile.get("race_date") or athlete_cfg.get("race_date", "")
    injuries = profile.get("injuries", [])

    try:
        rd = date.fromisoformat(race_date_str) if race_date_str else None
        days_to_race = (rd - date.today()).days if rd else "?"
    except Exception:
        days_to_race = "?"

    # Pre-compute recovery score
    recovery = None
    try:
        from icu_api import IcuClient
        import recovery_score as rs
        athletes_cfg = json.loads(ATHLETES_CONFIG.read_text())
        a = athletes_cfg[slug]
        client = IcuClient(a["icu_athlete_id"], a["icu_api_key"])
        wellness_rows = client.get_wellness(8)
        hrv_t, hrv_b, tsb, sleep = rs._parse_wellness(wellness_rows)
        pain = 0
        state_f = adir / "current-state.json"
        if state_f.exists():
            pain = json.loads(state_f.read_text()).get("ankle", {}).get("pain_during", 0) or 0
        recovery = rs.compute(hrv_t, hrv_b, tsb, sleep, pain)
    except Exception:
        pass  # score is optional — morning card still sends without it

    prompt = _build_prompt(slug, first_name, race_name, race_date_str, days_to_race, injuries, recovery)

    with open(log_file, "a") as lf:
        result = subprocess.run(
            [CLAUDE, "-p", prompt, "--allowedTools", TOOLS, "--model", "claude-sonnet-4-6"],
            stdout=subprocess.PIPE, stderr=lf, text=True,
            cwd=PROJECT_DIR, timeout=180,
        )

    raw = (result.stdout or "").strip()
    import re as _re
    m = _re.search(r"<telegram>(.*?)</telegram>", raw, _re.DOTALL)
    output = m.group(1).strip() if m else ""
    if output:
        notify(output, chat_id)


def main():
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] morning-checkin starting", file=sys.stderr)
    try:
        athletes = json.loads(ATHLETES_CONFIG.read_text())
    except Exception as e:
        print(f"[{ts}] Failed to load athletes config: {e}", file=sys.stderr)
        sys.exit(1)

    for slug, cfg in athletes.items():
        if not cfg.get("active", True):
            continue
        try:
            run_athlete(slug, cfg)
        except Exception as exc:
            print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}][{slug}] morning-checkin error: {exc}", file=sys.stderr)


if __name__ == "__main__":
    main()
