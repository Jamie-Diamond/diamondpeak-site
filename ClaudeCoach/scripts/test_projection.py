#!/usr/bin/env python3
"""Offline tests for the forward CTL projection on the app's Fitness chart (15 Aug 2026).
Run: python3 ClaudeCoach/scripts/test_projection.py

WHAT THIS COVERS. refresh-site-data.py cannot be run here — it needs live intervals.icu
credentials and the athlete directories are VM-only — so _projection_block is driven
directly from synthetic weekCalendar/fitness fixtures on a FIXED today. The failures
that would otherwise be invisible in review:

  * the JOIN. The linear tail has to start from the last day of the plan segment, not
    from today's CTL. Getting that wrong puts a step in a line the app draws as one
    continuous curve, and it looks like real data.
  * the CAP and the STOP DATE. Run past the Peak milestone at a positive ramp, the
    tail finishes above the "Race day" milestone plotted on the same axes and the
    chart contradicts its own markers.
  * the RAMP. It must be the identical figure /form quotes; a second definition of
    "4-wk ramp" is the bot and the app telling the athlete different things.
  * the public allow-list. ctlProjection.projection is nested, and an unnamed nested
    key is dropped SILENTLY — the chart would just draw nothing forward of today.
"""
import importlib.util
import sys
from datetime import date, timedelta
from pathlib import Path

_here = Path(__file__).resolve().parent
sys.path.insert(0, str(_here.parent / "lib"))
from public_sanitise import sanitise_training_data          # noqa: E402

# refresh-site-data.py is not importable by name (hyphens); importing it runs only
# module-level constants and imports, no network.
_spec = importlib.util.spec_from_file_location(
    "refresh_site_data", _here / "refresh-site-data.py")
R = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(R)

FAILED = []


def check(label, cond, detail=""):
    if cond:
        print(f"ok   {label}")
    else:
        FAILED.append(label)
        print(f"FAIL {label}" + (f"  [{detail}]" if detail else ""))


def close(a, b, tol=0.06):
    return a is not None and b is not None and abs(a - b) <= tol


TODAY = date(2026, 8, 15)
PEAK = {"date": "2026-09-07", "ctl": 112, "label": "Peak"}
RACE = {"date": "2026-09-19", "ctl": 97, "label": "Race day"}
MILESTONES = [PEAK, RACE]


def cal(loads, completed_back=7):
    """weekCalendar: `completed_back` days of completed history, then `loads`
    (day offset -> TSS) as planned events from today forward."""
    out = [{"date": (TODAY - timedelta(days=i)).isoformat(), "status": "completed",
            "tss": 80} for i in range(completed_back, 0, -1)]
    for off, tss in sorted(loads.items()):
        out.append({"date": (TODAY + timedelta(days=off)).isoformat(),
                    "status": "planned", "tss": tss})
    return out


def fitness(now_ctl, ramp_per_week, n=120):
    """A CTL series ending at `now_ctl` that rose at `ramp_per_week`. Index -28 is
    27 days back, matching bot.py's `history[-28]` exactly."""
    per_day = ramp_per_week / 7.0
    return [[(TODAY - timedelta(days=n - 1 - i)).isoformat(),
             round(now_ctl - per_day * (n - 1 - i), 1)] for i in range(n)]


# ── the ramp figure matches /form ────────────────────────────────────────────
series = fitness(105.1, 2.8)
ramp = R._ramp_4wk_per_week(series, 105.1)
expected = round((105.1 - series[-28][1]) / 4.0, 2)
check("ramp_4wk uses bot.py's series[-28] arithmetic", close(ramp, expected, 0.001),
      f"{ramp} vs {expected}")
check("ramp is None under four weeks of history",
      R._ramp_4wk_per_week(series[-20:], 105.1) is None)

# ── the CTL recursion over booked sessions ──────────────────────────────────
loads = {0: 0, 1: 174, 2: 113, 3: 0, 4: 255, 5: 62, 6: 69, 7: 169, 8: 174}
p = R._projection_block(105.0, cal(loads), series, MILESTONES, 4.0, TODAY)

