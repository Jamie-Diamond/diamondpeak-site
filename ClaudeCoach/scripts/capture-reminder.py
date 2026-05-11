#!/usr/bin/env python3
"""Capture reminder — runs via VM crontab at 20:00 daily. Loops over all active athletes."""
import json, subprocess, sys
from pathlib import Path

BASE            = Path(__file__).parent.parent  # ClaudeCoach/
PROJECT_DIR     = str(BASE.parent)
CLAUDE          = "/usr/bin/claude"
NOTIFY          = BASE / "telegram/notify.py"
ATHLETES_CONFIG = BASE / "config/athletes.json"
LOG_DIR         = Path.home() / "Library/Logs/ClaudeCoach"
LOG_DIR.mkdir(parents=True, exist_ok=True)

TOOLS = "Read,Bash"


def _build_prompt(slug, first_name):
    return f"""\
You are running the evening session capture reminder for {first_name}. Run silently — only produce output if there is an unlogged key session.

Step 1 — Fetch data via Bash:
  python3 ClaudeCoach/lib/icu_fetch.py --athlete {slug} --endpoint profile
  python3 ClaudeCoach/lib/icu_fetch.py --athlete {slug} --endpoint history --days 2

Step 2 — Read ClaudeCoach/athletes/{slug}/session-log.json.

Check for completed activities in the last 36 hours that meet ALL of:
1. TSS > 40 OR duration > 45 minutes
2. Sport is Ride, VirtualRide, Run, VirtualRun, Brick, or Swim (skip Strength)
3. No entry in session-log.json with a matching activity_id

If an unlogged key session is found:
- Output exactly one line: "Log [session name] — say 'log session'"

If no unlogged key sessions: output nothing."""


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
    log_file = LOG_DIR / "capture-reminder.log"

    profile = {}
    if (adir / "profile.json").exists():
        try:
            profile = json.loads((adir / "profile.json").read_text())
        except Exception:
            pass

    first_name = profile.get("name", slug).split()[0]
    prompt = _build_prompt(slug, first_name)

    with open(log_file, "a") as lf:
        result = subprocess.run(
            [CLAUDE, "-p", prompt, "--allowedTools", TOOLS],
            capture_output=True, text=True,
            cwd=PROJECT_DIR, timeout=120,
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
            print(f"[{slug}] capture-reminder error: {exc}", file=sys.stderr)


if __name__ == "__main__":
    main()
