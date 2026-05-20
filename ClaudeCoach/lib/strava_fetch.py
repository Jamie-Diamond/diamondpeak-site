#!/usr/bin/env python3
"""CLI wrapper — fetch Strava activity detail (laps, splits, segment PRs) for use in prompts."""
import argparse, json, sys
from pathlib import Path

BASE = Path(__file__).parent.parent
sys.path.insert(0, str(BASE / "lib"))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--athlete", required=True)
    parser.add_argument("--strava-id", required=True)
    args = parser.parse_args()

    from strava_client import StravaClient
    try:
        sc = StravaClient(args.athlete)
        detail = sc.get_activity_detail(args.strava_id)
    except FileNotFoundError:
        print(json.dumps({"error": "no_tokens"}))
        return
    except Exception as e:
        print(json.dumps({"error": str(e)}))
        return

    def pace_str(speed_ms):
        if not speed_ms:
            return None
        secs = round(1000 / speed_ms)
        return f"{secs // 60}:{secs % 60:02d}/km"

    laps = []
    for i, lap in enumerate(detail.get("laps") or [], 1):
        spd = lap.get("average_speed") or 0
        laps.append({
            "lap":        i,
            "distance_km": round((lap.get("distance") or 0) / 1000, 3),
            "moving_time_s": lap.get("moving_time"),
            "pace":       pace_str(spd),
            "avg_hr":     int(lap["average_heartrate"]) if lap.get("average_heartrate") else None,
            "avg_watts":  round(lap["average_watts"]) if lap.get("average_watts") else None,
        })

    splits = []
    for s in (detail.get("splits_metric") or []):
        spd = s.get("average_speed") or 0
        splits.append({
            "km":      s.get("split"),
            "pace":    pace_str(spd),
            "avg_hr":  int(s["average_heartrate"]) if s.get("average_heartrate") else None,
            "avg_watts": round(s["average_watts"]) if s.get("average_watts") else None,
        })

    segment_prs = [
        se["name"] for se in (detail.get("segment_efforts") or [])
        if se.get("pr_rank") == 1 and se.get("name")
    ]

    print(json.dumps({
        "laps":         laps,
        "splits_metric": splits,
        "segment_prs":  segment_prs,
    }, indent=2))


if __name__ == "__main__":
    main()
