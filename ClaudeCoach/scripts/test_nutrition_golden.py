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

print()
if FAILED:
    print(f"{len(FAILED)} FAILED: " + ", ".join(FAILED))
    sys.exit(1)
print("all golden checks passed")
