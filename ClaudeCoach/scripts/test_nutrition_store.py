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

# 14b) THE QUEUE IS READ BACK NOW (18 Aug 2026). It was write-only for as long as it
#      existed, which is how ten rows piled up on the VM with an item stuck in there for
#      most of a day and the athlete never told. Every check below is a reader.
check("the queue reads back, oldest first",
      [r["raw_text"] for r in store.read_unresolved()]
      == ["some artisanal thing from a market stall", "another mystery"])
check("a read fills the whole row shape rather than handing back holes",
      store.read_unresolved()[0]["times_seen"] == 1
      and store.read_unresolved()[0]["last_seen_on"] == TODAY.isoformat()
      and store.read_unresolved()[0]["nudged_on"] == [])

# 14c) A repeat BUMPS, it does not append. Three rows for one stuck item need one
#      mapping added to species.json, and would have read the same item back to him
#      three times in a single nudge.
_tmr = TODAY + timedelta(days=1)
store.log_unresolved("  ANOTHER Mystery ", day=_tmr)
store.log_unresolved("another mystery", day=_tmr)
_q = store.read_unresolved()
check("a repeat does not add a second row", len(_q) == 2)
_myst = [r for r in _q if r["raw_text"] == "another mystery"][0]
check("case and stray space are the same string", _myst["times_seen"] == 3)
check("seen_on stays the FIRST sighting - how long it has been stuck is the point",
      _myst["seen_on"] == TODAY.isoformat())
check("and last_seen_on carries the latest", _myst["last_seen_on"] == _tmr.isoformat())
check("a different portion is a DIFFERENT item, never folded in",
      len(store.read_unresolved()) == 2
      and store.log_unresolved("62g of the thing", day=_tmr)["times_seen"] == 1
      and len(store.read_unresolved()) == 3)

# 14d) Being nudged about is recorded, so a row cannot nag for ever.
store.mark_unresolved_nudged("another mystery", day=_tmr)
store.mark_unresolved_nudged("another mystery", day=_tmr)
_myst = [r for r in store.read_unresolved() if r["raw_text"] == "another mystery"][0]
check("a nudge is recorded once per day, not once per send",
      _myst["nudged_on"] == [_tmr.isoformat()])
check("marking a row that is gone is None, never a new row",
      store.mark_unresolved_nudged("never queued at all", day=_tmr) is None
      and len(store.read_unresolved()) == 3)
try:
    store.mark_unresolved_nudged("another mystery")
    check("mark_unresolved_nudged requires an explicit local day too", False)
except TypeError:
    check("mark_unresolved_nudged requires an explicit local day too", True)

# 14e) Once it is logged it stops being open - the drain commit_one calls.
check("clearing a queued row removes exactly it",
      store.clear_unresolved("Another Mystery  ") == 1
      and [r["raw_text"] for r in store.read_unresolved()]
      == ["some artisanal thing from a market stall", "62g of the thing"])
check("clearing something never queued is 0, not an error",
      store.clear_unresolved("a thing that resolved fine") == 0)
check("and an empty string can never truncate the file",
      store.clear_unresolved("") == 0 and store.clear_unresolved("   ") == 0
      and len(store.read_unresolved()) == 2)

# 14f) LEGACY ROWS. The ten live rows on the VM predate every field above and carry
#      only raw_text and seen_on. They must read, dedupe against, nudge and clear
#      without one of them.
_legacy = S.NutritionStore(Path(tempfile.mkdtemp(prefix="nut-unres-legacy-")))
(_legacy.dir).mkdir(parents=True, exist_ok=True)
(_legacy.dir / "unresolved.json").write_text(json.dumps(
    [{"raw_text": "same Co-op item as yesterday", "seen_on": "2026-08-16"},
     {"raw_text": "unspecified item weighed 62g", "seen_on": "2026-08-17"}]))
_lr = _legacy.read_unresolved()
check("a legacy row reads with the new fields defaulted",
      len(_lr) == 2 and _lr[0]["times_seen"] == 1
      and _lr[0]["last_seen_on"] == "2026-08-16" and _lr[0]["nudged_on"] == [])
