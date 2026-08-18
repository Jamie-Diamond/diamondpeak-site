#!/usr/bin/env python3
"""Offline tests for lib/nutrition_resolve.py. No network: every rung is injected.
Run: python3 ClaudeCoach/scripts/test_nutrition_resolve.py

The failure mode that matters is silent degradation - the ladder quietly resolving
one rung lower than it claims. Most of these checks are about that being visible.
"""
import sys
import tempfile
from datetime import date
from pathlib import Path

_here = Path(__file__).resolve().parent
for cand in (_here / "lib", _here.parent / "lib"):
    if (cand / "nutrition_resolve.py").exists():
        sys.path.insert(0, str(cand))
        break
import nutrition_resolve as R
import nutrition_store as S
import plants as P

FAILED = []


def check(name, cond):
    print(("PASS" if cond else "FAIL"), name)
    if not cond:
        FAILED.append(name)


TODAY = date(2026, 8, 10)
TABLE = P.SpeciesTable()
# Most tests inject every rung explicitly, so CoFID is stubbed empty to keep the
# local table from silently answering and masking which rung is under test.
EMPTY_COFID = R.CofidTable(data={"foods": []})


def new_store():
    return S.NutritionStore(Path(tempfile.mkdtemp(prefix="nut-res-")))


def outcome(item, rung):
    return next((a["outcome"] for a in item["attempts"] if a["rung"] == rung), None)


RETAILER_HIT = {"kcal": 235, "protein_g": 6.0, "carb_g": 6.0, "fat_g": 21.0,
                "fibre_g": 3.0, "dietary_sodium_mg": 45,
                "resolved_name": "M&S Nut Collection",
                "ingredients": "almonds, cashew nuts, hazelnuts, brazil nuts, sea salt",
                "source_url": "https://www.marksandspencer.com/x"}
OFF_HIT = {"kcal": 240, "protein_g": 7.0, "carb_g": 7.0, "fat_g": 20.0,
           "resolved_name": "Generic mixed nuts", "source_url": "https://off/x"}
LLM_HIT = {"kcal": 250, "protein_g": 8.0, "carb_g": 8.0, "fat_g": 19.0,
           "resolved_name": "mixed nuts (estimated)"}

# 1) The ladder prefers the retailer and labels it correctly.
st = new_store()
item = R.resolve("M&S nut collection 75g", day=TODAY, store=st, portion_g=37.5,
                 table=TABLE, cofid=EMPTY_COFID,
                 fetchers={R.Rung.RETAILER: lambda t, p: RETAILER_HIT,
                           R.Rung.OFF: lambda t, p: OFF_HIT,
                           R.Rung.LLM: lambda t, p: LLM_HIT})
check("retailer rung wins when it hits", item["source_rung"] == R.Rung.RETAILER)
check("retailer resolution is label confidence", item["confidence"] == "label")
check("lower rungs are not even attempted", outcome(item, R.Rung.OFF) is None)
check("cache miss is recorded, not skipped", outcome(item, R.Rung.CACHE) == "miss")
check("macros come through", item["kcal"] == 235 and item["dietary_sodium_mg"] == 45)
# Species come from the INGREDIENTS, not the name. "M&S nut collection" tags zero
# species off its name alone, which is how this was found.
# Species are stored as {"id", "score"}: the score is the one MATCHED, so a refined
# derivative keeps its 0 instead of being read back as the category default.
sp_ids = {s["id"] for s in item["species"]}
check("species are tagged from the ingredients list",
      {"prunus_dulcis", "anacardium_occidentale", "corylus_avellana",
       "bertholletia_excelsa"} <= sp_ids)
check("each species carries the score it was matched at",
      all("score" in s for s in item["species"]))
check("the item records that species came from ingredients",
      item["species_from"] == "ingredients")
check("a composite product name alone yields no species",
      TABLE.match_text("M&S nut collection")["species"] == [])
check("not degraded when the top rung works", item["degraded"] is False)

# 2) An ABSENT retailer hook is reported as not_configured on every item, so the
#    gap is visible rather than looking like normal operation.
item2 = R.resolve("mixed nuts", day=TODAY, store=new_store(), table=TABLE,
                  cofid=EMPTY_COFID,
                  fetchers={R.Rung.OFF: lambda t, p: OFF_HIT,
                            R.Rung.LLM: lambda t, p: LLM_HIT})
check("the retailer rung is not walked at all now, so it logs nothing",
      outcome(item2, R.Rung.RETAILER) is None)
check("an unbuilt rung is NOT counted as degradation", item2["degraded"] is False)
check("it falls to Open Food Facts", item2["source_rung"] == R.Rung.OFF)
check("OFF resolution is database confidence", item2["confidence"] == "database")

# 3) A FAILING retailer rung IS degradation, and says which rung failed.
def boom(t, p):
    raise TimeoutError("retailer timed out")


item3 = R.resolve("mixed nuts", day=TODAY, store=new_store(), table=TABLE,
                  cofid=EMPTY_COFID,
                  fetchers={R.Rung.RETAILER: boom,
                            R.Rung.OFF: lambda t, p: OFF_HIT,
                            R.Rung.LLM: lambda t, p: LLM_HIT})
check("a failed preferred rung marks the result degraded", item3["degraded"] is True)
check("the error is recorded with its type",
      "TimeoutError" in (next(a["detail"] for a in item3["attempts"]
                              if a["rung"] == R.Rung.RETAILER)))
check("it still resolves rather than giving up", item3["source_rung"] == R.Rung.OFF)
check("the reply text admits a better source failed",
      "failed" in R.describe_provenance(item3))

# 4) The LLM is LAST, and only reached when everything above misses.
item4 = R.resolve("something homemade", day=TODAY, store=new_store(), table=TABLE,
                  cofid=EMPTY_COFID,
                  fetchers={R.Rung.RETAILER: lambda t, p: None,
                            R.Rung.OFF: lambda t, p: None,
                            R.Rung.LLM: lambda t, p: LLM_HIT})
check("LLM is reached only after the others miss", item4["source_rung"] == R.Rung.LLM)
check("LLM resolution is estimate confidence", item4["confidence"] == "estimate")
check("retailer and OFF misses are both logged",
      outcome(item4, R.Rung.RETAILER) == "no_match"
      and outcome(item4, R.Rung.OFF) == "no_match")
check("the reply states the uncertainty on an estimate",
      "10-15%" in R.describe_provenance(item4))
check("an estimate never describes itself as a listing",
      "retailer" not in R.describe_provenance(item4))

# 5) Total failure returns a usable record that ASKS, never zeroes.
st5 = new_store()
item5 = R.resolve("utterly unknown thing", day=TODAY, store=st5, table=TABLE,
                  cofid=EMPTY_COFID,
                  fetchers={R.Rung.RETAILER: lambda t, p: None,
                            R.Rung.OFF: lambda t, p: None,
                            R.Rung.LLM: lambda t, p: None})
check("total failure sets needs_input", item5["needs_input"] is True)
check("macros are None, never a confident zero",
      all(item5[f] is None for f in R.MACRO_FIELDS))
check("the unresolved string is queued for review",
      len(__import__("json").loads(
          (st5.dir / "unresolved.json").read_text())) == 1)
