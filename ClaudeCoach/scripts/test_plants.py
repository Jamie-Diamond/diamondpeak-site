#!/usr/bin/env python3
"""Offline tests for lib/plants.py. Run: python3 ClaudeCoach/scripts/test_plants.py

The failure modes here are quiet ones: a substring match inflates the count, a
refined form scores as a whole grain, or the same species counts twice. Every one
of those corrupts the headline metric without erroring.
"""
import sys
from datetime import date, timedelta
from pathlib import Path

_here = Path(__file__).resolve().parent
for cand in (_here / "lib", _here.parent / "lib"):
    if (cand / "plants.py").exists():
        sys.path.insert(0, str(cand))
        break
import plants as P

FAILED = []


def check(name, cond):
    print(("PASS" if cond else "FAIL"), name)
    if not cond:
        FAILED.append(name)


T = P.SpeciesTable()
TODAY = date(2026, 8, 10)


def ids(text):
    return {s["id"] for s in T.match_text(text)["species"]}


def scored(text):
    return {s["id"]: s["score"] for s in T.match_text(text)["species"]}


# 1) The table loaded and is not trivially small.
check(f"table loaded ({len(T.species)} species)", len(T.species) >= 80)
check("every species has a latin name", all(s["latin"] for s in T.species.values()))
check("herbs score 0.25 by category",
      T.species["coriandrum_sativum"]["score"] == P.SCORE_HERB_SPICE)
check("whole plants score 1.0", T.species["avena_sativa"]["score"] == P.SCORE_WHOLE)

# 2) The spec's own canonicalisation examples (6.3).
check("spring onion and scallion are one species",
      ids("spring onion") == ids("scallion") == {"allium_fistulosum"})
check("spring onion does NOT also count as onion",
      "allium_cepa" not in ids("spring onion"))
check("brown rice and black rice are one species",
      ids("brown rice") == ids("black rice") == {"oryza_sativa"})
check("cocoa, cacao and dark chocolate are one species",
      ids("cocoa") == ids("cacao nibs") == ids("dark chocolate") == {"theobroma_cacao"})
check("chickpea, garbanzo, gram flour and hummus are one species",
      ids("chickpea") == ids("garbanzo") == ids("gram flour") == ids("houmous")
      == {"cicer_arietinum"})
check("broad bean and fava are one species",
      ids("broad bean") == ids("fava bean") == {"vicia_faba"})
check("coriander leaf and seed are one species at 0.25",
      scored("coriander seed") == scored("coriander leaf")
      == {"coriandrum_sativum": 0.25})
check("edamame, tofu and miso are one species",
      ids("edamame") == ids("tofu") == ids("miso") == {"glycine_max"})

# 3) Refined derivatives: same species, zero score.
check("rice flour is Oryza sativa at 0.0", scored("rice flour") == {"oryza_sativa": 0.0})
check("white flour is wheat at 0.0", scored("plain flour") == {"triticum_aestivum": 0.0})
check("sunflower oil is the species at 0.0",
      scored("sunflower oil") == {"helianthus_annuus": 0.0})
check("sugar resolves to beet at 0.0", scored("caster sugar") == {"beta_vulgaris": 0.0})
check("wholemeal is wheat at 1.0", scored("wholemeal bread") == {"triticum_aestivum": 1.0})

# 4) A species in two forms on one day counts ONCE, at its best score.
both = scored("brown rice with rice flour thickener")
check(f"rice in two forms is one species at the best score (got {both})",
      both == {"oryza_sativa": 1.0})

# 5) Substring traps. Every one of these silently inflated a naive matcher.
check("liquorice does not match rice", "oryza_sativa" not in ids("liquorice"))
check("cornish pasty does not match corn", "zea_mays" not in ids("cornish pasty"))
check("peach does not match pea", "pisum_sativum" not in ids("peach"))
check("peanut is a peanut, not a pea", ids("peanut butter") == {"arachis_hypogaea"})
check("goat's cheese does not match oat", "avena_sativa" not in ids("goats cheese"))
check("update does not match date", "phoenix_dactylifera" not in ids("update the log"))
check("beansprout is mung, not common bean",
      ids("beansprout") == {"vigna_radiata"})

# 6) Plurals resolve without a duplicate table entry.
for singular, plural in (("almond", "almonds"), ("tomato", "tomatoes"),
                         ("oat", "oats"), ("date", "dates"), ("olive", "olives"),
                         ("blueberry", "blueberries"), ("cherry", "cherries"),
                         ("raspberry", "raspberries"), ("strawberry", "strawberries"),
                         ("bay leaf", "bay leaves")):
    check(f"{plural} resolves like {singular}", ids(plural) == ids(singular))
# REGRESSION: -y to -ies was missing, so every berry silently dropped out of the
# count. Nothing errored, the number just came out lower.
check("blueberries resolve at all", ids("blueberries") == {"vaccinium_corymbosum"})

