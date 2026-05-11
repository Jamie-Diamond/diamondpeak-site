#!/usr/bin/env python3
"""CLI wrapper around IcuClient — lets Claude call the Intervals.icu API via Bash.

Usage:
  python3 icu_fetch.py --athlete jamie --endpoint wellness --days 14
  python3 icu_fetch.py --athlete jamie --endpoint events --start 2026-05-11 --end 2026-06-01
  python3 icu_fetch.py --athlete jamie --endpoint activity_detail --activity-id i146909545
  python3 icu_fetch.py --athlete jamie --endpoint best_efforts --sport Ride --period 1y
  python3 icu_fetch.py --athlete jamie --endpoint power_curves --sport Ride --curves 90d
  python3 icu_fetch.py --athlete jamie --endpoint training_summary --start 2026-04-13 --end 2026-05-11
  python3 icu_fetch.py --athlete jamie --endpoint sport_settings --sport Ride

Outputs JSON to stdout. Exits non-zero on error.
"""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "lib"))

from icu_api import IcuClient

CONFIG = ROOT / "config" / "athletes.json"


def load_client(slug: str) -> IcuClient:
    athletes = json.loads(CONFIG.read_text())
    if slug not in athletes:
        print(f"ERROR: athlete '{slug}' not in config/athletes.json", file=sys.stderr)
        sys.exit(1)
    a = athletes[slug]
    return IcuClient(a["icu_athlete_id"], a["icu_api_key"])


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--athlete", required=True)
    p.add_argument("--endpoint", required=True,
                   choices=["profile", "sport_settings", "fitness", "wellness",
                            "history", "events", "activity_detail", "extended_metrics",
                            "streams", "best_efforts", "power_curves", "training_summary",
                            "push_workout", "edit_workout", "delete_workout"])
    p.add_argument("--days",        type=int, default=14)
    p.add_argument("--start",       default=None, help="oldest date YYYY-MM-DD")
    p.add_argument("--end",         default=None, help="newest date YYYY-MM-DD")
    p.add_argument("--sport",       default=None, help="e.g. Ride, Run, Swim")
    p.add_argument("--period",      default="1y",  help="for best_efforts: 4w 6w 3m 6m 1y 2y all")
    p.add_argument("--curves",      default="90d", help="for power_curves: 90d 1y s0 s1 all")
    p.add_argument("--activity-id", default=None, dest="activity_id")
    p.add_argument("--event-id",    default=None, dest="event_id")
    p.add_argument("--newest",      default=None, help="future end date for fitness projection")
    p.add_argument("--payload",     default=None,
                   help="JSON string for push_workout / edit_workout fields")
    args = p.parse_args()

    client = load_client(args.athlete)

    ep = args.endpoint
    if ep == "profile":
        result = client.get_athlete_profile()
    elif ep == "sport_settings":
        result = client.get_sport_settings(args.sport)
    elif ep == "fitness":
        result = client.get_fitness(args.days, newest=args.newest)
    elif ep == "wellness":
        result = client.get_wellness(args.days, newest=args.newest)
    elif ep == "history":
        result = client.get_training_history(args.days, sport=args.sport)
    elif ep == "events":
        if not args.start or not args.end:
            print("ERROR: --start and --end required for events", file=sys.stderr)
            sys.exit(1)
        result = client.get_events(args.start, args.end, category=args.sport)
    elif ep == "activity_detail":
        if not args.activity_id:
            print("ERROR: --activity-id required", file=sys.stderr)
            sys.exit(1)
        result = client.get_activity_detail(args.activity_id)
    elif ep == "extended_metrics":
        if not args.activity_id:
            print("ERROR: --activity-id required", file=sys.stderr)
            sys.exit(1)
        result = client.get_extended_metrics(args.activity_id)
    elif ep == "streams":
        if not args.activity_id:
            print("ERROR: --activity-id required", file=sys.stderr)
            sys.exit(1)
        result = client.get_activity_streams(args.activity_id)
    elif ep == "best_efforts":
        result = client.get_best_efforts(args.sport or "Ride", args.period)
    elif ep == "power_curves":
        result = client.get_power_curves(args.sport or "Ride", args.curves)
    elif ep == "training_summary":
        if not args.start or not args.end:
            print("ERROR: --start and --end required for training_summary", file=sys.stderr)
            sys.exit(1)
        result = client.get_training_summary(args.start, args.end, sport=args.sport)
    elif ep == "push_workout":
        if not args.payload:
            print("ERROR: --payload JSON required for push_workout", file=sys.stderr)
            sys.exit(1)
        fields = json.loads(args.payload)
        result = client.push_workout(**fields)
    elif ep == "edit_workout":
        if not args.event_id or not args.payload:
            print("ERROR: --event-id and --payload required for edit_workout", file=sys.stderr)
            sys.exit(1)
        fields = json.loads(args.payload)
        result = client.edit_workout(args.event_id, **fields)
    elif ep == "delete_workout":
        if not args.event_id:
            print("ERROR: --event-id required for delete_workout", file=sys.stderr)
            sys.exit(1)
        client.delete_workout(args.event_id)
        result = {"deleted": args.event_id}

    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
