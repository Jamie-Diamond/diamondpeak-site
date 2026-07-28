#!/usr/bin/env python3
"""Capture reminder — RETIRED 2026-07-28. Its job is now Case A2 of evening-checkin.py.

It ran at 20:10, twenty minutes before the night-before brief and fifty before the evening
check-in, and asked its own question ("Log <session> — say 'log session'"). Three evening
pushes from three crons that knew nothing about each other is the defect; the ask itself is
not. So the ask moved into the 21:00 check-in, which now reads the same
.capture-reminded.json ledger and makes it at most once per activity_id.

The 20:10 crontab entry is removed. main() is a no-op rather than working code so that
re-adding the cron cannot silently restore the second push; _build_prompt is kept below
because Case A2 of evening-checkin.py is derived from it and the two must stay comparable.
"""
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
sys.path.insert(0, str(BASE / "lib"))
import ops_log

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
            [CLAUDE, "-p", "--allowedTools", TOOLS, "--model", "claude-haiku-4-5-20251001"],
            input=prompt,  # prompt on stdin, not argv (MAX_ARG_STRLEN)
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
        ops_log.record_run("capture-reminder", athlete=slug, ok=True, detail="sent")
    elif result.returncode == 0:
        # HEARTBEAT. "No unlogged key session" is the COMMON case and the prompt
        # demands total silence for it, so empty stdout on a clean exit is a success —
        # it ran and correctly said nothing.
        ops_log.record_run("capture-reminder", athlete=slug, ok=True,
                           detail="silent (nothing unlogged)")
    else:
        # ...but empty stdout on a NON-ZERO exit is the CLI failing, not the model
        # choosing silence, and the two are indistinguishable downstream. This script
        # spawns the CLI directly rather than through claude_call, so it has no auth
        # detection at all: without this branch a dead token would look like "correctly
        # stayed silent" every night, forever.
        tail = (result.stdout or "")[-200:].replace("\n", " ")
        ops_log.record_run("capture-reminder", athlete=slug, ok=False,
                           detail=f"claude CLI exited {result.returncode} with no "
                                  f"<notify> block: {tail}")


def main():
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] capture-reminder is RETIRED — its job is Case A2 of "
          f"evening-checkin.py (21:00). Sending nothing.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    main()
