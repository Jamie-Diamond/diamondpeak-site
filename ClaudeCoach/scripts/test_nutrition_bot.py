#!/usr/bin/env python3
"""Offline tests for telegram/nutrition_bot.py's pure logic. No network, no Telegram.
Run: python3 ClaudeCoach/scripts/test_nutrition_bot.py

The reply formatting is where the spec's safety rules either hold or quietly break:
a ceiling rendered as a progress bar reads compliance as failure, and an estimate
rendered like label data corrupts trust in the whole record. Those are tested here.
"""
import importlib.util
import sys
import tempfile
from datetime import date
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE / "lib"))
spec = importlib.util.spec_from_file_location("nb", BASE / "telegram" / "nutrition_bot.py")
NB = importlib.util.module_from_spec(spec)
spec.loader.exec_module(NB)

import nutrition_engine as NE  # noqa: E402
import nutrition_resolve as NR  # noqa: E402
import nutrition_store as S  # noqa: E402
import plants as PL  # noqa: E402

FAILED = []


def check(name, cond):
    print(("PASS" if cond else "FAIL"), name)
    if not cond:
        FAILED.append(name)


TODAY = date(2026, 8, 10)
W = 83.3
RMR = NE.mifflin_st_jeor(W, 1.86, date(1995, 5, 6), "M", on=TODAY)
RIDE = [{"type": "Ride", "moving_time": 7200, "calories": 1600, "average_watts": 210}]

Z_STD = NE.zones(day_type="standard", rolling_weight=W, rmr=RMR, sessions=RIDE,
                 deficit_enabled=True)
Z_PRELONG = NE.zones(day_type="recovery", rolling_weight=W, rmr=RMR,
                     tomorrow_type="long_ride", deficit_enabled=True)

# 1) Weight parsing is bounded. An unbounded parse turns a mistyped food quantity
#    into a weight reading, and a bad weight moves the mean the deficit rides on.
check("a bare number parses as weight", NB.parse_weight("83.4") == 83.4)
check("kg suffix parses", NB.parse_weight("83.4kg") == 83.4)
check("the word weight parses", NB.parse_weight("weight 83.4") == 83.4)
check("food text is not a weight", NB.parse_weight("two slices of toast") is None)
check("an implausible number is rejected", NB.parse_weight("750") is None)
check("a portion size is rejected as a weight", NB.parse_weight("30") is None)

# 2) A CEILING must never render as consumed/target. This is the misreading spec
#    4.1 warns about: a bar reading low against a ceiling looks like failure when
#    it is compliance.
line = NB.fmt_zone("Fibre", 8, Z_PRELONG["fibre_g"])
check(f"a ceiling renders as a ceiling (got {line!r})", "ceiling" in line)
check("under a ceiling reads ok, not short", "ok" in line and "short" not in line)
check("a ceiling line has no slash-target framing", "/" not in line)
over = NB.fmt_zone("Fibre", 35, Z_PRELONG["fibre_g"])
check("above a ceiling reads over", "over" in over)

# 3) A FLOOR never flags high. Exceeding the protein floor is not an event.
plenty = NB.fmt_zone("P", 240, Z_STD["protein_g"])
check(f"well over the protein floor still reads ok (got {plenty!r})", "ok" in plenty)
check("over a floor is never called over", "over" not in plenty)
short = NB.fmt_zone("P", 100, Z_STD["protein_g"])
check("below a floor states how far short", "short" in short)

# 4) The totals block: collagen is visible but excluded, and says so.
totals = {"kcal": 1200, "protein_g": 68, "carb_g": 210, "fat_g": 62, "fibre_g": 14,
          "non_counting_protein_g": 15, "dietary_sodium_mg": 1800}
block = NB.fmt_totals(totals, Z_STD)
check("totals show energy against the target", "1,200" in block)
check("collagen is shown separately and labelled not counted",
      "collagen" in block and "not counted" in block)
check("sodium is shown with no target", "no target" in block)
check("no grade, score or streak appears anywhere",
      not any(w in block.lower() for w in ("streak", "grade", "score", "failed")))

# 5) Flags: at most one line in chat, and the wording matches the direction.
flags = [{"macro": "protein_g", "direction": "cannot_reach_floor", "distance": 40.0},
         {"macro": "fat_g", "direction": "exceeds_ceiling", "distance": 12.0}]
fl = NB.fmt_flags(flags)
check("only one flag reaches chat", fl.count("\n_") == 1)
check("a floor flag says short of its floor", "short of its floor" in fl)
check("no flags means no flag line", NB.fmt_flags([]) == "")
check("a ceiling flag says over its ceiling",
      "over its ceiling" in NB.fmt_flags([flags[1]]))

# 6) The confirm message ALWAYS states the rung, and an estimate says so.
est = {"resolved_name": "mixed nuts", "kcal": 250, "protein_g": 8, "carb_g": 8,
       "fat_g": 19, "confidence": "estimate", "source_rung": NR.Rung.LLM,
       "species": ["prunus_dulcis"], "attempts": [], "degraded": False}
