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

# 1) CONVERSATIONAL weight statements, bounded. An unbounded parse turns a mistyped
#    portion into a weight reading, and a bad weight moves the mean the deficit
#    rides on. This must handle how a person actually types, not a fixed format.
import nutrition_nlu as NLU  # noqa: E402
for phrase in ("83.4", "83.4kg", "weight 83.4", "83.4 kg", "weighed 83.4",
               "I'm 83.4 this morning", "83,4", "scales said 83.4",
               "weighed in at 83.4kg"):
    check(f"weight understood: {phrase!r}", NLU.looks_like_weight(phrase) == 83.4)
check("food text is not a weight", NLU.looks_like_weight("two slices of toast") is None)
check("an implausible number is rejected", NLU.looks_like_weight("750") is None)
check("a portion size is not a weight", NLU.looks_like_weight("200g chicken") is None)
check("a gram quantity is never a weigh-in", NLU.looks_like_weight("75g pack") is None)
check("a number inside a long sentence is not a weigh-in",
      NLU.looks_like_weight("I had about 83 of those little tomatoes with lunch today")
      is None)

# 1b) INTENT is decided before anything is resolved. The first cut sent
#     "how much protein have I had?" to the resolution ladder, which came back with a
#     food item and offered to log it.
def fake_model(reply):
    return lambda *a, **k: type("P", (), {"stdout": reply, "stderr": ""})()


q = NLU.classify("how much protein have I had?", False, "claude", "m",
                 log=lambda *a: None, runner=fake_model('{"intent":"question"}'))
check("a question is a question, not food", q["intent"] == "question")
check("a question falls back to question even if the model fails",
      NLU.classify("how much protein have I had?", False, "claude", "m",
                   log=lambda *a: None,
                   runner=fake_model("garbage"))["intent"] == "question")
check("a lookup question is still a question",
      NLU.classify("whats in a chicken breast?", False, "claude", "m",
                   log=lambda *a: None,
                   runner=fake_model('{"intent":"question"}'))["intent"] == "question")

# Slash commands, yes and no never reach the model at all.
check("a slash command is a fast path",
      NLU.fast_intent("/today", False)["intent"] == "command")
for yes in ("y", "yes", "go on", "log it", "please do"):
    check(f"{yes!r} confirms", NLU.fast_intent(yes, True)["intent"] == "confirm")
for no in ("n", "no", "forget it", "drop it"):
    check(f"{no!r} cancels", NLU.fast_intent(no, True)["intent"] == "cancel")
check("yes with nothing pending is not a confirm",
      NLU.fast_intent("yes", False) is None
      or NLU.fast_intent("yes", False)["intent"] != "confirm")

# 1c) Multiple foods in one sentence are separate items, or each one loses its own
#     provenance and the whole string gets mis-costed as one lookup.
multi = NLU.parse_with_model(
    "porridge with blueberries and a flat white", "claude", "m", log=lambda *a: None,
    runner=fake_model('{"intent":"log_food","items":['
                      '{"text":"porridge","portion_g":60},'
                      '{"text":"blueberries","portion_g":80},'
                      '{"text":"flat white"}]}'))
check("three foods parse as three items", len(multi["items"]) == 3)
check("portions are carried per item", multi["items"][0]["portion_g"] == 60)
check("a missing portion is null, not zero", multi["items"][2]["portion_g"] is None)
check("in-session defaults to false", multi["items"][0]["in_session"] is False)
sess = NLU.parse_with_model("gel on the bike", "claude", "m", log=lambda *a: None,
                            runner=fake_model('{"intent":"log_food","items":'
                                              '[{"text":"gel","in_session":true}]}'))
check("in-session fuel is tagged", sess["items"][0]["in_session"] is True)

# 1d) The model NEVER supplies macros through this path, and junk is never food.
check("an unparseable reply is unknown, never food",
      NLU.parse_with_model("x", "claude", "m", log=lambda *a: None,
                           runner=fake_model("sorry"))["intent"] == "unknown")
