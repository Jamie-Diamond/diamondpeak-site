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
import nutrition_resolve as NR  # noqa: E402
import plants as PL  # noqa: E402

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

print()
if FAILED:
    print(f"{len(FAILED)} FAILED: " + ", ".join(FAILED))
    sys.exit(1)
print("all golden checks passed")
