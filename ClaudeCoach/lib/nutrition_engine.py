#!/usr/bin/env python3
"""nutrition_engine.py - daily landing zones, projection and guardrails. PURE.

Spec v0.2 (10 Aug 2026). No file I/O, no network: callers hand it activities,
wellness rows and the day's totals, it hands back zones and flags. That keeps it
testable offline and lets the bot and the Peak publish step share one brain, the
same split lib/engine.py made for the coach. nutrition_store.py owns the disk.

ZONES, NOT POINT TARGETS (v0.2 §3.2)
Every macro is a range plus a `bias` that decides which direction is worth a
warning: `floor` warns low only, `ceiling` warns high only, `band` warns both.
Exceeding a protein floor is not an event and must not render as one. This
generalises what v0.1 carried as a single fibre_is_ceiling boolean.

WHY THE RESTING SUBTRACTION IS NOT OPTIONAL
  maintenance = base_tdee + net_session_kcal
  net_session_kcal = Σ(activity_kcal) − Σ(activity_hours) × rmr / 24
Device-reported activity calories already contain the resting energy the athlete
would have burned anyway during those hours. Adding raw activity kcal to a
full-day base double-counts by roughly 75-80 kcal per training hour, which at
12-16 hrs/week is a systematic 130-150 kcal/day overstatement, enough to turn an
intended deficit into maintenance without anyone noticing.

Input error this inherits: intervals.icu `calories` is kJ-derived and sound for
power-metered rides, but an HR-based estimate for runs and swims. `kcal_confidence`
reports which, so the zone is not read as exact.

Known and NOT modelled: training hours displace NEAT as well as resting energy, but
only RMR is subtracted. The 1.35 multiplier covers a non-training day's NEAT, and an
athlete on a bike for four hours is not also doing four hours of ordinary pottering.
That is roughly 20 kcal per training hour, about 80 on a long ride, so it points the
same way as the error above and is second-order beside it. Left as a documented
approximation rather than a third estimated correction stacked on two others; the
closed loop in deficit_correction is the answer to accumulated estimate error, not
more estimating.

THE DEFICIT IS PROPORTIONAL AND SELF-LIMITING (Jamie's call, 10 Aug 2026)
  deficit = min(deficit_pct × maintenance, headroom)
  headroom = maintenance − (protein_floor + fat_floor + carb_band_minimum)
A FLAT deficit does not land equally. The floors are near-fixed costs (165 g
protein + 75 g fat = 1,335 kcal whatever the day), which is 54% of a 2,487 kcal
recovery day but only 34% of a 3,933 kcal standard day, so a flat 350 crowds small
days hard and large days barely - on a recovery day it left the floors and the
carb band ~200 kcal short of satisfiable. Proportional tracks the same crowding
mechanism §3.3 identifies for fat, and the headroom cap means it can never breach
a floor. At 10% that is ~153 kcal on a recovery day, ~393 on a standard day, zero
on long days, projecting ~1.3 kg over 40 days.

Known tension, recorded rather than resolved: proportional puts the largest
absolute cut on the largest non-long training day, which is the opposite of "fuel
the work required". The orthodox inverse (bigger cut on easy days) is arithmetically
impossible here because easy days have no headroom, so proportional wins by
default, not on merit.

Two limits hold regardless of deficit_pct, as safety ceilings rather than
preferences:
  1. never on long_run or long_ride days
  2. suppressed entirely while the RHR guard is active - a deficit stacked on an
     unresolved illness signal is the failure mode §10.2 exists to prevent, and it
     already happened once in early August
The low-energy-availability flag (§10.1) fires whether or not the deficit is
deliberate.

FAT IS BIDIRECTIONAL AND ITS CEILING IS DERIVED, NOT LITERATURE (v0.2 §3.3)
Two mechanisms bind on opposite days. Calorie crowding binds on low-energy days:
once protein and carbs are paid for, what remains for fat on a recovery day is
tight. Gastric emptying binds on long days, where there is plenty of calorie room
but fat slows absorption of the carbs the session needs. So the ceiling is computed
from residual calorie space and then tightened on pre-long and race days. The floor
(0.9 g/kg) is well sourced; the ceilings are reasoning, and the returned zone says
so via `basis`.

G/KG BASIS DIFFERS FROM THE FUELLING ENGINE, DELIBERATELY
plan_tools.py race-fuelling computes carbs/kg off profile `race_weight_kg` (79.0),
because it plans for the body that will start the race. This module uses the
rolling 7-day mean of MORNING weights, because it targets the body the athlete has
today. The two will quote different g/kg for the same day. Correct for each
purpose; do not "fix" it by making them agree.

The morning-only filter is structural, not cosmetic. Jamie weighs repeatedly on
long-ride days to measure sweat rate and those readings sit 2-3 kg low. With the
deficit driven off rolling weight, one post-ride reading in the mean would read as
progress that did not happen. See rolling_weight_kg.

FORWARD-LOOKING MODIFIERS NEED A +1 DAY LOOKAHEAD (v0.2 §4.0)
A rest day before a long ride is a LOW-fibre day even though its own day type says
high: same day type, opposite bias, decided purely by what is on tomorrow's
calendar. So callers must pass tomorrow's classification, not just today's
activities. An empty calendar is NOT a rest day - fall back to the athlete's
typical week (config/athletes.json day_rules) and mark the zone low_confidence,
because guessing rest when a long ride is planned inverts the fibre advice.

PROJECTION REPLACES PERCENTAGE PACING (v0.2 §7, reversing v0.1)
v0.1 flagged a macro when its consumed-percentage ran ahead of the day's
calorie-percentage. That is deleted. It needed a blanket suppression on long days
and on any day containing in-session fuel (carbs legitimately spike mid-session
while fat stays flat), and a rule needing a carve-out that large was the tell. It
also could not see the failure that matters most: a macro that cannot REACH its
floor. Flags now fire only when the projected landing range cannot reach the zone.
Straddling a boundary is not a flag, and no long-day special case is needed.

`plausible_remaining` is the load-bearing unknown. v1 asks the athlete their plan
at breakfast rather than inferring it; with no plan supplied, only already-breached
ceilings can flag, and the projection is reported as open-ended.

SODIUM HAS NO TARGET, ON PURPOSE
Jamie declined the overdue sweat test - "anecdotally I'm a salty sweater, that's
what we have to work with". Self-reported saltiness correlates only weakly with
measured sweat [Na+], so this does NOT raise the fuelling engine's 950 mg/L
default on the anecdote. It reports a 950-1500 mg/L band tagged `assumed`.
In-session sodium already exists upstream as `nutrition_mg_sodium`; only DIETARY
sodium is new. In-session carb g/hr targets live in
ironman-analysis/primitives/nutrition.py and are read, never restated here.
"""

