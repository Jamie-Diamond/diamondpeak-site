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
    # Non-Jamie athletes write to ClaudeCoach/training-data-{slug}.json;
    # Jamie writes to ClaudeCoach/athletes/jamie/training-data.json (private).
    candidates = [
        BASE / f"ClaudeCoach/training-data-{slug}.json",
        BASE / "ClaudeCoach/athletes" / slug / "training-data.json",
    ]
    for path in candidates:
        if path.exists():
            try:
                return json.loads(path.read_text())
            except json.JSONDecodeError as e:
                log(f"[{slug}] {path.name} parse error: {e}")
                return None
    log(f"[{slug}] training-data.json not found — skipping")
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
    def run(cmd):
        r = subprocess.run(cmd, cwd=str(BASE), capture_output=True, text=True)
        return r.returncode, r.stdout + r.stderr

    for cmd in [
        ["git", "add", "ClaudeCoach/site-data.json"],
        ["git", "commit", "-m", f"data: refresh public site-data {date.today()}"],
        # Fetch using origin/main ref directly — avoids FETCH_HEAD race condition
        # with cc-gitpull.sh (both start at :00) which runs git fetch origin
        # (all branches) and overwrites FETCH_HEAD before rebase can read it.
        ["git", "fetch", "origin", "main"],
    ]:
        rc, out = run(cmd)
        if rc != 0 and "nothing to commit" not in out:
            log(f"git error ({' '.join(cmd[:2])}): {out.strip()}")
            return False

    # Stash any unstaged script edits so rebase can proceed
    _, stash_out = run(["git", "stash"])
    did_stash = "No local changes to save" not in stash_out

    rc, out = run(["git", "rebase", "origin/main"])
    if did_stash:
        run(["git", "stash", "pop"])
    if rc != 0:
        log(f"git error (git rebase): {out.strip()}")
        return False

    rc, out = run(["git", "push", "origin", "main"])
    if rc != 0:
        log(f"git error (git push): {out.strip()}")
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
