#!/usr/bin/env python3
"""publish-nutrition-data.py - build public/nutrition-<slug>.json for the Peak page.

Runs on the VM, chained off refresh-site-data.py. Reads the athlete's nutrition log and
writes the subset the page renders.

OPT-IN PER ATHLETE, DEFAULT OFF
Nothing is published unless the athlete's profile.json carries
`nutrition_tracker: true`. Absent or false means no file is written and the tab does not
appear, which is why Kathryn and Calum are off by default: the flag is opt-in, not
opt-out, so a new athlete can never be published by omission.

THIS DIRECTORY IS PUBLIC
`public/` is served from a PUBLIC repo, and every write here is a permanent commit in
public git history. Jamie decided on 10 Aug 2026 that his own weight, body fat and food
log may be public, and was told the exposure includes the history, not just the URL.
That decision is HIS ALONE and does not extend to anyone else - hence the per-athlete
flag above rather than a global switch.

WHAT IS DELIBERATELY LEFT OUT
- raw_text of each entry, and any free-text note: the resolved name is what the page
  needs and the raw text is where stray personal detail ends up
- source_url: no value on the page, and it leaks which retailer account was browsed
- BODY FAT as a trend. It is stored, but BIA fat is a residual of weight divided by an
  assumed hydration constant and correlates with scale weight at r = 0.999 in this
  athlete's data. The spec forbids charting it and this script gives the page no
  series to chart even if someone later tries.

TARGETS ARE READ FROM THE DAY, NOT RECOMPUTED
Each day carries the zones that were in force when it was logged. Recomputing here
would quietly rewrite history, because intervals.icu revises activity calories after
the fact and today's data would produce different targets for a day three weeks ago.
"""

import json
import sys
from datetime import date, timedelta
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE / "lib"))

sys.path.insert(0, str(BASE / "ironman-analysis"))

import nutrition_engine as NE       # noqa: E402
import nutrition_reconcile as RC    # noqa: E402
import plants as PL                 # noqa: E402
from nutrition_store import NutritionStore  # noqa: E402
from primitives.nutrition import (fuel_target, recent_avg_g_hr,  # noqa: E402
                                  recent_run_avg_g_hr, run_fuel_target)

RUN_SPORTS_FUEL = ("Run", "VirtualRun", "TrailRun")

PUBLIC = BASE / "public"
WINDOW_DAYS = 28          # enough for the rolling views and a block history
FLAG = "nutrition_tracker"


def athlete_enabled(slug: str) -> bool:
    p = BASE / "athletes" / slug / "profile.json"
    if not p.exists():
        return False
    try:
        return bool(json.loads(p.read_text()).get(FLAG))
    except json.JSONDecodeError:
        return False


def _prescribed_g_hr(sport: str, session_log, cfg: dict) -> float:
    """The prescribed in-session rate, READ from the existing fuelling primitives.

    Runs use run_fuel_target, ceilinged near 60 g/hr; rides use fuel_target, ramping
    toward the athlete's race figure. Restating either number here would let the bot and
    the coach disagree about the same session, which is the divergence this project has
    refused everywhere else."""
    if sport in RUN_SPORTS_FUEL:
        avg = recent_run_avg_g_hr(session_log)
        avg = avg[0] if isinstance(avg, tuple) else avg
        return float(run_fuel_target(avg))
    avg = recent_avg_g_hr(session_log)
    avg = avg[0] if isinstance(avg, tuple) else avg
    return float(fuel_target(avg, cfg.get("nutrition_target_g_hr") or 90))


