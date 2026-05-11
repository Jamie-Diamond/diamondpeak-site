#!/usr/bin/env python3
"""Evening check-in — runs via VM crontab at 21:00 daily. Loops over all active athletes."""
import json, subprocess, sys
from datetime import date
from pathlib import Path

BASE            = Path(__file__).parent.parent  # ClaudeCoach/
PROJECT_DIR     = str(BASE.parent)
CLAUDE          = "/usr/bin/claude"
NOTIFY          = BASE / "telegram/notify.py"
ATHLETES_CONFIG = BASE / "config/athletes.json"
LOG_DIR         = Path.home() / "Library/Logs/ClaudeCoach"
LOG_DIR.mkdir(parents=True, exist_ok=True)

TOOLS = "Read,Bash"


def _build_prompt(slug, first_name, injuries):
    today = date.today().isoformat()
    injury_case = (
        "  - Run: \"Good [X km] run done. Injury pain during and this morning? (0-10)\""
        if injuries else
        "  - Run: \"Good [X km] run done. RPE and how did it feel?\""
    )

    return f"""\
Evening training log check for {first_name}.

Step 1 — Fetch data via Bash:
  python3 ClaudeCoach/lib/icu_fetch.py --athlete {slug} --endpoint history --days 1
  python3 ClaudeCoach/lib/icu_fetch.py --athlete {slug} --endpoint events --start {today} --end {today}

Step 2 — Read ClaudeCoach/athletes/{slug}/session-log.json (check which activity_ids are already stubbed).

Step 3 — Decide whether to send a message:

Case A — A completed activity exists NOT yet in session-log.json:
  Send one specific question (max 2 sentences, no preamble):
{injury_case}
  - Ride (>90 min): "Solid [X km] ride done. Nutrition — roughly g carbs/hr and bottles?"
  - Swim: "Swim done — [X m] at [pace]. RPE and how did it feel?"
  - Strength: "Strength session done. RPE and main focus?"

Case B — A planned session has NO matching completed activity AND it's after 19:00:
  Send: "Did the [session name] happen today?"

Case C — All sessions accounted for and already stubbed: output nothing.

Case D — No planned sessions and no activities: output nothing.

Priority: Case A > Case B > silence. Only ever send ONE message."""


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
    log_file = LOG_DIR / "evening-checkin.log"

    profile = {}
    if (adir / "profile.json").exists():
        try:
            profile = json.loads((adir / "profile.json").read_text())
        except Exception:
            pass

    first_name = profile.get("name", slug).split()[0]
    injuries = profile.get("injuries", [])

    prompt = _build_prompt(slug, first_name, injuries)

    with open(log_file, "a") as lf:
        result = subprocess.run(
            [CLAUDE, "-p", prompt, "--allowedTools", TOOLS],
            capture_output=True, text=True,
            cwd=PROJECT_DIR, timeout=180,
            stderr=lf,
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
            print(f"[{slug}] evening-checkin error: {exc}", file=sys.stderr)


if __name__ == "__main__":
    main()