c = NB.fmt_confirm(est)
check("an estimate is labelled estimated", "estimated" in c)
check("an estimate states its uncertainty", "10-15%" in c)
check("an estimate never claims a listing", "listing" not in c)
lab = dict(est, confidence="label", source_rung=NR.Rung.COFID)
cl = NB.fmt_confirm(lab)
check("a CoFID resolution cites the dataset", "CoFID" in cl)
check("label data does not carry an estimate caveat", "10-15%" not in cl)
deg = dict(est, degraded=True,
           attempts=[{"rung": "retailer", "outcome": "error", "detail": "x"}])
check("a degraded resolution admits a better source failed",
      "failed" in NB.fmt_confirm(deg))
need = {"resolved_name": "mystery", "needs_input": True, "confidence": "estimate",
        "source_rung": NR.Rung.LLM, "attempts": [], "degraded": False}
cn = NB.fmt_confirm(need)
check("an unresolvable item asks rather than reporting numbers", "?" in cn)
check("an unresolvable item quotes no macros", "kcal" not in cn)

# 7) /target explains where each number comes from, including which are reasoned.
tgt = NB.fmt_target(Z_STD)
check("target names the day type", "standard" in tgt)
check("target states the basis of each zone", tgt.count("floor") + tgt.count("ceiling")
      + tgt.count("band") >= 4)
check("target distinguishes sourced from practice on fat",
      "sourced" in tgt or "practice" in tgt)
check("target reports the deficit when one applies", "deficit" in tgt)
low = NE.zones(day_type="recovery", rolling_weight=W, rmr=RMR,
               day_confidence="low_confidence")
check("a guessed day type says it was guessed", "guessed" in NB.fmt_target(low))

# 8) /plants is a variety prompt, never a score.
table = PL.SpeciesTable()
days = [{"date": TODAY.isoformat(),
         "entries": [{"resolved_name": "oats, blueberries, almonds, kale, ginger"}]}]
pl = NB.fmt_plants(PL.diversity(days, table, on=TODAY))
check("plants states the 7-day count", "plants" in pl)
check("plants states the evidence for 30", "not a threshold" in pl)
check("plants has no streak or score",
      not any(w in pl.lower() for w in ("streak", "score", "grade", "%")))

# 9) Pending confirmations survive a restart, because the watchdog restarts this
#    process and an in-memory pending item would leave a "yes" answering a question
#    the bot had forgotten.
store = S.NutritionStore(Path(tempfile.mkdtemp(prefix="nb-")))
check("no pending item initially", NB.get_pending(store) is None)
NB.set_pending(store, {"resolved_name": "x", "kcal": 100})
check("pending item persists to disk", NB.pending_path(store).exists())
check("pending item reads back", NB.get_pending(store)["resolved_name"] == "x")
NB.clear_pending(store)
check("pending item clears", NB.get_pending(store) is None)
check("clearing twice is safe", NB.clear_pending(store) is None)

# 10) The ladder wiring: absent keys leave rungs not_configured rather than failing,
#     and the retailer rung is deliberately never wired here.
fetchers = NB.build_fetchers({})
status = NR.ladder_status(fetchers, NR.CofidTable())
check("OFF is wired with no key needed", status[NR.Rung.OFF] == "ready")
check("the LLM rung is wired", status[NR.Rung.LLM] == "ready")
check("USDA without a key is not_configured", status[NR.Rung.USDA] == "not_configured")
check("Nutritionix without keys is not_configured",
      status[NR.Rung.NUTRITIONIX] == "not_configured")
check("the retailer rung is still not built", status[NR.Rung.RETAILER] == "not_configured")
keyed = NB.build_fetchers({"fdc_api_key": "k", "nutritionix_app_id": "a",
                           "nutritionix_app_key": "b"})
check("a supplied FDC key wires USDA", NR.Rung.USDA in keyed)
check("supplied Nutritionix keys wire that rung", NR.Rung.NUTRITIONIX in keyed)

# 11) The LLM rung refuses junk rather than inventing numbers.
fetch = NB.make_llm_fetch(log=lambda *a: None)
NB.subprocess = type("S", (), {"run": staticmethod(
    lambda *a, **k: type("P", (), {"stdout": "sorry I cannot help", "stderr": ""})())})
check("unparseable model output is a miss, not a zero", fetch("thing") is None)
NB.subprocess = type("S", (), {"run": staticmethod(
    lambda *a, **k: type("P", (), {"stdout": '{"kcal": null}', "stderr": ""})())})
check("a model reply with no kcal is a miss", fetch("thing") is None)
NB.subprocess = type("S", (), {"run": staticmethod(
    lambda *a, **k: type("P", (), {
        "stdout": 'here you go {"resolved_name":"toast","kcal":160} thanks',
        "stderr": ""})())})
got = fetch("toast")
check("JSON is extracted from surrounding prose", got and got["kcal"] == 160)

print()
if FAILED:
    print(f"{len(FAILED)} FAILED: " + ", ".join(FAILED))
    sys.exit(1)
print("all checks passed")
