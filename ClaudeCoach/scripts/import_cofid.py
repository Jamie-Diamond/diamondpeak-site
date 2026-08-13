#!/usr/bin/env python3
"""import_cofid.py - convert the published PHE CoFID spreadsheet into config/cofid.json.

Source: McCance and Widdowson's The Composition of Foods Integrated Dataset, 2021
edition, published 19 March 2021 by Public Health England.
  page:  https://www.gov.uk/government/publications/composition-of-foods-integrated-dataset-cofid
  xlsx:  reference/McCance_Widdowsons_Composition_of_Foods_Integrated_Dataset_2021.xlsx

Two sheets are read and joined on the food code: "1.3 Proximates" carries the macros
and "1.4 Inorganics" carries sodium.

THREE DECISIONS WORTH KNOWING, because each one could quietly corrupt the table.

FIBRE IS AOAC ONLY, NEVER BACKFILLED FROM NSP. The dataset publishes both AOAC fibre
and the older NSP figure, and NSP runs systematically lower for the same food. About
1,100 rows have NSP but no AOAC value. Filling those from NSP would give the bot one
fibre column with two definitions in it and no way to tell which it was reading, so
those rows ship with no fibre_g at all - CofidTable already omits absent fields.

'N' AND 'Tr' ARE NOT THE SAME THING. 'Tr' is a measured trace and becomes 0. 'N' means
the nutrient was not measured and must stay ABSENT, not zero: a missing sodium figure
rendered as 0 mg would read as label-grade evidence that a food contains no salt.

THE 34 SEED FOODS WIN. The bot has resolution history against those exact names and
figures (the test suite asserts cheddar at 723 mg sodium and porridge oats at 379
kcal), so a seed entry beats a CoFID row of the same name and seed aliases keep the
key they already own. Seeds are emitted LAST because CofidTable's index is built in
list order and later entries overwrite earlier ones.

Usage: python3 scripts/import_cofid.py
"""

import json
import sys
from pathlib import Path

import openpyxl

ROOT = Path(__file__).resolve().parent.parent
XLSX = ROOT / "reference" / "McCance_Widdowsons_Composition_of_Foods_Integrated_Dataset_2021.xlsx"
OUT = ROOT / "config" / "cofid.json"
# The 34 hand-built seed foods, kept SEPARATELY from the output. Reading the seeds back
# out of config/cofid.json would make this script non-idempotent - a second run would
# treat all 2,800-odd imported rows as seeds and the merge report would be meaningless.
SEEDS = ROOT / "reference" / "cofid_seed_foods.json"
SOURCE_URL = "PHE Composition of Foods Integrated Dataset (CoFID)"

PROXIMATES = "1.3 Proximates"
INORGANICS = "1.4 Inorganics"
HEADER_ROWS = 3  # three header rows: label, code, short label

# Column indices in "1.3 Proximates" / "1.4 Inorganics", verified against the header row.
C_CODE, C_NAME = 0, 1
C_PROTEIN, C_FAT, C_CARB, C_KCAL = 9, 10, 11, 12
C_AOAC_FIBRE = 25
C_SODIUM = 7

# Aliases for the staples an athlete actually types, mapped onto the published row that
# best answers the bare word. Cooked forms are preferred over raw where the bare word
# implies a cooked food ("boiled rice", "jacket potato"), because that is what gets
# logged. A key already owned by a seed food is skipped by the merge and reported.
STAPLE_ALIASES = {
    "Butter, salted": ["butter", "salted butter"],
    "Butter, unsalted": ["unsalted butter"],
    "Milk, semi-skimmed, pasteurised, average": ["semi-skimmed milk"],
    "Milk, whole, pasteurised, average": ["whole milk"],
    "Milk, skimmed, pasteurised, average": ["skimmed milk"],
    "Eggs, chicken, whole, boiled": ["boiled egg"],
    "Eggs, chicken, whole, fried in sunflower oil": ["fried egg"],
    "Eggs, chicken, whole, scrambled, without milk": ["scrambled egg"],
    "Oranges, flesh only": ["orange", "oranges"],
    "Bread, white, average": ["white bread", "slice of white bread"],
    "Bread, brown, average": ["brown bread"],
    "Rice, white, long grain, boiled in unsalted water": ["boiled rice"],
    "Pasta, white, dried, boiled in unsalted water": ["pasta", "boiled pasta"],
    "Potatoes, old, baked, flesh and skin": ["jacket potato", "baked potato"],
    "Jam, stone fruit": ["jam"],
    "Houmous": ["hummus"],
    "Yogurt, whole milk, plain": ["yoghurt", "yogurt"],
    # Deliberately absent: rye bread (the dataset has only Crispbread, rye and Flour,
    # rye - neither is a loaf); porridge oats, oats, white rice, wholemeal bread,
    # chicken breast, salmon, tuna, cheddar, egg, eggs, banana, apple, honey, olive
    # oil, potato, greek yogurt - all already owned by a seed food.
}


