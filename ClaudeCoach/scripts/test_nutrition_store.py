#!/usr/bin/env python3
"""Offline tests for lib/nutrition_store.py. Run: python3 scripts/test_nutrition_store.py

Writes to a tmpdir, never to a real athlete directory. Covers the failure modes
that would corrupt the longitudinal record silently: sweat weigh-in tagging,
collagen protein exclusion, stale cache treated as a miss, flag de-duplication,
snapshotted targets, and a corrupt month file not taking the bot down.
"""
import json
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

_here = Path(__file__).resolve().parent
for cand in (_here / "lib", _here.parent / "lib"):
    if (cand / "nutrition_store.py").exists():
        sys.path.insert(0, str(cand))
        break
import nutrition_store as S
import nutrition_engine as N

FAILED = []


def check(name, cond):
    print(("PASS" if cond else "FAIL"), name)
    if not cond:
        FAILED.append(name)


TODAY = date(2026, 8, 10)
tmp = Path(tempfile.mkdtemp(prefix="nut-test-"))
store = S.NutritionStore(tmp)

# 1) A blank day is a real shape, never None.
d = store.get_day(TODAY)
check("blank day has the full shape",
      d["entries"] == [] and d["closed_at"] is None and d["date"] == TODAY.isoformat())
check("blank day did not create a file", not (tmp / "nutrition" / "2026-08.json").exists())

# 2) Entries round-trip, and dietary sodium is a first-class field.
e1 = store.add_entry(TODAY, raw_text="half a bag of M&S nut collection, 75g pack",
                     resolved_name="M&S nut collection 75g", portion_g=37.5,
                     kcal=235, protein_g=6, carb_g=6, fat_g=21, fibre_g=3,
                     dietary_sodium_mg=45, confidence="label", source_rung="retailer",
                     source_url="https://www.marksandspencer.com/x", logged_at=f"{TODAY}T12:40",
                     species=["Corylus avellana", "Anacardium occidentale"])
check("entry gets a stable id", e1["id"] == "2026-08-10-001")
check("dietary sodium stored", store.get_day(TODAY)["entries"][0]["dietary_sodium_mg"] == 45)
check("source rung stored", e1["source_rung"] == "retailer")
check("species stored", len(e1["species"]) == 2)
check("month file now exists", (tmp / "nutrition" / "2026-08.json").exists())

# 3) Bad confidence or rung is rejected loudly rather than stored.
for bad in ({"confidence": "guess"}, {"source_rung": "vibes"}):
    try:
        store.add_entry(TODAY, raw_text="x", **bad)
        check(f"rejects bad {list(bad)[0]}", False)
    except ValueError:
        check(f"rejects bad {list(bad)[0]}", True)

# 4) Collagen is excluded from the protein total but reported separately.
store.add_entry(TODAY, raw_text="chicken breast 200g", resolved_name="chicken breast",
                kcal=330, protein_g=62, confidence="label", source_rung="retailer")
store.add_supplement(TODAY, nutrient="collagen peptides", dose=15, unit="g",
                     protein_g=15, timing="30 min before ankle session")
t = store.day_totals(TODAY)
check(f"protein total excludes collagen (got {t['protein_g']})", t["protein_g"] == 68.0)
check("collagen reported separately", t["non_counting_protein_g"] == 15.0)
check("supplement confidence is label by definition",
      store.get_day(TODAY)["supplements"][0]["confidence"] == "label")

# 5) In-session fuel counts toward totals but is also isolated.
store.add_entry(TODAY, raw_text="Maurten 320", resolved_name="Maurten drink mix 320",
                kcal=320, carb_g=80, confidence="label", source_rung="retailer",
                in_session=True)
t = store.day_totals(TODAY)
check("in-session kcal counted in the day total", t["kcal"] == 885.0)
check("in-session totalled separately for protection", t["in_session_carb_g"] == 80.0)

# REGRESSION: with NO in-session items, the in-session totals must be ZERO, not the
# whole day. `rows or entries` made an empty list fall through to every entry, so a
# normal day reported all its calories as protected in-session fuel.
plain = S.NutritionStore(Path(tempfile.mkdtemp(prefix="nut-plain-")))
plain.add_entry(TODAY, raw_text="porridge", kcal=400, carb_g=60, confidence="label",
                source_rung="cofid")
pt = plain.day_totals(TODAY)
check(f"no in-session items means zero in-session kcal (got {pt['in_session_kcal']})",
      pt["in_session_kcal"] == 0)
