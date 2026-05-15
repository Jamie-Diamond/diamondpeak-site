#!/usr/bin/env python3
"""
Daily watchdog — fires a Telegram notification only if a trigger trips.
Runs via VM crontab at 05:30 daily. Loops over all active athletes.
Safe to run manually: python3 ClaudeCoach/scripts/watchdog.py
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
LOG_FILE    = LOG_DIR / "watchdog.log"

TOOLS = "Read,Write,Edit,Bash"


def trim_log(path: Path, max_lines: int = 5000):
    try:
        lines = path.read_text().splitlines()
        if len(lines) > max_lines:
            path.write_text("\n".join(lines[-max_lines:]) + "\n")
    except Exception:
        pass


def build_prompt(slug: str, name: str, race_name: str, race_date: str) -> str:
    today = date.today().isoformat()
    athlete_dir = BASE / "athletes" / slug

    return f"""You are running the daily watchdog check for {name}'s {race_name} coaching system.
Run silently — only produce output if a trigger fires.

Read these files (skip any that do not exist):
- {athlete_dir}/current-state.md
- {athlete_dir}/current-state.json
- {athlete_dir}/reference/rules.md
- {athlete_dir}/reference/decision-points.md
- {athlete_dir}/session-log.json
- {athlete_dir}/heat-log.json

Pull live data via Bash (use today's date {today} for all calculations):
  python3 ClaudeCoach/lib/icu_fetch.py --athlete {slug} --endpoint profile
  python3 ClaudeCoach/lib/icu_fetch.py --athlete {slug} --endpoint fitness --days 14
  python3 ClaudeCoach/lib/icu_fetch.py --athlete {slug} --endpoint history --days 14
  python3 ClaudeCoach/lib/icu_fetch.py --athlete {slug} --endpoint wellness --days 14

Evaluate these triggers in order (skip any whose required data files are missing):
T1 (Tier 2): ATL > CTL + 25 for 3+ consecutive days
T2 (Tier 2): CTL ramp >4/wk while ankle still in rehab (check current-state.md ankle quality-sessions-resumed field)
T3 (Tier 1): HRV trend down >7% over last 7 days
T4 (Tier 1): Sleep <7h for 3+ days in last 7 (skip if no sleep data available)
T5 (Tier 1): Missed planned sessions >=2 in last rolling 7 days
T6 (Tier 1): Aerobic decoupling >5% on any Z2 ride in last 7 days (check via activity_detail for rides with IF < 0.75):
  python3 ClaudeCoach/lib/icu_fetch.py --athlete {slug} --endpoint activity_detail --activity-id ID
T7 (Tier 1): From 15 May 2026 only — sum of dose in heat-log.json for last 14 days < 3.0 (skip if heat-log.json does not exist)
T8 (Tier 2): From 15 May 2026 only — most recent date in heat-log.json is >7 days ago (skip if heat-log.json does not exist)
T9 (Tier 2): Decision-point action due within 7 days and not marked done in current-state.json open_actions[].status
  - Read {athlete_dir}/reference/decision-points.md for dated items (skip if file missing)
  - Cross-check against open_actions in current-state.json; fire for any item whose due date <= today+7 and status != "done"
  - Example fire: "FTP retest due 2026-05-31 — not yet done"
T10 (Tier 2): Run weekly km increase >10% week-on-week
  - Sum run distance (km) from history endpoint for Mon–today (current week)
  - Sum run distance for the 7 days prior (last week)
  - Also cross-check current-state.json ankle.weekly_run_km_this_week vs ankle.weekly_run_km_last_week (if fields exist)
  - Fire if this_week_km > last_week_km * 1.10 AND last_week_km > 0
  - Fire message: "warning T10: run km +X% week-on-week ([this]km vs [last]km) — 10% cap applies"

If NO triggers fire: output nothing. Silent run.

If ANY trigger fires:
1. Send ONE Telegram notification (under 200 characters): "warning [trigger]: [action]" (Tier 2) or "info [trigger]: [note]" (Tier 1). Multiple triggers: list names, lead with highest tier.
   Run: python3 {NOTIFY} --chat-id CHAT_ID "message"
   (Replace CHAT_ID with the appropriate value from athletes.json for slug={slug})
2. Update current-state.md — append to the relevant section with today's date and trigger name + signal value. Do not rewrite sections that do not need updating.
3. Run: git add ClaudeCoach/athletes/{slug}/current-state.md && git fetch origin && git rebase --autostash origin/main && git commit -m "watchdog: [trigger list] {today}" && git push origin main
4. Output one L2 reasoning trail per trigger to stdout:
   [signal with real number] -> [rule: T1-T10] -> [suggested adjustment] -> [expected effect]
   Example: "ATL 148 vs CTL 121 for 4 days -> T1 (ATL > CTL +25) -> insert recovery day -> TSB recovers ~8 pts by weekend"
"""


def run_for_athlete(slug: str, cfg: dict) -> str | None:
    name      = cfg.get("name", slug)
    race_name = cfg.get("race_name", "upcoming race")
    race_date = cfg.get("race_date", "")
    chat_id   = str(cfg.get("chat_id", ""))

    prompt = build_prompt(slug, name, race_name, race_date)

    with tempfile.NamedTemporaryFile(
        mode="w", prefix="claudecoach_watchdog_", delete=False, suffix=".txt"
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
                lf.write(f"[watchdog:{slug}] STDERR: {stderr}\n")
        return output or None
    except Exception as e:
        with open(LOG_FILE, "a") as lf:
            lf.write(f"[watchdog:{slug}] Exception: {e}\n")
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
            lf.write(f"[watchdog:{slug}] {'triggered' if output else 'silent'}\n")
        if output:
            print(output, flush=True)
            if chat_id:
                subprocess.run(
                    ["python3", str(NOTIFY), "--chat-id", chat_id, output[:4000]],
                    cwd=PROJECT_DIR,
                )
    trim_log(LOG_FILE)


if __name__ == "__main__":
    main()
