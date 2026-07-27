#!/usr/bin/env python3
"""
One-off, idempotent loader for the race registry (lib/races.py) — the initial backfill of
every race the repo has evidence for, into config/athletes.json.

Every entry below cites the file that states it. NOTHING here is inferred: where the
evidence does not give a priority, `priority` is left None and the race is listed in the
gap report at the end for the athlete to confirm. The one class of inference that IS made
is the one the registry is explicitly designed around — the athlete's existing `race_date`
is taken to be their A-race, because that is what every consumer of that field has always
meant by it (the taper maths, the countdown, the blueprint all treat it as the race being
trained for).

Re-runnable: add_race updates a race with the same date in place rather than duplicating,
so filling in a priority later is a matter of editing the table and running this again.

    python3 scripts/load-race-registry.py            # show what would change
    python3 scripts/load-race-registry.py --write     # apply
"""
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent          # ClaudeCoach/
sys.path.insert(0, str(BASE / "lib"))
import races as races_lib                              # noqa: E402

# (slug, name, date, priority, distance, status, source)
# priority None == NOT STATED BY THE EVIDENCE. Do not fill these in by reasoning; ask.
REGISTRY = [
    # -- Jamie ---------------------------------------------------------------------
    # A: config/athletes.json race_date + athletes/jamie/profile.json race_date. Also
    # named "the A-race (Cervia)" outright in athletes/jamie/persistent-rules.md:45.
    ("jamie", "IM Italy Emilia-Romagna", "2026-09-19", "A", "Full Ironman", "upcoming",
     "config/athletes.json race_date; profile.json; persistent-rules.md:45"),
    # B: athletes/jamie/persistent-rules.md:32 ("For B-races ... e.g. Dorney Olympic tri"),
    # corroborated by current-state.md:19 and :137 and five watchdog entries, all "B-race".
    # NOTE a stale conflicting source: training-plan-2026-05-18_to_2026-05-31.md:89 calls it
    # a C-race, but for a DIFFERENT date (6 Jun, "TBC entry") that did not happen. Flagged.
    ("jamie", "Dorney Olympic tri", "2026-07-26", "B", "Olympic triathlon", "completed",
     "persistent-rules.md:32,45; current-state.md:19,137"),
    # Completed, from the structured prev_race block. Priority NOT stated — it was almost
    # certainly that season's A-race, but "almost certainly" is a guess, so it stays None.
    ("jamie", "IM Italy Emilia-Romagna", "2025-09-20", None, "Full Ironman", "completed",
     "profile.json prev_race"),
    ("jamie", "Barcelona IM", "2023-10-07", None, "Full Ironman", "completed",
     "profile.json prev2_race_date/prev2_race_name"),

    # -- Kathryn -------------------------------------------------------------------
    ("kathryn", "70.3 Emilia Romagna", "2026-09-20", "A", "70.3", "upcoming",
     "config/athletes.json race_date; profile.json; current-state.md:33"),
    ("kathryn", "70.3 Marathonas Greece", "2024-10-20", None, "70.3", "completed",
     "profile.json prev_race (the 'Greece 2024' reference in current-state.md)"),
    ("kathryn", "Venice 70.3", "2023-09-24", None, "70.3", "completed",
     "profile.json prev2_race_date/prev2_race_name"),

    # -- Calum ---------------------------------------------------------------------
    # Route confirmed by Calum himself 21 Jul per current-state.md:11,32.
    ("calum", "Tour de Stations / Marmottes", "2026-08-29", "A",
     "Sportive — 134 km / 4,700 m", "upcoming",
     "config/athletes.json race_date; profile.json; current-state.md:11,32"),
]


def main():
    write = "--write" in sys.argv
    gaps = []
    for slug, name, d, pri, dist, status, source in REGISTRY:
        if pri is None:
            gaps.append(f"{slug}: {name} ({d}) — priority not stated by any source")
        line = f"{slug:8} {d}  {pri or '?':1}  {name}  [{status}]"
        if write:
            races_lib.add_race(slug, name, d, priority=pri, distance=dist,
                               status=status, source=source)
            print("wrote  " + line)
        else:
            print("would  " + line)

    if write:
        cfg = races_lib._load_config()
        print("\n-- legacy race_date after sync (must be unchanged) --")
        for slug in cfg:
            rs = races_lib.load_races(slug, cfg)
            a = races_lib.a_race(rs)
            print(f"  {slug:8} race_date={cfg[slug].get('race_date')} "
                  f"race_name={cfg[slug].get('race_name')!r} "
                  f"A-race={(a or {}).get('name')!r} races={len(rs)}")

    print("\n-- GAPS for the athlete to confirm --")
    for g in gaps:
        print("  ? " + g)
    print("  ? jamie: Dorney priority recorded as B (persistent-rules.md:32, current-state.md); "
          "training-plan-2026-05-18…md:89 says C-race for a different, abandoned date (6 Jun). "
          "Confirm B.")
    print("  ? no athlete has a single RACE-category event in their intervals.icu calendar "
          "(checked 2025-01-01..2027-12-31), so nothing could be cross-referenced from there.")


if __name__ == "__main__":
    main()