check("and zero in-session carbs", pt["in_session_carb_g"] == 0)
check("while the day total is unaffected", pt["kcal"] == 400.0)

# 6) Lowest confidence propagates, so the UI can never imply label-grade data.
check("all-label day reads label", t["lowest_confidence"] == "label")
store.add_entry(TODAY, raw_text="handful of something", kcal=100, confidence="estimate",
                source_rung="llm")
check("one estimate downgrades the whole day",
      store.day_totals(TODAY)["lowest_confidence"] == "estimate")

# 7) Undo removes the last entry only.
before = len(store.get_day(TODAY)["entries"])
popped = store.undo_last(TODAY)
check("undo returns the removed entry", popped["raw_text"] == "handful of something")
check("undo removed exactly one", len(store.get_day(TODAY)["entries"]) == before - 1)
check("remove_entry finds by id", store.remove_entry(TODAY, e1["id"])["id"] == e1["id"])
check("remove_entry on a missing id returns None",
      store.remove_entry(TODAY, "nope-999") is None)

# 7b) REGRESSION: ids must never be reused after a removal. Deriving the id from
#     list length meant /undo then log again produced a duplicate id, so
#     remove_entry and the ICU re-push after a retrospective edit acted on the
#     wrong row.
idtmp = Path(tempfile.mkdtemp(prefix="nut-id-"))
ids = S.NutritionStore(idtmp)
first = [ids.add_entry(TODAY, raw_text=f"item {i}", kcal=10,
                       confidence="estimate", source_rung="llm")["id"]
         for i in range(3)]
ids.undo_last(TODAY)
ids.undo_last(TODAY)
after = ids.add_entry(TODAY, raw_text="item 3", kcal=10,
                      confidence="estimate", source_rung="llm")["id"]
check(f"entry id not reused after undo (got {after}, earlier {first})",
      after not in first)
s1 = ids.add_supplement(TODAY, nutrient="creatine", dose=5, unit="g")["id"]
ids.get_day(TODAY)  # no supplement removal path, but the counter must still advance
s2 = ids.add_supplement(TODAY, nutrient="collagen", dose=15, unit="g",
                        protein_g=15)["id"]
check(f"supplement ids are distinct ({s1}, {s2})", s1 != s2)

# 7c) REGRESSION: a lost update. Atomic writes stop a torn file, not a lost
#     update - two writers each loading, appending and replacing would leave only
#     the second. Two independent store objects stand in for the bot and a cron.
race = Path(tempfile.mkdtemp(prefix="nut-race-"))
a, b = S.NutritionStore(race), S.NutritionStore(race)
a.add_entry(TODAY, raw_text="from the bot", kcal=100, confidence="estimate",
            source_rung="llm")
b.add_entry(TODAY, raw_text="from the cron", kcal=200, confidence="estimate",
            source_rung="llm")
texts = [e["raw_text"] for e in a.get_day(TODAY)["entries"]]
check(f"both writers' entries survive (got {texts})",
      "from the bot" in texts and "from the cron" in texts)
check("lock file is a sidecar, not the month file",
      (race / "nutrition" / ".2026-08.lock").exists()
      and (race / "nutrition" / "2026-08.json").exists())

# 7d) The non-counting protein token list has ONE home. Asserted by IDENTITY, not
#     by monkey-patching both modules: patching both would pass whether or not the
#     store kept its own copy, which is the thing under test.
check("store and engine share one token list object",
      S.NON_COUNTING_PROTEIN_SOURCES is N.NON_COUNTING_PROTEIN_SOURCES)
bt = Path(tempfile.mkdtemp(prefix="nut-tok-"))
tok = S.NutritionStore(bt)
for token in N.NON_COUNTING_PROTEIN_SOURCES:
    tok.add_entry(TODAY, raw_text=f"{token} product", resolved_name=f"{token} product",
                  protein_g=10, kcal=40, confidence="label", source_rung="retailer")
tt = tok.day_totals(TODAY)
check(f"every engine token is excluded by the store (got {tt['protein_g']})",
      tt["protein_g"] == 0.0
      and tt["non_counting_protein_g"] == 10.0 * len(N.NON_COUNTING_PROTEIN_SOURCES))

# 7e) log_unresolved must not silently date itself off server UTC.
try:
    store.log_unresolved("no date given")
    check("log_unresolved requires an explicit local day", False)
except TypeError:
    check("log_unresolved requires an explicit local day", True)

