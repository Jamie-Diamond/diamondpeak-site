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
check("protein zone states its g/kg basis", "g/kg" in zz["protein_g"]["basis"])
# UPDATED for Option B (13 Aug 2026). Four mechanisms can now set the fat ceiling and
# two can set the carb zone, and they produce indistinguishable numbers, so the zone
# must name WHICH bound it. The old assertions checked for one specific wording, which
# only held while there was one mechanism.
check(f"fat zone names the bound that set it (got {zz['fat_g']['bound']})",
      zz["fat_g"]["bound"] in ("g-kg", "GI", "share", "residual")
      and zz["fat_g"]["bound"] in zz["fat_g"]["basis"])
check(f"carb zone names its bound (got {zz['carb_g']['bound']})",
      zz["carb_g"]["bound"] in ("prescription", "energy"))
check("carb zone states the demand band it came from",
      "g/kg" in zz["carb_g"]["basis"]
      and ("demand band" in zz["carb_g"]["basis"] or "energy bound" in zz["carb_g"]["basis"]))
_gkg = z(day_type="long_ride", sessions=LONGRIDE)["fat_g"]
check("the g/kg fat basis still declares which end is sourced and which is practice",
      _gkg["bound"] == "g-kg"
      and "sourced" in _gkg["basis"] and "practice" in _gkg["basis"])
check("both bands publish their share of the day's energy",
      len(zz["fat_g"]["kcal_share"]) == 2 and 0 < zz["carb_g"]["kcal_share"][1] < 1)
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

# 7b) The day must be SATISFIABLE at both ends: the floors fit under the target, and
#     the tops of the zones cover it. UPDATED for Option B - the old form asserted an
#     identity (protein floor + fat floor + carb high == target) which held only while
#     carbs were the residual and therefore took whatever was left by construction. With
#     carbs prescribed that identity is false by design, and asserting it would force the
#     residual model back. What still MUST hold is that the athlete can land inside every
#     zone at once, which is the property the old check was standing in for, and it is a
#     stronger statement: it uses the protein and fat ranges rather than their floors.
for label, zc in (("recovery", rec), ("standard", std), ("long_ride", lng),
                  ("pre-long", pre)):
    lows = (zc["protein_g"]["low"] * 4 + zc["fat_g"]["low"] * 9
            + zc["carb_g"]["low"] * 4)
    # The protein FLOOR, deliberately: protein's high is not a ceiling, so counting it as
    # absorbing capacity would let a day close only by assuming he eats past what he was
    # asked for. This is the pessimistic reading, which is the right one for a check whose
    # job is to notice a day that does not add up.
    highs = (zc["protein_g"]["low"] * 4 + zc["fat_g"]["high"] * 9
             + zc["carb_g"]["high"] * 4)
    check(f"{label} floors fit inside the target ({lows:.0f} vs {zc['kcal_target']})",
          lows <= zc["kcal_target"] + 2)
    check(f"{label} zone tops reach the target ({highs:.0f} vs {zc['kcal_target']})",
          highs >= zc["kcal_target"] - 2)
    check(f"{label} carb high is not below its safety floor",
          zc["carb_g"]["high"] >= zc["carb_g"]["low"] - 0.5)
    # And the day may not strand a large share of its energy above every zone without
    # saying so. That was v0.2's failure - 824 kcal unallocated on a long ride day with
    # nothing to absorb it and no warning either.
    unallocated = zc["kcal_target"] - highs
    check(f"{label} strands no energy silently ({unallocated:.0f} kcal)",
          unallocated <= N.DAY_UNALLOCATED_WARN_FRACTION * zc["kcal_target"]
          or any("disagree" in w for w in zc["warnings"]))

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
# UPDATED for Option B. The old check compared two RECOVERY days, one before a long
# ride, and asserted the carb floor was lifted. Under the demand bands a rest day before
# a long session is prescribed 8-10 g/kg - about 2,700 kcal of carbohydrate against a
# 2,491 kcal maintenance - so the lift is not physically available on that day and the
# model reports the disagreement instead of pretending. The lift is asserted on a day
# that HAS the energy for it, which is where the behaviour is real.
check("a rest day cannot hold the pre-long prescription, and says so",
      flip["carb_g"]["bound"] == "energy"
      and any("disagree" in m for m in flip["modifiers"]))
