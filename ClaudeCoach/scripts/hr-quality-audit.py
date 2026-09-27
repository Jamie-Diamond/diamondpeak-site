#!/usr/bin/env python3
"""Run the HR-quality check back over an athlete's history and report how big the
bad-HR problem is. Optionally writes athletes/<slug>/hr-quality-log.json so every
consumer that asks lib/hr_quality.untrusted_ids() sees the history, not just
activities that landed after the watcher started checking.

Jamie's call (27 Sep 2026): before building HR fallbacks for new athletes, run the
check over the existing athletes to see how much of their HR is actually bad.

Usage:
  python3 scripts/hr-quality-audit.py --all --days 180            # report only
  python3 scripts/hr-quality-audit.py --athlete kathryn --write   # report + log
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE / "lib"))

import hr_quality as q          # noqa: E402
from icu_api import IcuClient   # noqa: E402

SPORTS = q.RUN_TYPES + q.RIDE_TYPES + q.SWIM_TYPES


def _family(t: str) -> str:
    return "swim" if t in q.SWIM_TYPES else ("run" if t in q.RUN_TYPES else "bike")


def audit(slug: str, cfg: dict, days: int, write: bool, workers: int = 4) -> dict:
    client = IcuClient(cfg["icu_athlete_id"], cfg["icu_api_key"])
    acts = [a for a in client.get_training_history(days=days) if a.get("type") in SPORTS]
    log = q.load_log(slug)

    def one(a):
        err = None
        for _ in range(3):
            try:
                return a, q.assess_activity(client, a)
            except Exception as e:      # ICU rate limits / timeouts: retry, then skip
                err = e
                time.sleep(2)
        return a, {"verdict": "error", "reasons": [str(err)[:80]]}

    with ThreadPoolExecutor(workers) as ex:
        results = list(ex.map(one, acts))

    table = defaultdict(Counter)
    flagged = []
    for a, r in results:
        fam = _family(a["type"])
        table[fam][r["verdict"]] += 1
        table[fam]["rr"] += 1 if r.get("rr_present") else 0
        table[fam]["total"] += 1
        if r["verdict"] != "error":
            q.record(slug, a, r, log=log, save=False)
        if r["verdict"] in ("bad", "none") and fam != "swim":
            flagged.append((a.get("start_date_local", "")[:10], a["type"], r["verdict"],
                            ",".join(r.get("reasons", [])), a.get("name") or ""))
    if write:
        q.save_log(slug, log)
    return {"table": table, "flagged": sorted(flagged, reverse=True), "n": len(acts)}


def print_report(slug: str, res: dict, days: int) -> None:
    print(f"\n== {slug}: {res['n']} activities, last {days} days")
    print(f"   {'sport':6} {'total':>5} {'ok':>4} {'susp':>4} {'bad':>4} {'noHR':>4} {'strap*':>6}")
    for fam in ("bike", "run", "swim"):
        c = res["table"].get(fam)
        if not c:
            continue
        print(f"   {fam:6} {c['total']:>5} {c['ok']:>4} {c['suspect']:>4} {c['bad']:>4} "
              f"{c['none']:>4} {c['rr']:>6}")
    for row in res["flagged"]:
        print(f"   {row[0]} {row[1]:<12} {row[2]:<5} {row[3]:<22} {row[4][:40]}")


def main():
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--athlete")
    g.add_argument("--all", action="store_true")
    ap.add_argument("--days", type=int, default=180)
    ap.add_argument("--write", action="store_true", help="write hr-quality-log.json")
    args = ap.parse_args()

    athletes = json.loads((BASE / "config/athletes.json").read_text())
    slugs = [s for s, c in athletes.items() if c.get("active")] if args.all else [args.athlete]
    for slug in slugs:
        cfg = athletes.get(slug)
        if not cfg:
            print(f"unknown athlete {slug}", file=sys.stderr)
            continue
        print_report(slug, audit(slug, cfg, args.days, args.write), args.days)
    print("\n* strap = RR intervals recorded (chest/arm strap). Swim HR is informational only.")


if __name__ == "__main__":
    main()