check("the reply asks instead of reporting a number",
      "?" in R.describe_provenance(item5))

# 6) Cache: label and database results are cached, estimates never are.
st6 = new_store()
lab = R.resolve("m&s nut collection", day=TODAY, store=st6, table=TABLE,
                cofid=EMPTY_COFID,
                fetchers={R.Rung.RETAILER: lambda t, p: RETAILER_HIT})
R.cache_resolved(st6, lab)
check("a label resolution is cached", st6.cache_get("m&s nut collection", on=TODAY))
est = R.resolve("guessy thing", day=TODAY, store=st6, table=TABLE,
                cofid=EMPTY_COFID,
                fetchers={R.Rung.LLM: lambda t, p: LLM_HIT})
R.cache_resolved(st6, est)
check("an LLM estimate is NOT cached, or one guess is re-served for a year",
      st6.cache_get("guessy thing", on=TODAY) is None)
R.cache_resolved(st6, item5)
check("a needs_input record is not cached",
      st6.cache_get("utterly unknown thing", on=TODAY) is None)

# 7) A cache hit short-circuits the ladder and inherits the original confidence.
item7 = R.resolve("m&s nut collection", day=TODAY, store=st6, table=TABLE,
                  cofid=EMPTY_COFID,
                  fetchers={R.Rung.RETAILER: boom, R.Rung.OFF: lambda t, p: OFF_HIT})
check("a cache hit is used", item7["source_rung"] == R.Rung.CACHE)
check("the cache hit inherits label confidence", item7["confidence"] == "label")
check("no network rung is touched on a cache hit",
      outcome(item7, R.Rung.RETAILER) is None)
check("a cache hit is not degraded even though the retailer would have failed",
      item7["degraded"] is False)

# 8) A stale cache entry is a MISS. Retailers reformulate.
st8 = new_store()
st8.cache_put("old biscuit", {"kcal": 100, "resolved_at": "2023-01-01",
                              "confidence": "label"})
# The stub name must plausibly BE the query, as a real fetcher's would: the ladder now
# checks relevance on every candidate, so an unrelated stub name is correctly rejected.
item8 = R.resolve("old biscuit", day=TODAY, store=st8, table=TABLE,
                  cofid=EMPTY_COFID,
                  fetchers={R.Rung.OFF: lambda t, p: dict(OFF_HIT,
                                                          resolved_name="Old Biscuit")})
check("a stale cache entry is bypassed", item8["source_rung"] == R.Rung.OFF)
check("the miss says why", "older than" in
      next(a["detail"] for a in item8["attempts"] if a["rung"] == R.Rung.CACHE))

# 9) Sodium confidence is tracked separately, because it resolves worst.
check("sodium confidence matches the rung when sodium is present",
      item["sodium_confidence"] == "label")
check("sodium confidence is unknown when the source carried none",
      item2["sodium_confidence"] == "unknown")

# 10) Per-100g scaling, and the OFF sodium unit trap (grams, not the salt field).
scaled = R._scale({"kcal": 600, "protein_g": 20, "fibre_g": None}, 50)
check("per-100g figures scale to the portion", scaled["kcal"] == 300)
check("absent fields stay absent rather than becoming zero", "fibre_g" not in scaled)


parsed = R._scale({"kcal": 500}, 100)
check("scaling with a 100g portion is identity", parsed["kcal"] == 500)

# 11) Guard rails on the shaping function itself.
try:
    R._finalise({}, "x", "not_a_rung", "label", [], None, TODAY, False)
    check("a bad rung is rejected", False)
except ValueError:
    check("a bad rung is rejected", True)
try:
    R._finalise({}, "x", R.Rung.LLM, "vibes", [], None, TODAY, False)
    check("a bad confidence is rejected", False)
except ValueError:
    check("a bad confidence is rejected", True)

# 12) resolved_at is stamped from the LOCAL day passed in, never from today().
item12 = R.resolve("mixed nuts", day=date(2026, 7, 1), store=new_store(), table=TABLE,
                   cofid=EMPTY_COFID, fetchers={R.Rung.OFF: lambda t, p: OFF_HIT})
check("resolved_at uses the supplied local day", item12["resolved_at"] == "2026-07-01")

# 13) CoFID: the UK composition tables, local, no network.
COFID = R.CofidTable()
check(f"CoFID seed loaded ({len(COFID.foods)} names)", COFID.available)
oats = COFID.lookup("porridge oats", 100)
check("CoFID resolves a whole food", oats is not None and oats["kcal"] == 379)
check("CoFID names the published entry, not the query",
      oats["resolved_name"].startswith("Oats"))
half = COFID.lookup("porridge oats", 50)
check("CoFID scales to the portion", half["kcal"] == 189.5)
check("CoFID sodium is already mg per 100g, scaled not converted",
      COFID.lookup("cheddar", 100)["dietary_sodium_mg"] == 723)
check("CoFID refuses a nonsense query rather than fuzzy-matching",
      COFID.lookup("zzzz unknowable zzzz") is None)

# CoFID is auto-wired when the table is present, and outranks USDA and OFF.
st13 = new_store()
item13 = R.resolve("porridge oats", day=TODAY, store=st13, portion_g=80, table=TABLE,
                   fetchers={R.Rung.OFF: lambda t, p: OFF_HIT,
                             R.Rung.LLM: lambda t, p: LLM_HIT})
check("CoFID is wired in automatically from the local table",
      item13["source_rung"] == R.Rung.COFID)
check("CoFID carries label confidence, not database",
      item13["confidence"] == "label")
check("CoFID outranks Open Food Facts", outcome(item13, R.Rung.OFF) is None)
check("CoFID cites the dataset", "CoFID" in R.describe_provenance(item13))

# A branded item the retailer would own still falls past CoFID correctly.
item13b = R.resolve("zzz branded thing zzz", day=TODAY, store=new_store(), table=TABLE,
                    fetchers={R.Rung.OFF: lambda t, p: dict(
                        OFF_HIT, resolved_name="ZZZ Branded Thing")})
check("CoFID misses on a branded product and the ladder continues",
      item13b["source_rung"] == R.Rung.OFF
      and outcome(item13b, R.Rung.COFID) == "no_match")

# 14) Keyed rungs return None rather than raising when unkeyed, so an unconfigured
#     key never shows up as degradation.
check("USDA without a key returns None",
      R.usda_fetch("oats", 100, api_key="") is None)
check("Nutritionix without keys returns None",
      R.nutritionix_fetch("oats", 100, app_id="", app_key="") is None)

# 15) THE UNIT TRAPS. Every one of these APIs reports sodium differently, and each
#     mix-up is a silent multi-fold error rather than an exception.
usda_payload = {"foods": [{"description": "Oats, raw", "dataType": "SR Legacy",
                           "fdcId": 1, "foodNutrients": [
                               {"nutrientId": 1008, "value": 379},
                               {"nutrientId": 1093, "value": 6}]}]}
