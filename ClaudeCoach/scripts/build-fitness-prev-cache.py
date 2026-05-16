#!/usr/bin/env python3
"""
Build fitness-prev-cache.json for a given athlete.
Fetches CTL history for the previous season and writes the cache file.

Usage:
  python3 build-fitness-prev-cache.py --athlete kathryn --start 2024-01-01 --end 2025-09-20

The end date should be the previous race date (or close to it).
The script will also update profile.json with prev_race if --prev-race is given.
"""
import argparse, json, subprocess, sys
from pathlib import Path
from datetime import date

BASE = Path(__file__).parent.parent
PROJECT_DIR = str(BASE.parent)
ICU_FETCH = BASE / "lib/icu_fetch.py"

def log(msg):
    print(msg, flush=True)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--athlete", required=True)
    ap.add_argument("--start",   required=True, help="YYYY-MM-DD season start (used to compute --days)")
    ap.add_argument("--end",     required=True, help="YYYY-MM-DD previous race date (used as --newest)")
    ap.add_argument("--prev-race", default=None, help="Previous race name e.g. '70.3 Pescara 2025'")
    args = ap.parse_args()

    slug      = args.athlete
    start     = args.start
    end       = args.end
    prev_race = args.prev_race

    cache_path = BASE / f"athletes/{slug}/fitness-prev-cache.json"
    profile_path = BASE / f"athletes/{slug}/profile.json"

    from datetime import date as _date
    start_d = _date.fromisoformat(start)
    end_d   = _date.fromisoformat(end)
    days    = (end_d - start_d).days + 1

    log(f"[{slug}] Fetching CTL from {start} to {end} ({days} days)...")
    result = subprocess.run(
        ["python3", str(ICU_FETCH), "--athlete", slug,
         "--endpoint", "fitness", "--days", str(days), "--newest", end],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        cwd=PROJECT_DIR, timeout=60,
    )
    if result.returncode != 0:
        log(f"[{slug}] icu_fetch error: {result.stderr[:200]}")
        sys.exit(1)

    rows = json.loads(result.stdout)
    series = [
        [r["id"][:10], round(r.get("ctl") or 0, 1)]
        for r in rows if r.get("ctl")
    ]
    if not series:
        log(f"[{slug}] No CTL data returned for that range.")
        sys.exit(1)

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(series, indent=2))
    log(f"[{slug}] Written {len(series)} days to {cache_path.name}")

    # Optionally update profile.json with prev_race
    if prev_race and profile_path.exists():
        try:
            prof = json.loads(profile_path.read_text())
            prof["prev_race"] = prev_race
            profile_path.write_text(json.dumps(prof, indent=2))
            log(f"[{slug}] Updated profile.json prev_race = '{prev_race}'")
        except Exception as e:
            log(f"[{slug}] profile.json update failed: {e}")

    log(f"[{slug}] Done. Date range: {series[0][0]} → {series[-1][0]}, peak CTL: {max(v for _,v in series)}")

if __name__ == "__main__":
    main()
