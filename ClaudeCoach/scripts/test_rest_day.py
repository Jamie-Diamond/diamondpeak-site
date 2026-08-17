#!/usr/bin/env python3
"""Offline tests for lib/rest_day.py — reserving the weekly rest day at build time.
Run: python3 ClaudeCoach/scripts/test_rest_day.py

WHAT THIS GUARDS. `validate_week` hard-fails a seven-day week, but the only thing that
had ever ASKED for a rest day during construction was prose in the Stage-1 prompt, so
every attempt could come back 7/7 and the picker would rank among seven-day weeks.
lib/rest_day makes that unreachable. The checks below are the four ways a reserver of
this kind goes wrong, and each is a real behaviour of this system rather than a
hypothetical:

  1. It disagrees with the validator about what "loaded" means. The rule counts a day
     with planned LOAD, so zero-load mobility must not cost a rest day and a 45-min
     kettlebells session (22 TSS, verified against planned_session_tss) must cost one.
  2. It empties a day the athlete agreed. Six of the seven days in Kathryn's week of
     17 Aug 2026 were pinned in chat; a reserver that ignores pins would have deleted
     one of them, which is the destruction lib/agreed_week exists to prevent.
  3. It spends a key session. The long ride and long run are what close_to_target
     already refuses to spend, so a low-load long run must lose to a higher-load
     ordinary day.
  4. It overrules a recorded reason. The rule's escape hatch is a stated waiver, and a
     week carrying one must be left exactly as proposed.

No LLM, no network, no ICU call, no athlete file: every check drives the pure function
on synthetic proposals.
"""
from __future__ import annotations

import sys
from pathlib import Path

_here = Path(__file__).resolve().parent
BASE = _here.parent
sys.path.insert(0, str(BASE / "lib"))
sys.path.insert(0, str(BASE / "ironman-analysis"))

import rest_day                                              # noqa: E402
from primitives.validate_plan import validate_week, REST_DAYS_MIN   # noqa: E402

MON = "2026-08-17"          # the real week this ticket came from
DAYS = [f"2026-08-{17 + i}" for i in range(7)]      # Mon 17 .. Sun 23

FAILED = []


def check(name, cond):
    print(f"{'PASS' if cond else 'FAIL'}  {name}")
    if not cond:
        FAILED.append(name)


def _week(loads, names=None, pinned=(), sports=None):
    """(proposal, built) for a 7-day week. `loads` is one load per day, keyed by index;
    a load of None means no session at all on that day."""
    prop, bui = {"sessions": []}, {"sessions": []}
    for i, load in enumerate(loads):
        if load is None:
            continue
        s = {"date": DAYS[i], "sport": (sports or {}).get(i, "Run"),
             "name": (names or {}).get(i, f"Session {i}"),
             "segments": [{"minutes": 40, "zone": "easy"}]}
        if DAYS[i] in pinned:
            s["pinned"] = True
        prop["sessions"].append(s)
        bui["sessions"].append({"date": DAYS[i], "sport": s["sport"], "name": s["name"],
                                "load_target": load, "duration_min": 40,
                                "pinned": s.get("pinned", False)})
    return prop, bui


def _events(proposal, built):
    """The proposal as validate_week events, so the reserved week can be graded by the
    same check that would grade it in the builder."""
    keep = {(s["date"], s["name"]) for s in proposal["sessions"]}
    return [{"start_date_local": f"{b['date']}T00:00:00", "type": b["sport"],
             "category": "WORKOUT", "load_target": b["load_target"],
             "name": b["name"], "moving_time": b["duration_min"] * 60}
            for b in built["sessions"] if (b["date"], b["name"]) in keep]


def _rest_codes(evs):
    return [v.code for v in validate_week(evs, __import__("datetime").date(2026, 8, 17)).violations
            if "rest" in v.code]


# ── 1. THE POLICY COUNT COMES FROM ONE PLACE ────────────────────────────────────────
import inspect                                                        # noqa: E402
check("validate_week's rest_days_min default IS the shared constant, not a literal 1",
      inspect.signature(validate_week).parameters["rest_days_min"].default is REST_DAYS_MIN)
check("rest_day reads the policy count from validate_plan, not a local copy",
      rest_day.REST_DAYS_MIN is REST_DAYS_MIN)

