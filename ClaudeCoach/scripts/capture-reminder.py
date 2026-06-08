#!/usr/bin/env python3
"""Capture reminder — runs via VM crontab at 20:00 daily. Loops over all active athletes."""
import json, subprocess, sys
from datetime import datetime
from pathlib import Path

BASE            = Path(__file__).parent.parent  # ClaudeCoach/
PROJECT_DIR     = str(BASE.parent)
CLAUDE          = "/usr/bin/claude"
NOTIFY          = BASE / "telegram/notify.py"
ATHLETES_CONFIG = BASE / "config/athletes.json"
LOG_DIR         = Path.home() / "Library/Logs/ClaudeCoach"
LOG_DIR.mkdir(parents=True, exist_ok=True)

TOOLS = "Read,Bash"


def _build_prompt(slug, first_name, reminded_ids=None):
    reminded = ", ".join(reminded_ids or []) or "(none)"
    return f"""\
You are running the evening session capture reminder for {first_name}.

Step 1 — Fetch data via Bash:
  python3 ClaudeCoach/lib/icu_fetch.py --athlete {slug} --endpoint profile
  python3 ClaudeCoach/lib/icu_fetch.py --athlete {slug} --endpoint history --days 2

Step 2 — Read ClaudeCoach/athletes/{slug}/session-log.json.

Check for completed activities in the last 36 hours that meet ALL of:
1. TSS > 40 OR duration > 45 minutes
2. Sport is Ride, VirtualRide, Run, VirtualRun, Brick, or Swim (skip Strength)
3. No entry in session-log.json with a matching activity_id
4. activity_id is NOT in the already-reminded list: {reminded}
   (one reminder per session — repeat nagging is not helpful)

OUTPUT RULES — follow exactly:
- If an unlogged key session is found: output <notify ids="[comma-separated activity_ids]">Log [session name] — say 'log session'</notify>
- If no unlogged key sessions exist: output nothing at all. No explanation. No confirmation. Silence.

Do not output anything outside the <notify> tag under any circumstances."""


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
    reminded_file = adir / ".capture-reminded.json"
    try:
        reminded_ids = json.loads(reminded_file.read_text()) if reminded_file.exists() else []
    except Exception:
        reminded_ids = []
    prompt = _build_prompt(slug, first_name, reminded_ids)

    with open(log_file, "a") as lf:
        result = subprocess.run(
            [CLAUDE, "-p", prompt, "--allowedTools", TOOLS, "--model", "claude-haiku-4-5-20251001"],
            stdout=subprocess.PIPE, stderr=lf, text=True,
            cwd=PROJECT_DIR, timeout=120,
        )

    output = (result.stdout or "").strip()
    import re
    m = re.search(r'<notify(?:\s+ids="([^"]*)")?>(.*?)</notify>', output, re.DOTALL | re.IGNORECASE)
    if m:
        notify(m.group(2).strip(), chat_id)
        new_ids = [i.strip() for i in (m.group(1) or "").split(",") if i.strip()]
        if new_ids:
            try:
                reminded_file.write_text(json.dumps((reminded_ids + new_ids)[-50:]))
            except Exception:
                pass


def main():
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] capture-reminder starting", file=sys.stderr)
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
            print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}][{slug}] capture-reminder error: {exc}", file=sys.stderr)


if __name__ == "__main__":
    main()