check("and the shortfall is a modifier, not a warning: the inputs are fine",
      not any("disagree" in w for w in flip["warnings"]))
lift_plain = z(day_type="standard", sessions=RIDE2H)
lift_pre = z(day_type="standard", sessions=RIDE2H, tomorrow_type="long_ride")
check(f"carbs go to the upper half before a long session when the day can hold it "
      f"(got {lift_pre['carb_g']['low']:.0f} vs {lift_plain['carb_g']['low']:.0f} g)",
      lift_pre["carb_g"]["low"] > lift_plain["carb_g"]["low"] * 1.5)

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

print("\n--- a band has to be a band ---")
# Carbs came out 466.5-466.5 on a long-run day: the derived low and the high both clamp
# against the carb floor, so when the protein and fat highs consume the energy they meet at a
# point. A zero-width band can only read "in zone" or "over" - there is nowhere to land -
# which makes a landing zone behave like the point target this model exists to avoid.
check("a collapsed band is given width", N.widen_band(466.5, 466.5)[1] > 466.5)
check("and it grows UPWARD, never below the floor",
      N.widen_band(466.5, 466.5)[0] == 466.5)
check("a band that is already wide is untouched",
      N.widen_band(278.2, 367.8) == (278.2, 367.8))
check("a nearly-collapsed band is widened too", N.widen_band(75.0, 76.0)[1] == 85.0)
# The absolute minimum must not swamp a small band: 10 g would turn 8 g into 8-18 g.
check("the rule is proportional at small values", N.widen_band(8.0, 8.0)[1] == 10.0)
check("a zero band stays zero rather than inventing a target",
      N.widen_band(0.0, 0.0) == (0.0, 0.0))
# And it must reach every band, because it is applied where zones are BUILT.
zb = N._zone(400.0, 400.0, N.BIAS_BAND, "test")
check("every band zone gets it, since _zone is the one construction point",
      zb["high"] > zb["low"])
check("and the adjustment is declared, not silent",
      zb.get("widened_from_high") == 400.0)
check("a FLOOR is one-sided on purpose and left alone",
      N._zone(180.0, 180.0, N.BIAS_FLOOR, "test")["high"] == 180.0)
check("so is a CEILING",
      N._zone(0.0, 20.0, N.BIAS_CEILING, "test")["high"] == 20.0)

print("\n--- fuel for the work required: the demand ahead (Option B, 13 Aug 2026) ---")
# The tier classifier, one trigger at a time. These are the thresholds that REPLACE
# classify_day's 240-minute ride cliff for fuelling purposes, and the cliff is exactly
# what they exist to fix: a 230-minute ride was fuelled as a `standard` day.
d = N.classify_demand
check("a 150 min ride is LONG today",
      d(today_sessions=[{"type": "Ride", "moving_time": 150 * 60}])["band"]
      == N.BAND_LONG_TODAY)
check("a 149 min ride is not",
      d(today_sessions=[{"type": "Ride", "moving_time": 149 * 60}])["band"]
      == N.BAND_EASY_AHEAD)
check("THE CLIFF: a 230 min ride is LONG, where classify_day still says standard",
      d(today_sessions=[{"type": "Ride", "moving_time": 230 * 60}])["tier"] == N.DEMAND_LONG
      and N.classify_day([{"type": "Ride", "moving_time": 230 * 60}]) == "standard")
check("a 90 min run is LONG today",
      d(today_sessions=[{"type": "Run", "moving_time": 90 * 60}])["band"]
      == N.BAND_LONG_TODAY)
check("planned load alone can make a session LONG, with no duration at all",
      d(today_sessions=[{"type": "Ride", "icu_training_load": 160}])["band"]
      == N.BAND_LONG_TODAY)
check("the coach's own load key is read too",
      d(today_sessions=[{"type": "Run", "load_target": 150}])["band"] == N.BAND_LONG_TODAY)