check("an invalid intent is rejected",
      NLU.parse_with_model("x", "claude", "m", log=lambda *a: None,
                           runner=fake_model('{"intent":"eat_everything"}'))["intent"]
      == "unknown")
check("a crashed model is unknown, not food",
      NLU.parse_with_model("x", "claude", "m", log=lambda *a: None,
                           runner=lambda *a, **k: (_ for _ in ()).throw(
                               TimeoutError("x")))["intent"] == "unknown")

# 1e) Corrections re-parse from combined text rather than patching the misparse.
combined = NLU.apply_correction("a bag of nuts", "it was the whole 200g bag")
check("correction folds into the original text",
      "bag of nuts" in combined and "200g" in combined)

# 1f) An answer is phrased from injected facts, and a dead model falls back rather
#     than leaving a question unanswered.
ans = NLU.answer_question("how much protein?", {"protein_g": 68}, "claude", "m",
                          log=lambda *a: None, runner=fake_model("You are on 68 g."))
check("an answer comes back phrased", "68" in ans)
check("a dead model returns None so the caller can fall back",
      NLU.answer_question("q", {}, "claude", "m", log=lambda *a: None,
                          runner=lambda *a, **k: (_ for _ in ()).throw(OSError("x")))
      is None)

# 1g) BARCODES. Distinct from weights by digit count, so the two never collide.
for code in ("5000112637922", "01234565", "012345678905"):
    check(f"barcode recognised: {code}", NLU.looks_like_barcode(code) == code)
check("a weight is not a barcode", NLU.looks_like_barcode("83.4") is None)
check("a barcode is not a weight", NLU.looks_like_weight("5000112637922") is None)
bc = NLU.fast_intent("5000112637922", False)
check("a bare barcode goes straight to log_food with no model call",
      bc["intent"] == "log_food" and bc["barcode"] == "5000112637922")

# 1h) PHOTOS take three different paths at three different confidences. A plate is an
#     ESTIMATE, a printed panel is LABEL data, and conflating them would put
#     label-grade confidence on a guess.
ph = NLU.read_photo("/tmp/x.jpg", "claude", "m", log=lambda *a: None,
                    runner=fake_model('{"kind":"barcode","barcode":"5000112637922"}'))
check("a barcode photo is recognised", ph["kind"] == "barcode")
plate = NLU.read_photo("/tmp/x.jpg", "claude", "m", log=lambda *a: None,
                       runner=fake_model('{"kind":"food_plate","items":['
                                         '{"text":"grilled chicken","portion_g":150},'
                                         '{"text":"basmati rice","portion_g":200}]}'))
check("a plate becomes items, not macros", len(plate["items"]) == 2)
check("the plate path supplies NO nutrition figures",
      not any(k in plate for k in ("kcal", "protein_g")))
check("an empty plate is unknown rather than an empty log",
      NLU.read_photo("/tmp/x.jpg", "claude", "m", log=lambda *a: None,
                     runner=fake_model('{"kind":"food_plate","items":[]}'))["kind"]
      == "unknown")
check("an unreadable photo is unknown, never food",
      NLU.read_photo("/tmp/x.jpg", "claude", "m", log=lambda *a: None,
                     runner=fake_model("no idea"))["kind"] == "unknown")

# THE SALT TRAP. UK panels print salt, not sodium, and salt is sodium x 2.5. A silent
# conversion the wrong way is a 150% error that looks entirely plausible.
lab = NLU.read_photo("/tmp/x.jpg", "claude", "m", log=lambda *a: None,
                     runner=fake_model('{"kind":"nutrition_label","per":"100g",'
                                       '"portion_g":200,"kcal":150,"protein_g":10,'
                                       '"carb_g":12,"fat_g":6,"fibre_g":2,'
                                       '"salt_g":1.0,"product":"Test Meal",'
                                       '"ingredients":"chicken, rice, spinach"}'))
check("salt is converted to sodium, not copied",
      lab["dietary_sodium_mg"] == 400 and lab.get("sodium_from_salt") is True)
item = NLU.label_to_item(lab)
check("a per-100g panel scales to the stated portion", item["kcal"] == 300.0)
check("scaled sodium follows the portion", item["dietary_sodium_mg"] == 800)
check("the label carries its ingredients for species tagging",
      "spinach" in item["ingredients"])