# ── 2. A WEEK THAT ALREADY RESTS IS LEFT ALONE ──────────────────────────────────────
prop, bui = _week([40, 50, 60, None, 70, 120, 90])
before = list(prop["sessions"])
p, reserved, notes = rest_day.reserve(prop, bui)
check("a week with an empty day is untouched (no session removed)",
      p["sessions"] == before and reserved == [] and notes == [])

# ── 3. ZERO-LOAD MOBILITY DOES NOT COST THE REST DAY ────────────────────────────────
prop, bui = _week([40, 50, 60, 0, 70, 120, 90],
                  names={3: "Mobility 20min"}, sports={3: "Workout"})
before = list(prop["sessions"])
p, reserved, notes = rest_day.reserve(prop, bui)
check("a zero-load mobility entry counts as rest — nothing is reserved or removed",
      p["sessions"] == before and reserved == [])

# ── 4. A SEVEN-DAY WEEK LOSES ITS CHEAPEST DAY ──────────────────────────────────────
# Kathryn's live week with the Monday strength session left off, so Thursday's 29-load
# "Easy Aerobic Run + VO2" is the cheapest day and sits between a Wednesday threshold
# block and a Friday brick — the choice the ticket describes as the correct one.
prop, bui = _week([80, 72, 92, 29, 154, 158, 197])
p, reserved, notes = rest_day.reserve(prop, bui)
check("the LOWEST-LOAD day is reserved, not the first day of the week",
      reserved == ["2026-08-20"])
check("the reserved day's session is gone from the proposal",
      all(s["date"] != "2026-08-20" for s in p["sessions"]) and len(p["sessions"]) == 6)
check("the reserved week no longer trips no_rest_day",
      _rest_codes(_events(p, bui)) == [])
check("the note names the day and the load freed",
      any("Thu 2026-08-20" in n and "29 load" in n for n in notes))

# ── 5. STRENGTH IS NOT FREE ─────────────────────────────────────────────────────────
# planned_session_tss gives a 45-min kettlebells session 22 TSS, so a Monday holding one
# is a LOADED day. A reserver that treated Strength as costless would call this week
# rested and leave a 7/7 week behind.
prop, bui = _week([22, 72, 92, 60, 154, 158, 197],
                  names={0: "Kettlebells — full body"}, sports={0: "WeightTraining"})
p, reserved, notes = rest_day.reserve(prop, bui)
check("a 22-load strength day counts as loaded, and is reserved as the cheapest day",
      reserved == ["2026-08-17"])

# ── 6. AGREED DAYS ARE NOT THE GENERATOR'S TO CLEAR ─────────────────────────────────
# Kathryn's real week: Mon/Tue/Thu/Fri/Sat/Sun pinned in chat, only Wed free. The
# cheapest day (Thu, 29) is PINNED, so it must not be chosen.
KATHRYN_PINS = {"2026-08-17": {}, "2026-08-18": {}, "2026-08-20": {},
                "2026-08-21": {}, "2026-08-22": {}, "2026-08-23": {}}
prop, bui = _week([22, 72, 92, 29, 154, 158, 197])
p, reserved, notes = rest_day.reserve(prop, bui, KATHRYN_PINS)
check("the cheapest day is skipped when it is pinned — Wed, the only free day, is taken",
      reserved == ["2026-08-19"])
check("no pinned session was removed",
      {s["date"] for s in p["sessions"]} == set(DAYS) - {"2026-08-19"})

# Every loaded day agreed: nothing can be reserved, and that is REPORTED, not resolved.
ALL_PINNED = {d: {} for d in DAYS}
prop, bui = _week([22, 72, 92, 29, 154, 158, 197])
before = list(prop["sessions"])
p, reserved, notes = rest_day.reserve(prop, bui, ALL_PINNED)
check("all seven days agreed -> nothing removed",
      p["sessions"] == before and reserved == [])
check("...and the impossibility is stated as a conversation to have",
      len(notes) == 1 and "could NOT reserve" in notes[0]
      and "not the generator's to clear" in notes[0])

# A session flagged pinned in the PROPOSAL is protected even without a pin record.
prop, bui = _week([22, 72, 92, 29, 154, 158, 197], pinned={"2026-08-20"})
p, reserved, notes = rest_day.reserve(prop, bui)
check("a proposal-level pinned flag also protects the day",
      reserved == ["2026-08-17"])