# 8) Weight: the first reading of the day is morning, later ones are sweat.
day2 = TODAY + timedelta(days=1)
m1 = store.add_measurement(day2, type="weight", value=83.4, logged_at=f"{day2}T06:12")
m2 = store.add_measurement(day2, type="weight", value=80.6, logged_at=f"{day2}T13:55")
check("first weight of the day is tagged morning", m1["tag"] == "morning")
check("second weight is auto-tagged session_sweat", m2["tag"] == "session_sweat")
check("reading_index records the order", (m1["reading_index"], m2["reading_index"]) == (0, 1))
check("explicit tag is honoured over the default",
      store.add_measurement(day2, type="weight", value=83.0,
                            logged_at=f"{day2}T20:00", tag="morning")["tag"] == "morning")
try:
    store.add_measurement(day2, type="bodyweight", value=83)
    check("rejects an unknown measurement type", False)
except ValueError:
    check("rejects an unknown measurement type", True)

# 9) The store feeds the engine: a contaminated day must not move the rolling mean.
for i in range(2, 8):
    d_i = TODAY - timedelta(days=i)
    store.add_measurement(d_i, type="weight", value=83.4, logged_at=f"{d_i}T06:10")
rows = store.measurements_range(TODAY - timedelta(days=7), day2, type="weight")
mean = N.rolling_weight_kg(rows, on=day2, days=8)
check(f"sweat reading excluded from the engine's mean (got {mean})",
      mean is not None and mean >= 83.3)

# 10) Flags de-duplicate by type, or the history becomes unreadable.
store.add_flag(TODAY, type="fat_frontload", severity="warn", payload={"deviation_pp": 22})
store.add_flag(TODAY, type="fat_frontload", severity="warn", payload={"deviation_pp": 31})
store.add_flag(TODAY, type="underfuel", severity="high")
flags = store.get_day(TODAY)["flags"]
check("one flag per type per day", len([f for f in flags if f["type"] == "fat_frontload"]) == 1)
check("latest payload wins",
      [f for f in flags if f["type"] == "fat_frontload"][0]["payload"]["deviation_pp"] == 31)
check("different types coexist", len(flags) == 2)
try:
    store.add_flag(TODAY, type="made_up")
    check("rejects an unknown flag type", False)
except ValueError:
    check("rejects an unknown flag type", True)

# 11) Targets are snapshotted, not recomputed. ICU revises activity calories after
#     the fact, so a day reviewed later must show what was in force on the day.
rmr = N.mifflin_st_jeor(83.3, 1.86, date(1995, 5, 6), "M", on=TODAY)
tgt = N.zones(day_type="standard", rolling_weight=83.3, rmr=rmr,
              sessions=[{"type": "Ride", "moving_time": 7200, "calories": 1600,
                         "average_watts": 210}])
store.set_targets(TODAY, tgt, day_type="standard", phase="maintenance")
saved = store.get_day(TODAY)
check("zones snapshotted onto the day", saved["targets"]["kcal_target"] == tgt["kcal_target"])
check("the snapshot keeps the bias, so a reviewed day renders the same way",
      saved["targets"]["fibre_g"]["bias"] == tgt["fibre_g"]["bias"])
check("day_type recorded", saved["day_type"] == "standard")
check("phase recorded", saved["phase"] == "maintenance")

# 12) Close-out and push markers.
store.close_day(TODAY, when=f"{TODAY}T22:10")
store.mark_pushed(TODAY, when=f"{TODAY}T22:11")
saved = store.get_day(TODAY)
check("close_day stamps closed_at", saved["closed_at"] == f"{TODAY}T22:10")
check("mark_pushed stamps the push", saved["pushed_to_intervals_at"] == f"{TODAY}T22:11")

# 13) Cache: fresh hits, stale misses.
store.cache_put("m&s nut collection 75g",
                {"kcal": 470, "resolved_at": "2026-08-01", "confidence": "label"})
check("fresh cache entry hits", store.cache_get("M&S Nut Collection 75g", on=TODAY) is not None)
check("cache key is case and space insensitive",
      store.cache_get("  m&s nut collection 75g  ", on=TODAY)["kcal"] == 470)
store.cache_put("old item", {"kcal": 100, "resolved_at": "2024-01-01"})
check("stale cache entry is a MISS, not a warning",
      store.cache_get("old item", on=TODAY) is None)
check("absent key is a miss", store.cache_get("never seen", on=TODAY) is None)