saved_get = R._get_json
try:
    R._get_json = lambda *a, **k: usda_payload
    u = R.usda_fetch("oats", 50, api_key="TEST")
    check("USDA sodium is mg per 100g, scaled only", u["dietary_sodium_mg"] == 3)
    check("USDA kcal scales to the portion", u["kcal"] == 189.5)

    off_payload = {"products": [{"product_name": "Oats", "brands": "X",
                                 "energy-kcal_100g": 379, "sodium_100g": 0.003,
                                 "ingredients_text": "wholegrain oats", "url": "u",
                                 "code": "1"}]}
    R._get_json = lambda *a, **k: off_payload
    o = R.off_fetch("oats", 100)
    check("OFF sodium is GRAMS per 100g and is converted to mg",
          o["dietary_sodium_mg"] == 3)
    check("OFF ingredients are captured for species tagging",
          o["ingredients"] == "wholegrain oats")

    nx_payload = {"foods": [{"food_name": "toast", "nf_calories": 160,
                             "nf_sodium": 300, "nf_protein": 6}]}
    R._get_json = lambda *a, **k: nx_payload
    n = R.nutritionix_fetch("two slices of toast", 200,
                            app_id="a", app_key="b")
    check("Nutritionix is NOT scaled by portion_g, it parses the portion itself",
          n["kcal"] == 160)
    check("Nutritionix sodium passes through as mg", n["dietary_sodium_mg"] == 300)

    R._get_json = lambda *a, **k: {"products": [{"product_name": "Empty",
                                                 "energy-kcal_100g": None}]}
    check("OFF skips products carrying a name but no macros",
          R.off_fetch("nothing", 100) is None)
finally:
    R._get_json = saved_get

# 16) ladder_status reports EVERY rung, so "off by default" is visible rather than
#     looking like a missing feature.
status = R.ladder_status(fetchers={R.Rung.OFF: lambda t, p: None})
check("an injected optional rung reports ready", status[R.Rung.OFF] == "ready")
check("the retailer rung reads off_by_default",
      status[R.Rung.RETAILER] == "off_by_default")
check("CoFID is ready from the local table", status[R.Rung.COFID] == "ready")
check("every rung is accounted for", set(status) == set(R.FULL_ORDER))

# The default ladder is the pruned one, and an injected optional rung JOINS it at its
# proper place rather than being ignored - enabling a rung that then does nothing is the
# same class of bug as a parameter nobody passes.
check("default ladder is cofid, web, llm",
      R.LADDER == (R.Rung.COFID, R.Rung.WEB, R.Rung.LLM))
eff = R.effective_ladder({R.Rung.OFF: lambda q, p: None})
check(f"an injected OFF joins the walk in order (got {eff})",
      eff.index(R.Rung.OFF) > eff.index(R.Rung.COFID)
      and eff.index(R.Rung.OFF) < eff.index(R.Rung.WEB))
check("nothing injected means the pruned ladder is walked",
      R.effective_ladder({}) == (R.Rung.COFID, R.Rung.WEB, R.Rung.LLM))

# 17) THE RELEVANCE GUARD. "400mg of my protein collagen capsules" resolved to "Soy
#     protein isolate" from USDA: a product he never ate, with confident macros, a
#     database badge, and a soy plant species tagged against it. USDA matched on the
#     word "protein" and the fetcher took the first hit carrying an energy figure.
check("the real failure is now rejected",
      R._relevant("400mg of my protein collagen capsules", "Soy protein isolate") is False)
check("a genuine collagen product is accepted",
      R._relevant("collagen capsules", "Collagen peptides, bovine") is True)
for q, n in (("porridge oats", "Oats, porridge, raw"),
             ("cottage cheese", "COTTAGE CHEESE"),
             ("M&S nut collection", "Nut Collection"),
             ("twix xtra", "TWIX Xtra Caramel Cookie Bars"),
             ("rubicon spring orange and mango", "Rubicon Spring Orange & Mango")):
    check(f"still accepts a real match: {q[:26]}", R._relevant(q, n) is True)
for q, n in (("sis immune tab", "Soy protein isolate"),
             ("chicken breast", "Whey protein powder"),
             ("beetroot shot", "Chicken, breast, raw")):
    check(f"rejects an unrelated match: {q[:26]}", R._relevant(q, n) is False)
check("'protein' alone can never be the shared token",
      R._relevant("protein bar", "protein isolate") is False)
check("an empty query does not reject everything",
      R._relevant("", "anything at all") is True)

# The guard is applied at the rung, not just available as a helper.
saved = R._get_json
try:
    R._get_json = lambda *a, **k: {"foods": [
        {"description": "Soy protein isolate", "dataType": "SR Legacy", "fdcId": 1,
         "foodNutrients": [{"nutrientId": 1008, "value": 335}]}]}
    check("USDA skips an irrelevant hit rather than returning it",
          R.usda_fetch("collagen capsules", 100, api_key="T") is None)
    R._get_json = lambda *a, **k: {"products": [
        {"product_name": "Soy protein isolate", "brands": "X",
         "energy-kcal_100g": 335, "url": "u", "code": "1"}]}
    check("OFF skips an irrelevant hit too",
          R.off_fetch("collagen capsules", 100) is None)
finally:
    R._get_json = saved

# 18) Species must not be credited from a mismatched product NAME.
res = R._finalise({"resolved_name": "Soy protein isolate", "kcal": 1},
                  "400mg of my protein collagen capsules", R.Rung.USDA, "database",
                  [], TABLE, TODAY, degraded=False)
check("no soy species is credited from a wrong product name",
      "glycine_max" not in {s["id"] for s in res["species"]})
check("species come from the raw text when there are no ingredients",
      res["species_from"] == "name")

# 19) INTERPRET FIRST, RESOLVE SECOND (Jamie's design). The ladder now searches the
#     interpreted terms and validates each hit against the stated FORM, which is the
#     structural version of a guard that hand-written word lists kept failing at.
CAP = {"form": "capsule", "category": "supplement", "search_terms": ["collagen peptides"]}
BAR = {"form": "bar", "category": "branded_packaged", "search_terms": ["protein bar"]}
MEAL = {"form": "prepared_meal", "category": "branded_packaged"}
check("a capsule expectation rejects a protein bar",
      R._hint_conflict(CAP, "COLLAGEN PROTEIN BAR, LEMON COOKIE") is True)
check("and accepts a real collagen supplement",
      R._hint_conflict(CAP, "Collagen peptides, hydrolysed") is False)
check("powder is the same family as capsule, so no conflict",
      R._hint_conflict(CAP, "Magnesium citrate powder") is False)
check("a bar expectation rejects capsules",
      R._hint_conflict(BAR, "Collagen capsules 400mg") is True)
check("and accepts the bar", R._hint_conflict(BAR, "COLLAGEN PROTEIN BAR") is False)
check("a meal expectation rejects tablets",
      R._hint_conflict(MEAL, "Vitamin D tablets") is True)
check("no hint means no conflict", R._hint_conflict({}, "anything") is False)

# The ladder must SEARCH the interpreted terms, not the raw sentence, and record a
# wrong-form rejection rather than silently accepting or silently missing.
seen = []


def spy(q, p):
    seen.append(q)
    return {"kcal": 300, "resolved_name": "COLLAGEN PROTEIN BAR, LEMON COOKIE"}


it = R.resolve("400mg of my protein collagen capsules", day=TODAY, store=new_store(),
               table=TABLE, cofid=EMPTY_COFID, hint=CAP,
               queries=["collagen peptides", "hydrolysed collagen"],
               fetchers={R.Rung.OFF: spy})