from datetime import date, datetime, timedelta

# --- constants ---------------------------------------------------------------

KCAL_PER_KG_FAT = 7700
NEAT_TEF_MULTIPLIER = 1.35

# Protein is a FLOOR with headroom, not the flat 180 of v0.1. Noting the cost of
# that change: v0.1 justified a flat figure as "the one target that should not move
# with training load", and the Block view charted adherence to it. v0.2 makes it a
# range and adds a post-long-ride modifier, so that chart loses its premise.
PROTEIN_FLOOR_G = 165
PROTEIN_HIGH_G = 200
PROTEIN_POST_LONG_FLOOR_G = 180        # yesterday was a long ride

FAT_FLOOR_G_PER_KG = 0.9               # well sourced. v0.1 used a flat 80 g
FAT_CEILING_PRE_LONG_G = 90            # GI, not energetics
FAT_CEILING_RACE_WEEK_G = 80

KCAL_BAND_PCT = 0.05                   # calories are a band, +/- 5%

DEFICIT_PCT_DEFAULT = 0.10
PACE_LEGACY_REMOVED = True             # v0.1 percentage pacing deleted, see §7

# The deficit must leave the fat zone some WIDTH. A first cut capped the deficit at
# the full headroom above the floors, which drove the target down to exactly the sum
# of the floors and made residual fat land on its floor: the zone collapsed to
# 75-75 on recovery days and on standard days with HR-estimated sessions. A
# zero-width zone is a point target, which is the thing v0.2 3.2 exists to remove,
# and every deviation from it flags. So the cap reserves this much fat range.
FAT_ZONE_MIN_WIDTH_G = 15

# Adaptive correction (see deficit_correction). Bounded so a noisy trend can nudge
# intake but never swing it, and gated on enough clean morning weights to mean
# anything: single readings carry ~+/-2.5 kg of 95% uncertainty, so a 14-day window
# is the minimum that can show a ~0.2 kg/week trend at all.
CORRECTION_MIN_DAYS = 14
CORRECTION_MAX_KCAL = 300

SWEAT_NA_ASSUMED_LOW = 950
SWEAT_NA_ASSUMED_HIGH = 1500

MICRO_WATCH = ("iron", "vitamin_d", "b12", "magnesium")

# Collagen and gelatin: ~15 g of protein with no tryptophan and little leucine.
# Counting it would show target met while real protein intake sat 15 g short every
# day. nutrition_store imports THIS tuple rather than keeping its own copy.
NON_COUNTING_PROTEIN_SOURCES = ("collagen", "gelatin", "gelatine")

BIAS_FLOOR, BIAS_CEILING, BIAS_BAND = "floor", "ceiling", "band"