# 14) Unresolved strings are queued for review, never dropped.
store.log_unresolved("some artisanal thing from a market stall", day=TODAY)
store.log_unresolved("another mystery", day=TODAY)
queue = json.loads((tmp / "nutrition" / "unresolved.json").read_text())
check("unresolved strings are queued", len(queue) == 2)

# 15) A corrupt month file must not take the bot down mid-conversation.
month = tmp / "nutrition" / "2026-09.json"
month.write_text("{not json at all")
sep = date(2026, 9, 3)
check("corrupt month reads as blank rather than raising",
      store.get_day(sep)["entries"] == [])
check("corrupt file preserved for inspection",
      (tmp / "nutrition" / "2026-09.json.corrupt").exists())
store.add_entry(sep, raw_text="recovery works", kcal=10, confidence="estimate",
                source_rung="llm")
check("writes resume after a corrupt file", len(store.get_day(sep)["entries"]) == 1)

# 16) get_range includes gaps, so a 7-day window cannot silently average over fewer.
rng = store.get_range(TODAY - timedelta(days=6), TODAY)
check("get_range returns every day including blanks", len(rng) == 7)

# 17) Month boundaries: entries land in the right file.
check("September entry lives in the September file",
      json.loads((tmp / "nutrition" / "2026-09.json").read_text())["days"]["2026-09-03"]
      ["entries"][0]["raw_text"] == "recovery works")
check("August day is untouched by the September write",
      len(store.get_day(TODAY)["entries"]) > 0)

print("\n--- filing an entry under a meal ---")
mstore = S.NutritionStore(Path(tempfile.mkdtemp(prefix="meal-")))
MDAY = "2026-08-11"
mstore.add_entry(MDAY, raw_text="protein bar", resolved_name="M&S Protein Bar",
                 kcal=200, logged_at=MDAY + "T08:17")
oats = mstore.add_entry(MDAY, raw_text="oats", resolved_name="M&S Overnight Oats",
                        kcal=322, logged_at=MDAY + "T08:52")
check("a new entry starts with no meal", not (oats.get("meal") or ""))
found = mstore.find_entry(MDAY, "overnight oats")
check("a named item is found by its words",
      found and found["resolved_name"] == "M&S Overnight Oats")
check("naming nothing picks the most recent",
      mstore.find_entry(MDAY, "")["resolved_name"] == "M&S Overnight Oats")
check("a name that matches nothing still falls back rather than failing",
      mstore.find_entry(MDAY, "sausage roll") is not None)
got = mstore.set_meal(MDAY, found["id"], "breakfast")
check("the meal is stored", got and got["meal"] == "breakfast")
check("and it persists",
      [e for e in mstore.get_day(MDAY)["entries"]
       if e["id"] == found["id"]][0]["meal"] == "breakfast")
check("a snack singular is normalised to the bucket the app renders",
      mstore.set_meal(MDAY, found["id"], "snack")["meal"] == "snacks")
check("an unknown bucket is REFUSED rather than invented",
      mstore.set_meal(MDAY, found["id"], "elevenses") is None)
check("and the previous value survives the refusal",
      [e for e in mstore.get_day(MDAY)["entries"]
       if e["id"] == found["id"]][0]["meal"] == "snacks")
check("an unknown entry id changes nothing",
      mstore.set_meal(MDAY, "nope-999", "lunch") is None)

print("\n--- remembering what he says it was NOT ---")
# A correction re-runs a deterministic ladder, so with no memory of the rejected candidate
# it returns the same wrong product: "butter" came back as "Peanut butter, smooth" six
# times on 12 Aug 2026, twice after he had said he never said peanut butter.
estore = S.NutritionStore(Path(tempfile.mkdtemp(prefix="excl-")))
EDAY = "2026-08-12"
check("a fresh day excludes nothing", estore.get_exclusions(EDAY) == [])
estore.add_exclusion(EDAY, "Peanut Butter")
check("an exclusion is stored lowercased", estore.get_exclusions(EDAY) == ["peanut butter"])
estore.add_exclusion(EDAY, "peanut butter")
check("the same phrase twice is stored once",
      estore.get_exclusions(EDAY) == ["peanut butter"])
estore.add_exclusion(EDAY, "cashew butter.")
check("trailing punctuation is stripped before storing",
      estore.get_exclusions(EDAY) == ["peanut butter", "cashew butter"])
check("an empty phrase is ignored rather than stored",
      estore.add_exclusion(EDAY, "  ") == [] and len(estore.get_exclusions(EDAY)) == 2)
