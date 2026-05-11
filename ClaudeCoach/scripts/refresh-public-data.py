"""
refresh-public-data.py
Reads the gitignored training-data.json and writes a public-safe subset
to ClaudeCoach/site-data.json, then commits and pushes to GitHub Pages.

Crontab (VM): 0 * * * * python3 /path/to/ClaudeCoach/scripts/refresh-public-data.py
"""
import json
import subprocess
import sys
from datetime import date
from pathlib import Path

BASE         = Path(__file__).parent.parent.parent
TRAINING     = BASE / "ClaudeCoach/athletes/jamie/training-data.json"
PUBLIC       = BASE / "ClaudeCoach/site-data.json"

ATHLETE_META = {
    "first_name": "Jamie",
    "race_name":  "Ironman Cervia",
    "race_date":  "2026-09-19",
}

# Rolling window for CTL history on the public chart
CTL_HISTORY_DAYS = 120


def log(msg):
    print(msg, flush=True)


def load_training():
    if not TRAINING.exists():
        log(f"training-data.json not found at {TRAINING} — skipping")
        return None
    try:
        return json.loads(TRAINING.read_text())
    except json.JSONDecodeError as e:
        log(f"training-data.json parse error: {e}")
        return None


def build_public(td):
    kpi = td.get("kpi", {})
    fitness_this = td.get("fitnessThis", [])

    # Keep last CTL_HISTORY_DAYS entries only
    history = fitness_this[-CTL_HISTORY_DAYS:] if len(fitness_this) > CTL_HISTORY_DAYS else fitness_this

    return {
        "updated": str(date.today()),
        "athletes": {
            "jamie": {
                **ATHLETE_META,
                "ctl":         kpi.get("ctl"),
                "atl":         kpi.get("atl"),
                "tsb":         kpi.get("tsb"),
                "ctl_history": [[row[0], round(row[1], 1)] for row in history if len(row) >= 2],
            }
        }
    }


def git_push():
    cmds = [
        ["git", "add", "ClaudeCoach/site-data.json"],
        ["git", "commit", "-m", f"data: refresh public site-data {date.today()}"],
        ["git", "pull", "--rebase", "origin", "main"],
        ["git", "push", "origin", "main"],
    ]
    for cmd in cmds:
        r = subprocess.run(cmd, cwd=str(BASE), capture_output=True, text=True)
        if r.returncode != 0 and "nothing to commit" not in r.stdout + r.stderr:
            log(f"git error ({' '.join(cmd[:2])}): {r.stderr.strip()}")
            return False
    return True


def main():
    td = load_training()
    if td is None:
        sys.exit(1)

    pub = build_public(td)
    PUBLIC.write_text(json.dumps(pub, separators=(",", ":")))
    log(f"Wrote {PUBLIC} — CTL {pub['athletes']['jamie']['ctl']}, {len(pub['athletes']['jamie']['ctl_history'])} history points")

    if git_push():
        log("Pushed site-data.json to GitHub Pages")
    else:
        log("Git push failed — site-data.json updated locally only")


if __name__ == "__main__":
    main()
