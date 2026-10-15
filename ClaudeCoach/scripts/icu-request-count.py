#!/usr/bin/env python3
"""How many Intervals.icu calls each athlete costs per day, and which job makes them.

IcuClient (lib/icu_api.py) and strava-to-icu.py log one line per call to
~/Library/Logs/ClaudeCoach/icu-requests/<UTC date>.log. Intervals.icu's coming limits
count per UTC day: 5000 per API key, and for OAuth (Peak's Connect Intervals.icu) 100
per athlete pooled across the app, minimum 5000 (lib/icu_link.py has the detail).

  python3 scripts/icu-request-count.py              today so far
  python3 scripts/icu-request-count.py 2026-10-16   one day
  python3 scripts/icu-request-count.py --days 3     the last 3 days
"""
import argparse
import json
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

CC = Path(__file__).resolve().parent.parent
LOG_DIR = Path(os.path.expanduser(os.environ.get("CC_ICU_COUNT_DIR",
                                                 "~/Library/Logs/ClaudeCoach/icu-requests")))


def slugs() -> dict:
    try:
        a = json.loads((CC / "config" / "athletes.json").read_text())
    except (OSError, ValueError):
        return {}
    return {str(v.get("icu_athlete_id") or "").lower().lstrip("i"): s
            for s, v in a.items() if isinstance(v, dict)}


def tally(day: str) -> tuple[Counter, dict, str, str]:
    """(calls per athlete, {athlete: Counter(caller)}, first time, last time) for a day."""
    per, by = Counter(), defaultdict(Counter)
    first = last = ""
    f = LOG_DIR / f"{day}.log"
    if not f.exists():
        return per, by, first, last
    names = slugs()
    for line in f.read_text().splitlines():
        parts = line.split()
        if len(parts) < 6:
            continue
        t, aid, _auth, caller = parts[:4]
        who = names.get(aid.lower().lstrip("i"), aid)
        per[who] += 1
        by[who][caller] += 1
        first, last = first or t, t
    return per, by, first, last


def main():
    p = argparse.ArgumentParser()
    p.add_argument("day", nargs="?")
    p.add_argument("--days", type=int, default=1)
    a = p.parse_args()
    end = datetime.strptime(a.day, "%Y-%m-%d") if a.day else datetime.now(timezone.utc)
    for i in range(a.days - 1, -1, -1):
        day = (end - timedelta(days=i)).strftime("%Y-%m-%d")
        per, by, first, last = tally(day)
        span = f" ({first}-{last} UTC)" if first else ""
        print(f"{day}: {sum(per.values())} calls{span}")
        for who, n in per.most_common():
            top = ", ".join(f"{c} {k}" for k, c in by[who].most_common(4))
            print(f"  {who:10} {n:6}   {top}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