check("reading a legacy queue does not rewrite it on disk",
      json.loads((_legacy.dir / "unresolved.json").read_text())[0]
      == {"raw_text": "same Co-op item as yesterday", "seen_on": "2026-08-16"})
_legacy.log_unresolved("same Co-op item as yesterday", day=date(2026, 8, 18))
_up = _legacy.read_unresolved()[0]
check("a recurrence upgrades a legacy row in place, keeping its original seen_on",
      len(_legacy.read_unresolved()) == 2 and _up["times_seen"] == 2
      and _up["seen_on"] == "2026-08-16" and _up["last_seen_on"] == "2026-08-18")
check("a legacy row can be nudged and cleared like any other",
      _legacy.mark_unresolved_nudged("unspecified item weighed 62g",
                                     day=date(2026, 8, 18))["nudged_on"] == ["2026-08-18"]
      and _legacy.clear_unresolved("unspecified item weighed 62g") == 1
      and len(_legacy.read_unresolved()) == 1)

# 14g) A corrupt queue must degrade to empty, exactly like a corrupt month file - the
#      bot logging food must never die because a review queue will not parse.
_broken = S.NutritionStore(Path(tempfile.mkdtemp(prefix="nut-unres-broken-")))
_broken.dir.mkdir(parents=True, exist_ok=True)
(_broken.dir / "unresolved.json").write_text("[not json at all")
check("a corrupt queue reads as empty rather than raising", _broken.read_unresolved() == [])
check("and writes resume over the top of it",
      _broken.log_unresolved("first good row", day=TODAY)["times_seen"] == 1
      and len(_broken.read_unresolved()) == 1)

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
# Was "a new entry starts with no meal", when meals were derived at publish time. An
# entry now lands in a bucket the moment it is written, and says the clock chose it.
check("a new entry is filed by the clock, and says so",
      oats.get("meal") == "breakfast" and oats.get("meal_inferred") is True)
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

print("\n--- which meal, decided at LOG time and honest about who decided ---")
# Jamie, 13 Aug 2026: "improve time of meal logging, often added to wrong category."
# Meals were derived when the app was published, from the log timestamp alone, so an 08:30
# slice of rye bread written up at 13:49 was lunch and nothing at log time read the message.
for hhmm, want in (("00:01", "breakfast"), ("08:30", "breakfast"), ("10:59", "breakfast"),
                   ("11:00", "lunch"), ("13:49", "lunch"), ("14:59", "lunch"),
                   ("15:00", "snacks"), ("17:29", "snacks"),
                   ("17:30", "dinner"), ("21:15", "dinner")):
    check(f"{hhmm} falls in {want}", S.meal_from_clock(f"2026-08-13T{hhmm}") == want)
# A time it cannot read gives NO meal rather than a default one: a bucket chosen from
# nothing is indistinguishable from a bucket chosen from the clock.
for bad in ("", None, "2026-08-13", "2026-08-13T24:00", "2026-08-13Txx:yy"):
    check(f"{bad!r} yields no meal at all", S.meal_from_clock(bad) == "")
check("aliases normalise on one path only",
      (S.normalise_meal("Supper"), S.normalise_meal("snack"),
       S.normalise_meal("brunch"), S.normalise_meal("elevenses"))
      == ("dinner", "snacks", "breakfast", ""))

cstore = S.NutritionStore(Path(tempfile.mkdtemp(prefix="meal-clock-")))
CDAY = "2026-08-13"
# The exemplar: breakfast eaten at 08:30, written up at 13:49. The stated time is on the
# entry, so the fallback reads THAT and not the moment he typed it.
rye = cstore.add_entry(CDAY, raw_text="rye bread", resolved_name="Rye bread", kcal=83,
                       logged_at=CDAY + "T08:30")
check("a stated time files the entry by the time he ate, not the time he typed",
      rye["meal"] == "breakfast" and rye["meal_inferred"] is True)
said = cstore.add_entry(CDAY, raw_text="porridge", resolved_name="Porridge", kcal=250,
                        logged_at=CDAY + "T13:49", meal="breakfast")
check("a meal HE named beats the clock and is not marked as a guess",
      said["meal"] == "breakfast" and said["meal_inferred"] is False)
