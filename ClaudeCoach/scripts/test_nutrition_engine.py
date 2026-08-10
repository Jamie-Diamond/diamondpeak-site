#!/usr/bin/env python3
"""Offline tests for lib/nutrition_engine.py. Run: python3 scripts/test_nutrition_engine.py

Covers the cases that would silently corrupt the longitudinal record if they
regressed: the resting subtraction, the deficit ceilings, the fibre lookahead,
pacing suppression on long days, the sweat-weigh-in filter, and the collagen
protein exclusion.
"""
import sys
from datetime import date, timedelta
from pathlib import Path

_here = Path(__file__).resolve().parent
for cand in (_here / "lib", _here.parent / "lib"):
    if (cand / "nutrition_engine.py").exists():
        sys.path.insert(0, str(cand))
        break
import nutrition_engine as N

FAILED = []


def check(name, cond):
    print(("PASS" if cond else "FAIL"), name)
    if not cond:
        FAILED.append(name)


TODAY = date(2026, 8, 10)
DOB = date(1995, 5, 6)

# 1) Mifflin-St Jeor against the spec's stated figure (~1,840 at 83 kg)
rmr83 = N.mifflin_st_jeor(83.0, 1.86, DOB, "M", on=TODAY)
check("age is 31 on 2026-08-10", N.age_years(DOB, TODAY) == 31)
check(f"RMR at 83kg is ~1842 (got {rmr83:.0f})", 1838 <= rmr83 <= 1846)
check("base TDEE ~2488", 2470 <= N.base_tdee(rmr83) <= 2510)

# 2) The resting subtraction actually subtracts. A 2-hour ride burning 1600 kcal
#    should net ~1600 − 2×(1842/24) = ~1447, not 1600.
ride2h = [{"type": "Ride", "moving_time": 7200, "calories": 1600, "average_watts": 210}]
net, conf = N.net_session_kcal(ride2h, rmr83)
check(f"net session kcal subtracts resting (got {net})", 1440 <= net <= 1455)
check("power-metered ride reads 'measured'", conf == "measured")
hr_run = [{"type": "Run", "moving_time": 3600, "calories": 700}]
check("HR-only session reads 'estimated'", N.net_session_kcal(hr_run, rmr83)[1] == "estimated")

# 3) Day classification. Ride wins over run in a brick, and the longest single
#    session decides it rather than the daily total.
check("4h ride is long_ride", N.classify_day(
    [{"type": "Ride", "moving_time": 14400, "calories": 3000}]) == "long_ride")
check("brick 4h ride + 30min run is long_ride", N.classify_day([
    {"type": "Ride", "moving_time": 14400}, {"type": "Run", "moving_time": 1800}]) == "long_ride")
check("2h run is long_run", N.classify_day([{"type": "Run", "moving_time": 7200}]) == "long_run")
check("two 90min rides is standard, not long_ride", N.classify_day([
    {"type": "Ride", "moving_time": 5400}, {"type": "Ride", "moving_time": 5400}]) == "standard")
check("30min swim is recovery", N.classify_day([{"type": "Swim", "moving_time": 1800}]) == "recovery")
check("nothing logged is recovery", N.classify_day([]) == "recovery")

# 4) Deficit ceilings - the three that hold regardless of the flag.
common = dict(rolling_weight=83.3, rmr=rmr83, deficit_enabled=True)
t_std = N.targets(day_type="standard", sessions=ride2h, **common)
check(f"deficit applies on a standard day (got {t_std['deficit_applied_kcal']})",
      t_std["deficit_applied_kcal"] == N.DEFICIT_KCAL_DEFAULT)

t_long = N.targets(day_type="long_ride",
                   sessions=[{"type": "Ride", "moving_time": 14400, "calories": 3200,
                              "average_watts": 200}], **common)
check("deficit suppressed on long_ride", t_long["deficit_applied_kcal"] == 0)
check("suppression is explained", any("long session" in w for w in t_long["warnings"]))

t_rhr = N.targets(day_type="standard", sessions=ride2h, rhr_guard_active=True, **common)
check("deficit suppressed while RHR guard active", t_rhr["deficit_applied_kcal"] == 0)
check("RHR suppression is explained", any("resting HR" in w for w in t_rhr["warnings"]))

t_off = N.targets(day_type="standard", sessions=ride2h, rolling_weight=83.3, rmr=rmr83)
check("deficit off by default", t_off["deficit_applied_kcal"] == 0)
check("deficit lowers the target by exactly the deficit",
      t_off["kcal_target"] - t_std["kcal_target"] == N.DEFICIT_KCAL_DEFAULT)

# 5) Protein is flat across every day type - the one target that must not move.
prot = {N.targets(day_type=dt, rolling_weight=83.3, rmr=rmr83)["protein_target_g"]
        for dt in N.DAY_TYPES}
check("protein target is flat across all day types", prot == {N.PROTEIN_G_FLAT})

