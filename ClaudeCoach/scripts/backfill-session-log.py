#!/usr/bin/env python3
"""
One-off backfill: seed session-log.json with the last 8 weeks of activities
from Intervals.icu that aren't already in the log.

Claude only fetches + outputs raw JSON to stdout — Python does all file work.
Safe to re-run: dedupes by activity_id.
"""
import json, subprocess, sys, re
from pathlib import Path
from datetime import datetime, date, timedelta

BASE        = Path(__file__).parent.parent
SESSION_LOG = BASE / "session-log.json"
PROJECT_DIR = str(BASE.parent)
CLAUDE      = "/usr/bin/claude"

WEEKS_BACK   = 8
CHUNK_WEEKS  = 2   # fetch in 2-week chunks to stay well within timeout

SPORT_MAP = {
    "ride": "Ride", "gravelride": "Ride", "virtualride": "Ride",
    "run": "Run", "walk": "Run",
    "swim": "Swim",
    "weighttraining": "Strength", "strength": "Strength", "yoga": "Strength",
}

TOOLS = ",".join([
    "mcp__claude_ai_icusync__get_athlete_profile",
    "mcp__claude_ai_icusync__get_training_history",
])

FETCH_PROMPT = """Fetch training history from Intervals.icu for a date range.

1. get_athlete_profile (to confirm today's date).
2. get_training_history(start_date="{start}", end_date="{end}").

Output ONLY a JSON array — no other text before or after. Each element:
{{"activity_id":"<id>","date":"YYYY-MM-DD","name":"<name>","sport":"<sport type exactly as returned>","tss":<int or null>,"duration_min":<int>,"distance_km":<float or null>,"avg_power":<int or null>,"norm_power":<int or null>,"avg_hr":<int or null>}}

If no activities: output []
"""


def log(msg):
    print(f"{datetime.now().strftime('%H:%M:%S')} {msg}")


def fetch_chunk(start: str, end: str) -> list:
    prompt = FETCH_PROMPT.format(start=start, end=end)
    result = subprocess.run(
        [CLAUDE, "-p", prompt, "--allowedTools", TOOLS],
        capture_output=True, text=True,
        cwd=PROJECT_DIR, timeout=90,
    )
    if result.returncode != 0:
        log(f"  Warning: fetch failed for {start}→{end}: {result.stderr[:120]}")
        return []
    # Extract JSON array from stdout (Claude may add surrounding text)
    text = result.stdout.strip()
    m = re.search(r'\[.*\]', text, re.DOTALL)
    if not m:
        log(f"  Warning: no JSON array found in output for {start}→{end}")
        return []
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError as e:
        log(f"  Warning: JSON parse error for {start}→{end}: {e}")
        return []


def normalise_sport(raw: str) -> str:
    return SPORT_MAP.get((raw or "").lower(), "Other")


def make_entry(act: dict) -> dict:
    sport = normalise_sport(act.get("sport", ""))
    base = {
        "activity_id": str(act.get("activity_id", "")),
        "date":        act.get("date", ""),
        "name":        act.get("name", ""),
        "sport":       sport,
        "tss":         act.get("tss"),
        "duration_min": act.get("duration_min"),
        "logged_at":   act.get("date", "") + "T00:00:00",
        "stub":        False,
    }
    if sport in ("Ride",):
        base.update({
            "distance_km": act.get("distance_km"),
            "avg_power":   act.get("avg_power"),
            "norm_power":  act.get("norm_power"),
            "avg_hr":      act.get("avg_hr"),
            "rpe": None, "feel": None,
            "ankle_pain_during": None, "ankle_pain_next_morning": None,
            "nutrition_g_carb": None, "hydration_ml": None, "notes": None,
        })
    elif sport == "Run":
        base.update({
            "distance_km": act.get("distance_km"),
            "avg_hr":      act.get("avg_hr"),
            "rpe": None, "feel": None,
            "ankle_pain_during": None, "ankle_pain_next_morning": None,
            "notes": None,
        })
    elif sport == "Swim":
        base.update({
            "distance_km": act.get("distance_km"),
            "avg_hr":      act.get("avg_hr"),
            "rpe": None, "feel": None, "notes": None,
        })
    else:
        base.update({"rpe": None, "feel": None, "notes": None})
    return base


def main():
    today = date.today()
    start_date = today - timedelta(weeks=WEEKS_BACK)

    # Load existing log
    existing = []
    if SESSION_LOG.exists():
        try:
            existing = json.loads(SESSION_LOG.read_text())
        except Exception:
            pass
    existing_ids = {str(e.get("activity_id", "")) for e in existing}
    log(f"Existing entries: {len(existing)} (IDs: {len(existing_ids)})")

    # Fetch in 2-week chunks
    all_raw = []
    chunk_start = start_date
    while chunk_start < today:
        chunk_end = min(chunk_start + timedelta(weeks=CHUNK_WEEKS), today)
        log(f"Fetching {chunk_start.isoformat()} → {chunk_end.isoformat()}…")
        chunk = fetch_chunk(chunk_start.isoformat(), chunk_end.isoformat())
        log(f"  Got {len(chunk)} activities")
        all_raw.extend(chunk)
        chunk_start = chunk_end + timedelta(days=1)

    # Dedupe fetched activities (same ID may appear in overlapping chunks)
    seen = set()
    new_entries = []
    for act in sorted(all_raw, key=lambda a: a.get("date", ""), reverse=True):
        aid = str(act.get("activity_id", ""))
        if not aid or aid in existing_ids or aid in seen:
            continue
        seen.add(aid)
        new_entries.append(make_entry(act))

    log(f"New entries to add: {len(new_entries)}")
    if not new_entries:
        log("Nothing to add — already up to date.")
        return

    merged = new_entries + existing
    SESSION_LOG.write_text(json.dumps(merged, indent=2))
    log(f"Written session-log.json: {len(merged)} total entries")

    # Commit and push
    for cmd in [
        ["git", "add", "ClaudeCoach/session-log.json"],
        ["git", "commit", "-m", f"backfill: session log {start_date.isoformat()} to {today.isoformat()}"],
        ["git", "fetch", "origin"],
        ["git", "rebase", "--autostash", "origin/main"],
        ["git", "push", "origin", "main"],
    ]:
        r = subprocess.run(cmd, cwd=PROJECT_DIR, capture_output=True, text=True)
        if r.returncode != 0 and "nothing to commit" not in r.stdout + r.stderr:
            log(f"git error: {r.stderr[:100]}")
            break
        log(f"git {cmd[1]}: ok")

    log(f"Done: added {len(new_entries)} entries, total {len(merged)}")


if __name__ == "__main__":
    main()
