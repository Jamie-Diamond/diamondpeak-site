#!/usr/bin/env python3
"""Night-before brief — runs via VM crontab at 20:30 daily. Loops over all active athletes."""
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


def _build_prompt(slug, first_name, ftp, css, run_threshold, race_name, injuries):
    today = date.today().isoformat()
    tomorrow = (date.today() + timedelta(days=1)).isoformat()

    # Build injury flag line
    injury_flag = ""
    if injuries:
        injury_flag = (
            "\n[If any injury pain score >=3 in current-state.json AND a run is planned: "
            "add \"⚠️ Check injury before starting — note score after.\"]"
        )

    # Build athlete thresholds line
    thresholds = []
    if ftp:
        thresholds.append(f"FTP {ftp} W")
    if run_threshold:
        thresholds.append(f"run threshold {run_threshold}")
    if css:
        thresholds.append(f"swim CSS {css}/100m")
    threshold_line = f"{first_name}: " + ", ".join(thresholds) + "." if thresholds else ""

    # Run-specific notes for injury athletes
    run_note = ""
    if injuries:
        run_note = " Note any active injury protocols from current-state.json."

    return f"""\
You are generating the night-before session brief for {first_name}.

Step 1 — Fetch data via Bash:
  python3 ClaudeCoach/lib/icu_fetch.py --athlete {slug} --endpoint profile
  python3 ClaudeCoach/lib/icu_fetch.py --athlete {slug} --endpoint events --start {tomorrow} --end {tomorrow}
  python3 ClaudeCoach/lib/icu_fetch.py --athlete {slug} --endpoint wellness --days 3

Step 2 — Read ClaudeCoach/athletes/{slug}/current-state.json (last injury pain score, if any).

Step 3 — If no events tomorrow, or only events with TSS < 30 AND duration < 40 min: output nothing. Stop.

Step 4 — Output the night-before brief in Telegram Markdown (no preamble, no sign-off):

*Tomorrow — [session name]*

[Sport-specific targets — 2-4 bullets:]
Ride: • NP target [W] (IF [X.XX]) • HR cap [bpm]
Run: • Target pace [/km] • HR cap [bpm]{run_note}
Swim: • Target pace [/100m] vs CSS {css or '?'} • Main set structure
Strength: • Main focus • Key movements

*Nutrition:* [g/hr carbs + ml/hr fluid — calibrated to session length and intensity. Zero if easy/recovery.]
*Sleep:* ≥8h tonight
*Form:* TSB [value] ([Fresh / Load / Heavy]){injury_flag}

{threshold_line}
Race: {race_name}
Keep the entire brief under 120 words. Never ask questions."""


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
    log_file = LOG_DIR / "night-before-brief.log"

    profile = {}
    if (adir / "profile.json").exists():
        try:
            profile = json.loads((adir / "profile.json").read_text())
        except Exception:
            pass

    first_name = profile.get("name", slug).split()[0]
    ftp = profile.get("ftp_watts")
    css = profile.get("swim_css_per_100m")
    run_threshold = profile.get("run_threshold_pace_per_km")
    race_name = profile.get("race_name") or athlete_cfg.get("race_name", "your race")
    injuries = profile.get("injuries", [])

    prompt = _build_prompt(slug, first_name, ftp, css, run_threshold, race_name, injuries)

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
            print(f"[{slug}] night-before-brief error: {exc}", file=sys.stderr)


if __name__ == "__main__":
    main()