# carb_g_per_kg drives the carb zone directly in v0.2 (it was a cross-check in
# v0.1, where carbs were a remainder). fibre_g is (low, high).
DAY_TYPES = {
    "recovery":  {"carb_g_per_kg": (3, 4), "fibre_g": (40, 45)},
    "standard":  {"carb_g_per_kg": (5, 6), "fibre_g": (30, 35)},
    "long_run":  {"carb_g_per_kg": (7, 8), "fibre_g": (0, 20)},
    "long_ride": {"carb_g_per_kg": (8, 9), "fibre_g": (0, 20)},
}
LONG_DAY_TYPES = ("long_run", "long_ride")

CARB_LOAD_G_PER_KG = (10, 12)
LOW_RESIDUE_CEILING_G = 20
RACE_WEEK_FIBRE_G = (10, 15)

RIDE_SPORTS = ("Ride", "VirtualRide", "GravelRide", "MountainBikeRide")
RUN_SPORTS = ("Run", "VirtualRun", "TrailRun")


def _as_date(v):
    """Accept date, datetime or an ISO-ish string; None on anything else. ICU
    returns both `2026-08-10` and `2026-08-10T06:03:00` depending on the endpoint."""
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    if isinstance(v, str) and len(v) >= 10:
        try:
            return date.fromisoformat(v[:10])
        except ValueError:
            return None
    return None


def _zone(low, high, bias, basis="", confidence="normal") -> dict:
    """One macro's landing zone. `bias` decides warning direction, `basis` records
    whether the numbers are sourced or reasoned so the UI can be honest about it."""
    return {"low": round(low, 1), "high": round(high, 1), "bias": bias,
            "basis": basis, "confidence": confidence}


# --- anthropometrics --------------------------------------------------------

def age_years(dob, on: date | None = None) -> int:
    """Whole years, birthday-aware. Truncating a fractional age would shift RMR."""
    dob = _as_date(dob)
    on = on or date.today()
    if not dob:
        raise ValueError("dob is required to compute age")
    return on.year - dob.year - ((on.month, on.day) < (dob.month, dob.day))


def mifflin_st_jeor(weight_kg: float, height_m: float, dob, sex: str = "M",
                    on: date | None = None) -> float:
    """RMR kcal/day. Male: 10*kg + 6.25*cm - 5*age + 5 (female: -161)."""
    if not weight_kg or not height_m:
        raise ValueError("weight_kg and height_m are required")
    base = 10 * weight_kg + 6.25 * (height_m * 100) - 5 * age_years(dob, on)
    return base + (5 if (sex or "M").upper().startswith("M") else -161)


def base_tdee(rmr: float, multiplier: float = NEAT_TEF_MULTIPLIER) -> float:
    """Non-training TDEE: RMR plus NEAT and the thermic effect of food. Training
    energy is added separately by net_session_kcal, never folded in here."""
    return rmr * multiplier


def rolling_weight_kg(measurements, on: date | None = None, days: int = 7):
    """Mean of MORNING weights over the trailing window. None if none available.

    Only the first weight of each day after 04:00 local counts; anything later is a
    sweat-rate weigh-in. Pre-tagged `session_sweat` entries are excluded outright.
    Never decide off a single reading: individual scale readings carry roughly
    +/-2.5 kg of 95% uncertainty, which is half the gap being chased."""
    on = on or date.today()
    cutoff = on - timedelta(days=days - 1)
    by_day = {}
    for m in measurements or []:
        if (m.get("type") or "weight") != "weight":
            continue
        if (m.get("tag") or "") == "session_sweat":
            continue
        d = _as_date(m.get("date") or m.get("logged_at"))
        if not d or d < cutoff or d > on:
            continue
        val = m.get("value")
        if val is None:
            continue
        stamp = m.get("logged_at") or m.get("date") or ""
        hhmm = stamp[11:16] if len(stamp) >= 16 else ""
        if hhmm and hhmm < "04:00":
            continue
        prev = by_day.get(d)
        if prev is None or (hhmm and hhmm < prev[0]):
            by_day[d] = (hhmm or "99:99", float(val))
    if not by_day:
        return None
    vals = [v for _, v in by_day.values()]
    return round(sum(vals) / len(vals), 2)


# --- day classification -----------------------------------------------------

def _duration_min(a) -> float:
    for key in ("moving_time", "elapsed_time", "duration"):
        v = a.get(key)
        if v:
            return float(v) / 60.0 if float(v) > 600 else float(v)
    return float(a.get("duration_min") or 0)


def _sport(a) -> str:
    return (a.get("type") or a.get("sport") or a.get("category") or "").strip()


def classify_day(sessions) -> str:
    """Day type from the day's sessions, planned or completed.

    The longest single session of each discipline decides it, not the daily total:
    two 90-minute rides is a standard day, one 4-hour ride is not. Ride is tested
    before run so a brick reads long_ride, which is the correct fuelling call."""
    ride_max = max([_duration_min(s) for s in sessions or []
                    if _sport(s) in RIDE_SPORTS] or [0])
    run_max = max([_duration_min(s) for s in sessions or []
                   if _sport(s) in RUN_SPORTS] or [0])
    total = sum(_duration_min(s) for s in sessions or [])
    if ride_max >= 240:
        return "long_ride"
    if run_max >= 120:
        return "long_run"
    return "standard" if total >= 60 else "recovery"