# 7) A realistic multi-plant string. This is the case the spec warns about: one
#    nutrient-dense meal carrying a big daily count.
meal = ("chargrilled vegetable and quinoa salad with red pepper, courgette, "
        "aubergine, spinach, cherry tomatoes, red onion, chickpeas, toasted "
        "pumpkin seeds, flat leaf parsley, lemon juice and extra virgin olive oil")
got = T.match_text(meal)
check(f"a dense meal resolves many species (got {len(got['species'])})",
      len(got["species"]) >= 10)
check("olive oil labelled extra virgin still scores as the fruit",
      any(s["id"] == "olea_europaea" and s["score"] == 1.0 for s in got["species"]))
check("pumpkin seed maps to the pumpkin species",
      any(s["id"] == "cucurbita_pepo" for s in got["species"]))

# 8) Unmapped text is returned for review, not silently swallowed.
res = T.match_text("artisanal fermented thing from the market")
check("unmapped text is returned", res["unmatched"] != "")
check("unmapped text does not invent a species", res["species"] == [])
res2 = T.match_text("almonds and some unpronounceable grain")
check("partial matches return only the leftover as unmatched",
      "almond" not in res2["unmatched"] and "unpronounceable" in res2["unmatched"])

# 9) The rolling window and the metric that actually matters.
def day(d, *texts):
    return {"date": d.isoformat(),
            "entries": [{"resolved_name": t} for t in texts]}


days = [
    day(TODAY - timedelta(days=6), "porridge oats with blueberries"),
    day(TODAY - timedelta(days=5), "porridge oats with blueberries"),
    day(TODAY - timedelta(days=4), "porridge oats with blueberries"),
    day(TODAY - timedelta(days=3), "porridge oats with blueberries"),
    day(TODAY - timedelta(days=2), "porridge oats with blueberries"),
    day(TODAY - timedelta(days=1), "porridge oats with blueberries"),
    day(TODAY, "porridge oats with blueberries", meal),
]
div = P.diversity(days, T, on=TODAY)
check(f"unique_7d counts distinct species (got {div['unique_7d']})", div["unique_7d"] >= 12)
check("target is 30", div["target"] == P.DIVERSITY_TARGET_7D)
check("the target states its evidence honestly",
      "not a threshold" in div["target_basis"] and "McDonald" in div["target_basis"])
check(f"new_species_today excludes the repeated breakfast "
      f"(got {div['new_species_today']})",
      div["new_species_today"] == div["unique_7d"] - 2)
check("oats are not new today", "Oats" not in div["new_species_today_names"])
check("weighted total discounts herbs",
      div["weighted_7d"] < div["unique_7d"])
check("herb count is reported separately", div["herb_spice_count"] >= 1)

# 10) A repeated dense meal gives a big daily count and a WEAK weekly one. This is
#     the spec's central point about why daily totals mislead.
repeated = [day(TODAY - timedelta(days=i), meal) for i in range(7)]
rep = P.diversity(repeated, T, on=TODAY)
same = P.diversity([day(TODAY, meal)], T, on=TODAY)
check("repeating one dense meal all week adds nothing to the unique count",
      rep["unique_7d"] == same["unique_7d"])
check("and reports zero new species today", rep["new_species_today"] == 0)

# 11) Days outside the window are excluded.
old = [day(TODAY - timedelta(days=30), "mango and pistachio"), day(TODAY, "oats")]
divold = P.diversity(old, T, on=TODAY)
check("species from outside the window do not count",
      "Mango" not in divold["species"])

# 12) A refined-only day has recognised species but no diversity.
refined_day = [day(TODAY, "white bread with sunflower oil and caster sugar")]
dref = P.diversity(refined_day, T, on=TODAY)
check(f"a refined-only day scores zero species (got {dref['unique_7d']})",
      dref["unique_7d"] == 0)

# 13) Stored species ids win over re-matching, so a table change cannot rewrite
#     history for days already logged.
# Scores must be STORED with the id. A bare id cannot say whether the match was a whole
# plant or a refined derivative, so the count becomes unreportable rather than wrong.
scored = [{"date": TODAY.isoformat(),
           "entries": [{"resolved_name": "unrecognisable at log time",
                        "species": [{"id": "mangifera_indica", "score": 1.0},
                                    {"id": "pistacia_vera", "score": 1.0}]}]}]
dstored = P.diversity(scored, T, on=TODAY)
check("stored species are used in preference to re-matching",
      dstored["unique_7d"] == 2 and "Mango" in dstored["species"])
check("a fully scored window is reportable", dstored["provisional"] is False)

legacy = [{"date": TODAY.isoformat(),
           "entries": [{"resolved_name": "old entry",
                        "species": ["mangifera_indica", "helianthus_annuus"]}]}]
