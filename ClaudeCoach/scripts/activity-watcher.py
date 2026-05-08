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

TOOLS = ",".join([
    "mcp__claude_ai_icusync__get_athlete_profile",
    "mcp__claude_ai_icusync__get_training_history",
    "mcp__claude_ai_icusync__get_activity_detail",
])

PROMPT = """Get the most recent activity from get_training_history (check today and yesterday).
Then call get_activity_detail to get full metrics for that activity.

Jamie: male, 30, FTP 316 W, run threshold 4:02/km, swim CSS 1:39/100m. Ankle in rehab — 9:1 walk-run only.

Respond in EXACTLY this format — no other text, no markdown:
ACTIVITY_ID: <numeric id>
ANALYSIS: <coaching message — see rules below>

Rules for the ANALYSIS (3-5 lines max):
- Line 1: One punchy sentence on the headline result. Lead with what went well or the key number. Examples: "Solid Z2 ride — NP 214 W (IF 0.68), right on target." / "Good 9:1 run, 12.5 km — ankle behaved."
- Line 2 (rides only): Cardiac decoupling if available (HR drift vs power drift as a %). If not available, comment on IF discipline or NP vs target.
- Line 2 (runs): Pace vs threshold context, or HR cap adherence.
- Line 2 (swims): Pace vs CSS target.
- Line 3: One question — nutrition and hydration for sessions >90 min, or ankle pain score for runs, or how it felt (RPE) for shorter sessions.
- Keep it conversational, not a data dump. Max 400 characters total.

If there are no activities at all, respond:
ACTIVITY_ID: none
ANALYSIS: none"""


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