def classify_from_day_rules(day, day_rules: dict) -> tuple[str, str]:
    """Fallback when the calendar is EMPTY. Returns (day_type, confidence).

    An empty calendar is not evidence of a rest day, and treating it as one inverts
    the fibre advice on the day before a long ride. So fall back to the athlete's
    stated typical week (config/athletes.json day_rules) and mark the result
    low_confidence so the UI can say it is a guess.

    Deliberately conservative: the typical week gives no DURATION, so a scheduled
    bike day reads `standard`, never long_ride. Guessing long would relax fibre and
    inflate carbs off nothing but a habit."""
    d = _as_date(day)
    if not d or not day_rules:
        return "recovery", "low_confidence"
    dow = d.strftime("%a")
    scheduled = any(dow in (day_rules.get(k) or [])
                    for k in ("swim_days", "bike_days", "run_days"))
    return ("standard" if scheduled else "recovery"), "low_confidence"


def net_session_kcal(sessions, rmr: float) -> tuple[float, str]:
    """Training energy above resting, plus a confidence label.

    'measured' only when every session contributing energy is power-metered;
    otherwise 'estimated', because ICU derives run and swim calories from heart
    rate at roughly +/-15-20%."""
    gross = hours = 0.0
    all_powered = True
    for s in sessions or []:
        kcal = s.get("calories") or s.get("kcal") or 0
        mins = _duration_min(s)
        if not kcal and not mins:
            continue
        gross += float(kcal or 0)
        hours += mins / 60.0
        if kcal and not s.get("average_watts"):
            all_powered = False
    net = gross - (hours * rmr / 24.0)
    return max(0.0, round(net, 1)), ("measured" if all_powered and gross else "estimated")


# --- the zone computation ---------------------------------------------------

