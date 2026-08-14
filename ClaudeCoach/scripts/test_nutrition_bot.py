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
from datetime import date, timedelta
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

# 1l) AN ORDER SCREENSHOT is a fourth photo kind. A real Deliveroo/Wagamama screenshot
#     sent on 10 Aug had no path, so it was forced into "plate" and the model returned a
#     MODIFIER as the item: "(meal is with double salmon and brown rice)", which then
#     matched raw brown rice in the composition tables.
order = NLU.read_photo("/tmp/x.jpg", "c", "m", log=lambda *a: None,
                       runner=fake_model('{"kind":"order","vendor":"Wagamama",'
                                         '"stated_item_count":5,"items":['
                                         '{"text":"new! gochujang salmon rice bowl with '
                                         'brown rice and extra salmon"},'
                                         '{"text":"edamame with chilli + garlic salt (vg)"},'
                                         '{"text":"soy sauce (vg)"},'
                                         '{"text":"(meal is with double salmon and brown rice)"}]}'))
check("an order screenshot is its own kind", order["kind"] == "order")
check("the vendor is captured", order["vendor"] == "Wagamama")
texts = [i["text"] for i in order["items"]]
check("a parenthetical modifier is never an item",
      not any(t.startswith("(") for t in texts))
check("modifiers stay folded into the dish they belong to",
      any("brown rice and extra salmon" in t for t in texts))
check("marketing and dietary markers are stripped",
      not any("new!" in t or "(vg)" in t for t in texts))
check("no stray punctuation is left where a marker was",
      not any(t.startswith(("!", ",", "-", ".")) for t in texts))
check("the vendor is appended so a dish name is searchable",
      all("Wagamama" in t for t in texts))
check("condiments are kept, because soy sauce is real sodium",
      any("soy sauce" in t for t in texts))

# "5 items" counts UNITS, not lines. Jamie's real order was 3 lines and 5 units, because
# one line was 3x soy sauce. Comparing the stated count to the LINE count would have called
# a complete screenshot cropped, which is crying wolf on correct input.
real = NLU.read_photo("/tmp/x.jpg", "c", "m", log=lambda *a: None,
                      runner=fake_model('{"kind":"order","vendor":"Wagamama",'
                                        '"stated_item_count":5,"items":['
                                        '{"text":"gochujang salmon rice bowl","qty":1},'
                                        '{"text":"edamame","qty":1},'
                                        '{"text":"soy sauce","qty":3}]}'))
check("units are summed from the quantities", real["units_seen"] == 5)
check("3 lines and 5 units is NOT treated as cropped",
      real["units_seen"] >= real["stated_item_count"])
check("a quantity is kept on the item rather than repeated in the reading",
      [i["qty"] for i in real["items"]] == [1, 1, 3])
check("a missing qty defaults to 1", NLU.read_photo(
    "/tmp/x.jpg", "c", "m", log=lambda *a: None,
    runner=fake_model('{"kind":"order","items":[{"text":"one thing"}]}')
)["units_seen"] == 1)

# A genuinely cropped screenshot must still be declared.
crop = NLU.read_photo("/tmp/x.jpg", "c", "m", log=lambda *a: None,
                      runner=fake_model('{"kind":"order","vendor":"Wagamama",'
                                        '"stated_item_count":5,"items":['
                                        '{"text":"salmon rice bowl","qty":1},'
                                        '{"text":"edamame","qty":1}]}'))
check("a real crop is still detectable", crop["units_seen"] < crop["stated_item_count"])

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

# 10) The pruned ladder. Every rung has to beat "the LLM just googling it" (Jamie,
#     10 Aug 2026); the name-search databases lost, so they are off the default path and
#     opt-in per athlete. They remain implemented and tested, because they lost on merit
#     rather than being broken.
fetchers = NB.build_fetchers({})
status = NR.ladder_status(fetchers, NR.CofidTable())
check("the web rung is wired by default", status[NR.Rung.WEB] == "ready")
check("the bare LLM estimate is wired by default", status[NR.Rung.LLM] == "ready")
check("CoFID is ready from the local table", status[NR.Rung.COFID] == "ready")
for rung in (NR.Rung.RETAILER, NR.Rung.USDA, NR.Rung.OFF, NR.Rung.NUTRITIONIX):
    check(f"{rung} is off by default", status[rung] == "off_by_default")
check("so the default walk is cofid, web, llm",
      NR.effective_ladder(fetchers) == (NR.Rung.COFID, NR.Rung.WEB, NR.Rung.LLM))

# An FDC key alone does nothing now: the flag is what enables the rung, so a key left in
# config from before cannot quietly put a losing rung back in the path.
keyed = NB.build_fetchers({"fdc_api_key": "k", "nutritionix_app_id": "a",
                           "nutritionix_app_key": "b"})
check("a key without the flag does NOT wire USDA", NR.Rung.USDA not in keyed)
opted = NB.build_fetchers({"enable_name_databases": True, "fdc_api_key": "k",
                           "nutritionix_app_id": "a", "nutritionix_app_key": "b"})
check("the flag plus a key wires USDA", NR.Rung.USDA in opted)
check("the flag wires Open Food Facts name search", NR.Rung.OFF in opted)
check("the flag plus keys wires Nutritionix", NR.Rung.NUTRITIONIX in opted)
check("and they join the walk in preference order",
      NR.effective_ladder(opted).index(NR.Rung.USDA)
      < NR.effective_ladder(opted).index(NR.Rung.WEB))

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

print("\n--- naming which meal something was ---")
# "That was breakfast" was answered as an unrecognised message, because meals existed only
# as a clock inference and nothing could be told otherwise.
for text, meal, item in (
        ("That was breakfast", "breakfast", ""),
        ("that was breakfast.", "breakfast", ""),
        ("the overnight oats was breakfast", "breakfast", "overnight oats"),
        ("put the twix as snacks", "snacks", "twix"),
        ("log it as dinner", "dinner", ""),
        ("make that a snack", "snacks", ""),
        ("call that dinner", "dinner", ""),
        # Words people use that are not one of the four buckets.
        ("the oats was brunch", "breakfast", "oats"),
        ("that was supper", "dinner", ""),
        ("it was tea", "dinner", "")):
    got = NLU.looks_like_meal_tag(text)
    check(f"{text!r} is a meal tag",
          got and got["meal"] == meal and got["item"] == item)
    fi = NLU.fast_intent(text, False)
    check(f"and routes without a model call", (fi or {}).get("intent") == "set_meal")

# The negatives matter more: a false positive files food under the wrong meal silently.
for text in ("that was lovely", "that was 200g", "it was a long run",
             "how much protein have i had", "chicken and rice", "that was hard work",
             "breakfast was good"):
    check(f"{text!r} is NOT a meal tag", NLU.looks_like_meal_tag(text) is None)
check("a meal tag never fires while something is pending, where yes/no is the answer",
      (NLU.fast_intent("that was breakfast", True) or {}).get("intent") != "set_meal")

print("\n--- in-session needs evidence in the words ---")
# The model flagged "M&s overnight oats salted caramel" as in-session with no session
# mentioned. That put 37 g of breakfast into session-log as in-run carbohydrate and fed the
# g/hr ramp the coach prescribes from, which is the one thing separating in-run from
# out-of-run was meant to prevent.
for text in ("gel during the ride", "took a gel mid-run", "two bottles on the bike",
             "90g per hour on the long one", "a gel every 20 min", "in-session drink mix",
             "energy drink whilst running", "gel on the move"):
    check(f"{text!r} is evidence of in-session", NLU.during_session_evidence(text))
for text in ("M&s overnight oats salted caramel", "Finished swim and having a protein bar",
             "M&S Cookies and Cream Protein Bar", "chicken and rice for dinner",
             "a gel", "post ride recovery shake", "before the run i had toast"):
    check(f"{text!r} is NOT", not NLU.during_session_evidence(text))
check("a post-session note is not a session",
      not NLU.during_session_evidence("after the ride i had a shake"))

print("\n--- and he can move one either way afterwards ---")
for text, want in (("that was during the run", True), ("that was on the bike", True),
                   ("that was mid-ride", True), ("that was in-session", True),
                   ("that was after the run", False), ("that was before the ride", False),
                   ("that was out of session", False)):
    check(f"{text!r} -> in_session={want}", NLU.looks_like_session_tag(text) is want)
for text in ("that was breakfast", "that was lovely", "chicken and rice"):
    check(f"{text!r} is not a session tag", NLU.looks_like_session_tag(text) is None)

print("\n--- TODAY's sessions reach facts, not only tomorrow's ---")
# THE BUG THIS EXISTS FOR, 13 Aug 2026. The bot told him "no run showing today or
# tomorrow, just tomorrow's 60-minute ride" while a 240-minute ride sat on TODAY's
# calendar, because facts_for_question injected tomorrow_brief() and nothing about
# today. today_brief mirrors it for the day in progress.
class FakeCtxToday:
    """Only what today_brief touches: zones_for as the side-effecting cache populator,
    exactly the shape zones_for leaves on a real Context."""
    def __init__(self, sessions):
        self._today_sessions = sessions

    def zones_for(self, day):
        return {}


ride_today = [{"type": "Ride", "name": "Long steady ride", "moving_time": 14400,
              "icu_training_load": 180, "description": "steady endurance, Z2",
              "_done": False}]
tb = NB.today_brief(FakeCtxToday(ride_today), TODAY)
check("today_brief reports today's date", tb["date"] == TODAY.isoformat())
check("today_brief carries the session's duration", tb["sessions"][0]["minutes"] == 240)
check("today_brief carries the coach's aim line",
      tb["sessions"][0]["aim"] == "steady endurance, Z2")
check("a still-to-come session is not marked done", tb["sessions"][0]["done"] is False)
check("total_minutes sums the day's sessions", tb["total_minutes"] == 240)

completed_swim = [{"id": "a1", "type": "Swim", "name": "Morning swim",
                   "moving_time": 2400, "_done": True}]
tb2 = NB.today_brief(FakeCtxToday(completed_swim), TODAY)
check("a completed activity is marked done", tb2["sessions"][0]["done"] is True)

empty = NB.today_brief(FakeCtxToday([]), TODAY)
check("an empty calendar reports no sessions and no total",
      empty["sessions"] == [] and empty["total_minutes"] is None)

# facts_for_question is the wiring point: without "today_sessions" in what it returns,
# today_brief exists but the chat model never sees it, which is exactly how this bug
# reached production despite tomorrow_brief working correctly.
import inspect
check("facts_for_question wires today_brief into the facts dict",
      'today_brief(ctx, day)' in inspect.getsource(NB.facts_for_question)
      and '"today_sessions"' in inspect.getsource(NB.facts_for_question))

print("\n--- logging exchanges reach the chat store ---")
# Food-logging never reached the chat store, so the chat model was blind to arguments
# the athlete had just had about what he ate.
store2 = S.NutritionStore(Path(tempfile.mkdtemp(prefix="nb-chat-")))
batch_one = [{"resolved_name": "Rye bread", "kcal": 83, "_raw": "rye bread"}]
NB.set_pending(store2, {"batch": batch_one})
store2.append_chat("coach", NB._offer_summary(batch_one))
chat = store2.recent_chat()
check("an offered single item names it and its kcal",
      "Rye bread" in chat[-1]["text"] and "83 kcal" in chat[-1]["text"]
      and "awaiting confirm" in chat[-1]["text"])
check("the offer summary line stays short enough not to bloat recent_chat()",
      len(chat[-1]["text"]) < 90)

batch_many = [{"resolved_name": "Toast", "kcal": 150}, {"resolved_name": "Eggs", "kcal": 140}]
check("an offered batch of several is summarised by count, not spelled out",
      NB._offer_summary(batch_many) == "[log] offered 2 items — awaiting confirm")

print("\n--- reading what he says it was NOT ---")
# "butter" came back as "Peanut butter, smooth" six times on 12 Aug 2026, twice AFTER "I
# never said peanut butter". Read with regexes rather than by asking the model: this runs on
# the message that is already the second complaint, so it cannot depend on a model call.
for text, want in (
        ("I never said peanut butter", "peanut butter"),
        ("i never said peanut butter!", "peanut butter"),
        ("not peanut butter", "peanut butter"),
        ("not the peanut butter", "peanut butter"),
        ("it was not peanut butter, it was just butter", "peanut butter"),
        ("it wasn't peanut butter at all", "peanut butter"),
        ("that isn't peanut butter", "peanut butter"),
        ("remove the peanut butter", "peanut butter"),
        ("never had peanut butter today", "peanut butter"),
        ("I never mentioned peanut butter", "peanut butter")):
    got = NB.exclusions_in(text)
    check(f"{text[:38]!r} rules out peanut butter", want in got)
check("the phrase is cut at four words, not the rest of the sentence",
      all(len(p.split()) <= 4 for p in
          NB.exclusions_in("it was not peanut butter and I have told you this twice now")))
check("a correction that names nothing rules out nothing",
      NB.exclusions_in("half the portion") == []
      and NB.exclusions_in("actually it was 200g") == [])
check("an ordinary log is not read as a rejection",
      NB.exclusions_in("porridge with blueberries and a flat white") == [])
# The exclusion has to survive as tokens, so it blocks the row it names and not its
# neighbour: rejecting peanut butter must not block the butter he actually ate.
check("the stored phrase blocks the row he rejected",
      NR._excluded_by("Peanut butter, smooth", NB.exclusions_in("not peanut butter"))
      == "peanut butter")
check("and leaves the one he ate alone",
      NR._excluded_by("Butter, salted", NB.exclusions_in("not peanut butter")) == "")

print("\n--- an assumed portion is stated, never silent ---")
assumed = {"resolved_name": "Butter, salted", "kcal": 37, "protein_g": 0, "carb_g": 0,
           "fat_g": 4, "confidence": "label", "source_rung": NR.Rung.WEB,
           "species": [], "attempts": [], "degraded": False,
           "portion_estimated": True, "portion_assumed": "5 g - a teaspoon"}
ca = NB.fmt_confirm(assumed)
check("the offer says what portion was assumed", "assumed 5 g - a teaspoon" in ca)
check("and invites the correction", "correct me if wrong" in ca)
check("a measured portion says nothing about assumptions",
      "assumed" not in NB.fmt_confirm(dict(assumed, portion_estimated=False,
                                           portion_assumed=None)))

