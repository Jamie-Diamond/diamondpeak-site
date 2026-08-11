#!/usr/bin/env python3
"""Offline tests for lib/nutrition_engine.py (spec v0.2).
Run: python3 ClaudeCoach/scripts/test_nutrition_engine.py

Covers what would silently corrupt the record or mislead the athlete: the resting
subtraction, the proportional deficit and its ceilings, zone collapse, the fibre
lookahead flip, projection-based flags (which replaced percentage pacing), the
sweat weigh-in filter, and the closed-loop correction.
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
W = 83.3
RMR = N.mifflin_st_jeor(W, 1.86, DOB, "M", on=TODAY)
RIDE2H = [{"type": "Ride", "moving_time": 7200, "calories": 1600, "average_watts": 210}]
RUN90 = [{"type": "Run", "moving_time": 5400, "calories": 900}]
LONGRIDE = [{"type": "Ride", "moving_time": 14400, "calories": 3200, "average_watts": 200}]


def z(**kw):
    kw.setdefault("rolling_weight", W)
    kw.setdefault("rmr", RMR)
    return N.zones(**kw)


# 1) Anthropometrics
check("age is 31 on 2026-08-10", N.age_years(DOB, TODAY) == 31)
check(f"RMR at 83.3kg is ~1845 (got {RMR:.0f})", 1840 <= RMR <= 1850)

# 2) The resting subtraction actually subtracts, and confidence is honest.
net, conf = N.net_session_kcal(RIDE2H, RMR)
check(f"net subtracts resting energy (got {net})", 1440 <= net <= 1455)
check("power-metered reads measured", conf == "measured")
check("HR-only reads estimated", N.net_session_kcal(RUN90, RMR)[1] == "estimated")

# 3) Day classification: longest single session decides, ride beats run in a brick.
check("4h ride is long_ride", N.classify_day(LONGRIDE) == "long_ride")
check("brick reads long_ride", N.classify_day(
    LONGRIDE + [{"type": "Run", "moving_time": 1800}]) == "long_ride")
check("2h run is long_run", N.classify_day([{"type": "Run", "moving_time": 7200}]) == "long_run")
check("two 90min rides is standard", N.classify_day(
    [{"type": "Ride", "moving_time": 5400}, {"type": "Ride", "moving_time": 5400}]) == "standard")
check("nothing is recovery", N.classify_day([]) == "recovery")

# 4) Empty calendar is NOT a rest day: fall back to the typical week, low confidence.
rules = {"swim_days": ["Tue", "Thu"], "bike_days": ["Fri", "Sat", "Sun"],
         "run_days": ["Tue", "Wed", "Sat", "Sun"]}
dt, cf = N.classify_from_day_rules(date(2026, 8, 14), rules)     # a Friday, bike day
check("scheduled day falls back to standard", dt == "standard")
check("fallback is marked low_confidence", cf == "low_confidence")
dt2, _ = N.classify_from_day_rules(date(2026, 8, 10), rules)     # a Monday, nothing
check("unscheduled day falls back to recovery", dt2 == "recovery")
check("fallback never guesses a long day",
      all(N.classify_from_day_rules(TODAY + timedelta(days=i), rules)[0]
          not in N.LONG_DAY_TYPES for i in range(7)))

# 5) Zones carry a bias, which is what decides warning direction.
zz = z(day_type="standard", sessions=RIDE2H)
check("protein is a floor", zz["protein_g"]["bias"] == N.BIAS_FLOOR)
check("calories are a band", zz["kcal"]["bias"] == N.BIAS_BAND)
check("fibre on a normal day is a floor", zz["fibre_g"]["bias"] == N.BIAS_FLOOR)
check("fat zone declares which end is sourced and which is practice",
      "sourced" in zz["fat_g"]["basis"] and "practice" in zz["fat_g"]["basis"])
check("protein zone states its g/kg basis", "g/kg" in zz["protein_g"]["basis"])
check("carb zone says it is the remainder", "remainder" in zz["carb_g"]["basis"])
check("calorie band is +/-5%",
      abs(zz["kcal"]["high"] - zz["kcal"]["low"] - 0.1 * zz["kcal_target"]) < 2)

# 6) REGRESSION: the deficit must never collapse a zone to a point. A first cut
#    capped it at the full headroom, so residual fat landed exactly on its floor
#    and the fat zone became 75-75 on recovery and HR-estimated days.
for label, kw in (("recovery", dict(day_type="recovery")),
                  ("standard, HR run", dict(day_type="standard", sessions=RUN90)),
                  ("standard, powered", dict(day_type="standard", sessions=RIDE2H))):
    zc = z(deficit_enabled=True, **kw)
    width = zc["fat_g"]["high"] - zc["fat_g"]["low"]
    check(f"fat zone keeps width on {label} (got {width:.1f} g)",
          width >= N.FAT_ZONE_MIN_WIDTH_G - 0.5)

# 6b) REGRESSION: no zone may collapse to a point on ANY day shape. The pre-long
#     carb-floor lift caused this a second time, in a different place: capping the
#     lift at exactly where the fat floor fits gave fat 75-75 and carbs 288-288.
#     Silencing a nuisance warning took the zones with it.
for label, kw in (("recovery", dict(day_type="recovery")),
                  ("pre-long recovery", dict(day_type="recovery",
                                             tomorrow_type="long_ride")),
                  ("pre-long standard", dict(day_type="standard", sessions=RIDE2H,
                                             tomorrow_type="long_ride")),
                  ("post-long recovery", dict(day_type="recovery",
                                              yesterday_type="long_ride")),
                  ("long ride", dict(day_type="long_ride", sessions=LONGRIDE)),
                  ("HR-only standard", dict(day_type="standard", sessions=RUN90))):
    zc = z(deficit_enabled=True, **kw)
    for macro in ("fat_g", "carb_g"):
        width = zc[macro]["high"] - zc[macro]["low"]
        check(f"{label}: {macro} keeps a real range (got {width:.0f} g)", width >= 10)

# 6c) And a pre-long recovery day must not emit the fat-floor warning at all: it
#     fired on every one of them for the sake of 2 g of fat, which trains the
#     athlete to ignore warnings that matter.
prelong_rec = z(day_type="recovery", tomorrow_type="long_ride", deficit_enabled=True)
check("pre-long recovery day does not warn about the fat floor",
      not any("no calorie room for the fat floor" in w
              for w in prelong_rec["warnings"]))
check("the carb floor easing is reported, not silent",
      any("eased" in m or "capped" in m for m in prelong_rec["modifiers"]))

# 7) The deficit is proportional, headroom-capped, and zero on the protected days.
rec = z(day_type="recovery", deficit_enabled=True)
std = z(day_type="standard", sessions=RIDE2H, deficit_enabled=True)
check(f"recovery deficit is small (got {rec['deficit_applied_kcal']})",
      0 <= rec["deficit_applied_kcal"] <= 60)
check(f"standard deficit is ~10% of maintenance (got {std['deficit_applied_kcal']})",
      370 <= std["deficit_applied_kcal"] <= 400)
check("proportional means the bigger day carries the bigger cut",
      std["deficit_applied_kcal"] > rec["deficit_applied_kcal"])
check("recovery cap is explained in warnings",
      any("floors leave only" in w for w in rec["warnings"]))

lng = z(day_type="long_ride", sessions=LONGRIDE, deficit_enabled=True)
check("no deficit on a long day", lng["deficit_applied_kcal"] == 0)
check("long-day suppression explained", any("long session" in w for w in lng["warnings"]))

guard = z(day_type="standard", sessions=RIDE2H, deficit_enabled=True, rhr_guard_active=True)
check("no deficit while the RHR guard is active", guard["deficit_applied_kcal"] == 0)
check("RHR suppression explained", any("resting HR" in w for w in guard["warnings"]))

pre = z(day_type="standard", sessions=RIDE2H, deficit_enabled=True, tomorrow_type="long_ride")
check("no deficit the day before a long session", pre["deficit_applied_kcal"] == 0)
check("pre-long suppression explained", any("glycogen" in w for w in pre["warnings"]))

off = z(day_type="standard", sessions=RIDE2H)
check("deficit off by default", off["deficit_applied_kcal"] == 0)
check("deficit lowers the target", off["kcal_target"] > std["kcal_target"])

# 7b) The day must CLOSE: protein floor + fat floor + carb high == the target. A
#     prescribed carb band left 824 kcal unallocated on long days.
#     NOTE this is close to an identity while the carb safety floor does not bind,
#     which is the point: the check that earns its keep is 7b-ii below, where the
#     floor DOES bind and max() would otherwise paper over an unsatisfiable day.
for label, zc in (("recovery", rec), ("standard", std), ("long_ride", lng),
                  ("pre-long", pre)):
    total = zc["protein_g"]["low"] * 4 + zc["fat_g"]["low"] * 9 + zc["carb_g"]["high"] * 4
    check(f"{label} day closes to its target (got {total:.0f} vs {zc['kcal_target']})",
          abs(total - zc["kcal_target"]) <= 2)
    check(f"{label} carb high is not below its safety floor",
          zc["carb_g"]["high"] >= zc["carb_g"]["low"] - 0.5)

# 7b-ii) When the floors genuinely exceed the day's energy, the engine must SAY so
#        rather than let max() hide it. Forced with an implausibly low RMR, because
#        the headroom cap makes this unreachable through the deficit alone.
# REGRESSION: the carb easing must not make an impossible day "fit" by starving
# carbs. An unbounded ease took the floor from 250 g to 35 g (0.4 g/kg) and the
# warning stopped firing, which is worse than the collapse it was fixing.
squeezed = N.zones(day_type="recovery", rolling_weight=W, rmr=1200.0)
check("an unsatisfiable day says so",
      any("does not fit" in w or "no calorie room" in w for w in squeezed["warnings"]))
check("carb easing is bounded, never starved to nothing",
      squeezed["carb_g"]["low"] >= 3.0 * W * N.CARB_EASE_FLOOR_FRACTION - 0.5)
check("an unsatisfiable day still returns a usable fat floor",
      squeezed["fat_g"]["low"] > 0)
check("an unsatisfiable day does not silently invert the carb zone",
      squeezed["carb_g"]["high"] >= squeezed["carb_g"]["low"] - 0.5)

# 7c) A deficit too small to mean anything is dropped, not reported.
check("recovery day reports no deficit rather than a 16 kcal one",
      rec["deficit_applied_kcal"] == 0)
check("the protein deficit bump is never claimed without a deficit",
      all((z_["deficit_applied_kcal"] > 0)
          == any("lean mass" in m for m in z_["modifiers"])
          for z_ in (rec, std, lng, pre)))

# 7d) Protein flexes with load and with the deficit, in g/kg.
check("protein floor is higher on a long day than a recovery day",
      lng["protein_g"]["low"] > rec["protein_g"]["low"])
check("protein floor rises when a deficit is actually applied",
      std["protein_g"]["low"] > z(day_type="standard", sessions=RIDE2H)["protein_g"]["low"])
check("protein tracks bodyweight, not a fixed gram figure",
      z(day_type="standard", rolling_weight=79.0, rmr=RMR)["protein_g"]["low"]
      < z(day_type="standard", rolling_weight=88.0, rmr=RMR)["protein_g"]["low"])

# 7e) Race week: carbs are prescribed and the energy follows them, with the surplus
#     stated. Deriving the target from maintenance left the day 1,470 kcal short.
rw = z(day_type="standard", sessions=RIDE2H, days_to_race=2, deficit_enabled=True)
check("race week reports a surplus against maintenance",
      rw["carb_load_surplus_kcal"] > 0)
check("race week target exceeds maintenance", rw["kcal_target"] > rw["kcal_maintenance"])
check("no deficit during the carb load", rw["deficit_applied_kcal"] == 0)
check("carb load suppression is explained",
      any("carb loading" in w for w in rw["warnings"]))

# 8) Fat is bidirectional: crowded on low days, GI-limited on pre-long days.
check("fat is capped in g/kg, so a long day cannot quote an absurd ceiling",
      lng["fat_g"]["high"] <= N.FAT_CEILING_G_PER_KG * W + 0.5)
check("carbs, not fat, absorb the long day's energy",
      lng["carb_g"]["high"] > rec["carb_g"]["high"] * 3)
check("pre-long day tightens the fat ceiling to 90 g",
      pre["fat_g"]["high"] <= N.FAT_CEILING_PRE_LONG_G)
check("fat floor is 0.9 g/kg on rolling weight",
      abs(std["fat_g"]["low"] - 0.9 * W) < 0.6)

# 9) Fibre: same day type, opposite bias, decided purely by the lookahead.
plain = z(day_type="recovery")
flip = z(day_type="recovery", tomorrow_type="long_ride")
check("recovery day normally targets high fibre",
      plain["fibre_g"]["low"] >= 40 and plain["fibre_g"]["bias"] == N.BIAS_FLOOR)
check("same day before a long ride flips to a ceiling",
      flip["fibre_g"]["high"] <= 20 and flip["fibre_g"]["bias"] == N.BIAS_CEILING)
check("the flip is reported as a modifier",
      any("fibre flipped" in m for m in flip["modifiers"]))
race = z(day_type="standard", days_to_race=2)
check("race week is a 10-15 g fibre ceiling",
      race["fibre_g"]["bias"] == N.BIAS_CEILING and race["fibre_g"]["high"] <= 15)
check("race week loads carbs to 10-12 g/kg",
      race["carb_g"]["low"] >= 10 * W - 1)
check("carbs go to the upper half before a long session",
      flip["carb_g"]["low"] > plain["carb_g"]["low"])

# 10) Post-long-ride protein modifier.
post = z(day_type="recovery", yesterday_type="long_ride")
check("protein floor rises after a long ride",
      abs(post["protein_g"]["low"] - N.PROTEIN_POST_LONG_FLOOR_G_PER_KG * W) < 0.6)
check("the modifier is reported", any("yesterday" in m for m in post["modifiers"]))

# 11) Projection replaces percentage pacing. No pacing function should survive.
check("percentage pacing is gone", not hasattr(N, "pacing"))
check("v0.1 targets() is gone", not hasattr(N, "targets"))

proj = N.project({"kcal": 1200, "protein_g": 60, "fat_g": 70, "carb_g": 120, "fibre_g": 8})
check("with no stated plan the projection is open-ended",
      proj["protein_g"]["open_ended"] is True and proj["protein_g"]["high"] is None)
flags = N.zone_flags(std, proj)
check("an open-ended projection cannot declare a floor unreachable",
      not any(f["direction"] == "cannot_reach_floor" for f in flags))

# already past a ceiling: that IS knowable without a plan
zc = z(day_type="standard", sessions=RIDE2H, tomorrow_type="long_ride")
over = N.project({"fibre_g": 35, "kcal": 1000})
fl = N.zone_flags(zc, over)
check("an exceeded fibre ceiling flags without a plan",
      any(f["macro"] == "fibre_g" and f["direction"] == "exceeds_ceiling" for f in fl))

# with a stated plan, an unreachable floor flags
plan = {"protein_g": (20, 40), "kcal": (800, 1400)}
short = N.project({"protein_g": 60, "kcal": 1200}, plan)
fl2 = N.zone_flags(std, short)
check("a protein floor that cannot be reached flags",
      any(f["macro"] == "protein_g" and f["direction"] == "cannot_reach_floor" for f in fl2))
check("the flag reports the distance from the zone",
      [f for f in fl2 if f["macro"] == "protein_g"][0]["distance"] > 0)

# straddling a boundary is NOT a flag
straddle = N.project({"protein_g": 140}, {"protein_g": (10, 60)})
check("straddling the floor is not a flag",
      not any(f["macro"] == "protein_g" for f in N.zone_flags(std, straddle)))

# a floor never flags high
plenty = N.project({"protein_g": 240}, {"protein_g": (0, 0)})
check("exceeding a protein floor is not an event",
      not any(f["macro"] == "protein_g" for f in N.zone_flags(std, plenty)))

# a ceiling never flags low: low fibre on a pre-long day is compliance
low_fibre = N.project({"fibre_g": 6}, {"fibre_g": (0, 4)})
check("low fibre on a pre-long day is compliance, not failure",
      not any(f["macro"] == "fibre_g" for f in N.zone_flags(zc, low_fibre)))

check("flags are ranked worst first",
      all(fl2[i]["distance"] >= fl2[i + 1]["distance"] for i in range(len(fl2) - 1)))

# 12) No long-day special case is needed any more (v0.2 7).
in_sess = N.project({"carb_g": 240, "kcal": 2000}, {"carb_g": (200, 400)})
check("long days need no suppression rule", isinstance(N.zone_flags(lng, in_sess), list))

# 13) Contributor ranking survives, and in-session fuel is protected.
ranked = N.rank_contributors([{"resolved_name": "gel", "carb_g": 30, "in_session": True},
                              {"resolved_name": "toast", "carb_g": 40}], "carb_g")
check("contributors ranked by amount", ranked[0]["name"] == "toast")
check("in-session fuel is marked protected",
      [r["protected"] for r in ranked if r["name"] == "gel"] == [True])

# 14) Weight: sweat weigh-ins must never enter the rolling mean.
days = [TODAY - timedelta(days=i) for i in range(7)]
clean = [{"type": "weight", "date": d.isoformat(),
          "logged_at": f"{d.isoformat()}T06:15", "value": 83.3} for d in days]
check("clean mean is 83.3", N.rolling_weight_kg(clean, on=TODAY) == 83.3)
check("later same-day reading excluded", N.rolling_weight_kg(
    clean + [{"type": "weight", "date": TODAY.isoformat(),
              "logged_at": f"{TODAY.isoformat()}T13:40", "value": 80.4}], on=TODAY) == 83.3)
check("tagged sweat reading excluded", N.rolling_weight_kg(
    clean + [{"type": "weight", "date": TODAY.isoformat(), "logged_at": f"{TODAY}T14:00",
              "value": 80.6, "tag": "session_sweat"}], on=TODAY) == 83.3)
check("pre-04:00 reading excluded", N.rolling_weight_kg(
    clean + [{"type": "weight", "date": TODAY.isoformat(),
              "logged_at": f"{TODAY.isoformat()}T03:10", "value": 70.0}], on=TODAY) == 83.3)
check("no data returns None", N.rolling_weight_kg([], on=TODAY) is None)
try:
    z(day_type="standard", rolling_weight=None)
    check("zones refuses to run without a rolling weight", False)
except ValueError:
    check("zones refuses to run without a rolling weight", True)

# 15) RHR guard, including the unfirable-at-3 regression.
base30 = [{"id": (TODAY - timedelta(days=i)).isoformat(), "restingHR": 52} for i in range(2, 32)]
spike = base30 + [{"id": TODAY.isoformat(), "restingHR": 76},
                  {"id": (TODAY - timedelta(days=1)).isoformat(), "restingHR": 71}]
g = N.rhr_guard(spike, on=TODAY)
check("guard fires on 71/76 against a 52 baseline", g["active"] is True)
check("baseline not dragged by the spike", 51 <= g["baseline_bpm"] <= 53)
calm = [{"id": (TODAY - timedelta(days=i)).isoformat(), "restingHR": 52} for i in range(32)]
check("quiet at baseline", N.rhr_guard(calm, on=TODAY)["active"] is False)
b3 = [{"id": (TODAY - timedelta(days=i)).isoformat(), "restingHR": 52} for i in range(3, 34)]
up3 = b3 + [{"id": (TODAY - timedelta(days=i)).isoformat(), "restingHR": 70} for i in (0, 1, 2)]
check("still firable at consecutive=3", N.rhr_guard(up3, on=TODAY, consecutive=3)["active"] is True)
miss = b3 + [{"id": (TODAY - timedelta(days=1)).isoformat(), "restingHR": 74},
             {"id": (TODAY - timedelta(days=2)).isoformat(), "restingHR": 71}]
check("fires with today's row missing", N.rhr_guard(miss, on=TODAY)["active"] is True)
check("scattered spikes are not consecutive", N.rhr_guard(
    base30 + [{"id": TODAY.isoformat(), "restingHR": 76},
              {"id": (TODAY - timedelta(days=8)).isoformat(), "restingHR": 76}],
    on=TODAY)["active"] is False)
check("no data is not an active guard", N.rhr_guard([], on=TODAY)["active"] is False)

# 16) Under-fuelling fires whether or not the deficit is deliberate.
check("underfuel fires below the floor", N.underfuel_flag([{"kcal": 1500}], std, RMR) is not None)
check("underfuel quiet at target",
      N.underfuel_flag([{"kcal": std["kcal_target"]}], std, RMR) is None)

# 17) Collagen excluded from protein.
c, nc = N.counting_protein_g([{"resolved_name": "chicken", "protein_g": 40},
                              {"resolved_name": "collagen peptides", "protein_g": 15}])
check("collagen excluded from protein", c == 40.0)
check("collagen reported separately", nc == 15.0)

# 18) Closed loop: gated on enough clean data, bounded, correctly signed.
none_yet = N.deficit_correction(clean[:3], 0.25, on=TODAY)
check("correction refuses to act on thin data", none_yet["usable"] is False)
check("thin data means zero correction, not a guess", none_yet["correction_kcal"] == 0)

flat = [{"type": "weight", "date": (TODAY - timedelta(days=i)).isoformat(),
         "logged_at": f"{(TODAY - timedelta(days=i)).isoformat()}T06:10",
         "value": 83.3, "tag": "morning"} for i in range(28)]
corr = N.deficit_correction(flat, 0.25, on=TODAY)
check(f"a flat trend against an intended loss asks for MORE deficit (got "
      f"{corr['correction_kcal']})", corr["usable"] and corr["correction_kcal"] > 0)

losing = [{"type": "weight", "date": (TODAY - timedelta(days=i)).isoformat(),
           "logged_at": f"{(TODAY - timedelta(days=i)).isoformat()}T06:10",
           "value": 83.3 + i * 0.09, "tag": "morning"} for i in range(28)]
corr2 = N.deficit_correction(losing, 0.25, on=TODAY)
check(f"losing faster than intended reduces the deficit (got {corr2['correction_kcal']})",
      corr2["correction_kcal"] < 0)
check("correction is bounded",
      abs(corr2["correction_kcal"]) <= N.CORRECTION_MAX_KCAL)

# the correction must actually move the target, and stay inside the headroom cap
z_corr = z(day_type="standard", sessions=RIDE2H, deficit_enabled=True, correction_kcal=200)
check("a positive correction increases the deficit",
      z_corr["deficit_applied_kcal"] > std["deficit_applied_kcal"])
z_big = z(day_type="recovery", deficit_enabled=True, correction_kcal=2000)
check("a correction cannot breach the headroom cap",
      z_big["deficit_applied_kcal"] <= z_big["deficit_headroom_kcal"])
check("a correction cannot collapse the fat zone",
      z_big["fat_g"]["high"] - z_big["fat_g"]["low"] >= N.FAT_ZONE_MIN_WIDTH_G - 0.5)

# 19) Race-weight projection tells the truth about the proportional deficit.
p = N.race_weight_projection(83.3, 79.0, 40, RMR)
check(f"projection lands short of 79 (got {p['projected_race_kg']})",
      81.0 <= p["projected_race_kg"] <= 83.0)
check("projection admits it misses", p["reaches_target"] is False)
check("projection states the shortfall", p["shortfall_kg"] > 0)
check(f"required daily deficit to reach 79 is ~830 "
      f"(got {p['required_daily_kcal_to_reach']})",
      780 <= p["required_daily_kcal_to_reach"] <= 880)
# The projection MUST agree with zones() day for day. It appears on the Peak tab and
# in the bot, and an earlier cut re-derived the arithmetic here and drifted: it priced
# the protein deficit bump unconditionally where zones() applies it only when headroom
# survives, so recovery days were projected off a floor the engine never uses.
check(f"projection agrees with zones() on a standard day "
      f"(got {p['deficit_by_day_type'].get('standard')} vs {std['deficit_applied_kcal']})",
      p["deficit_by_day_type"].get("standard") == std["deficit_applied_kcal"])
check(f"projection agrees with zones() on a recovery day "
      f"(got {p['deficit_by_day_type'].get('recovery')} vs {rec['deficit_applied_kcal']})",
      p["deficit_by_day_type"].get("recovery") == rec["deficit_applied_kcal"])
check("projection gives long days no deficit",
      p["deficit_by_day_type"].get("long_ride") == 0)

# 20) Micronutrients never fabricate an adequacy verdict.
micro = N.micronutrient_status([{"nutrient": "vitamin_d", "dose": 2000, "unit": "IU"}])
check("supplemented reads supplemented", micro["vitamin_d"]["state"] == "supplemented")
check("no nutrient is labelled adequate or low",
      all(v["state"] in ("supplemented", "not_supplemented", "unknown") for v in micro.values()))

# 21) Sodium stays an assumed band, never a target.
check("sodium is an assumed band",
      std["sodium_basis"]["confidence"] == "assumed"
      and std["sodium_basis"]["sweat_na_mg_l"] == [950, 1500])

# 22) meal_requirement: what to reach for. This answers a DIFFERENT question from
#     pace, and everything the page shows is computed here, because a rendering layer
#     doing its own arithmetic produces plausible wrong numbers instead of visible
#     errors.
Z_PRE = z(day_type="recovery", tomorrow_type="long_ride", deficit_enabled=True)


def req(totals, zone=None):
    return N.meal_requirement(totals, zone or std)


# The spec's own case: fat has met its floor but has almost no room, so "met" was the
# wrong verdict. Headroom gets the same density treatment as still-needed.
fatty = req({"kcal": 1800, "protein_g": 45, "carb_g": 140, "fat_g": 95, "fibre_g": 12})
check(f"a nearly-full ceiling reads avoid, not met (got {fatty['macros']['fat_g']['density']})",
      fatty["macros"]["fat_g"]["density"] == "avoid")
check("the callout names both the want and the avoid",
      "protein" in fatty["headline"] and "near zero fat" in fatty["headline"])
check("the reason quantifies the remaining room",
      "g of room left" in fatty["reason"])

# Fibre must not win the headline by default. It did on an empty day, headlining
# "Reach for fibre" while 183 g of protein was the actual gap.
empty = req({"kcal": 0})
check(f"an empty day does not headline fibre (got {empty['headline']!r})",
      "fibre" not in empty["headline"].lower())
check("an empty day says eat normally", "balanced" in empty["headline"])
check("but the reason still lists every floor outstanding",
      "protein under its floor" in empty["reason"])

# Fibre reaches the headline only when it is the ONLY thing outstanding.
only = req({"kcal": 3400, "protein_g": 190, "carb_g": 460, "fat_g": 85, "fibre_g": 18})
check("fibre headlines when nothing else is short", "fibre" in only["headline"].lower())

# A day under a floor must never be described as on track. An earlier cut said
# "every zone is on track" while fat sat below its floor.
short = req({"kcal": 2200, "protein_g": 130, "carb_g": 300, "fat_g": 60, "fibre_g": 22})
check("a day under a floor is not called on track",
      "on track" not in short["reason"] and "every zone is met" not in short["reason"])
check("and the shortfall is named", "fat under its floor" in short["reason"])

# Bars measure against the zone MINIMUM, so a met floor reads as met.
met = req({"kcal": 3500, "protein_g": 190, "carb_g": 480, "fat_g": 85, "fibre_g": 34})
check("a met floor reports 100% of floor",
      met["macros"]["protein_g"]["pct_of_floor"] == 100.0)
check("all zones met is stated plainly", "essentially there" in met["headline"]
      or "Every zone is met" in met["headline"])

# At or past the target, the page must stop asking for more energy.
over = req({"kcal": 3900, "protein_g": 200, "carb_g": 500, "fat_g": 95, "fibre_g": 35})
check("past the target is flagged as at_target", over["at_target"] is True)
check("and does not tell him to reach for more energy",
      "Reach for" not in over["headline"])

# A fibre CEILING day asks for low residue, never for more fibre.
pre = req({"kcal": 900, "protein_g": 40, "carb_g": 110, "fat_g": 25, "fibre_g": 9}, Z_PRE)
check("a pre-long day asks for low residue", "low residue" in pre["headline"])
check("and never asks for more fibre on a ceiling day",
      "Reach for fibre" not in pre["headline"])
check("fibre remaining room is reported as a ceiling", "ceiling has" in pre["reason"])

# The required share is compared against a NORMAL meal, which is what makes it mean
# anything. Both numbers are published so the page never invents the comparison.
pm = fatty["macros"]["protein_g"]
check("required share is published", pm["required_share"] > 0)
check("normal share is published alongside it", pm["normal_share"] == 0.20)
check("required protein density is above a normal meal",
      pm["required_share"] > pm["normal_share"])

# 23) ICU weight ingest. ICU holds ONE untimestamped weight per day and mixes morning
#     weights with sweat-rate weigh-ins, so they are separated by physics rather than by
#     time: nobody loses 3.3 kg of fat in three days. Real series from this athlete.
ICU_ROWS = [{"id": "2026-07-27", "weight": 83.099, "bodyFat": 10.8},
            {"id": "2026-07-30", "weight": 80.58, "bodyFat": 9.3},
            {"id": "2026-07-31", "weight": 81.65, "bodyFat": 9.9},
            {"id": "2026-08-03", "weight": 84.889, "bodyFat": 11.9},
            {"id": "2026-08-06", "weight": 81.589, "bodyFat": 9.9},
            {"id": "2026-08-10", "weight": 84.0, "bodyFat": 11.3}]
cls = N.classify_icu_weights(ICU_ROWS)
tags = {r["date"]: r["tag"] for r in cls}
check("the 2.5 kg drop is caught as a sweat weigh-in",
      tags["2026-07-30"] == "session_sweat")
check("the 3.3 kg drop is caught too", tags["2026-08-06"] == "session_sweat")
check("morning readings are kept", tags["2026-08-10"] == "morning"
      and tags["2026-08-03"] == "morning")
check("the first reading is accepted, having nothing to compare against",
      tags["2026-07-27"] == "morning")
morning = [r["value"] for r in cls if r["tag"] == "morning"]
mean = sum(morning) / len(morning)
allmean = sum(r["weight"] for r in ICU_ROWS) / len(ICU_ROWS)
check(f"filtered mean recovers ~83.4 kg (got {mean:.2f})", 83.2 <= mean <= 83.6)
check(f"the unfiltered mean would be ~0.8 kg lower (got {allmean:.2f})",
      mean - allmean > 0.6)
check("every rejection states its reason",
      all(r["reason"] for r in cls if r["tag"] == "session_sweat"))
check("body fat is carried through but never returned as a series",
      all("body_fat_pct" in r for r in cls))

# A timestamped reading the athlete logged himself always wins: known provenance beats
# inferred provenance, so the ICU value for that date is skipped rather than second-guessed.
sup = N.classify_icu_weights(ICU_ROWS, existing_by_date={"2026-08-10": {"value": 83.3}})
check("an athlete-logged day supersedes the ICU value",
      {r["date"]: r["tag"] for r in sup}["2026-08-10"] == "superseded")
check("a superseded reading is not counted as morning",
      "2026-08-10" not in [r["date"] for r in sup if r["tag"] == "morning"])

# The baseline must use ACCEPTED readings only. Including rejected ones would drag it
# down and let the next sweat reading through, the same self-defeating loop the RHR
# guard avoids by excluding the days under test.
drifting = [{"id": "2026-08-01", "weight": 84.0},
            {"id": "2026-08-02", "weight": 81.0},
            {"id": "2026-08-03", "weight": 81.0},
            {"id": "2026-08-04", "weight": 81.0}]
dt = {r["date"]: r["tag"] for r in N.classify_icu_weights(drifting)}
check("repeated sweat readings do not become the new baseline",
      all(dt[d] == "session_sweat" for d in ("2026-08-02", "2026-08-03", "2026-08-04")))

# Real fat loss must never be rejected: it moves ~0.2 kg a week.
slow = [{"id": f"2026-08-{d:02d}", "weight": 84.0 - i * 0.03}
        for i, d in enumerate(range(1, 15))]
check("a genuine slow downward trend is all kept",
      all(r["tag"] == "morning" for r in N.classify_icu_weights(slow)))

# And the classified output feeds rolling_weight_kg without the sweat readings.
merged = [{"type": "weight", "date": r["date"], "value": r["value"],
           "logged_at": r["date"] + "T06:00", "tag": r["tag"]}
          for r in cls if r["tag"] == "morning"]
rm = N.rolling_weight_kg(merged, on=date(2026, 8, 10), days=14)
check(f"rolling mean from the filtered set is ~83.4 (got {rm})", 83.2 <= rm <= 83.6)

# 24) In-session fuel assessed as a RATE, apart from the day's carb budget. Jamie:
#     "I can over carb in the day and under in the run and it looks fine." A day zone
#     is a budget; a session is a delivery rate that dinner cannot fix.
big_day = {"carb_g": 700, "in_session_carb_g": 60, "kcal": 4200}
sp = N.split_carbs(big_day)
check("the carb split separates in-run from the rest",
      sp["in_session_g"] == 60 and sp["out_of_session_g"] == 640)
ins = N.in_session_requirement(session_minutes=165, carbs_in_session_g=60,
                               target_g_hr=40, sport="Run")
check("a 700 g carb day still reports the run as under-fuelled",
      ins["verdict"] == "under")
check("the shortfall is in grams over the session", ins["shortfall_g"] == 50)
check("the rate is reported, not just the total", ins["g_per_hr"] == 21.8)
ok = N.in_session_requirement(session_minutes=165, carbs_in_session_g=120,
                              target_g_hr=40, sport="Run")
check("meeting the rate reads on_target", ok["verdict"] == "on_target")
check("and reports no shortfall", ok["shortfall_g"] == 0)
mid = N.in_session_requirement(session_minutes=165, carbs_in_session_g=100,
                               target_g_hr=40, alert_g_hr=34, sport="Run")
check("between the alert and the target reads acceptable", mid["verdict"] == "acceptable")
check("a session under 90 min is not assessed at all",
      N.in_session_requirement(session_minutes=45, carbs_in_session_g=0,
                               target_g_hr=40) is None)
check("a zero-length session is not assessed",
      N.in_session_requirement(session_minutes=0, carbs_in_session_g=0,
                               target_g_hr=40) is None)
check("the basis names the ramp rather than a hardcoded number",
      "ramp" in ins["basis"])
check("no in-session carbs still splits cleanly",
      N.split_carbs({"carb_g": 400})["out_of_session_g"] == 400)

# 25) The shared fuelling ramp. These live here rather than in the primitive's own tests
#     because the nutrition bot is now a consumer of them, and the failures were found
#     through it.
import os                                                              # noqa: E402
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "ironman-analysis"))
from primitives import nutrition as NU                                 # noqa: E402

check("race-day run figure is unchanged at 60", NU.RUN_TARGET_G_HR == 60)
check("training ceiling is separate and higher", NU.RUN_TRAINING_TARGET_G_HR == 90)
check("training is the DEFAULT ceiling for a prescription, not the race figure",
      NU.run_fuel_target(85) > 60)

# The bug Jamie caught: prescribing below last week.
check("never prescribes below the last long run",
      NU.run_fuel_target(57.2, last_g_hr=64.0) > 64)
check(f"his real numbers give 70 g/hr "
      f"(got {NU.run_fuel_target(57.2, last_g_hr=64.0)})",
      NU.run_fuel_target(57.2, last_g_hr=64.0) == 70)

# The second bug: ramping off the trailing average stalled it. A six-session mean creeps
# ~1 g/hr per session, so it sat at 65 for seven blocks and a raised ceiling did nothing.
a, l, seen = 57.2, 64.0, []
for _ in range(6):
    t = NU.run_fuel_target(a, last_g_hr=l)
    seen.append(t)
    a, l = (a * 5 + t) / 6, t
check(f"the ramp actually climbs rather than stalling (got {seen})",
      len(set(seen)) >= 4 and seen[-1] >= 85)
check("and reaches the training ceiling within his remaining long runs",
      max(seen) >= NU.RUN_TRAINING_TARGET_G_HR - 5)

# Guards that must survive both changes.
check("no fuelling history starts at the useful floor, not the ceiling",
      NU.run_fuel_target(None) == 40)
check("one bad-gut session does not drop the prescription, the average holds",
      NU.run_fuel_target(65, last_g_hr=30) >= 65)
check("an explicit race ceiling is still respected",
      NU.run_fuel_target(57.2, NU.RUN_TARGET_G_HR, 64.0) == 60)
check("the ride ramp still caps at the race target",
      NU.fuel_target(71.8, 90, last_g_hr=89.6) == 90)
check("the documented aggressive-ramp case is unchanged",
      NU.fuel_target(20, 70) == 45)
check("last_run_g_hr returns the most recent, not the mean",
      NU.last_run_g_hr([
          {"sport": "Run", "duration_min": 120, "nutrition_g_carb": 100,
           "date": "2026-07-01"},
          {"sport": "Run", "duration_min": 120, "nutrition_g_carb": 200,
           "date": "2026-08-01"}]) == 100.0)
check("a short run does not qualify as a fuelling data point",
      NU.last_run_g_hr([{"sport": "Run", "duration_min": 30,
                         "nutrition_g_carb": 30, "date": "2026-08-01"}]) is None)

print("\n--- fuel not yet taken is not food he can eat now ---")
# Jamie, 11 Aug 2026: "isnt today going to tell me to under eat then over eat after my run
# later?" The day carb zone is a TOTAL, so the run's 192 g sat inside it: eat it at lunch
# and the ceiling goes on the gels, and after the run those gels shrink what is left and the
# page tells him to stop eating, which is when recovery matters most.
Z = {"kcal_target": 2950, "kcal": {"low": 2800.0, "high": 3100.0, "bias": "band"},
     "protein_g": {"low": 183.0, "high": 216.0, "bias": "floor"},
     "carb_g": {"low": 466.0, "high": 466.0, "bias": "band"},
     "fat_g": {"low": 75.0, "high": 100.0, "bias": "band"},
     "fibre_g": {"low": 0, "high": 20, "bias": "ceiling"}}
T0 = {"kcal": 743.0, "protein_g": 46.0, "carb_g": 100.0, "fat_g": 17.0, "fibre_g": 13.0}
plain = N.meal_requirement(T0, Z)
held = N.meal_requirement(T0, Z, reserved={"carb_g": 192.5})
check("without a reserve the run's carbs are offered as food",
      plain["macros"]["carb_g"]["still_needed_g"] > 360)
check("with one, only FOOD carbs are asked for",
      abs(held["macros"]["carb_g"]["still_needed_g"] - 174.0) < 1.0)
check("the energy budget drops by the reserved fuel",
      plain["remaining_kcal"] - held["remaining_kcal"] == 770)
check("and the reserve is stated, not silently applied",
      held["reserved_for_session"]["carb_g"] == 192.5
      and held["reserved_for_session"]["kcal"] == 770)
check("no reserve means no claim of one", "reserved_for_session" not in plain)
# After the run the fuel is logged, the reserve goes to zero, and the budget reopens for
# recovery - which is the whole point of it being prescription MINUS what is logged.
after = dict(T0, kcal=1513.0, carb_g=292.5)
done = N.meal_requirement(after, Z, reserved={})
# The budget is IDENTICAL before and after the run, which is the property that actually
# matters and is stronger than the one I first asserted: reserving the fuel in advance means
# logging it changes nothing, so there is no cliff at the end of the session and no moment
# where the page suddenly decides he has eaten his allowance.
check("logging the fuel afterwards moves the budget by nothing",
      done["remaining_kcal"] == held["remaining_kcal"])
check("and the reserve is gone once it is real",
      "reserved_for_session" not in done)
check("a reserve never makes the day look finished when it is not",
      held["at_target"] is False)

print()
if FAILED:
    print(f"{len(FAILED)} FAILED: " + ", ".join(FAILED))
    sys.exit(1)
print("all checks passed")