check(f"searched the interpreted terms, not the sentence (got {seen})",
      seen and "collagen peptides" in seen[0] and "400mg" not in seen[0])
check("tried the fallback term too after the first was rejected", len(seen) == 2)
check("a wrong-form hit is recorded as wrong_form",
      outcome(it, R.Rung.OFF) == "wrong_form")
check("and the wrong product is NOT returned",
      "BAR" not in (it.get("resolved_name") or "").upper())

# A rung may declare its own confidence: the web rung is label data on a manufacturer
# page and an estimate otherwise.
web_label = R.resolve("twix xtra", day=TODAY, store=new_store(), table=TABLE,
                      cofid=EMPTY_COFID,
                      fetchers={R.Rung.WEB: lambda q, p: {
                          "kcal": 370, "resolved_name": "Twix Xtra",
                          "source_kind": "manufacturer", "confidence": "label"}})
check("a manufacturer page counts as label data",
      web_label["source_rung"] == R.Rung.WEB and web_label["confidence"] == "label")
web_est = R.resolve("something vague", day=TODAY, store=new_store(), table=TABLE,
                    cofid=EMPTY_COFID,
                    fetchers={R.Rung.WEB: lambda q, p: {
                        "kcal": 200, "resolved_name": "guess",
                        "source_kind": "estimate", "confidence": "estimate"}})
check("a web guess stays an estimate", web_est["confidence"] == "estimate")
check("an invalid declared confidence falls back to the rung default",
      R.resolve("x", day=TODAY, store=new_store(), table=TABLE, cofid=EMPTY_COFID,
                fetchers={R.Rung.WEB: lambda q, p: {
                    "kcal": 1, "resolved_name": "x", "confidence": "nonsense"}}
                )["confidence"] == "database")
check("web sits before the bare llm estimate in the ladder",
      R.LADDER.index(R.Rung.WEB) < R.LADDER.index(R.Rung.LLM))

# 20) CoFID IS A WHOLE-FOOD TABLE. Letting it answer branded products turned three of
#     eight real items into single ingredients wearing label confidence: an M&S satay
#     chicken pack became "Chicken, breast, skinless, raw" at 106 kcal, the same for the
#     bang bang pack, and an overnight-oats pot became "Oats, porridge, raw" at 379. All
#     matched on ONE word. The exact failure USDA was dropped for, in the rung I kept.
C = R.CofidTable()
for q in ("porridge oats", "chicken breast", "cheddar", "blueberries"):
    check(f"CoFID still answers a whole food: {q}", C.lookup(q, 100) is not None)
for q in ("M&S Satay Chicken with Black Rice & Mango",
          "satay chicken with black rice and mango",
          "bang bang chicken with satay dip",
          "salted caramel overnight oats",
          "M&S Cookies and Cream Protein Bar"):
    check(f"CoFID refuses a branded dish: {q[:34]}", C.lookup(q, 100) is None)

# And the ladder skips it entirely by CATEGORY, so it cannot even be asked.
skipped = R.resolve("M&S Satay Chicken with Black Rice & Mango", day=TODAY,
                    store=new_store(), table=TABLE,
                    hint={"category": "branded_packaged", "form": "prepared_meal"},
                    fetchers={R.Rung.WEB: lambda q, p: {
                        "kcal": 339, "resolved_name": "M&S Satay Chicken",
                        "source_kind": "retailer", "confidence": "label"}})
check("a branded product skips CoFID by category",
      outcome(skipped, R.Rung.COFID) == "skipped")
check("and resolves on the web rung instead", skipped["source_rung"] == R.Rung.WEB)
check("the skip says why", "whole-food table" in next(
    a["detail"] for a in skipped["attempts"] if a["rung"] == R.Rung.COFID))
whole = R.resolve("porridge oats", day=TODAY, store=new_store(), table=TABLE,
                  hint={"category": "whole_food", "form": "whole_food"},
                  fetchers={})
check("a whole food still reaches CoFID", whole["source_rung"] == R.Rung.COFID)
check("with no hint at all CoFID is still tried",
      R.resolve("porridge oats", day=TODAY, store=new_store(), table=TABLE,
                fetchers={})["source_rung"] == R.Rung.COFID)

# 21) IDENTITY COVERAGE. "butter" resolved to "Peanut butter, smooth" six times on 12 Aug
#     2026, twice after he had said "I never said peanut butter". Coverage was only ever
#     tested one way round - how much of the QUERY the row explained - so a single-token
#     query matched any row containing that token.
BUTTER_ONLY = R.CofidTable(data={"foods": [
    {"name": "Peanut butter, smooth", "aliases": ["peanut butter"],
     "kcal": 609, "protein_g": 22.6, "carb_g": 13.1, "fat_g": 51.8}]})
check("a one-word query no longer matches a two-word identity",
      BUTTER_ONLY.lookup("butter", 100) is None)
check("and the food he DID name still hits, on the alias",
      (BUTTER_ONLY.lookup("peanut butter", 100) or {})["resolved_name"]
      == "Peanut butter, smooth")
check("the same holds for the published name",
      BUTTER_ONLY.lookup("peanut butter, smooth", 100) is not None)
check("a portion word does not smuggle the query past the identity rule",
      BUTTER_ONLY.lookup("one teaspoon of butter", 100) is None)
check("nor does the shipped table answer 'butter' with peanut butter",
      "peanut" not in ((C.lookup("butter", 100) or {}).get("resolved_name") or "").lower())
# The identity rule must not be what rejects a query that NAMES the food and only ADDS
# qualifiers. A variety name ("medium pink lady apple") is refused by the older coverage
# cap, not by this rule - see the note beside COFID_MAX_UNEXPLAINED_TOKENS - and that is
# left alone deliberately: widening the cap is what let a Wagamama rice bowl match raw
# brown rice.
check("a variety-qualified query satisfies the identity rule",
      R._tokens("Apple") <= R._tokens("medium pink lady apple"))
check("identity does not reject a query that adds one qualifier",
      R._tokens("Apple") <= R._tokens("medium pink apple"))
for q in ("porridge oats", "brown rice", "chicken breast", "greek yoghurt",
          "a handful of almonds", "half a large banana", "extra virgin olive oil",
          "wholemeal bread", "tinned chickpeas", "baby spinach"):
    check(f"identity coverage does not cost an existing hit: {q[:26]}",
          C.lookup(q, 100) is not None)
check("and the composite dish is still refused",
      C.lookup("satay chicken with black rice and mango", 100) is None)

# 22) A REJECTED CANDIDATE IS A MISS, on every rung. Corrections carried no memory, so a
#     re-resolution walked the same deterministic ladder and returned the same wrong
#     product - which is why saying it twice changed nothing.
PB = {"kcal": 609, "resolved_name": "Peanut butter, smooth", "protein_g": 22.6}
SALTED = {"kcal": 744, "resolved_name": "Butter, salted", "protein_g": 0.6}
ex = R.resolve("butter", day=TODAY, store=new_store(), table=TABLE, cofid=EMPTY_COFID,
               exclude=["peanut butter"],
               fetchers={R.Rung.OFF: lambda t, p: PB,
                         R.Rung.WEB: lambda t, p: dict(SALTED, source_kind="manufacturer",
                                                       confidence="label")})
