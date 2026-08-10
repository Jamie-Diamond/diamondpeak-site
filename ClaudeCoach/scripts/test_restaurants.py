#!/usr/bin/env python3
"""Offline tests for lib/restaurants.py. Run: python3 ClaudeCoach/scripts/test_restaurants.py

WHAT THIS GUARDS
Reading a nutrition matrix by column POSITION is the worst failure available here: a fat
figure logged as carbohydrate stays plausible, raises nothing, and quietly wrongs the day.
So the parser is only allowed to answer when two identities hold - protein x 4 + carb x 4
+ fat x 9 accounts for the stated energy, and salt equals sodium x 2.5 - and the fixtures
below deliberately break each one.

The second thing it guards is dish matching. "steamed brown rice" must never answer a
salmon bowl just because the order line mentions brown rice, which is the same coverage
failure that let CoFID answer a Wagamama order with raw grain.
"""
import sys
from pathlib import Path

_here = Path(__file__).resolve().parent
for cand in (_here / "lib", _here.parent / "lib"):
    if (cand / "restaurants.py").exists():
        sys.path.insert(0, str(cand))
        break
import restaurants as RS  # noqa: E402

FAILED = []


def check(name, cond, detail=""):
    print(("PASS" if cond else "FAIL"), name, (f"  {detail}" if not cond and detail else ""))
    if not cond:
        FAILED.append(name)


HEADER = ("<div>energy (kcal)</div><div>Energy (kj)</div><div>protein (g)</div>"
          "<div>carb (g)</div><div>of which sugars (g)</div><div>fat (g)</div>"
          "<div>sat fat (g)</div><div>sodium (g)</div><div>salt (g)</div>"
          "<div>fibre (g)</div>")


def dish(name, kcal, kj, prot, carb, sug, fat, sat, sodium, salt, fib):
    """One dish card in the shape the real matrix uses: a data-calories anchor, the name,
    then the ten values in header order."""
    vals = "".join(f"<td>{v}</td>" for v in
                   (kcal, kj, prot, carb, sug, fat, sat, sodium, salt, fib))
    return (f'<div data-calories="{kcal}"><span>{name}</span></div>'
            f'<div class="allergens"><span>May contain sulphites</span></div>'
            f"<table><tr>{vals}</tr></table>")


GOOD = HEADER + "".join([
    dish("gochujang salmon rice bowl (may contain small bones)",
         786, "3,281", 36.3, 69.7, 20.8, 39.4, 4.9, 1.6, 4.0, 5.4),
    dish("extra salmon", 282, 1170, 27.2, 0.0, 0.0, 19.2, 4.1, 0.16, 0.4, 0.0),
    dish("edamame with chilli + garlic salt", 287, 1200, 24.3, 10.2, 1.6, 13.3, 2.0,
         0.4, 1.0, 13.9),
    dish("soy sauce", 8, 33, 0.8, 1.1, 0.5, 0.0, 0.0, 0.64, 1.6, 0.0),
    dish("sticky rice | 250g", 378, 1580, 8.5, 84.1, 0.1, 1.3, 0.3, 0.0, 0.0, 4.0),
    dish("steamed brown rice | 250g", 370, 1547, 9.3, 75.0, 0.6, 2.0, 0.4, 0.0, 0.0, 7.5),
])

print("--- the parse ---")
rows = RS.parse_tenkites(GOOD)
check(f"all six dishes parse (got {len(rows)})", len(rows) == 6, [r["name"] for r in rows])
by = {r["name"]: r for r in rows}
check("the parenthetical is stripped from the name",
      "gochujang salmon rice bowl" in by, sorted(by))
check("the bowl carries the published macros",
      by["gochujang salmon rice bowl"]["protein_g"] == 36.3
      and by["gochujang salmon rice bowl"]["carb_g"] == 69.7)
check("a stated side weight is kept",
      by["steamed brown rice"]["portion_g"] == 250.0)

print("\n--- a matrix that reordered its columns is refused outright ---")
check("no header, no parse", RS.parse_tenkites("<div>dish</div>") == [])
check("header check is explicit", RS.header_present(GOOD) is True)

print("\n--- rows whose numbers do not hang together are DROPPED ---")
# carb and fat swapped: 69.7 g of fat and 39.4 g of carb would be ~1000 kcal, not 786.
swapped = HEADER + dish("mystery bowl", 786, 3281, 36.3, 39.4, 20.8, 69.7, 4.9, 1.6,
                        4.0, 5.4)
