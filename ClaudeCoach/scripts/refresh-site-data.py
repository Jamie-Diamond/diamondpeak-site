#!/usr/bin/env python3
"""
Pull live data from Intervals.icu and update training-data.json, then push to GitHub.
Run daily (e.g. 06:00 via launchd/cron). Requires git push credentials (SSH key or keychain).
"""
import json, subprocess, sys, time
from pathlib import Path
from datetime import datetime, timedelta

BASE        = Path(__file__).parent.parent          # ClaudeCoach/
OUT_FILE    = BASE / "training-data.json"
PROJECT_DIR = str(BASE.parent)                       # diamondpeak-site/
LOCK_FILE   = BASE / ".refresh_site_data.lock"
CLAUDE      = "/usr/bin/claude"

TOOLS = ",".join([
    "Write",
    "mcp__claude_ai_icusync__get_athlete_profile",
    "mcp__claude_ai_icusync__get_fitness",
    "mcp__claude_ai_icusync__get_training_history",
    "mcp__claude_ai_icusync__get_power_curves",
    "mcp__claude_ai_icusync__get_wellness",
])

PROMPT = """Fetch live training data from Intervals.icu and write ClaudeCoach/training-data.json.

Steps:
1. get_athlete_profile → note current_date_local (today) and FTP
2. get_fitness(start_date="2026-01-01", end_date=today) → daily CTL/ATL/TSB series
3. get_training_history(start_date=<14 days ago>, end_date=today) → recent activities
4. get_power_curves → best power efforts for standard durations
5. get_wellness → latest entry for HRV and RHR (use most recent available)

Then use the Write tool to write ClaudeCoach/training-data.json with EXACTLY this schema
(no trailing text after the Write call):

{
  "generated": "<today YYYY-MM-DD>",
  "kpi": {
    "ctl": <today CTL, 1dp float>,
    "atl": <today ATL, 1dp float>,
    "tsb": <today TSB, 1dp float — negative means fatigued>,
    "ramp7d": <CTL today minus CTL 7 days ago, 1dp float>,
    "hrv": <latest HRV integer or null>,
    "rhr": <latest RHR integer or null>
  },
  "fitnessThis": [
    ["YYYY-MM-DD", <ctl float>],
    ... one entry per day from 2026-01-01 to today inclusive
  ],
  "recent": [
    {
      "date": "YYYY-MM-DD",
      "sport": "<Ride|Run|Swim|Strength|GravelRide|VirtualRide|Other>",
      "name": "<activity name>",
      "dur": <duration in whole minutes>,
      "dist": <distance in km, 2dp float, or null>,
      "pace": "<formatted string: '31.7 kph' for rides, '5:02/km' for runs, '1:39/100m' for swims>",
      "hr": <average HR integer or null>,
      "powAvg": <average power watts integer or null — cycling only>,
      "powNp": <normalised power watts integer or null — cycling only>,
      "tss": <TSS integer>
    },
    ... all activities from the last 14 days, most recent first
  ],
  "powerCurve": [
    {"t": <seconds>, "label": "<e.g. 5s>", "w": <best watts integer>, "wPrev": <last year same window or null>},
    ... include durations: 5s(5), 10s(10), 30s(30), 1m(60), 2m(120), 5m(300), 10m(600), 20m(1200), 30m(1800), 60m(3600), 90m(5400), 2h(7200)
  ]
}

After writing the file, output one line: "Done: CTL <value>, <N> activities"
"""


def log(msg):
    print(f"{datetime.now().strftime('%H:%M:%S')} {msg}")


def acquire_lock():
    if LOCK_FILE.exists() and time.time() - LOCK_FILE.stat().st_mtime < 600:
        return False
    LOCK_FILE.touch()
    return True


def release_lock():
    try:
        LOCK_FILE.unlink()
    except FileNotFoundError:
        pass


def main():
    if not acquire_lock():
        log("Already running — skipping")
        sys.exit(0)

    try:
        log("Fetching live data via Claude + IcuSync...")
        result = subprocess.run(
            [CLAUDE, "-p", PROMPT, "--allowedTools", TOOLS],
            capture_output=True, text=True,
            cwd=PROJECT_DIR, timeout=300,
        )

        if result.returncode != 0:
            log(f"Claude error: {result.stderr[:200]}")
            sys.exit(1)

        log(f"Claude: {result.stdout.strip()[:120]}")

        if not OUT_FILE.exists():
            log("training-data.json was not written — aborting push")
            sys.exit(1)

        # Validate JSON before committing
        try:
            data = json.loads(OUT_FILE.read_text())
            assert "kpi" in data and "fitnessThis" in data and "recent" in data
            log(f"JSON valid: CTL {data['kpi']['ctl']}, {len(data['recent'])} activities")
        except Exception as e:
            log(f"JSON validation failed: {e} — aborting push")
            sys.exit(1)

        # Commit and push
        today = datetime.now().strftime("%Y-%m-%d")
        for cmd in [
            ["git", "add", "ClaudeCoach/training-data.json"],
            ["git", "commit", "-m", f"data: refresh training data {today}"],
            ["git", "pull", "--rebase", "origin", "main"],
            ["git", "push", "origin", "main"],
        ]:
            r = subprocess.run(cmd, cwd=PROJECT_DIR, capture_output=True, text=True)
            if r.returncode != 0 and "nothing to commit" not in r.stdout + r.stderr:
                log(f"git error ({' '.join(cmd[:2])}): {r.stderr[:120]}")
                break
            log(f"git {cmd[1]}: ok")

        log("Done.")

    finally:
        release_lock()


if __name__ == "__main__":
    main()