# ── 7. A REST-DAY PIN ALREADY SATISFIES THE RULE ────────────────────────────────────
# agreed_week records "nothing on Wednesday" as a pin with session None: there is no
# session to reserve and none to remove, and the day is already rest.
prop, bui = _week([22, 72, None, 29, 154, 158, 197])
before = list(prop["sessions"])
p, reserved, notes = rest_day.reserve(prop, bui, {"2026-08-19": {"session": None}})
check("a rest-day pin is counted as rest and the week is left alone",
      p["sessions"] == before and reserved == [] and notes == [])

# ── 8. KEY SESSIONS LOSE LAST ───────────────────────────────────────────────────────
# Sunday's long run is the LOWEST load here (a deload week), but it is a key session, so
# the higher-load ordinary Tuesday must go instead.
prop, bui = _week([40, 30, 92, 60, 154, 158, 25],
                  names={6: "Long Run 104min", 5: "Long Ride 195min"})
def _is_key(s):
    n = (s.get("name") or "").lower()
    return "long run" in n or "long ride" in n
p, reserved, notes = rest_day.reserve(prop, bui, is_key_fn=_is_key)
check("a low-load LONG RUN is protected; the cheapest NON-key day is reserved",
      reserved == ["2026-08-18"])
check("the long run and long ride both survive",
      {s["name"] for s in p["sessions"]} >= {"Long Run 104min", "Long Ride 195min"})

# THE PREDICATE STAGE-1 ACTUALLY PASSES. Built from the same two helpers the shaping
# levers use, so what the reserver protects and what close_to_target refuses to spend
# cannot drift apart. A threshold block is key; a strength session (no segments) is not.
_s1 = __import__("importlib.util", fromlist=["util"])
_spec = _s1.spec_from_file_location("s1", BASE / "scripts" / "stage1-plan.py")
S1 = _s1.module_from_spec(_spec)
_spec.loader.exec_module(S1)
STAGE1_KEY = lambda s: (S1._is_long_ride(s) or S1._is_long_run(s)
                        or (bool(s.get("segments")) and not S1._is_endurance(s)))
check("stage1's predicate calls a threshold block KEY",
      STAGE1_KEY({"sport": "Ride", "name": "Threshold 4x10min",
                  "segments": [{"minutes": 60, "zone": "threshold"}]}))
check("...an easy Z2 ride is NOT key",
      not STAGE1_KEY({"sport": "Ride", "name": "Endurance Spin",
                      "segments": [{"minutes": 60, "zone": "easy"}]}))
check("...a long run is key even when it is all easy",
      STAGE1_KEY({"sport": "Run", "name": "Long Run 104min",
                  "segments": [{"minutes": 104, "zone": "easy"}]}))
check("...a segment-less Strength session is NOT key (cheapest thing to move)",
      not STAGE1_KEY({"sport": "WeightTraining", "name": "Kettlebells", "segments": []}))

# The threshold day is protected against a cheaper ordinary day even though it is
# heavier: Wednesday's 92-load threshold survives, Tuesday's 30-load spin goes.
prop = {"sessions": [
    {"date": DAYS[i], "sport": "Ride", "name": "Endurance Spin",
     "segments": [{"minutes": 60, "zone": "easy"}]} for i in range(7)]}
prop["sessions"][2] = {"date": DAYS[2], "sport": "Ride", "name": "Threshold 4x10min",
                       "segments": [{"minutes": 60, "zone": "threshold"}]}
bui = {"sessions": [{"date": DAYS[i], "sport": "Ride", "name": prop["sessions"][i]["name"],
                     "load_target": (30 if i == 1 else 92 if i == 2 else 80),
                     "duration_min": 60} for i in range(7)]}
p, reserved, notes = rest_day.reserve(prop, bui, is_key_fn=STAGE1_KEY)
check("the quality day survives; the cheapest EASY day is reserved",
      reserved == [DAYS[1]]
      and any(s["name"] == "Threshold 4x10min" for s in p["sessions"]))

# When the quality day is the ONLY unpinned day it is taken anyway — the pin constraint
# is hard and the ranking is only a preference — but that is said out loud.
prop = {"sessions": [
    {"date": DAYS[i], "sport": "Ride", "name": "Endurance Spin", "pinned": i != 2,
     "segments": [{"minutes": 60, "zone": "easy"}]} for i in range(7)]}
