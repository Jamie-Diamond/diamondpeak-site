#!/usr/bin/env python3
"""
Daily session prescription — runs via VM crontab at 05:00 daily.
Loops over all active athletes. Safe to run manually:
  python3 ClaudeCoach/scripts/daily-prescription.py
"""
import json, subprocess, sys, tempfile, os
from datetime import date
from pathlib import Path

BASE        = Path(__file__).parent.parent   # ClaudeCoach/
PROJECT_DIR = str(BASE.parent)               # diamondpeak-site/
CLAUDE      = "/usr/bin/claude"
NOTIFY      = BASE / "telegram/notify.py"
CONFIG      = BASE / "config/athletes.json"
LOG_DIR     = Path.home() / "Library/Logs/ClaudeCoach"
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE    = LOG_DIR / "prescription.log"

TOOLS = "Read,Write,Edit,Bash"


def trim_log(path: Path, max_lines: int = 5000):
    try:
        lines = path.read_text().splitlines()
        if len(lines) > max_lines:
            path.write_text("\n".join(lines[-max_lines:]) + "\n")
    except Exception:
        pass


def build_prompt(slug: str, name: str, race_name: str) -> str:
    today = date.today().isoformat()
    athlete_dir = BASE / "athletes" / slug
    first_name  = name.split()[0]

    return f"""You are running the daily session prescription for {name}'s {race_name} coaching system.

Step 1 — Pull live data via Bash (use today's date {today} for all calculations):
  python3 ClaudeCoach/lib/icu_fetch.py --athlete {slug} --endpoint profile
  python3 ClaudeCoach/lib/icu_fetch.py --athlete {slug} --endpoint fitness --days 7
  python3 ClaudeCoach/lib/icu_fetch.py --athlete {slug} --endpoint history --days 7
  python3 ClaudeCoach/lib/icu_fetch.py --athlete {slug} --endpoint wellness --days 14
  python3 ClaudeCoach/lib/icu_fetch.py --athlete {slug} --endpoint events --start {today} --end {today}

Step 2 — Read these files:
- {athlete_dir}/current-state.md
- {athlete_dir}/session-log.json (most recent entry = last RPE)

Step 3 — Assemble the readiness dict:
  atl: from fitness endpoint most recent row
  ctl: from fitness endpoint most recent row
  hrv_trend_pct: (today HRV - 7d avg HRV) / 7d avg HRV x 100  [if no HRV data, use 0.0]
  sleep_h_last_night: from wellness endpoint (most recent night)
  last_session_rpe: most recent rpe field in session-log.json (null if empty)
  ankle_pain_score: from current-state.md (0 if not present)
  ankle_quality_cleared: from current-state.md (True once 4 consecutive pain-free quality sessions confirmed)
  temp_c: today's forecast ambient temp — use 18.0 as fallback if unavailable
  dew_point_c: today's forecast dew point — use 10.0 as fallback if unavailable

Step 4 — Identify today's planned session from the events endpoint. Map to session_type:
  Threshold/FTP intervals -> bike_threshold
  Z2 / long ride -> bike_z2
  VO2max -> bike_vo2
  Race-pace bike -> bike_race_pace
  Run intervals / tempo -> run_quality
  Easy run / walk-run -> run_easy
  Long run -> run_long
  Brick -> brick
  Swim -> swim
  Gym -> strength
  No session planned -> output "Rest day — no session planned." and stop.

Also extract from the planned session event:
  target_intensity (if not explicit, derive from session type: threshold=1.0, race_pace=0.72, z2=0.65, vo2=1.10)
  interval_count (null if not an interval session)
  interval_duration_min (null if not an interval session)
  recovery_min (null if not an interval session)
  total_duration_min

Step 5 — Call the modulation engine:
  python3 /Users/diamondpeakconsulting/diamondpeak-site/ClaudeCoach/ironman-analysis/scripts/modulate.py '<json with planned and readiness keys>'

Step 6 — If modified or swapped_to_z2: push the adjusted session to Intervals.icu via Bash:
  python3 ClaudeCoach/lib/icu_fetch.py --athlete {slug} --endpoint push_workout --payload '{{"sport":"...", "date":"{today}", "name":"...", "description":"...", "planned_training_load": N}}'
  If go == false: push a recovery note workout with description "BLOCKED: [R1 reason from reasoning trail]".
  If no rules fired: no push needed.

Step 7 — Output the prescription card in exactly this format:

---
**Today: [session name] — [GO / MODIFIED / SWAPPED / BLOCKED]**

| Field | Planned | Prescribed |
|---|---|---|
| Intensity | X% FTP | Y% FTP |
| Intervals | N x M min | N' x M min |
| Recovery | X min | X min |
| Duration | X min | X min |

**Reasoning trail(s):**
- [L2 trail for each fired rule — format: (signal with real number) -> (rule) -> (adjustment) -> (expected effect)]

*[One-sentence summary]*

---

If no rules fired: output "Today: [session name] — execute as planned." and the planned targets only (no reasoning trails section).

Step 8 — Update current-state.md: in the "Off-plan in last 7 days" section, note today's prescribed session status (modified/swapped/blocked) and the reason if any rule fired. Also update ankle section if today's prescription was affected by ankle status.
Run: git add ClaudeCoach/athletes/{slug}/current-state.md && git pull --rebase origin main && git commit -m "prescription: {today} [status]" && git push origin main

Step 9 — Send Telegram notification if session was modified, swapped, or blocked. Message under 200 characters:
  "[session name]: [one-line summary of change]"
  Do not send if session is unchanged.
  Run: python3 {NOTIFY} --chat-id CHAT_ID "message"
  (Replace CHAT_ID with the value from athletes.json for slug={slug})
"""