check("an excluded candidate is skipped and the ladder continues",
      ex["source_rung"] == R.Rung.WEB and ex["resolved_name"] == "Butter, salted")
check("the skip is recorded as excluded_by_athlete, not as a miss",
      outcome(ex, R.Rung.OFF) == "excluded_by_athlete")
check("the attempt says which phrase ruled it out",
      "peanut butter" in next(a["detail"] for a in ex["attempts"]
                              if a["outcome"] == "excluded_by_athlete"))
check("with no exclusions the same fetcher still wins",
      R.resolve("butter", day=TODAY, store=new_store(), table=TABLE, cofid=EMPTY_COFID,
                fetchers={R.Rung.OFF: lambda t, p: PB})["resolved_name"]
      == "Peanut butter, smooth")
# THE OPPOSITE SIGN OF THE SAME BUG. Blocking the thing he actually ate would be no better
# than serving the thing he did not.
check("'peanut butter' does NOT block plain butter",
      R._excluded_by("Butter, salted", ["peanut butter"]) == "")
check("'peanut butter' blocks the smooth peanut butter row",
      R._excluded_by("Peanut butter, smooth", ["peanut butter"]) == "peanut butter")
check("an empty exclusion list blocks nothing",
      R._excluded_by("Peanut butter, smooth", []) == "")
# The CACHE is the rung most likely to hold what he just rejected: it is keyed on his own
# words, so a wrong answer once confirmed is exactly what gets re-served.
st22 = new_store()
st22.cache_put("butter", {"kcal": 609, "resolved_name": "Peanut butter, smooth",
                          "confidence": "label", "resolved_at": TODAY.isoformat()})
cached = R.resolve("butter", day=TODAY, store=st22, table=TABLE, cofid=EMPTY_COFID,
                   exclude=["peanut butter"],
                   fetchers={R.Rung.WEB: lambda t, p: dict(SALTED, confidence="label",
                                                           source_kind="manufacturer")})
check("an excluded CACHE hit is bypassed too",
      cached["source_rung"] == R.Rung.WEB
      and outcome(cached, R.Rung.CACHE) == "excluded_by_athlete")

# 23) DEFAULT PORTIONS. A per-100g label with no derivable pack size dead-ended at kcal
#     None, so "one teaspoon of butter" was answered with "how much, in grams?" - a question
#     he had already answered in the units that food is eaten in.
PER_100 = {"needs_portion": True, "resolved_name": "Butter, salted",
           "per_100g": {"kcal": 744, "protein_g": 0.6, "carb_g": 0.6, "fat_g": 82.2},
           "confidence": "label", "source_kind": "manufacturer"}
tsp = R.resolve("one teaspoon of butter", day=TODAY, store=new_store(), table=TABLE,
                cofid=EMPTY_COFID, fetchers={R.Rung.WEB: lambda t, p: PER_100})
check("a teaspoon is assumed rather than asked about", tsp["needs_input"] is False)
check("the per-100g figures are scaled to 5 g", tsp["kcal"] == 37.2)
check("the assumption is flagged on the item", tsp["portion_estimated"] is True)
check("the portion used is recorded", tsp["portion_used_g"] == 5.0)
check("the assumption is stated in words for the offer",
      "teaspoon" in tsp["portion_assumed"] and "5 g" in tsp["portion_assumed"])
check("an assumed AMOUNT does not downgrade the label figures",
      tsp["confidence"] == "label")
check("the assumption is recorded in the attempt log",
      outcome(tsp, R.Rung.WEB) == "portion_assumed")
# A count is honoured, or two slices of toast log as one.
two = R.resolve("two slices of wholemeal bread", day=TODAY, store=new_store(), table=TABLE,
                cofid=EMPTY_COFID,
                fetchers={R.Rung.WEB: lambda t, p: dict(
                    PER_100, resolved_name="Bread, wholemeal",
                    per_100g={"kcal": 217})})
check("a stated count multiplies the unit default", two["portion_used_g"] == 72.0)
# A fraction is a count too. "half a large banana" is the query this table is FOR, and
# reading it as a whole one doubles the entry.
check("half of something is half the default",
      R.default_portion_g("half a slice of toast", "Bread, wholemeal") == 18.0)
check("and it reads back as half, not as 0.5 slices",
      "half" in R._default_portion("half a slice of toast", "Bread, wholemeal")[1])
# THE ASSUMPTION HAS TO SURVIVE THE CACHE. The cache payload is an allowlist, so without
# the portion keys the second time he says "a teaspoon of butter" the cache hit renders as
# plain label data and the assumption is dropped in silence - which is the one thing the
# defaults were allowed on condition of never doing.
st23 = new_store()
R.cache_resolved(st23, R.resolve("one teaspoon of butter", day=TODAY, store=st23,
                                table=TABLE, cofid=EMPTY_COFID,
                                fetchers={R.Rung.WEB: lambda t, p: PER_100}))
again = R.resolve("one teaspoon of butter", day=TODAY, store=st23, table=TABLE,
                  cofid=EMPTY_COFID, fetchers={})
check("the second time is a cache hit", again["source_rung"] == R.Rung.CACHE)
check("and the cached hit still declares the assumption",
      again.get("portion_estimated") is True
      and "teaspoon" in (again.get("portion_assumed") or ""))
check("a measured resolution caches no assumption flag",
      "portion_estimated" not in (st6.cache_get("m&s nut collection", on=TODAY) or {}))
# And where nothing applies, the behaviour is unchanged: it ASKS.
ask = R.resolve("that pot of skyr", day=TODAY, store=new_store(), table=TABLE,
                cofid=EMPTY_COFID,
                fetchers={R.Rung.WEB: lambda t, p: dict(PER_100,
                                                        resolved_name="Skyr, natural")})
check("with no unit word it still asks rather than guessing",
      ask["needs_portion"] is True and ask["needs_input"] is True)
check("and its macros stay None rather than a 100 g guess",
      all(ask[f] is None for f in R.MACRO_FIELDS))
for raw, want in (("one teaspoon of butter", 5.0), ("a tbsp of peanut butter", 15.0),
                  ("a knob of butter", 10.0), ("a handful of almonds", 30.0),
                  ("a small banana", 90.0), ("a medium banana", 118.0),
                  ("a large banana", 136.0), ("an apple", 180.0),
                  ("a medium orange", 130.0), ("an egg", 50.0)):
    check(f"default portion for {raw!r} is {want} g",
          R.default_portion_g(raw, "") == want)
for raw in ("a handful of chicken", "a slice of ham", "chicken breast", "bananas",
            "that pot of the new stuff"):
    check(f"no default is invented for {raw!r}", R.default_portion_g(raw, "") is None)
check("a slice is bread-sized once the row names bread",
      R.default_portion_g("a slice of it", "Bread, wholemeal") == 36.0)

