#!/usr/bin/env python3
"""Heat acclimation score — current % and 30-day ASCII trend.

Usage: python3 heat_accl.py <slug>
"""
import sys
from datetime import date, timedelta
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE / "lib"))
import heat as heat_lib


def main():
    if len(sys.argv) < 2:
        print("Usage: heat_accl.py <slug>", file=sys.stderr)
        sys.exit(1)
    slug = sys.argv[1]
    today = date.today()

    score = heat_lib.acclimation_score(slug)
    score_7d = heat_lib.acclimation_score(slug, today - timedelta(days=7))
    delta = score - score_7d

    trend = ""
    if abs(delta) > 5:
        trend = f" ↑ (+{delta:.1f}%)" if delta > 0 else f" ↓ ({delta:.1f}%)"

    print(f"Heat acclimation: {score:.0f}%{trend}")
    print(f"7-day ago score:  {score_7d:.0f}%")
    print()
    print("30-day trend (each █ = 5%):")
    for i in range(29, -1, -1):
        d = today - timedelta(days=i)
        s = heat_lib.acclimation_score(slug, d)
        bar = "█" * max(0, int(s / 5))
        label = f"{s:.0f}%".rjust(4)
        print(f"  {d}  {bar} {label}")


if __name__ == "__main__":
    main()