check("tomorrow's long session is LONG AHEAD, a different band from today's",
      d(tomorrow_sessions=[{"type": "Ride", "moving_time": 240 * 60}])["band"]
      == N.BAND_LONG_AHEAD)
check("a 60 min threshold ride is KEY on its name alone",
      d(today_sessions=[{"type": "Ride", "moving_time": 3600,
                         "name": "Build ride (3x20 sweet spot)"}])["tier"] == N.DEMAND_KEY)
check("so is a VO2 session tomorrow",
      d(tomorrow_sessions=[{"type": "Run", "moving_time": 3600,
                            "name": "VO2 intervals"}])["band"] == N.BAND_KEY_AHEAD)
check("and a swim CSS test, which the coach's classifier has no bucket for",
      d(today_sessions=[{"type": "Swim", "moving_time": 3600, "name": "CSS test",
                         "session_type": "swim"}])["tier"] == N.DEMAND_KEY)
check("the coach's own quality verdict is honoured when it is supplied",
      d(today_sessions=[{"type": "Ride", "moving_time": 3600, "name": "Wednesday ride",
                         "session_type": "bike_vo2"}])["tier"] == N.DEMAND_KEY)
check("and its EASY verdict overrides prose that merely mentions intervals",
      d(today_sessions=[{"type": "Ride", "moving_time": 3600, "name": "Endurance ride",
                         "session_type": "bike_z2",
                         "description": "steady, no intervals today"}])["tier"]
      == N.DEMAND_EASY)
check("LONG outranks KEY: the day is fuelled for the long session",
      d(today_sessions=[{"type": "Ride", "moving_time": 200 * 60, "name": "Long ride"},
                        {"type": "Run", "moving_time": 1800,
                         "name": "Tempo run"}])["band"] == N.BAND_LONG_TODAY)
check("a known-empty calendar is a REST window",
      d(today_sessions=[], tomorrow_sessions=[], calendar_known=True)["band"]
      == N.BAND_REST_AHEAD)
# The fallback. Guessing rest would UNDER-fuel a day that may have had a session in it,
# and under-fuelling is the costlier error - the same asymmetry classify_from_day_rules
# applies to the day type.
nc = d(calendar_known=False)
check("no calendar data assumes EASY, never REST", nc["band"] == N.BAND_EASY_AHEAD)
check("and marks itself a guess", nc["confidence"] == "low_confidence" and nc["note"])
check("the guess reaches the athlete as a warning he can correct",
      any("demand could not be classified" in w
          for w in z(day_type="recovery")["warnings"]))
check("a legacy caller passing only tomorrow_type still gets the long band",
      d(tomorrow_type="long_ride")["band"] == N.BAND_LONG_AHEAD)
check("and one passing only day_type still gets long TODAY",
      d(day_type="long_ride")["band"] == N.BAND_LONG_TODAY)
# REGRESSION: the fallback token match is word-bounded, and the bare "race" token reads
# the session NAME only. An Endurance ride whose aim said "building toward race day" came
# back KEY, which suppresses the deficit on an easy day for no reason at all.
check("coaching prose about race day does not make an easy ride a key session",
      not N.is_key_session({"type": "Ride", "name": "Endurance ride",
                            "description": "steady Z2, building toward race day"}))
check("nor does a description that NEGATES intensity",
      not N.is_key_session({"type": "Swim", "name": "Easy swim",
                            "description": "recovery float, no racing this block"}))
check("but a session named as a race still counts",
      N.is_key_session({"type": "Ride", "name": "Sunday race"}))
check("and a compound race form counts in the aim text too",
      N.is_key_session({"type": "Ride", "name": "Wednesday ride",
                        "description": "3x15 at race pace"}))
check("the driving sessions are named, not just counted",
      d(tomorrow_sessions=[{"type": "Ride", "moving_time": 240 * 60,
                            "name": "Long Z2 ride"}])["sessions"]
      == ["Long Z2 ride (tomorrow)"])

