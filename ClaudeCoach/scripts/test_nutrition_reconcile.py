#!/usr/bin/env python3
"""Offline tests for lib/nutrition_reconcile.py. Tmpdir only, never a real athlete dir.
Run: python3 ClaudeCoach/scripts/test_nutrition_reconcile.py

Both failure modes here are silent, which is why they get heavy cover: fuel counted
twice inflates the day, and fuel counted zero times fires the under-fuelling guard
falsely and starves the coach's g/hr ramp.
"""
import json
import sys
import tempfile
from datetime import date
from pathlib import Path

_here = Path(__file__).resolve().parent
for cand in (_here / "lib", _here.parent / "lib"):
    if (cand / "nutrition_reconcile.py").exists():
        sys.path.insert(0, str(cand))
        break
import nutrition_reconcile as RC  # noqa: E402
import nutrition_store as S  # noqa: E402

FAILED = []


def check(name, cond):
    print(("PASS" if cond else "FAIL"), name)
    if not cond:
        FAILED.append(name)


TODAY = date(2026, 8, 10)


def fresh(session_rows=None):
    d = Path(tempfile.mkdtemp(prefix="nut-rc-"))
    if session_rows is not None:
        (d / "session-log.json").write_text(json.dumps(session_rows, indent=1))
    return d, S.NutritionStore(d)


RIDE = {"activity_id": "i1", "date": TODAY.isoformat(), "name": "Long ride",
        "sport": "Ride", "duration_min": 182, "nutrition_g_carb": 200,
        "nutrition_mg_sodium": 1500, "hydration_ml": 2250}
SWIM = {"activity_id": "i2", "date": TODAY.isoformat(), "name": "Swim",
        "sport": "Swim", "duration_min": 45}


def add_food(store, **kw):
    base = dict(raw_text="x", kcal=0, confidence="label", source_rung="cofid")
    base.update(kw)
    return store.add_entry(TODAY, **base)


# 1) Coach owns the day when the nutrition bot has no in-session entries.
d, st = fresh([RIDE, SWIM])
add_food(st, raw_text="porridge", kcal=400, carb_g=60, protein_g=13)
rec = RC.reconcile(st, d, TODAY)
check("session-log owns a day with no bot in-session entries",
      rec["owner"] == RC.OWNER_SESSION_LOG)
check("the coach's carbs are the authoritative figure", rec["fuel"]["carb_g"] == 200)
check("energy is flagged as derived from carbs", rec["energy_is_derived"] is True)
check("derived energy is carbs x 4", rec["fuel"]["kcal"] == 800.0)
check("hydration comes through", rec["fuel"]["hydration_ml"] == 2250)
check("the sessions are named", rec["sessions"][0]["name"] == "Long ride")

# 2) The ride is FOLDED IN, so the day is not short by 800 kcal. That shortfall is what
#    fired the under-fuelling guard falsely.
m = RC.merged_totals(st, d, TODAY)
check(f"coach fuel is folded into the day's energy (got {m['kcal']})", m["kcal"] == 1200.0)
check("and into carbs", m["carb_g"] == 260.0)
check("and is isolated as in-session so nothing can propose trimming it",
      m["in_session_carb_g"] == 200.0)
check("the merge says the fuel came from the coach", m["in_session_from_coach"] is True)
check("sodium folds in too", m["dietary_sodium_mg"] == 1500)

# 3) Nutrition bot owns the day as soon as it has one in-session entry, and the coach's
#    figure is NOT added on top.
d2, st2 = fresh([RIDE, SWIM])
add_food(st2, raw_text="porridge", kcal=400, carb_g=60)
add_food(st2, raw_text="Maurten 320", kcal=320, carb_g=80, in_session=True)
add_food(st2, raw_text="PF30 chew", kcal=120, carb_g=30, in_session=True)
rec2 = RC.reconcile(st2, d2, TODAY)
check("nutrition bot owns the day once it has in-session entries",
      rec2["owner"] == RC.OWNER_NUTRITION)