check("exclusions persist to the month file",
      json.loads((estore.dir / "2026-08.json").read_text())["days"][EDAY]["exclusions"]
      == ["peanut butter", "cashew butter"])
# Per DAY, deliberately: he is rejecting this match for this item, not telling the app that
# peanut butter is never food.
check("tomorrow starts clean", estore.get_exclusions("2026-08-13") == [])
estore.add_entry(EDAY, raw_text="butter", kcal=37, confidence="label",
                 source_rung="cofid")
check("an exclusion does not disturb the day's entries",
      len(estore.get_day(EDAY)["entries"]) == 1
      and estore.get_exclusions(EDAY) == ["peanut butter", "cashew butter"])

print()
if FAILED:
    print(f"{len(FAILED)} FAILED: " + ", ".join(FAILED))
    sys.exit(1)
print("all checks passed")


# --- update_entry patches amounts, never identity (13 Aug 2026) -------------------------
with tempfile.TemporaryDirectory() as td:
    st = S.NutritionStore(Path(td))
    e = st.add_entry(date(2026, 8, 13), raw_text="omelette", resolved_name="Spanish omelette",
                     kcal=120, carb_g=10, confidence="label", source_rung="manual")
    done = st.update_entry(date(2026, 8, 13), e["id"], kcal=192.0, carb_g=16.0,
                           portion_used_g=160, resolved_name="HACKED")
    check("update_entry patches amounts", done and done["kcal"] == 192.0)
    check("update_entry cannot change identity",
          done["resolved_name"] == "Spanish omelette")
    check("update_entry with nothing patchable is a no-op",
          st.update_entry(date(2026, 8, 13), e["id"], species=["x"]) is None)

if FAILED:
    print(f"{len(FAILED)} FAILED"); sys.exit(1)
print("all checks passed")


# --- the log-editing verbs (13 Aug 2026) -----------------------------------------------
# Retiming and renaming an entry, and remembering a lasting fact about a product. All
# three were edits Jamie had to make through a human operator because the store had no
# path for them.

print("\n--- an entry's time and meal are patchable, its identity is not ---")
D = date(2026, 8, 13)
with tempfile.TemporaryDirectory() as td:
    st = S.NutritionStore(Path(td))
    e = st.add_entry(D, raw_text="rye bread", resolved_name="Rye bread", kcal=83,
                     confidence="label", source_rung="manual",
                     logged_at="2026-08-13T14:02")
    # The app buckets entries into meals by the clock, so an 08:30 slice written up at
    # 14:02 read as lunch and nothing could move it.
    done = st.update_entry(D, e["id"], logged_at="2026-08-13T08:30")
    check("logged_at is patchable", done["logged_at"] == "2026-08-13T08:30")
    check("and it persists to the month file",
          json.loads((st.dir / "2026-08.json").read_text())
          ["days"]["2026-08-13"]["entries"][0]["logged_at"] == "2026-08-13T08:30")
    check("meal is patchable too",
          st.update_entry(D, e["id"], meal="breakfast")["meal"] == "breakfast")
    check("identity is still refused, whatever else is in the same patch",
          st.update_entry(D, e["id"], logged_at="2026-08-13T09:00",
                          resolved_name="HACKED")["resolved_name"] == "Rye bread")
    # Deliberately NOT re-sorted: /undo pops the tail and "delete that" reads the last
    # entry, both meaning "the thing you logged most recently".
    later = st.add_entry(D, raw_text="banana", resolved_name="Banana", kcal=95,
                         confidence="label", source_rung="cofid",
                         logged_at="2026-08-13T15:00")
    st.update_entry(D, later["id"], logged_at="2026-08-13T06:00")
    check("a retime does not reorder the day",
          st.find_entry(D, "")["id"] == later["id"])