check("his own word for it is normalised, not rejected",
      cstore.add_entry(CDAY, raw_text="curry", resolved_name="Curry", kcal=700,
                       logged_at=CDAY + "T12:00", meal="Supper")["meal"] == "dinner")
check("a meal word nothing renders falls back to the clock",
      cstore.add_entry(CDAY, raw_text="crisps", resolved_name="Crisps", kcal=180,
                       logged_at=CDAY + "T16:00", meal="elevenses")["meal"] == "snacks")

# IN-SESSION FUEL IS NOT A MEAL. A gel taken at 13:00 is not lunch, and filing it as lunch
# makes the day's meals look like they contain the fuelling. Forced rather than refused: a
# mislabelled gel must not cost him the log.
gel = cstore.add_entry(CDAY, raw_text="gel on the bike", resolved_name="SiS GO gel",
                       kcal=87, logged_at=CDAY + "T13:00", in_session=True,
                       meal="lunch")
check("in-session fuel never gets a meal, whatever was passed",
      gel["meal"] == "" and gel["meal_inferred"] is False)

print("\n--- a retime moves a GUESSED meal with it, and leaves a stated one alone ---")
# Meals used to be re-derived from logged_at on every publish, so retiming an entry moved
# its bucket for free. Freezing the meal at log time silently took that away.
late = cstore.add_entry(CDAY, raw_text="toast", resolved_name="Toast", kcal=180,
                        logged_at=CDAY + "T13:49")
check("it starts as lunch, by the clock", late["meal"] == "lunch")
moved = cstore.update_entry(CDAY, late["id"], logged_at=CDAY + "T08:30")
check("retiming it to 08:30 makes it breakfast",
      moved["meal"] == "breakfast" and moved["meal_inferred"] is True)
kept = cstore.update_entry(CDAY, said["id"], logged_at=CDAY + "T14:00")
check("a meal he STATED survives a retime - a late breakfast is a real thing",
      kept["meal"] == "breakfast" and kept["meal_inferred"] is False)
check("and a retime does not hand a meal to in-session fuel",
      cstore.update_entry(CDAY, gel["id"], logged_at=CDAY + "T09:00")["meal"] == "")
check("an explicit meal in the same patch is not overruled by the clock",
      cstore.update_entry(CDAY, late["id"], logged_at=CDAY + "T20:00",
                          meal="breakfast")["meal"] == "breakfast")

print("\n--- correcting the meal, and moving fuel in and out ---")
told = cstore.set_meal(CDAY, late["id"], "snack")
check("set_meal normalises and stops calling it a guess",
      told["meal"] == "snacks" and told["meal_inferred"] is False)
check("an unknown bucket is still refused", cstore.set_meal(CDAY, late["id"], "brunchy")
      is None)
# publish buckets a STATED meal ahead of its in-session check, so a stale meal on an entry
# he has just called in-session fuel keeps rendering it under that meal.
into = cstore.set_in_session(CDAY, late["id"], True)
check("moving an entry into session strips its meal",
      into["meal"] == "" and into["meal_inferred"] is False)
out = cstore.set_in_session(CDAY, late["id"], False)
# That entry now stands at 20:00 (retimed above), so the clock says dinner - the stated
# "snacks" was stripped when it went in-session and is not resurrected. Out-of-session
# food must land in SOME bucket, and the clock is all there is left to go on.
check("and moving it back out gives it a bucket again, by the clock",
      out["logged_at"].endswith("T20:00")
      and out["meal"] == "dinner" and out["meal_inferred"] is True)