print("\n--- the deficit is ONE rule: EASY or REST ahead ---")
SESS_EASY = [{"type": "Ride", "moving_time": 3600, "name": "Easy spin",
              "session_type": "bike_z2", "calories": 600, "average_watts": 150}]
SESS_KEY = [{"type": "Ride", "moving_time": 3600, "name": "Threshold 3x12",
             "session_type": "bike_threshold"}]


def dz(**kw):
    kw.setdefault("day_type", "standard")
    kw.setdefault("sessions", SESS_EASY)
    kw.setdefault("calendar_known", True)
    kw.setdefault("deficit_enabled", True)
    return z(**kw)


easy_ahead = dz()
check(f"EASY ahead allows a deficit (got {easy_ahead['deficit_applied_kcal']})",
      easy_ahead["deficit_applied_kcal"] > 0)
key_tomorrow = dz(tomorrow_sessions=SESS_KEY)
check("a threshold session TOMORROW suppresses it - the gap the old gate list had",
      key_tomorrow["deficit_applied_kcal"] == 0)
check("and the reason is stated, not silent",
      any("quality session" in w for w in key_tomorrow["warnings"]))
check("a threshold session LATER TODAY suppresses it too",
      dz(sessions=SESS_EASY + SESS_KEY)["deficit_applied_kcal"] == 0)
check("a long session today suppresses it",
      dz(sessions=[{"type": "Ride", "moving_time": 200 * 60, "calories": 2600,
                    "average_watts": 200}])["deficit_applied_kcal"] == 0)
check("the RHR guard still suppresses it on an otherwise easy day",
      dz(rhr_guard_active=True)["deficit_applied_kcal"] == 0)
check("and says why",
      any("resting HR" in w for w in dz(rhr_guard_active=True)["warnings"]))
rest_win = dz(day_type="recovery", sessions=[], calendar_known=True, tomorrow_sessions=[])
check(f"a rest window is never suppressed by the tier rule (band "
      f"{rest_win['demand_ahead']['band']})",
      rest_win["demand_ahead"]["band"] == N.BAND_REST_AHEAD
      and not any("suppressed" in w for w in rest_win["warnings"]))
check("every suppressed day carries its reason when the deficit is enabled",
      all(any("suppressed" in w for w in zc["warnings"])
          for zc in (key_tomorrow, dz(rhr_guard_active=True),
                     dz(days_to_race=2), dz(tomorrow_type="long_ride"))))
check("race week is a KEY window by construction, even with an easy calendar",
      dz(days_to_race=2)["demand_ahead"]["tier"] == N.DEMAND_KEY
      and dz(days_to_race=2)["deficit_applied_kcal"] == 0)

print("\n--- the four published days, reconstructed (10-13 Aug 2026) ---")
# Inputs are reconstructed from public/nutrition-jamie.json: the maintenance figures in
# the file fix each day's net session energy exactly (net = maintenance - 1.35 x RMR),
# and the day types and in-session carb totals fix the sessions' shape.
D11, D12, D13 = date(2026, 8, 11), date(2026, 8, 12), date(2026, 8, 13)


def _rmr(d):
    return N.mifflin_st_jeor(W, 1.86, DOB, "M", on=d)


def _gross(net, mins, d):
    """Device calories that net to `net` after the resting subtraction."""
    return round(net + (mins / 60.0) * _rmr(d) / 24.0)


RUN_11AUG = [{"type": "Run", "moving_time": 165 * 60, "icu_training_load": 260,
              "name": "Long run 33km with tempo finish",
              "calories": _gross(2985, 165, D11)}]
RIDE_13AUG = [{"type": "Ride", "moving_time": 230 * 60, "icu_training_load": 230,
               "name": "Long endurance ride", "average_watts": 205,
               "calories": _gross(2727, 230, D13)}]
SPIN_12AUG = [{"type": "Ride", "moving_time": 45 * 60, "name": "Recovery spin",
               "session_type": "bike_z2", "average_watts": 120,
               "calories": _gross(534, 45, D12)}]

g11 = N.zones(day_type="long_run", rolling_weight=W, rmr=_rmr(D11), sessions=RUN_11AUG,
              calendar_known=True, deficit_enabled=True)
