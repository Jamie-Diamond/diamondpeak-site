#!/usr/bin/env python3
"""retag-species.py - re-match stored entries against a GROWN species table.

    python3 scripts/retag-species.py --month 2026-08              # report only
    python3 scripts/retag-species.py --month 2026-08 --write
    python3 scripts/retag-species.py --day 2026-08-10 --write

WHY THIS EXISTS, AND WHY IT IS OPT-IN
Stored species win over re-matching on purpose: the count for a day already logged must
not change because the table changed later, or history rewrites itself every time the
table grows. That rule is right, and it has one consequence - adding a species does
nothing for the days already logged.

On 10 Aug an M&S smoothie listed blackcurrant purée and blackcurrant juice, and the table
had no Ribes nigrum, so a real plant was dropped from the count in silence. Six berries
were missing. Adding them fixed nothing retrospectively until this ran.

So this is the deliberate, auditable exception: it re-matches the STORED ingredient text,
touches nothing but the species list, reports every change, and writes only when asked.
Macros are never recomputed - they came from a label and this script has no business
near them.
"""

import argparse
import calendar
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE / "lib"))

import plants as PL                            # noqa: E402
from nutrition_store import NutritionStore     # noqa: E402


def species_key(species) -> set:
    """Comparable form, tolerant of the legacy bare-id shape."""
    return {(s["id"], s.get("score")) if isinstance(s, dict) else (s, None)
            for s in (species or [])}


def retag_day(rec: dict, table: PL.SpeciesTable) -> list:
    """Returns a list of (entry_name, added, removed) for whatever changed."""
    changes = []
    for e in rec.get("entries") or []:
        # The ingredient list is what the species matcher is FOR. Falling back to the
        # product name is what tagged "M&S nut collection" with nothing at all.
        text = e.get("ingredients") or e.get("resolved_name") or ""
        if not text or e.get("_supplement"):
            continue
        got = table.match_food(text, ingredients=e.get("ingredients") or None)
        before, after = species_key(e.get("species")), species_key(got["species"])
        if before == after:
            continue
        names = {sid: table.species.get(sid, {}).get("canonical", sid)
                 for sid, _ in before | after}
        added = sorted(f"{names[sid]} ({score})" for sid, score in after - before)
        removed = sorted(f"{names[sid]} ({score})" for sid, score in before - after)
        e["species"] = got["species"]
        e["species_from"] = "ingredients" if e.get("ingredients") else "name"
        if got.get("unmatched"):
            e["species_unmatched"] = got["unmatched"]
        if got.get("species_suppressed"):
            e["species_suppressed"] = got["species_suppressed"]
            e["processing_markers"] = got.get("processing_markers")
        else:
            e.pop("species_suppressed", None)
            e.pop("processing_markers", None)
        changes.append((e.get("resolved_name") or "?", added, removed))
    return changes


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--athlete", default="jamie")
    ap.add_argument("--month", help="YYYY-MM")
    ap.add_argument("--day", help="YYYY-MM-DD")
    ap.add_argument("--write", action="store_true",
                    help="without this, nothing is written")
    a = ap.parse_args(argv)
    if not (a.month or a.day):
        ap.error("give --month or --day")

    store = NutritionStore(BASE / "athletes" / a.athlete)
    table = PL.SpeciesTable()
    print(f"table has {len(table.species)} species")
    if a.day:
        days = [a.day]
    else:
        y, m = (int(x) for x in a.month.split("-"))
        last = calendar.monthrange(y, m)[1]
        days = [f"{a.month}-{d:02d}" for d in range(1, last + 1)]

    total = 0
    for day in days:
        # _mutate_day writes the record back unconditionally, blank ones included, so
        # walking a whole month with --write would stamp 31 empty days into the file -
        # including days that have not happened yet. Look before touching.
        if not (store.get_day(day).get("entries") or []):
            continue
        if a.write:
            # Through _mutate_day, so the retag takes the month lock like every other
            # write. Re-reading and saving by hand would race the live bot.
            changes = store._mutate_day(day, lambda rec: retag_day(rec, table))
        else:
            changes = retag_day(store.get_day(day), table)
        if not changes:
            continue
        print(f"\n{day}")
        for name, added, removed in changes:
            print(f"  {name[:52]}")
            for x in added:
                print(f"     + {x}")
            for x in removed:
                print(f"     - {x}")
        total += len(changes)

    if not total:
        print("\nnothing to change")
        return 0
    print(f"\n{total} entr{'y' if total == 1 else 'ies'} "
          + ("REWRITTEN" if a.write else "would change; pass --write to apply"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