check("a fat/carb swap fails the energy identity",
      RS.parse_tenkites(swapped) == [], RS.parse_tenkites(swapped))
# salt must be sodium x 2.5; 4.0 against 0.2 says those two columns are not what we think.
bad_salt = HEADER + dish("mystery side", 300, 1250, 10.0, 40.0, 2.0, 10.0, 2.0, 0.2,
                         4.0, 3.0)
check("salt that is not 2.5x sodium fails the salt identity",
      RS.parse_tenkites(bad_salt) == [])
good_row = HEADER + dish("plain side", 300, 1250, 10.0, 40.0, 2.0, 10.0, 2.0, 0.8, 2.0, 3.0)
check("a row that satisfies both identities is kept",
      len(RS.parse_tenkites(good_row)) == 1)

print("\n--- dish matching ---")
LINE = "gochujang salmon rice bowl with brown rice and extra salmon, Wagamama"
got = RS.match_dish(rows, LINE)
check("the order line matches the bowl", got and got["name"] == "gochujang salmon rice bowl",
      got and got["name"])
check("THE OLD BUG: brown rice does not answer a salmon bowl",
      got and "rice bowl" in got["name"] and got["kcal"] == 786.0)
check("the edamame side matches its own row",
      (RS.match_dish(rows, "edamame with chilli and garlic salt, Wagamama") or {})
      .get("name") == "edamame with chilli + garlic salt")
check("an unrelated dish matches nothing",
      RS.match_dish(rows, "chicken katsu curry, Wagamama") is None)
check("a bare vendor name matches nothing",
      RS.match_dish(rows, "Wagamama") is None)

print("\n--- modifiers and swaps ---")
check("extra salmon is spotted as a modifier",
      any("salmon" in m for m in RS.unaccounted_modifiers(LINE,
                                                         "gochujang salmon rice bowl")))
out = {"kcal": 786.0, "protein_g": 36.3, "carb_g": 69.7, "fat_g": 39.4, "fibre_g": 5.4}
groups = [{"component": "rice", "default_row": "sticky rice",
           "applies_to_tokens": ["bowl", "donburi"],
           "options": [{"row": "steamed brown rice", "match": ["brown rice"]}]}]
applied = RS.apply_swaps(out, LINE, by["gochujang salmon rice bowl"], rows, groups)
check("the brown rice swap is applied", applied and "brown" in applied[0], applied)
check("and it is the DIFFERENCE, not the whole side",
      out["kcal"] == 778.0, out["kcal"])
check("fibre moves by the published difference (+3.5)",
      out["fibre_g"] == 8.9, out["fibre_g"])
out2 = {"kcal": 287.0, "fibre_g": 13.9}
check("a swap is not applied to a dish it cannot apply to",
      RS.apply_swaps(out2, "edamame with brown rice on the side",
                     by["edamame with chilli + garlic salt"], rows, groups) == []
      and out2["kcal"] == 287.0)

print("\n--- vendor identification ---")
reg = {"wagamama": {"display": "Wagamama", "aliases": ["wagamama", "wagamamas"]},
       "pizza": {"display": "Pizza", "aliases": ["pizza"]}}
for name in ("Wagamama", "wagamama - Camden", "WAGAMAMAS", "wagamama (halal)"):
    check(f"{name!r} identifies the chain",
          RS.find_vendor(name, reg)[0] == "wagamama", RS.find_vendor(name, reg)[0])
check("an unknown vendor returns nothing, so the ladder falls through",
      RS.find_vendor("Bob's Kebab House", reg)[0] is None)

print("\n--- a size is part of a dish's identity ---")
sizes = HEADER + dish("9 inch margherita", 700, 2900, 30.0, 90.0, 8.0, 25.0, 10.0,
                      0.8, 2.0, 4.0) + dish(
    "12 inch margherita", 1200, 5000, 52.0, 150.0, 12.0, 44.0, 18.0, 1.4, 3.5, 7.0)
srows = RS.parse_tenkites(sizes)
check("both sizes parse", len(srows) == 2, [r["name"] for r in srows])
check("a 12 inch order does not match the 9 inch row",
      (RS.match_dish(srows, "12 inch margherita, Franco Manca") or {}).get("kcal")
      == 1200.0,
      (RS.match_dish(srows, "12 inch margherita, Franco Manca") or {}).get("name"))