check("plan projects exactly 7 days", p["plan_days"] == 7, str(p["plan_days"]))
check("plan[0] is the anchor at today", p["plan"][0]["date"] == TODAY.isoformat())
# THE STEP-AT-TODAY GUARD. plan[0] must equal today's actual CTL, or the projection
# starts below the 'This season' line at the same x and the chart shows a step down.
# It fired for real: recursing today as day 0 applies a day of decay to a CTL that
# already contains today's training, which put the plan line 2.5 CTL low.
check("anchor sits exactly on today's actual CTL",
      close(p["plan"][0]["ctl"], 105.0, 0.001), str(p["plan"][0]["ctl"]))
check("anchor carries no planned_load (it is measured, not projected)",
      "planned_load" not in p["plan"][0])
check("plan's last day is today+7",
      p["plan"][-1]["date"] == (TODAY + timedelta(days=7)).isoformat())

# Hand-run the recursion the docstring states: CTL += (TSS - CTL) / 42, from tomorrow.
ctl = 105.0
for i in range(1, 8):
    ctl += (loads[i] - ctl) / 42.0
check("plan CTL is the 42-day recursion over planned TSS",
      close(p["plan"][-1]["ctl"], round(ctl, 1)),
      f'{p["plan"][-1]["ctl"]} vs {round(ctl, 1)}')
check("planned_load is published per projected day",
      [x["planned_load"] for x in p["plan"][1:]] == [loads[i] for i in range(1, 8)])

# ── the join ────────────────────────────────────────────────────────────────
slope = p["linear_slope_per_week"]
# Same current_ctl the block was given (105.0), not the 105.1 used above — the ramp
# is measured against today's CTL, so the two must be quoted on the same basis.
check("slope is the observed ramp when it is under the cap",
      close(slope, R._ramp_4wk_per_week(series, 105.0), 0.001),
      f'{slope} vs {R._ramp_4wk_per_week(series, 105.0)}')
join = p["plan"][-1]["ctl"]
check("tail starts the day after the plan segment",
      p["linear"][0]["date"] == (TODAY + timedelta(days=8)).isoformat())
check("tail day 1 joins the PLAN endpoint, not today's CTL",
      close(p["linear"][0]["ctl"], round(join + slope / 7.0, 1)),
      f'{p["linear"][0]["ctl"]} vs {round(join + slope / 7.0, 1)}')
check("tail does not repeat the plan's last date",
      p["linear"][0]["date"] > p["plan"][-1]["date"])
dates = [x["date"] for x in p["plan"]] + [x["date"] for x in p["linear"]]
check("plan+tail dates are contiguous and unique",
      len(dates) == len(set(dates)) and all(
          date.fromisoformat(dates[i + 1]) - date.fromisoformat(dates[i]) == timedelta(days=1)
          for i in range(len(dates) - 1)))
# Straightness is asserted against the closed form, not against equal successive
# steps: the published values are rounded to 1dp, so a genuinely straight line has
# steps that alternate (e.g. 0.4, 0.3, 0.4) without being curved. The min() is the
# Peak cap, which binds in this fixture around 3 Sep — that flattening IS the design.
check("tail is straight: every point is min(join + slope x weeks, peak cap)",
      all(close(x["ctl"], round(min(join + slope * ((i + 1) / 7.0), float(PEAK["ctl"])), 1))
          for i, x in enumerate(p["linear"])))
check("the peak cap actually binds in this fixture (the guard is exercised)",
      p["linear"][-1]["ctl"] == float(PEAK["ctl"]) and
      join + slope * (len(p["linear"]) / 7.0) > PEAK["ctl"])

# ── the blocking check: never above the milestones it is drawn beside ───────
check("tail stops on the Peak milestone date", p["extend_to"] == PEAK["date"],
      str(p["extend_to"]))
check("tail's last date is the Peak date", p["linear"][-1]["date"] == PEAK["date"])
check("no projected point exceeds the Peak milestone",
      max(x["ctl"] for x in p["plan"] + p["linear"]) <= PEAK["ctl"])
check("tail never reaches race day, so it cannot contradict the Race-day marker",
      max(x["date"] for x in p["linear"]) < RACE["date"])
check("basis names both regimes", p["basis"] == "plan+ramp", p["basis"])

# ── clamping ────────────────────────────────────────────────────────────────
hot = R._projection_block(105.0, cal(loads), fitness(105.0, 9.0), MILESTONES, 4.0, TODAY)
check("slope clamps at max_ctl_ramp_per_week",
      close(hot["linear_slope_per_week"], 4.0, 0.001),
      str(hot["linear_slope_per_week"]))