print()
if FAILED:
    print(f"{len(FAILED)} FAILED: " + ", ".join(FAILED))
    sys.exit(1)
print("all checks passed")


# --- quantity corrections are arithmetic, never a search (13 Aug 2026) ----------------

qc = NB.quantity_correction("That's 100g I had 160g")
check("two numbers: the amount EATEN wins", qc == {"grams": 160.0})
check("whole pack detected", NB.quantity_correction("But I had the whole pack")
      == {"whole_pack": True})
check("half of it is a factor", NB.quantity_correction("only had half of it")
      == {"factor": 0.5})
check("identity dispute is NOT a quantity correction",
      NB.quantity_correction("not peanut butter, it was 20g of jam") is None)
check("kg converts to grams", NB.quantity_correction("I had 1.5kg")
      == {"grams": 1500.0})

item = {"resolved_name": "Spanish omelette", "kcal": 120.0, "carb_g": 10.0,
        "fat_g": 5.0, "per_100g": {"kcal": 120.0, "carb_g": 10.0, "fat_g": 5.0},
        "portion_used_g": 100.0}
new = NB.rescale_item(item, grams=160)
check("rescale from per-100g basis", new["kcal"] == 192.0 and new["carb_g"] == 16.0)
check("rescale records the stated amount", new["portion_used_g"] == 160
      and new["portion_estimated"] is False)
new = NB.rescale_item({"kcal": 100.0, "portion_used_g": 50.0}, grams=75)
check("rescale falls back to portion ratio", new["kcal"] == 150.0)
new = NB.rescale_item({"kcal": 100.0}, factor=0.5)
check("rescale by bare factor", new["kcal"] == 50.0)
check("no basis at all refuses", NB.rescale_item({"resolved_name": "x"}, grams=50)
      is None)
check("identity never touched by rescale",
      NB.rescale_item(item, grams=160)["resolved_name"] == "Spanish omelette")

# --- exclusions must name a food, never an amount --------------------------------------
check("'100g' never becomes an exclusion",
      NB.exclusions_in("That's 100g I had 160g") == [])
check("'whole pack' never becomes an exclusion",
      NB.exclusions_in("no, I had the whole pack") == [])
check("a real food still excludes",
      "peanut butter" in NB.exclusions_in("I never said peanut butter"))

if FAILED:
    print(f"{len(FAILED)} FAILED"); sys.exit(1)
print("all checks passed")


# --- the log-editing verbs (13 Aug 2026) -----------------------------------------------
# Four edits that had to go through a human operator. The three that touch the bot's own
# wiring are checked here: remembered product facts reaching the LADDER, a stated time
# reaching the STORE, and a named entry that matches nothing asking rather than guessing.

print("\n--- remembered product facts reach the resolution ladder ---")
# "A rego scoop is half a portion" is only worth storing if it is then consulted. The
# injection is deterministic and code-side: a stored number, never a model guess at
# logging time.
facts = {"sis rego": {"scoop_g": 25.0}, "sis choco": {"means": "SiS GO Energy Choco Fudge bar"}}
planned = [{"canonical_name": "SiS REGO Rapid Recovery", "portion_g": None, "count": None,
            "search_terms": ["SiS REGO Rapid Recovery"]}]
out = NB.apply_product_facts(facts, planned, said="1 scoop sis rego")
check("a remembered scoop weight becomes the portion", out[0]["portion_g"] == 25.0)
check("and says where the figure came from", "told me" in (out[0].get("portion_from_fact") or ""))
# The word "scoop" is stripped by the interpretation, so the fact has to be matched
# against HIS wording as well as the canonical name.
check("the scoop word is read from what he actually said",
      NB.apply_product_facts(facts, [{"canonical_name": "SiS REGO Rapid Recovery",
                                      "portion_g": None,
                                      "search_terms": ["sis rego"]}],
                             said="sis rego, one scoop")[0]["portion_g"] == 25.0)
check("two scoops is two of them",
      NB.apply_product_facts(facts, [{"canonical_name": "sis rego", "portion_g": None,
                                      "count": 2, "search_terms": ["sis rego"]}],
                             said="2 scoops of rego")[0]["portion_g"] == 50.0)
# A fact about a SCOOP is not a fact about a bar, and a stated amount outranks a
# remembered one - the fact exists to fill a gap, not to overrule him.
check("no scoop in the words means no injected portion",
      NB.apply_product_facts(facts, [{"canonical_name": "sis rego bar",
                                      "portion_g": None,
                                      "search_terms": ["sis rego bar"]}],
                             said="a sis rego bar")[0]["portion_g"] is None)
check("a stated gram amount is never overwritten",
      NB.apply_product_facts(facts, [{"canonical_name": "sis rego", "portion_g": 60,
                                      "search_terms": ["sis rego"]}],
                             said="60g scoop of sis rego")[0]["portion_g"] == 60)
check("an unknown product is left entirely alone",
      NB.apply_product_facts(facts, [{"canonical_name": "porridge", "portion_g": None,
                                      "search_terms": ["porridge"]}],
                             said="a scoop of porridge")[0]["portion_g"] is None)
check("no facts at all is a no-op",
      NB.apply_product_facts({}, [{"canonical_name": "sis rego", "portion_g": None}],
                             said="a scoop")[0]["portion_g"] is None)

aliased = NB.apply_product_facts(facts, [{"canonical_name": "sis choco",
                                          "portion_g": None,
                                          "search_terms": ["sis choco", "sis choco bar"]}],
                                 said="had a sis choco")
check("a means alias rewrites the canonical name",
      aliased[0]["canonical_name"] == "SiS GO Energy Choco Fudge bar")
# offer_planned passes queries=it["search_terms"] into the ladder, so an alias applied to
# the name alone would never reach a search - the same computed-here-lost-at-the-hand-off
# shape that dropped the photo hint.
check("and rewrites the search terms, which are what the ladder actually searches",
      aliased[0]["search_terms"]
      == ["SiS GO Energy Choco Fudge bar", "SiS GO Energy Choco Fudge bar bar"])
check("the alias also rewrites a plain text item, as offer_items gets them",
      NB.apply_product_facts(facts, [{"text": "sis choco"}], said="sis choco")[0]["text"]
      == "SiS GO Energy Choco Fudge bar")

print("\n--- and the injection is wired into the call sites, not merely defined ---")
# The library being right while the caller drops the value on the floor is the recurring
# failure in this file, so the wiring is asserted rather than assumed.
for fn in (NB.offer_planned, NB.offer_items):
    src = inspect.getsource(fn)
    check(f"{fn.__name__} applies the facts before resolving",
          "apply_product_facts(remembered_facts(ctx)" in src)
check("offer_planned passes the athlete's own wording in",
      "said=t" in inspect.getsource(NB.handle_text))
# The model is now asked what a correction means even with NOTHING logged, because a fact
# about a product is not a fact about an entry. That makes target_item None where it used
# to be guaranteed, so whatever handles a meal correction has to cope with that.
#
# Was 'and not pend and target_item:' inline in handle_text. The `not pend` half of that
# guard was the bug: it dropped the meal whenever an offer was still on the table. Both
# cases now live in apply_meal_correction, which is tested against the store below.
check("the meal branch handles a pending offer as well as a committed entry",
      'apply_meal_correction(ctx, decision, pend, target_item, day,'
      in inspect.getsource(NB.handle_text)
      and 'target_item' in inspect.getsource(NB.apply_meal_correction))
check("the model is consulted even when nothing is logged",
      "target_item or {}" in inspect.getsource(NB.handle_text))

print("\n--- a stated time reaches the store, built from the LOCAL day ---")
# "Add second slice of toast with butter at 1350" was stamped with the moment the message
# arrived, so the app filed it under the wrong meal.
stated_store = S.NutritionStore(Path(tempfile.mkdtemp(prefix="nb-at-")))


class FakeCtxCommit:
    """Only what commit_pending and commit_one touch. publish_now and the fuel write-back
    are stubbed out at module level below, so nothing here reaches git or the network."""
    slug = "test"
    table = None
    cofid = None
    fetchers = {}

    def __init__(self, store):
        self.store = store
        self.athlete_dir = store.dir.parent


sent_msgs = []
# NB.tg and NB.NR are the SAME module objects this file imported at the top, so these
# stubs are global and are put back at the end of the file. The golden fixtures were
# silently green for a while because a stub like this was left in place and every later
# check ran against it instead of the real code.
_REAL = {"send": NB.tg.send, "publish_now": NB.publish_now,
         "today_block": NB.today_block, "cache_resolved": NB.NR.cache_resolved,
         "_chat": NB._chat, "gate_runner": NB.GATE_RUNNER}
NB.tg.send = lambda token, chat, text, **k: sent_msgs.append(text)
# THE PRE-SEND GATE, stubbed to a plain "send" for every check in this file that is about
# something else. Registered in _REAL and restored with the rest: this file's own comment
# above says a leftover stub is how fixtures went silently green, and a gate stub left in
# place would make the restore assertion below meaningless. Without it, every offer in this
# file would try to spawn the real CLI.
gate_calls = []


def gate_says(reply):
    def run(cmd, input=None, **kwargs):
        gate_calls.append(input)
        return type("P", (), {"stdout": reply, "stderr": ""})()
    return run


NB.GATE_RUNNER = gate_says('{"verdict":"send","reason":"fine"}')
NB.publish_now = lambda ctx: None
NB.today_block = lambda ctx, day: "(totals)"
NB.NR.cache_resolved = lambda store, item: None
NB._chat = lambda ctx, role, text: None

_ctx = FakeCtxCommit(stated_store)
timed = {"raw_text": "second slice of toast with butter", "_raw": "toast with butter",
         "resolved_name": "Toast with butter", "kcal": 180.0, "protein_g": 5.0,
         "carb_g": 20.0, "fat_g": 9.0, "confidence": "label", "source_rung": "cofid",
         "_at": "13:50"}
NB.commit_pending(_ctx, {"batch": [timed]}, TODAY, "token", 1)
entry = stated_store.get_day(TODAY)["entries"][0]
check("the stated time becomes the entry's logged_at",
      entry["logged_at"] == f"{TODAY.isoformat()}T13:50")
check("which is the LOCAL day, not the server's clock",
      entry["logged_at"].startswith(TODAY.isoformat()))
# The default has to stay now-time, or every untimed log lands at midnight and reads as
# breakfast.
NB.commit_pending(_ctx, {"batch": [dict(timed, _at=None, resolved_name="Banana")]},
                  TODAY, "token", 1)
untimed = stated_store.get_day(TODAY)["entries"][1]
check("an item with no stated time still gets a real clock time",
      untimed["logged_at"][11:16] not in ("", "00:00"))
check("the offer says which time it will use, before anything is written",
      NB._stated_time_note([timed]) == ["_Logging at 13:50, as you said._"])
check("and says nothing when no time was stated", NB._stated_time_note([{}]) == [])

print("\n--- a named entry that matches nothing asks, rather than guessing ---")
# find_entry falls back to the most recent entry, which is right for "that" and wrong for a
# name it could not find: silently retiming the wrong entry looks entirely correct in the
# reply. Same guard the delete branch already has.
guard_store = S.NutritionStore(Path(tempfile.mkdtemp(prefix="nb-which-")))
guard_store.add_entry(TODAY, raw_text="rye bread", resolved_name="Rye bread", kcal=83,
                      confidence="label", source_rung="manual")
guard_store.add_entry(TODAY, raw_text="160g chicken", resolved_name="Chicken breast",
                      kcal=265, confidence="label", source_rung="manual",
                      portion_g=160)
# A third entry AFTER the chicken, deliberately. With the chicken last, every check below
# would also pass on find_entry's fall-back-to-latest, which matches resolved_name only -
# so "the 160g" would have been green while a real day with anything logged afterwards
# said "I cannot see 'the 160g'".
guard_store.add_entry(TODAY, raw_text="flat white", resolved_name="Flat white", kcal=120,
                      confidence="label", source_rung="cofid")
gctx = FakeCtxCommit(guard_store)
sent_msgs.clear()
check("a name that matches nothing returns no entry",
      NB.entry_he_means(gctx, TODAY, "the porridge", "retime", "token", 1) is None)
check("and says so, listing what today actually has",
      sent_msgs and "cannot see" in sent_msgs[-1] and "Rye bread" in sent_msgs[-1])
check("a name that matches finds THAT entry, not the latest",
      (NB.entry_he_means(gctx, TODAY, "initial rye bread", "retime", "token", 1) or {})
      .get("resolved_name") == "Rye bread")
check("no name at all means the most recent",
      (NB.entry_he_means(gctx, TODAY, "", "retime", "token", 1) or {})
      .get("resolved_name") == "Flat white")
# "The initial rye bread" is the case where taking the newest match is wrong, and
# "initial" is a word no matcher here understands - so two matches asks.
guard_store.add_entry(TODAY, raw_text="rye bread again", resolved_name="Rye bread",
                      kcal=83, confidence="label", source_rung="manual")
sent_msgs.clear()
check("two entries matching the same name asks which one",
      NB.entry_he_means(gctx, TODAY, "initial rye bread", "retime", "token", 1) is None
      and "Which one" in sent_msgs[-1])
guard_store.remove_entry(TODAY, guard_store.get_day(TODAY)["entries"][-1]["id"])
# "The 160g" names the AMOUNT. Matching only against resolved_name would have asked him
# which entry he meant while pointing straight at it.
check("an amount identifies an entry as well as a name does",
      (NB.entry_he_means(gctx, TODAY, "the 160g", "rename", "token", 1) or {})
      .get("resolved_name") == "Chicken breast")
empty_ctx = FakeCtxCommit(S.NutritionStore(Path(tempfile.mkdtemp(prefix="nb-empty-"))))
sent_msgs.clear()
check("an empty day says nothing is logged rather than failing",
      NB.entry_he_means(empty_ctx, TODAY, "", "retime", "token", 1) is None
      and "Nothing logged today to retime" in sent_msgs[-1])

print("\n--- retime and rename, end to end against the store ---")
sent_msgs.clear()
handled = NB.apply_retime(gctx, {"kind": "retime", "time": "08:30",
                                 "which": "initial rye bread"}, TODAY, "token", 1)
