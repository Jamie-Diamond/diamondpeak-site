#!/usr/bin/env python3
"""Offline tests for lib/baseline.py — the new-athlete baseline block.
Run: python3 ClaudeCoach/scripts/test_baseline.py

WHAT THIS GUARDS, in the order it would hurt someone:
  1. An established athlete is touched. No baseline.json must mean no gate, no RPE
     override, no prompt text: Jamie, Kathryn and Calum run exactly as before.
  2. The tests are crammed. Jamie's condition for a one-week block was "if spaced":
     bike and run tests 48 h apart, a clear day between tests whenever the window
     allows, and a second week only when it does not.
  3. A result is read wrong. Ramp = 75% of the best minute, run threshold = the last
     20 min of the best 30, CSS from the two rep times (and yards pools work).
  4. A result lands without the athlete's say-so, or a confirmed one misses
     Intervals.icu (the renderer reads ICU, not profile.json).
  5. The block never ends, or ends silently.

No network, no real athlete file: a temp BASE and a fake ICU client.
"""
from __future__ import annotations

import copy
import json
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

_here = Path(__file__).resolve().parent
sys.path.insert(0, str(_here.parent / "lib"))

import baseline as bl   # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


TRI = ["swim", "bike", "run"]
MON = date(2026, 9, 28)


def tri_state(**kw):
    return bl.new_state(TRI, kw.get("hr", "wrist"), kw.get("power", True),
                        kw.get("known", {"bike": {"ftp": 250, "_source": "icu"}}),
                        kw.get("recent", set()), today=MON)


# ── 1. new_state ─────────────────────────────────────────────────────────────
st = bl.new_state(TRI, "wrist", False,
                  {"bike": {"ftp": 250, "_source": "icu"}, "run": {"threshold_pace": "4:30", "_source": "athlete"}},
                  {"run"})
c = {f: st["sports_state"][f]["confidence"] for f in TRI}
check("stated recent test = tested, ICU number = estimated, none = missing",
      c == {"run": "tested", "bike": "estimated", "swim": "missing"}, c)
check("tested sport gets no test", st["sports_state"]["run"]["test"] is None)
check("bike with no power and a wrist sensor has no test (nothing to measure)",
      st["sports_state"]["bike"]["test"] is None)
check("bike with no power but a strap gets the HR time trial",
      bl.protocol_for("bike", False, "strap") == "bike_hr")
check("bike with power gets the ramp", bl.protocol_for("bike", True, "none") == "bike_ramp")
check("athlete-typed tested value remembers it came from the athlete",
      st["sports_state"]["run"]["value_source"] == "athlete")


# ── 2. scheduling ────────────────────────────────────────────────────────────
def tests_of(days):
    return {d["family"]: date.fromisoformat(d["date"]) for d in days if d["kind"] == "test"}


for i in range(7):
    first = MON + timedelta(days=i)
    s = tri_state()
    days = bl.schedule(s, first, None, 10)
    t = tests_of(days)
    end = date.fromisoformat(s["window"]["end"])
    ds = sorted(t.values())
    gaps = [(ds[k + 1] - ds[k]).days for k in range(len(ds) - 1)]
    tag = first.strftime("%a")
    check(f"{tag} start: all three tests placed", len(t) == 3, t)
    check(f"{tag} start: bike and run tests 48 h+ apart", abs((t["bike"] - t["run"]).days) >= 2, t)
    check(f"{tag} start: a clear day between every test", min(gaps) >= 2, gaps)
    check(f"{tag} start: never a test on the first day", date.fromisoformat(days[0]["date"]) not in ds)
    check(f"{tag} start: window ends on a Sunday", end.weekday() == 6, end)
    check(f"{tag} start: at most two weeks", (end - first).days <= 13, (end - first).days)
    # one rest in every 7-day stretch of 6+ days
    for w0 in range(0, len(days), 7):
        chunk = days[w0:w0 + 7]
        if len(chunk) >= 6:
            check(f"{tag} start: rest day in days {w0}-{w0 + len(chunk) - 1}",
                  any(d["kind"] == "rest" for d in chunk), [d["kind"] for d in chunk])
    # day before a test is short
    for k in range(len(days) - 1):
        if days[k]["kind"] == "easy" and days[k + 1]["kind"] == "test":
            check(f"{tag} start: short easy day before a test", days[k]["minutes"] <= 40, days[k])
            break