prop["sessions"][2] = {"date": DAYS[2], "sport": "Ride", "name": "Threshold 4x10min",
                       "segments": [{"minutes": 60, "zone": "threshold"}]}
bui = {"sessions": [{"date": DAYS[i], "sport": "Ride", "name": prop["sessions"][i]["name"],
                     "load_target": 92, "duration_min": 60} for i in range(7)]}
p, reserved, notes = rest_day.reserve(prop, bui, is_key_fn=STAGE1_KEY)
check("the only free day is taken even though it is a key session",
      reserved == [DAYS[2]])
check("...and losing a key session is stated, with the ask back to the athlete",
      any("carried a KEY session" in n and "which day they would rather rest" in n
          for n in notes))

# A key_fn that raises must not take the build down with it.
prop, bui = _week([22, 72, 92, 29, 154, 158, 197])
p, reserved, notes = rest_day.reserve(prop, bui, is_key_fn=lambda s: 1 / 0)
check("a raising is_key_fn is survived (the cheapest day is still reserved)",
      reserved == ["2026-08-17"])

# ── 9. A MULTI-SESSION DAY COSTS ALL OF IT ──────────────────────────────────────────
# Kathryn's Friday carries a bike and a run. Reserving that day empties both, and the
# CHOICE has to be made on the day's total, not on either session alone.
prop = {"sessions": [
    {"date": DAYS[0], "sport": "Run", "name": "Easy", "segments": []},
    {"date": DAYS[1], "sport": "Ride", "name": "Brick leg 1", "segments": []},
    {"date": DAYS[1], "sport": "Run", "name": "Brick leg 2", "segments": []},
] + [{"date": DAYS[i], "sport": "Ride", "name": f"S{i}", "segments": []} for i in range(2, 7)]}
bui = {"sessions": [
    {"date": DAYS[0], "sport": "Run", "name": "Easy", "load_target": 60, "duration_min": 40},
    {"date": DAYS[1], "sport": "Ride", "name": "Brick leg 1", "load_target": 20, "duration_min": 40},
    {"date": DAYS[1], "sport": "Run", "name": "Brick leg 2", "load_target": 25, "duration_min": 40},
] + [{"date": DAYS[i], "sport": "Ride", "name": f"S{i}", "load_target": 90,
      "duration_min": 40} for i in range(2, 7)]}
p, reserved, notes = rest_day.reserve(prop, bui)
check("a two-session day is judged on its TOTAL (45), so the single 60 survives",
      reserved == [DAYS[1]])
check("...and BOTH of that day's sessions are removed",
      all(s["date"] != DAYS[1] for s in p["sessions"]) and len(p["sessions"]) == 6)

# ── 10. A RECORDED REASON IS NOT OVERRULED ──────────────────────────────────────────
prop, bui = _week([22, 72, 92, 29, 154, 158, 197])
before = list(prop["sessions"])
p, reserved, notes = rest_day.reserve(
    prop, bui, waiver="race week — every day is a 20min opener")
check("a waived week keeps all seven days",
      p["sessions"] == before and reserved == [])
check("...and the waiver reason is quoted into the run output",
      len(notes) == 1 and "20min opener" in notes[0] and "TRAINING THROUGH" in notes[0])
check("a BLANK waiver is not a waiver — the day is still reserved (fails closed)",
      rest_day.reserve(*_week([22, 72, 92, 29, 154, 158, 197]), waiver="   ")[1]
      == ["2026-08-17"])
# The validator agrees: given the same reason it downgrades rather than blocking.
prop, bui = _week([22, 72, 92, 29, 154, 158, 197])
_soft = [v.code for v in validate_week(
    _events(prop, bui), __import__("datetime").date(2026, 8, 17),
    rest_day_waiver="race week").violations if "rest" in v.code]
check("validate_week downgrades the same waived week to soft no_rest_day_waived",
      _soft == ["no_rest_day_waived"])

# ── 11. TWO REST DAYS, AND PARTIAL PROGRESS ─────────────────────────────────────────
prop, bui = _week([22, 72, 92, 29, 154, 158, 197])
p, reserved, notes = rest_day.reserve(prop, bui, rest_days_min=2)
check("rest_days_min=2 reserves the two cheapest days, cheapest first",
      reserved == ["2026-08-17", "2026-08-20"])