print("\n--- rename keeps HIS figures, and only where they are his ---")
with tempfile.TemporaryDirectory() as td:
    st = S.NutritionStore(Path(td))
    # "The 160g was a pack of bbq chicken": the figures came off the pack he was holding,
    # so the name was the only thing wrong with the entry.
    own = st.add_entry(D, raw_text="160g chicken", resolved_name="Chicken breast, raw",
                       kcal=265, protein_g=50, confidence="label", source_rung="manual",
                       ingredients="chicken breast", species=[{"id": "x", "score": 1}])
    done = st.rename_entry(D, own["id"], "BBQ chicken, 160g pack")
    check("a manual entry renames", done and done["resolved_name"] == "BBQ chicken, 160g pack")
    check("and keeps the figures he read off the pack", done["kcal"] == 265.0)
    check("the old name is kept as provenance",
          done["renamed_from"] == "Chicken breast, raw")
    # The old ingredients described a product this entry is not, and the plant count is
    # built from them.
    check("the wrong product's ingredients and species go with the wrong name",
          done["ingredients"] == "" and done["species"] == [])
    check("supplied ingredients are kept when there are some",
          st.rename_entry(D, own["id"], "BBQ chicken", ingredients="chicken, bbq sauce")
          ["ingredients"] == "chicken, bbq sauce")
    check("a rename to nothing is refused", st.rename_entry(D, own["id"], "  ") is None)
    check("an unknown id is refused", st.rename_entry(D, "nope", "x") is None)

    # A lookup's figures are only as good as the name that produced them, so renaming one
    # would leave a food wearing another food's macros. That is a reidentify, not a rename.
    looked_up = st.add_entry(D, raw_text="chicken", resolved_name="Chicken, roasted",
                             kcal=190, confidence="database", source_rung="usda")
    check("a database entry refuses to be renamed",
          st.rename_entry(D, looked_up["id"], "BBQ chicken") is None)
    check("and is left untouched by the refusal",
          st.get_day(D)["entries"][-1]["resolved_name"] == "Chicken, roasted")
    est = st.add_entry(D, raw_text="chicken", resolved_name="Chicken, guessed",
                      kcal=190, confidence="estimate", source_rung="llm")
    check("an estimate refuses too", st.rename_entry(D, est["id"], "BBQ chicken") is None)
    # CoFID and retailer listings are label-grade, and a name he corrects on one of those
    # is the same case as his own pack reading: the figures stand.
    cofid = st.add_entry(D, raw_text="butter", resolved_name="Butter, salted", kcal=37,
                        confidence="label", source_rung="cofid")
    check("a label-grade entry renames whatever rung produced it",
          st.rename_entry(D, cofid["id"], "Kerrygold, salted") is not None)

print("\n--- remembered product facts persist ---")
# "A rego scoop is half a portion" was a fact the bot could hear and not keep, so every
# scoop of REGO cost the same conversation again.
with tempfile.TemporaryDirectory() as td:
    st = S.NutritionStore(Path(td))
    check("no file means no facts, not a crash", st.product_facts() == {})
    rec = st.set_product_fact("SiS REGO", "scoop_g", 25)
    check("a scoop weight is stored", rec and rec["scoop_g"] == 25.0)
    check("the product key is lowercased",
          list(st.product_facts()) == ["sis rego"])
    check("and stamped so a stale fact is visible later",
          st.product_facts()["sis rego"].get("set_at", "").startswith("20"))
    st.set_product_fact("sis rego", "pack_g", 1600)
    both = st.product_facts()["sis rego"]
    check("a second field joins the same product rather than replacing it",
          both["scoop_g"] == 25.0 and both["pack_g"] == 1600.0)
    st.set_product_fact("SiS choco", "means", "SiS GO Energy Choco Fudge bar")
    check("an alias is stored as text",
          st.product_facts()["sis choco"]["means"] == "SiS GO Energy Choco Fudge bar")
    check("the facts survive a fresh store on the same directory",
          S.NutritionStore(Path(td)).product_facts()["sis rego"]["scoop_g"] == 25.0)
    check("it is one file next to the cache, not a day record",
          (st.dir / "product-facts.json").exists())
    # PERMANENT and consulted deterministically, so the gate is at the write.
    check("an unknown field is refused", st.set_product_fact("sis rego", "kcal", 80) is None)
    check("a non-numeric weight is refused",
          st.set_product_fact("sis rego", "scoop_g", "a scoop") is None)
    check("a zero weight is refused", st.set_product_fact("sis rego", "pack_g", 0) is None)
    check("a fact about no product is refused",
          st.set_product_fact("   ", "scoop_g", 25) is None)
    check("an empty alias is refused", st.set_product_fact("x", "means", " ") is None)
    check("and none of the refusals wrote anything",
          set(st.product_facts()) == {"sis rego", "sis choco"})
    (st.dir / "product-facts.json").write_text("{not json")
    check("a corrupt facts file degrades to no facts, not a crash",
          st.product_facts() == {})

if FAILED:
    print(f"{len(FAILED)} FAILED: " + ", ".join(FAILED)); sys.exit(1)
print("all checks passed")