rye = [e for e in guard_store.get_day(TODAY)["entries"]
       if e["resolved_name"] == "Rye bread"][0]
check("retime moves the named entry's timestamp",
      handled and rye["logged_at"] == f"{TODAY.isoformat()}T08:30")
check("and confirms it in one line", "08:30" in sent_msgs[-1])
check("the entry he did not name keeps its own timestamp",
      guard_store.get_day(TODAY)["entries"][1]["logged_at"][11:16] != "08:30")

sent_msgs.clear()
handled = NB.apply_rename(gctx, {"kind": "rename", "name": "BBQ chicken, 160g pack",
                                 "which": "the 160g"}, TODAY, "token", 1)
renamed = guard_store.get_day(TODAY)["entries"][1]
check("rename renames the entry he pointed at",
      handled and renamed["resolved_name"] == "BBQ chicken, 160g pack")
check("and keeps the figures he read off the pack", renamed["kcal"] == 265.0)
check("the reply says the figures were kept", "Kept your figures" in sent_msgs[-1])

print("\n--- remember stores the fact, and rescales when that is what it also fixes ---")
sent_msgs.clear()
rem_store = S.NutritionStore(Path(tempfile.mkdtemp(prefix="nb-rem-")))
rem_store.add_entry(TODAY, raw_text="1 scoop sis rego", resolved_name="SiS REGO",
                    kcal=100, carb_g=23, confidence="label", source_rung="manual")
rctx = FakeCtxCommit(rem_store)
handled = NB.apply_remember(rctx, {"kind": "remember", "product": "sis rego",
                                   "field": "scoop_g", "value": 25.0},
                            None, TODAY, "token", 1)
check("the fact is stored", handled
      and rem_store.product_facts()["sis rego"]["scoop_g"] == 25.0)
check("and the reply states exactly what was stored, so a wobble is visible",
      "25 g" in sent_msgs[-1] and "sis rego" in sent_msgs[-1])
handled = NB.apply_remember(rctx, {"kind": "remember", "product": "sis choco",
                                   "field": "means",
                                   "value": "SiS GO Energy Choco Fudge bar"},
                            None, TODAY, "token", 1)
check("an alias is confirmed in its own words",
      handled and "SiS GO Energy Choco Fudge bar" in sent_msgs[-1])
check("an unusable fact is refused rather than stored",
      NB.apply_remember(rctx, {"kind": "remember", "product": "sis rego",
                               "field": "kcal", "value": 80}, None, TODAY, "token", 1)
      is False)

# The REGO message does BOTH: remember that a scoop is 25 g, and fix this entry to 25 g.
sent_msgs.clear()
both_store = S.NutritionStore(Path(tempfile.mkdtemp(prefix="nb-both-")))
both_store.add_entry(TODAY, raw_text="1 scoop sis rego", resolved_name="SiS REGO",
                     kcal=200, carb_g=46, confidence="label", source_rung="manual")
bctx = FakeCtxCommit(both_store)
both_store.update_entry(TODAY, both_store.get_day(TODAY)["entries"][0]["id"],
                        portion_used_g=50.0)
handled = NB.apply_remember(bctx, {"kind": "remember_and_rescale", "product": "sis rego",
                                   "field": "scoop_g", "value": 25.0, "grams": 25.0},
                            None, TODAY, "token", 1)
after = both_store.get_day(TODAY)["entries"][0]
check("the fact is stored and the entry rescaled in one go",
      handled and both_store.product_facts()["sis rego"]["scoop_g"] == 25.0
      and after["kcal"] == 100.0)
check("the rescale is arithmetic on the entry, not a fresh search",
      after["resolved_name"] == "SiS REGO" and after["portion_used_g"] == 25.0)

print("\n--- the meal reaches the store, or the clock decides and says it guessed ---")
# Jamie, 13 Aug 2026: "improve time of meal logging, often added to wrong category." The
# store owns the fallback; what is tested here is that the bot's hand-off does not drop the
# meal on the floor, which is the recurring failure in this file.
meal_store = S.NutritionStore(Path(tempfile.mkdtemp(prefix="nb-meal-")))
mctx = FakeCtxCommit(meal_store)
base = {"raw_text": "porridge", "_raw": "porridge", "resolved_name": "Porridge",
        "kcal": 250.0, "confidence": "label", "source_rung": "cofid"}
NB.commit_pending(mctx, {"batch": [dict(base, _meal="breakfast", _at="13:49")]},
                  TODAY, "token", 1)
stated = meal_store.get_day(TODAY)["entries"][0]
check("a meal he named is written with the entry and not marked as a guess",
      stated["meal"] == "breakfast" and stated["meal_inferred"] is False)
check("even though the clock would have said lunch",
      stated["logged_at"].endswith("T13:49"))
NB.commit_pending(mctx, {"batch": [dict(base, resolved_name="Rye bread", _at="08:30")]},
                  TODAY, "token", 1)
guessed = meal_store.get_day(TODAY)["entries"][1]
check("with no meal named, the stated TIME files it and the guess is flagged",
      guessed["meal"] == "breakfast" and guessed["meal_inferred"] is True)
# The in-session guard, at the commit boundary rather than only in the store: fuel logged
# at 13:00 must not appear under lunch on the app.
NB.commit_pending(mctx, {"batch": [dict(base, resolved_name="SiS GO gel", _at="13:00",
                                        in_session=True, _meal="lunch")]},
                  TODAY, "token", 1)
fuel = meal_store.get_day(TODAY)["entries"][2]
check("in-session fuel commits with no meal at all",
      fuel["in_session"] is True and fuel["meal"] == ""
      and fuel["meal_inferred"] is False)

# The wiring, asserted rather than assumed: a library that is right while the caller drops
# the value is how the photo hint and the species score were both lost.
for fn in (NB.offer_planned, NB.offer_items):
    src = inspect.getsource(fn)
    check(f"{fn.__name__} takes a message-level meal and puts it on every item",
          "default_meal: str = None" in src and '"_meal"' in src
          and 'or default_meal or ""' in src)
check("handle_text reads the meal off the parsed items and passes it to every offer path",
      'stated_meal = next((i.get("meal")' in inspect.getsource(NB.handle_text)
      # Four paths now: the costed meal, the athlete-supplied figures, the interpreted plan
      # and the raw classify items. A path that forgot the meal would drop it silently.
      and inspect.getsource(NB.handle_text).count("default_meal=stated_meal") == 4)
check("commit_one hands it to the store", 'meal=item.get("_meal")'
      in inspect.getsource(NB.commit_one))
check("the offer says which meal it will use, before anything is written",
      NB._stated_meal_note([{"_meal": "breakfast"}])
      == ["_Filing under breakfast, as you said._"])
check("and says nothing when the clock is doing the guessing",
      NB._stated_meal_note([{"_meal": ""}]) == [] and NB._stated_meal_note([{}]) == [])

print("\n--- “that was breakfast” works with an offer still on the table ---")
# fast_intent deliberately keeps out of the way while a yes/no is outstanding, so this
# arrives as a correction and the model returns {"kind":"meal"}. The branch that handled it
# required `not pend`, so the meal was dropped exactly when he was still confirming the item
# it belonged to - and applying it to the last COMMITTED entry instead would have filed the
# wrong food.
check("a meal tag does not fire on the fast path while something is pending",
      (NLU.fast_intent("that was breakfast", True) or {}).get("intent") != "set_meal")
decision = NLU.decide_correction(
    "that was breakfast", {"resolved_name": "Rye bread"}, "claude", "m",
    log=lambda *a: None,
    runner=lambda *a, **k: type("P", (), {
        "stdout": '{"kind":"meal","meal":"breakfast"}', "stderr": ""})())
check("the model's decision comes back as a meal", decision == {"kind": "meal",
                                                                "meal": "breakfast"})

pend_store = S.NutritionStore(Path(tempfile.mkdtemp(prefix="nb-pmeal-")))
pctx = FakeCtxCommit(pend_store)
offer = {"batch": [dict(base, resolved_name="Rye bread"),
                   dict(base, resolved_name="Butter")]}
NB.set_pending(pend_store, offer)
sent_msgs.clear()
handled = NB.apply_meal_correction(pctx, decision, NB.get_pending(pend_store), None,
                                   TODAY, "token", 1)
still = NB.get_pending(pend_store)
check("the meal lands on the pending batch rather than being dropped",
      handled and [i["_meal"] for i in still["batch"]] == ["breakfast", "breakfast"])
check("the offer is still pending - nothing was written behind his back",
      not pend_store.get_day(TODAY)["entries"] and "when you confirm" in sent_msgs[-1])
NB.commit_pending(pctx, still, TODAY, "token", 1)
check("and it commits with the meal he named",
      [(e["meal"], e["meal_inferred"])
       for e in pend_store.get_day(TODAY)["entries"]]
      == [("breakfast", False), ("breakfast", False)])

# With nothing pending it files the committed entry, and stops calling it a guess.
sent_msgs.clear()
done_store = S.NutritionStore(Path(tempfile.mkdtemp(prefix="nb-cmeal-")))
dctx = FakeCtxCommit(done_store)
entry = done_store.add_entry(TODAY, raw_text="rye bread", resolved_name="Rye bread",
                             kcal=83, confidence="label", source_rung="manual",
                             logged_at=f"{TODAY.isoformat()}T13:49")
check("it starts out guessed as lunch",
      entry["meal"] == "lunch" and entry["meal_inferred"] is True)
handled = NB.apply_meal_correction(dctx, {"kind": "meal", "meal": "breakfast"}, None,
                                   entry, TODAY, "token", 1)
after = done_store.get_day(TODAY)["entries"][0]
check("a meal correction on a committed entry files it and clears the guess flag",
      handled and after["meal"] == "breakfast" and after["meal_inferred"] is False)
check("and it is confirmed in one line", "breakfast" in sent_msgs[-1])
check("an unusable meal is refused rather than written",
      NB.apply_meal_correction(dctx, {"kind": "meal", "meal": "elevenses"}, None,
                               entry, TODAY, "token", 1) is False)
check("and with neither an offer nor an entry it declines instead of failing",
      NB.apply_meal_correction(dctx, {"kind": "meal", "meal": "breakfast"}, None,
                               None, TODAY, "token", 1) is False)

print("\n--- REPLAY: his pasted macro table logs HIS figures (14 Aug 2026) ---")
# THE DEFECT. After the bot had twice mis-priced a stir-fry, he pasted a complete
# breakdown - a ~980 kcal total and a row per component. Every row went down the ladder as
# a fresh lookup and the meal came back at 2,400 kcal: the dried-noodle row scaled wrong,
# and 100 g of oil at 899 kcal. He had given the answer and was argued with using worse
# data. His figures are now copied verbatim and no rung is walked.
PASTED = ("Large stir-fry bowl ~980 kcal\n"
          "Egg noodles (300g cooked) 380 kcal, 12P, 75C, 3F\n"
          "Steak (100g) 220 kcal, 26P, 0C, 13F\n"
          "Soy/ginger/garlic sauce 80 kcal, 2P, 8C, 4F\n"
          "Vegetables (200g) 90 kcal, 4P, 15C, 1F\n"
          "Oil 210 kcal, 0P, 0C, 23F")
_PARSED_TABLE = (
    '{"intent":"log_food","items":[{"text":"large stir-fry bowl with egg noodles, steak, '
    'soy ginger garlic sauce, vegetables and oil","portion_g":null,"in_session":false,'
    '"at":null,"meal":null,"stated":{"kcal":980,"protein_g":44,"carb_g":98,"fat_g":44,'
    '"basis":"estimate","components":["Egg noodles (300g cooked) 380 kcal, 12P, 75C, 3F",'
    '"Steak (100g) 220 kcal, 26P, 0C, 13F","Soy/ginger/garlic sauce 80 kcal, 2P, 8C, 4F",'
    '"Vegetables (200g) 90 kcal, 4P, 15C, 1F","Oil 210 kcal, 0P, 0C, 23F"]}}]}')

# THE LADDER MUST NOT RUN AT ALL, so it is replaced by something that fails loudly rather
# than merely asserted about afterwards. A number that happens to be right while a lookup
# still happened is the bug one edit away from returning.
_ladder_calls = []


def _exploding_resolve(*a, **k):
    _ladder_calls.append(a)
    raise AssertionError("the resolution ladder ran on figures the athlete supplied")


class FakeCtxHandle(FakeCtxCommit):
    """handle_text needs the athlete's local day; nothing else here touches ICU."""

    def local_today(self):
        return TODAY


table_store = S.NutritionStore(Path(tempfile.mkdtemp(prefix="nb-stated-")))
tctx = FakeCtxHandle(table_store)
# NB.NLU and NB.NR are the same module objects this file imported, so the real functions
# have to be captured BEFORE they are replaced - reading them back off the module at the
# end would restore the stub over itself.
_real_resolve, _real_interpret = NB.NR.resolve, NB.NLU.interpret
_real_classify, _real_parse = NB.NLU.classify, NB.NLU.parse_with_model
_real_decide = NB.NLU.decide_correction
NB.NR.resolve = _exploding_resolve
NB.NLU.interpret = lambda *a, **k: (_ladder_calls.append(("interpret",)) or None)
NB.NLU.classify = lambda text, pending, *a, **k: _real_parse(
    text, "claude", "m", log=lambda *x: None,
    runner=lambda *c, **kw: type("P", (), {"stdout": _PARSED_TABLE, "stderr": ""})())
sent_msgs.clear()
NB.handle_text(tctx, PASTED, "token", 1)
check("no lookup of any kind ran on his own figures", _ladder_calls == [])
pend = NB.get_pending(table_store)
check("the pasted table becomes ONE pending item, not five",
      pend and len(pend["batch"]) == 1)
_meal = pend["batch"][0]
check("980 kcal stays 980 kcal, exactly", _meal["kcal"] == 980.0)
check("and every stated macro is his, to the number",
      (_meal["protein_g"], _meal["carb_g"], _meal["fat_g"]) == (44.0, 98.0, 44.0))
check("the rung says a person supplied it", _meal["source_rung"] == NB.NR.Rung.MANUAL)
check("his reckoning is recorded as an estimate, not as label data",
      _meal["confidence"] == "estimate")
check("his own rows are kept as the breakdown",
      "Egg noodles (300g cooked) 380 kcal" in _meal["ingredients"]
      and len(_meal["_components"]) == 5)