check("clamped tail still cannot pass the Peak CTL",
      max(x["ctl"] for x in hot["linear"]) <= PEAK["ctl"])

cold = R._projection_block(105.0, cal(loads), fitness(105.0, -6.0), MILESTONES, 4.0, TODAY)
check("a negative ramp flattens to zero rather than projecting a decline",
      cold["linear_slope_per_week"] == 0.0)
check("flat tail holds the join value",
      len({x["ctl"] for x in cold["linear"]}) == 1 and
      close(cold["linear"][0]["ctl"], cold["plan"][-1]["ctl"]))

# ── degraded cases ──────────────────────────────────────────────────────────
empty = R._projection_block(105.0, cal({}), series, MILESTONES, 4.0, TODAY)
check("empty calendar gives no plan segment", empty["plan"] == [] and empty["plan_days"] == 0)
check("empty calendar still gives the linear tail", len(empty["linear"]) > 0)
check("empty-calendar tail starts from today's CTL",
      close(empty["linear"][0]["ctl"], round(105.0 + empty["linear_slope_per_week"] / 7.0, 1)))
check("empty calendar is flagged ramp-only", empty["basis"] == "ramp-only", empty["basis"])

past_peak = R._projection_block(105.0, cal(loads), series,
                                [{"date": "2026-08-10", "ctl": 112, "label": "Peak"}, RACE],
                                4.0, TODAY)
check("past peak: no tail is drawn rather than one faked into the taper",
      past_peak["linear"] == [] and past_peak["extend_to"] is None)
check("past peak is flagged plan-only", past_peak["basis"] == "plan-only", past_peak["basis"])

no_ms = R._projection_block(105.0, cal(loads), series, None, 4.0, TODAY)
check("no milestones: plan only, no invented horizon",
      no_ms["linear"] == [] and no_ms["basis"] == "plan-only")

short = R._projection_block(105.0, cal({0: 90, 1: 90, 2: 90}), series, MILESTONES, 4.0, TODAY)
check("a calendar shorter than 7 days truncates the plan segment",
      short["plan_days"] == 2, str(short["plan_days"]))
check("short calendar hands off to the tail at the plan's real end",
      short["linear"][0]["date"] == (TODAY + timedelta(days=3)).isoformat())
check("short plan still anchors on today's actual CTL",
      close(short["plan"][0]["ctl"], 105.0, 0.001))

nothing = R._projection_block(105.0, [], series[-10:], None, 4.0, TODAY)
check("no data at all is flagged 'none' and carries empty series",
      nothing["basis"] == "none" and nothing["plan"] == [] and nothing["linear"] == [])

# double-counting guard: a projected date that ALSO has a completed session is
# already inside current_ctl, so its planned twin must contribute nothing.
dup = cal({1: 200, 2: 90})
dup.append({"date": (TODAY + timedelta(days=1)).isoformat(),
            "status": "completed", "tss": 200})
d = R._projection_block(105.0, dup, series, MILESTONES, 4.0, TODAY)
check("planned TSS on a date already completed is not counted again",
      d["plan"][1]["planned_load"] == 0, str(d["plan"][1]["planned_load"]))

# ── the public allow-list ───────────────────────────────────────────────────
pub = sanitise_training_data({"ctlProjection": {
    "target_milestones": MILESTONES, "race_date": "2026-09-19",
    "target_ctl_min": 105, "target_ctl_max": 115, "projection": p,
}})
sp = (pub.get("ctlProjection") or {}).get("projection") or {}
for k in ("plan", "plan_days", "linear", "linear_slope_per_week",
          "ramp_4wk_per_week", "ramp_cap_per_week", "extend_to", "basis"):
    check(f"survives sanitising: projection.{k}", k in sp)
check("survives sanitising: projected plan rows keep planned_load",
      len(sp.get("plan") or []) > 1 and "planned_load" in sp["plan"][1])
check("survives sanitising: linear rows keep date and ctl",
      bool(sp.get("linear")) and set(sp["linear"][0]) == {"date", "ctl"})

print()
if FAILED:
    print(f"{len(FAILED)} FAILED: " + "; ".join(FAILED))
    sys.exit(1)
print("all projection tests passed")