per_portion = NLU.label_to_item({"per": "portion", "portion_g": 200, "kcal": 150})
check("a per-portion panel is NOT scaled again", per_portion["kcal"] == 150.0)
absent = NLU.label_to_item({"per": "100g", "portion_g": 100, "kcal": 150})
check("a field the panel did not show stays absent, not zero",
      "fibre_g" not in absent)

# 1i) DEBATE. Options are resolved before the discussion, and nothing is logged.
adv = NLU.parse_with_model("should I have the pasta or the rice tonight?", "claude",
                           "m", log=lambda *a: None,
                           runner=fake_model('{"intent":"advice","options":'
                                             '["pasta","rice"]}'))
check("a decision is advice, not a log", adv["intent"] == "advice")
check("both options are captured", adv["options"] == ["pasta", "rice"])
check("advice yields no items to log", not adv.get("items"))
reply = NLU.advise("pasta or rice?", {"options": []}, "claude", "m",
                   log=lambda *a: None, runner=fake_model("Rice, you are short on carbs."))
check("advice comes back phrased", "Rice" in reply)
check("a dead model returns None so the caller can fall back",
      NLU.advise("q", {}, "claude", "m", log=lambda *a: None,
                 runner=lambda *a, **k: (_ for _ in ()).throw(OSError("x"))) is None)

# 1j) A DOSE FORM is a supplement, not a food. "had 400mg of my protein collagen
#     capsules. (1 pill)" was classified as food, name-searched, and came back as soy
#     protein isolate with a plant species attached.
msg = "had 400mg of my protein collagen capsules. (1 pill) This morning"
check("capsules are detected as a dose form", NLU.looks_like_supplement(msg) is True)
check("the mg dose is parsed", NLU.tiny_dose_mg(msg) == 400.0)
check("a gram dose is not treated as a mg dose",
      NLU.tiny_dose_mg("15g collagen powder") is None)
sup = NLU.classify(msg, False, "c", "m", log=lambda *a: None,
                   runner=fake_model('{"intent":"log_food","items":'
                                     '[{"text":"collagen capsules"}]}'))
check("a dose form overrides a log_food classification",
      sup["intent"] == "log_supplement")
check("the override is recorded", sup.get("form_detected") is True)
check("400 mg is flagged nutritionally trivial",
      sup.get("nutritionally_trivial") is True)
big = NLU.classify("had 15g of collagen powder", False, "c", "m", log=lambda *a: None,
                   runner=fake_model('{"intent":"log_food","items":'
                                     '[{"text":"collagen powder","portion_g":15}]}'))
check("a real 15 g dose is not flagged trivial",
      not big.get("nutritionally_trivial"))
check("food is still food", NLU.classify(
    "porridge with blueberries", False, "c", "m", log=lambda *a: None,
    runner=fake_model('{"intent":"log_food","items":[{"text":"porridge"}]}')
)["intent"] == "log_food")

# 1k) A SUPPLEMENT NEVER TOUCHES A FOOD DATABASE. The intent was routed correctly but
#     the item still went through the resolution ladder, so "400mg of my protein collagen
#     capsules" name-matched "COLLAGEN PROTEIN BAR, LEMON COOKIE" and picked up 4 plant
#     species from that bar's ingredient list.
check("a dose form conflicts with a food form",
      NR._relevant("400mg of my protein collagen capsules",
                   "COLLAGEN PROTEIN BAR, LEMON COOKIE") is False)
check("but a genuine collagen supplement still matches",
      NR._relevant("collagen capsules", "Collagen peptides, bovine") is True)
check("a real protein bar can still match a protein bar",
      NR._relevant("protein bar", "COLLAGEN PROTEIN BAR, LEMON COOKIE") is True)
check("tablets do not match a fortified cereal",
      NR._relevant("vitamin d tablets", "Vitamin D Fortified Cereal") is False)
check("a powder is not a dose-form conflict",
      NR._relevant("magnesium capsules", "Magnesium citrate powder") is True)

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