def num(value):
    """A cell as a float, or None where the dataset says the nutrient is unknown.

    'Tr' is a measured trace and becomes 0.0. 'N' (not measured), 'n/a' and blanks
    become None so the field is omitted entirely. Leading '<' means below the limit of
    detection, which is a trace by any useful reading.
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip()
    if not s or s in ("N", "n/a", "-"):
        return None
    if s.startswith("Tr"):
        return 0.0
    s = s.lstrip("<").replace(",", "").strip()
    try:
        return float(s)
    except ValueError:
        return None


def read_sheet(wb, title):
    return [r for r in wb[title].iter_rows(min_row=HEADER_ROWS + 1, values_only=True)
            if r[C_CODE] and r[C_NAME]]


def build_cofid_foods(report):
    wb = openpyxl.load_workbook(XLSX, read_only=True, data_only=True)
    sodium_by_code = {r[C_CODE]: num(r[C_SODIUM]) for r in read_sheet(wb, INORGANICS)}

    foods, seen, skipped_no_kcal, dup_names = [], {}, 0, []
    for row in read_sheet(wb, PROXIMATES):
        name = str(row[C_NAME]).strip()
        kcal = num(row[C_KCAL])
        if kcal is None:
            skipped_no_kcal += 1
            continue
        key = name.lower()
        if key in seen:
            # Two published rows share a name once case is folded. Keeping the first is
            # the only honest option: the index is keyed on the lowercased name, so the
            # second would silently overwrite it.
            dup_names.append(name)
            continue
        food = {"name": name, "kcal": round(kcal)}
        for field, col in (("protein_g", C_PROTEIN), ("carb_g", C_CARB),
                           ("fat_g", C_FAT), ("fibre_g", C_AOAC_FIBRE)):
            v = num(row[col])
            if v is not None:
                food[field] = round(v, 2)
        sodium = sodium_by_code.get(row[C_CODE])
        if sodium is not None:
            food["dietary_sodium_mg"] = round(sodium)
        food["ingredients"] = name
        food["source_url"] = SOURCE_URL
        seen[key] = food
        foods.append(food)

    report["cofid_rows_with_kcal"] = len(foods)
    report["skipped_no_kcal"] = skipped_no_kcal
    report["duplicate_names_dropped"] = dup_names
    report["no_aoac_fibre"] = sum(1 for f in foods if "fibre_g" not in f)
    report["no_sodium"] = sum(1 for f in foods if "dietary_sodium_mg" not in f)
    return foods, seen


def attach_staple_aliases(by_name, seed_keys, report):
    """Attach the curated aliases, skipping any key another row already answers."""
    attached, skipped = {}, {}
    claimed = set()
    for target, aliases in STAPLE_ALIASES.items():
        food = by_name.get(target.lower())
        if food is None:
            skipped[target] = "no such row in the dataset"
            continue
        for alias in aliases:
            a = alias.strip().lower()
            if a in seed_keys:
                skipped[alias] = f"owned by a seed food (wanted {target})"
            elif a in by_name and by_name[a] is not food:
                skipped[alias] = f"collides with the published name '{by_name[a]['name']}'"
            elif a in claimed:
                skipped[alias] = "already attached to another row"
            else:
                claimed.add(a)
                food.setdefault("aliases", []).append(a)
                attached.setdefault(target, []).append(a)
    report["aliases_attached"] = attached
    report["aliases_skipped"] = skipped


def merge_seeds(cofid_foods, by_name, seeds, report):
    """Seed foods win: a CoFID row of the same name is dropped, and seeds go last so
    their aliases hold the keys they already own."""
    seed_names = {s["name"].strip().lower() for s in seeds}
    displaced = [f["name"] for f in cofid_foods if f["name"].lower() in seed_names]
    kept = [f for f in cofid_foods if f["name"].lower() not in seed_names]

    seed_keys = set(seed_names)
    for s in seeds:
        seed_keys.update(a.strip().lower() for a in (s.get("aliases") or []))
    shadowed = sorted(k for k in seed_keys
                      if k not in seed_names and k in by_name)

    report["seeds"] = len(seeds)
    report["seed_displaced_cofid_rows"] = displaced
    report["seed_aliases_shadowing_a_cofid_name"] = shadowed
    return kept, seed_keys


def main():
    report = {}
    seeds = json.loads(SEEDS.read_text())["foods"]

    cofid_foods, by_name = build_cofid_foods(report)
    kept, seed_keys = merge_seeds(cofid_foods, by_name, seeds, report)
    # Aliases are attached after the merge so a displaced row cannot receive one.
    by_kept = {f["name"].lower(): f for f in kept}
    attach_staple_aliases(by_kept, seed_keys, report)

    foods = kept + seeds
    OUT.write_text(json.dumps({"foods": foods}, separators=(",", ":"),
                              ensure_ascii=False))

    size = OUT.stat().st_size
    print("CoFID import - McCance and Widdowson's CoFID, 2021 edition (19 Mar 2021)")
    print(f"  source xlsx                : {XLSX.name}")
    print(f"  rows with a usable kcal    : {report['cofid_rows_with_kcal']}")
    print(f"  rows skipped, no kcal      : {report['skipped_no_kcal']}")
    print(f"  duplicate names dropped    : {len(report['duplicate_names_dropped'])} "
          f"{report['duplicate_names_dropped']}")
    print(f"  no AOAC fibre (left absent): {report['no_aoac_fibre']}")
    print(f"  no sodium (left absent)    : {report['no_sodium']}")
    print(f"  seed foods kept, winning   : {report['seeds']}")
    print(f"  CoFID rows displaced by a seed of the same name: "
          f"{len(report['seed_displaced_cofid_rows'])}")
    for n in report["seed_displaced_cofid_rows"]:
        print(f"      - {n}")
    print("  seed aliases that shadow a published CoFID name (row still reachable by "
          f"token match): {report['seed_aliases_shadowing_a_cofid_name'] or 'none'}")
    print("  staple aliases attached:")
    for target, aliases in sorted(report["aliases_attached"].items()):
        print(f"      {target}: {', '.join(aliases)}")
    print("  staple aliases skipped:")
    for alias, why in sorted(report["aliases_skipped"].items()):
        print(f"      {alias}: {why}")
    print(f"  total foods written        : {len(foods)}")
    print(f"  file size                  : {size / 1024:.0f} KB -> {OUT}")

    # Sanity checks. Asserted through CofidTable, not through a local dict, because the
    # thing that must hold is what the CONSUMER resolves - seeds winning depends on
    # emission order inside that class, so checking a reconstruction proves nothing.
    sys.path.insert(0, str(ROOT / "lib"))
    from nutrition_resolve import CofidTable  # noqa: E402

    assert len(foods) >= 2000, f"only {len(foods)} foods"
    assert size < 4 * 1024 * 1024, f"{size} bytes is over 4MB"

    table = CofidTable(path=OUT)
    butter = table.lookup("butter")
    assert butter is not None, "'butter' does not resolve"
    head = butter["resolved_name"].split(",")[0].lower()
    assert set(head.split()) <= {"salted", "butter"}, \
        f"'butter' resolved to {butter['resolved_name']!r}"
    assert "salted" in butter["resolved_name"].lower(), \
        f"'butter' resolved to {butter['resolved_name']!r}, not a salted butter"
    assert 700 <= butter["kcal"] <= 760, f"butter is {butter['kcal']} kcal"
    for q in ("salted butter", "semi skimmed milk", "banana", "porridge oats",
              "cheddar", "hummus", "white bread"):
        assert table.lookup(q) is not None, f"{q!r} does not resolve"
    assert table.lookup("cheddar", 100)["dietary_sodium_mg"] == 723, \
        "the seed cheddar figure the test suite asserts has been displaced"
    print("  sanity checks              : all passed")


if __name__ == "__main__":
    main()