# 24) A MACRO HE STATED WITHOUT A KCAL FIGURE (17 Aug 2026). "chicken salad with 21g
#     protein" gives one number and describes the rest, and the whole block was thrown
#     away because it carried no total - so the ladder answered with a table's protein
#     where he had given his own. Athlete-stated figures are law: the ladder still runs,
#     and his figures go over the top of what it finds.
SALAD = {"kcal": 300, "protein_g": 12.0, "carb_g": 30.0, "fat_g": 9.0, "fibre_g": 4.0,
         "dietary_sodium_mg": 400, "resolved_name": "Chicken salad",
         "source_url": "https://example/salad"}


def stated_outcomes(item):
    return [a["outcome"] for a in item["attempts"] if a["outcome"] == "stated_override"]


part = R.resolve("chicken salad with 21g protein", day=TODAY, store=new_store(),
                 table=TABLE, cofid=EMPTY_COFID,
                 fetchers={R.Rung.WEB: lambda t, p: dict(SALAD)},
                 stated={"protein_g": 21})
check("his stated protein beats the lookup's", part["protein_g"] == 21.0)
check("the macros he said nothing about are left alone",
      part["kcal"] == 300 and part["carb_g"] == 30.0 and part["fat_g"] == 9.0
      and part["fibre_g"] == 4.0)
check("which figures were his is recorded on the item",
      part["stated_fields"] == ["protein_g"])
check("and the overlay is in the attempt log, not just in the numbers",
      stated_outcomes(part) == ["stated_override"])
check("the rung is still the rung that answered", part["source_rung"] == R.Rung.WEB)
# Precedent is the assumed-portion guard, which does not downgrade because the figures are
# still the source's. It runs the other way too: one figure of his off a pack does not make
# the database's kcal and carbs label data.
check("an overlaid figure does not move the confidence", part["confidence"] == "database")
label_basis = R.resolve("chicken salad with 21g protein", day=TODAY, store=new_store(),
                        table=TABLE, cofid=EMPTY_COFID,
                        fetchers={R.Rung.WEB: lambda t, p: dict(SALAD)},
                        stated={"protein_g": 21, "basis": "label"})
check("nor does his saying he read it off a pack",
      label_basis["confidence"] == "database" and "basis" not in label_basis)
# SODIUM IS THE ONE FIGURE THE OVERLAY COULD OVERSTATE. It carries its own confidence
# because it is the figure that goes wrong quietly, and a label-grade CoFID row that
# returned no sodium at all would otherwise have lent its grade to his own reckoning.
salty = R.resolve("chicken salad, about 800mg of salt in it", day=TODAY,
                  store=new_store(), table=TABLE, cofid=EMPTY_COFID,
                  fetchers={R.Rung.COFID: lambda t, p: dict(SALAD)},
                  stated={"dietary_sodium_mg": 800})
check("a sodium figure of his is his own reckoning, not the rung's grade",
      salty["confidence"] == "label" and salty["sodium_confidence"] == "estimate"
      and salty["dietary_sodium_mg"] == 800.0)
check("and a lookup's own sodium is graded as it always was",
      R.resolve("chicken salad with 21g protein", day=TODAY, store=new_store(),
                table=TABLE, cofid=EMPTY_COFID,
                fetchers={R.Rung.WEB: lambda t, p: dict(SALAD,
                                                        dietary_sodium_mg=400)},
                stated={"protein_g": 21})["sodium_confidence"] == "database")
check("he reads on the confirm line which figure was his",
      "your own figure: protein 21 g" in R.describe_provenance(part))
# NO ATWATER. A kcal computed from his protein would be indistinguishable, a week later,
# from a total he gave - the same objection this file already makes to rounding his rows.
missed = R.resolve("something homemade with 21g protein", day=TODAY, store=new_store(),
                   table=TABLE, cofid=EMPTY_COFID, fetchers={},
                   stated={"protein_g": 21})
check("with every rung missing, his figure is still the one figure there is",
      missed["protein_g"] == 21.0)
check("and no kcal is invented from it", missed["kcal"] is None)
check("the item still asks, because a protein figure is not a meal",
      missed["needs_input"] is True)
check("and the question says what it already has",
      "protein 21 g" in R.describe_provenance(missed))
# Read back in the order the macros are quoted in, not alphabetically: "sodium, protein"
# is not how anybody says it, and this list is a sentence he reads.
two_of_his = R.resolve("mystery bar", day=TODAY, store=new_store(), table=TABLE,
                       cofid=EMPTY_COFID, fetchers={},
                       stated={"dietary_sodium_mg": 800, "protein_g": 21})
check("several figures of his read back in macro order",
      two_of_his["stated_fields"] == ["protein_g", "dietary_sodium_mg"]
      and "protein 21 g, sodium 800 mg" in R.describe_provenance(two_of_his))
# The needs_portion dead end: the lookup is per-100g and unusable until he says how much,
# but what he stated is for the food he actually ate and does not depend on that answer.
NO_SIZE = {"needs_portion": True, "resolved_name": "Skyr, natural",
           "per_100g": {"kcal": 63, "protein_g": 10.3}, "confidence": "label",
           "source_kind": "manufacturer"}
asked = R.resolve("that pot of skyr, 21g protein in it", day=TODAY, store=new_store(),
                  table=TABLE, cofid=EMPTY_COFID,
                  fetchers={R.Rung.WEB: lambda t, p: dict(NO_SIZE)},
                  stated={"protein_g": 21})
check("a portion question does not blank the figure he gave",
      asked["protein_g"] == 21.0 and asked["needs_portion"] is True)
check("and the rest of the row is still cleared, as it was",
      asked["kcal"] is None and asked["carb_g"] is None)
# A zero is two different things depending on the field. Zero energy is an absence and
# would wipe a real lookup; zero fat is something he can truthfully say about a food.
zeroes = R.resolve("chicken salad", day=TODAY, store=new_store(), table=TABLE,
                   cofid=EMPTY_COFID, fetchers={R.Rung.WEB: lambda t, p: dict(SALAD)},
                   stated={"kcal": 0, "fat_g": 0})
check("a stated zero kcal is dropped rather than zeroing the lookup",
      zeroes["kcal"] == 300 and "kcal" not in zeroes["stated_fields"])
check("a stated zero for another macro is a figure and is kept",
      zeroes["fat_g"] == 0.0 and "fat_g" in zeroes["stated_fields"])
check("a negative figure is dropped, never clamped",
      R.resolve("chicken salad", day=TODAY, store=new_store(), table=TABLE,
                cofid=EMPTY_COFID, fetchers={R.Rung.WEB: lambda t, p: dict(SALAD)},
                stated={"protein_g": -4})["protein_g"] == 12.0)
check("a field that is not a macro is not smuggled onto the item",
      "components" not in R.resolve(
          "chicken salad", day=TODAY, store=new_store(), table=TABLE, cofid=EMPTY_COFID,
          fetchers={R.Rung.WEB: lambda t, p: dict(SALAD)},
          stated={"components": ["a row"], "protein_g": 21}))
# HIS FIGURE MUST NOT BE CACHED. The cache is keyed on his words and stamped with the
# rung's confidence, so caching this would re-serve his 21 g for a year against sentences
# in which he stated nothing - the same objection that keeps LLM estimates out of it.
st24 = new_store()
R.cache_resolved(st24, R.resolve("chicken salad with 21g protein", day=TODAY, store=st24,
                                 table=TABLE, cofid=EMPTY_COFID,
                                 fetchers={R.Rung.WEB: lambda t, p: dict(SALAD)},
                                 stated={"protein_g": 21}))