check("the offer tells him they are his figures, not a source's",
      "Your figures, logged exactly as you gave them." in sent_msgs[-1])
check("and it is offered for confirmation like anything else, once",
      "Log it?" in sent_msgs[-1] and len(sent_msgs) == 1)
NB.commit_pending(tctx, pend, TODAY, "token", 1)
logged = table_store.get_day(TODAY)["entries"][-1]
check("it commits with his total intact",
      logged["kcal"] == 980.0 and logged["protein_g"] == 44.0
      and logged["source_rung"] == "manual")
check("the pending offer is cleared once written",
      NB.get_pending(table_store) is None)
NB.NR.resolve, NB.NLU.interpret = _real_resolve, _real_interpret
NB.NLU.classify = _real_classify
def _code_of(fn):
    """Source with the docstring dropped, so a function that only MENTIONS the ladder in
    its rationale is not mistaken for one that calls it."""
    src = inspect.getsource(fn)
    parts = src.split('"""')
    return parts[0] + "".join(parts[2:]) if len(parts) > 2 else src


check("the stated path is structurally incapable of resolving",
      "NR.resolve" not in _code_of(NB.offer_stated)
      and "NR.resolve" not in _code_of(NB.stated_item))
check("and handle_text checks for stated figures BEFORE it plans any lookup",
      inspect.getsource(NB.handle_text).index('offer_stated(')
      < inspect.getsource(NB.handle_text).index("NLU.interpret("))

print("\n--- REPLAY: the stir-fry, costed whole by one capable model (14 Aug 2026) ---")
# THE DEFECT. "a large stir fry with egg noodles, a small steak, soy ginger garlic sauce and
# veg" was broken into four components, each looked up separately, and offered as 447 kcal
# of raw and dried 100 g parts. Jamie got a correct table by asking a generic Opus 5 himself.
# The composition tables hold ingredients, not dinners, so a cooked meal is now costed in ONE
# call and the ladder never runs on it.
STIR_FRY = ("a large stir fry with egg noodles, a small steak, soy ginger garlic sauce "
            "and veg")
MEAL_TABLE = """{"meal_name":"Large beef stir-fry with egg noodles",
 "components":[
  {"name":"egg noodles, cooked","portion_g":300,"portion_basis":"a large bowl",
   "kcal":420,"protein_g":14,"carb_g":80,"fat_g":4,"fibre_g":4},
  {"name":"rump steak, grilled","portion_g":120,"portion_basis":"a small steak",
   "kcal":210,"protein_g":37,"carb_g":0,"fat_g":7,"fibre_g":0},
  {"name":"soy, ginger and garlic sauce","portion_g":45,"portion_basis":"2 tbsp",
   "kcal":60,"protein_g":2,"carb_g":10,"fat_g":1,"fibre_g":0},
  {"name":"stir-fried mixed vegetables","portion_g":200,"portion_basis":"a handful",
   "kcal":110,"protein_g":4,"carb_g":12,"fat_g":5,"fibre_g":5},
  {"name":"vegetable oil for the pan","portion_g":15,"portion_basis":"1 tbsp",
   "kcal":135,"protein_g":0,"carb_g":0,"fat_g":15,"fibre_g":0}],
 "total":{"kcal":935,"protein_g":57,"carb_g":102,"fat_g":32,"fibre_g":9},
 "error_band_pct":18,
 "plants":["wheat","garlic","ginger","soya","onion","red pepper","broccoli"],
 "assumptions":["Large bowl taken as 300g cooked noodles","1 tbsp oil in the pan"]}"""
_PARSED_COMPOSED = ('{"intent":"log_food","composed_meal":true,"items":[{"text":'
                    '"large stir fry with egg noodles, steak, sauce and veg",'
                    '"portion_g":null,"in_session":false,"at":null,"meal":"dinner"}]}')

_meal_asks = []


def _meal_runner(reply):
    """Records the prompt and which MODEL was asked, so the routing is checked and not
    assumed: the whole point of this path is that it goes to the best model available."""
    def run(cmd, input=None, **kwargs):
        _meal_asks.append({"cmd": list(cmd), "prompt": input})
        return type("P", (), {"stdout": reply, "stderr": ""})()
    return run


class FakeCtxMeal(FakeCtxHandle):
    """A ctx WITH the plant table: a costed meal's species come from the deterministic
    matcher, and a stub with table=None would hide that the wiring exists."""
    table = PL.SpeciesTable()


meal_store = S.NutritionStore(Path(tempfile.mkdtemp(prefix="nb-meal-")))
mctx = FakeCtxMeal(meal_store)
_ladder_calls.clear()
_meal_asks.clear()
NB.NR.resolve = _exploding_resolve
NB.NLU.interpret = lambda *a, **k: (_ladder_calls.append(("interpret",)) or None)
NB.NLU.classify = lambda text, pending, *a, **k: _real_parse(
    text, "claude", "m", log=lambda *x: None,
    runner=lambda *c, **kw: type("P", (), {"stdout": _PARSED_COMPOSED, "stderr": ""})())
_real_subprocess_run = NB.NLU.subprocess.run
NB.NLU.subprocess.run = _meal_runner(MEAL_TABLE)
sent_msgs.clear()
NB.handle_text(mctx, STIR_FRY, "token", 1)
check("the ladder never ran on a meal he cooked", _ladder_calls == [])
check("and it was costed by Opus explicitly, not the config default",
      _meal_asks and "claude-opus-5" in _meal_asks[0]["cmd"]
      and NB.MEAL_MODEL == "claude-opus-5")
check("his own words are what the model was given",
      "a large stir fry with egg noodles" in (_meal_asks[0]["prompt"] or ""))
pend = NB.get_pending(meal_store)
check("the meal is ONE pending entry, not five", pend and len(pend["batch"]) == 1)
_m = pend["batch"][0]
check("its total is the sum of the costed components",
      _m["kcal"] == 935.0 and _m["protein_g"] == 57.0 and _m["carb_g"] == 102.0)
check("which is a real dinner rather than the 447 kcal he was offered",
      700 < _m["kcal"] < 1300)
check("it is labelled an estimate and carries the model's error band",
      _m["confidence"] == "estimate" and _m["_error_band_pct"] == 18
      and "+/-18%" in (_m.get("note") or ""))
check("the components are kept on the entry, with their portions",
      len(_m["_components_detail"]) == 5
      and _m["_components_detail"][0]["portion_g"] == 300.0)
check("the plants it named are tagged by the deterministic matcher",
      len(_m["species"]) >= 4 and all(isinstance(s.get("id"), str)
                                      for s in _m["species"]))
check("the whole entry is an assumed portion, and says so",
      _m["portion_estimated"] is True and _m["portion_assumed"])
check("the meal he named still reaches the entry", _m["_meal"] == "dinner")
# The offer he reads: the table, then the total, then the assumptions he corrects.
_offer = sent_msgs[-1]
check("the offer shows the table, component by component",
      "300g egg noodles, cooked" in _offer and "420 kcal" in _offer
      and "vegetable oil for the pan" in _offer)
check("with the total and the band",
      "*Total* kcal 935" in _offer and "+/-18%" in _offer)
check("and every assumption stated where he can see it",
      "assumed: Large bowl taken as 300g cooked noodles" in _offer
      and "assumed: 1 tbsp oil in the pan" in _offer)
check("and it is one confirm message, not five",
      _offer.count("Log it?") == 1 and len(sent_msgs) == 1)
NB.commit_pending(mctx, pend, TODAY, "token", 1)
_logged = meal_store.get_day(TODAY)["entries"][-1]
check("it commits as one entry at the costed total",
      _logged["kcal"] == 935.0 and _logged["source_rung"] == "llm"
      and _logged["confidence"] == "estimate")
check("with the components kept as its ingredients",
      "egg noodles, cooked" in _logged["ingredients"]
      and "ginger" in _logged["ingredients"])

# A CORRECTION RE-TABLES, it does not re-search. "The noodles were 400g" is a fact about one
# row of a table built from his description, so the meal is re-costed with that fact added.
RETABLED = MEAL_TABLE.replace('"portion_g":300', '"portion_g":400').replace(
    '"kcal":420', '"kcal":560').replace('"total":{"kcal":935', '"total":{"kcal":1075')
NB.set_pending(meal_store, {"batch": [dict(_m)]})
NB.NLU.classify = lambda text, pending, *a, **k: {"intent": "correction",
                                                 "correction": text}
NB.NLU.decide_correction = lambda *a, **k: {"kind": "unclear"}
NB.NLU.subprocess.run = _meal_runner(RETABLED)
_meal_asks.clear()
_ladder_calls.clear()
sent_msgs.clear()
NB.handle_text(mctx, "the noodles were 400g", "token", 1)
check("a correction re-tables the meal instead of re-resolving it",
      _ladder_calls == [] and _meal_asks
      and "the noodles were 400g" in (_meal_asks[-1]["prompt"] or ""))
check("and his original description goes with it, so nothing is lost",
      "large stir fry" in (_meal_asks[-1]["prompt"] or ""))
_re = NB.get_pending(meal_store)["batch"][0]
check("the re-costed meal replaces the offer at the new total",
      _re["kcal"] == 1075.0
      and _re["_components_detail"][0]["portion_g"] == 400.0)
check("nothing was written while he was still correcting it",
      len(meal_store.get_day(TODAY)["entries"]) == 1)

# AN UNREACHABLE MODEL MUST NOT MEAN AN UNLOGGABLE DINNER: the interpret-and-resolve path is
# a poor second to a costed table and far better than a refusal.
NB.clear_pending(meal_store)
NB.NLU.classify = lambda text, pending, *a, **k: _real_parse(
    text, "claude", "m", log=lambda *x: None,
    runner=lambda *c, **kw: type("P", (), {"stdout": _PARSED_COMPOSED, "stderr": ""})())
NB.NLU.subprocess.run = _meal_runner("API Error: 401 OAuth access token has expired")
# The ladder is EXPECTED to run here, so it records instead of exploding: the fallback's
# whole purpose is that his dinner is still loggable when the meal model is down.
NB.NR.resolve = lambda text, **k: (_ladder_calls.append(("resolve", text)) or {
    "resolved_name": text, "kcal": 200.0, "confidence": "estimate",
    "source_rung": "llm", "attempts": [], "species": []})
_ladder_calls.clear()
sent_msgs.clear()
NB.handle_text(mctx, STIR_FRY, "token", 1)
check("an unreachable meal model falls back to the ladder rather than refusing",
      ("interpret",) in _ladder_calls
      and any(c[0] == "resolve" for c in _ladder_calls))
check("and he still gets an offer he can confirm", "Log it?" in sent_msgs[-1])
NB.NR.resolve = _exploding_resolve
check("and offer_composed reports the failure rather than offering half a meal",
      NB.offer_composed(mctx, STIR_FRY, TODAY, "token", 1) is False)
NB.NLU.subprocess.run = _real_subprocess_run
NB.NR.resolve, NB.NLU.interpret = _real_resolve, _real_interpret
NB.NLU.classify, NB.NLU.decide_correction = _real_classify, _real_decide
check("the composed path cannot reach the ladder either",
      "NR.resolve" not in _code_of(NB.offer_composed)
      and "NR.resolve" not in _code_of(NB.composed_item))
check("a barcode keeps its exact-product path, whatever else the message looks like",
      'not got.get("barcode")' in inspect.getsource(NB.handle_text))
# A whole dinner must not become in-run fuel because a gel earlier in the same message was:
# fuel counted in the session rewrites the g/hr history the coach prescribes from.
check("the meal's own in-session flag decides, not the message's",
      'in_session=bool((got.get("items") or [{}])[0]' in inspect.getsource(NB.handle_text))

# A COSTED MEAL AND ITS OWN COMPONENT ROWS MUST NEVER DISAGREE. rescale_item moves the
# entry's totals and knows nothing about _components_detail, so scaling in place would leave
# a 1,402 kcal entry whose rows still sum to 935 - and those rows are what the next
# correction is applied to. It would also re-render through fmt_confirm, dropping the table.
_composed_pend = {"batch": [dict(_m)]}
check("a ratio against a costed meal is declined, so it goes to the re-table path",
      NB.apply_batch_rescale(mctx, _composed_pend,
                             {"kind": "rescale_all", "factor": 1.5}, TODAY, "token", 1)
      is False)
check("and a grams correction against one is declined for the same reason",
      NB.apply_quantity_correction(mctx, _composed_pend, {"grams": 400.0},
                                   TODAY, "token", 1) is False)
check("so the entry and its component rows still agree",
      _composed_pend["batch"][0]["kcal"]
      == sum(c["kcal"] for c in _composed_pend["batch"][0]["_components_detail"]))
# His pasted rows are TEXT, so scaling the entry cannot scale them: leaving them on shows
# "Egg noodles 380 kcal" under a 1,470 kcal heading.
_scaled_stated = NB.drop_stale_breakdown(
    {"resolved_name": "Stir-fry bowl", "kcal": 1470.0, "_stated": True,
     "_components": ["Egg noodles 380 kcal"], "ingredients": "Egg noodles 380 kcal"})
check("a rescaled stated entry drops a breakdown that no longer adds up",
      "_components" not in _scaled_stated
      and _scaled_stated["ingredients"] == "Stir-fry bowl")
check("and an item with no breakdown is untouched",
      NB.drop_stale_breakdown({"kcal": 100.0}) == {"kcal": 100.0})

