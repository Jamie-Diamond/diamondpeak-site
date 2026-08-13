#!/usr/bin/env python3
"""Offline tests for the per-sport DURATION series published for the app's Hours chart
(13 Aug 2026). Run: python3 ClaudeCoach/scripts/test_duration_series.py

WHAT THIS COVERS. refresh-site-data.py cannot be run here — it needs live intervals.icu
credentials and the athlete directories are VM-only — so the two pieces that can be
wrong silently are tested directly against synthetic activities:

  * _compute_per_sport_duration: the bucketing (does an OpenWaterSwim count as a Swim,
    does a WeightTraining count towards Total but no sport), the EWMA constant and the
    hours/week conversion. A unit slip here is invisible in review and shows up as a
    chart that is out by a factor of 8.6 — the trap telegram/charts.py documents.
  * the public allow-list: durationBySport has to survive sanitising WITH its Total key.
    The spec is fixed-key, so an unnamed bucket is dropped silently, and the chart's
    'All' state would then read as no data.
"""
import importlib.util
import json
import math
import sys
from datetime import date, timedelta
from pathlib import Path

_here = Path(__file__).resolve().parent
sys.path.insert(0, str(_here.parent / "lib"))
from public_sanitise import sanitise_training_data          # noqa: E402

# refresh-site-data.py is not importable by name (hyphens), and importing it runs only
# module-level constants and imports, no network.
_spec = importlib.util.spec_from_file_location(
    "refresh_site_data", _here / "refresh-site-data.py")
R = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(R)

FAILED = []


def check(label, cond):
    if cond:
        print(f"ok   {label}")
    else:
        FAILED.append(label)
        print(f"FAIL {label}")


def act(d, sport, minutes):
    return {"start_date_local": f"{d}T07:00:00", "type": sport,
            "moving_time": minutes * 60, "icu_training_load": 50}


# ── bucketing ───────────────────────────────────────────────────────────────
start = date(2026, 1, 1)
end = date(2026, 1, 10)
acts = [
    act("2026-01-01", "OpenWaterSwim", 60),
    act("2026-01-01", "VirtualRide", 90),
    act("2026-01-02", "TrailRun", 45),
    act("2026-01-02", "GravelRide", 120),
    act("2026-01-03", "WeightTraining", 30),        # Total only, no sport bucket
    act("2026-01-04", "Sail", 240),                 # Total only
    {"start_date_local": "", "type": "Run", "moving_time": 3600},          # undated
    act("2026-01-05", "Run", 0),                                           # no time
]
s = R._compute_per_sport_duration(acts, start, end)

check("all four buckets are published", sorted(s) == ["Ride", "Run", "Swim", "Total"])
check("every series covers the whole window, one point per day",
      all(len(v) == 10 for v in s.values()))
check("dates are contiguous from the window start",
      [r[0] for r in s["Total"]][:3] == ["2026-01-01", "2026-01-02", "2026-01-03"])

# Day 1 with the EWMA seeded at zero: k * minutes, in hours/week.
K = 1.0 - math.exp(-1.0 / 42.0)
check("swim variants count as Swim",
      abs(s["Swim"][0][1] - round(60 * K * 7 / 60, 2)) < 0.01)
check("virtual and gravel rides count as Ride",
      abs(s["Ride"][0][1] - round(90 * K * 7 / 60, 2)) < 0.01)
check("trail runs count as Run", s["Run"][0][1] == 0.0 and s["Run"][1][1] > 0)
check("Total is every activity, sport or not",
      abs(s["Total"][0][1] - round(150 * K * 7 / 60, 2)) < 0.01)
check("an undated activity is dropped, not dated to the window start",
      abs(s["Run"][0][1]) < 1e-9)

# Decay and the Total/sport split, on durations big enough that a day of decay is
# visible at the published 2dp. The first fixture's 45-minute run decays by 0.0003
# h/wk a day, which rounds to no change at all and would make a strict comparison
# fail on the rounding rather than on the maths.
decay = R._compute_per_sport_duration(
    [act("2026-01-01", "Ride", 300), act("2026-01-03", "WeightTraining", 300)],
    start, end)
