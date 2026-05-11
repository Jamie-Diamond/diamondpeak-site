#!/usr/bin/env python3
"""
Check for new activities and send a brief analysis to Telegram.
Run every 15 min via cron. Loops over all active athletes. Skips if already running.
"""
import json, subprocess, sys, time
from datetime import datetime
from pathlib import Path

BASE            = Path(__file__).parent.parent  # ClaudeCoach/
LOCK_FILE       = BASE / ".activity_watcher.lock"
NOTIFY          = BASE / "telegram/notify.py"
PROJECT_DIR     = str(BASE.parent)
CLAUDE          = "/usr/bin/claude"
ATHLETES_CONFIG = BASE / "config/athletes.json"

TOOLS = "Read,Write,Bash"


def _build_prompt(slug, first_name, ftp, injuries):
    """Build the per-athlete activity analysis prompt."""
    # Injury context for the athlete line and run analysis
    injury_line = ""
    if injuries:
        descs = "; ".join(
            f"{inj.get('location','unknown')} ({inj.get('description','')})"
            for inj in injuries if inj.get("location")
        )
        protocol = next((inj.get("protocol", "") for inj in injuries if inj.get("protocol")), "")
        injury_line = f" Injuries: {descs}." + (f" Protocol: {protocol}." if protocol else "")

    run_injury_ask = (
        "- Run: Line 1 = distance + avg GAP pace. Line 2 = HR cap adherence (cap 150 bpm)."
        " Line 3 = \"Injury pain score during and this morning? (0-10)\""
        if injuries else
        "- Run: Line 1 = distance + avg GAP pace. Line 2 = HR zone / feel check."
        " Line 3 = \"RPE and how did it feel?\""
    )

    return f"""\
Check for new activities for {first_name} and stub them into the session log.

Step 1 — Fetch data via Bash:
  python3 ClaudeCoach/lib/icu_fetch.py --athlete {slug} --endpoint profile
  python3 ClaudeCoach/lib/icu_fetch.py --athlete {slug} --endpoint history --days 3

Step 2 — Read ClaudeCoach/athletes/{slug}/session-log.json and note all existing activity_id values.

Step 3 — For the most recent activity that is NOT already in session-log.json:
  - Fetch full detail via Bash: python3 ClaudeCoach/lib/icu_fetch.py --athlete {slug} --endpoint activity_detail --activity-id <id>
  - Add a stub entry to ClaudeCoach/athletes/{slug}/session-log.json (prepend to array, most recent first).

  For Ride or Run:
    {{
      "activity_id": "<id>", "date": "<YYYY-MM-DD>", "name": "<name>", "sport": "<sport>",
      "tss": <tss>, "duration_min": <duration>, "distance_km": <distance or null>,
      "avg_power": <avg_power or null>, "norm_power": <norm_power or null>, "avg_hr": <avg_hr or null>,
      "rpe": null, "feel": null,
      "injury_pain_during": null, "injury_pain_next_morning": null,
      "nutrition_g_carb": null, "hydration_ml": null, "notes": null,
      "logged_at": "<current datetime as YYYY-MM-DDTHH:MM:SS>", "stub": true
    }}

  For Swim:
    {{
      "activity_id": "<id>", "date": "<YYYY-MM-DD>", "name": "<name>", "sport": "Swim",
      "tss": <tss>, "duration_min": <duration>, "distance_km": <distance_m / 1000>,
      "pace_per_100m": <avg pace in seconds per 100m — distance_m / (duration_s / 100)>,
      "avg_hr": <avg_hr or null>,
      "rpe": null, "feel": null, "notes": null, "logged_at": "<current datetime as YYYY-MM-DDTHH:MM:SS>", "stub": true
    }}
    Also append to ClaudeCoach/athletes/{slug}/swim-log.json:
    {{"date":"<YYYY-MM-DD>","activity_id":"<id>","name":"<name>","distance_m":<int>,"pace_per_100m":<seconds float>,"duration_min":<int>,"tss":<int or null>}}
    Include swim-log.json in the git add below.

  For WeightTraining / Strength:
    {{
      "activity_id": "<id>", "date": "<YYYY-MM-DD>", "name": "<name>", "sport": "Strength",
      "tss": <tss or null>, "duration_min": <duration>,
      "rpe": null, "feel": null, "notes": null, "logged_at": "<current datetime as YYYY-MM-DDTHH:MM:SS>", "stub": true
    }}

  - Write the updated array back to ClaudeCoach/athletes/{slug}/session-log.json.
  - Run via Bash: git -C {PROJECT_DIR} add ClaudeCoach/athletes/{slug}/session-log.json ClaudeCoach/athletes/{slug}/swim-log.json && git -C {PROJECT_DIR} commit -m "stub: <name> <date>" && git -C {PROJECT_DIR} push origin main

Step 4 — Respond in EXACTLY this format (no other text):
ACTIVITY_ID: <id or none>
ANALYSIS: <coaching message — see rules below>

{first_name}: FTP {ftp} W.{injury_line}

Rules for ANALYSIS (2-3 lines, max 400 chars):
- Ride: Line 1 = NP + IF. Line 2 (>90 min) = aerobic decoupling %. Line 3 = "Nutrition — g carbs/hr and bottles?"
{run_injury_ask}
- Swim: Line 1 = distance + pace vs CSS target (state +/- seconds). Line 2 = "RPE and how did it feel?"
- Strength: Line 1 = duration. Line 2 = "RPE and what was the main focus?"

For rides > 3 hours: also output a DECOUPLING line:
DECOUPLING: <activity_id>|<date>|<name>|<duration_min>|<intensity_factor>|<decoupling_pct>|<tss>
If ride < 3 hours or Pa:HR data unavailable: DECOUPLING: none

For structured sessions with >3 intervals:
SESSION_CHART: {{"name":"<activity name>","ftp":{ftp},"intervals":[{{"duration_seconds":600,"average_power":250,"type":"WORK"}},...}]}}
Fetch interval data from activity_detail endpoint. type values: WORK, RECOVERY, WARMUP, COOLDOWN.
If unstructured: SESSION_CHART: none

If no activities at all: ACTIVITY_ID: none  ANALYSIS: none"""