print("\n--- REPLAY: per-component rescaling of a pending meal (14 Aug 2026) ---")
# "Make the noodles, steak and sauce 1.5x and the vegetables 3x" had no shape to be
# expressed in, "do all of that X1.5" was decided correctly and applied to nothing, and
# "it was a whole meal" was decided against a brookie he had committed hours earlier.
def _meal_batch():
    """The four components as they were actually offered: per-100g bases, small portions."""
    return [{"resolved_name": "Noodles, egg, medium, dried, boiled in unsalted water",
             "_raw": "egg noodles, cooked", "kcal": 166.0, "protein_g": 5.0,
             "carb_g": 33.0, "fat_g": 0.5, "portion_used_g": 100.0,
             "per_100g": {"kcal": 166.0, "protein_g": 5.0, "carb_g": 33.0, "fat_g": 0.5},
             "confidence": "label", "source_rung": "cofid"},
            {"resolved_name": "Beef, rump steak, grilled, lean only", "_raw": "steak",
             "kcal": 177.0, "protein_g": 31.0, "carb_g": 0.0, "fat_g": 5.9,
             "portion_used_g": 100.0,
             "per_100g": {"kcal": 177.0, "protein_g": 31.0, "carb_g": 0.0, "fat_g": 5.9},
             "confidence": "label", "source_rung": "cofid"},
            {"resolved_name": "Soy sauce", "_raw": "soy ginger garlic sauce",
             "kcal": 43.0, "protein_g": 3.0, "carb_g": 8.2, "fat_g": 0.0,
             "portion_used_g": 100.0,
             "per_100g": {"kcal": 43.0, "protein_g": 3.0, "carb_g": 8.2, "fat_g": 0.0},
             "confidence": "label", "source_rung": "cofid"},
            {"resolved_name": "Vegetables, stir-fried", "_raw": "vegetables",
             "kcal": 52.0, "protein_g": 2.0, "carb_g": 4.0, "fat_g": 3.2,
             "portion_used_g": 100.0,
             "per_100g": {"kcal": 52.0, "protein_g": 2.0, "carb_g": 4.0, "fat_g": 3.2},
             "confidence": "label", "source_rung": "cofid"}]


rs_store = S.NutritionStore(Path(tempfile.mkdtemp(prefix="nb-rescale-")))
rctx = FakeCtxCommit(rs_store)
NB.set_pending(rs_store, {"batch": _meal_batch()})
sent_msgs.clear()
handled = NB.apply_batch_rescale(
    rctx, NB.get_pending(rs_store),
    {"kind": "rescale_items", "items": [{"index": 0, "factor": 1.5},
                                        {"index": 1, "factor": 1.5},
                                        {"index": 2, "factor": 1.5},
                                        {"index": 3, "factor": 3.0}]},
    TODAY, "token", 1)
after = NB.get_pending(rs_store)["batch"]
# Expected per item from ITS OWN per-100g basis at rescale_item's 1 dp, not from the total.
check("three at 1.5x and one at 3x, each from its own basis",
      handled and [i["kcal"] for i in after] == [249.0, 265.5, 64.5, 156.0])
check("and the macros scale with them",
      [i["protein_g"] for i in after] == [7.5, 46.5, 4.5, 6.0])
check("portions move with the figures",
      [i["portion_used_g"] for i in after] == [150.0, 150.0, 150.0, 300.0])
check("nothing was written - it is re-offered for confirmation",
      not rs_store.get_day(TODAY)["entries"] and "Log these?" in sent_msgs[-1])
check("the new total is stated on the offer",
      f"*Total* {round(249.0 + 265.5 + 64.5 + 156.0)} kcal" in sent_msgs[-1])
check("a factor he gave is not presented as an assumption",
      all(i["portion_estimated"] is False for i in after))

# "Do all of that X1.5": one factor, every component.
NB.set_pending(rs_store, {"batch": _meal_batch()})
sent_msgs.clear()
NB.apply_batch_rescale(rctx, NB.get_pending(rs_store),
                       {"kind": "rescale_all", "factor": 1.5}, TODAY, "token", 1)
check("rescale_all reaches every component",
      [i["kcal"] for i in NB.get_pending(rs_store)["batch"]]
      == [249.0, 265.5, 64.5, 78.0])

# "It was a whole meal, work it out": the model sizes the portions, the code prices them,
# and every one is declared an estimate on the message he confirms.
NB.set_pending(rs_store, {"batch": _meal_batch()})
sent_msgs.clear()
NB.apply_batch_rescale(
    rctx, NB.get_pending(rs_store),
    {"kind": "meal_portions", "items": [{"index": 0, "grams": 300},
                                        {"index": 1, "grams": 150},
                                        {"index": 2, "grams": 40},
                                        {"index": 3, "grams": 200}]},
    TODAY, "token", 1)
sized = NB.get_pending(rs_store)["batch"]
check("a meal-sized offer prices each portion from its own per-100g basis",
      [i["kcal"] for i in sized] == [498.0, 265.5, 17.2, 104.0])
check("which is a real dinner rather than the 447 kcal he was offered",
      800 < sum(i["kcal"] for i in sized) < 1100)
check("EVERY sized portion is flagged as an estimate",
      all(i["portion_estimated"] is True for i in sized))
check("and the offer says so, per component and in the lead",
      "my estimate of the portion" in sent_msgs[-1]
      and "every portion below is my estimate" in sent_msgs[-1])

# A component with no basis at all is NAMED, never silently left at its old figure inside a
# rescaled meal: a wrong total he cannot see is worse than a question.
NB.set_pending(rs_store, {"batch": _meal_batch()[:1] + [
    {"resolved_name": "Homemade sauce", "_raw": "sauce", "kcal": 60.0,
     "confidence": "estimate", "source_rung": "llm"}]})
sent_msgs.clear()
NB.apply_batch_rescale(rctx, NB.get_pending(rs_store),
                       {"kind": "meal_portions",
                        "items": [{"index": 0, "grams": 300}, {"index": 1, "grams": 40}]},
                       TODAY, "token", 1)
check("a component with no basis is named in the reply",
      "could not scale Homemade sauce" in sent_msgs[-1]
      and NB.get_pending(rs_store)["batch"][1]["kcal"] == 60.0)
check("while the ones that could be scaled still were",
      NB.get_pending(rs_store)["batch"][0]["kcal"] == 498.0)
# An item still waiting on its figures has NOTHING to multiply, and the factor branch would
# pass it through untouched - so the reply would have claimed to scale it.
check("and with nothing scalable at all it declines rather than pretending",
      NB.apply_batch_rescale(rctx, {"batch": [{"resolved_name": "x",
                                              "needs_input": True}]},
                             {"kind": "rescale_all", "factor": 2}, TODAY, "token", 1)
      is False)

print("\n--- a pending batch is the ONLY thing a correction can be about ---")
# The wrong-target bug: `batch[0] if len(batch) == 1 else find_entry(day, "")` meant a
# correction aimed at a four-component meal was decided against the last COMMITTED entry.
_ht = inspect.getsource(NB.handle_text)
check("with a batch pending, no committed entry is fetched as the target",
      "None if batch" in _ht and 'else ctx.store.find_entry(day, "") or None' in _ht)
check("and the whole batch is what the model is shown",
      "batch=batch or None" in _ht)
check("the batch kinds are executed before any single-item branch",
      _ht.index('kind in ("rescale_all"') < _ht.index('kind == "rescale" and'))
# apply_quantity_correction had the same trap: with a four-component offer pending it fell
# through to find_entry and would have rescaled something he logged earlier.
qc_store = S.NutritionStore(Path(tempfile.mkdtemp(prefix="nb-qcguard-")))
qctx = FakeCtxCommit(qc_store)
_earlier = qc_store.add_entry(TODAY, raw_text="brookie", resolved_name="Brookie",
                              kcal=450.0, confidence="estimate", source_rung="llm",
                              portion_g=100)
check("a single-item rescale is declined while a whole meal is pending",
      NB.apply_quantity_correction(qctx, {"batch": _meal_batch()}, {"factor": 1.5},
                                   TODAY, "token", 1) is False)
check("and the entry he logged earlier is untouched",
      qc_store.get_day(TODAY)["entries"][0]["kcal"] == 450.0)
check("while a single pending item still rescales as it always did",
      NB.apply_quantity_correction(qctx, {"batch": _meal_batch()[:1]}, {"factor": 1.5},
                                   TODAY, "token", 1) is True)

print("\n--- a correction against HIS figures never re-runs the ladder ---")
# The last door out of the correction branch re-resolves the pending subject's raw text,
# which for a stated offer is his own pasted table - so an unexecuted decision would have
# re-priced the 980 kcal meal all over again, the reported defect one level down.
st_store = S.NutritionStore(Path(tempfile.mkdtemp(prefix="nb-stcorr-")))
sctx = FakeCtxHandle(st_store)
_ladder_calls.clear()
NB.NR.resolve = _exploding_resolve
NB.NLU.interpret = lambda *a, **k: (_ladder_calls.append(("interpret",)) or None)
NB.NLU.classify = lambda text, pending, *a, **k: {"intent": "correction",
                                                 "correction": text}
for _unexecuted in ({"kind": "unclear"},
                    {"kind": "reidentify", "text": "stir fry", "exclusions": []},
                    None):
    NB.set_pending(st_store, {"batch": [NB.stated_item(
        {"text": "large stir-fry bowl", "stated": {"kcal": 980.0, "protein_g": 44.0,
                                                   "components": ["Egg noodles 380 kcal"]}},
        TODAY)]})
    NB.NLU.decide_correction = lambda *a, _d=_unexecuted, **k: _d
    sent_msgs.clear()
    NB.handle_text(sctx, "that's not right", "token", 1)
    check(f"a {_unexecuted['kind'] if _unexecuted else 'failed'} decision does not "
          f"re-resolve his figures",
          _ladder_calls == [] and "will not go looking them up again" in sent_msgs[-1])
    check("and his pending figures are left exactly as he gave them",
          NB.get_pending(st_store)["batch"][0]["kcal"] == 980.0)
NB.NR.resolve, NB.NLU.interpret = _real_resolve, _real_interpret
NB.NLU.classify, NB.NLU.decide_correction = _real_classify, _real_decide

print("\n--- an interpreted portion is HIS or OURS, and the offer says which ---")
# correct_in_batch re-resolves one component by name. A resolved item carries
# portion_used_g, not portion_g, so with the meal now sized this dropped that component
# back to a per-100g basis inside a portioned dinner.
check("re-resolving one component keeps the portion it was sized to",
      'target.get("portion_used_g")' in inspect.getsource(NB.correct_in_batch))
# resolve() takes a caller-supplied portion as stated fact, so a portion the interpreter
# reasoned out for "a large stir fry" would read on the offer exactly like a weight he gave.
_op = inspect.getsource(NB.offer_planned)
check("offer_planned reads the estimated-portion flag off the plan",
      'it.get("portion_estimated")' in _op and "my estimate for a portion" in _op)
check("and never overwrites an assumption resolve already made",
      'not item.get("portion_estimated")' in _op)
check("fmt_confirm states an assumed portion on the line he confirms",
      "assumed" in NB.fmt_confirm(
          {"resolved_name": "Egg noodles", "kcal": 498.0, "source_rung": "cofid",
           "confidence": "label", "portion_estimated": True,
           "portion_assumed": "300 g - my estimate for a portion this size"}))

print("\n--- the pre-send gate: nothing model-derived leaves unread ---")
# Jamie, 14 Aug 2026: "everything should be verified by opus to make sure the output is
# sensible against the input". These check the WIRING; nutrition_gate's own contract is
# tested in test_nutrition_gate.py.
gate_store = S.NutritionStore(Path(tempfile.mkdtemp(prefix="nb-gate-")))
gctx2 = FakeCtxHandle(gate_store)
sent_full = []
_section_send = NB.tg.send
NB.tg.send = lambda token, chat, text, **k: (sent_msgs.append(text),
                                            sent_full.append((text,
                                                              k.get("reply_markup"))))
gate_lines = []
_section_log = NB.log
NB.log = lambda msg: gate_lines.append(str(msg))

STATED = [{"text": "large stir fry with steak and noodles", "portion_g": None,
           "in_session": False, "at": None, "meal": None,
           "stated": {"kcal": 980.0, "protein_g": 44.0, "carb_g": 98.0, "fat_g": 44.0,
                      "basis": "estimate", "components": ["Steak (100g) 220 kcal"]}}]

# 1) A BLOCK. The reply he would have got is replaced by an honest line, the offer survives
#    so a correction can still land on it, and it stops being confirmable.
NB.GATE_RUNNER = gate_says(
    '{"verdict":"block","reason_class":"magnitude",'
    '"reason":"447 kcal is not plausible for that meal",'
    '"fallback":"I could not produce a sensible answer for that - tell me the portions."}')
NB.set_inbound(gctx2, "large stir fry with steak and noodles")
sent_msgs.clear(); sent_full.clear(); gate_lines.clear()
NB.offer_stated(gctx2, STATED, TODAY, "token", 1)
check("a blocked reply is replaced by the gate's fallback",
      len(sent_msgs) == 1 and sent_msgs[-1].startswith("I could not produce"))
check("and the crap it replaced never went out", "980" not in sent_msgs[-1])
check("the block is logged on one line with its reason",
      any(l.startswith("[gate] blocked:") and "not plausible" in l for l in gate_lines))
check("with the verdict, the milliseconds and the reason class",
      any(l.startswith("[gate] block ") and "ms kind=offer" in l and "class=magnitude" in l
          for l in gate_lines))
check("the pending offer is kept intact, so a correction can still land on it",
      (NB.get_pending(gate_store) or {})["batch"][0]["kcal"] == 980.0)
check("it is marked as one he was never shown",
      "not plausible" in NB.get_pending(gate_store)["_gate_blocked"])
check("and it is offered with no Log it button", sent_full[-1][1] is None)

# The reader for that mark. Without it the gate is one "ok" away from being decorative:
# fast_intent sees a pending record and returns confirm, and commit_pending writes the very
# figures the gate called absurd.
sent_msgs.clear()
NB.commit_pending(gctx2, NB.get_pending(gate_store), TODAY, "token", 1)
check("a blocked offer refuses to commit",
      not (gate_store.get_day(TODAY).get("entries") or [])
      and "not logging that one" in sent_msgs[-1])
check("and says so without gating the refusal itself",
      not any("kind=confirmation" in l for l in gate_lines[-2:]))

# 2) A re-offer clears the mark, or a corrected offer he HAS seen would stay unloggable.
NB.GATE_RUNNER = gate_says('{"verdict":"send","reason":"fine"}')
sent_msgs.clear()
NB.offer_stated(gctx2, STATED, TODAY, "token", 1)
check("re-offering clears the block", "_gate_blocked" not in NB.get_pending(gate_store))
check("and a verified offer passes through untouched, figures and button intact",
      "980" in sent_msgs[-1] and sent_full[-1][1] is not None)