# 6) Fibre: ceiling the day BEFORE a long session, regardless of today's own type.
g, ceil = N.fibre_target("recovery")
check(f"recovery day fibre is a target ~42 (got {g})", g > 35 and ceil is False)
g, ceil = N.fibre_target("recovery", tomorrow_type="long_ride")
check("recovery day before a long ride flips to a ceiling", g <= 20 and ceil is True)
g, ceil = N.fibre_target("long_run")
check("long run day is itself a ceiling", g <= 20 and ceil is True)
g, ceil = N.fibre_target("standard", days_to_race=3)
check("race week overrides to 10-15g ceiling", 10 <= g <= 15 and ceil is True)
check("race week beats the day-before rule",
      N.fibre_target("standard", tomorrow_type="long_ride", days_to_race=2)[0] <= 15)

# 7) Pacing. Fires on a front-loaded fat day, suppressed on long days and on any
#    day containing in-session fuel.
tgt = N.targets(day_type="standard", sessions=ride2h, rolling_weight=83.3, rmr=rmr83)
frontload = [
    {"resolved_name": "M&S nut collection 75g", "kcal": 470, "fat_g": 42, "protein_g": 12, "carb_g": 12},
    {"resolved_name": "high-protein ready meal", "kcal": 450, "fat_g": 20, "protein_g": 40, "carb_g": 30},
]
p = N.pacing(frontload, tgt)
check("fat front-load is flagged", any(f["macro"] == "fat" for f in p["flags"]))
fat_flag = [f for f in p["flags"] if f["macro"] == "fat"][0]
check("flag names ranked contributors", len(fat_flag["contributors"]) == 2)
check("top contributor is the nuts", fat_flag["contributors"][0]["name"].startswith("M&S"))

p_long = N.pacing(frontload, t_long)
check("pacing suppressed on a long day", p_long["suppressed"] is True and not p_long["flags"])
p_insess = N.pacing(frontload + [{"resolved_name": "Maurten 320", "kcal": 320, "carb_g": 80,
                                  "in_session": True}], tgt)
check("pacing suppressed when in-session fuel present", p_insess["suppressed"] is True)

# 8) In-session fuel is marked protected so nothing can offer it for reduction.
ranked = N.rank_contributors([{"resolved_name": "gel", "carb_g": 30, "in_session": True},
                              {"resolved_name": "toast", "carb_g": 40}], "carb_g")
check("in-session items are flagged protected",
      [r["protected"] for r in ranked if r["name"] == "gel"] == [True])

# 9) Weight: sweat weigh-ins must not enter the rolling mean.
days = [TODAY - timedelta(days=i) for i in range(7)]
clean = [{"type": "weight", "date": d.isoformat(),
          "logged_at": f"{d.isoformat()}T06:15", "value": 83.3} for d in days]
check("clean 7-day mean is 83.3", N.rolling_weight_kg(clean, on=TODAY) == 83.3)

contaminated = clean + [
    {"type": "weight", "date": TODAY.isoformat(),
     "logged_at": f"{TODAY.isoformat()}T13:40", "value": 80.4},          # post-ride, later same day
    {"type": "weight", "date": days[1].isoformat(),
     "logged_at": f"{days[1].isoformat()}T14:02", "value": 80.6, "tag": "session_sweat"},
]
check("later same-day reading is excluded by time",
      N.rolling_weight_kg(contaminated, on=TODAY) == 83.3)
check("explicitly tagged sweat reading is excluded",
      N.rolling_weight_kg(contaminated, on=TODAY) == 83.3)
check("pre-04:00 reading is excluded", N.rolling_weight_kg(
    clean + [{"type": "weight", "date": TODAY.isoformat(),
              "logged_at": f"{TODAY.isoformat()}T03:10", "value": 70.0}], on=TODAY) == 83.3)
check("no usable readings returns None", N.rolling_weight_kg([], on=TODAY) is None)

# 10) RHR guard. Baseline must exclude the days under test, or a sustained
#     elevation drags its own reference up and the guard stops firing.
wellness = [{"id": (TODAY - timedelta(days=i)).isoformat(), "restingHR": 52}
            for i in range(2, 32)]
wellness += [{"id": TODAY.isoformat(), "restingHR": 76},
             {"id": (TODAY - timedelta(days=1)).isoformat(), "restingHR": 71}]
g = N.rhr_guard(wellness, on=TODAY)
check(f"RHR guard fires on 71/76 against a 52 baseline (got {g})", g["active"] is True)
check("baseline is ~52, not dragged by the spike", 51 <= g["baseline_bpm"] <= 53)

calm = [{"id": (TODAY - timedelta(days=i)).isoformat(), "restingHR": 52} for i in range(32)]
check("RHR guard quiet at baseline", N.rhr_guard(calm, on=TODAY)["active"] is False)
check("one elevated day is not enough", N.rhr_guard(
    [{"id": (TODAY - timedelta(days=i)).isoformat(), "restingHR": 52} for i in range(2, 32)]
    + [{"id": TODAY.isoformat(), "restingHR": 76},
       {"id": (TODAY - timedelta(days=1)).isoformat(), "restingHR": 52}],
    on=TODAY)["active"] is False)
