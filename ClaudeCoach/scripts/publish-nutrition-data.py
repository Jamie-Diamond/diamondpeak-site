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
from primitives.nutrition import (fuel_target, last_ride_g_hr,  # noqa: E402
                                  last_run_g_hr, recent_avg_g_hr,
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
        return float(run_fuel_target(avg, last_g_hr=last_run_g_hr(session_log)))
    avg = recent_avg_g_hr(session_log)
    avg = avg[0] if isinstance(avg, tuple) else avg
    return float(fuel_target(avg, cfg.get("nutrition_target_g_hr") or 90,
                             last_g_hr=last_ride_g_hr(session_log)))


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
        requirement = None          # computed below, once the session is known
        # In-session fuel assessed on its OWN terms, as a RATE. A day carb zone is an
        # energy budget and can look satisfied while the session inside it was badly
        # under-fuelled, which a rate cannot recover from at dinner.
        rec_fuel = RC.reconcile(store, athlete_dir, day)
        sessions = rec_fuel.get("sessions") or []
        longest = max(sessions, key=lambda x: float(x.get("duration_min") or 0),
                      default=None)
        in_session = None
        if longest and float(longest.get("duration_min") or 0) >= 90:
            _rate = _prescribed_g_hr(longest.get("sport") or "", slog, cfg)
            in_session = NE.in_session_requirement(
                session_minutes=float(longest["duration_min"]),
                carbs_in_session_g=(rec_fuel["fuel"].get("carb_g") or 0),
                target_g_hr=_rate,
                alert_g_hr=cfg.get("nutrition_alert_threshold_g_hr"),
                sport=longest.get("sport") or "")
        carb_split = NE.split_carbs(totals)
        # Fuel prescribed for a session and not yet taken is RESERVED out of the food
        # budget. Prescription minus what is already logged in-session, so it is full
        # before the session and zero afterwards without needing to know which.
        reserve = {}
        planned_fuel = float((z or {}).get("planned_in_session_carb_g") or 0)
        taken_fuel = float((totals or {}).get("in_session_carb_g") or 0)
        if planned_fuel - taken_fuel > 5:
            reserve["carb_g"] = round(planned_fuel - taken_fuel, 1)
        elif in_session and (in_session.get("target_g_hr") or 0) > 0:
            planned = ((in_session.get("target_g_hr") or 0)
                       * (in_session.get("session_minutes") or 0) / 60.0)
            taken = float((rec_fuel.get("fuel") or {}).get("carb_g") or 0)
            if planned - taken > 5:
                reserve["carb_g"] = round(planned - taken, 1)
        requirement = NE.meal_requirement(totals, z, reserved=reserve) if z else None

        # A STATED meal wins; the clock is only the fallback. He knows when he ate, and
        # writing the log up an hour later is normal - so "that was breakfast" has to be
        # able to override an 11:30 timestamp rather than argue with it.
        meals = {"breakfast": [], "lunch": [], "snacks": [], "dinner": []}
        inferred_any = False
        for e in entries:
            hhmm = (e.get("logged_at") or "")[11:16]
            stated = (e.get("meal") or "").strip().lower()
            if stated in meals:
                bucket = stated
            elif e.get("in_session"):
                bucket = "snacks"
                inferred_any = True
            elif hhmm and hhmm < "11:00":
                bucket = "breakfast"
                inferred_any = True
            elif hhmm and hhmm < "15:00":
                bucket = "lunch"
                inferred_any = True
            elif hhmm and hhmm >= "17:00":
                bucket = "dinner"
                inferred_any = True
            else:
                bucket = "snacks"
                inferred_any = True
            meals[bucket].append({
                "name": e.get("resolved_name"), "kcal": e.get("kcal"),
                "protein_g": e.get("protein_g"), "carb_g": e.get("carb_g"),
                "fat_g": e.get("fat_g"), "confidence": e.get("confidence"),
                "rung": e.get("source_rung"), "in_session": bool(e.get("in_session")),
                "logged_at": hhmm, "meal_stated": bool(stated)})
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
                         # WHY the deficit is what it is. Without this a suppressed
                         # deficit is SILENT: the page shows a target equal to
                         # maintenance and nothing says the engine held it there
                         # deliberately ("deficit suppressed: resting HR elevated").
                         # Absent on days logged before the key existed.
                         "warnings": z.get("warnings") or [],
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
            # The carb target in TWO parts, because one number cannot answer both
            # questions. 900 g on a long-run day is not 900 g of food: some of it is taken
            # on the move at a prescribed RATE, and a rate cannot be made up at dinner.
            "carb_plan": ({"total_low": (z["carb_g"]["low"] if z else None),
                           "total_high": (z["carb_g"]["high"] if z else None),
                           "in_session_planned_g": planned_fuel or None,
                           "in_session_taken_g": taken_fuel or None,
                           "out_of_session_low": (round(z["carb_g"]["low"] - planned_fuel)
                                                  if z else None),
                           "out_of_session_high": (round(z["carb_g"]["high"] - planned_fuel)
                                                   if z else None)}
                          if z and planned_fuel else None),
            "carb_split": carb_split,
            "meals": meals,
            # Only claim inference when something actually WAS inferred, so a day he has
            # filed himself does not carry a caveat telling him not to trust it.
            "meals_inferred_from_clock": inferred_any,
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
    # WEEKLY SUMMARY (Jamie, 10 Aug 2026: "missing one day is fine, missing every day is a
    # problem"). A single day says nothing about a pattern, so the week reports per-day
    # compliance and the counts that go with it. Everything here is derived from the days
    # already built above, so the page never recomputes and cannot disagree.
    def _week(win: int):
        rows, wk = [], days[-win:]
        for d in wk:
            z, t = d.get("zones") or {}, d.get("totals") or {}
            pz, fz = z.get("protein_g") or {}, z.get("fibre_g") or {}
            logged = bool(d.get("items"))
            pro, fib = t.get("protein_g"), t.get("fibre_g")
            fib_ok = None
            if logged and fz:
                fib_ok = ((fib or 0) <= fz["high"] if fz.get("bias") == "ceiling"
                          else (fib or 0) >= fz["low"])
            ins = d.get("in_session") or None
            rows.append({
                "date": d["date"],
                "dow": date.fromisoformat(d["date"]).strftime("%a"),
                "logged": logged,
                # Carried from the day, never re-derived: the day dict already knows
                # whether it was explicitly closed, and _week() reading it means an
                # athlete who closes today off gets today counted as settled.
                "closed": bool(d.get("closed")),
                "day_type": d.get("day_type"),
                "kcal": t.get("kcal") if logged else None,
                "kcal_target": z.get("kcal_target"),
                # Maintenance, so energy BALANCE can be reported. The target is already
                # deficit-adjusted, so intake against target answers "did I hit the
                # plan", never "am I in a deficit" - the two are different questions and
                # publishing only the first is what made the page label adherence as
                # deficit, sign inverted.
                "kcal_maintenance": z.get("kcal_maintenance"),
                "protein_g": pro if logged else None,
                "protein_floor": pz.get("low"),
                "protein_met": (logged and pz and (pro or 0) >= pz["low"]) or None,
                "fibre_g": fib if logged else None,
                "fibre_limit": fz.get("high"),
                "fibre_bias": fz.get("bias"),
                "fibre_ok": fib_ok,
                "in_run_g_hr": (ins or {}).get("g_per_hr"),
                "in_run_target": (ins or {}).get("target_g_hr"),
                "in_run_verdict": (ins or {}).get("verdict"),
                "flags": [f["type"] for f in (d.get("flags") or [])],
            })
        done = [r for r in rows if r["logged"]]
        # A DAY IN PROGRESS CANNOT CONTRIBUTE TO A DEFICIT. Today is part-logged by
        # definition - 743 kcal at mid-morning against a 5,356 target - and including it
        # dragged the rolling average to -1,967 kcal/day, implying -1.8 kg a week off
        # nothing but the clock. The deficit is computed over SETTLED days only: closed, or
        # simply not today.
        # `today` from the caller, NOT date.today(): a backfill run for an earlier date
        # would otherwise treat every day in its window as past and settle a day that was
        # still in progress at the time the figures claim to describe.
        settled = [r for r in done if r["closed"] or r["date"] < today.isoformat()]
        # ONE DENOMINATOR PER FIGURE, and it is the days carrying BOTH halves of the
        # subtraction. A day whose stored snapshot predates kcal_maintenance would, under
        # `or 0`, contribute MINUS ITS WHOLE INTAKE and fabricate a multi-thousand-kcal
        # deficit out of a missing key. Same for a logged day with no zones at all, which
        # has no target either. Each mean therefore names its own basis and its own count,
        # and an empty basis returns None rather than zero: no number is readable, a zero
        # is a lie that looks like a measurement.
        by_maint = [r for r in settled if r["kcal_maintenance"] is not None]
        by_target = [r for r in settled if r["kcal_target"] is not None]
        # POSITIVE MEANS A REAL DEFICIT: maintenance minus what was eaten. Unrounded here
        # and rounded once at each use, so the kg figure is not derived from a rounded
        # kcal figure.
        deficit_vs_maint = (sum(r["kcal_maintenance"] - (r["kcal"] or 0) for r in by_maint)
                            / len(by_maint)) if by_maint else None
        # POSITIVE MEANS HE ATE OVER THE PLAN. Adherence to an already-deficit-adjusted
        # target, which is a different question from the balance above and is named as
        # such - the two were previously the SAME number wearing the deficit's label.
        adherence = (sum((r["kcal"] or 0) - r["kcal_target"] for r in by_target)
                     / len(by_target)) if by_target else None
        runs = [r for r in rows if r["in_run_verdict"]]
        return {
            "days": rows,
            "summary": {
                "days_logged": len(done), "days_in_window": len(rows),
                # NOT a compliance figure. Jamie logs occasionally to spot-check rather
                # than daily, so an unlogged day is a choice, not a miss. It is reported
                # only so a rolling number is read as "across N days sampled" instead of
                # as a week. An earlier cut called these `days_missed` and coloured three
                # or more red, which nagged him about something he had deliberately
                # decided.
                "days_unlogged": len(rows) - len(done),
                "protein_met_days": sum(1 for r in done if r["protein_met"]),
                "fibre_respected_days": sum(1 for r in done if r["fibre_ok"]),
                "settled_days": len(settled),
                # SETTLED, not logged. These two sat beside the balance figures on one
                # card while averaging over a different set of days, so the mean intake
                # included today's part-logged 743 kcal and the balance beside it did not:
                # two denominators, one card, no way to tell from the page.
                "mean_kcal": (round(sum(r["kcal"] or 0 for r in settled) / len(settled))
                              if settled else None),
                "mean_target": (round(sum(r["kcal_target"] for r in by_target)
                                      / len(by_target)) if by_target else None),
                # THE ROLLING ENERGY BALANCE, which is the figure that means anything in
                # Ironman training. A single day swings by thousands - one day's
                # maintenance is 5,218 and the next is 2,491 - so a daily number is noise
                # wearing a verdict.
                #
                # Against MAINTENANCE, and POSITIVE MEANS A REAL DEFICIT. The figure this
                # replaces was intake minus the deficit-adjusted target, so it measured
                # adherence, reported the opposite sign to its own name, and read "+73
                # kcal/day deficit" on a day he had eaten 73 over the plan.
                #
                # Averaged over the days SETTLED, never over the window. An unlogged day is
                # not a zero-intake day; treating it as one would report a catastrophic
                # deficit for a day he simply did not write down, and he logs to spot-check
                # rather than daily. Coverage is published alongside so the figure can never
                # be read as a full week when it is two days.
                "mean_deficit_vs_maintenance_kcal_day": (round(deficit_vs_maint)
                                                         if deficit_vs_maint is not None
                                                         else None),
                "maintenance_basis_days": len(by_maint),
                # 7,700 kcal per kg of body mass, the standard figure. POSITIVE MEANS
                # WEIGHT DOWN, because it is derived from the deficit above. Stated as a
                # rate so it can be checked against the ONLY measured quantity in the
                # chain - the rolling morning weight - rather than believed on its own.
                "implied_kg_per_week": (round(deficit_vs_maint * 7 / 7700.0, 2)
                                        if deficit_vs_maint is not None else None),
                # A SEPARATE QUESTION, separately named: did he eat what the plan asked?
                # Positive means over the target. Jamie: "over yersterday and tommorow it
                # should balance out" - so this is a rolling mean and a single day under
                # target is not a verdict.
                "target_adherence_kcal_day": (round(adherence) if adherence is not None
                                              else None),
                "target_basis_days": len(by_target),
                "deficit_coverage": (f"{len(by_maint)} of {len(rows)} settled days"
                                     if rows else None),
                "in_run_sessions": len(runs),
                "in_run_on_target": sum(1 for r in runs
                                        if r["in_run_verdict"] in ("on_target",
                                                                   "acceptable")),
                "flag_days": sum(1 for r in rows if r["flags"]),
            },
        }

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
        "week": _week(7),
        "fortnight": _week(14),
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
        # Pass the diversity block through WHOLE rather than picking keys. Hand-picking
        # them is how `provisional` was computed and then never reached the page, which is
        # the fourth instance today of a value produced at one stage and dropped at the
        # next. A dict that forwards everything cannot silently omit a new field.
        "plants": {**div, "basis": div["target_basis"],
                   "new_today": div["new_species_today"]},
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
