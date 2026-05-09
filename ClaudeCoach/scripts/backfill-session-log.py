#!/usr/bin/env python3
"""
One-off backfill: seed session-log.json with the last 8 weeks of activities
from Intervals.icu that aren't already in the log.

Entries are marked stub:false (historical — no RPE follow-up expected).
Run once on the VM, then discard or keep for re-runs (safe to run again — dedupes by activity_id).
"""
import json, subprocess, sys
from pathlib import Path
from datetime import datetime, date, timedelta

BASE        = Path(__file__).parent.parent
SESSION_LOG = BASE / "session-log.json"
PROJECT_DIR = str(BASE.parent)
CLAUDE      = "/usr/bin/claude"

WEEKS_BACK = 8

existing_ids = set()
if SESSION_LOG.exists():
    try:
        for e in json.loads(SESSION_LOG.read_text()):
            existing_ids.add(str(e.get("activity_id", "")))
    except Exception:
        pass

start_date = (date.today() - timedelta(weeks=WEEKS_BACK)).isoformat()
today      = date.today().isoformat()

TOOLS = ",".join([
    "Read", "Write",
    "mcp__claude_ai_icusync__get_athlete_profile",
    "mcp__claude_ai_icusync__get_training_history",
])

PROMPT = f"""Backfill ClaudeCoach/session-log.json with historical activities.

Step 1 — get_athlete_profile (for today's date).

Step 2 — get_training_history(start_date="{start_date}", end_date="{today}").

Step 3 — Read ClaudeCoach/session-log.json. Note existing activity_id values:
{json.dumps(sorted(existing_ids))}

Step 4 — For every activity in training_history that is NOT in the existing list above:
  Build a historical entry. Use this schema (sport-dependent):

  For Ride / GravelRide / VirtualRide:
    {{
      "activity_id": "<id>", "date": "<YYYY-MM-DD>", "name": "<name>", "sport": "Ride",
      "tss": <tss or null>, "duration_min": <duration>, "distance_km": <distance or null>,
      "avg_power": <avg_power or null>, "norm_power": <norm_power or null>, "avg_hr": <avg_hr or null>,
      "rpe": null, "feel": null,
      "ankle_pain_during": null, "ankle_pain_next_morning": null,
      "nutrition_g_carb": null, "hydration_ml": null, "notes": null,
      "logged_at": "<date>T00:00:00", "stub": false
    }}

  For Run:
    {{
      "activity_id": "<id>", "date": "<YYYY-MM-DD>", "name": "<name>", "sport": "Run",
      "tss": <tss or null>, "duration_min": <duration>, "distance_km": <distance or null>,
      "avg_hr": <avg_hr or null>,
      "rpe": null, "feel": null,
      "ankle_pain_during": null, "ankle_pain_next_morning": null,
      "notes": null, "logged_at": "<date>T00:00:00", "stub": false
    }}

  For Swim:
    {{
      "activity_id": "<id>", "date": "<YYYY-MM-DD>", "name": "<name>", "sport": "Swim",
      "tss": <tss or null>, "duration_min": <duration>, "distance_km": <distance or null>,
      "avg_hr": <avg_hr or null>,
      "rpe": null, "feel": null, "notes": null,
      "logged_at": "<date>T00:00:00", "stub": false
    }}

  For WeightTraining / Strength / Other:
    {{
      "activity_id": "<id>", "date": "<YYYY-MM-DD>", "name": "<name>", "sport": "Strength",
      "tss": <tss or null>, "duration_min": <duration>,
      "rpe": null, "feel": null, "notes": null,
      "logged_at": "<date>T00:00:00", "stub": false
    }}

Step 5 — Merge new entries with the existing session-log.json array.
  - Prepend new entries (most recent first by date).
  - Do NOT duplicate any activity already in the existing list.
  - Write the full merged array back to ClaudeCoach/session-log.json.

Step 6 — Run:
  git add ClaudeCoach/session-log.json && git commit -m "backfill: session log {start_date} to {today}" && git push origin main

Step 7 — Output one line: "Done: added <N> entries, total <M>"
"""


def main():
    print(f"{datetime.now().strftime('%H:%M:%S')} Backfilling session log from {start_date}…")
    result = subprocess.run(
        [CLAUDE, "-p", PROMPT, "--allowedTools", TOOLS],
        capture_output=True, text=True,
        cwd=PROJECT_DIR, timeout=240,
    )
    if result.returncode != 0:
        print(f"Error: {result.stderr[:300]}")
        sys.exit(1)
    print(result.stdout.strip())


if __name__ == "__main__":
    main()