def _notify(msg, chat_id):
    try:
        subprocess.run(
            ["python3", str(NOTIFY), "--chat-id", str(chat_id), msg],
            cwd=PROJECT_DIR, timeout=15,
        )
    except Exception:
        pass


def load_state(state_file):
    if state_file.exists():
        return json.loads(state_file.read_text())
    return {"last_id": None}


def save_state(state, state_file):
    state_file.write_text(json.dumps(state))


def acquire_lock():
    if LOCK_FILE.exists():
        if time.time() - LOCK_FILE.stat().st_mtime < 300:
            return False
    LOCK_FILE.touch()
    return True


def release_lock():
    LOCK_FILE.unlink(missing_ok=True)


def _send_followup_nudge(state, session_log_f, chat_id):
    """If any stub is >2h old with rpe=null and hasn't been nudged, send one re-ping."""
    if not session_log_f.exists():
        return
    try:
        log_entries = json.loads(session_log_f.read_text())
    except Exception:
        return

    now = datetime.now()
    nudged_ids = set(state.get("nudged_ids") or [])

    for e in log_entries:
        if not e.get("stub", False):
            continue
        if e.get("rpe") is not None:
            continue
        aid = str(e.get("activity_id", ""))
        if aid in nudged_ids:
            continue
        logged_at = e.get("logged_at")
        if not logged_at:
            continue
        try:
            logged_dt = datetime.fromisoformat(logged_at + "T00:00:00" if "T" not in logged_at else logged_at)
        except Exception:
            continue
        age_hours = (now - logged_dt).total_seconds() / 3600
        if 2 <= age_hours <= 24:
            sport = e.get("sport", "session")
            name = e.get("name", "")
            if sport == "Run":
                msg = f"Quick one — injury score for the {name or 'run'}: during and this morning? (0-10)"
            elif sport == "Swim":
                msg = f"RPE for the {name or 'swim'}? And how did it feel overall?"
            elif sport == "Ride" and (e.get("duration_min") or 0) >= 90:
                msg = f"Fuelling check for the {name or 'ride'} — roughly g carbs/hr?"
            else:
                msg = f"RPE for {name or 'last session'}? (1–10)"
            _notify(msg, chat_id)
            nudged_ids.add(aid)
            state["nudged_ids"] = list(nudged_ids)
            break  # one nudge per run


