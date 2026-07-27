"""
refresh-public-data.py
Reads the gitignored training-data.json for each active athlete and writes
a public-safe subset to ClaudeCoach/site-data.json, then commits and pushes
to GitHub Pages.

Crontab (VM): 0 * * * * python3 /path/to/ClaudeCoach/scripts/refresh-public-data.py
"""
import json
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
    """Commit + push site-data.json through the shared git helper.

    Was a bespoke add/commit/fetch/stash/rebase/push block whose only failure
    signal was "Git push failed" in public-data.log — which is exactly how the
    06:00 and 08:00 push failures on 27 Jul 2026 escaped alerting altogether.
    lib/git_sync.py is the same path activity-watcher.py and daily-prescription.py
    already use, and brings the repo-wide lock, one bounded push retry (so a
    lost race self-heals silently) and a LOUD failure — standing flag file, ops
    digest entry and an immediate Telegram — when a push really is broken."""
    sys.path.insert(0, str(BASE / "ClaudeCoach/lib"))
    from git_sync import sync_commit_push
    return sync_commit_push(
        ["ClaudeCoach/site-data.json"],
        f"data: refresh public site-data {date.today()}",
        script="refresh-public-data",
    )


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