dl = P.diversity(legacy, T, on=TODAY)
check("bare ids make the count UNREPORTABLE rather than wrong",
      dl["unique_7d"] is None)
check("the upper bound is still available for diagnosis",
      dl["unique_7d_upper_bound"] == 2)
check("and the reason is countable", dl["unscored_species"] == 2)
check("a refined derivative with a stored 0 is excluded, not counted",
      P.diversity([{"date": TODAY.isoformat(), "entries": [{"resolved_name": "x",
        "species": [{"id": "helianthus_annuus", "score": 0.0},
                    {"id": "mangifera_indica", "score": 1.0}]}]}],
        T, on=TODAY)["unique_7d"] == 1)

# 14) Low-variety prompt: a prompt, never a failure, and absent when not needed.
same_days = [day(TODAY - timedelta(days=i), "oats and blueberries") for i in range(7)]
flag = P.low_variety_flag(same_days, T, on=TODAY)
check("repeated identical days raise a variety prompt", flag is not None)
check("the prompt is info severity, never a failure", flag["severity"] == "info")
check("the prompt language does not blame",
      "fail" not in flag["message"].lower() and "worth" in flag["message"].lower())
# A genuinely varied week needs DIFFERENT species each day, not two menus
# alternating: alternating produces zero new species too, which is the whole point
# of measuring new-per-day rather than per-day totals.
menus = [
    "kale, walnuts, kiwi, lentils, fennel, mango, ginger",
    "beetroot, pistachio, fig, barley, leek, turmeric, pear",
    "sweet potato, chia, blackberry, buckwheat, asparagus, dill, plum",
    "aubergine, pecan, pomegranate, rye, artichoke, cumin, peach",
    "cauliflower, hazelnut, grapes, quinoa, celery, cinnamon, apple",
    "spinach, cashew, raspberry, oats, mushroom, basil, banana",
    "carrot, brazil nuts, strawberry, spelt, cucumber, mint, orange",
]
varied = [day(TODAY - timedelta(days=i), menus[i]) for i in range(7)]
check("a varied week raises no prompt", P.low_variety_flag(varied, T, on=TODAY) is None)
vdiv = P.diversity(varied, T, on=TODAY)
check(f"a varied week approaches the 30 target (got {vdiv['unique_7d']})",
      vdiv["unique_7d"] >= 30)

# 15) Nothing in the output can be rendered as a streak or a score out of 100.
check("no streak or score fields are exposed",
      not any(k in div for k in ("streak", "score", "grade", "percent", "passed")))

print("\n--- additives are not plants ---")
# Jamie, 11 Aug 2026: "a cookies and cream protein bar has no plants in it". It had been
# credited with soy, cacao and vanilla - three species toward the 7-day count - off
# "Lecithins (Soya)", "Cocoa Butter", "Cocoa Mass" and "Natural Vanilla Flavouring".
BAR = ("Milk Chocolate with Sweetener (24%) (Sweetener: Maltitol, Cocoa Butter, Dried "
       "Whole Milk, Cocoa Mass, Emulsifier: Lecithins (Soya), Natural Vanilla "
       "Flavouring), Calcium Caseinate (Milk), Humectant: Glycerol, Hydrolysed Beef "
       "Collagen, Polydextrose, Concentrated Whey Protein (Milk), Whey Protein Isolate")
bar = T.match_text(BAR)
counting = [s for s in bar["species"] if s["score"] > 0]
check(f"the protein bar counts zero plants (got {len(counting)})", not counting)
check("the species are still RECOGNISED, just scored zero",
      len(bar["species"]) >= 3)
for form, sid in (("soya lecithin", "glycine_max"), ("cocoa butter", "theobroma_cacao"),
                  ("milk chocolate", "theobroma_cacao"),
                  ("natural vanilla flavouring", "vanilla_planifolia"),
                  ("maltodextrin", "zea_mays"), ("glucose syrup", "zea_mays"),
                  ("palm oil", None)):
    got = {x["id"]: x["score"] for x in T.match_text(form)["species"]}
    if sid is None:
        continue
    check(f"{form} scores 0", got.get(sid) == 0.0)
# And the real foods must be untouched, or the fix has eaten the metric.
for food, sid in (("dark chocolate", "theobroma_cacao"), ("cocoa nibs", "theobroma_cacao"),
                  ("edamame", "glycine_max"), ("tofu", "glycine_max"),
                  ("miso", "glycine_max"), ("sweetcorn", "zea_mays"),
                  ("brown rice", "oryza_sativa"),
                  ("coconut flakes", "cocos_nucifera")):
    got = {x["id"]: x["score"] for x in T.match_text(food)["species"]}
    check(f"{food} still counts as a whole plant", got.get(sid) == 1.0)

print()
if FAILED:
    print(f"{len(FAILED)} FAILED: " + ", ".join(FAILED))
    sys.exit(1)
print("all checks passed")