def build(slug: str, today: date) -> dict:
    athlete_dir = BASE / "athletes" / slug
    store = NutritionStore(athlete_dir)
    profile = json.loads((athlete_dir / "profile.json").read_text())
    table = PL.SpeciesTable()
    cfg_all = json.loads((BASE / "config" / "athletes.json").read_text())
    cfg = cfg_all.get(slug, {})
    slog_path = athlete_dir / "session-log.json"
    try:
        slog = json.loads(slog_path.read_text()) if slog_path.exists() else []
        if isinstance(slog, dict):
            slog = slog.get("sessions") or slog.get("entries") or []
    except json.JSONDecodeError:
        slog = []

    start = today - timedelta(days=WINDOW_DAYS - 1)
    days_raw = store.get_range(start, today)

    days = []
    for rec in days_raw:
        d = rec.get("date")
        if not d:
            continue
        day = date.fromisoformat(d)
        totals = RC.merged_totals(store, athlete_dir, day)
        z = rec.get("targets") or None
        if day == today and z is None:
            z = None    # nothing logged yet today; the bot writes the snapshot on first use
        entries = rec.get("entries") or []
        # Computed HERE, not in the page. A rendering layer doing its own arithmetic
        # produces plausible wrong numbers rather than visible errors, and the page has
        # no way to signal that it guessed.
        requirement = NE.meal_requirement(totals, z) if z else None
        # In-session fuel assessed on its OWN terms, as a RATE. A day carb zone is an
        # energy budget and can look satisfied while the session inside it was badly
        # under-fuelled, which a rate cannot recover from at dinner.
        rec_fuel = RC.reconcile(store, athlete_dir, day)
        sessions = rec_fuel.get("sessions") or []
        longest = max(sessions, key=lambda x: float(x.get("duration_min") or 0),
                      default=None)
        in_session = None
        if longest and float(longest.get("duration_min") or 0) >= 90:
            in_session = NE.in_session_requirement(
                session_minutes=float(longest["duration_min"]),
                carbs_in_session_g=(rec_fuel["fuel"].get("carb_g") or 0),
                target_g_hr=_prescribed_g_hr(longest.get("sport") or "", slog, cfg),
                alert_g_hr=cfg.get("nutrition_alert_threshold_g_hr"),
                sport=longest.get("sport") or "")
        carb_split = NE.split_carbs(totals)
        # Meals are inferred from the clock, because nothing asks the athlete to
        # categorise. Stated as inferred so an odd bucket is not read as a data error.
        meals = {"breakfast": [], "lunch": [], "snacks": [], "dinner": []}
        for e in entries:
            hhmm = (e.get("logged_at") or "")[11:16]
            if e.get("in_session"):
                bucket = "snacks"
            elif hhmm and hhmm < "11:00":
                bucket = "breakfast"
            elif hhmm and hhmm < "15:00":
                bucket = "lunch"
            elif hhmm and hhmm >= "17:00":
                bucket = "dinner"
            else:
                bucket = "snacks"
            meals[bucket].append({
                "name": e.get("resolved_name"), "kcal": e.get("kcal"),
                "protein_g": e.get("protein_g"), "carb_g": e.get("carb_g"),
                "fat_g": e.get("fat_g"), "confidence": e.get("confidence"),
                "rung": e.get("source_rung"), "in_session": bool(e.get("in_session")),
                "logged_at": hhmm})
        days.append({
            "date": d,
            "day_type": rec.get("day_type"),
            "closed": bool(rec.get("closed_at")),
            "totals": {k: totals.get(k) for k in
                       ("kcal", "protein_g", "carb_g", "fat_g", "fibre_g",
                        "dietary_sodium_mg", "non_counting_protein_g",
                        "in_session_kcal", "in_session_carb_g", "lowest_confidence")},
            "fuel_owner": totals.get("fuel_owner"),
            "fuel_from_coach": totals.get("in_session_from_coach"),
            "energy_is_derived": totals.get("energy_is_derived", False),
            # The zones AS THEY WERE on the day, bias included, so the page renders a
            # ceiling as a ceiling in history too.
            "zones": ({k: z[k] for k in ("kcal", "protein_g", "carb_g", "fat_g",
                                         "fibre_g") if k in z}
                      | {"kcal_target": z.get("kcal_target"),
                         "kcal_maintenance": z.get("kcal_maintenance"),
                         "deficit_applied_kcal": z.get("deficit_applied_kcal"),
                         "modifiers": z.get("modifiers") or [],
                         "confidence": z.get("confidence")}) if z else None,
            # Names and confidence only: no raw_text, no notes, no source urls.
            "items": [{"name": e.get("resolved_name"), "kcal": e.get("kcal"),
                       "confidence": e.get("confidence"),
                       "rung": e.get("source_rung"),
                       "in_session": bool(e.get("in_session"))} for e in entries],
            "supplements": [{"nutrient": s.get("nutrient"), "dose": s.get("dose"),
                             "unit": s.get("unit")}
                            for s in (rec.get("supplements") or [])],
            "flags": [{"type": f.get("type"), "severity": f.get("severity")}
                      for f in (rec.get("flags") or [])],
            "requirement": requirement,
            "in_session": in_session,
            "carb_split": carb_split,
            "meals": meals,
            "meals_inferred_from_clock": True,
            # Pace: where each macro would sit if it tracked calories exactly. This
            # answers "what do I reach for", NEVER "am I in trouble" - only the
            # projection answers that, and the two must not be conflated. A macro can
            # be well ahead of pace and still land perfectly.
            "pace_pct": (round((totals.get("kcal") or 0) / z["kcal_target"] * 100, 1)
                         if z and z.get("kcal_target") else None),
            "provenance": {
                "label": sum(1 for e in entries if e.get("confidence") == "label"),
                "database": sum(1 for e in entries if e.get("confidence") == "database"),
                "estimate": sum(1 for e in entries if e.get("confidence") == "estimate"),
                "estimate_error_band": "+/-10-15%"},
        })

    # ICU is the right weight source (the scale syncs there), but it holds ONE
    # UNTIMESTAMPED reading per day and mixes morning weights with sweat-rate weigh-ins.
    # Classify before use, or the page's 7-day mean carries a post-session reading and
    # shows progress that did not happen. On this athlete's fortnight that is the
    # difference between 83.4 and 82.6 kg.
    measurements = store.measurements_range(start, today, type="weight")
    own_morning = {(m.get("date") or "")[:10]: m for m in measurements
                   if m.get("tag") == "morning"}
    icu_rejected = []
    try:
        cfg = json.loads((BASE / "config" / "athletes.json").read_text())[slug]
        from icu_api import IcuClient
        rows = IcuClient(cfg["icu_athlete_id"], cfg["icu_api_key"]).get_wellness(
            days=WINDOW_DAYS + 7) or []
        for r in NE.classify_icu_weights(rows, existing_by_date=own_morning):
            if r["tag"] == "morning":
                measurements.append({"type": "weight", "date": r["date"],
                                     "value": r["value"],
                                     "logged_at": r["date"] + "T06:00",
                                     "tag": "morning", "source": "intervals.icu"})
            elif r["tag"] == "session_sweat":
                icu_rejected.append({"date": r["date"], "kg": r["value"],
                                     "reason": r["reason"]})
    except Exception as exc:
        print(f"{slug}: icu weights unavailable ({exc}); using logged readings only")
    weight_series = [{"date": m["date"], "kg": m["value"]}
                     for m in measurements if m.get("tag") == "morning"]
    weight_series.sort(key=lambda r: r["date"])
    div = PL.diversity(days_raw, table, on=today)

    race = profile.get("race_date") or None
    days_to_race = ((date.fromisoformat(race) - today).days if race else None)
    weight_now = NE.rolling_weight_kg(measurements, on=today)
    projection = None
    if weight_now and profile.get("race_weight_kg") and days_to_race is not None:
        rmr = NE.mifflin_st_jeor(weight_now, float(profile.get("height_m") or 1.86),
                                 profile.get("dob") or "1995-05-06", "M", on=today)
        projection = NE.race_weight_projection(
            weight_now, float(profile["race_weight_kg"]), days_to_race, rmr)

    return {
        "generated": today.isoformat(),
        "athlete": slug,
        "nutrition_enabled": True,
        "window_days": WINDOW_DAYS,
        "days": days,
        "weight": {
            # Morning readings only. Sweat weigh-ins are excluded upstream, not hidden
            # by the chart: on long-ride days they sit 2-3 kg low and would read as
            # progress that did not happen.
            "morning_series": weight_series,
            "rolling_7d_mean_kg": weight_now,
            "race_target_kg": profile.get("race_weight_kg"),
            "projection": projection,
            # Deliberately no body-fat series. See the module docstring.
            "body_fat_charted": False,
            # Shown so the filtering is visible rather than silent: a rejected reading
            # is a real number the athlete saw on his scale, and hiding the rejection
            # would look like the app had lost it.
            "sweat_readings_excluded": icu_rejected,
        },
        "plants": {"unique_7d": div["unique_7d"], "weighted_7d": div["weighted_7d"],
                   "target": div["target"], "basis": div["target_basis"],
                   "new_today": div["new_species_today"],
                   "species": div["species"],
                   "herb_spice_count": div["herb_spice_count"],
                   # Plants a pack claimed that could not be NAMED, so the headline
                   # count reads honestly incomplete rather than quietly low.
                   "claimed_unresolved": div.get("claimed_unresolved", 0)},
        "block": {"days_to_race": days_to_race, "race_name": profile.get("race_name"),
                  "protein_floor_basis": "g/kg bodyweight, flexes with load"},
        "sodium": {"has_sweat_test": False,
                   "assumed_band_mg_l": [NE.SWEAT_NA_ASSUMED_LOW,
                                         NE.SWEAT_NA_ASSUMED_HIGH]},
    }


def main(argv=None):
    argv = argv or sys.argv[1:]
    today = date.fromisoformat(argv[1]) if len(argv) > 1 else date.today()
    slugs = [argv[0]] if argv else [p.name for p in (BASE / "athletes").iterdir()
                                    if p.is_dir()]
    written = []
    for slug in slugs:
        if not athlete_enabled(slug):
            print(f"{slug}: {FLAG} not set, skipping (nothing published)")
            continue
        data = build(slug, today)
        out = PUBLIC / f"nutrition-{slug}.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(data, separators=(",", ":")) + "\n")
        written.append(str(out.relative_to(BASE)))
        print(f"{slug}: wrote {out.name} "
              f"({len(data['days'])} days, {data['plants']['unique_7d']} plants)")
    if not written:
        print("nothing published")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
