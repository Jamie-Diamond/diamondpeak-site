#!/usr/bin/env python3
"""Morning briefing — runs via VM crontab at 06:20 daily. Loops over all active athletes."""
import json, subprocess, sys
from datetime import date, timedelta
from pathlib import Path

BASE            = Path(__file__).parent.parent  # ClaudeCoach/
PROJECT_DIR     = str(BASE.parent)
CLAUDE          = "/usr/bin/claude"
NOTIFY          = BASE / "telegram/notify.py"
ATHLETES_CONFIG = BASE / "config/athletes.json"
LOG_DIR         = Path.home() / "Library/Logs/ClaudeCoach"
LOG_DIR.mkdir(parents=True, exist_ok=True)

TOOLS = "Read,Bash"


def _build_prompt(slug, first_name, race_name, race_date, days_to_race, injuries):
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

    return f"""\
You are generating the morning briefing for {first_name}'s training day.

Step 1 — Fetch data via Bash:
  python3 ClaudeCoach/lib/icu_fetch.py --athlete {slug} --endpoint wellness --days 2
  python3 ClaudeCoach/lib/icu_fetch.py --athlete {slug} --endpoint events --start {today} --end {today}
  python3 ClaudeCoach/lib/icu_fetch.py --athlete {slug} --endpoint wellness --days 3

Step 2 — Read:
- ClaudeCoach/athletes/{slug}/current-state.md (open actions, watchdog flags)
- ClaudeCoach/athletes/{slug}/current-state.json (weight_readings, injury pain scores)

Step 3 — Determine ONE question to ask (or none):
{injury_question}
- Else: no question

Step 4 — Output the morning card in Telegram Markdown (no preamble, no sign-off):

*Good morning — [Day date, e.g. Sat 9 May]*

*Today:* [session name] · [planned TSS if available] TSS · [duration] min
*TSB:* [value] ([Fresh / Load / Heavy])
*Sleep:* [hours]h · *HRV:* [value] · *RHR:* [value]

[If watchdog flag active: ⚠️ [flag]: [one-line note]]
[If decision-point due within 7 days: 📌 [action] due [date]]

[Question if applicable — one line]

_{days_to_race} days to {race_name}_

Athlete context: {first_name} — {race_name} ({race_date}). Injuries: {injuries_note}.
TSB zones: >+5 = Fresh, 0 to −20 = Load, <−20 = Heavy.
If no planned session: "Rest day — recovery only". Omit unavailable fields silently.
Never ask for subjective mood/fatigue/motivation scores."""


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

    prompt = _build_prompt(slug, first_name, race_name, race_date_str, days_to_race, injuries)

    with open(log_file, "a") as lf:
        result = subprocess.run(
            [CLAUDE, "-p", prompt, "--allowedTools", TOOLS],
            stdout=subprocess.PIPE, stderr=lf, text=True,
            cwd=PROJECT_DIR, timeout=180,
        )

    output = (result.stdout or "").strip()
    if output:
        notify(output, chat_id)


def main():
    try:
        athletes = json.loads(ATHLETES_CONFIG.read_text())
    except Exception as e:
        print(f"Failed to load athletes config: {e}", file=sys.stderr)
        sys.exit(1)

    for slug, cfg in athletes.items():
        if not cfg.get("active", True):
            continue
        try:
            run_athlete(slug, cfg)
        except Exception as exc:
            print(f"[{slug}] morning-checkin error: {exc}", file=sys.stderr)


if __name__ == "__main__":
    main()
