#!/usr/bin/env python3
"""Golden fixtures: real items, recorded responses, expected outcomes. No network.
Run: python3 ClaudeCoach/scripts/test_nutrition_golden.py

WHY THIS EXISTS
On 10 Aug 2026 the resolution path broke five times in a row and each break was found by
RUNNING it against Jamie's real day, one bug at a time, over several hours:

  1. USDA name-matched "collagen capsules" to "Soy protein isolate"
  2. the supplement intent routed but the item still went through the food ladder and
     matched a COLLAGEN PROTEIN BAR
  3. CoFID matched the single word "chicken" and turned two M&S prepared meals into raw
     chicken breast, and an overnight-oats pot into raw porridge oats
  4. re-resolving from the already-resolved name lost the portion and doubled two items
  5. the matched species SCORE was discarded on write, so refined derivatives read back as
     whole plants
  6. a Deliveroo screenshot of a Wagamama order was logged as "Rice, brown, raw", because
     CoFID's two-shared-token bar is easy to clear by accident (rice + brown) and because
     the photo path dropped the hint that would have skipped CoFID entirely

Every one produced a plausible number rather than an error. A fixture file turns each into
a one-second check: the recorded response goes in, the expected verdict comes out, and a
change that reintroduces any of them fails here rather than in a live re-run.

The responses are RECORDED, not live. That is the point: the test is deterministic, runs
offline, and pins the behaviour rather than the internet.
"""

import sys
from datetime import date
from pathlib import Path

_here = Path(__file__).resolve().parent
for cand in (_here / "lib", _here.parent / "lib"):
    if (cand / "nutrition_resolve.py").exists():
        sys.path.insert(0, str(cand))
        break
import nutrition_nlu as NLU  # noqa: E402
import nutrition_resolve as NR  # noqa: E402
import plants as PL  # noqa: E402

BASE = _here.parent