s = tri_state()
bl.schedule(s, MON, None, 10)
check("Monday start with every day free is ONE week", s["window"]["end"] == "2026-10-04", s["window"])

one = bl.new_state(["bike"], "none", True, {}, set(), today=MON)
bl.schedule(one, MON + timedelta(days=2), None, 8)
check("a single test never needs a second week (Wed start ends Sunday)",
      one["window"]["end"] == "2026-10-04", one["window"])

s = tri_state()
days = bl.schedule(s, MON, ["Saturday", "Sunday"], 5)
placed = [f for f in TRI if s["sports_state"][f]["test"]["status"] == "scheduled"]
check("two training days a week still places what it can, marks the rest",
      placed and all(s["sports_state"][f]["test"]["status"] in ("scheduled", "unplaceable") for f in TRI),
      {f: s["sports_state"][f]["test"] for f in TRI})
check("  ...and puts nothing on days the athlete cannot train",
      all(d["kind"] == "rest" for d in days
          if bl.DAYS[date.fromisoformat(d["date"]).weekday()] not in ("Saturday", "Sunday")))

# ── 3. workouts ──────────────────────────────────────────────────────────────
s = tri_state()
days = bl.schedule(s, MON, None, 10)
ramp = next(bl.workout_for(d) for d in days if d.get("protocol") == "bike_ramp")
check("ramp steps are fixed watts from 40% of the estimated FTP",
      "1m 100w" in ramp["description"] and "1m 120w" in ramp["description"], ramp["description"][:200])
easy = next(bl.workout_for(d) for d in days if d["kind"] == "easy")
check("easy sessions carry no % target (RPE only)", "%" not in easy["description"], easy["description"])
swim = next(bl.workout_for(d) for d in days if d.get("protocol") == "swim_css")
check("swim reps use metres (mtr), not minutes", "400mtr" in swim["description"]
      and "200mtr" in swim["description"], swim["description"])
check("no em-dashes in anything the athlete reads",
      not any("—" in (w or {}).get(k, "") for d in days for w in [bl.workout_for(d)]
              for k in ("name", "description", "description_raw")))

