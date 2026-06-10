#!/usr/bin/env python3
"""Evening check-in — runs via VM crontab at 21:00 daily. Loops over all active athletes."""
import json, os, subprocess, sys, time
from datetime import date, datetime
from pathlib import Path

BASE            = Path(__file__).parent.parent  # ClaudeCoach/
PROJECT_DIR     = str(BASE.parent)
CLAUDE          = "/usr/bin/claude"
NOTIFY          = BASE / "telegram/notify.py"
ATHLETES_CONFIG = BASE / "config/athletes.json"
LOG_DIR         = Path.home() / "Library/Logs/ClaudeCoach"
LOG_DIR.mkdir(parents=True, exist_ok=True)
sys.path.insert(0, str(BASE / "lib"))
from coaching_levels import level_block as _level_block
import ops_log

TOOLS = "Read,Bash"


def _build_prompt(slug, first_name, injuries, pain_next_morning=0, coaching_level="mid"):
    today = date.today().isoformat()
    # Ask the injury question only if pain_next_morning > 0 — if last morning score
    # was 0, the ankle is fine and we don't ask every single run.
    if injuries and pain_next_morning > 0:
        injury_case = "  - Run: \"Good [X km] run done. Injury pain during today's run? (0-10)\""
    else:
        injury_case = "  - Run: \"Good [X km] run done. RPE and how did it feel?\""

    return f"""\
Evening training log check for {first_name}.

{_level_block(coaching_level)}


Step 1 — Fetch data via Bash:
  python3 ClaudeCoach/lib/icu_fetch.py --athlete {slug} --endpoint history --days 1
  python3 ClaudeCoach/lib/icu_fetch.py --athlete {slug} --endpoint events --start {today} --end {today}

Step 2 — Read ClaudeCoach/athletes/{slug}/session-log.json (check which activity_ids are already stubbed).
An entry with a matching activity_id counts as ACCOUNTED FOR even if stub is true or rpe/pain
fields are null — missing field data is the capture-reminder's job, never yours. Case A applies
ONLY when there is NO entry at all for the activity.

Step 3 — Decide whether to send a message:

Case A — A completed activity exists NOT yet in session-log.json:
  Send one specific question (max 2 sentences, no preamble):
{injury_case}
  - Ride (>90 min): "Solid [X km] ride done. Nutrition — roughly g carbs/hr and bottles?"
  - Swim: "Swim done — [X m] at [pace]. RPE and how did it feel?"
  - Strength: "Strength session done. RPE and main focus?"

Case B — A planned session has NO matching completed activity AND it's after 19:00:
  Before sending: read ClaudeCoach/athletes/{slug}/current-state.md — if there is any note from today indicating the session was swapped, substituted, or intentionally skipped, suppress the message entirely (treat as Case C).
  Otherwise send: "Did the [session name] happen today?"

Case C — All sessions accounted for and already stubbed: produce no output.

Case D — No planned sessions and no activities: produce no output.

Priority: Case A > Case B > silence. Only ever send ONE message.

OUTPUT FORMAT — follow exactly:
- Cases A or B: wrap your single message in <notify>...</notify> tags. Nothing outside the tags.
- Cases C or D: output nothing at all. No tags, no text, no punctuation. Absolute silence."""


def notify(msg, chat_id, slug=""):
    """Send via notify.py, retry once; alert the ops log if delivery fails."""
    for _attempt in (1, 2):
        try:
            r = subprocess.run(
                ["python3", str(NOTIFY), "--chat-id", str(chat_id), msg],
                cwd=PROJECT_DIR, timeout=15,
            )
            if r.returncode == 0:
                return True
        except Exception:
            pass
    ops_log.alert("evening-checkin", "Telegram send failed after retry", athlete=slug)
    return False


def run_athlete(slug, athlete_cfg):
    adir = BASE / f"athletes/{slug}"
    chat_id = athlete_cfg.get("chat_id", "")
    log_file = LOG_DIR / "evening-checkin.log"
    if not chat_id:
        print(f"[{slug}] SKIP: no chat_id in athletes.json", file=sys.stderr)
        return

    profile = {}
    if (adir / "profile.json").exists():
        try:
            profile = json.loads((adir / "profile.json").read_text())
        except Exception:
            pass

    first_name = profile.get("name", slug).split()[0]
    injuries = profile.get("injuries", [])

    pain_next_morning = 0
    state_f = adir / "current-state.json"
    if state_f.exists():
        try:
            ankle = json.loads(state_f.read_text()).get("ankle", {})
            pain_next_morning = ankle.get("pain_next_morning", 0) or 0
        except Exception:
            pass

    coaching_level = profile.get("coaching_level", "mid")
    prompt = _build_prompt(slug, first_name, injuries, pain_next_morning, coaching_level=coaching_level)

    with open(log_file, "a") as lf:
        result = subprocess.run(
            [CLAUDE, "-p", prompt, "--allowedTools", TOOLS, "--model", "claude-haiku-4-5-20251001"],
            stdout=subprocess.PIPE, stderr=lf, text=True,
            cwd=PROJECT_DIR, timeout=180,
        )

    output = (result.stdout or "").strip()
    import re
    m = re.search(r'<notify>(.*?)</notify>', output, re.DOTALL | re.IGNORECASE)
    if m:
        if notify(m.group(1).strip(), chat_id, slug=slug):
            ops_log.record_run("evening-checkin", athlete=slug, ok=True, detail="sent")
    else:
        # Cases C/D — silence is the designed outcome; record it so the digest
        # can tell "ran and had nothing to say" from "never ran".
        ops_log.record_run("evening-checkin", athlete=slug, ok=True, detail="silent")


def main():
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] evening-checkin starting", file=sys.stderr)
    try:
        athletes = json.loads(ATHLETES_CONFIG.read_text())
    except Exception as e:
        print(f"[{ts}] Failed to load athletes config: {e}", file=sys.stderr)
        sys.exit(1)

    stagger = int(os.environ.get("ATHLETE_STAGGER_S", "30"))
    processed = False
    for slug, cfg in athletes.items():
        if not cfg.get("active", True):
            continue
        if processed:
            time.sleep(stagger)   # space Claude calls — rate-limit contention
        processed = True
        try:
            run_athlete(slug, cfg)
        except Exception as exc:
            print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}][{slug}] evening-checkin error: {exc}", file=sys.stderr)
            ops_log.alert("evening-checkin", f"exception: {exc}", athlete=slug)


if __name__ == "__main__":
    main()
