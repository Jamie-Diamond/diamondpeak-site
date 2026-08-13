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
# to be guaranteed, and the meal branch dereferences it.
check("the meal branch still checks it has an entry to file",
      'and not pend and target_item:' in inspect.getsource(NB.handle_text))
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
         "_chat": NB._chat}
NB.tg.send = lambda token, chat, text, **k: sent_msgs.append(text)
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

# Put the real functions back, so anything appended after this block tests the code rather
# than the stubs.
NB.tg.send, NB.publish_now = _REAL["send"], _REAL["publish_now"]
NB.today_block, NB._chat = _REAL["today_block"], _REAL["_chat"]
NB.NR.cache_resolved = _REAL["cache_resolved"]
check("the stubs are restored for whatever is appended next",
      NB.tg.send is _REAL["send"] and NB.NR.cache_resolved is _REAL["cache_resolved"])

print()
if FAILED:
    print(f"{len(FAILED)} FAILED: " + ", ".join(FAILED)); sys.exit(1)
print("all checks passed")
