#!/bin/bash
# Weekly summary — runs via VM crontab at 20:00 every Sunday.
# Delegates to weekly-summary.py which fetches IcuSync data directly (no MCP).
# Loops over all active athletes in config/athletes.json.
# Safe to run manually: bash weekly-summary.sh

cd /Users/diamondpeakconsulting/diamondpeak-site

python3 - <<'PYEOF'
import json, subprocess, sys
from pathlib import Path

BASE   = Path("ClaudeCoach")
config = json.loads((BASE / "config/athletes.json").read_text())

for slug, a in config.items():
    if not a.get("active"):
        continue
    print(f"[weekly-summary] Running for {slug}...", flush=True)
    r = subprocess.run(
        ["python3", str(BASE / "scripts/weekly-summary.py"), "--athlete", slug],
        cwd="/Users/diamondpeakconsulting/diamondpeak-site",
    )
    if r.returncode != 0:
        print(f"[weekly-summary] ERROR for {slug} (exit {r.returncode})", file=sys.stderr)
PYEOF