def run_for_athlete(slug: str, cfg: dict) -> str | None:
    name      = cfg.get("name", slug)
    race_name = cfg.get("race_name", "upcoming race")
    chat_id   = str(cfg.get("chat_id", ""))

    prompt = build_prompt(slug, name, race_name)

    with tempfile.NamedTemporaryFile(
        mode="w", prefix="claudecoach_prescription_", delete=False, suffix=".txt"
    ) as f:
        f.write(prompt)
        prompt_file = f.name

    try:
        result = subprocess.run(
            [CLAUDE, "-p", open(prompt_file).read(),
             "--allowedTools", TOOLS,
             "--model", "claude-sonnet-4-6"],
            capture_output=True, text=True,
            cwd=PROJECT_DIR,
        )
        output = result.stdout.strip()
        stderr = result.stderr.strip()
        if stderr:
            with open(LOG_FILE, "a") as lf:
                lf.write(f"[prescription:{slug}] STDERR: {stderr}\n")
        return output or None
    except Exception as e:
        with open(LOG_FILE, "a") as lf:
            lf.write(f"[prescription:{slug}] Exception: {e}\n")
        return None
    finally:
        os.unlink(prompt_file)


def main():
    athletes = json.loads(CONFIG.read_text())
    for slug, cfg in athletes.items():
        if not cfg.get("active"):
            continue
        chat_id = str(cfg.get("chat_id", ""))
        output = run_for_athlete(slug, cfg)
        with open(LOG_FILE, "a") as lf:
            lf.write(f"[prescription:{slug}] {'output sent' if output else 'no output'}\n")
        if output:
            print(output, flush=True)
            if chat_id:
                subprocess.run(
                    ["python3", str(NOTIFY), "--chat-id", chat_id, output[:4000]],
                    cwd=PROJECT_DIR,
                )
    trim_log(LOG_FILE)

    # Refresh site data after prescriptions — background, non-blocking
    subprocess.Popen(
        ["python3",
         "/Users/diamondpeakconsulting/diamondpeak-site/ClaudeCoach/scripts/refresh-site-data.py"],
        stdout=open(LOG_DIR / "refresh.log", "a"),
        stderr=subprocess.STDOUT,
    )


if __name__ == "__main__":
    main()