print("\n--- a photographed label supersedes an entry's lookup figures (14 Aug 2026) ---")
# He logged "Coop Chianti beef pizza" by name at a web figure of 1,147 kcal and then sent the
# pack's label. Applying it has to move the FIGURES and the name together: a label is the
# manufacturer's own panel, so keeping the lookup's name beside the pack's numbers would
# leave an entry describing neither.
with tempfile.TemporaryDirectory() as td:
    st = S.NutritionStore(Path(td))
    pizza = st.add_entry(D, raw_text="coop chianti beef pizza",
                         resolved_name="Coop Chianti beef pizza", kcal=1147, protein_g=52,
                         carb_g=130, fat_g=44, confidence="database", source_rung="web",
                         ingredients="wheat flour, tomato, beef",
                         species=[{"id": "wheat", "score": 1}])
    done = st.apply_label_to_entry(D, pizza["id"], {
        "resolved_name": "Chianti beef pizza, stone baked", "kcal": 482.0,
        "protein_g": 22.0, "carb_g": 58.0, "fat_g": 17.0, "dietary_sodium_mg": 640.4,
        "per_100g": {"kcal": 241.0}, "portion_used_g": 200.0, "pack_g": 400.0,
        "ingredients": "wheat flour, mozzarella, beef", "source_url": "photo of the product label"})
    check("the label's figures replace the lookup's", done["kcal"] == 482.0
          and done["protein_g"] == 22.0 and done["fat_g"] == 17.0)
    check("sodium is rounded to the milligram like everywhere else",
          done["dietary_sodium_mg"] == 640)
    check("the entry is now label data, from the pack he was holding",
          done["confidence"] == "label" and done["source_rung"] == "manual"
          and "label" in done["source_url"])
    check("the per-100g basis travels, so the next 'I had 160g' is arithmetic",
          done["per_100g"]["kcal"] == 241.0 and done["pack_g"] == 400.0)
    check("the portion is the label's, and not flagged as a guess",
          done["portion_used_g"] == 200.0 and done["portion_estimated"] is False
          and "from the label" in done["portion_assumed"])
    check("the pack's own name wins, with the lookup's kept as provenance",
          done["resolved_name"] == "Chianti beef pizza, stone baked"
          and done["renamed_from"] == "Coop Chianti beef pizza")
    check("and the wrong product's species go with its name",
          done["species"] == [] and "mozzarella" in done["ingredients"])
    check("it is ONE entry, which is the whole point",
          len(st.get_day(D)["entries"]) == 1)
    check("an id that is not there is refused rather than written blind",
          st.apply_label_to_entry(D, "nope", {"kcal": 100}) is None)
    check("and a label that is not a dict is refused",
          st.apply_label_to_entry(D, pizza["id"], None) is None)
    # A COSTED MEAL'S COMPONENT ROWS LIVE IN `ingredients`, and the app renders them under
    # the entry's total. With the total replaced by a pack's panel those rows contradict the
    # heading above them, so a label that restates no ingredients drops them - the same rule
    # the bot's drop_stale_breakdown follows for a pending offer.
    meal = st.add_entry(D, raw_text="a large stir fry",
                        resolved_name="Large beef stir-fry with egg noodles", kcal=935,
                        confidence="estimate", source_rung="llm",
                        ingredients="egg noodles, cooked; rump steak, grilled; soy sauce",
                        species=[{"id": "wheat", "score": 1}])
    swapped = st.apply_label_to_entry(D, meal["id"], {"kcal": 480.0})
    check("a breakdown that no longer adds up goes with the figures it described",
          swapped["kcal"] == 480.0
          and swapped["ingredients"] == "Large beef stir-fry with egg noodles")
    check("but the plants it was credited stay, being their own field and the same food",
          swapped["species"] == [{"id": "wheat", "score": 1}])
    # And a label that DOES restate the ingredients simply uses them.
    same = st.apply_label_to_entry(D, pizza["id"],
                                   {"resolved_name": "Chianti beef pizza, stone baked",
                                    "kcal": 500.0, "ingredients": "wheat flour, mozzarella"})
    check("a label that restates the ingredients keeps them",
          same["kcal"] == 500.0 and "mozzarella" in same["ingredients"])

print("\n--- move_entry: an entry filed on the wrong day (16 Aug 2026) ---")
# "Dinner last night was a big salad" was costed correctly and written to TODAY, and there
# was no verb for moving it: retime could change an entry's clock time and nothing could
# change its date. The entry was moved by hand in the month file.
mv = S.NutritionStore(Path(tempfile.mkdtemp(prefix="nut-move-")))
_TOD, _YDAY = date(2026, 8, 16), date(2026, 8, 15)
salad = mv.add_entry(_TOD, raw_text="big salad with chicken and avocado",
                     resolved_name="Large chicken and avocado salad", kcal=1352,
                     protein_g=78, carb_g=41, fat_g=92, fibre_g=14,
                     dietary_sodium_mg=1100, portion_g=650, confidence="estimate",
                     source_rung="llm", logged_at=f"{_TOD.isoformat()}T07:41",
                     meal="dinner", ingredients="chicken, avocado, rocket, tomato",
                     species=[{"id": "avocado", "score": 1},
                              {"id": "tomato", "score": 1}])