def zones(*, day_type: str, rolling_weight: float, rmr: float,
          sessions=None, tomorrow_type: str | None = None,
          yesterday_type: str | None = None, days_to_race: int | None = None,
          deficit_enabled: bool = False, deficit_pct: float = DEFICIT_PCT_DEFAULT,
          rhr_guard_active: bool = False, day_confidence: str = "normal",
          correction_kcal: float = 0.0,
          tdee_multiplier: float = NEAT_TEF_MULTIPLIER) -> dict:
    """The day's landing zones, the modifiers applied, and any warning the inputs
    justify.

    Order matters: protein and carbs are priced first, the deficit is then capped by
    what remains above those floors, and fat's ceiling is whatever residual calorie
    space is left. Computing fat before the deficit would let a deficit eat into the
    carbs that fuel the session instead of into fat."""
    if day_type not in DAY_TYPES:
        raise ValueError(f"unknown day_type {day_type!r}")
    if not rolling_weight:
        raise ValueError("rolling_weight is required; do not fall back to a single reading")

    net, kcal_confidence = net_session_kcal(sessions, rmr)
    maintenance = base_tdee(rmr, tdee_multiplier) + net
    modifiers, warnings = [], []
    is_long = day_type in LONG_DAY_TYPES
    pre_long = tomorrow_type in LONG_DAY_TYPES
    race_week = days_to_race is not None and 0 <= days_to_race <= 3

    # 1. protein: a floor with headroom, raised the day after a long ride
    p_low, p_high = PROTEIN_FLOOR_G, PROTEIN_HIGH_G
    if yesterday_type == "long_ride":
        p_low = PROTEIN_POST_LONG_FLOOR_G
        modifiers.append("protein floor raised: yesterday was a long ride")

    # 2. carbs: g/kg by day type, or the carb load inside 3 days of the race
    if race_week:
        c_lo_kg, c_hi_kg = CARB_LOAD_G_PER_KG
        modifiers.append("carb load: within 3 days of the race")
    else:
        c_lo_kg, c_hi_kg = DAY_TYPES[day_type]["carb_g_per_kg"]
    c_low, c_high = c_lo_kg * rolling_weight, c_hi_kg * rolling_weight
    if pre_long and not race_week:
        # Upper half only: topping glycogen before the session, not after.
        c_low = (c_low + c_high) / 2
        modifiers.append("carbs to the upper half: long session tomorrow")

    # 3. fat floor is sourced; the deficit is then capped so it cannot breach it
    f_floor = FAT_FLOOR_G_PER_KG * rolling_weight

    floors_kcal = p_low * 4 + f_floor * 9 + c_low * 4
    headroom = max(0.0, maintenance - floors_kcal)
    # Reserve fat-zone width, or the zone collapses to a point. See FAT_ZONE_MIN_WIDTH_G.
    allowable = max(0.0, headroom - FAT_ZONE_MIN_WIDTH_G * 9)

    deficit = 0.0
    if deficit_enabled:
        if is_long:
            warnings.append("deficit suppressed: long session day, fuelling takes priority")
        elif rhr_guard_active:
            warnings.append("deficit suppressed: resting HR elevated, holding maintenance")
        elif pre_long:
            # A deficit here fights the modifier already applied: carbs have just
            # been pushed to the upper half to arrive at tomorrow's session
            # glycogen-loaded. Cutting calories on the same day works against it.
            warnings.append("deficit suppressed: topping glycogen for tomorrow's long session")
        else:
            uncapped = deficit_pct * maintenance
            deficit = min(uncapped, allowable) + correction_kcal
            deficit = max(0.0, min(deficit, allowable))
            if deficit < uncapped - 1:
                warnings.append(
                    f"deficit capped at {deficit:.0f} kcal (from {uncapped:.0f}): the "
                    f"protein, fat and carb floors leave only {allowable:.0f} kcal of room")
    target_kcal = maintenance - deficit

    # 4. fat ceiling from residual calorie space, then tightened for GI reasons
    residual_fat_g = (target_kcal - p_low * 4 - c_low * 4) / 9
    f_high = max(f_floor, residual_fat_g)
    fat_basis = "residual calorie space (reasoned, not literature)"
    if race_week and f_high > FAT_CEILING_RACE_WEEK_G:
        f_high = FAT_CEILING_RACE_WEEK_G
        fat_basis = "race week ceiling (GI, reasoned)"
        modifiers.append("fat ceiling tightened: race week")
    elif pre_long and f_high > FAT_CEILING_PRE_LONG_G:
        f_high = FAT_CEILING_PRE_LONG_G
        fat_basis = "pre-long-session ceiling (GI, reasoned)"
        modifiers.append("fat ceiling tightened: long session tomorrow")
    if residual_fat_g < f_floor:
        warnings.append(
            f"no calorie room for the fat floor: protein and carb floors leave "
            f"{residual_fat_g:.0f} g against a {f_floor:.0f} g floor")

    # 5. fibre: a target most days, a CEILING before long sessions and at the race.
    #    Same day type can carry opposite bias, decided purely by the lookahead.
    if race_week:
        fb_low, fb_high, fb_bias = RACE_WEEK_FIBRE_G[0], RACE_WEEK_FIBRE_G[1], BIAS_CEILING
        modifiers.append("fibre ceiling: race week")
    elif pre_long:
        fb_low, fb_high, fb_bias = 0, LOW_RESIDUE_CEILING_G, BIAS_CEILING
        modifiers.append("fibre flipped to a ceiling: long session tomorrow")
    elif is_long:
        fb_low, fb_high, fb_bias = 0, LOW_RESIDUE_CEILING_G, BIAS_CEILING
    else:
        fb_low, fb_high = DAY_TYPES[day_type]["fibre_g"]
        fb_bias = BIAS_FLOOR

    return {
        "day_type": day_type,
        "confidence": day_confidence,
        "kcal": _zone(target_kcal * (1 - KCAL_BAND_PCT), target_kcal * (1 + KCAL_BAND_PCT),
                      BIAS_BAND, f"maintenance {maintenance:.0f} less deficit {deficit:.0f}",
                      day_confidence),
        "kcal_target": round(target_kcal),
        "kcal_maintenance": round(maintenance),
        "kcal_confidence": kcal_confidence,
        "net_session_kcal": net,
        "deficit_applied_kcal": round(deficit),
        "deficit_headroom_kcal": round(headroom),
        "protein_g": _zone(p_low, p_high, BIAS_FLOOR, "2.2-2.7 g/kg FFM"),
        "fat_g": _zone(f_floor, f_high, BIAS_BAND, fat_basis),
        "carb_g": _zone(c_low, c_high, BIAS_BAND,
                        f"{c_lo_kg}-{c_hi_kg} g/kg on rolling weight"),
        "fibre_g": _zone(fb_low, fb_high, fb_bias,
                         "ceiling before long sessions: residue and splanchnic flow"),
        "modifiers": modifiers,
        "warnings": warnings,
        "sodium_basis": {"sweat_na_mg_l": [SWEAT_NA_ASSUMED_LOW, SWEAT_NA_ASSUMED_HIGH],
                         "confidence": "assumed",
                         "note": "no sweat test; band is not a target"},
        "weight_basis_kg": rolling_weight,
    }


# --- projection and flags (v0.2 §3.4 and §7) --------------------------------

MACRO_KEYS = ("kcal", "protein_g", "fat_g", "carb_g", "fibre_g")


