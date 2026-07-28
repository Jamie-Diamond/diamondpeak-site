"""
refresh-public-data.py
Reads the gitignored training-data.json for each active athlete, writes the
private full roll-up to ClaudeCoach/site-data.json (gitignored), and writes an
ALLOW-LIST SANITISED copy to ClaudeCoach/public/site-data.json which is the only
one committed and pushed to GitHub Pages.

build_athlete_entry() below was already an allow-list in shape, but nothing
enforced that: it was a hand-written dict literal with no guard, so one added
line would have published a new field silently. The payload now goes through
lib/public_sanitise.SITE_DATA_SPEC, which drops anything not named there and
refuses the write outright if a forbidden key is present.

Crontab (VM): 0 * * * * python3 /path/to/ClaudeCoach/scripts/refresh-public-data.py
"""
import json
import subprocess
import sys
from datetime import date
from pathlib import Path

# NOTE the BASE here is diamondpeak-site/, one level up from the BASE in
# refresh-site-data.py (which is ClaudeCoach/). The sanitiser never computes
# output paths for that reason - callers pass absolute paths in.
BASE    = Path(__file__).parent.parent.parent  # diamondpeak-site/
PRIVATE = BASE / "ClaudeCoach/site-data.json"          # gitignored, full roll-up
PUBLIC  = BASE / "ClaudeCoach/public/site-data.json"   # tracked, sanitised, published
CONFIG  = BASE / "ClaudeCoach/config/athletes.json"
GIT_PUSH = BASE / "ClaudeCoach/scripts/cc-git-commit-push.sh"
PUBLIC_REL = "ClaudeCoach/public/site-data.json"

sys.path.insert(0, str(BASE / "ClaudeCoach/lib"))
from public_sanitise import sanitise_site_data, write_public_json

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


# Publication restored 28 Jul 2026, to public/site-data.json only.
#
# The removal note this replaces was right about the facts and wrong about the
# decision being available to make here: first name + race name + race date +
# ctl/atl/tsb + rolling ctl history is exactly the set the owner has now
# explicitly approved for publication. What was NOT approved - weight_kg, hrv,
# rhr - never appeared in this file, and now cannot: SITE_DATA_SPEC names seven
# keys per athlete and write_public_json() refuses the write if a forbidden key
# turns up.
#
# One consequence for the owner, not for this script: overview.html states the
# public data carries no "identifying performance metrics". That claim is no
# longer accurate. It is the owner-s copy to change, so it is flagged rather
# than rewritten.


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
    # Private full roll-up (gitignored) stays where every existing consumer
    # expects it. Nothing is published from this path.
    PRIVATE.write_text(json.dumps(pub, separators=(",", ":")))
    log(f"Wrote private {PRIVATE} with {len(athletes_out)} athlete(s): {list(athletes_out)}")

    # Sanitised public copy - the only one that goes to GitHub Pages.
    try:
        write_public_json(sanitise_site_data(pub), PUBLIC)
    except Exception as exc:
        log(f"PUBLIC WRITE REFUSED - {exc}")
        log("Nothing published.")
        sys.exit(1)
    log(f"Wrote public {PUBLIC} ({PUBLIC.stat().st_size} bytes, allow-listed)")

    try:
        r = subprocess.run(
            [str(GIT_PUSH), "refresh: sanitised public site data", PUBLIC_REL],
            cwd=str(BASE), capture_output=True, text=True, timeout=300)
        log(f"publish rc={r.returncode} {(r.stdout or '').strip()[-300:]}")
        if r.returncode != 0:
            log(f"publish stderr: {(r.stderr or '').strip()[-300:]}")
    except Exception as e:
        log(f"publish failed (non-fatal): {e}")


if __name__ == "__main__":
    main()