check("an overlaid resolution is never written to the cache",
      st24.cache_get("chicken salad with 21g protein", on=TODAY) is None)
# And where he states nothing the item is exactly what it was, with no new key on it -
# the marker is what the cache and the confirm line branch on.
st24c = new_store()
plain = R.resolve("chicken salad", day=TODAY, store=st24c, table=TABLE,
                  cofid=EMPTY_COFID, fetchers={R.Rung.WEB: lambda t, p: dict(SALAD)})
check("an ordinary resolution carries no stated marker",
      "stated_fields" not in plain and not stated_outcomes(plain))
check("nothing of his is claimed on its confirm line",
      "your own" not in R.describe_provenance(plain))
R.cache_resolved(st24c, plain)
check("and it still caches, as it always did",
      (st24c.cache_get("chicken salad", on=TODAY) or {}).get("protein_g") == 12.0)
# A CACHE HIT IS A RUNG LIKE ANY OTHER, and it is the rung most likely to answer the
# second time he says this. An overlay that only ran on a fresh lookup would honour his
# figure today and quietly drop it tomorrow.
st24b = new_store()
R.cache_resolved(st24b, R.resolve("chicken salad", day=TODAY, store=st24b, table=TABLE,
                                  cofid=EMPTY_COFID,
                                  fetchers={R.Rung.WEB: lambda t, p: dict(SALAD)}))
cached_part = R.resolve("chicken salad", day=TODAY, store=st24b, table=TABLE,
                        cofid=EMPTY_COFID, fetchers={}, stated={"protein_g": 21})
check("a cache hit takes the overlay too",
      cached_part["source_rung"] == R.Rung.CACHE and cached_part["protein_g"] == 21.0
      and cached_part["kcal"] == 300)

# 25) THE CACHE KEY (18 Aug 2026). It was his sentence, lower-cased, so a photographed
#     Co-op cookie label saved on 16 Aug under "One cookie" was unreachable the next day
#     when the same biscuit was "a whole matcha cookie". Every rephrasing re-walked the
#     ladder and re-guessed a label we already had, which is how a cookie took 17 hours,
#     several photos and a hand-backfill to log.
COOKIE = {"kcal": 470, "protein_g": 5.0, "carb_g": 55.0, "fat_g": 25.0,
          "resolved_name": "Co-op Matcha & White Chocolate Cookie",
          "confidence": "label", "source_kind": "manufacturer",
          "source_url": "https://coop.co.uk/x"}
st25 = new_store()
R.cache_resolved(st25, R.resolve("One cookie", day=TODAY, store=st25, table=TABLE,
                                 cofid=EMPTY_COFID,
                                 fetchers={R.Rung.WEB: lambda t, p: COOKIE}))
# THE ROW IS FILED UNDER THE PRODUCT, and his words are an alias pointing at it - so the
# commonest case, saying the same thing again, is still one dict hop.
keys25 = dict(st25.cache_rows(on=TODAY))
check("the payload is keyed on the product identity and the amount",
      list(keys25) == ["chocolate cookie matcha white#x1"])
check("his exact words still reach it", (st25.cache_get("one cookie", on=TODAY)
                                         or {}).get("kcal") == 470)
check("an alias is a pointer, not a second copy of the macros",
      __import__("json").loads((st25.dir / "cache.json").read_text())["one cookie"]
      == {"alias_of": "chocolate cookie matcha white#x1"})
# THE BUG ITSELF. `boom` on the rung that would otherwise answer, as in test 7: a
# non-degraded CACHE hit is the only way this can come back.
rephrased = R.resolve("a whole matcha cookie", day=TODAY, store=st25, table=TABLE,
                      cofid=EMPTY_COFID, fetchers={R.Rung.WEB: boom})
check("a rephrasing of the same product now hits the cache",
      rephrased["source_rung"] == R.Rung.CACHE and rephrased["kcal"] == 470)
check("and it short-circuits the ladder rather than surviving a failure",
      rephrased["degraded"] is False)
check("the log says it was matched on the product, not on his words",
      "matched on the product" in
      next(a["detail"] for a in rephrased["attempts"] if a["rung"] == R.Rung.CACHE))
same = R.resolve("One cookie", day=TODAY, store=st25, table=TABLE, cofid=EMPTY_COFID,
                 fetchers={R.Rung.WEB: boom})
check("an exact repeat still hits, and says it matched his own words",
      same["source_rung"] == R.Rung.CACHE
      and "matched on the product" not in
      next(a["detail"] for a in same["attempts"] if a["rung"] == R.Rung.CACHE))
# NO MIGRATION. Every row in a cache file written before this is a payload keyed on his
# words - and it carries the resolved_name, so the identity search finds it where it lies.
# Old data gets better rather than merely surviving.
st25old = new_store()
st25old.dir.mkdir(parents=True, exist_ok=True)
(st25old.dir / "cache.json").write_text(__import__("json").dumps(
    {"one cookie": {"kcal": 470, "confidence": "label", "resolved_at": TODAY.isoformat(),
                    "resolved_name": "Co-op Matcha & White Chocolate Cookie"}}))
check("a pre-existing flat cache file needs no migration to be rephrased into",
      R.resolve("a whole matcha cookie", day=TODAY, store=st25old, table=TABLE,
                cofid=EMPTY_COFID,
                fetchers={R.Rung.WEB: boom})["source_rung"] == R.Rung.CACHE)
check("and it still answers the words it was saved under",
      R.resolve("one cookie", day=TODAY, store=st25old, table=TABLE, cofid=EMPTY_COFID,
                fetchers={R.Rung.WEB: boom})["source_rung"] == R.Rung.CACHE)


def saved(name, raw="whatever", kcal=200, at=None):
    """A store holding one saved resolution, written the way cache_resolved writes."""
    st = new_store()
    st.dir.mkdir(parents=True, exist_ok=True)
    primary, aliases = R.cache_keys(name, raw)
    st.cache_put(primary, {"kcal": kcal, "resolved_name": name, "confidence": "label",
                           "amount_key": R._amount_key(raw),
                           "resolved_at": (at or TODAY).isoformat()}, aliases=aliases)
    return st


def cache_rung(st, phrase, **kw):
    return R.resolve(phrase, day=TODAY, store=st, table=TABLE, cofid=EMPTY_COFID,
                     fetchers={R.Rung.WEB: boom}, **kw)["source_rung"]


# THE HEAD NOUN IS WHAT STOPS THIS BECOMING THE 12 AUG BUG AGAIN. Containment alone
# accepts "peanut butter" against a saved peanut butter BAR - same tokens, one a subset
# of the other, "protein" a stopword - and hands back a bar's macros wearing the label
# confidence the bar's own label earned. English puts the product type last, and that is
# the only thing telling the two apart.
check("a saved BAR does not answer a question about the butter",
      cache_rung(saved("Peanut Butter Protein Bar", "a peanut butter bar"),
                 "peanut butter") != R.Rung.CACHE)