def load_bot():
    """Import the bot module so its CALL SITES can be exercised, not just the library.

    Every fixture here used to call NR.resolve directly, which is exactly why the photo
    path could drop its hint and still show a green run: the bug was in the caller."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "nb", BASE / "telegram" / "nutrition_bot.py")
    nb = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(nb)
    return nb

FAILED = []


def check(name, cond, detail=""):
    print(("PASS" if cond else "FAIL"), name, (f"  {detail}" if not cond and detail else ""))
    if not cond:
        FAILED.append(name)


TODAY = date(2026, 8, 10)
TABLE = PL.SpeciesTable()
EMPTY_COFID = NR.CofidTable(data={"foods": []})
REAL_COFID = NR.CofidTable()

# --- recorded responses, verbatim in shape from the real sources -------------

REC = {
    # The USDA hit that started it. Real response shape, real wrong product.
    "usda_soy": {"kcal": 335.0, "resolved_name": "Soy protein isolate",
                 "ingredients": "soy protein isolate", "source_url": "https://fdc/1"},
    # The web rung's real answer for the same query.
    "web_collagen_bar": {"kcal": 201.0, "resolved_name": "COLLAGEN PROTEIN BAR, LEMON COOKIE",
                         "source_kind": "retailer", "confidence": "label",
                         "ingredients": "collagen, dates, almonds"},
    # Genuine manufacturer pages, as returned on 10 Aug.
    "web_rubicon": {"kcal": 15.0, "protein_g": 0.0, "carb_g": 2.5, "fat_g": 0.0,
                    "resolved_name": "Rubicon Spring Orange & Mango 500ml",
                    "source_kind": "manufacturer", "confidence": "label",
                    "source_url": "https://www.rubicondrinks.com/products/spring-orange-mango",
                    "ingredients": "carbonated spring water, apple juice, orange juice, mango juice"},
    "web_oats": {"kcal": 322.0, "protein_g": 21.8, "carb_g": 37.2, "fat_g": 8.0,
                 "fibre_g": 6.6, "dietary_sodium_mg": 320,
                 "resolved_name": "M&S Salted Caramel Overnight Oats (High Protein)",
                 "source_kind": "manufacturer", "confidence": "label",
                 "ingredients": "water, oats, dates, cocoa nibs, chia seeds, sunflower oil, sugar"},
    # A label with no amount attached: the shape that must ASK rather than assume.
    "web_no_portion": {"kcal": 106.0, "per": "portion", "pack_g": None,
                       "resolved_name": "M&S Satay Chicken",
                       "source_kind": "retailer", "confidence": "label"},
}


def fetch(rec_key):
    return lambda q, p: dict(REC[rec_key])


# --- 1. the wrong-product family -------------------------------------------

print("--- wrong product, confident numbers ---")
it = NR.resolve("400mg of my protein collagen capsules", day=TODAY, store=None,
                table=TABLE, cofid=EMPTY_COFID, fetchers={NR.Rung.USDA: fetch("usda_soy")})
check("USDA soy is rejected for collagen capsules",
      "Soy" not in (it.get("resolved_name") or ""), it.get("resolved_name"))
check("and no soy species is credited",
      "glycine_max" not in {s["id"] if isinstance(s, dict) else s
                            for s in (it.get("species") or [])})

it = NR.resolve("collagen capsules", day=TODAY, store=None, table=TABLE,
                cofid=EMPTY_COFID, hint={"form": "capsule", "category": "supplement"},
                fetchers={NR.Rung.WEB: fetch("web_collagen_bar")})
check("a collagen protein BAR is rejected for capsules",
      "BAR" not in (it.get("resolved_name") or "").upper(), it.get("resolved_name"))

# --- 2. CoFID must not answer a branded product ----------------------------

print("\n--- CoFID stays in its lane ---")
for q in ("M&S Satay Chicken with Black Rice & Mango",
          "M&S Bang Bang Chicken with Satay Dip",
          "M&S Salted Caramel High Protein Overnight Oats"):
    got = REAL_COFID.lookup(q, 100)
    check(f"CoFID refuses {q[:34]}", got is None, (got or {}).get("resolved_name"))
it = NR.resolve("M&S Satay Chicken with Black Rice & Mango", day=TODAY, store=None,
                table=TABLE, cofid=REAL_COFID,
                hint={"category": "branded_packaged", "form": "prepared_meal"},
                fetchers={NR.Rung.WEB: fetch("web_rubicon")})
check("a branded product skips CoFID entirely",
      next((a["outcome"] for a in it["attempts"] if a["rung"] == NR.Rung.COFID), None)
      == "skipped")
for q in ("porridge oats", "chicken breast", "cheddar", "blueberries"):
    check(f"CoFID still answers {q}", REAL_COFID.lookup(q, 100) is not None)

# --- 3. label data comes back as label data --------------------------------

print("\n--- real manufacturer pages ---")
it = NR.resolve("Rubicon Spring Orange & Mango 500ml", day=TODAY, store=None, table=TABLE,
                cofid=EMPTY_COFID, fetchers={NR.Rung.WEB: fetch("web_rubicon")})
check("a manufacturer page is label confidence", it["confidence"] == "label")
check("15 kcal, not a juice-drink guess of ~200", it["kcal"] == 15.0, it["kcal"])
check("its ingredients tag real species",
      {"malus_domestica", "citrus_sinensis", "mangifera_indica"}
      <= {s["id"] if isinstance(s, dict) else s for s in it["species"]})
it = NR.resolve("M&S Salted Caramel High Protein Overnight Oats", day=TODAY, store=None,
                table=TABLE, cofid=EMPTY_COFID, fetchers={NR.Rung.WEB: fetch("web_oats")})
check("the M&S pot is 322 kcal, not CoFID's raw-oat 379", it["kcal"] == 322.0, it["kcal"])
check("carbs are 37.2 from the label, not 44 from an estimate", it["carb_g"] == 37.2)

# --- 4. species scores survive the round trip ------------------------------

print("\n--- species scores are stored, not defaulted ---")
sp = {s["id"]: s.get("score") for s in it["species"] if isinstance(s, dict)}
check("every species carries a score", all(v is not None for v in sp.values()), sp)
check("sunflower OIL is scored 0, not counted as the seed",
      sp.get("helianthus_annuus") == 0.0, sp.get("helianthus_annuus"))
check("sugar is scored 0, not counted as beet",
      sp.get("beta_vulgaris") == 0.0, sp.get("beta_vulgaris"))
check("oats are scored 1.0", sp.get("avena_sativa") == 1.0)
day = [{"date": TODAY.isoformat(), "entries": [{"resolved_name": "x", "species": it["species"]}]}]
div = PL.diversity(day, TABLE, on=TODAY)
check("the count excludes the refined ones", div["unique_7d"] is not None
      and div["unique_7d"] < len(it["species"]),
      f"{div['unique_7d']} of {len(it['species'])}")
legacy = [{"date": TODAY.isoformat(),
           "entries": [{"resolved_name": "x", "species": ["helianthus_annuus"]}]}]
check("a bare id makes the count unreportable rather than wrong",
      PL.diversity(legacy, TABLE, on=TODAY)["unique_7d"] is None)

# --- 5. a label with no amount must ask ------------------------------------

print("\n--- an amountless label asks rather than assumes ---")
it = NR.resolve("M&S Satay Chicken", day=TODAY, store=None, table=TABLE,
                cofid=EMPTY_COFID,
                fetchers={NR.Rung.WEB: lambda q, p: {"needs_portion": True,
                                                     "resolved_name": "M&S Satay Chicken",
                                                     "per_100g": {"kcal": 106.0},
                                                     "confidence": "label"}})
check("it asks for a portion", it.get("needs_portion") is True)
check("and logs no macros in the meantime",
      all(it.get(f) is None for f in NR.MACRO_FIELDS))
check("the ladder stops rather than letting a lower rung guess",
      next((a["outcome"] for a in it["attempts"] if a["rung"] == NR.Rung.WEB), None)
      == "needs_portion")

# --- 6. a restaurant order is not a row in a whole-food table ---------------

print("\n--- the Wagamama order ---")
DISH = "gochujang salmon rice bowl with brown rice and extra salmon, Wagamama"

# Two SHARED tokens were the bar, and this dish clears it: "rice" and "brown" both appear
# in the table row "Rice, brown, raw". The earlier M&S fixtures never caught this because
# they only ever shared ONE token, so the >= 2 branch was never actually exercised.
got = REAL_COFID.lookup(DISH, None)
check("CoFID refuses the Wagamama dish", got is None, (got or {}).get("resolved_name"))
check("and refuses it with a portion attached too",
      REAL_COFID.lookup(DISH, 450) is None)
for q in ("edamame with chilli and garlic salt, Wagamama",
          "katsu chicken curry with sticky white rice",
          "chicken teriyaki donburi with steamed rice and greens"):
    got = REAL_COFID.lookup(q, 100)
    check(f"CoFID refuses {q[:38]}", got is None, (got or {}).get("resolved_name"))

# The coverage rule must not swing the other way: these are exactly what CoFID is FOR.
print("\n--- and still answers the whole foods it exists for ---")
for q in ("brown rice", "porridge oats", "chicken breast", "cheddar cheese",
          "blueberries", "extra virgin olive oil", "half a large banana",
          "a handful of almonds", "sweet potato", "greek yogurt"):
    got = REAL_COFID.lookup(q, 100)
    check(f"CoFID answers {q}", got is not None, "refused")

# --- 7. what the photo established must REACH the ladder -------------------

print("\n--- the hint survives the hand-off from photo to ladder ---")
order = {"kind": "order", "vendor": "Wagamama",
         "items": [{"text": DISH}, {"text": "edamame with chilli and garlic salt"}]}
items = NLU.photo_item_hints(order)
check("an order's items are tagged as restaurant dishes",
      all((i.get("hint") or {}).get("category") == "restaurant_dish" for i in items))
check("and the vendor survives as the brand",
      items[0]["hint"].get("brand") == "Wagamama")
plate = NLU.photo_item_hints({"kind": "food_plate",
                              "items": [{"text": "brown rice"}]})
check("a plate's components stay whole_food, so CoFID still serves them",
      plate[0]["hint"]["category"] == "whole_food")

it = NR.resolve(DISH, day=TODAY, store=None, table=TABLE, cofid=REAL_COFID,
                hint=items[0]["hint"], fetchers={NR.Rung.WEB: fetch("web_rubicon")})
check("with that hint the dish skips CoFID entirely",
      next((a["outcome"] for a in it["attempts"] if a["rung"] == NR.Rung.COFID), None)
      == "skipped")

# THE CALLER. The library was right and the caller dropped the hint on the floor, so a
# library-only fixture would have passed while the bot stayed broken.
nb = load_bot()
seen = {}


def _capture(text, **kw):
    seen.update(kw)
    seen["text"] = text
    return NR._finalise({"kcal": 500.0, "protein_g": 30.0, "carb_g": 60.0, "fat_g": 12.0},
                        text, NR.Rung.WEB, "estimate", [], TABLE, TODAY, degraded=False)


# nb.NR IS this module's NR - same import, same object - so this stub is GLOBAL and has
# to be put back afterwards. It was not, and every later fixture silently ran against the
# stub instead of the real resolver: green because nothing was being tested.
_real_resolve = NR.resolve
nb.NR.resolve = _capture
nb.tg.send = lambda *a, **k: None
nb.tg.inline = lambda rows: None
nb.set_pending = lambda store, item: None


class _Ctx:
    store, table, cofid, fetchers = None, TABLE, EMPTY_COFID, {}


nb.offer_items(_Ctx(), items, TODAY, "token", 1)
check("offer_items FORWARDS the hint to resolve",
      (seen.get("hint") or {}).get("category") == "restaurant_dish",
      f"hint={seen.get('hint')}")
check("and forwards the search terms with it",
      seen.get("queries") == [items[-1]["text"]], seen.get("queries"))
NR.resolve = _real_resolve
check("the real resolver is back in place for the fixtures below",
      NR.resolve is _real_resolve and nb.NR.resolve is _real_resolve)

# --- 8. the whole photo chain, on the recorded real screenshot --------------

# The vision call is STUBBED with what the model actually returned for Jamie's Deliveroo
# screenshot on 10 Aug at 20:17, taken from the bot log. That keeps this offline and
# deterministic while still exercising read_photo's cleaning, the hint annotation, and the
# ladder - the three hand-offs the live failure passed through.
print("\n--- the recorded Deliveroo screenshot, end to end ---")

RECORDED_ORDER = """Looking at the image now.
{"kind": "order", "vendor": "Wagamama", "stated_item_count": 5, "items": [
  {"text": "gochujang salmon rice bowl with brown rice and extra salmon", "qty": 1},
  {"text": "(meal is with double salmon and brown rice)", "qty": 1},
  {"text": "edamame with chilli and garlic salt (vg)", "qty": 1},
  {"text": "new! soy sauce sachet", "qty": 3}]}"""


class _Proc:
    def __init__(self, out):
        self.stdout, self.stderr, self.returncode = out, "", 0


got = NLU.read_photo("/tmp/x.jpg", "claude", "m", log=lambda *a: None,
                     runner=lambda *a, **k: _Proc(RECORDED_ORDER))
check("the screenshot reads as an order from Wagamama",
      got["kind"] == "order" and got["vendor"] == "Wagamama", got.get("kind"))
texts = [i["text"] for i in got["items"]]
check("the parenthetical modifier is not treated as a dish",
      not any(t.startswith("(") for t in texts), texts)
check("and it does not survive as a rice-shaped item either",
      not any("double salmon" in t for t in texts), texts)
check("the (vg) marker and the new! shout are stripped",
      not any("(vg)" in t or t.lower().startswith("new") for t in texts), texts)
check("the vendor is appended to every dish",
      all("Wagamama" in t for t in texts), texts)
check("3 lines but 5 UNITS, matching what the screen said",
      got["units_seen"] == 5 == got["stated_item_count"], got.get("units_seen"))

# Expand and resolve exactly as handle_photo does.
expanded = []
for i in got["items"]:
    expanded.extend([dict(i)] * i["qty"])
got["items"] = expanded
check("5 units go forward to be logged", len(expanded) == 5, len(expanded))
NLU.photo_item_hints(got)
bowl = next(i for i in got["items"] if "gochujang" in i["text"])
res = NR.resolve(bowl["text"], day=TODAY, store=None, table=TABLE, cofid=REAL_COFID,
                 hint=bowl["hint"], queries=bowl["hint"]["search_terms"],
                 fetchers={NR.Rung.WEB: fetch("web_rubicon")})
check("THE BUG: the salmon bowl is no longer raw brown rice",
      "Rice, brown, raw" != res.get("resolved_name"), res.get("resolved_name"))
check("CoFID was skipped rather than merely outvoted",
      next((a["outcome"] for a in res["attempts"] if a["rung"] == NR.Rung.COFID), None)
      == "skipped")

# --- 9. an expired token is not an unreadable photo ------------------------

print("\n--- an outage announces itself ---")
EXPIRED = ("Failed to authenticate. API Error: 401 OAuth access token has expired. "
           "Re-authenticate to continue.")
check("an expired token is recognised", NLU.model_unavailable(EXPIRED))
check("a usage limit is recognised too",
      NLU.model_unavailable("Claude AI usage limit reached"))
check("a real JSON answer is not mistaken for an outage",
      not NLU.model_unavailable(RECORDED_ORDER))
check("empty output is not called an outage", not NLU.model_unavailable(""))
got = NLU.read_photo("/tmp/x.jpg", "claude", "m", log=lambda *a: None,
                     runner=lambda *a, **k: _Proc(EXPIRED))
check("read_photo flags the outage instead of blaming the photo",
      got.get("model_unavailable") is True, got)

# --- 10. the composed meal, costed whole by one capable model ---------------

# 14 AUG 2026, from the bot log. "a large stir fry with egg noodles, a small steak, soy
# ginger garlic [sauce], [veg]" was broken into four components, each looked up separately,
# and offered as 447 kcal of raw and dried 100 g parts for a meal of about 980. Jamie got a
# correct table by asking a generic Opus 5 himself, and was right that we should be able to:
# the composition tables hold INGREDIENTS, not dinners. So a described meal is now costed in
# one full-intelligence call and the ladder never runs on it.
print("\n--- the stir-fry, costed as one meal ---")

STIR_FRY_MSG = ("a large stir fry with egg noodles, a small steak, soy ginger garlic sauce "
                "and veg")

# The table, RECORDED as a fixture exactly like the Deliveroo response: what a capable model
# returns for that sentence. It pins the CONTRACT - cooked states, a portion and a basis per
# component, a total, a band, the plants, the assumptions - not what a live model says today.
RECORDED_MEAL = """Here is the breakdown.
{"meal_name":"Large beef stir-fry with egg noodles",
 "components":[
  {"name":"egg noodles, cooked","portion_g":300,"portion_basis":"a large bowl of cooked noodles","kcal":420,"protein_g":14,"carb_g":80,"fat_g":4,"fibre_g":4},
  {"name":"rump steak, grilled","portion_g":120,"portion_basis":"a small steak",
   "kcal":210,"protein_g":37,"carb_g":0,"fat_g":7,"fibre_g":0},
  {"name":"soy, ginger and garlic sauce","portion_g":45,"portion_basis":"2 tbsp",
   "kcal":60,"protein_g":2,"carb_g":10,"fat_g":1,"fibre_g":0},
  {"name":"stir-fried mixed vegetables","portion_g":200,"portion_basis":"a generous handful","kcal":110,"protein_g":4,"carb_g":12,"fat_g":5,"fibre_g":5},
  {"name":"vegetable oil for the pan","portion_g":15,"portion_basis":"1 tbsp",
   "kcal":135,"protein_g":0,"carb_g":0,"fat_g":15,"fibre_g":0}],
 "total":{"kcal":935,"protein_g":57,"carb_g":102,"fat_g":32,"fibre_g":9},
 "error_band_pct":18,
 "plants":["wheat","garlic","ginger","soya","onion","red pepper","broccoli"],
 "assumptions":["Large bowl taken as 300 g cooked noodles","1 tbsp oil in the pan",
                "Small steak taken as 120 g raw, grilled"]}"""

meal = NLU.describe_meal(STIR_FRY_MSG, "claude", "claude-opus-5", log=lambda *a: None,
                         runner=lambda *a, **k: _Proc(RECORDED_MEAL))
check("the meal is costed as one table, not four lookups",
      meal is not None and len(meal["components"]) == 5, meal and len(meal["components"]))
check("THE BUG: the total is a real dinner, not 447 kcal",
      700 < meal["total"]["kcal"] < 1300, meal["total"]["kcal"])
check("and it is the SUM of the components, computed by the code",
      meal["total"]["kcal"] == sum(c["kcal"] for c in meal["components"]),
      meal["total"]["kcal"])
check("every component is in the state he ATE it",
      not any(w in c["name"].lower() for c in meal["components"]
              for w in ("dried", " raw")),
      [c["name"] for c in meal["components"]])
check("every component has an as-eaten portion and a stated basis for it",
      all(c["portion_g"] and c["portion_basis"] for c in meal["components"]),
      [(c["portion_g"], c["portion_basis"]) for c in meal["components"]])
check("the cooking oil the ladder always missed is in the meal",
      any("oil" in c["name"].lower() for c in meal["components"]))
check("but it is the teaspoons that went in the pan, not 100 g of oil",
      all(c["portion_g"] <= 30 for c in meal["components"]
          if "oil" in c["name"].lower()))
check("it declares an honest error band", 10 <= meal["error_band_pct"] <= 40)
check("and every assumption is stated, because that is what he corrects",
      len(meal["assumptions"]) == 3)

# THE CALLER, through the real bot: an item a library got right and the caller mangled is the
# shape of every hand-off bug in this file.
_sent = []
nb.tg.send = lambda token, chat, text, **k: _sent.append(text)
nb.tg.inline = lambda rows: None
_pend = {}
nb.set_pending = lambda store, item: _pend.update(item)
nb._chat = lambda ctx, role, text: None
# nb.NLU is this file's NLU, so this stub is GLOBAL and is put back below.
_real_meal_run = nb.NLU.subprocess.run
nb.NLU.subprocess.run = lambda *a, **k: _Proc(RECORDED_MEAL)


class _MealCtx:
    store, table, cofid, fetchers = None, TABLE, EMPTY_COFID, {}


_offered = nb.offer_composed(_MealCtx(), STIR_FRY_MSG, TODAY, "token", 1,
                             default_meal="dinner")
_item = (_pend.get("batch") or [{}])[0]
check("the bot offers it as ONE entry", _offered and len(_pend.get("batch") or []) == 1)
check("at the costed total", _item.get("kcal") == 935.0, _item.get("kcal"))
check("labelled an estimate, with the band on the line he reads",
      _item.get("confidence") == "estimate" and "+/-18%" in _sent[-1])
check("the plants in it are credited to the diversity count",
      len(_item.get("species") or []) >= 4,
      [s["id"] for s in _item.get("species") or []])
check("the table and its assumptions are in the confirm message",
      "300g egg noodles, cooked" in _sent[-1]
      and "assumed: 1 tbsp oil in the pan" in _sent[-1])
check("and it asks once", _sent[-1].count("Log it?") == 1)
# Put it back. A stub left in place is how this file was silently green once already: the
# fixtures below pass an explicit runner, so nothing would have complained.
nb.NLU.subprocess.run = _real_meal_run
check("the meal-model stub is restored for the fixtures below",
      nb.NLU.subprocess.run is _real_meal_run)

# --- 11. the interpret fallback, for when the meal model is down ------------

# SECOND BEST, AND IT HAS TO BE GOOD. When the meal model cannot be reached, the same
# message goes down the interpret-and-resolve path, and that path was where the original
# 447 kcal came from: four components at per-100g, no portions, priced off "Noodles, egg,
# dried, raw" and a raw steak row. A fallback nobody fixed is a fallback that ships the
# original bug on the first outage, so it gets cooked states, as-eaten portions, and a CoFID
# that prefers a cooked row over a raw one.
print("\n--- the fallback path: cooked and portioned, when the meal model is down ---")

STIR_FRY = ("a large stir fry with egg noodles, a small steak, soy ginger garlic sauce "
            "and veg")

# What the interpreter now returns for it: cooked search terms, an as-eaten portion per
# component, and every portion declared a guess. Recorded here as a fixture, exactly like
# the Deliveroo response, so the fixture pins the CONTRACT between the two model calls and
# the ladder rather than what a live model says today.
PLANNED_STIR_FRY = """{"items":[
 {"canonical_name":"egg noodles, cooked","brand":null,"form":"whole_food",
  "category":"whole_food","is_supplement":false,"expect_macros":true,"portion_g":300,
  "portion_estimated":true,"in_session":false,"at":null,"meal":"dinner",
  "search_terms":["egg noodles, cooked","noodles, egg, boiled"]},
 {"canonical_name":"rump steak, grilled","brand":null,"form":"whole_food",
  "category":"whole_food","is_supplement":false,"expect_macros":true,"portion_g":100,
  "portion_estimated":true,"in_session":false,"at":null,"meal":"dinner",
  "search_terms":["beef, rump steak, grilled, lean"]},
 {"canonical_name":"soy sauce","brand":null,"form":"other","category":"whole_food",
  "is_supplement":false,"expect_macros":true,"portion_g":30,"portion_estimated":true,
  "in_session":false,"at":null,"meal":"dinner","search_terms":["soy sauce"]},
 {"canonical_name":"vegetables, stir-fried","brand":null,"form":"whole_food",
  "category":"whole_food","is_supplement":false,"expect_macros":true,"portion_g":200,
  "portion_estimated":true,"in_session":false,"at":null,"meal":"dinner",
  "search_terms":["vegetables, stir-fried"]}]}"""

plan = NLU.interpret(STIR_FRY, "claude", "m", log=lambda *a: None,
                     runner=lambda *a, **k: _Proc(PLANNED_STIR_FRY))
check("all four components are planned", len(plan["items"]) == 4, len(plan["items"]))
# The three components the table holds in more than one state. A sauce has no cooked form
# and must not be made to claim one - the rule is that the state he ate it in is the state
# searched for, not that every string carries a cooking verb.
check("each component that HAS a raw form is searched in its cooked one",
      all(any(w in t for t in plan["items"][i]["search_terms"] for w in
              ("cooked", "boiled", "grilled", "stir-fried"))
          for i in (0, 1, 3)),
      [i["search_terms"] for i in plan["items"]])
check("and none of them asks for a raw or dried row",
      not any("raw" in t or "dried" in t
              for i in plan["items"] for t in i["search_terms"]),
      [i["search_terms"] for i in plan["items"]])
check("every component carries a portion, scaled to a LARGE stir fry",
      [i["portion_g"] for i in plan["items"]] == [300, 100, 30, 200],
      [i["portion_g"] for i in plan["items"]])
check("and every portion is declared a guess, not a measurement",
      all(i["portion_estimated"] is True for i in plan["items"]))

# THE LOOKUP, against the real published table. The raw row wins on overlap and coverage
# alone - it is the shortest name, so it adds least - which is exactly how it beat the
# boiled one.
check("THE BUG: cooked egg noodles are no longer the dried, raw row",
      REAL_COFID.lookup("egg noodles, cooked", 100)["resolved_name"]
      != "Noodles, egg, dried, raw",
      REAL_COFID.lookup("egg noodles, cooked", 100)["resolved_name"])
check("they are a boiled row, at roughly half the energy of the dried one",
      "boiled" in REAL_COFID.lookup("egg noodles, cooked", 100)["resolved_name"]
      and REAL_COFID.lookup("egg noodles, cooked", 100)["kcal"] < 200,
      REAL_COFID.lookup("egg noodles, cooked", 100)["kcal"])
check("a query that ASKS for a dried food still gets one",
      REAL_COFID.lookup("dried apricots", 100)["resolved_name"] == "Apricots, dried")
check("and a raw food eaten raw is untouched",
      "raw" in REAL_COFID.lookup("raw carrot", 100)["resolved_name"])
# The basis has to travel with the figures or a later "x1.5" has nothing to scale from and
# "300 g of that" cannot be applied at all.
_noodles = REAL_COFID.lookup("egg noodles, cooked", 300)
check("a CoFID hit carries the per-100g basis it scaled from",
      (_noodles.get("per_100g") or {}).get("kcal") is not None
      and _noodles["portion_used_g"] == 300.0)
check("and the portion is priced from that basis",
      round(_noodles["kcal"]) == round(_noodles["per_100g"]["kcal"] * 3),
      _noodles["kcal"])

# THE CALLER, with the real ladder and the real table: a library that is right while
# offer_planned drops the flag is the shape of every hand-off bug in this file.
nb.tg.send = lambda token, chat, text, **k: _offered.append(text)
_offered = []
_pending = {}
nb.set_pending = lambda store, item: _pending.update(item)


class _MealCtx:
    store, table, cofid, fetchers = None, TABLE, REAL_COFID, {}


nb.offer_planned(_MealCtx(), plan["items"], TODAY, "token", 1, said=STIR_FRY)
batch = _pending.get("batch") or []
check("the offer carries all four components", len(batch) == 4, len(batch))
check("none of them is a raw or dried row",
      not any("raw" in (i.get("resolved_name") or "").lower()
              and "boiled" not in (i.get("resolved_name") or "").lower()
              for i in batch),
      [i.get("resolved_name") for i in batch])
check("every component has the portion the interpreter sized",
      [i.get("portion_used_g") for i in batch] == [300.0, 100.0, 30.0, 200.0],
      [i.get("portion_used_g") for i in batch])
check("and every estimated portion says so on the message he confirms",
      all(i.get("portion_estimated") is True for i in batch)
      and _offered and _offered[-1].count("assumed") == 4,
      [i.get("portion_assumed") for i in batch])
_total = sum(i.get("kcal") or 0 for i in batch)
check("the total is a plausible dinner, not the 447 kcal he was offered",
      700 < _total < 1300, round(_total))
check("each component keeps a basis, so it can still be rescaled",
      all(i.get("per_100g") for i in batch),
      [bool(i.get("per_100g")) for i in batch])

print()
if FAILED:
    print(f"{len(FAILED)} FAILED: " + ", ".join(FAILED))
    sys.exit(1)
print("all golden checks passed")