# A second entry that must not move, and a portion field add_entry does not name - which
# is the whole reason this is a deep copy rather than a re-add through add_entry.
mv.update_entry(_TOD, salad["id"], portion_used_g=650.0, portion_estimated=True,
                portion_assumed="portions worked out from your description")
mv.add_entry(_TOD, raw_text="flat white", resolved_name="Flat white", kcal=120,
             confidence="label", source_rung="cofid")
before = dict(mv.get_day(_TOD)["entries"][0])
res = mv.move_entry(_TOD, salad["id"], _YDAY)
moved, removed = res["moved"], res["removed"]
check("the entry lands on the target day",
      [e["id"] for e in mv.get_day(_YDAY)["entries"]] == [moved["id"]])
check("and is gone from the day it was on",
      removed is not None
      and salad["id"] not in [e["id"] for e in mv.get_day(_TOD)["entries"]])
check("the entry it was logged beside is untouched",
      [e["resolved_name"] for e in mv.get_day(_TOD)["entries"]] == ["Flat white"])
check("every field survives the move, including the ones add_entry never names",
      all(moved.get(k) == before.get(k) for k in before if k not in ("id", "logged_at")))
check("provenance and species come with it",
      moved["source_rung"] == "llm" and moved["confidence"] == "estimate"
      and moved["species"] == [{"id": "avocado", "score": 1},
                               {"id": "tomato", "score": 1}])
check("his time of day is kept and only the date changes",
      moved["logged_at"] == f"{_YDAY.isoformat()}T07:41")
check("the id is the target day's own, because ids carry their date",
      moved["id"].startswith(_YDAY.isoformat()) and moved["id"] != salad["id"])
check("and the totals moved with it",
      round(mv.day_totals(_YDAY)["kcal"]) == 1352
      and round(mv.day_totals(_TOD)["kcal"]) == 120)
# A stated time replaces the clock; the meal comes with it as HIS word, not a guess.
res2 = mv.move_entry(_YDAY, moved["id"], _TOD,
                     logged_at=f"{_TOD.isoformat()}T20:00", meal="dinner")
check("a supplied stamp and meal are applied on arrival",
      res2["moved"]["logged_at"] == f"{_TOD.isoformat()}T20:00"
      and res2["moved"]["meal"] == "dinner"
      and res2["moved"]["meal_inferred"] is False)
check("an entry that is not on the source day is refused, not invented",
      mv.move_entry(_YDAY, "nope-001", _TOD) is None)
check("and a move to the day it is already on is refused rather than re-idded",
      mv.move_entry(_TOD, res2["moved"]["id"], _TOD) is None)
# The month lock is per month FILE, so this is two acquisitions and cannot be atomic.
_prev = date(2026, 7, 31)
res3 = mv.move_entry(_TOD, res2["moved"]["id"], _prev)
check("a move across a month boundary lands in the other month file",
      res3 and [e["id"] for e in mv.get_day(_prev)["entries"]] == [res3["moved"]["id"]]
      and (mv.dir / "2026-07.json").exists())

print("\n--- stated_fields survives the commit (17 Aug 2026) ---")
# rescale_item refuses to recompute a macro the athlete gave, and it reads `stated_fields`
# off the item to know which. add_entry had no such keyword, so the flag was dropped at the
# commit and the stored row could not tell his 21 g of protein from a lookup's. Reproduced
# end to end at 21.0 -> 42.0 -> 63.0 over two corrections against the committed row.
sf = S.NutritionStore(Path(tempfile.mkdtemp(prefix="nut-stated-")))
_SD = date(2026, 8, 12)
his = sf.add_entry(_SD, raw_text="chicken salad with 21g protein",
                   resolved_name="Chicken salad", kcal=240, protein_g=21, carb_g=12,
                   fat_g=14, confidence="estimate", source_rung="llm",
                   stated_fields=["protein_g"])
