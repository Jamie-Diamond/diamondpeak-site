#!/usr/bin/env python3
"""
Check for new activities and send a brief analysis to Telegram.
Run every 15 min via cron. Skips if already running.
"""
import json, subprocess, sys, time
from pathlib import Path

BASE        = Path(__file__).parent.parent  # ClaudeCoach/
STATE_FILE  = BASE / "last_activity_state.json"
LOCK_FILE   = BASE / ".activity_watcher.lock"
NOTIFY      = BASE / "telegram/notify.py"
PROJECT_DIR = str(BASE.parent)
CLAUDE      = "/usr/bin/claude"

SESSION_LOG = BASE / "session-log.json"

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
- Line 2 (ride >90 min): cardiac decoupling if available; else IF discipline vs target.
  Line 2 (run): pace vs threshold context or HR cap adherence.
  Line 2 (swim): pace vs CSS 1:39/100m target.
- Line 3: one question. Ride >90 min → "How was nutrition — g carbs/hr and bottles?"
  Run → "Ankle score during and this morning?"  Short session → "RPE and how did it feel?"

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
        analysis_lines = []
        in_analysis = False
        for line in output.split("\n"):
            if line.startswith("ACTIVITY_ID:"):
                activity_id = line.split(":", 1)[1].strip()
            elif line.startswith("ANALYSIS:"):
                in_analysis = True
                first = line.split(":", 1)[1].strip()
                if first:
                    analysis_lines.append(first)
            elif in_analysis:
                analysis_lines.append(line)

        if not activity_id or activity_id == "none":
            return

        if activity_id == str(state.get("last_id")):
            return  # already processed

        save_state({"last_id": activity_id})

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