def check_athlete(slug, athlete_cfg):
    """Run activity check for one athlete."""
    adir = BASE / f"athletes/{slug}"
    state_file = adir / "last_activity_state.json"
    session_log_f = adir / "session-log.json"
    decoupling_log = adir / "decoupling-log.json"
    chat_id = athlete_cfg.get("chat_id", "")

    # Load profile for athlete-specific data
    profile = {}
    if (adir / "profile.json").exists():
        try:
            profile = json.loads((adir / "profile.json").read_text())
        except Exception:
            pass

    ftp = profile.get("ftp_watts") or 250
    first_name = profile.get("name", slug).split()[0]
    injuries = profile.get("injuries", [])

    state = load_state(state_file)

    # Snapshot existing IDs before Claude runs
    existing_ids: set = set()
    if session_log_f.exists():
        try:
            existing_ids = {str(e.get("activity_id", "")) for e in json.loads(session_log_f.read_text())}
        except (json.JSONDecodeError, OSError):
            pass

    prompt = _build_prompt(slug, first_name, ftp, injuries)

    t_start = time.time()
    try:
        result = subprocess.run(
            [CLAUDE, "-p", prompt, "--allowedTools", TOOLS],
            capture_output=True, text=True,
            cwd=PROJECT_DIR, timeout=180,
        )
    except subprocess.TimeoutExpired:
        _notify(
            f"Activity watcher timed out for {first_name} (180s). "
            f"Last known activity: {state.get('last_id', 'unknown')}.",
            chat_id,
        )
        return
    except Exception as exc:
        print(f"[{slug}] Failed to launch Claude: {exc}", file=sys.stderr)
        return

    if result.returncode != 0:
        stderr_snippet = (result.stderr or "").strip()[:200]
        print(f"[{slug}] Claude exited {result.returncode}: {stderr_snippet}", file=sys.stderr)
        return

    output = result.stdout.strip()
    if not output:
        print(f"[{slug}] Claude returned no output", file=sys.stderr)
        return

    activity_id = None
    decoupling_raw = None
    session_chart_raw = None
    analysis_lines = []
    in_analysis = False
    for line in output.split("\n"):
        if line.startswith("ACTIVITY_ID:"):
            activity_id = line.split(":", 1)[1].strip()
            in_analysis = False
        elif line.startswith("DECOUPLING:"):
            decoupling_raw = line.split(":", 1)[1].strip()
            in_analysis = False
        elif line.startswith("SESSION_CHART:"):
            session_chart_raw = line.split(":", 1)[1].strip()
            in_analysis = False
        elif line.startswith("ANALYSIS:"):
            in_analysis = True
            first = line.split(":", 1)[1].strip()
            if first:
                analysis_lines.append(first)
        elif in_analysis and not line.startswith(("ACTIVITY_ID:", "DECOUPLING:", "SESSION_CHART:")):
            analysis_lines.append(line)

    if not activity_id or activity_id == "none":
        _send_followup_nudge(state, session_log_f, chat_id)
        return

    # Dedup check
    if activity_id in existing_ids:
        _send_followup_nudge(state, session_log_f, chat_id)
        return

    if activity_id == state.get("last_id"):
        _send_followup_nudge(state, session_log_f, chat_id)
        return

    state["last_id"] = activity_id
    state["notified_at"] = datetime.now().isoformat()
    save_state(state, state_file)

    # Log decoupling for long rides
    if decoupling_raw and decoupling_raw != "none":
        try:
            parts = decoupling_raw.split("|")
            if len(parts) == 7:
                entry = {
                    "activity_id": parts[0].strip(),
                    "date":         parts[1].strip(),
                    "name":         parts[2].strip(),
                    "duration_min": float(parts[3].strip()),
                    "if":           float(parts[4].strip()),
                    "decoupling_pct": float(parts[5].strip()),
                    "tss":          float(parts[6].strip()),
                }
                entries = json.loads(decoupling_log.read_text()) if decoupling_log.exists() else []
                if not any(e.get("activity_id") == entry["activity_id"] for e in entries):
                    entries.append(entry)
                    decoupling_log.write_text(json.dumps(entries, indent=2))
        except Exception:
            pass

    # Send session structure chart
    if session_chart_raw and session_chart_raw != "none":
        try:
            import sys as _sys
            _sys.path.insert(0, str(BASE / "telegram"))
            import charts as _charts
            chart_data = json.loads(session_chart_raw)
            png = _charts.session_chart(
                chart_data.get("name", "Session"),
                chart_data.get("intervals", []),
                ftp=chart_data.get("ftp", ftp),
            )
            if png:
                import tempfile, os as _os
                with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
                    f.write(png)
                    tmp_path = f.name
                subprocess.run(
                    ["python3", str(NOTIFY), "--chat-id", str(chat_id), "--photo", tmp_path],
                    cwd=PROJECT_DIR,
                )
                _os.unlink(tmp_path)
        except Exception:
            pass

    analysis = "\n".join(analysis_lines).strip()
    if analysis and analysis != "none":
        _notify(f"*New activity*\n\n{analysis}", chat_id)

    _send_followup_nudge(state, session_log_f, chat_id)

    # Trigger site data refresh in background
    subprocess.Popen(
        ["python3", str(BASE / "scripts/refresh-site-data.py")],
        cwd=PROJECT_DIR,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def main():
    if not acquire_lock():
        sys.exit(0)

    try:
        athletes = json.loads(ATHLETES_CONFIG.read_text())
        for slug, athlete_cfg in athletes.items():
            if not athlete_cfg.get("active", True):
                continue
            try:
                check_athlete(slug, athlete_cfg)
            except Exception as exc:
                print(f"[{slug}] Unhandled error: {exc}", file=sys.stderr)
    finally:
        release_lock()


if __name__ == "__main__":
    main()