# Only one day is free but two are required: reserve what is possible and SAY so, rather
# than either breaking a pin or silently delivering one rest day short.
prop, bui = _week([22, 72, 92, 29, 154, 158, 197])
p, reserved, notes = rest_day.reserve(prop, bui, KATHRYN_PINS, rest_days_min=2)
check("one free day against two required -> reserve the one, report the shortfall",
      reserved == ["2026-08-19"]
      and any("only 1 of the 2 rest days" in n for n in notes))

# ── 12. DISABLED AND DEGENERATE INPUTS ──────────────────────────────────────────────
check("rest_days_min=0 disables reservation entirely",
      rest_day.reserve(*_week([22, 72, 92, 29, 154, 158, 197]), rest_days_min=0)[1] == [])
check("an empty proposal is a no-op, not a crash",
      rest_day.reserve({"sessions": []}, {"sessions": []}) == ({"sessions": []}, [], []))
check("a part-planned week (3 days) needs no reservation",
      rest_day.reserve(*_week([40, 50, 60, None, None, None, None]))[1] == [])
# Determinism: an identical week must reserve the identical day every run, or two
# attempts of one build disagree about which day is rest.
_a = rest_day.reserve(*_week([90, 90, 90, 90, 90, 90, 90]))[1]
_b = rest_day.reserve(*_week([90, 90, 90, 90, 90, 90, 90]))[1]
check("an all-equal week reserves the same (earliest) day deterministically",
      _a == _b == ["2026-08-17"])

# ── 13. THE WEEK WINDOW ─────────────────────────────────────────────────────────────
# The Stage-1 model writes the dates and nothing checks them against the DATE GRID, so a
# session outside the week is a live possibility. Unwindowed, eight loaded dates make the
# rest count NEGATIVE and two days get reserved to fix a week that needed one.
prop, bui = _week([80, 72, 92, 29, 154, 158, 197])
prop["sessions"].append({"date": "2026-08-24", "sport": "Run", "name": "Stray Monday",
                         "segments": [{"minutes": 40, "zone": "easy"}]})
bui["sessions"].append({"date": "2026-08-24", "sport": "Run", "name": "Stray Monday",
                        "load_target": 60, "duration_min": 40})
p, reserved, notes = rest_day.reserve(prop, bui, week_start=MON)
check("a session dated outside the week is ignored, and ONE day is reserved",
      reserved == ["2026-08-20"])
check("...and the stray session is left alone (not this week's to remove)",
      any(s["date"] == "2026-08-24" for s in p["sessions"]))
check("unwindowed, the same input would over-reserve — the window is what prevents it",
      len(rest_day.reserve(*_week([80, 72, 92, 29, 154, 158, 197]),
                           rest_days_min=2)[1]) == 2)
check("loaded_days confines itself to the seven dates it is given",
      set(rest_day.loaded_days(bui, rest_day.week_dates(MON))) == set(DAYS[:7])
      and "2026-08-24" not in rest_day.loaded_days(bui, rest_day.week_dates(MON)))
check("week_dates is the seven days from Monday inclusive",
      rest_day.week_dates(MON) == DAYS and len(DAYS) == 7)

# ── 14. A MISPAIRED BUILD IS A VISIBLE NO-OP ────────────────────────────────────────
# built pairs with proposal BY INDEX. If quality_inject mutates the proposal and then
# raises, the rebuild is skipped and `built` is stale — one session's load would be
# attributed to another and a day chosen for a reason nothing in the output explains.
prop, bui = _week([80, 72, 92, 29, 154, 158, 197])
bui["sessions"].pop()
before = list(prop["sessions"])
p, reserved, notes = rest_day.reserve(prop, bui, week_start=MON)
check("a build that no longer lines up with the proposal reserves NOTHING",
      reserved == [] and p["sessions"] == before)
check("...and says why, rather than choosing a day on mis-attributed load",
      len(notes) == 1 and "cannot be attributed" in notes[0]
      and "Rebuild before reserving" in notes[0])

print()
if FAILED:
    print(f"{len(FAILED)} CHECK(S) FAILED:")
    for f in FAILED:
        print(f"  - {f}")
    sys.exit(1)
print("all rest-day checks passed")