check(f"(i) 11 Aug classifies LONG today (got {g11['demand_ahead']['band']})",
      g11["demand_ahead"]["band"] == N.BAND_LONG_TODAY)
check(f"(i) and its maintenance still reconstructs the published 5476 "
      f"(got {g11['kcal_maintenance']})", abs(g11["kcal_maintenance"] - 5476) <= 2)
lo11, hi11 = g11["carb_g"]["low"] / W, g11["carb_g"]["high"] / W
check(f"(i) carbs land inside 8-12 g/kg at 83.3 kg (got {lo11:.1f}-{hi11:.1f})",
      7.95 <= lo11 and hi11 <= 12.05 and hi11 - lo11 > 1)
check("(i) no deficit on the day of the long run", g11["deficit_applied_kcal"] == 0)

g13 = N.zones(day_type="standard", rolling_weight=W, rmr=_rmr(D13), sessions=RIDE_13AUG,
              calendar_known=True, deficit_enabled=True)
check(f"(ii) 13 Aug's 230 min ride now classifies LONG today, not standard-with-a-cliff "
      f"(got {g13['demand_ahead']['band']}, day_type still {g13['day_type']})",
      g13["demand_ahead"]["band"] == N.BAND_LONG_TODAY and g13["day_type"] == "standard")
check(f"(ii) carbs land 8-11.5 g/kg rather than the published 10.5-11.5 against a 5-6 "
      f"reference (got {g13['carb_g']['low'] / W:.1f}-{g13['carb_g']['high'] / W:.1f})",
      7.95 <= g13["carb_g"]["low"] / W and g13["carb_g"]["high"] / W <= 12.05)
check("(ii) and the day no longer warns about its own carb targets",
      not any("reference band" in w for w in g13["warnings"]))
check("(ii) fibre is a ceiling with an after-session floor, which the cliff had missed",
      g13["fibre_g"]["bias"] == N.BIAS_CEILING and "after_session" in g13["fibre_g"])

# (iii) The small day is UNTOUCHED by the share term: 25% of a 2,842 kcal target is 79 g,
# under the 100 g that 1.2 g/kg already allows, so the residual still binds at 90 g.
g12 = N.zones(day_type="recovery", rolling_weight=W, rmr=_rmr(D12), sessions=SPIN_12AUG,
              tomorrow_sessions=[{"type": "Ride", "moving_time": 3600, "name": "Easy spin",
                                  "session_type": "bike_z2"}],
              yesterday_type="long_run", calendar_known=True, deficit_enabled=True)
check(f"(iii) 12 Aug keeps a deficit with a long run behind it and an easy day ahead "
      f"(got {g12['deficit_applied_kcal']})", g12["deficit_applied_kcal"] > 0)
check(f"(iii) fat's ceiling stays at 100 or below on a {g12['kcal_target']} kcal day "
      f"(got {g12['fat_g']['high']}, bound by {g12['fat_g']['bound']})",
      g12["fat_g"]["high"] <= N.FAT_CEILING_G_PER_KG * W + 0.5
      and g12["fat_g"]["bound"] in ("residual", "g-kg"))
check("(iii) the share term cannot lift a small day's fat ceiling",
      N.FAT_SHARE_TARGET * g12["kcal_target"] / 9 < N.FAT_CEILING_G_PER_KG * W)
# The same day against the REAL calendar, where the 230-minute ride is tomorrow: it is
# now visible as a long session, so 12 Aug loses its deficit entirely. Worth pinning,
# because it is the behaviour change with the largest effect on the weight programme.
g12_real = N.zones(day_type="recovery", rolling_weight=W, rmr=_rmr(D12),
                   sessions=SPIN_12AUG, tomorrow_sessions=RIDE_13AUG,
                   yesterday_type="long_run", calendar_known=True, deficit_enabled=True)
check("(iii) with the 230 min ride visible tomorrow, 12 Aug's deficit goes entirely",
      g12_real["deficit_applied_kcal"] == 0
      and any("glycogen" in w for w in g12_real["warnings"]))

