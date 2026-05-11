#!/usr/bin/env python3
"""
Check for new activities and send a brief analysis to Telegram.
Run every 15 min via cron. Skips if already running.
"""
import json, subprocess, sys, time
from datetime import datetime
from pathlib import Path

BASE            = Path(__file__).parent.parent  # ClaudeCoach/
STATE_FILE      = BASE / "athletes/jamie/last_activity_state.json"
LOCK_FILE       = BASE / ".activity_watcher.lock"
NOTIFY          = BASE / "telegram/notify.py"
PROJECT_DIR     = str(BASE.parent)
CLAUDE          = "/usr/bin/claude"

SESSION_LOG     = BASE / "athletes/jamie/session-log.json"
DECOUPLING_LOG  = BASE / "athletes/jamie/decoupling-log.json"

TOOLS = ",".join([
    "Read", "Write", "Bash",
    "mcp__claude_ai_icusync__get_athlete_profile",
    "mcp__claude_ai_icusync__get_training_history",
    "mcp__claude_ai_icusync__get_activity_detail",
])

PROMPT = """Check for new activities and stub them into the session log.

Step 1 — get_athlete_profile (today's date), then get_training_history (last 2 days).

Step 2 — Read ClaudeCoach/athletes/jamie/session-log.json and note all existing activity_id values.

Step 3 — For the most recent activity that is NOT already in session-log.json:
  - Call get_activity_detail for that activity to get full metrics (all sports).
  - Add a stub entry to session-log.json (prepend to the array, most recent first).

  For Ride or Run:
    {
      "activity_id": "<id>", "date": "<YYYY-MM-DD>", "name": "<name>", "sport": "<sport>",
      "tss": <tss>, "duration_min": <duration>, "distance_km": <distance or null>,
      "avg_power": <avg_power or null>, "norm_power": <norm_power or null>, "avg_hr": <avg_hr or null>,
      "rpe": null, "feel": null,
      "ankle_pain_during": null, "ankle_pain_next_morning": null,
      "nutrition_g_carb": null, "hydration_ml": null, "notes": null,
      "logged_at": "<current datetime as YYYY-MM-DDTHH:MM:SS>", "stub": true
    }

  For Swim:
    {
      "activity_id": "<id>", "date": "<YYYY-MM-DD>", "name": "<name>", "sport": "Swim",
      "tss": <tss>, "duration_min": <duration>, "distance_km": <distance_m / 1000>,
      "pace_per_100m": <avg pace in seconds per 100m — distance_m / (duration_s / 100)>,
      "avg_hr": <avg_hr or null>,
      "rpe": null, "feel": null, "notes": null, "logged_at": "<current datetime as YYYY-MM-DDTHH:MM:SS>", "stub": true
    }
    Also append to ClaudeCoach/athletes/jamie/swim-log.json:
    {"date":"<YYYY-MM-DD>","activity_id":"<id>","name":"<name>","distance_m":<int>,"pace_per_100m":<seconds float>,"duration_min":<int>,"tss":<int or null>}
    git add ClaudeCoach/athletes/jamie/swim-log.json in the commit below.

  For WeightTraining / Strength:
    {
      "activity_id": "<id>", "date": "<YYYY-MM-DD>", "name": "<name>", "sport": "Strength",
      "tss": <tss or null>, "duration_min": <duration>,
      "rpe": null, "feel": null, "notes": null, "logged_at": "<current datetime as YYYY-MM-DDTHH:MM:SS>", "stub": true
    }

  - Write the updated array back to ClaudeCoach/athletes/jamie/session-log.json.
  - Run: git add ClaudeCoach/athletes/jamie/session-log.json ClaudeCoach/athletes/jamie/swim-log.json && git commit -m "stub: <name> <date>" && git push origin main

Step 4 — Respond in EXACTLY this format (no other text):
ACTIVITY_ID: <id or none>
ANALYSIS: <coaching message — see rules below>

Jamie: male, 30, FTP 316 W, run threshold 4:02/km, swim CSS 1:39/100m. Ankle in rehab — 5:30 walk-run only.

Rules for ANALYSIS (2-3 lines, max 400 chars):
- Ride: Line 1 = NP + IF. Line 2 (>90 min) = aerobic decoupling %. Line 3 = "Nutrition — g carbs/hr and bottles?"
- Run: Line 1 = distance + avg pace. Line 2 = HR cap adherence (cap 150 bpm). Line 3 = "Ankle score during and this morning?"
- Swim: Line 1 = distance + pace vs CSS 1:39 target (state +/- seconds). Line 2 = "RPE and how did it feel?"
- Strength: Line 1 = duration. Line 2 = "RPE and what was the main focus?"

For rides > 3 hours: also output a DECOUPLING line (Pa:HR decoupling % from activity detail):
DECOUPLING: <activity_id>|<date>|<name>|<duration_min>|<intensity_factor>|<decoupling_pct>|<tss>
If ride < 3 hours or Pa:HR data unavailable: DECOUPLING: none

For structured sessions with >3 intervals (interval training, bricks, threshold work):
Output a SESSION_CHART line with compact JSON for the session structure chart:
SESSION_CHART: {"name":"<activity name>","ftp":316,"intervals":[{"duration_seconds":600,"average_power":250,"type":"WORK"},...]}
Use interval data from get_activity_detail. type values: WORK, RECOVERY, WARMUP, COOLDOWN.
If session is unstructured (Z2, easy run, swim, etc.): SESSION_CHART: none

If no activities at all: ACTIVITY_ID: none  ANALYSIS: none"""