def project(consumed: dict, plausible_remaining: dict | None = None) -> dict:
    """Projected landing per macro: {low, high, open_ended}.

    Display the PROJECTION against the zone, not progress against a total. The
    range narrows as meals log and collapses to actual at close-out.

    `plausible_remaining` maps a macro to (min, max) for the rest of the day, and is
    the load-bearing unknown in the whole model. v1 asks the athlete their plan at
    breakfast rather than inferring it. With nothing supplied the projection is
    open_ended: the low bound is what is already eaten and the high bound is
    unbounded, so a floor can never be declared unreachable on no information.
    Inventing a plausible remainder would manufacture flags out of an assumption."""
    out = {}
    for key in MACRO_KEYS:
        eaten = float((consumed or {}).get(key) or 0)
        rng = (plausible_remaining or {}).get(key)
        if rng is None:
            out[key] = {"low": round(eaten, 1), "high": None, "open_ended": True}
        else:
            lo, hi = float(rng[0] or 0), float(rng[1] or 0)
            out[key] = {"low": round(eaten + lo, 1), "high": round(eaten + hi, 1),
                        "open_ended": False}
    return out


def zone_flags(z: dict, projection: dict) -> list:
    """Flag only when the projected landing CANNOT reach the zone.

    Replaces v0.1's percentage pacing entirely. Straddling a boundary is not a
    flag: a projection of 150-190 g against a 165 g floor is still reachable and
    saying otherwise trains the athlete to ignore flags. Ranked by distance from
    the zone, worst first.

    Direction follows `bias`. A floor never flags high, so eating 210 g of protein
    against a 165-200 zone is not an event. A ceiling never flags low, so 8 g of
    fibre on a pre-long-ride day is compliance, not failure - the exact misreading
    v0.1 §4.1 warned about."""
    flags = []
    for key in MACRO_KEYS:
        zone, proj = z.get(key), projection.get(key)
        if not zone or not proj:
            continue
        bias = zone["bias"]
        lo, hi = zone["low"], zone["high"]

        if bias in (BIAS_FLOOR, BIAS_BAND):
            # Unreachable only when even the optimistic bound falls short. An
            # open-ended projection can always still reach a floor.
            best = proj["high"] if not proj["open_ended"] else None
            if best is not None and best < lo:
                flags.append({"macro": key, "direction": "cannot_reach_floor",
                              "zone_low": lo, "projected_high": best,
                              "distance": round(lo - best, 1),
                              "message": f"{key} projects to land below its {lo:g} floor"})
        if bias in (BIAS_CEILING, BIAS_BAND):
            # Unavoidable only when even the pessimistic bound is over. This works
            # on an open-ended projection because what is already eaten is a floor
            # under the landing.
            worst = proj["low"]
            if worst > hi:
                flags.append({"macro": key, "direction": "exceeds_ceiling",
                              "zone_high": hi, "projected_low": worst,
                              "distance": round(worst - hi, 1),
                              "message": f"{key} has already passed its {hi:g} ceiling"})
    flags.sort(key=lambda f: f["distance"], reverse=True)
    return flags


def rank_contributors(entries, field: str, top: int = 5) -> list:
    """The day's entries ranked by contribution to one macro, so a flag can name
    what is driving it. The cause is almost never one bad item, it is four or five
    reasonable ones stacking. In-session fuel is marked `protected`: it may be
    shown, but nothing may ever propose trimming it."""
    rows = [{"name": e.get("resolved_name") or e.get("raw_text") or "?",
             "amount": round(float(e.get(field) or 0), 1),
             "in_session": bool(e.get("in_session")),
             "protected": bool(e.get("in_session"))}
            for e in entries or [] if float(e.get(field) or 0) > 0]
    rows.sort(key=lambda r: r["amount"], reverse=True)
    return rows[:top]


# --- guardrails -------------------------------------------------------------

def underfuel_flag(entries, z: dict, rmr: float) -> dict | None:
    """Low energy availability at close: consumed < RMR + half the training cost.

    Fires whether or not the deficit is deliberate. In lean male endurance athletes
    low energy availability suppresses testosterone, impairs recovery and raises
    infection risk, so a deliberate deficit must not silence it. Three or more in a
    week is the escalation the tracking page should shout about."""
    consumed = sum(float(e.get("kcal") or 0) for e in entries or [])
    floor = rmr + 0.5 * (z.get("net_session_kcal") or 0)
    if consumed >= floor:
        return None
    return {"type": "underfuel", "severity": "high",
            "consumed_kcal": round(consumed), "floor_kcal": round(floor),
            "shortfall_kcal": round(floor - consumed),
            "message": "Energy intake below the low-availability floor for today's training."}


