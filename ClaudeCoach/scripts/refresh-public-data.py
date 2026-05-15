"""
refresh-public-data.py
Reads the gitignored training-data.json for each active athlete and writes
a public-safe subset to ClaudeCoach/site-data.json, then commits and pushes
to GitHub Pages.

Crontab (VM): 0 * * * * python3 /path/to/ClaudeCoach/scripts/refresh-public-data.py
"""
import json
import subprocess
import sys
from datetime import date
from pathlib import Path

BASE    = Path(__file__).parent.parent.parent  # diamondpeak-site/
PUBLIC  = BASE / "ClaudeCoach/site-data.json"
CONFIG  = BASE / "ClaudeCoach/config/athletes.json"

# Rolling window for CTL history on the public chart
CTL_HISTORY_DAYS = 120


def log(msg):
    print(msg, flush=True)


def load_athletes() -> dict:
    if not CONFIG.exists():
        log(f"athletes.json not found at {CONFIG}")
        return {}
    return json.loads(CONFIG.read_text())


def load_training(slug: str) -> dict | None:
    path = BASE / "ClaudeCoach/athletes" / slug / "training-data.json"
    if not path.exists():
        log(f"[{slug}] training-data.json not found at {path} — skipping")
        return None
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError as e:
        log(f"[{slug}] training-data.json parse error: {e}")
        return None


def build_athlete_entry(slug: str, cfg: dict, td: dict) -> dict:
    """Build the public-safe dict for one athlete."""
    kpi     = td.get("kpi", {})
    history = td.get("fitnessThis", [])

    # Keep last CTL_HISTORY_DAYS entries only
    history = history[-CTL_HISTORY_DAYS:] if len(history) > CTL_HISTORY_DAYS else history

    # First name from athletes.json name field
    name_parts = cfg.get("name", slug).split()
    first_name = name_parts[0] if name_parts else slug

    return {
        "first_name": first_name,
        "race_name":  cfg.get("race_name", ""),
        "race_date":  cfg.get("race_date", ""),
        "ctl":         kpi.get("ctl"),
        "atl":         kpi.get("atl"),
        "tsb":         kpi.get("tsb"),
        "ctl_history": [[row[0], round(row[1], 1)] for row in history if len(row) >= 2],
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
    athletes   = load_athletes()
    athletes_out = {}

    for slug, cfg in athletes.items():
        if not cfg.get("active"):
            continue
        td = load_training(slug)
        if td is None:
            continue
        entry = build_athlete_entry(slug, cfg, td)
        athletes_out[slug] = entry
        log(f"[{slug}] CTL {entry['ctl']}, {len(entry['ctl_history'])} history points")

    if not athletes_out:
        log("No athlete data found — skipping write")
        sys.exit(1)

    pub = {
        "updated":  str(date.today()),
        "athletes": athletes_out,
    }
    PUBLIC.write_text(json.dumps(pub, separators=(",", ":")))
    log(f"Wrote {PUBLIC} with {len(athletes_out)} athlete(s): {list(athletes_out)}")

    if git_push():
        log("Pushed site-data.json to GitHub Pages")
    else:
        log("Git push failed — site-data.json updated locally only")


if __name__ == "__main__":
    main()