def _notify_plain(msg: str):
    """Send a plain Telegram message without involving Claude — used for error reporting."""
    try:
        subprocess.run(["python3", str(NOTIFY), msg], cwd=PROJECT_DIR, timeout=15)
    except Exception:
        pass


def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"last_id": None}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state))


def acquire_lock():
    if LOCK_FILE.exists():
        if time.time() - LOCK_FILE.stat().st_mtime < 300:
            return False  # another instance running
    LOCK_FILE.touch()
    return True


def release_lock():
    try:
        LOCK_FILE.unlink()
    except FileNotFoundError:
        pass


def main():
    if not acquire_lock():
        sys.exit(0)

    try:
        state = load_state()

        # Snapshot existing IDs before Claude runs so the dedupe check below
        # uses the pre-run state, not the post-stub state.
        existing_ids: set = set()
        if SESSION_LOG.exists():
            try:
                existing_ids = {str(e.get("activity_id", "")) for e in json.loads(SESSION_LOG.read_text())}
            except (json.JSONDecodeError, OSError):
                pass

        t_start = time.time()
        try:
            result = subprocess.run(
                [CLAUDE, "-p", PROMPT, "--allowedTools", TOOLS],
                capture_output=True, text=True,
                cwd=PROJECT_DIR, timeout=120,
            )
        except subprocess.TimeoutExpired:
            _notify_plain(
                f"Activity watcher timed out after 120s. "
                f"Possible causes: IcuSync slow, Intervals.icu API down, or Claude API issue. "
                f"Last known activity: {state.get('last_id', 'unknown')}. "
                f"Run manually: python3 ClaudeCoach/scripts/activity-watcher.py"
            )
            return
        except Exception as exc:
            _notify_plain(
                f"Activity watcher failed to launch Claude: {exc}. "
                f"Check that /usr/bin/claude exists and is executable on the VM."
            )
            return

        elapsed = int(time.time() - t_start)
        if result.returncode != 0:
            stderr_snippet = (result.stderr or "").strip()[:300]
            _notify_plain(
                f"Activity watcher: Claude exited with code {result.returncode} after {elapsed}s. "
                f"Stderr: {stderr_snippet or '(empty)'}"
            )
            return

        output = result.stdout.strip()
        if not output:
            _notify_plain(
                f"Activity watcher: Claude ran for {elapsed}s but returned no output. "
                f"May be an auth issue or IcuSync connection problem. Last ID: {state.get('last_id', 'unknown')}."
            )
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
            return

        if activity_id == str(state.get("last_id")):
            return  # already processed

        # Belt-and-braces dedupe: check against the pre-run snapshot so we don't
        # suppress notifications for stubs Claude just wrote in this run.
        if activity_id in existing_ids:
            state["last_id"] = activity_id
            save_state(state)
            return

        state["last_id"] = activity_id
        state["notified_at"] = datetime.now().isoformat()
        save_state(state)

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
                    entries = json.loads(DECOUPLING_LOG.read_text()) if DECOUPLING_LOG.exists() else []
                    if not any(e.get("activity_id") == entry["activity_id"] for e in entries):
                        entries.append(entry)
                        DECOUPLING_LOG.write_text(json.dumps(entries, indent=2))
            except Exception as exc:
                pass  # non-fatal

        # Send session structure chart first (before text, so it appears above)
        if session_chart_raw and session_chart_raw != "none":
            try:
                import sys as _sys
                _sys.path.insert(0, str(BASE / "telegram"))
                import charts as _charts
                chart_data = json.loads(session_chart_raw)
                png = _charts.session_chart(
                    chart_data.get("name", "Session"),
                    chart_data.get("intervals", []),
                    ftp=chart_data.get("ftp", 316),
                )
                if png:
                    import tempfile, os as _os
                    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
                        f.write(png)
                        tmp_path = f.name
                    subprocess.run(
                        ["python3", str(NOTIFY), "--photo", tmp_path],
                        cwd=PROJECT_DIR,
                    )
                    _os.unlink(tmp_path)
            except Exception as exc:
                pass  # non-fatal — text message will still go out

        analysis = "\n".join(analysis_lines).strip()
        if analysis and analysis != "none":
            subprocess.run(
                ["python3", str(NOTIFY), f"*New activity*\n\n{analysis}"],
                cwd=PROJECT_DIR,
            )

        # Follow-up nudge check — any stub with rpe=null, 2–24 hr old, not yet nudged
        _send_followup_nudge(state)

        # Trigger site data refresh in background — non-blocking
        subprocess.Popen(
            ["python3", str(BASE / "scripts/refresh-site-data.py")],
            cwd=PROJECT_DIR,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    finally:
        release_lock()


def _send_followup_nudge(state):
    """If any stub is >2h old with rpe=null and hasn't been nudged, send one re-ping."""
    if not SESSION_LOG.exists():
        return
    try:
        log_entries = json.loads(SESSION_LOG.read_text())
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
                msg = f"Quick one — ankle score for the {name or 'run'}: during and this morning?"
            elif sport == "Swim":
                msg = f"RPE for the {name or 'swim'}? And how did it feel overall?"
            elif sport == "Ride" and (e.get("duration_min") or 0) >= 90:
                msg = f"Fuelling check for the {name or 'ride'} — roughly g carbs/hr?"
            else:
                msg = f"RPE for {name or 'last session'}? (1–10)"
            subprocess.run(["python3", str(NOTIFY), msg], cwd=PROJECT_DIR)
            nudged_ids.add(aid)
            state["nudged_ids"] = list(nudged_ids)
            save_state(state)
            break  # one nudge per run


if __name__ == "__main__":
    main()