def rhr_guard(wellness, on: date | None = None, baseline_days: int = 30,
              threshold: float = 1.10, consecutive: int = 2) -> dict:
    """Resting HR elevated above its own baseline for N consecutive days.

    When active, callers must force maintenance. Jamie's baseline is ~52 bpm and
    readings of 71 and 76 occurred in early August; a deficit through that is the
    exact stacking this guard exists to stop.

    The baseline excludes the days under test, otherwise a sustained elevation
    drags its own reference upward and the guard stops firing precisely when it
    matters most.

    The tested window is the newest `consecutive` rows that EXIST over a slightly
    wider calendar window, not the last `consecutive` calendar days. Keying it to
    calendar days made the guard unfirable at consecutive >= 3, because ICU's
    current-day row is routinely absent, and a guard that cannot fire is worse than
    no guard: it reads as coverage. Contiguity is still enforced via `span`.

    Reading an unfinalised row is safe here: `restingHR` is device-sourced and ICU
    does not recompute it. `_wellness_row_finalized` guards CTL/ATL/Form, which this
    never touches, so no finalisation filter belongs here."""
    on = on or date.today()
    rows = []
    for w in wellness or []:
        d = _as_date(w.get("id") or w.get("date"))
        rhr = w.get("restingHR") or w.get("resting_hr")
        if d and rhr and d <= on:
            rows.append((d, float(rhr)))
    rows.sort(reverse=True)
    if not rows:
        return {"active": False, "reason": "no resting HR data"}

    search = [(d, v) for d, v in rows if (on - d).days <= consecutive + 1]
    tested = search[:consecutive]
    if len(tested) < consecutive:
        return {"active": False, "reason": "insufficient recent resting HR data"}

    span = (tested[0][0] - tested[-1][0]).days + 1
    if span > consecutive + 1:
        return {"active": False, "reason": "recent readings too scattered to call consecutive"}

    oldest_tested = tested[-1][0]
    baseline_pool = [v for d, v in rows if 0 < (oldest_tested - d).days <= baseline_days]
    if len(baseline_pool) < 7:
        return {"active": False, "reason": "insufficient baseline history"}

    baseline = sum(baseline_pool) / len(baseline_pool)
    limit = baseline * threshold
    elevated = [(d.isoformat(), v) for d, v in tested if v > limit]
    active = len(elevated) >= consecutive
    return {"active": active, "baseline_bpm": round(baseline, 1),
            "limit_bpm": round(limit, 1),
            "tested_days": [d.isoformat() for d, _ in tested],
            "elevated": elevated,
            "message": ("Resting HR elevated above baseline - holding maintenance."
                        if active else "")}


def observed_rate_kg_per_week(measurements, on: date | None = None,
                              window_days: int = 28) -> dict:
    """Trend in the rolling morning weight, kg/week, by comparing the two halves of
    the window. Returns {'rate', 'days_used', 'usable'}.

    Halves rather than a regression on purpose: a mean of means over a week each side
    is robust to the one contaminated reading that slips past the sweat filter, and it
    is legible enough that the athlete can check it by hand."""
    on = on or date.today()
    half = window_days // 2
    recent = rolling_weight_kg(measurements, on=on, days=half)
    earlier = rolling_weight_kg(measurements, on=on - timedelta(days=half), days=half)
    days = sum(1 for m in measurements or []
               if (m.get("tag") or "morning") == "morning"
               and (d := _as_date(m.get("date") or m.get("logged_at")))
               and 0 <= (on - d).days < window_days)
    if recent is None or earlier is None or days < CORRECTION_MIN_DAYS:
        return {"rate": None, "days_used": days, "usable": False,
                "reason": f"need {CORRECTION_MIN_DAYS} morning weights in the window"}
    weeks = half / 7.0
    return {"rate": round((recent - earlier) / weeks, 3), "days_used": days,
            "usable": True, "recent_mean": recent, "earlier_mean": earlier}


def deficit_correction(measurements, intended_rate_kg_per_week: float,
                       on: date | None = None) -> dict:
    """Close the loop: correct the deficit from MEASURED weight change, not from the
    estimated TDEE.

    Why this exists. The open-loop target is built from three unvalidated inputs
    stacked on each other: Mifflin-St Jeor, the 1.35 NEAT/TEF multiplier, and ICU's
    activity calories. Their combined error is comparable to or larger than the
    deficit itself - on a 90-minute HR-derived run the activity-calorie error alone
    is roughly +/-133 kcal against a 275 kcal deficit, and swapping the multiplier
    from 1.35 to 1.45 moves the base by 185 kcal, more than a whole recovery-day
    deficit. So a 10% open-loop deficit is not a controlled 10%; on some days it is
    not a deficit at all.

    The rolling morning weight is the only measured quantity in the chain, so it is
    the only honest controller. The TDEE estimate becomes the starting point and this
    corrects it: if the trend is not tracking the intended rate, shift intake by the
    energy equivalent of the discrepancy.

    Bounded and gated deliberately. Bounded (CORRECTION_MAX_KCAL) so scale noise
    cannot swing intake; gated on CORRECTION_MIN_DAYS of clean morning weights
    because single readings carry ~+/-2.5 kg of 95% uncertainty and a shorter window
    would chase hydration. Sign convention: a POSITIVE correction increases the
    deficit (losing too slowly), negative reduces it.

    Honest limitation to surface in the UI: with 40 days to the race this loop gets
    at most two correction cycles, so it cannot rescue a target that open-loop
    arithmetic already says is out of reach."""
    obs = observed_rate_kg_per_week(measurements, on=on)
    if not obs["usable"]:
        return {"correction_kcal": 0, "usable": False, "reason": obs.get("reason"),
                "observed_rate": None}
    # Loss is negative in rate terms; intended is given as a positive loss rate.
    observed_loss = -obs["rate"]
    shortfall = intended_rate_kg_per_week - observed_loss
    raw = shortfall * KCAL_PER_KG_FAT / 7.0
    capped = max(-CORRECTION_MAX_KCAL, min(CORRECTION_MAX_KCAL, raw))
    return {"correction_kcal": round(capped), "usable": True,
            "observed_rate": obs["rate"], "observed_loss_kg_per_week": round(observed_loss, 3),
            "intended_loss_kg_per_week": intended_rate_kg_per_week,
            "uncapped_kcal": round(raw), "days_used": obs["days_used"],
            "message": ("Losing slower than intended" if capped > 0 else
                        "Losing faster than intended" if capped < 0 else "On track")}