NB.commit_pending(gctx2, NB.get_pending(gate_store), TODAY, "token", 1)
check("so the same offer, verified, commits normally",
      (gate_store.get_day(TODAY).get("entries") or [{}])[0].get("kcal") == 980.0)

# 2b) ...but an ANNOTATION on the offer that was just sent must not clear it. This runs
#     after the offer on both replace-an-entry paths, so a stripped mark here would hand the
#     blocked figures back to the next "yes".
NB.GATE_RUNNER = gate_says('{"verdict":"block","reason_class":"magnitude",'
                           '"reason":"implausible","fallback":null}')
sent_msgs.clear()
NB.offer_stated(gctx2, STATED, TODAY, "token", 1)
NB.mark_pending_replaces(gctx2, "some-entry-id", "Brookie")
_after = NB.get_pending(gate_store)
check("noting a replacement keeps the gate's block on that offer",
      _after["_gate_blocked"] == "implausible"
      and _after["_replaces"]["id"] == "some-entry-id")
check("and a block with no fallback of its own still sends an honest line",
      sent_msgs[-1] == NB.NG.built_fallback("magnitude"))

# 3) What the gate is shown. A verifier handed the reply but not the message cannot judge
#    coherence, and one handed no figures cannot judge magnitude.
gate_calls.clear(); sent_msgs.clear()
NB.offer_stated(gctx2, STATED, TODAY, "token", 1)
check("the prompt carries what he said, the exact reply, and the figures behind it",
      len(gate_calls) == 1
      and "large stir fry with steak and noodles" in gate_calls[0]
      and "Log it?" in gate_calls[0] and '"kcal": 980' in gate_calls[0])
check("and the rows he typed himself, which is where an absurd total shows",
      "Steak (100g) 220 kcal" in gate_calls[0])

# 4) FAIL OPEN. The bot must never go mute because the verifier is down.
def gate_down(*a, **k):
    raise FileNotFoundError("no claude binary")


NB.GATE_RUNNER = gate_down
sent_msgs.clear(); gate_lines.clear()
NB.offer_stated(gctx2, STATED, TODAY, "token", 1)
check("an unreachable gate sends the original anyway", "980" in sent_msgs[-1])
check("and says so in the log, which is the only way anyone would know",
      any("[gate] unavailable - sent unverified" in l for l in gate_lines))
check("an unverified offer is still confirmable",
      "_gate_blocked" not in NB.get_pending(gate_store))

# 5) A gate reply trying to write nutrition data into the chat is ignored: the wrapper reads
#    a verdict and a fallback, never a rewrite.
NB.GATE_RUNNER = gate_says(
    '{"verdict":"block","reason_class":"magnitude","reason":"too low",'
    '"corrected_reply":"*Stir fry* 2400 kcal. Log it?","kcal":2400,'
    '"fallback":"That should be about 2,400 kcal."}')
sent_msgs.clear()
NB.offer_stated(gctx2, STATED, TODAY, "token", 1)
check("neither its rewrite nor its figures reach him",
      "2400" not in sent_msgs[-1] and "2,400" not in sent_msgs[-1])
check("he gets the built honest line for the reason class instead",
      sent_msgs[-1] == NB.NG.built_fallback("magnitude"))

# 6) ONE GATE CALL PER INBOUND MESSAGE. The gate costs one Opus call; a path that gates two
#    fragments of the same turn doubles that for nothing, and this is the check that catches
#    a future edit adding a second gated send.
NB.GATE_RUNNER = gate_says('{"verdict":"send","reason":"fine"}')
_real_classify2, _real_interpret2 = NB.NLU.classify, NB.NLU.interpret
_real_resolve2 = NB.NR.resolve
NB.NLU.classify = lambda *a, **k: {
    "intent": "log_food", "items": [{"text": "handful of nuts", "portion_g": None,
                                     "in_session": False}],
    "degraded": True, "nutritionally_trivial": True, "dose_mg": 400.0}
NB.NLU.interpret = lambda *a, **k: None
NB.NR.resolve = lambda text, **k: {
    "resolved_name": "Mixed nuts", "kcal": 180.0, "protein_g": 6.0, "carb_g": 4.0,
    "fat_g": 16.0, "confidence": "estimate", "source_rung": "cofid", "species": [],
    "attempts": [], "degraded": False, "needs_input": False}
gate_calls.clear(); sent_msgs.clear()
NB.handle_text(gctx2, "handful of nuts", "token", 1)
check("a turn that sends three messages still costs exactly one gate call",
      len(gate_calls) == 1 and len(sent_msgs) == 3)
check("and the one that was checked is the offer, not a mechanical note",
      "Mixed nuts" in gate_calls[0])
NB.NLU.classify, NB.NLU.interpret = _real_classify2, _real_interpret2
NB.NR.resolve = _real_resolve2

# 7) THE EXEMPT LIST, asserted rather than described. Each of these is a fixed string or a
#    straight read of the store, with no model or ladder figure in it to be insane - and two
#    of them exist to report that a model call failed, which a broken verifier must not be
#    able to silence.
gate_calls.clear(); sent_msgs.clear()
NB.handle_text(gctx2, "help", "token", 1)
NB.NLU.classify = lambda *a, **k: {"intent": "cancel"}
NB.handle_text(gctx2, "no", "token", 1)
NB.NLU.classify = lambda *a, **k: {"intent": "secret"}
NB.handle_text(gctx2, "sk-" + "x" * 40, "token", 1)
NB.NLU.classify = lambda *a, **k: {"intent": "unknown"}
NB.handle_text(gctx2, "hmmmm", "token", 1)
NB.NLU.classify = _real_classify2
check("help, Dropped it., the credential warning and the fallback are all ungated",
      gate_calls == [] and len(sent_msgs) == 4)
check("and they were sent, not swallowed",
      any("Dropped it." == m for m in sent_msgs)
      and any("could not tell whether that was food" in m for m in sent_msgs))
_gate_src = (BASE / "telegram" / "nutrition_bot.py").read_text()
check("the exemptions are written down where the wiring is",
      "EXEMPT_SENDS" in _gate_src and "Looking at that" in _gate_src)

# 8) No inbound message means nothing to judge coherence AGAINST, so the gate is skipped
#    rather than run on an empty question - and Context outlives a message, so the stash is
#    set on every inbound path including the buttons.
gate_calls.clear(); sent_msgs.clear()
NB.set_inbound(gctx2, "")
NB.offer_stated(gctx2, STATED, TODAY, "token", 1)
check("a send with no inbound message goes out ungated and says so",
      gate_calls == [] and "980" in sent_msgs[-1]
      and any("[gate] skipped: no inbound message" in l for l in gate_lines))
_main_src = inspect.getsource(NB.main)
check("the button path stashes what he tapped, so a commit is judged against that",
      "set_inbound(ctx," in _main_src and "[tapped Log it]" in _main_src)
_photo_src = inspect.getsource(NB.handle_photo)
check("and the photo path stashes the photo and its caption",
      "set_inbound(ctx," in _photo_src)
check("naming what the photo turned out to BE, which a bare marker does not",
      "[sent a photo of a {kind" in _photo_src)
# 9) A reply the gate blocked is not remembered as something the coach said. Stored before
#    the send, it would sit in the transcript as a thread he never saw and the next turn
#    would follow it.
_conv, _deb = inspect.getsource(NB.converse_reply), inspect.getsource(NB.debate)
check("a converse reply reaches the transcript only once it has actually gone out",
      "if send_verified(ctx, token, chat_id, out, kind=\"reply\"):" in _conv
      and _conv.index("send_verified") < _conv.index('_chat(ctx, "coach", out)'))
check("and a debate reply on the same terms",
      "if send_verified(ctx, token, chat_id, reply, kind=\"reply\"):" in _deb
      and _deb.index("send_verified") < _deb.index('_chat(ctx, "coach", reply)'))
check("every path that returns True to handle_text still returns True when blocked",
      # send_verified's False is about WHAT was sent, never about whether the caller
      # handled it: a block that returned False would fall through to a second reply and a
      # second gate call.
      all("if send_verified" not in inspect.getsource(f)
          for f in (NB.apply_retime, NB.apply_rename, NB.apply_meal_correction,
                    NB.apply_batch_rescale, NB.correct_in_batch,
                    NB.apply_quantity_correction)))

print("\n--- REPLAY: the pizza, logged by name and then corrected by its LABEL (14 Aug 2026) ---")
# THE DEFECT, end to end. He logged "Coop Chianti beef pizza" by name and got a web figure of
# 1,147 kcal. He then photographed the pack to correct it - and every label was offered as a
# NEW item, so after rescaling it to the whole pizza he confirmed a SECOND pizza at 964 kcal
# and the duplicate had to be cleared out of his store by hand.
LABEL_PHOTO = {"kind": "nutrition_label", "per": "100g", "portion_g": 200, "kcal": 241,
               "protein_g": 11, "carb_g": 29, "fat_g": 8.5, "fibre_g": 2, "pack_g": 400,
               "product": "Chianti beef pizza, stone baked",
               "ingredients": "wheat flour, mozzarella, beef, tomato"}
_real_read_photo, _real_download = NB.NLU.read_photo, NB.download_photo
_real_label_target = NB.NLU.decide_label_target
_real_write_back, _real_fuel = NB.RC.write_back, NB.RC.bot_in_session_totals
NB.NLU.read_photo = lambda *a, **k: dict(LABEL_PHOTO)
NB.download_photo = lambda ctx, file_id, token: Path("/tmp/nb-test-label.jpg")
# The fuel write-back reaches the coach's session-log, which is not what these checks are
# about and does not exist in a tmpdir.
NB.RC.write_back = lambda *a, **k: {"written": False, "reason": "test"}
NB.RC.bot_in_session_totals = lambda *a, **k: {"carb_g": 0, "sodium_mg": 0}

pz_store = S.NutritionStore(Path(tempfile.mkdtemp(prefix="nb-pizza-")))
pctx = FakeCtxHandle(pz_store)
pizza = pz_store.add_entry(TODAY, raw_text="coop chianti beef pizza",
                           resolved_name="Coop Chianti beef pizza", kcal=1147,
                           protein_g=52, carb_g=130, fat_g=44, confidence="database",
                           source_rung="web")
_label_shown = []
NB.NLU.decide_label_target = lambda label, cands, *a, **k: (
    _label_shown.append(cands) or {"kind": "replace", "entry_id": cands[0]["entry_id"]})
sent_msgs.clear(); gate_calls.clear()
NB.handle_photo(pctx, "file-id", "", TODAY, "token", 1)
check("the label is compared against what is already on today's log",
      _label_shown and _label_shown[0][0]["entry_id"] == pizza["id"]
      and _label_shown[0][0]["kcal"] == 1147.0
      and _label_shown[0][0]["figures_from"] == "web")
_pend = NB.get_pending(pz_store)
check("a label that matches a logged item is offered as a REPLACEMENT",
      (_pend.get("_apply_label_to") or {}).get("id") == pizza["id"])
check("the offer states both figures, so he can see what changes",
      "1147" in sent_msgs[-1] and "482" in sent_msgs[-1]
      and "Replace it?" in sent_msgs[-1])
check("and nothing is written until he says yes",
      len(pz_store.get_day(TODAY)["entries"]) == 1
      and pz_store.get_day(TODAY)["entries"][0]["kcal"] == 1147.0)
check("the offer claims no action, so the gate saw an empty ledger",
      '"actions_this_turn": []' in gate_calls[-1])

# "The whole pizza is two portions" - the rescale that produced the second entry. It has to
# land on the PENDING REPLACEMENT and stay a replacement.
_real_classify3, _real_decide3 = NB.NLU.classify, NB.NLU.decide_correction
NB.NLU.classify = lambda *a, **k: {"intent": "correction",
                                   "correction": "that was the whole pizza, so double it"}
NB.NLU.decide_correction = lambda *a, **k: {"kind": "rescale_factor", "factor": 2.0}
sent_msgs.clear()
NB.handle_text(pctx, "that was the whole pizza, so double it", "token", 1)
_pend = NB.get_pending(pz_store)
check("rescaling the label keeps it a replacement rather than a new item",
      (_pend.get("_apply_label_to") or {}).get("id") == pizza["id"]
      and _pend["batch"][0]["kcal"] == 964.0
      and _pend["batch"][0]["portion_used_g"] == 400.0)
check("and the re-offer says so, so 'Log it' cannot read as a second pizza",
      "replac" in sent_msgs[-1].lower())
check("still nothing written", len(pz_store.get_day(TODAY)["entries"]) == 1)

sent_msgs.clear(); gate_calls.clear()
NB.set_inbound(pctx, "[tapped Log it]")
NB.commit_pending(pctx, NB.get_pending(pz_store), TODAY, "token", 1)
_entries = pz_store.get_day(TODAY)["entries"]
check("confirming leaves ONE pizza, not two", len(_entries) == 1)
check("and it is the entry he logged in the first place, updated",
      _entries[0]["id"] == pizza["id"])
check("at the label's figures, rescaled to the whole pack",
      _entries[0]["kcal"] == 964.0 and _entries[0]["portion_used_g"] == 400.0)
check("with the pack's provenance and its per-100g basis",
      _entries[0]["confidence"] == "label" and _entries[0]["source_rung"] == "manual"
      and _entries[0]["per_100g"]["kcal"] == 241.0)
check("the confirmation is built from the store's result, and claims no new log",
      "Replaced" in sent_msgs[-1] and "One entry, not two" in sent_msgs[-1])
check("and the gate was shown the update the reply claims",
      any("updated entry" in c for c in gate_calls[-1:] if c)
      and '"actions_this_turn": []' not in gate_calls[-1])
check("the pending record is cleared", NB.get_pending(pz_store) is None)
# A replacement OFFER and a label CORRECTION are two different marks on the same record, and
# the label branch returns before commit_pending's own removal. They cannot coexist today -
# offer_label_as_correction writes a fresh record - but an orphan left by a future path would
# sit in the log under a reply that says "One entry, not two".
NB.set_pending(pz_store, {"batch": [{"resolved_name": "old pizza"}],
                          "_replaces": {"id": "x", "name": "y"}})
NB.NLU.decide_label_target = lambda label, cands, *a, **k: {
    "kind": "replace", "entry_id": next(c["entry_id"] for c in cands
                                        if not str(c["entry_id"]).startswith("pending"))}