check("the figures he stated are named on the returned entry",
      his["stated_fields"] == ["protein_g"])
check("and are there when the month file is read back",
      sf.get_day(_SD)["entries"][0]["stated_fields"] == ["protein_g"])
check("they survive a round trip through the JSON on disk",
      json.loads((sf.dir / "2026-08.json").read_text())["days"][_SD.isoformat()]
      ["entries"][0]["stated_fields"] == ["protein_g"])

# THE SHAPE OF EVERY OTHER ROW IS UNCHANGED. These month files are a record he keeps, and
# almost no entry has a stated figure: the key is written only when there is something to
# say, rather than a `[]` on every row from today onwards.
plain = sf.add_entry(_SD, raw_text="flat white", resolved_name="Flat white", kcal=120,
                     confidence="label", source_rung="cofid")
check("an entry with no stated figure does not grow the key at all",
      "stated_fields" not in plain)
check("and neither does one handed an empty list",
      "stated_fields" not in sf.add_entry(_SD, raw_text="oat milk", kcal=40,
                                          stated_fields=[]))
# A row written before today has no key either, which is the same absence - so there is
# nothing to migrate and nothing that reads back wrong.
old = sf.get_day(_SD)["entries"][1]
check("an old row lacking the key reads back as no stated figures",
      (old.get("stated_fields") or ()) == ())
check("and can still be patched like any other",
      sf.update_entry(_SD, old["id"], kcal=130)["kcal"] == 130.0)

check("stated_fields is patchable, or a row that lost it starts multiplying his number",
      sf.update_entry(_SD, old["id"], stated_fields=["kcal"])["stated_fields"] == ["kcal"])
check("and a patch that says nothing about it leaves his figures named",
      sf.update_entry(_SD, his["id"], kcal=300)["stated_fields"] == ["protein_g"])
check("a move carries it to the new day, like every other field",
      sf.move_entry(_SD, his["id"], date(2026, 8, 13))["moved"]["stated_fields"]
      == ["protein_g"])
sf.move_entry(date(2026, 8, 13), sf.get_day(date(2026, 8, 13))["entries"][0]["id"], _SD)

print("\n--- a label supersedes his figure and the claim to it (17 Aug 2026) ---")
# Reachable only because entries carry `stated_fields` from today. The label loop replaces
# the number his claim was about with the manufacturer's, so a row left claiming it would
# have the next rescale hold the PACK's protein and fmt_confirm caption it "your own
# figure" - the same fabricated attribution, pointing the other way.
lb = S.NutritionStore(Path(tempfile.mkdtemp(prefix="nut-stated-lb-")))
row = lb.add_entry(_SD, raw_text="chicken salad with 21g protein and 3g fibre",
                   resolved_name="Chicken salad", kcal=240, protein_g=21, fibre_g=3,
                   confidence="estimate", source_rung="llm",
                   stated_fields=["protein_g", "fibre_g"])
part = lb.apply_label_to_entry(_SD, row["id"], {"kcal": 310.0, "protein_g": 26.0})
check("the field the panel replaced stops being his",
      part["protein_g"] == 26.0 and "protein_g" not in part["stated_fields"])
check("but a field the panel said nothing about is still his",
      part["fibre_g"] == 3.0 and part["stated_fields"] == ["fibre_g"])
whole = lb.apply_label_to_entry(_SD, row["id"], {"fibre_g": 5.5})
check("and when the panel has replaced them all the key goes, not an empty list",
      whole["fibre_g"] == 5.5 and "stated_fields" not in whole)
bare = lb.add_entry(_SD, raw_text="pack of nuts", kcal=200, confidence="label",
                    source_rung="manual")
check("a label against a row that never claimed anything is untouched by all this",
      "stated_fields" not in lb.apply_label_to_entry(_SD, bare["id"], {"kcal": 235.0}))

if FAILED:
    print(f"{len(FAILED)} FAILED: " + ", ".join(FAILED)); sys.exit(1)
print("all checks passed")