def race_weight_projection(current_kg: float, target_kg: float, days_to_race: int,
                           rmr: float, weekly_mix=None,
                           deficit_pct: float = DEFICIT_PCT_DEFAULT,
                           tdee_multiplier: float = NEAT_TEF_MULTIPLIER) -> dict:
    """What the PROPORTIONAL deficit will actually deliver by race day.

    Exists so the app never shows a race-weight target without the shortfall beside
    it. Because the deficit now scales with the day and is headroom-capped, the
    projection has to walk a representative week rather than multiply one number:
    `weekly_mix` maps day_type to how many such days a week, defaulting to Jamie's
    pattern from day_rules (2 long, 4 standard, 1 recovery)."""
    mix = weekly_mix or {"long_ride": 1, "long_run": 1, "standard": 4, "recovery": 1}
    base = base_tdee(rmr, tdee_multiplier)
    weekly_deficit = 0.0
    for dtype, count in mix.items():
        if dtype in LONG_DAY_TYPES:
            continue                                    # never a deficit on long days
        net = {"standard": 1446.0, "recovery": 0.0}.get(dtype, 0.0)
        maintenance = base + net
        c_low = DAY_TYPES[dtype]["carb_g_per_kg"][0] * current_kg
        floors = PROTEIN_FLOOR_G * 4 + FAT_FLOOR_G_PER_KG * current_kg * 9 + c_low * 4
        headroom = max(0.0, maintenance - floors)
        weekly_deficit += count * min(deficit_pct * maintenance, headroom)

    weeks = max(0, days_to_race) / 7.0
    loss_kg = weekly_deficit * weeks / KCAL_PER_KG_FAT
    projected = round(current_kg - loss_kg, 1)
    gap_kg = max(0.0, current_kg - target_kg)
    return {"gap_kg": round(gap_kg, 1),
            "projected_race_kg": projected,
            "projected_loss_kg": round(loss_kg, 1),
            "weekly_deficit_kcal": round(weekly_deficit),
            "reaches_target": projected <= target_kg,
            "shortfall_kg": round(max(0.0, projected - target_kg), 1),
            "required_daily_kcal_to_reach": (
                round(gap_kg * KCAL_PER_KG_FAT / max(1, days_to_race)) if gap_kg else 0)}


# --- protein accounting and micronutrients ----------------------------------

def counting_protein_g(entries) -> tuple[float, float]:
    """Returns (counting_g, non_counting_g). Collagen and gelatin are excluded from
    the protein figure: ~15 g with no tryptophan and little leucine would otherwise
    show target met while real protein sat short. Still stored and shown."""
    counting = non_counting = 0.0
    for e in entries or []:
        grams = float(e.get("protein_g") or 0)
        name = (e.get("resolved_name") or e.get("raw_text") or "").lower()
        if any(tok in name for tok in NON_COUNTING_PROTEIN_SOURCES):
            non_counting += grams
        else:
            counting += grams
    return round(counting, 1), round(non_counting, 1)


def micronutrient_status(supplement_entries) -> dict:
    """Compliance read on the watch list, NOT a status read.

    Jamie declined blood testing and per-item food micronutrients, so the only
    honest states are supplemented / not_supplemented / unknown. Never label these
    'adequate' or 'low': with no blood data and no food micro totals there is no
    basis for either, and a fabricated 'adequate' is worse than an honest
    'unknown'. Plant diversity carries the breadth signal instead."""
    taken = {}
    for e in supplement_entries or []:
        name = (e.get("nutrient") or e.get("resolved_name") or "").lower().replace(" ", "_")
        for key in MICRO_WATCH:
            if key in name:
                taken[key] = {"state": "supplemented",
                              "dose": e.get("dose"), "unit": e.get("unit")}
    return {key: taken.get(key, {"state": "not_supplemented", "dose": None, "unit": None})
            for key in MICRO_WATCH}
