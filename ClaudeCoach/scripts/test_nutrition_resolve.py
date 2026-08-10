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
check("species are tagged from the ingredients list",
      {"prunus_dulcis", "anacardium_occidentale", "corylus_avellana",
       "bertholletia_excelsa"} <= set(item["species"]))
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
check("absent retailer hook reads not_configured",
      outcome(item2, R.Rung.RETAILER) == "not_configured")
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
item8 = R.resolve("old biscuit", day=TODAY, store=st8, table=TABLE,
                  cofid=EMPTY_COFID, fetchers={R.Rung.OFF: lambda t, p: OFF_HIT})
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
                    fetchers={R.Rung.OFF: lambda t, p: OFF_HIT})
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

# 16) ladder_status makes an unbuilt ladder visible without reading an item.
status = R.ladder_status(fetchers={R.Rung.OFF: lambda t, p: None})
check("status reports a wired rung ready", status[R.Rung.OFF] == "ready")
check("status reports the retailer gap", status[R.Rung.RETAILER] == "not_configured")
check("status reports CoFID ready from the local table",
      status[R.Rung.COFID] == "ready")
check("every ladder rung is accounted for", set(status) == set(R.LADDER))

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
      "glycine_max" not in res["species"])
check("species come from the raw text when there are no ingredients",
      res["species_from"] == "name")

print()
if FAILED:
    print(f"{len(FAILED)} FAILED: " + ", ".join(FAILED))
    sys.exit(1)
print("all checks passed")