# (iv) A big EASY day is where the share term earns its keep: holding fat at 100 g there
# strands over a thousand calories that carbs have declined to take.
g_big = N.zones(day_type="standard", rolling_weight=W, rmr=_rmr(D13), calendar_known=True,
                sessions=[{"type": "Ride", "moving_time": 120 * 60, "name": "Easy spin",
                           "session_type": "bike_z2", "average_watts": 205,
                           "calories": _gross(2727, 120, D13)}],
                tomorrow_sessions=[{"type": "Ride", "moving_time": 3600,
                                    "name": "Easy spin", "session_type": "bike_z2"}])
check(f"(iv) a ~5200 kcal EASY-ahead day gets a ~144 g fat ceiling from the share term "
      f"(got {g_big['fat_g']['high']}, bound by {g_big['fat_g']['bound']})",
      g_big["fat_g"]["bound"] == "share" and 140 <= g_big["fat_g"]["high"] <= 150)
check("(iv) which is a quarter of the day's energy, as the constant says",
      abs(g_big["fat_g"]["kcal_share"][1] - N.FAT_SHARE_TARGET) < 0.01)
check("(iv) and an easy day carrying that much training energy is still called out",
      any("check activity calories" in w for w in g_big["warnings"]))

print("\n--- fat: which bound bound, and the GI cap on hard days ---")
check("a quality session ahead caps fat at 90 g even though pre_long never fired",
      dz(tomorrow_sessions=SESS_KEY)["fat_g"]["high"] <= N.FAT_CEILING_PRE_LONG_G
      and dz(tomorrow_sessions=SESS_KEY)["fat_g"]["bound"] in ("GI", "residual"))
check("an ordinary easy day is not GI-capped",
      easy_ahead["fat_g"]["bound"] in ("g-kg", "share", "residual"))
check("the fat floor is never subject to the share argument",
      all(abs(zc["fat_g"]["low"] - N.FAT_FLOOR_G_PER_KG * W) < 0.6
          for zc in (g11, g12, g13, g_big)))
check("fibre flips to a 30 g ceiling with a quality session ahead, looser than pre-long",
      dz(tomorrow_sessions=SESS_KEY)["fibre_g"]["high"] == N.FIBRE_CEILING_KEY_AHEAD_G
      and dz(tomorrow_sessions=SESS_KEY)["fibre_g"]["bias"] == N.BIAS_CEILING)
check("and floors are untouched on easy and rest windows",
      easy_ahead["fibre_g"]["bias"] == N.BIAS_FLOOR)
# The ceiling is PHASED when the hard session is today, whichever kind it is: the residue
# reason expires once the work is done, and a ceiling that runs all day tells him off for
# eating his fibre afterwards. Tomorrow's session gets no phase - the ceiling applies to
# the whole day, because the session is not in it.
key_today = dz(sessions=SESS_EASY + SESS_KEY)
check("a quality session TODAY phases the fibre ceiling, as a long one does",
      "after_session" in key_today["fibre_g"]
      and key_today["fibre_g"]["after_session"]["bias"] == N.BIAS_FLOOR)
check("a quality session TOMORROW does not: the ceiling holds all day",
      "after_session" not in dz(tomorrow_sessions=SESS_KEY)["fibre_g"])

print("\n--- what the page is given ---")
for key in ("demand_ahead", "carb_basis", "fat_basis"):
    check(f"zones() exposes {key}", key in g13)
check("demand_ahead names the tier and the sessions behind it",
      g13["demand_ahead"]["tier"] == N.DEMAND_LONG
      and g13["demand_ahead"]["sessions"] == ["Long endurance ride"])
check("the bases are the same strings the zones carry, not a second rendering",
      g13["carb_basis"] == g13["carb_g"]["basis"]
      and g13["fat_basis"] == g13["fat_g"]["basis"])
check("carbs and fat both publish a kcal_share pair",
      all(len(g13[k]["kcal_share"]) == 2 for k in ("carb_g", "fat_g")))

print()
if FAILED:
    print(f"{len(FAILED)} FAILED: " + ", ".join(FAILED))
    sys.exit(1)
print("all checks passed")