NB.handle_photo(pctx, "file-id", "", TODAY, "token", 1)
check("offering a label as a correction leaves no replacement mark behind",
      "_replaces" not in (NB.get_pending(pz_store) or {}))
_orphan = pz_store.add_entry(TODAY, raw_text="stray", resolved_name="Stray pizza",
                             kcal=100, confidence="estimate", source_rung="llm")
_both = dict(NB.get_pending(pz_store), _replaces={"id": _orphan["id"], "name": "Stray"})
NB.set_pending(pz_store, _both)
NB.commit_pending(pctx, NB.get_pending(pz_store), TODAY, "token", 1)
check("and if both marks ever did arrive together, the replaced entry still goes",
      all(e["id"] != _orphan["id"] for e in pz_store.get_day(TODAY)["entries"]))
NB.clear_pending(pz_store)

# A LABEL FOR SOMETHING ELSE IS STILL A NEW ITEM, which is the normal case and the safe one.
NB.NLU.decide_label_target = lambda *a, **k: {"kind": "new"}
sent_msgs.clear()
NB.handle_photo(pctx, "file-id", "", TODAY, "token", 1)
_pend = NB.get_pending(pz_store)
check("a label the model calls new is offered as today's own item",
      _pend and "_apply_label_to" not in _pend and len(_pend["batch"]) == 1
      and _pend["batch"][0]["kcal"] == 482.0 and "Log it?" in sent_msgs[-1])
# An unreachable model returns None, and the fallback is the behaviour that existed before
# this path: offer it as new. An outage costs him a correction, never a wrong write.
NB.NLU.decide_label_target = lambda *a, **k: None
NB.clear_pending(pz_store)
NB.handle_photo(pctx, "file-id", "", TODAY, "token", 1)
check("and an unreachable decider offers it as new rather than guessing a replacement",
      "_apply_label_to" not in (NB.get_pending(pz_store) or {}))

# A PENDING ITEM COUNTS. He photographs the pack while the offer is still on the table more
# often than after confirming it, and a second offer there is the same double-log one message
# earlier.
NB.set_pending(pz_store, {"batch": [{"resolved_name": "Chianti beef pizza", "_raw": "pizza",
                                     "kcal": 1147.0, "confidence": "database",
                                     "source_rung": "web", "_meal": "dinner"}]})
NB.NLU.decide_label_target = lambda label, cands, *a, **k: (
    _label_shown.append(cands) or {"kind": "replace", "entry_id": cands[0]["entry_id"]})
_label_shown.clear(); sent_msgs.clear()
NB.handle_photo(pctx, "file-id", "", TODAY, "token", 1)
_pend = NB.get_pending(pz_store)
check("the pending item is offered to the decider with a pending id",
      _label_shown and _label_shown[0][0]["entry_id"] == "pending:0")
check("a label for a pending item replaces ITS figures, still one offer",
      len(_pend["batch"]) == 1 and _pend["batch"][0]["kcal"] == 482.0
      and "_apply_label_to" not in _pend)
check("and the meal he had already named survives the swap",
      _pend["batch"][0]["_meal"] == "dinner")
check("the message says it used the pack's figures rather than offering it twice",
      "rather than offering it twice" in sent_msgs[-1])
NB.clear_pending(pz_store)

print("\n--- REPLAY 15:25: 'you've added the pizza twice' (14 Aug 2026) ---")
# It was decided `unclear`, fell into a re-resolution, and the reply said the duplicate had
# been "noted and removed" while both copies sat in the log and a THIRD was being offered.
dd_store = S.NutritionStore(Path(tempfile.mkdtemp(prefix="nb-dupe-")))
dctx = FakeCtxHandle(dd_store)


def _two_pizzas():
    """The store as it actually was: a web figure and the label copy of the same pizza."""
    for e in list(dd_store.get_day(TODAY).get("entries") or []):
        dd_store.remove_entry(TODAY, e["id"])
    web = dd_store.add_entry(TODAY, raw_text="coop chianti beef pizza",
                             resolved_name="Coop Chianti beef pizza", kcal=1147,
                             confidence="database", source_rung="web")
    lab = dd_store.add_entry(TODAY, raw_text="pizza label",
                             resolved_name="Chianti beef pizza, stone baked", kcal=964,
                             confidence="label", source_rung="manual",
                             source_url="photo of the product label")
    return web, lab


web, lab = _two_pizzas()
check("provenance is read off the entry, label above a web lookup",
      NB.provenance_bucket(lab) == "label" and NB.provenance_bucket(web) == "web"
      and NB.DEDUP_KEEP_ORDER.index("label") < NB.DEDUP_KEEP_ORDER.index("web"))
NB.set_inbound(dctx, "you've added the pizza twice")
sent_msgs.clear(); gate_calls.clear()
check("a duplicate complaint is always handled, never fallen through",
      NB.apply_delete_duplicate(dctx, {"kind": "delete_duplicate", "which": "the pizza"},
                                TODAY, "token", 1) is True)
_left = dd_store.get_day(TODAY)["entries"]
check("one copy is gone", len(_left) == 1)
check("and it is the WORSE one, so the correction he made survives",
      _left[0]["id"] == lab["id"] and _left[0]["kcal"] == 964.0)
check("the reply names what went, what stayed and the new day total",
      "Removed the duplicate" in sent_msgs[-1]
      and "Coop Chianti beef pizza" in sent_msgs[-1]
      and "964 kcal" in sent_msgs[-1] and "Today is now 964 kcal" in sent_msgs[-1])
check("and it is code-built: no model composes a correction outcome",
      "NLU." not in _code_of(NB.apply_delete_duplicate))
check("the removal is on the ledger the gate checks claims against",
      any("removed entry" in a for a in getattr(dctx, "_actions", []))
      and "removed entry" in (gate_calls[-1] or ""))

# AMBIGUITY ASKS, WITH IDS. Three copies, or two different pairs, and picking one would be a
# silent wrong deletion that reads perfectly in the reply.
_two_pizzas()
third = dd_store.add_entry(TODAY, raw_text="another pizza",
                           resolved_name="Chianti beef pizza", kcal=900,
                           confidence="estimate", source_rung="llm")
sent_msgs.clear()
check("three of something asks rather than choosing",
      NB.apply_delete_duplicate(dctx, {"kind": "delete_duplicate", "which": "pizza"},
                                TODAY, "token", 1) is True)
check("nothing was removed", len(dd_store.get_day(TODAY)["entries"]) == 3)
check("and the question carries the ids he can name",
      "not removed anything" in sent_msgs[-1] and third["id"] in sent_msgs[-1]
      and "which id" in sent_msgs[-1].lower())
# Nothing logged twice at all: one message, no re-resolution, and never a fresh offer.
solo = S.NutritionStore(Path(tempfile.mkdtemp(prefix="nb-solo-")))
sctx2 = FakeCtxHandle(solo)
solo.add_entry(TODAY, raw_text="porridge", resolved_name="Porridge", kcal=320,
               confidence="label", source_rung="cofid")
sent_msgs.clear()
check("no duplicate found still returns handled",
      NB.apply_delete_duplicate(sctx2, {"kind": "delete_duplicate", "which": "the pizza"},
                                TODAY, "token", 1) is True)
check("and says what today actually has instead of offering him something",
      "cannot see the same thing logged twice" in sent_msgs[-1]
      and "Porridge" in sent_msgs[-1] and "Log it?" not in sent_msgs[-1])
check("an empty day is answered rather than crashed",
      NB.apply_delete_duplicate(
          FakeCtxHandle(S.NutritionStore(Path(tempfile.mkdtemp(prefix="nb-none-")))),
          {"kind": "delete_duplicate", "which": ""}, TODAY, "token", 1) is True)
# The routing: a delete_duplicate decision must reach the handler and return, offer on the
# table or not, or it falls into the re-resolution that produced the false claim.
check("handle_text dispatches the verb and returns on it",
      'kind == "delete_duplicate"' in inspect.getsource(NB.handle_text)
      and "apply_delete_duplicate(ctx, decision" in inspect.getsource(NB.handle_text))

print("\n--- HIS OWN figures are not quietly overruled by a pack ---")
# The strongest rule in this file is that figures he supplied are never re-priced behind his
# back. A pack IS better data than his reckoning of a plate, and a label correcting a lookup
# is the whole point of the path - but a label landing on numbers he typed has to say so on
# the message he confirms. `manual` covers both his typed figures and a pack he read out, so
# the confidence separates them: his reckoning is an estimate, a pack reading is label data.
check("a lookup's figures are the unremarkable case",
      NB.whose_figures({"source_rung": "web", "confidence": "database"}) == "a lookup"
      and NB._whose_note("a lookup") == "")
check("figures he typed himself are recognised as his",
      NB.whose_figures({"source_rung": "manual", "confidence": "estimate"}) == "your own"
      and NB.whose_figures({"_stated": True}) == "your own")
check("a pack HE read out is label data, not his reckoning",
      NB.whose_figures({"source_rung": "manual", "confidence": "label"}) == "a lookup")
check("and a costed meal is named as the model's table",
      NB.whose_figures({"_composed": True}) == "my costed table"
      and NB.whose_figures({"source_rung": "llm"}) == "my costed table")
his = pz_store.add_entry(TODAY, raw_text="my pizza, about 1100 kcal",
                         resolved_name="Chianti beef pizza (my figures)", kcal=1100,
                         confidence="estimate", source_rung="manual")
NB.NLU.decide_label_target = lambda label, cands, *a, **k: {"kind": "replace",
                                                            "entry_id": his["id"]}
sent_msgs.clear()
NB.handle_photo(pctx, "file-id", "", TODAY, "token", 1)
check("a label offered against his own figures says whose they were",
      "Those were YOUR figures" in sent_msgs[-1]
      and "leave yours exactly as they are" in sent_msgs[-1])
check("and it is still only an offer - nothing overwritten yet",
      pz_store.get_day(TODAY)["entries"][-1]["kcal"] == 1100.0)
NB.clear_pending(pz_store)
# The same on a PENDING item, where his pasted table or a costed meal is what gets swapped.
NB.set_pending(pz_store, {"batch": [{"resolved_name": "Stir-fry bowl", "_raw": "stir fry",
                                     "kcal": 980.0, "_stated": True,
                                     "source_rung": "manual", "confidence": "estimate"}]})
NB.NLU.decide_label_target = lambda label, cands, *a, **k: {"kind": "replace",
                                                            "entry_id": "pending:0"}
sent_msgs.clear()
NB.handle_photo(pctx, "file-id", "", TODAY, "token", 1)
check("swapping a stated offer for a label names whose figures went",
      "Those were YOUR figures" in sent_msgs[-1])
check("and the label item carries none of the old offer's markers, so nothing desyncs",
      all(k not in NB.get_pending(pz_store)["batch"][0]
          for k in ("_stated", "_composed", "_components", "_components_detail")))
NB.clear_pending(pz_store)

print("\n--- an outcome is never CLAIMED without the store result behind it ---")
# The gate is fail-open by design: a slow or unreachable Opus sends the original text. So the
# truth of an action claim cannot rest on the gate - it rests on the confirmation being built
# from what the store actually returned, with the gate as the second net.
# CLAUDE_BIN, not "NLU.": every model call in this file passes the binary, while
# NLU.normalise_meal is a string helper an executor is entitled to use. Banning the module
# would ban the helper and pass a function that spawned a model some other way.
check("no outcome message is composed by a model",
      all("CLAUDE_BIN" not in _code_of(f)
          for f in (NB.apply_delete_duplicate, NB.apply_retime, NB.apply_rename,
                    NB.apply_quantity_correction, NB.apply_meal_correction,
                    NB.commit_pending)))
_two_pizzas()
_real_remove = S.NutritionStore.remove_entry
S.NutritionStore.remove_entry = lambda self, day, entry_id: None
NB.set_inbound(dctx, "you've added the pizza twice")
sent_msgs.clear()
check("a removal that the store refused is still handled",
      NB.apply_delete_duplicate(dctx, {"kind": "delete_duplicate", "which": "the pizza"},
                                TODAY, "token", 1) is True)
check("but nothing claims a removal",
      "Removed" not in sent_msgs[-1] and "could not remove" in sent_msgs[-1]
      and "nothing has changed" in sent_msgs[-1])
check("and the ledger stays empty, so the gate would catch a claim anyway",
      getattr(dctx, "_actions") == [])
S.NutritionStore.remove_entry = _real_remove
_real_apply_label = S.NutritionStore.apply_label_to_entry
S.NutritionStore.apply_label_to_entry = lambda self, day, entry_id, label: None
NB.set_inbound(pctx, "[tapped Replace]")
NB.set_pending(pz_store, {"batch": [{"resolved_name": "Chianti beef pizza", "kcal": 482.0}],
                          "_apply_label_to": {"id": "gone", "name": "Gone", "kcal": 1147}})
sent_msgs.clear()
NB.commit_pending(pctx, NB.get_pending(pz_store), TODAY, "token", 1)
check("a label the store could not apply never reports a replacement",
      "Replaced" not in sent_msgs[-1] and "not there any more" in sent_msgs[-1]
      and getattr(pctx, "_actions") == [])
check("and the offer is cleared rather than left confirmable against a missing entry",
      NB.get_pending(pz_store) is None)
S.NutritionStore.apply_label_to_entry = _real_apply_label

print("\n--- an action claim the code did not perform is blocked (15:25, 14 Aug 2026) ---")
# The gate passed "duplicate noted and removed" because every figure in it was plausible,
# which was all it was judging. It is now shown what the code actually did.
NB.GATE_RUNNER = gate_says('{"verdict":"block","reason_class":"false_claim",'
                           '"reason":"claims a removal that is not in actions_this_turn",'
                           '"fallback":null}')
claim_ctx = FakeCtxHandle(solo)
NB.set_inbound(claim_ctx, "you've added the pizza twice")
gate_calls.clear(); sent_msgs.clear()
_went = NB.send_verified(claim_ctx, "token", 1,
                         "Duplicate noted and removed. You are on 3,050 kcal for the day.",
                         kind="correction")
check("the gate is told the log was not touched this turn",
      '"actions_this_turn": []' in gate_calls[-1])
check("the false claim never reaches him", _went is False
      and "removed" not in sent_msgs[-1])