check("a saved SANDWICH does not answer a question about the salad",
      cache_rung(saved("Chicken Salad Sandwich", "a chicken salad sandwich"),
                 "chicken salad") != R.Rung.CACHE)
check("one identifying word is never enough to reach a product he did not name",
      cache_rung(saved("Peanut butter, smooth", "peanut butter"), "butter")
      != R.Rung.CACHE)
check("but the product he DID name still answers",
      cache_rung(saved("Peanut butter, smooth", "peanut butter"), "smooth peanut butter")
      == R.Rung.CACHE)
# HOW MUCH is in the key, because the payload is a resolution of a PORTION. Identity
# alone would have filed these as one row and answered either question with the other's
# figures - a silent trebling, which is worse than the miss this whole change is about.
st25oats = new_store()
for text, kcal in (("50g of porridge oats", 190), ("150g of porridge oats", 570)):
    R.cache_resolved(st25oats, R.resolve(
        text, day=TODAY, store=st25oats, table=TABLE, cofid=EMPTY_COFID,
        fetchers={R.Rung.WEB: lambda t, p, _k=kcal: {
            "kcal": _k, "resolved_name": "Porridge oats", "confidence": "label",
            "source_kind": "manufacturer"}}))
check("two portions of one food are two rows, not one overwriting the other",
      len(st25oats.cache_rows(on=TODAY)) == 2)
check("and each amount gets its own figures back",
      [R.resolve(t, day=TODAY, store=st25oats, table=TABLE, cofid=EMPTY_COFID,
                 fetchers={R.Rung.WEB: boom})["kcal"]
       for t in ("50g of porridge oats", "150g of porridge oats")] == [190, 570])
check("a count he stated is part of the amount too, or two cookies log as one",
      cache_rung(saved("Co-op Matcha & White Chocolate Cookie", "one cookie"),
                 "two matcha cookies") != R.Rung.CACHE)
check("and a bare plural is a count QUESTION, not a licence to serve the one-cookie row",
      cache_rung(saved("Co-op Matcha & White Chocolate Cookie", "one cookie"),
                 "some matcha cookies") != R.Rung.CACHE)
# THE PLURAL GUARD HAS TO HOLD ON THE EXACT-IDENTITY PATH TOO, which is easier to reach
# than it looks: "Co-op Cookie" keys as `cookie#x1` because "co" and "op" are too short to
# identify anything, and "cookies" reduces to the same identity.
check("a plural does not slip through the exact-identity lookup either",
      cache_rung(saved("Co-op Cookie", "one cookie"), "cookies") != R.Rung.CACHE)
# HOW MUCH, read off a sentence that also carries a time or a figure. Settling for the
# default at the first number it sees filed "half a cookie" as one whole cookie - the
# amount key's own version of the silent doubling it exists to prevent.
for said, want in (("at 1350, half a cookie", "x0.5"), ("400 kcal, two portions", "x2"),
                   ("a cookie at 9:30", "x1"), ("150g of porridge oats", "150g x1"),
                   ("a big bowl of porridge", "big x1"), ("two slices of toast", "x2")):
    check(f"the amount in {said!r} reads as {want!r}", R._amount_key(said) == want)
check("while the singular he saved it under still answers",
      cache_rung(saved("Co-op Matcha & White Chocolate Cookie", "one cookie"),
                 "a matcha cookie") == R.Rung.CACHE)
check("and so is a size word, which the tokeniser drops as noise",
      cache_rung(saved("Porridge with berries", "a small bowl of porridge"),
                 "a big bowl of porridge") != R.Rung.CACHE)
# AMBIGUITY IS A MISS. Two saved products that both fit means we do not know which he
# ate, and picking by dict order would be a coin toss wearing label confidence.
st25two = new_store()
for name in ("Co-op Matcha White Chocolate Cookie", "Tesco Matcha Oat Cookie"):
    primary, aliases = R.cache_keys(name, "a cookie")
    st25two.cache_put(primary, {"kcal": 470, "resolved_name": name, "amount_key": "x1",
                                "confidence": "label",
                                "resolved_at": TODAY.isoformat()}, aliases=aliases)
amb = R.resolve("a matcha cookie", day=TODAY, store=st25two, table=TABLE,
                cofid=EMPTY_COFID, fetchers={R.Rung.WEB: boom})
check("two saved products that both fit is a miss, not a guess",
      amb["source_rung"] != R.Rung.CACHE
      and "ambiguous" in next(a["detail"] for a in amb["attempts"]
                              if a["rung"] == R.Rung.CACHE))
# A rejection still bypasses the cache, and now does so however he worded it.
check("something he ruled out today is still bypassed under the new key",
      outcome(R.resolve("smooth peanut butter", day=TODAY, table=TABLE,
                        cofid=EMPTY_COFID, exclude=["peanut butter"],
                        store=saved("Peanut butter, smooth", "peanut butter"),
                        fetchers={R.Rung.WEB: lambda t, p: dict(
                            SALTED, confidence="label", source_kind="manufacturer")}),
              R.Rung.CACHE) == "excluded_by_athlete")

# 26) WHAT MAY BE CACHED DID NOT CHANGE, and the check has to be made against the NEW
#     key. Asserting only that his sentence misses would pass even if the payload had
#     been written under the product key, which is precisely the row that is now
#     reachable from the most phrasings. So: nothing at all may be in the file.
st26 = new_store()
R.cache_resolved(st26, R.resolve("guessy thing", day=TODAY, store=st26, table=TABLE,
                                 cofid=EMPTY_COFID,
                                 fetchers={R.Rung.LLM: lambda t, p: LLM_HIT}))
check("an LLM estimate writes NO row under any key, old or new",
      st26.cache_rows(on=TODAY) == []
      and st26.cache_get("guessy thing", on=TODAY) is None
      and st26.cache_get(R.cache_keys("mixed nuts (estimated)", "guessy thing")[0],
                         on=TODAY) is None)
st26b = new_store()
overlaid = R.resolve("chicken salad with 21g protein", day=TODAY, store=st26b,
                     table=TABLE, cofid=EMPTY_COFID, stated={"protein_g": 21},
                     fetchers={R.Rung.WEB: lambda t, p: dict(SALAD)})
R.cache_resolved(st26b, overlaid)
check("a figure of his writes NO row under any key either",
      st26b.cache_rows(on=TODAY) == []
      and st26b.cache_get(R.cache_keys(overlaid["resolved_name"],
                                       overlaid["raw_text"])[0], on=TODAY) is None)
check("so no rephrasing can pull his one-off figure back out",
      R.resolve("that chicken salad", day=TODAY, store=st26b, table=TABLE,
                cofid=EMPTY_COFID,
                fetchers={R.Rung.WEB: lambda t, p: dict(SALAD)})["source_rung"]
      != R.Rung.CACHE)
st26c = new_store()
R.cache_resolved(st26c, R.resolve("utterly unknown thing", day=TODAY, store=st26c,
                                  table=TABLE, cofid=EMPTY_COFID,
                                  fetchers={R.Rung.WEB: lambda t, p: None}))
check("and a needs_input record writes nothing either", st26c.cache_rows(on=TODAY) == [])

print()
if FAILED:
    print(f"{len(FAILED)} FAILED: " + ", ".join(FAILED))
    sys.exit(1)
print("all checks passed")
