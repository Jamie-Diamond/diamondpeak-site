#!/usr/bin/env python3
"""Night-before brief — runs via VM crontab at 20:30 daily. Loops over all active athletes."""
import json, subprocess, sys
from datetime import date, datetime, timedelta
from pathlib import Path

BASE            = Path(__file__).parent.parent  # ClaudeCoach/
PROJECT_DIR     = str(BASE.parent)
CLAUDE          = "/usr/bin/claude"
NOTIFY          = BASE / "telegram/notify.py"
ATHLETES_CONFIG = BASE / "config/athletes.json"
LOG_DIR         = Path.home() / "Library/Logs/ClaudeCoach"
LOG_DIR.mkdir(parents=True, exist_ok=True)
sys.path.insert(0, str(BASE / "lib"))
sys.path.insert(0, str(BASE / "ironman-analysis"))
import claude_call
from progression import long_run_cap_km as _lr_cap

TOOLS = "Read,Bash"


def _build_prompt(slug, first_name, ftp, css, run_threshold, race_name, injuries, long_run_cap_km=None):
    today = date.today().isoformat()
    tomorrow = (date.today() + timedelta(days=1)).isoformat()

    long_run_cap_block = ""
    if long_run_cap_km is not None:
        long_run_cap_block = (
            f"\n## Long-run cap (pre-computed — authoritative)\n"
            f"Ceiling: {long_run_cap_km:.1f} km — do not reference or target a distance above this for tomorrow's long run.\n"
        )

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

Step 3 — If no events tomorrow, or only events with planned Load < 30 AND duration < 40 min: output nothing. Stop.
{long_run_cap_block}
Step 4 — Output the night-before brief in Telegram Markdown (no preamble, no sign-off):

*Tomorrow — [session name]*

[Sport-specific targets — 2-4 bullets:]
Ride: • NP target [W] (IF [X.XX]) • HR cap [bpm]
Run: • Target pace [/km] • HR cap [bpm]{run_note}
Swim: • Target pace [/100m] vs CSS {css or '?'} • Main set structure
Strength: • Main focus • Key movements

*Nutrition:* [g/hr carbs + ml/hr fluid — calibrated to session length and intensity. Zero if easy/recovery.]
*Sleep:* ≥8h tonight
*Form:* [value] ([Fresh / In training / Heavy]){injury_flag}

{threshold_line}
Race: {race_name}
Keep the entire brief under 120 words. Never ask questions.
Wrap your entire output in <telegram> and </telegram> tags. If Step 3 says output nothing, output empty tags: <telegram></telegram>. Output nothing outside those tags."""


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

    # Pre-compute tomorrow's long-run distance cap the same way morning-checkin
    # does, so a long run can't be quoted past the progression ceiling here either.
    long_run_cap = None
    try:
        from primitives.modulation import classify_session_type as _lr_classify
        from icu_api import IcuClient as _Icu
        tomorrow = (date.today() + timedelta(days=1)).isoformat()
        athlete_cfg_full = json.loads(ATHLETES_CONFIG.read_text())[slug]
        events = _Icu(athlete_cfg_full["icu_athlete_id"], athlete_cfg_full["icu_api_key"]).get_events(tomorrow, tomorrow)
        session_log_path = adir / "session-log.json"
        session_log = json.loads(session_log_path.read_text()) if session_log_path.exists() else []
        long_run_cap = _lr_cap(events, session_log, _lr_classify)
    except Exception as exc:
        print(f"[{slug}] long-run cap pre-compute failed: {exc}", file=sys.stderr)

    prompt = _build_prompt(slug, first_name, ftp, css, run_threshold, race_name, injuries,
                           long_run_cap_km=long_run_cap)

    with open(log_file, "a") as lf:
        result = claude_call.run_claude(
            prompt, model=claude_call.SONNET, fallback=[claude_call.OPUS],
            allowed_tools=TOOLS, stderr=lf, cwd=PROJECT_DIR, timeout=180, label=slug,
        )

    import re as _re
    raw = (result.stdout or "").strip()
    m = _re.search(r"<telegram>(.*?)</telegram>", raw, _re.DOTALL)
    output = m.group(1).strip() if m else ""
    if output:
        notify(output, chat_id)


def main():
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] night-before-brief starting", file=sys.stderr)
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
            print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}][{slug}] night-before-brief error: {exc}", file=sys.stderr)


if __name__ == "__main__":
    main()