# ── 4. result maths ──────────────────────────────────────────────────────────
t = list(range(0, 900 + 11 * 60))
w = [120] * 900 + [100 + 20 * (i // 60) for i in range(11 * 60)]   # ramp to 300 W
r = bl.bike_ramp_result({"time": t, "watts": w})
check("ramp: best minute 300 W -> FTP 225", r and r["ftp"] == 225 and r["best1m_w"] == 300, r)
check("ramp ridden at 100 W is unreadable", bl.bike_ramp_result({"time": t, "watts": [100] * len(t)}) is None)

n = 60 * 60
t = list(range(n))
v = [3.0] * 900 + [1000 / 240] * 1800 + [2.8] * (n - 2700)          # 30 min at 4:00/km
hr = [130] * 900 + [150] * 600 + [168] * 1200 + [140] * (n - 2700)
r = bl.run_tt_result({"time": t, "velocity_smooth": v, "heartrate": hr}, hr_trusted=True)
check("run TT: 30 min at 4:00/km -> threshold 4:00/km", r and r["threshold_pace"] == "4:00", r)
check("run TT: LTHR = last 20 min average HR", r and r.get("lthr") == 168, r)
r = bl.run_tt_result({"time": t, "velocity_smooth": v, "heartrate": hr}, hr_trusted=False)
check("run TT with untrusted HR sets pace but no LTHR", r and "lthr" not in r, r)

ivs = [{"type": "WORK", "distance": 400, "moving_time": 400, "average_speed": 1.0, "start_index": 100},
       {"type": "RECOVERY", "distance": None, "moving_time": 300, "start_index": 500},
       {"type": "WORK", "distance": 200, "moving_time": 190, "average_speed": 1.05, "start_index": 800}]
r = bl.swim_css_result(ivs)
check("CSS: 400 in 6:40, 200 in 3:10 -> 1:45/100m", r and r["css"] == "1:45", r)
yd = [{"type": "WORK", "distance": 365.76, "moving_time": 380, "average_speed": 0.96, "start_index": 1},
      {"type": "WORK", "distance": 182.88, "moving_time": 180, "average_speed": 1.02, "start_index": 9}]
check("CSS in a yards pool uses the distances swum",
      bl.swim_css_result(yd) and bl.swim_css_result(yd)["css"] == "1:49", bl.swim_css_result(yd))
check("typed times parse", bl.parse_swim_times("400 in 7:10, 200 in 3:25") == (430, 205))
check("one time is not enough", bl.parse_swim_times("did the 400 in 7:10") is None)


# ── 5. state lifecycle with a temp BASE and a fake ICU ──────────────────────
class FakeICU:
    def __init__(self):
        self.puts = []

    def _put(self, path, payload):
        self.puts.append((path, payload))
        return payload


with tempfile.TemporaryDirectory() as d:
    bl.BASE = Path(d)
    (Path(d) / "athletes" / "new").mkdir(parents=True)
    (Path(d) / "athletes" / "new" / "profile.json").write_text("{}")

    check("no file: blocks nothing", not bl.blocks_week("jamie", MON))
    check("no file: nothing on RPE", bl.rpe_only_families("jamie") == set())
    check("no file: no prompt text", bl.prompt_block("jamie", "Jamie") == "")
    check("no file: tick is silent", bl.tick("jamie", MON + timedelta(days=30)) is None)

    s = tri_state()
    bl.save("new", s)
    check("pending blocks every week", bl.blocks_week("new", MON + timedelta(days=21)))
    days = bl.schedule(s, MON, None, 10)
    s["status"] = "active"
    for f in TRI:
        s["sports_state"][f]["test"]["icu_event_id"] = f"ev-{f}"
    bl.save("new", s)
    check("active blocks its own week", bl.blocks_week("new", MON))
    check("active does not block the week after", not bl.blocks_week("new", MON + timedelta(days=7)))
    check("in_window on a block day", bl.in_window("new", MON + timedelta(days=2)))
    check("all three sports on RPE until tested", bl.rpe_only_families("new") == set(TRI))
    check("prompt says baseline block while active", "BASELINE BLOCK" in bl.prompt_block("new", "Sam"))

    bike_day = s["sports_state"]["bike"]["test"]["date"]
    act = {"id": "i1", "type": "Ride", "start_date_local": bike_day + "T07:00:00", "name": "Morning Ride"}
    t = list(range(0, 900 + 11 * 60))
    w = [120] * 900 + [100 + 20 * (i // 60) for i in range(11 * 60)]
    out = bl.capture("new", act, {"time": t, "watts": w}, hr_trusted=False)
    check("a ride on the bike test day is captured as pending", out and out["keyboard"]
          and bl.load("new")["sports_state"]["bike"]["test"]["status"] == "result_pending", out)
    check("pending is NOT applied yet", bl.load("new")["sports_state"]["bike"]["confidence"] == "estimated")
    other = {"id": "i2", "type": "Run", "start_date_local": bike_day + "T18:00:00", "name": "Jog"}
    check("a run on the bike test day is not the run test", bl.capture("new", other, {}, True) is None)

    icu = FakeICU()
    msg = bl.confirm("new", "bike", icu)
    check("confirm writes FTP to Intervals.icu Ride settings",
          icu.puts == [("sport-settings/Ride", {"ftp": 225})], icu.puts)
    check("confirm marks bike tested and takes it off RPE",
          bl.load("new")["sports_state"]["bike"]["confidence"] == "tested"
          and "bike" not in bl.rpe_only_families("new"))
    check("confirm writes profile.json ftp_watts",
          json.loads((Path(d) / "athletes/new/profile.json").read_text()).get("ftp_watts") == 225)
    check("confirm twice is harmless", "already" in bl.confirm("new", "bike", icu) and len(icu.puts) == 1)
    check("athlete-facing confirm text has no em-dash", "—" not in msg, msg)

    swim_day = s["sports_state"]["swim"]["test"]["date"]
    sw = {"id": "i3", "type": "Swim", "start_date_local": swim_day + "T07:00:00", "name": "Pool"}
    out = bl.capture("new", sw, {}, False, intervals=[])
    check("swim with unreadable reps asks for the times",
          out and "send me the two times" in out["text"]
          and bl.load("new")["sports_state"]["swim"]["test"]["status"] == "awaiting_times", out)
    out = bl.capture_swim_times("new", "400 in 6:40 and 200 in 3:10")
    check("typed swim times become a pending CSS result", out and "1:45" in out["text"], out)
    bl.reject("new", "swim")
    check("reject leaves swim untested and on RPE",
          bl.load("new")["sports_state"]["swim"]["confidence"] == "missing"
          and "swim" in bl.rpe_only_families("new"))

    end = date.fromisoformat(bl.load("new")["window"]["end"])
    check("tick is silent inside the window", bl.tick("new", end) is None)
    m = bl.tick("new", end + timedelta(days=1))
    st2 = bl.load("new")
    check("tick after the window closes the block with one message",
          m and st2["status"] == "complete" and "Baseline week done" in m, m)
    check("  ...the untaken run test is marked missed",
          st2["sports_state"]["run"]["test"]["status"] == "missed")
    check("  ...and says run stays by feel", "run sessions stay by feel" in m, m)
    check("tick twice sends nothing", bl.tick("new", end + timedelta(days=2)) is None)
    check("complete: plan no longer blocked", not bl.blocks_week("new", end + timedelta(days=1)))
    check("complete: untested sports stay on RPE", bl.rpe_only_families("new") == {"run", "swim"})
    check("complete prompt offers to book the missing test", "baseline-week.py book" in
          bl.prompt_block("new", "Sam"))

# ── rough figures (30 Sep 2026): a range to check the test against, never a threshold ──
pe = bl.parse_estimate
check("5k time -> a threshold range just slower than 5k pace",
      pe("run", "5k 24:30 in August") == {"low": 300, "high": 327, "unit": "s_km", "raw": "5k 24:30 in August"})
check("half written h:mm is read as hours", pe("run", "half 1:52")["low"] < 330)
check("marathon h:mm read as hours", 250 < pe("run", "marathon 3:45")["low"] < 320)
check("a run time with no distance is not guessed", pe("run", "24:30") is None)
check("don't know / nov are told apart", pe("bike", "don't know") is None and pe("run", "nov 5k 24:30") is not None)
check("FTP guess becomes +-8%", pe("bike", "about 230") == {"low": 212, "high": 248, "unit": "w", "raw": "about 230"})
check("FTP range kept as given", (pe("bike", "240-210")["low"], pe("bike", "240-210")["high"]) == (210, 240))
check("swim 400 time per 100", pe("swim", "400m 8:30")["unit"] == "s_100m" and 115 < pe("swim", "400m 8:30")["low"] < 125)
ex = pe("bike", "about 230")
check("a ramp result far below the guess is questioned", "weaker" in bl.expected_note("bike", {"ftp": 150}, ex))
check("a ramp result near the guess is not", bl.expected_note("bike", {"ftp": 225}, ex) == "")
check("a run pace far faster than the guess is questioned",
      "stronger" in bl.expected_note("run", {"threshold_pace": "3:50"}, pe("run", "5k 24:30")))
st = bl.new_state(["bike", "run"], "strap", True, {}, set(), expected={"bike": ex})
check("expected range stored on the sport being tested", st["sports_state"]["bike"].get("expected") == ex
      and "expected" not in st["sports_state"]["run"])
check("rough guide reads as a loose easy range", bl.rough_guide("bike", ex).startswith("bike easy roughly 117-174 W"))

print()
print("ALL PASS" if not FAILS else f"{len(FAILS)} FAILED: {FAILS}")
sys.exit(1 if FAILS else 0)