check("its own itemised total is authoritative", rec2["fuel"]["carb_g"] == 110.0)
check("the coach's figure is reported, not added", rec2["other_side"]["carb_g"] == 200)
check("a disagreement is surfaced", rec2["disagrees"] is True)
m2 = RC.merged_totals(st2, d2, TODAY)
check(f"NO double count: 400+320+120 = 840 (got {m2['kcal']})", m2["kcal"] == 840.0)
check("carbs are not double counted", m2["carb_g"] == 170.0)
check("energy is not derived when the bot owns it",
      m2.get("energy_is_derived") is not True)

# 4) None is not zero. An unlogged ride and a zero-fuel ride are different facts, and
#    conflating them would make the g/hr ramp read a gap as a real zero.
d3, st3 = fresh([SWIM])
rec3 = RC.reconcile(st3, d3, TODAY)
check("a day with no fuel logged anywhere reports None, not 0",
      rec3["fuel"]["carb_g"] is None)
check("no fuel means no disagreement", rec3["disagrees"] is False)
check("merged totals on an empty day stay empty",
      RC.merged_totals(st3, d3, TODAY)["kcal"] == 0)
d4, st4 = fresh(None)
check("a missing session log is survivable",
      RC.reconcile(st4, d4, TODAY)["owner"] == RC.OWNER_SESSION_LOG)
(d4 / "session-log.json").write_text("{not json")
check("a corrupt session log is survivable",
      RC.session_fuel_for_day(d4, TODAY)["carb_g"] is None)

# 5) Write-back keeps the coach's fuelling ramp fed. It must land on the LONGEST
#    session, because g/hr is per session and spreading a ride's fuel onto a swim
#    would understate the ride's rate.
d5, st5 = fresh([dict(SWIM), {**RIDE, "nutrition_g_carb": None,
                              "nutrition_mg_sodium": None}])
res = RC.write_back(d5, TODAY, carb_g=110, sodium_mg=900, log=lambda *a: None)
check("write-back reports success", res["written"] is True)
rows = json.loads((d5 / "session-log.json").read_text())
ride = [r for r in rows if r["activity_id"] == "i1"][0]
swim = [r for r in rows if r["activity_id"] == "i2"][0]
check("the carb total lands on the ride, not the swim",
      ride["nutrition_g_carb"] == 110 and swim.get("nutrition_g_carb") is None)
check("sodium lands with it", ride["nutrition_mg_sodium"] == 900)
check("the source is recorded so provenance is not lost",
      ride["nutrition_source"] == "nutrition_bot")
check("other fields on the session are untouched",
      ride["duration_min"] == 182 and ride["hydration_ml"] == 2250)

# 6) Write-back must never invent a session, and must not blow up when the activity has
#    not synced yet. The caller retries on the next log.
d6, st6 = fresh([])
check("no session for that day is a soft no",
      RC.write_back(d6, TODAY, carb_g=100, log=lambda *a: None)["written"] is False)
check("nothing to write is a soft no",
      RC.write_back(d5, TODAY, log=lambda *a: None)["written"] is False)

# 7) The dict-wrapped session-log shape is handled as well as the bare list, since the
#    file's shape is not guaranteed across athletes.
d7 = Path(tempfile.mkdtemp(prefix="nut-rc7-"))
(d7 / "session-log.json").write_text(json.dumps({"sessions": [RIDE]}))
check("a {sessions: [...]} log is read", RC.session_fuel_for_day(d7, TODAY)["carb_g"] == 200)
RC.write_back(d7, TODAY, carb_g=55, log=lambda *a: None)
check("and written back in place, preserving the wrapper",
      json.loads((d7 / "session-log.json").read_text())["sessions"][0]["nutrition_g_carb"]
      == 55)

# 8) Multiple fuelled sessions in one day sum on the coach side.
d8 = Path(tempfile.mkdtemp(prefix="nut-rc8-"))
(d8 / "session-log.json").write_text(json.dumps([
    RIDE, {**SWIM, "nutrition_g_carb": 30}]))
check("two fuelled sessions sum", RC.session_fuel_for_day(d8, TODAY)["carb_g"] == 230.0)

print()
if FAILED:
    print(f"{len(FAILED)} FAILED: " + ", ".join(FAILED))
    sys.exit(1)
print("all checks passed")
