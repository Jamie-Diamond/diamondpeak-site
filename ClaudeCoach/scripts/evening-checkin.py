#!/usr/bin/env python3
"""Evening check-in — runs via VM crontab at 21:00 daily. Loops over all active athletes."""
import json, subprocess, sys
from datetime import date, datetime
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

Case C — All sessions accounted for and already stubbed: produce zero output. No text at all.

Case D — No planned sessions and no activities: produce zero output. No text at all.

Priority: Case A > Case B > silence. Only ever send ONE message.
CRITICAL: In Cases C and D your entire response must be completely empty — not even a case label, not even "silence", not even a full stop. Empty string only."""


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
            [CLAUDE, "-p", prompt, "--allowedTools", TOOLS, "--model", "claude-haiku-4-5-20251001"],
            stdout=subprocess.PIPE, stderr=lf, text=True,
            cwd=PROJECT_DIR, timeout=180,
        )

    output = (result.stdout or "").strip()
    if output and not _is_meta(output):
        notify(output, chat_id)


_META_PREFIXES = ("case a", "case b", "case c", "case d", "silence", "no output", "nothing to send")

def _is_meta(text: str) -> bool:
    return text.lower().startswith(_META_PREFIXES)


def main():
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] evening-checkin starting", file=sys.stderr)
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
            print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}][{slug}] evening-checkin error: {exc}", file=sys.stderr)


if __name__ == "__main__":
    main()