check("and he gets the honest line for that class instead",
      sent_msgs[-1] == NB.NG.built_fallback("false_claim"))
# The other half: a confirmation of work that DID happen must go out, or the bot apologises
# for a correction it made correctly.
NB.GATE_RUNNER = gate_says('{"verdict":"send","reason":"fine"}')
NB.record_action(claim_ctx, "removed entry 2026-08-10-001 Porridge (320 kcal) as a duplicate")
gate_calls.clear(); sent_msgs.clear()
check("an executed action's confirmation goes out",
      NB.send_verified(claim_ctx, "token", 1, "Removed the duplicate *Porridge*.",
                       kind="correction") is True
      and "Removed the duplicate" in sent_msgs[-1])
check("and the ledger it was checked against named that removal",
      "removed entry 2026-08-10-001" in gate_calls[-1])
# The ledger is per MESSAGE. Context outlives one, so actions left over from the last turn
# would substantiate a false claim in this one.
NB.set_inbound(claim_ctx, "how much protein have I had?")
check("a new inbound message starts with an empty ledger",
      getattr(claim_ctx, "_actions") == [])
check("and every action is recorded where the store call happens, not where the sentence is",
      all("record_action" in inspect.getsource(f)
          for f in (NB.apply_delete_duplicate, NB.apply_retime, NB.apply_rename,
                    NB.apply_quantity_correction, NB.commit_pending,
                    NB.apply_meal_correction)))

NB.NLU.read_photo, NB.download_photo = _real_read_photo, _real_download
NB.NLU.decide_label_target = _real_label_target
NB.NLU.classify, NB.NLU.decide_correction = _real_classify3, _real_decide3
NB.RC.write_back, NB.RC.bot_in_session_totals = _real_write_back, _real_fuel

NB.tg.send, NB.log = _section_send, _section_log
NB.GATE_RUNNER = gate_says('{"verdict":"send","reason":"fine"}')

# Put the real functions back, so anything appended after this block tests the code rather
# than the stubs.
NB.tg.send, NB.publish_now = _REAL["send"], _REAL["publish_now"]
NB.today_block, NB._chat = _REAL["today_block"], _REAL["_chat"]
NB.NR.cache_resolved = _REAL["cache_resolved"]
NB.GATE_RUNNER = _REAL["gate_runner"]
check("the stubs are restored for whatever is appended next",
      NB.tg.send is _REAL["send"] and NB.NR.cache_resolved is _REAL["cache_resolved"])

print("\n--- what he actually eats, with enough about it to CHOOSE ---")
# 13 Aug 2026. "What should I eat?" was answered with the day's remaining kcal and a
# category ("something carb-forward"), because the facts could only support checking a
# suggestion after the fact, never reaching one: foods_he_actually_eats was 25 names and
# their macros, with nothing saying what each food IS or when he eats it. macro_lean and
# usual_meal are computed here, deterministically, so a named meal can answer a named gap.
check("a potato is carbohydrate",
      NB.macro_lean({"carb_g": 60, "protein_g": 5, "fat_g": 1}) == "carb-heavy")
check("tuna is protein",
      NB.macro_lean({"carb_g": 0, "protein_g": 30, "fat_g": 1}) == "protein-heavy")
check("olive oil is fat",
      NB.macro_lean({"carb_g": 0, "protein_g": 0, "fat_g": 14}) == "fat-heavy")
# Shares are of the macros' OWN energy: dividing by a label's stated kcal puts the 50%
# threshold at the mercy of the few per cent by which the two routinely disagree.
check("fat counts at 9 kcal a gram, so 20 g of it beats 30 g of carbohydrate",
      NB.macro_lean({"carb_g": 30, "protein_g": 2, "fat_g": 20}) == "fat-heavy")
check("a mixed meal is not forced into a category",
      NB.macro_lean({"carb_g": 40, "protein_g": 25, "fat_g": 12}) == "mixed")
check("a splash of milk has no character to report",
      NB.macro_lean({"carb_g": 1, "protein_g": 0.7, "fat_g": 0.5}) is None)
check("and an entry with no macros at all does not crash it",
      NB.macro_lean({}) is None and NB.macro_lean({"kcal": 200}) is None)

lever_store = S.NutritionStore(Path(tempfile.mkdtemp(prefix="nb-levers-")))


def _log(day, name, meal, **macros):
    lever_store.add_entry(day, raw_text=name.lower(), resolved_name=name,
                          confidence="label", source_rung="manual",
                          logged_at=f"{day.isoformat()}T19:30", meal=meal, **macros)


# Three weeks of a plausible repertoire: the potato eaten most, at dinner every time.
for n, back in enumerate((14, 11, 8, 5, 2)):
    _log(TODAY - timedelta(days=back), "Jacket potato", "dinner",
         kcal=290, carb_g=62, protein_g=7, fat_g=1, fibre_g=6)
for back in (11, 5):
    _log(TODAY - timedelta(days=back), "Tuna in spring water", "dinner",
         kcal=110, carb_g=0, protein_g=25, fat_g=1)
_log(TODAY - timedelta(days=9), "Peanut butter", "breakfast",
     kcal=190, carb_g=6, protein_g=8, fat_g=16)
# Same food at two different meals, breakfast the more common: the modal slot wins, and it
# must not depend on which one the store happened to write first.
for back in (12, 6):
    _log(TODAY - timedelta(days=back), "Overnight oats", "lunch",
         kcal=380, carb_g=52, protein_g=18, fat_g=10)
for back in (10, 7, 3):
    _log(TODAY - timedelta(days=back), "Overnight oats", "breakfast",
         kcal=380, carb_g=52, protein_g=18, fat_g=10)
# In-session fuel: kept, because it is the right answer to "how do I get carbohydrate in
# before tomorrow", and tagged, because it is the wrong answer to "what is for dinner".
lever_store.add_entry(TODAY - timedelta(days=4), raw_text="gel on the bike",
                      resolved_name="SiS GO gel", kcal=87, carb_g=22, protein_g=0,
                      fat_g=0, confidence="label", source_rung="manual",
                      in_session=True, logged_at=f"{(TODAY - timedelta(days=4)).isoformat()}T10:00")
# Outside the window entirely: three weeks back is deliberate, and 40 days ago is not what
# he eats now.
_log(TODAY - timedelta(days=40), "Christmas pudding", "dinner",
     kcal=400, carb_g=70, protein_g=4, fat_g=12)


class FakeCtxLevers:
    def __init__(self, store):
        self.store = store


levers = NB.eating_levers(FakeCtxLevers(lever_store), TODAY)
by_name = {r["name"]: r for r in levers}
check("the most-eaten food comes first", levers[0]["name"] == "Jacket potato")
check("and it is counted, not just listed", by_name["Jacket potato"]["times"] == 5)
check("a food outside the 21-day window is not in his current repertoire",
      "Christmas pudding" not in by_name)
check("each food says what it mostly IS",
      [by_name[n]["lean"] for n in ("Jacket potato", "Tuna in spring water",
                                    "Peanut butter", "Overnight oats")]
      == ["carb-heavy", "protein-heavy", "fat-heavy", "carb-heavy"])
check("the meal he usually has it at is the modal one, not the first logged",
      by_name["Overnight oats"]["usual_meal"] == "breakfast")
check("a food only ever eaten at one meal reports that meal",
      by_name["Jacket potato"]["usual_meal"] == "dinner")
check("the label figures come through for the swap arithmetic the CODE does",
      (by_name["Tuna in spring water"]["protein_g"],
       by_name["Tuna in spring water"]["kcal"]) == (25, 110))
check("training fuel is present but tagged as fuel, not as a meal",
      by_name["SiS GO gel"]["in_session_fuel"] is True
      and by_name["SiS GO gel"]["usual_meal"] is None)
check("ordinary food is not tagged as fuel",
      by_name["Jacket potato"]["in_session_fuel"] is False)
check("the last time he had it is the LATEST, not the first seen",
      by_name["Jacket potato"]["last_eaten"] == (TODAY - timedelta(days=2)).isoformat())
check("the internal meal tally is not leaked into the prompt",
      all("_meals" not in r for r in levers))
# This list is injected into a prompt verbatim. Two calls that differ mean two different
# prompts from the same log, which is a bug that only ever shows up as an odd reply.
check("the same log produces the same list twice",
      NB.eating_levers(FakeCtxLevers(lever_store), TODAY) == levers)

print("\n--- the facts say WHY, not just how much ---")
# The zones moved to demand-based fuelling: 8-10 g/kg of carbohydrate is not a diet, it is
# tomorrow's long ride arriving. demand_ahead and the basis strings were computed, published
# by the engine, and then dropped on the floor here - so the model could read the number and
# not the reason, and "what should I eat" could only ever come back as a budget report.
class FakeCtxFacts:
    """Everything facts_for_question touches, and nothing else. zones_for returns a REAL
    engine snapshot: a stub dict would let the wiring pass while the values it carries are
    all None, which is the failure this is here to catch."""
    slug = "nobody-real"
    # The plant table is a large file this test has no interest in loading; the species
    # count is not what is under test here.
    table = type("NoSpecies", (), {
        "match_text": staticmethod(lambda text: {"species": [], "unmatched": ""})})()

    def __init__(self, store, zones):
        self.store = store
        self.athlete_dir = store.dir.parent
        self._zones = zones

    def zones_for(self, day):
        return self._zones


facts = NB.facts_for_question(FakeCtxFacts(lever_store, Z_PRELONG), TODAY)
check("demand_ahead reaches the facts with its tier and its sessions intact",
      (facts["demand_ahead"] or {}).get("tier") == NE.DEMAND_LONG
      and facts["demand_ahead"]["when"] == "tomorrow")
check("and it names the window in words the reply can use",
      "long session tomorrow" in (facts["demand_ahead"]["label"] or ""))
check("the carbohydrate g/kg the demand asked for is there to be quoted",
      facts["demand_ahead"]["carb_g_per_kg"] == [8, 10])
check("carb_basis and fat_basis arrive as the engine's own sentences",
      isinstance(facts["carb_basis"], str) and len(facts["carb_basis"]) > 20
      and facts["fat_basis"] == Z_PRELONG["fat_g"]["basis"])
check("each macro carries the basis for its own bound",
      all(facts["macros"][k]["basis"] for k in
          ("protein_g", "carb_g", "fat_g", "fibre_g")))
check("carbohydrate and fat carry their bound and their share of the day's energy",
      all(facts["macros"][k].get("bound") and len(facts["macros"][k]["kcal_share"]) == 2
          for k in ("carb_g", "fat_g")))
check("protein and fibre have neither, and say so by absence rather than by a null",
      all("kcal_share" not in facts["macros"][k] and "bound" not in facts["macros"][k]
          for k in ("protein_g", "fibre_g")))

# THE GAP, computed HERE. The prompt forbids the model doing arithmetic, so a "why" with a
# size in it is only possible if the size is already in the facts.
check("the gap to the bottom of the zone is precomputed for every macro",
      all("gap_to_low_g" in facts["macros"][k] for k in
          ("protein_g", "carb_g", "fat_g", "fibre_g")))
carb = facts["macros"]["carb_g"]
check("with nothing logged the whole zone is still open",
      carb["gap_to_low_g"] == carb["low"] and carb["room_to_high_g"] == carb["high"])
_over = NB.macro_fact({"carb_g": {"low": 300, "high": 400, "bias": "band"}}, 450, "carb_g")
check("past the top of the zone the gap closes to zero and the room goes negative",
      _over["gap_to_low_g"] == 0 and _over["room_to_high_g"] == -50)
_short = NB.macro_fact({"carb_g": {"low": 300, "high": 400, "bias": "band"}}, 180, "carb_g")
check("and short of it the gap is the size of the shortfall",
      _short["gap_to_low_g"] == 120 and _short["room_to_high_g"] == 220)
check("a macro with nothing consumed reports no gap rather than a fabricated one",
      "gap_to_low_g" not in NB.macro_fact({"carb_g": {"low": 300, "high": 400}}, None,
                                          "carb_g"))
# Each bound guarded on its own presence. Defaulting a missing high to zero would report
# "over by 180 g" against a top that does not exist - a breach of nothing, and the model has
# no way to tell it is fictional.
_nohigh = NB.macro_fact({"protein_g": {"low": 150, "bias": "floor"}}, 180, "protein_g")
check("a zone with a floor and no top reports the gap and no room figure",
      _nohigh["gap_to_low_g"] == 0 and "room_to_high_g" not in _nohigh)

# The fibre PHASE, which the prompt has always told the model to respect and which the old
# comprehension dropped: the instruction was unreachable from the facts.
Z_LONGTODAY = NE.zones(day_type="long_ride", rolling_weight=W, rmr=RMR,
                       sessions=[{"type": "Ride", "moving_time": 18000,
                                  "name": "Long endurance ride", "calories": 3000}],
                       calendar_known=True, deficit_enabled=True)
phased = NB.facts_for_question(FakeCtxFacts(lever_store, Z_LONGTODAY), TODAY)["macros"]
check("on a long day the fibre ceiling arrives with the phase that expires it",
      phased["fibre_g"]["after_session"]["low"] > 0
      and "then back to the floor" in phased["fibre_g"]["phase_note"])
check("and on a day with no session of its own there is no phase to report",
      "after_session" not in facts["macros"]["fibre_g"])

# Old snapshots. set_targets stores the zone dict as it was on the day, so a day recorded
# before the demand model existed comes back without any of these keys.
legacy = NB.macro_fact({}, 100, "carb_g")
check("a snapshot from before the demand model degrades instead of raising",
      legacy["low"] is None and "gap_to_low_g" not in legacy)


class FakeCtxLegacy(FakeCtxFacts):
    def zones_for(self, day):
        return {"day_type": "standard", "kcal_target": 3000, "kcal_maintenance": 3000,
                "deficit_applied_kcal": 0, "kcal_confidence": "estimate",
                "weight_basis_kg": W}


old = NB.facts_for_question(FakeCtxLegacy(lever_store, None), TODAY)
check("and the whole facts dict still builds, with the why simply absent",
      old["demand_ahead"] is None and old["carb_basis"] is None
      and old["macros"]["carb_g"]["low"] is None)

print()
if FAILED:
    print(f"{len(FAILED)} FAILED: " + ", ".join(FAILED)); sys.exit(1)
print("all checks passed")