check("and a 9 inch order does not match the 12 inch row",
      (RS.match_dish(srows, "9 inch margherita, Franco Manca") or {}).get("kcal")
      == 700.0)

print("\n--- discovery: a vendor nobody approved in advance ---")
import shutil          # noqa: E402
import tempfile         # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="rtest-"))
_real_download = RS._download
CALLS = {"download": 0, "discover": 0}


def stub_download(url):
    CALLS["download"] += 1
    if url == "https://menus.example.com/nandos/matrix":
        return GOOD_BIG
    if url == "https://example.com/not-a-matrix":
        return "<html><body>calories only, no macros</body></html>"
    raise OSError("host unreachable")


# A matrix big enough to earn label confidence, built from the same shape as the real one.
GOOD_BIG = HEADER + "".join(
    dish(f"dish number {i}", 300 + i, 1250, 10.0, 40.0, 2.0, 10.0, 2.0, 0.8, 2.0, 3.0)
    for i in range(25))

RS._download = stub_download


def discover_ok(vendor):
    CALLS["discover"] += 1
    return {"official_site": "nandos.co.uk",
            "nutrition_url": "https://menus.example.com/nandos/matrix",
            "platform": "tenkites", "many_dishes_with_macros": True}


def discover_nothing(vendor):
    CALLS["discover"] += 1
    return {"official_site": "bobskebabs.example", "nutrition_url": None,
            "notes": "publishes no per-dish macros"}


def discover_unparseable(vendor):
    CALLS["discover"] += 1
    return {"nutrition_url": "https://example.com/not-a-matrix", "platform": "unknown"}


key, entry = RS.learn_vendor("Nando's", TMP, discover_ok, now=1000.0)
check("an unknown chain is discovered and verified", key == "nando-s", key)
check("and it records WHAT it verified",
      entry and entry.get("rows_verified") == 25 and "identities held" in entry["verified"],
      entry)
check("a learned vendor has no swap groups, since those cannot be discovered",
      entry.get("swap_groups") == [])

CALLS["discover"] = 0
RS.learn_vendor("Nando's", TMP, discover_ok, now=2000.0)
check("a second order does not re-discover", CALLS["discover"] == 0)

got = RS.lookup("Nando's", "dish number 7", TMP, registry={}, discover=discover_ok)
check("a dish resolves from the learned source", got and got["kcal"] == 307.0, got)
check("25 consistent rows earns LABEL confidence",
      got and got["confidence"] == "label", got and got["confidence"])

# Too few rows is a partial extraction, not an authoritative one.
RS._download = lambda url: GOOD          # the 6-row fixture
small = RS.lookup("Wagamama", "soy sauce", TMP,
                  registry={"wagamama": {"display": "Wagamama", "platform": "tenkites",
                                         "nutrition_url": "https://x/y",
                                         "aliases": ["wagamama"]}})
check("a thin extraction is downgraded to database, not passed off as label",
      small and small["confidence"] == "database", small and small["confidence"])
RS._download = stub_download

CALLS["discover"] = 0
key2, _ = RS.learn_vendor("Bobs Kebab House", TMP, discover_nothing, now=3000.0)
check("a vendor with no published data returns nothing", key2 is None)
RS.learn_vendor("Bobs Kebab House", TMP, discover_nothing, now=3100.0)
check("and the miss is remembered rather than re-searched every time",
      CALLS["discover"] == 1, CALLS["discover"])
RS.learn_vendor("Bobs Kebab House", TMP, discover_nothing,
                now=3000.0 + RS.NEGATIVE_TTL_S + 1)
check("but the miss expires, so a chain that starts publishing is picked up",
      CALLS["discover"] == 2, CALLS["discover"])

key3, _ = RS.learn_vendor("Someplace New", TMP, discover_unparseable, now=4000.0)
check("an unreadable format is refused rather than half-parsed", key3 is None)
rec = RS.load_learned(TMP).get("someplace-new") or {}
check("and its URL is kept as a sample for the next parser",
      rec.get("tried_url") == "https://example.com/not-a-matrix"
      and "sample" in (rec.get("reason") or ""), rec)

RS._download = _real_download
shutil.rmtree(TMP, ignore_errors=True)

print()
if FAILED:
    print(f"{len(FAILED)} FAILED: " + ", ".join(FAILED))
    sys.exit(1)
print("all restaurant checks passed")
