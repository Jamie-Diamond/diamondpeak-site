#!/usr/bin/env python3
"""
Build duration-prev-cache.json for a given athlete: per-day training MINUTES for a
previous season, in the {"race", "label", "days"} shape telegram/charts.py's
duration_chart() consumes directly as payload["prev"] / payload["prev2"].

Mirrors build-fitness-prev-cache.py's --athlete/--start/--end CLI shape, but there is
no single ICU endpoint for a rolling minutes/week series the way `fitness` gives CTL,
so this fetches raw activities directly via IcuClient._get("activities", ...) — the
same call get_training_summary() makes internally (lib/icu_api.py) — rather than going
through icu_fetch.py's CLI, which only windows the `activities` endpoint from "today"
backwards (get_training_history(days)) and has no oldest+newest pair for a past season.

Usage:
  python3 build-duration-prev-cache.py --athlete kathryn --season prev \
      --start 2024-10-01 --end 2025-07-30 --race 2025-07-30 --label "2025 Ironman UK"

  python3 build-duration-prev-cache.py --athlete kathryn --season prev2 \
      --start 2023-10-01 --end 2024-07-28 --race 2024-07-28 --label "2024 season"

--season selects which key of duration-prev-cache.json this run writes ("prev" or
"prev2"), so a file can hold both without the second overwriting the first — same
reason build-fitness-prev-cache.py's 2023 season lives in a SEPARATE file
(fitness-2023-cache.json); here it's one file, two keys, because duration_chart's
payload contract already expects "prev"/"prev2" as sibling keys.

--race defaults to --end (the common case: the fetch window ends on race day) and
--label defaults to a bare year. Missing history (no activities in range, no athlete
in config) is handled by exiting non-zero with a message rather than writing an empty
or partial cache — a missing previous season should fall back to duration_chart's own
"no overlay" path, not a cache entry with an empty "days" list.
"""
import argparse, json, sys
from pathlib import Path
from datetime import date

BASE = Path(__file__).parent.parent
sys.path.insert(0, str(BASE / "lib"))
from icu_api import IcuClient  # noqa: E402


def log(msg):
    print(msg, flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--athlete", required=True)
    ap.add_argument("--season", choices=["prev", "prev2"], default="prev",
                    help="which duration_chart payload key this season fills")
    ap.add_argument("--start", required=True, help="YYYY-MM-DD season start")
    ap.add_argument("--end",   required=True, help="YYYY-MM-DD end of the fetch window "
                                                     "(usually the race date)")
    ap.add_argument("--race",  default=None, help="YYYY-MM-DD race date for this season "
                                                    "(defaults to --end)")
    ap.add_argument("--label", default=None, help="e.g. '2025 Ironman UK' (defaults to "
                                                    "'<year> season')")
    args = ap.parse_args()

    slug  = args.athlete
    start = args.start
    end   = args.end
    race  = args.race or end
    label = args.label or f"{race[:4]} season"

    # Fail fast on unparseable dates rather than writing a cache duration_chart will
    # later skip anyway (it catches a malformed `race` and drops that season silently —
    # better to catch it here, at build time, where a mistake is visible).
    for d in (start, end, race):
        try:
            date.fromisoformat(d)
        except ValueError:
            log(f"ERROR: '{d}' is not a YYYY-MM-DD date")
            sys.exit(1)

    config_path = BASE / "config/athletes.json"
    athletes = json.loads(config_path.read_text())
    if slug not in athletes:
        log(f"ERROR: athlete '{slug}' not in config/athletes.json")
        sys.exit(1)
    a = athletes[slug]
    client = IcuClient(a["icu_athlete_id"], a["icu_api_key"])

    log(f"[{slug}] Fetching activities {start} to {end} for season '{args.season}'...")
    try:
        activities = client._get("activities", {
            "oldest": start, "newest": end,
            "cols": "start_date_local,moving_time",
        })
    except Exception as e:
        log(f"[{slug}] icu fetch error: {e}")
        sys.exit(1)

    if not activities:
        log(f"[{slug}] No activities returned for {start}..{end}.")
        sys.exit(1)

    by_day = {}
    for act in activities:
        d = (act.get("start_date_local") or "")[:10]
        if not d:
            continue
        try:
            date.fromisoformat(d)
        except ValueError:
            continue
        by_day[d] = by_day.get(d, 0.0) + float(act.get("moving_time") or 0) / 60.0

    if not by_day:
        log(f"[{slug}] No dated activities in {start}..{end}.")
        sys.exit(1)

    series = [[d, round(m, 1)] for d, m in sorted(by_day.items())]

    cache_path = BASE / f"athletes/{slug}/duration-prev-cache.json"
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache = {}
    if cache_path.exists():
        try:
            cache = json.loads(cache_path.read_text())
        except Exception:
            cache = {}
    cache[args.season] = {"race": race, "label": label, "days": series}
    cache_path.write_text(json.dumps(cache, indent=2))
    log(f"[{slug}] Written {len(series)} days to {cache_path.name} [{args.season}] "
        f"({series[0][0]} -> {series[-1][0]}, race={race}, label='{label}')")


if __name__ == "__main__":
    main()