check("no data is not an active guard", N.rhr_guard([], on=TODAY)["active"] is False)

# 10b) REGRESSION: the guard must stay firable at consecutive=3, and must survive
#      a missing current-day row. An earlier cut keyed the tested window off
#      calendar days, so an absent today row (routine in ICU) made the guard
#      silently unfirable - coverage that does not fire is worse than none.
base30 = [{"id": (TODAY - timedelta(days=i)).isoformat(), "restingHR": 52}
          for i in range(3, 34)]
three_up = base30 + [{"id": (TODAY - timedelta(days=i)).isoformat(), "restingHR": 70}
                     for i in (0, 1, 2)]
g3 = N.rhr_guard(three_up, on=TODAY, consecutive=3)
check(f"guard fires at consecutive=3 (got {g3.get('active')}, {g3.get('reason', '')})",
      g3["active"] is True)

# today's row absent, yesterday and the day before elevated
missing_today = [{"id": (TODAY - timedelta(days=i)).isoformat(), "restingHR": 52}
                 for i in range(3, 34)]
missing_today += [{"id": (TODAY - timedelta(days=1)).isoformat(), "restingHR": 74},
                  {"id": (TODAY - timedelta(days=2)).isoformat(), "restingHR": 71}]
g_missing = N.rhr_guard(missing_today, on=TODAY)
check(f"guard still fires when today's row is missing (got {g_missing.get('active')})",
      g_missing["active"] is True)
check("guard reports which days it tested", len(g_missing.get("tested_days", [])) == 2)

# two elevated readings a week apart are not consecutive
scattered = base30 + [{"id": TODAY.isoformat(), "restingHR": 76},
                      {"id": (TODAY - timedelta(days=8)).isoformat(), "restingHR": 76}]
check("scattered elevated readings do not count as consecutive",
      N.rhr_guard(scattered, on=TODAY)["active"] is False)

# baseline must not include the days under test at consecutive=3 either
check("baseline excludes the tested days at consecutive=3",
      51 <= N.rhr_guard(three_up, on=TODAY, consecutive=3)["baseline_bpm"] <= 53)

# a thin baseline is insufficient history, not a quiet pass
thin = [{"id": (TODAY - timedelta(days=i)).isoformat(), "restingHR": 52} for i in (2, 3, 4)]
thin += [{"id": TODAY.isoformat(), "restingHR": 76},
         {"id": (TODAY - timedelta(days=1)).isoformat(), "restingHR": 76}]
r = N.rhr_guard(thin, on=TODAY)
check(f"thin baseline reports insufficient history (got {r.get('reason')})",
      r["active"] is False and "baseline" in (r.get("reason") or ""))

# 11) Under-fuelling guard fires whether or not the deficit is deliberate.
check("underfuel fires well below the floor",
      N.underfuel_flag([{"kcal": 1500}], tgt, rmr83) is not None)
check("underfuel quiet at target",
      N.underfuel_flag([{"kcal": tgt['kcal_target']}], tgt, rmr83) is None)

# 12) Collagen is excluded from the protein total.
counting, non_counting = N.counting_protein_g([
    {"resolved_name": "chicken breast", "protein_g": 40},
    {"resolved_name": "collagen peptides 15g", "protein_g": 15},
])
check(f"collagen excluded from protein total (got {counting})", counting == 40.0)
check("collagen still reported separately", non_counting == 15.0)

# 13) Race-weight projection tells the truth about what the deficit delivers.
proj = N.race_weight_projection(83.3, 79.0, days_to_race=40)
check(f"40-day projection lands ~81.9, not 79 (got {proj['projected_race_kg']})",
      81.0 <= proj["projected_race_kg"] <= 82.5)
check("projection admits it misses the target", proj["reaches_target"] is False)
check(f"required daily deficit to reach 79 is ~830 (got {proj['required_daily_kcal_to_reach']})",
      780 <= proj["required_daily_kcal_to_reach"] <= 880)

# 14) Micronutrients report compliance, never a fabricated adequacy verdict.
micro = N.micronutrient_status([{"nutrient": "vitamin_d", "dose": 2000, "unit": "IU"}])
check("supplemented nutrient reads supplemented", micro["vitamin_d"]["state"] == "supplemented")
check("unsupplemented nutrient reads not_supplemented",
      micro["iron"]["state"] == "not_supplemented")
check("no nutrient is ever labelled adequate or low",
      all(v["state"] in ("supplemented", "not_supplemented", "unknown")
          for v in micro.values()))

# 15) Sodium is a band, tagged assumed, and never a target.
check("sodium reported as an assumed band",
      tgt["sodium_basis"]["confidence"] == "assumed"
      and tgt["sodium_basis"]["sweat_na_mg_l"] == [950, 1500])

print()
if FAILED:
    print(f"{len(FAILED)} FAILED: " + ", ".join(FAILED))
    sys.exit(1)
print("all checks passed")
