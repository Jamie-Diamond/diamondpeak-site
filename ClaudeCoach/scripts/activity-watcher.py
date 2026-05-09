#!/usr/bin/env python3
"""
Check for new activities and send a brief analysis to Telegram.
Run every 15 min via cron. Skips if already running.
"""
import json, subprocess, sys, time
from datetime import datetime
from pathlib import Path

BASE            = Path(__file__).parent.parent  # ClaudeCoach/
STATE_FILE      = BASE / "last_activity_state.json"
LOCK_FILE       = BASE / ".activity_watcher.lock"
NOTIFY          = BASE / "telegram/notify.py"
PROJECT_DIR     = str(BASE.parent)
CLAUDE          = "/usr/bin/claude"

SESSION_LOG     = BASE / "session-log.json"
DECOUPLING_LOG  = BASE / "decoupling-log.json"

TOOLS = ",".join([
    "Read", "Write", "Bash",
    "mcp__claude_ai_icusync__get_athlete_profile",
    "mcp__claude_ai_icusync__get_training_history",
    "mcp__claude_ai_icusync__get_activity_detail",
])

PROMPT = """Check for new activities and stub them into the session log.

Step 1 — get_athlete_profile (today's date), then get_training_history (last 2 days).

Step 2 — Read ClaudeCoach/session-log.json and note all existing activity_id values.

Step 3 — For the most recent activity that is NOT already in session-log.json:
  - If sport is Swim or Strength: skip to Step 4 (no stub needed).
  - Otherwise call get_activity_detail for that activity to get full metrics.
  - Add a stub entry to session-log.json (prepend to the array, most recent first):
    {
      "activity_id": "<id>",
      "date": "<YYYY-MM-DD>",
      "name": "<name>",
      "sport": "<sport>",
      "tss": <tss>,
      "duration_min": <duration>,
      "distance_km": <distance or null>,
      "avg_power": <avg_power or null>,
      "norm_power": <norm_power or null>,
      "avg_hr": <avg_hr or null>,
      "rpe": null,
      "feel": null,
      "ankle_pain_during": null,
      "ankle_pain_next_morning": null,
      "nutrition_g_carb": null,
      "hydration_ml": null,
      "notes": null,
      "logged_at": "<today>",
      "stub": true
    }
  - Write the updated array back to ClaudeCoach/session-log.json.
  - Run: git add ClaudeCoach/session-log.json && git commit -m "stub: <name> <date>" && git push origin main

Step 4 — Respond in EXACTLY this format (no other text):
ACTIVITY_ID: <id or none>
ANALYSIS: <coaching message — see rules below>

Jamie: male, 30, FTP 316 W, run threshold 4:02/km, swim CSS 1:39/100m. Ankle in rehab — 9:1 walk-run only.

Rules for ANALYSIS (3-5 lines, max 400 chars):
- Line 1: One punchy headline. Lead with what went well or the key number.
  Ride: "Solid Z2 — NP 211 W (IF 0.67), right on plan."
  Run: "Good 9:1, 12.5 km — ankle at 2/10, manageable."
- Line 2 (ride >90 min): aerobic decoupling (Pa:HR ratio — state the %) and IF discipline.
  Line 2 (run): pace vs threshold context or HR cap adherence.
  Line 2 (swim): pace vs CSS 1:39/100m target.
- Line 3: one question. Ride >90 min → "How was nutrition — g carbs/hr and bottles?"
  Run → "Ankle score during and this morning?"  Short session → "RPE and how did it feel?"

For rides > 3 hours: also output a DECOUPLING line (Pa:HR decoupling % from activity detail):
DECOUPLING: <activity_id>|<date>|<name>|<duration_min>|<intensity_factor>|<decoupling_pct>|<tss>
If ride < 3 hours or Pa:HR data unavailable: DECOUPLING: none

For structured sessions with >3 intervals (interval training, bricks, threshold work):
Output a SESSION_CHART line with compact JSON for the session structure chart:
SESSION_CHART: {"name":"<activity name>","ftp":316,"intervals":[{"duration_seconds":600,"average_power":250,"type":"WORK"},...]}
Use interval data from get_activity_detail. type values: WORK, RECOVERY, WARMUP, COOLDOWN.
If session is unstructured (Z2, easy run, swim, etc.): SESSION_CHART: none

If no activities at all: ACTIVITY_ID: none  ANALYSIS: none"""


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

        result = subprocess.run(
            [CLAUDE, "-p", PROMPT, "--allowedTools", TOOLS],
            capture_output=True, text=True,
            cwd=PROJECT_DIR, timeout=120,
        )

        output = result.stdout.strip()
        if not output:
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

        # Belt-and-braces dedupe: ignore the LLM's claim and re-check session-log.json
        # directly. If the activity_id is already present, pin state to it and bail
        # without notifying — covers cases where the previous run committed the stub
        # but crashed/timed out before save_state().
        if SESSION_LOG.exists():
            try:
                log = json.loads(SESSION_LOG.read_text())
                if any(str(e.get("activity_id")) == activity_id for e in log):
                    save_state({"last_id": activity_id})
                    return
            except (json.JSONDecodeError, OSError):
                pass

        save_state({"last_id": activity_id, "notified_at": datetime.now().isoformat()})

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

    finally:
        release_lock()


if __name__ == "__main__":
    main()