check("a sport series decays on a day with nothing in it",
      decay["Ride"][1][1] < decay["Ride"][0][1] and decay["Ride"][1][1] > 0)
check("strength lifts Total", decay["Total"][2][1] > decay["Total"][1][1])
check("strength lifts no sport bucket",
      decay["Ride"][2][1] < decay["Ride"][1][1] and
      all(v[2][1] == 0.0 for v in (decay["Run"], decay["Swim"])))
check("a zero-duration activity does not lift the series",
      R._compute_per_sport_duration([act("2026-01-02", "Ride", 0)],
                                    start, end)["Ride"][-1][1] == 0.0)

# ── the unit: a steady hour a day converges on 7 hours a week ───────────────
long_start, long_end = date(2025, 1, 1), date(2026, 6, 30)
steady = [act((long_start + timedelta(days=i)).isoformat(), "Ride", 60)
          for i in range((long_end - long_start).days + 1)]
st = R._compute_per_sport_duration(steady, long_start, long_end)
check("an hour a day settles at 7 h/wk (not 1, not 60)",
      abs(st["Ride"][-1][1] - 7.0) < 0.05)
check("the 42-day constant is slow: one week in, still well under steady state",
      st["Ride"][6][1] < 1.2)
check("the same series is reported for Total", st["Total"][-1][1] == st["Ride"][-1][1])

# ── an empty season is a flat zero series, not an exception ─────────────────
empty = R._compute_per_sport_duration([], start, end)
check("no activities gives a zero series of the right length",
      all(len(v) == 10 and all(r[1] == 0.0 for r in v) for v in empty.values()))

# ── the bucketing rule is shared with the CTL series ───────────────────────
check("one bucketing rule for both series",
      R._ctl_sport_family("OpenWaterSwim") == "Swim" and
      R._ctl_sport_family("VirtualRide") == "Ride" and
      R._ctl_sport_family("TrailRun") == "Run" and
      R._ctl_sport_family("WeightTraining") is None and
      R._ctl_sport_family(None) is None)
ctl = R._compute_per_sport_ctl(acts, start, end)
check("CTL and duration cover the same sports and the same days",
      all(len(ctl[k]) == len(s[k]) and
          [r[0] for r in ctl[k]] == [r[0] for r in s[k]] for k in ctl))

# ── the public allow-list ──────────────────────────────────────────────────
season = {"Total": [["2026-01-01", 8.0]], "Ride": [["2026-01-01", 4.0]],
          "Run": [["2026-01-01", 2.5]], "Swim": [["2026-01-01", 1.5]]}
raw = {
    "generated": "2026-08-13",
    "durationBySport": {"current": dict(season, Hike=[["2026-01-01", 1.0]]),
                        "prev": season, "prev2": {}},
    # Not named in the spec, so it must not reach the public file. Guards against a
    # copy-paste that widens the block rather than adds one key to it.
    "weightTrend": [["2026-01-01", 71.2]],
}
pub = sanitise_training_data(raw)
check("durationBySport reaches the public file", "durationBySport" in pub)
check("Total survives sanitising",
      pub["durationBySport"]["current"]["Total"] == [["2026-01-01", 8.0]])
check("all three seasons survive",
      sorted(pub["durationBySport"]) == ["current", "prev", "prev2"])
check("an unnamed bucket is dropped",
      "Hike" not in pub["durationBySport"]["current"])
check("nothing else rode in with it", "weightTrend" not in pub)

# ── shape: what the app reads is what the publisher writes ─────────────────
check("every point is [date, number]",
      all(isinstance(r, list) and len(r) == 2 and isinstance(r[0], str)
          and isinstance(r[1], float) for v in s.values() for r in v))

if FAILED:
    print(f"\n{len(FAILED)} FAILED")
    sys.exit(1)
print("\nall checks passed")
