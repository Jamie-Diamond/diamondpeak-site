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

FUEL FOR THE WORK REQUIRED (Option B, Jamie's call 13 Aug 2026)
The day's carbohydrate is now prescribed from the DEMAND of the loading window - the
sessions still to come or already done today, plus tomorrow's planned ones - not from
the day type alone and not from the calorie residual. See classify_demand and
DEMAND_CARB_G_PER_KG. Three things follow, and each replaces a rule that read the
calendar too coarsely:

  1. `day_type` no longer decides whether the day is a LONG one for fuelling. It
     could not: classify_day's 240-minute ride cliff called a 230-minute ride
     `standard`, so 13 Aug 2026 was fuelled off a 5-6 g/kg reference band while the
     day actually needed 8-12, and the carb cross-check warned about the day's own
     targets. The demand tiers use 150 min ride / 90 min run / 150 planned load, and
     `is_long` inside zones() is now "a long session TODAY" per those tiers.
     classify_day survives unchanged for protein, fibre and the stored day label.
  2. Intensity counts, not just duration. A threshold or VO2 session is a glycogen
     demand a 60-minute duration cannot express, so any session the coach flags as
     quality - or names as one - lifts the window to KEY.
  3. The deficit gate collapses from a list of five special cases to one rule: a
     deficit is only ever available when the tier ahead is EASY or REST.

THE PRESCRIPTION CAN EXCEED THE DAY'S ENERGY, AND THAT IS REPORTED, NOT RESOLVED
A rest day before a long run has ~2,500 kcal of maintenance and a 8-10 g/kg carb
prescription worth ~2,700 kcal of carbohydrate alone. Those cannot both be true. The
model does NOT quietly pick a winner: the safety floors hold (protein, the essential
fat floor, the fat zone's width), so carbs land at what the energy allows, and the
gap is stated. Which list it lands in depends on the CAUSE, because a warning that
fires every pre-long day is a warning the athlete learns to ignore:
  - energy short of the prescription: a MODIFIER, beside the carb-easing line. The
    activity calories are fine; the day structurally cannot hold the load at
    maintenance. Only race week is allowed to let energy follow the carbs into a
    surplus, and that stays a deliberate exception.
  - energy far ABOVE the prescription: a WARNING naming the activity calories,
    because a 5,000 kcal day with nothing key or long in the window means the
    session energy is wrong, not the band.

THE DEFICIT IS PROPORTIONAL AND SELF-LIMITING (Jamie's call, 10 Aug 2026)
  deficit = min(deficit_pct × maintenance, headroom)
  headroom = maintenance − (protein_floor + fat_floor + carb_band_minimum)
A FLAT deficit does not land equally. The floors are near-fixed costs (~167 g
protein + 75 g fat = ~1,343 kcal whatever the day), which is 54% of a 2,491 kcal
recovery day but only 34% of a 3,938 kcal standard day, so a flat 350 crowds small
days hard and large days barely - on a recovery day it left the floors and the carb
floor ~200 kcal short of satisfiable. Proportional tracks the same crowding
mechanism §3.3 identifies for fat, and the headroom cap means it can never breach a
floor. In practice that is ~394 kcal on a standard day and ZERO on a recovery day:
after the headroom cap and the fat-zone reserve there are only ~16 kcal of room
there, which is inside the error on every input, so it is dropped rather than
reported as a deficit. Projects ~1.1-1.3 kg over 40 days.

Known tension, recorded rather than resolved: proportional puts the largest
absolute cut on the largest non-long training day, which is the opposite of "fuel
the work required". The orthodox inverse (bigger cut on easy days) is arithmetically
impossible here because easy days have no headroom, so proportional wins by
default, not on merit.

ONE RULE now decides availability (13 Aug 2026): a deficit exists only when the tier
ahead is EASY or REST. That single test subsumes the four special cases it replaces -
long day, day before a long day, quality session ahead, race-week loading are all
"not EASY or REST" - and it closes the gap the list had, which was a threshold session
tomorrow. The RHR guard remains a separate veto on top: a deficit stacked on an
unresolved illness signal is the failure mode §10.2 exists to prevent, and it already
happened once in early August. The low-energy-availability flag (§10.1) fires whether
or not the deficit is deliberate.

FAT IS BIDIRECTIONAL AND ITS CEILING IS DERIVED, NOT LITERATURE (v0.2 §3.3)
Two mechanisms bind on opposite days. Calorie crowding binds on low-energy days:
once protein and carbs are paid for, what remains for fat on a recovery day is
tight. Gastric emptying binds on long days, where there is plenty of calorie room
but fat slows absorption of the carbs the session needs.

Fat is a g/kg BAND (0.9-1.2), not a residual. An earlier cut derived the ceiling
from whatever calorie space remained, which quoted 75-229 g on a long ride day: 229
g is not a sane target and would compromise GI on the day it matters most. The floor
(0.9 g/kg) is well sourced and matches commercial practice exactly; the 1.2 g/kg
ceiling is practice, and the pre-long (90 g) and race-week (80 g) tightenings are
reasoning. `basis` says which on every zone returned.

CARBS ARE PRESCRIBED FROM THE DEMAND, AND THE RESIDUAL BECOMES THE CROSS-CHECK
v0.2 prescribed a carb band and the day did not add up: on a long ride day, protein
plus fat capped at 1.2 g/kg plus carbs at the top of the 8-9 g/kg band left 824 kcal
unallocated with nothing to absorb it. v0.3 reverted to a residual. Option B restores
the prescription with the two mechanisms that were missing:
  - the bands are indexed to the DEMAND AHEAD rather than to the day type, so a long
    day's own band reaches 12 g/kg and the top of the band no longer sits far below
    the day's energy, and
  - fat's ceiling is energy-aware on easy and rest windows (FAT_SHARE_TARGET), so
    what carbs decline to take has somewhere to go on a big day.
Where they still cannot be reconciled the residual arithmetic is kept as a stated
cross-check rather than a silent override - see the section above on which list the
disagreement lands in. The physiological carb floors in DAY_TYPES survive as the
bound on how far the prescription may be eased, which is what stops an impossible day
being made to "fit" by starving carbohydrate.

Race week inverts this. A 10-12 g/kg load is a PRESCRIPTION and at 83 kg it is
3,300-4,000 kcal of carbohydrate alone, so it cannot fit inside a maintenance
target: the energy follows the carbs, and the resulting surplus is reported in
`carb_load_surplus_kcal`. That surplus is exactly why the spec suppresses the weight
display during the load - 1-2 kg of glycogen-bound water reads as fat gain on a BIA
scale.

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

import re
from datetime import date, datetime, timedelta

# --- constants ---------------------------------------------------------------

KCAL_PER_KG_FAT = 7700
NEAT_TEF_MULTIPLIER = 1.35

# Protein is expressed PER KG and flexes with load, not as a fixed gram figure.
# v0.1 had a flat 180 g; v0.2 made it a 165-200 g range; this makes it g/kg so it
# tracks the athlete's weight as it changes, which a fixed figure cannot. Range
# and magnitude match commercial practice (Fuelin prescribes 2.0-2.5 g/kg
# bodyweight, flexing with training load) rather than being invented here.
# Cost of the change, worth knowing: v0.1 justified a flat figure as "the one target
# that should not move with training load" and the Block view charted adherence to
# it. Protein now moves, so that chart loses its premise.
PROTEIN_G_PER_KG = {
    "recovery":  (2.0, 2.5),
    "standard":  (2.1, 2.5),
    "long_run":  (2.2, 2.6),
    "long_ride": (2.2, 2.6),
}
# In an energy deficit protein requirement rises to protect lean mass (Helms et al.,
# cited in the spec's own provenance note), so the floor lifts when a deficit is
# actually applied - not merely when the flag is on.
PROTEIN_DEFICIT_BUMP_G_PER_KG = 0.1
PROTEIN_POST_LONG_FLOOR_G_PER_KG = 2.2   # yesterday was a long ride

FAT_FLOOR_G_PER_KG = 0.9               # well sourced; also Fuelin's floor exactly
# Fat's ceiling is energy-aware on EASY and REST windows, as a share of the day's
# energy. 1.2 g/kg is 100 g at 83 kg, which is 900 kcal - fine on a 3,000 kcal day and
# absurdly tight on a 5,200 kcal one, where holding fat at 100 g while carbs are capped
# at their prescribed band strands over a thousand calories with nowhere to go. The
# share term is what gives that energy a home on days with no GI reason to refuse it.
# FAT_SHARE_MAX is the outer limit: fat is never allowed past a 0.35 share of the day,
# which is the crowding argument in reverse and binds on small days where 1.2 g/kg
# would otherwise be over a third of the day's energy.
FAT_SHARE_TARGET = 0.25
FAT_SHARE_MAX = 0.35
# Fat now has a HARD ceiling in g/kg rather than taking whatever residual calorie
# space remains. Deriving it from the residual gave 75-229 g on a long ride day:
# 229 g is not a sane target and would compromise GI on the day it matters most.
# 1.2 g/kg is Fuelin's ceiling.
FAT_CEILING_G_PER_KG = 1.2
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

# A BAND HAS TO BE A BAND. Carbs came out 466.5-466.5 on a long-run day: the derived low and
# the high both clamp against the carb floor, so when the protein and fat highs consume the
# energy they meet at a point. A zero-width band can only ever read "in zone" or "over" -
# there is no room to land in - which makes a landing zone behave like the point target this
# model exists to avoid.
#
# Widened UPWARD only, never downward. The lower edge of these bands is a physiological floor
# (protein per kg, the carb floor, the essential-fat floor) and lowering it would sanction
# eating less than the floor to stay "in zone" - the one direction that does real harm. The
# upper edge is practice, so that is the edge that moves.
BAND_MIN_WIDTH_G = 10.0
BAND_MIN_WIDTH_FRACTION = 0.05


def widen_band(low: float, high: float) -> tuple:
    """Give a collapsed band usable width, above the floor rather than below it."""
    low, high = float(min(low, high)), float(max(low, high))
    mid = (low + high) / 2.0
    # The absolute minimum is capped at a quarter of the band's own size, so a 10 g rule
    # cannot turn an 8 g band into 8-18 g. Proportional at small values, absolute at large.
    want = max(min(BAND_MIN_WIDTH_G, mid * 0.25), mid * BAND_MIN_WIDTH_FRACTION)
    if high - low >= want:
        return round(low, 1), round(high, 1)
    return round(low, 1), round(low + want, 1)

# How far the carb safety floor may be eased to protect the fat zone, as a fraction of
# the day-type floor. There has to be a limit: without one, an impossible day was made
# to "fit" by easing carbs from 250 g to 35 g (0.4 g/kg) and the unsatisfiable warning
# stopped firing. Easing is a small accommodation, not a release valve.
CARB_EASE_FLOOR_FRACTION = 0.8

# Below this the deficit is not a deficit, it is rounding. A headroom-capped
# recovery day produced a 16 kcal "deficit", which is far inside the error on every
# input feeding it; reporting it would be false precision.
DEFICIT_MIN_MEANINGFUL_KCAL = 50

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
FIBRE_CEILING_KEY_AHEAD_G = 30         # looser than pre-long: see the fibre step
RACE_WEEK_FIBRE_G = (10, 15)

# How far the prescribed carb band and the energy residual may diverge before the day
# says so. A tolerance rather than a strict "outside the band" test: the residual sitting
# a little inside the band is the normal state of an ordinary day, and warning on that
# would fire on nearly every day and mean nothing.
CARB_DEMAND_TOLERANCE = 0.20

# How much of the day's energy may sit ABOVE the top of every zone before the day is
# called incoherent. With carbs prescribed rather than emergent, a small amount routinely
# does - the protein and fat ranges absorb it, and a 2-hour ride day leaves ~120 kcal
# unallocated at 4-6 g/kg, which is inside the error on the activity calories anyway. A
# fifth of the day is different in kind and means the session energy is wrong.
DAY_UNALLOCATED_WARN_FRACTION = 0.10

RIDE_SPORTS = ("Ride", "VirtualRide", "GravelRide", "MountainBikeRide")
RUN_SPORTS = ("Run", "VirtualRun", "TrailRun")

# --- the demand ahead (Option B, 13 Aug 2026) --------------------------------

DEMAND_KEY, DEMAND_LONG, DEMAND_EASY, DEMAND_REST = "key", "long", "easy", "rest"

# The BANDS, which are the tier plus WHEN it falls. A long session today and a long
# session tomorrow are different fuelling problems: today's band is the higher one
# because the in-session fuel is counted inside the day it is taken, so the day total
# has to carry it. Tomorrow's is a top-up, not a top-up plus the session itself.
BAND_LONG_TODAY = "long_today"
BAND_LONG_AHEAD = "long_ahead"
BAND_KEY_AHEAD = "key_ahead"
BAND_EASY_AHEAD = "easy_ahead"
BAND_REST_AHEAD = "rest_ahead"

DEMAND_CARB_G_PER_KG = {
    BAND_LONG_TODAY: (8, 12),
    BAND_LONG_AHEAD: (8, 10),
    BAND_KEY_AHEAD:  (6, 8),
    BAND_EASY_AHEAD: (4, 6),
    BAND_REST_AHEAD: (3, 5),
}
BAND_LABEL = {
    BAND_LONG_TODAY: "long session today",
    BAND_LONG_AHEAD: "long session tomorrow",
    BAND_KEY_AHEAD:  "quality session in the window",
    BAND_EASY_AHEAD: "easy window",
    BAND_REST_AHEAD: "rest window",
}

# What makes a session LONG for fuelling. These REPLACE classify_day's 240-minute ride
# cliff for demand purposes - see the module docstring on 13 Aug. The load trigger is
# not a nicety: planned ICU events frequently carry no duration at all, so a threshold
# ride can arrive as a load figure and nothing else.
LONG_RIDE_MIN = 150
LONG_RUN_MIN = 90
LONG_PLANNED_LOAD = 150

# The coach's own quality vocabulary (primitives/modulation.py _QUALITY_TYPES, plus
# brick, which modulation also treats as quality). Read from the session dict when the
# caller has already classified it - the bot does, using the shared classifier - so the
# two cannot drift. `brick` is included because a bike-to-run brick is a glycogen
# demand whatever its duration.
QUALITY_SESSION_TYPES = ("bike_threshold", "bike_vo2", "bike_race_pace",
                         "run_quality", "brick")
# The coach's classifier is EXHAUSTIVE for bike and run - every such session lands in a
# quality bucket or one of these - so for those an easy verdict is final. It is not
# exhaustive for swims and strength work: everything in the pool comes back `swim`
# whether it is a recovery float or a CSS test, so those fall through to the text.
EASY_SESSION_TYPES = ("bike_z2", "run_easy", "run_long")

# FALLBACK only, for callers with no access to the coach's classifier (the publish
# step, the tests). Mirrors modulation's keyword list and adds the swim CSS test, which
# modulation has no bucket for but which is unambiguously a key session here.
#
# Unlike modulation this reads the AIM TEXT as well as the name. modulation matches the
# name alone, having measured that coaching prose false-matches on words like
# "intervals"; that asymmetry is deliberate rather than an oversight. A false KEY here
# costs a slightly higher carb band and a suppressed deficit for one day; a missed KEY
# under-fuels a session, and under-fuelling is the costlier error.
# Word-boundary matched, as modulation does: a substring match on a short token that is
# also an ordinary English word reads coaching prose as a prescription. "race" was the
# one that bit - an Endurance ride whose aim said "building toward race day" came back
# KEY, which would suppress the deficit on an easy day for no reason. So the bare "race"
# token is matched against the session NAME only, where it means the session IS a race;
# in the aim text only the compound forms count.
_KEY_TOKENS = (r"threshold", r"sweet ?spot", r"vo ?2", r"v02", r"ftp", r"intervals?",
               r"over[/ -]?unders?", r"tempo", r"fartlek", r"hill repeats?",
               r"race ?pace", r"race rehearsal", r"race sim\w*", r"css", r"brick",
               r"reps?")
KEY_SESSION_RE = re.compile(r"\b(?:" + "|".join(_KEY_TOKENS + (r"race",)) + r")\b")
KEY_AIM_RE = re.compile(r"\b(?:" + "|".join(_KEY_TOKENS) + r")\b")




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


# The floor an ordinary day carries, used as the POST-session fibre target on a long day.
EVERYDAY_FIBRE_G = DAY_TYPES.get("standard", {}).get("fibre_g") or (40, 45)


def _kcal_share(low, high, kcal_per_g, target_kcal) -> list:
    """A macro zone as a share of the day's energy. Grams are not comparable across a
    recovery day and a long-ride day; shares are, and the shares are what the fat
    ceiling and the crowding argument are actually about."""
    if not target_kcal:
        return [0.0, 0.0]
    return [round(low * kcal_per_g / target_kcal, 3),
            round(high * kcal_per_g / target_kcal, 3)]


def _zone(low, high, bias, basis="", confidence="normal") -> dict:
    """One macro's landing zone. `bias` decides warning direction, `basis` records
    whether the numbers are sourced or reasoned so the UI can be honest about it.

    Every zone in the model is built here, which is why the band-width rule lives here:
    applied per caller it would be applied to some macros and not others, and the one it
    was missing from would collapse silently. Floors and ceilings are one-sided on purpose
    and are left alone."""
    if bias == BIAS_BAND:
        widened_low, widened_high = widen_band(low, high)
        out = {"low": widened_low, "high": widened_high, "bias": bias,
               "basis": basis, "confidence": confidence}
        if round(high, 1) != widened_high:
            # Said out loud: the upper edge is no longer the derived figure, and a number
            # the page shows that was adjusted here has to admit it.
            out["widened_from_high"] = round(high, 1)
        return out
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


def _planned_load(s) -> float:
    """Planned or actual training load. ICU spells it `icu_training_load`; the coach's
    own proposals use `load_target`."""
    for key in ("icu_training_load", "load_target", "planned_load", "training_load"):
        v = s.get(key)
        if v:
            try:
                return float(v)
            except (TypeError, ValueError):
                return 0.0
    return 0.0


def is_long_session(s) -> bool:
    """Long for FUELLING purposes: 150 min on the bike, 90 min running, or 150 load."""
    sport = _sport(s)
    mins = _duration_min(s)
    if sport in RIDE_SPORTS and mins >= LONG_RIDE_MIN:
        return True
    if sport in RUN_SPORTS and mins >= LONG_RUN_MIN:
        return True
    return _planned_load(s) >= LONG_PLANNED_LOAD


def is_key_session(s) -> bool:
    """Intensity, not duration. Prefers the caller's own classification."""
    stype = (s.get("session_type") or "").strip().lower()
    if stype in QUALITY_SESSION_TYPES:
        return True
    if stype in EASY_SESSION_TYPES:
        # The caller's easy verdict is final for bike and run: a `bike_z2` session whose
        # aim text mentions intervals is an easy session, and second-guessing it here is
        # how two vocabularies for the same thing start.
        return False
    name = " ".join(str(s.get(k) or "") for k in ("name", "title", "type")).lower()
    aim = " ".join(str(s.get(k) or "") for k in ("aim", "description")).lower()
    return bool(KEY_SESSION_RE.search(name) or KEY_AIM_RE.search(aim))


def _session_label(s) -> str:
    name = (s.get("name") or s.get("title") or "").strip()
    if name:
        return name[:60]
    mins = _duration_min(s)
    sport = _sport(s) or "session"
    return f"{sport} {mins:.0f} min" if mins else sport


def classify_demand(*, today_sessions=None, tomorrow_sessions=None,
                    day_type: str | None = None, tomorrow_type: str | None = None,
                    calendar_known: bool | None = None) -> dict:
    """The demand of the loading window: today's sessions plus tomorrow's planned ones.

    Returns tier, band, when, the sessions that decided it, and a confidence. The BAND
    is what the carb prescription reads; the tier is what a human reads.

    Precedence is long-today, long-tomorrow, key, easy, rest. Long outranks key because
    the two bands overlap and the long one is higher: a day with a threshold session and
    a long ride in it is fuelled for the ride.

    NO CALENDAR IS EASY, NOT REST. An empty result from a failed or absent calendar is
    treated as an easy window rather than a rest one, and says so. The two bands differ
    by 1 g/kg at the floor, and getting it wrong in the rest direction under-fuels a day
    that may have had a session in it - which is the costlier error, the same asymmetry
    classify_from_day_rules already applies to the day type.

    `day_type` and `tomorrow_type` are read as LEGACY signals only: callers that predate
    Option B pass classifications rather than sessions (race_weight_projection passes
    neither), and a `long_ride` label must keep behaving like a long day for them."""
    today = [s for s in (today_sessions or []) if s]
    tomorrow = [s for s in (tomorrow_sessions or []) if s]
    known = calendar_known if calendar_known is not None else bool(today or tomorrow)

    long_today = [s for s in today if is_long_session(s)]
    long_ahead = [s for s in tomorrow if is_long_session(s)]
    key_today = [s for s in today if is_key_session(s)]
    key_ahead = [s for s in tomorrow if is_key_session(s)]
    legacy_long_today = day_type in LONG_DAY_TYPES
    legacy_long_ahead = tomorrow_type in LONG_DAY_TYPES

    note, confidence = "", "normal"
    if long_today or legacy_long_today:
        band, tier, when = BAND_LONG_TODAY, DEMAND_LONG, "today"
        driving = long_today or [{"name": f"{day_type} (day type)"}]
    elif long_ahead or legacy_long_ahead:
        band, tier, when = BAND_LONG_AHEAD, DEMAND_LONG, "tomorrow"
        driving = long_ahead or [{"name": f"{tomorrow_type} (day type)"}]
    elif key_today or key_ahead:
        band, tier = BAND_KEY_AHEAD, DEMAND_KEY
        when = "today" if key_today else "tomorrow"
        driving = key_today + key_ahead
    elif not known:
        band, tier, when, driving = BAND_EASY_AHEAD, DEMAND_EASY, None, []
        confidence = "low_confidence"
        note = ("no calendar data: assuming an easy window rather than a rest one, "
                "because under-fuelling is the costlier error")
    elif today or tomorrow:
        band, tier, when = BAND_EASY_AHEAD, DEMAND_EASY, ("today" if today else "tomorrow")
        driving = today + tomorrow
    else:
        band, tier, when, driving = BAND_REST_AHEAD, DEMAND_REST, None, []

    labels = []
    for s in driving:
        label = _session_label(s)
        if s in tomorrow and s not in today:
            label += " (tomorrow)"
        if label not in labels:
            labels.append(label)
    return {"tier": tier, "band": band, "when": when, "sessions": labels,
            "key_in_window": bool(key_today or key_ahead),
            "long_in_window": bool(long_today or long_ahead
                                   or legacy_long_today or legacy_long_ahead),
            "calendar_known": bool(known), "confidence": confidence, "note": note,
            "carb_g_per_kg": list(DEMAND_CARB_G_PER_KG[band])}


# Compendium MET values, for a session that is PLANNED and therefore carries no measured
# energy. Deliberately mid-range rather than optimistic: this feeds a maintenance figure,
# and over-estimating it would manufacture an intake target out of nothing.
MET_BY_SPORT = {"run": 10.0, "ride": 8.0, "bike": 8.0, "virtualride": 8.0,
                "swim": 8.0, "walk": 3.5, "hike": 6.0, "row": 7.0,
                "strength": 3.5, "weighttraining": 3.5, "yoga": 2.5}


def planned_session_kcal(session, weight_kg: float) -> float:
    """Gross energy for a session with a duration but no measured calories.

    kcal/min = MET x 3.5 x kg / 200, the standard form. Returns 0.0 when the sport is
    unknown, so an unrecognised session contributes nothing rather than a guess."""
    if not weight_kg:
        return 0.0
    raw = (session.get("type") or session.get("category") or session.get("sport") or "")
    key = raw.strip().lower().replace(" ", "")
    met = next((v for k, v in MET_BY_SPORT.items() if k in key), None)
    if not met:
        return 0.0
    return met * 3.5 * float(weight_kg) / 200.0 * _duration_min(session)


def net_session_kcal(sessions, rmr: float, weight_kg: float = None) -> tuple[float, str]:
    """Training energy above resting, plus a confidence label.

    'measured' only when every session contributing energy is power-metered;
    otherwise 'estimated', because ICU derives run and swim calories from heart
    rate at roughly +/-15-20%.

    A PLANNED session carries no calories, and the old loop still counted its HOURS - so
    net = gross - hours x rmr/24 made it SUBTRACT energy. Adding a 33 km run to the day
    lowered maintenance from 3,162 to 2,950, when the run alone is around 2,400 kcal: a day
    that needed ~5,500 was being asked to live on under 3,000. Duration without energy is
    now either estimated from a MET value or ignored entirely - never counted as resting
    time he did not spend resting."""
    gross = hours = 0.0
    all_powered = True
    estimated_any = False
    for s in sessions or []:
        kcal = float(s.get("calories") or s.get("kcal") or 0)
        mins = _duration_min(s)
        if not kcal and mins:
            kcal = planned_session_kcal(s, weight_kg)
            if not kcal:
                # Unknown sport and no measured energy: contribute NOTHING, hours
                # included. Counting the hours alone is what made a session cost him
                # calories instead of earning them.
                continue
            estimated_any = True
        if not kcal and not mins:
            continue
        gross += kcal
        hours += mins / 60.0
        if kcal and not s.get("average_watts"):
            all_powered = False
    net = gross - (hours * rmr / 24.0)
    conf = "measured" if (all_powered and gross and not estimated_any) else "estimated"
    return max(0.0, round(net, 1)), conf


# --- the zone computation ---------------------------------------------------

def zones(*, day_type: str, rolling_weight: float, rmr: float,
          sessions=None, tomorrow_type: str | None = None,
          yesterday_type: str | None = None, days_to_race: int | None = None,
          tomorrow_sessions=None, calendar_known: bool | None = None,
          deficit_enabled: bool = False, deficit_pct: float = DEFICIT_PCT_DEFAULT,
          rhr_guard_active: bool = False, day_confidence: str = "normal",
          correction_kcal: float = 0.0,
          tdee_multiplier: float = NEAT_TEF_MULTIPLIER) -> dict:
    """The day's landing zones, the modifiers applied, and any warning the inputs
    justify.

    Order matters: protein and the PRESCRIBED carbs are priced first, the deficit is
    then capped by what remains above those floors, and fat takes the energy left
    between its own floor and its ceiling. Computing fat before the deficit would let a
    deficit eat into the carbs that fuel the session instead of into fat.

    `sessions` is today's (completed and still-planned); `tomorrow_sessions` is
    tomorrow's planned. Together they are the loading window the carb prescription is
    read from. `calendar_known` says whether the calendar was actually readable, which
    an empty session list cannot: a genuine rest day and a failed ICU call look
    identical from here, and they must not be fuelled identically."""
    if day_type not in DAY_TYPES:
        raise ValueError(f"unknown day_type {day_type!r}")
    if not rolling_weight:
        raise ValueError("rolling_weight is required; do not fall back to a single reading")

    net, kcal_confidence = net_session_kcal(sessions, rmr, weight_kg=rolling_weight)
    maintenance = base_tdee(rmr, tdee_multiplier) + net
    modifiers, warnings = [], []
    race_week = days_to_race is not None and 0 <= days_to_race <= 3

    demand = classify_demand(today_sessions=sessions, tomorrow_sessions=tomorrow_sessions,
                             day_type=day_type, tomorrow_type=tomorrow_type,
                             calendar_known=calendar_known)
    if race_week and demand["band"] in (BAND_EASY_AHEAD, BAND_REST_AHEAD):
        # Race week is a KEY window by construction, and the calendar cannot be relied
        # on to say so - the race is often not an ICU event at all, and the taper
        # sessions around it read as easy. Correcting the band here rather than adding
        # a second deficit gate is what keeps the deficit rule a single test.
        demand = dict(demand, band=BAND_KEY_AHEAD, tier=DEMAND_KEY, when="today",
                      note="race week: the race is the key session ahead")
    band = demand["band"]
    # is_long means "a long session TODAY", from the demand tiers rather than from
    # day_type. day_type's 240-minute ride cliff called a 230-minute ride standard and
    # left the fibre ceiling and the fat GI cap off on a day that needed both.
    is_long = band == BAND_LONG_TODAY
    pre_long = band == BAND_LONG_AHEAD
    key_ahead = band == BAND_KEY_AHEAD
    # A hard session TODAY, long or quality. It is what decides whether the fibre ceiling
    # is PHASED: the residue reason expires when the session is done, and a ceiling that
    # ran all day told him off for eating his fibre after the work - the exact complaint
    # that produced the phasing in the first place. A quality session earns the same
    # treatment as a long one; only the ceiling's height differs.
    session_today = (is_long or key_ahead) and demand["when"] == "today"
    if demand["note"] and demand["confidence"] == "low_confidence":
        # Only when the demand was GUESSED, not merely when the session lists were empty:
        # a caller that passes a `long_ride` tomorrow_type and no session dicts has told
        # us what we need. A guess is a warning rather than a modifier because it is the
        # one input the athlete can correct - he can tell the bot what he is doing.
        warnings.append("demand could not be classified: " + demand["note"])

    # 1. protein: a g/kg band that flexes with load, the deficit, and yesterday.
    #    The deficit bump is applied only if a deficit will ACTUALLY be applied, which
    #    is not the same as deficit_enabled. Bumping on the flag alone was circular:
    #    the bump raised the floors, the floors consumed the headroom, the headroom
    #    cap then zeroed the deficit, and the day carried a modifier saying "energy
    #    deficit" with no deficit in it. Two passes, because the bump feeds back into
    #    the headroom that decides whether it should have been applied at all.
    p_lo_kg, p_hi_kg = PROTEIN_G_PER_KG[day_type]
    # ONE RULE: a deficit exists only when the window ahead is EASY or REST. The RHR
    # guard is a separate veto, not part of the tier test - it is a safety ceiling.
    deficit_tier_ok = band in (BAND_EASY_AHEAD, BAND_REST_AHEAD)
    deficit_possible = deficit_enabled and deficit_tier_ok and not rhr_guard_active
    if deficit_possible:
        # Probe with the BUMPED floor, not the base one: the question is whether a
        # deficit survives once the bump is paid for. Probing with the base floor
        # said yes on a recovery day, applied the bump, and the bump then consumed
        # the last of the headroom - leaving the modifier claiming a deficit that
        # had been capped to zero.
        probe_low = (p_lo_kg + PROTEIN_DEFICIT_BUMP_G_PER_KG) * rolling_weight
        probe_floors = (probe_low * 4 + FAT_FLOOR_G_PER_KG * rolling_weight * 9
                        + DAY_TYPES[day_type]["carb_g_per_kg"][0] * rolling_weight * 4)
        probe_room = maintenance - probe_floors - FAT_ZONE_MIN_WIDTH_G * 9
        if probe_room > 0:
            p_lo_kg += PROTEIN_DEFICIT_BUMP_G_PER_KG
            modifiers.append("protein floor raised: energy deficit, protecting lean mass")
    if yesterday_type == "long_ride":
        p_lo_kg = max(p_lo_kg, PROTEIN_POST_LONG_FLOOR_G_PER_KG)
        modifiers.append("protein floor raised: yesterday was a long ride")
    p_low, p_high = p_lo_kg * rolling_weight, p_hi_kg * rolling_weight

    # 2. carbs: PRESCRIBED from the demand ahead. Race week keeps its own, higher
    #    prescription, which also overrides the demand band rather than adding to it.
    if race_week:
        c_lo_kg, c_hi_kg = CARB_LOAD_G_PER_KG
        modifiers.append("carb load: within 3 days of the race")
    else:
        c_lo_kg, c_hi_kg = DEMAND_CARB_G_PER_KG[band]
        detail = (f" ({', '.join(demand['sessions'][:2])})" if demand["sessions"] else "")
        modifiers.append(f"carbs prescribed {c_lo_kg}-{c_hi_kg} g/kg: "
                         f"{BAND_LABEL[band]}{detail}")
    # 3. fat floor is known before the carb floor moves, because the carb easing below
    #    has to respect it.
    f_floor = FAT_FLOOR_G_PER_KG * rolling_weight

    c_low = c_lo_kg * rolling_weight
    carb_bound = "prescription"

    # The carb floor must never squeeze the fat zone flat. This cap is general, not
    # pre-long-only: the same collapse appeared three times in three different places
    # before it was generalised - once from the deficit taking the full headroom, once
    # from the pre-long carb lift, and once from the post-long-ride protein floor rising
    # to 2.2 g/kg on a low-energy day (fat 75-84, nine grams of range).
    #
    # What the easing may NOT go below is the day type's own physiological floor in
    # DAY_TYPES, discounted by CARB_EASE_FLOOR_FRACTION - not a fraction of the
    # prescription. The prescription is a demand target and on a low-energy day it can
    # sit two to three times above what the day's energy holds; bounding the easing at
    # 80% of THAT would leave a floor the day cannot pay for, and the arithmetic
    # downstream then produces a negative fat residual rather than a usable zone.
    if not race_week:
        cap = (maintenance - p_low * 4 - (f_floor + FAT_ZONE_MIN_WIDTH_G) * 9) / 4
        hard_min = (DAY_TYPES[day_type]["carb_g_per_kg"][0] * rolling_weight
                    * CARB_EASE_FLOOR_FRACTION)
        if c_low > cap:
            eased = max(cap, hard_min)
            if eased < c_low:
                modifiers.append(
                    f"carb floor eased to {eased / rolling_weight:.1f} g/kg to leave "
                    f"fat some room")
                c_low = eased
                carb_bound = "energy"
            if cap < hard_min:
                # Easing stopped at its own limit, so the day genuinely does not fit.
                # Say so rather than starving carbs until the arithmetic closes.
                warnings.append(
                    f"this day does not fit: protein {p_low:.0f} g plus a "
                    f"{c_low / rolling_weight:.1f} g/kg carb floor leaves too little "
                    f"for the fat floor")

    floors_kcal = p_low * 4 + f_floor * 9 + c_low * 4
    headroom = max(0.0, maintenance - floors_kcal)
    # Reserve fat-zone width, or the zone collapses to a point. See FAT_ZONE_MIN_WIDTH_G.
    allowable = max(0.0, headroom - FAT_ZONE_MIN_WIDTH_G * 9)

    deficit = 0.0
    if deficit_enabled:
        # The reason is derived from the ONE rule, not from a second list of gates. It
        # must always be present when deficit_enabled and the deficit is nil, or the
        # athlete sees a deficit setting that silently does nothing.
        if rhr_guard_active:
            warnings.append("deficit suppressed: resting HR elevated, holding maintenance")
        elif race_week:
            warnings.append("deficit suppressed: carb loading for the race")
        elif is_long:
            warnings.append("deficit suppressed: long session day, fuelling takes priority")
        elif pre_long:
            # A deficit here fights the prescription already applied: carbs have been
            # set at 8-10 g/kg to arrive at tomorrow's session glycogen-loaded, and
            # cutting calories on the same day works against it.
            warnings.append("deficit suppressed: topping glycogen for tomorrow's long session")
        elif key_ahead:
            named = (f" ({demand['sessions'][0]})" if demand["sessions"] else "")
            warnings.append(f"deficit suppressed: quality session in the window"
                            f"{named}, fuelling takes priority")
        else:
            uncapped = deficit_pct * maintenance
            deficit = min(uncapped, allowable) + correction_kcal
            deficit = max(0.0, min(deficit, allowable))
            if 0 < deficit < DEFICIT_MIN_MEANINGFUL_KCAL:
                warnings.append(
                    f"deficit dropped: only {deficit:.0f} kcal of room, which is inside "
                    f"the error on the inputs")
                deficit = 0.0
            if deficit < uncapped - 1:
                warnings.append(
                    f"deficit capped at {deficit:.0f} kcal (from {uncapped:.0f}): the "
                    f"protein, fat and carb floors leave only {allowable:.0f} kcal of room")
    target_kcal = maintenance - deficit

    # Race week inverts the whole calculation. A 10-12 g/kg carb load is a
    # PRESCRIPTION, and at 83 kg it is 3,300-4,000 kcal of carbohydrate alone, so it
    # cannot be fitted inside a maintenance target: the energy has to follow the
    # carbs. Deriving the target from maintenance here produced a day that did not
    # close by 1,470 kcal and a collapsed fat zone. The resulting surplus is expected
    # and is exactly why the spec suppresses the weight display during the load: the
    # 1-2 kg of glycogen-bound water reads as fat gain on a BIA scale.
    carb_load_surplus = 0
    if race_week:
        load_mid = sum(CARB_LOAD_G_PER_KG) / 2 * rolling_weight
        target_kcal = p_low * 4 + f_floor * 9 + load_mid * 4
        carb_load_surplus = round(target_kcal - maintenance)
        modifiers.append(
            f"target driven by the carb load, not maintenance: "
            f"{carb_load_surplus:+d} kcal against maintenance")

    # 4. the carbs the day's ENERGY can hold at the floors. This is the old residual
    #    figure, kept as the cross-check rather than as the answer.
    carbs_energy_allows = (target_kcal - p_low * 4 - f_floor * 9) / 4

    # 5. fat: floor sourced, ceiling either GI-bound, g/kg, energy-share or residual -
    #    and the basis has to say WHICH, because four different mechanisms can produce
    #    the same number and the athlete cannot tell them apart from the figure alone.
    #
    #    GI governs whenever there is a hard session in the window, today or tomorrow:
    #    fat slows gastric emptying and the carbohydrate is the point of those days. On
    #    easy and rest windows there is no GI reason to refuse fat, and holding it at
    #    1.2 g/kg on a large day strands energy nothing else can take.
    gi_governs = is_long or pre_long or key_ahead or race_week
    f_ceiling = FAT_CEILING_G_PER_KG * rolling_weight
    fat_bound = "g-kg"
    fat_basis = (f"g-kg bound: {FAT_FLOOR_G_PER_KG}-{FAT_CEILING_G_PER_KG} g/kg "
                 f"(floor sourced, ceiling practice)")
    if race_week and f_ceiling > FAT_CEILING_RACE_WEEK_G:
        f_ceiling, fat_bound = FAT_CEILING_RACE_WEEK_G, "GI"
        fat_basis = "GI bound: race week ceiling (reasoned)"
        modifiers.append("fat ceiling tightened: race week")
    elif (pre_long or key_ahead) and f_ceiling > FAT_CEILING_PRE_LONG_G:
        # Applies to a quality session ahead as well as a long one. The GI argument does
        # not care which kind of hard session it is, and the pre_long flag alone missed
        # every threshold and VO2 day.
        f_ceiling, fat_bound = FAT_CEILING_PRE_LONG_G, "GI"
        fat_basis = f"GI bound: {FAT_CEILING_PRE_LONG_G} g with a hard session ahead (reasoned)"
        modifiers.append(f"fat ceiling tightened: {BAND_LABEL[band]}")
    elif not gi_governs:
        share_ceiling = FAT_SHARE_TARGET * target_kcal / 9
        if share_ceiling > f_ceiling:
            f_ceiling, fat_bound = share_ceiling, "share"
            fat_basis = (f"share bound: {FAT_SHARE_TARGET:.0%} of the "
                         f"{target_kcal:.0f} kcal target, above the "
                         f"{FAT_CEILING_G_PER_KG} g/kg figure")
        # And never past FAT_SHARE_MAX of the day, which is the crowding argument: on a
        # small day 1.2 g/kg is itself more than a third of the energy. Never below the
        # floor, which is a safety figure and not subject to the share argument.
        share_cap = max(f_floor, FAT_SHARE_MAX * target_kcal / 9)
        if share_cap < f_ceiling:
            f_ceiling, fat_bound = share_cap, "share"
            fat_basis = (f"share bound: capped at {FAT_SHARE_MAX:.0%} of the "
                         f"{target_kcal:.0f} kcal target")

    # Energy left for fat once protein and the PRESCRIBED carb floor are paid for.
    fat_room_g = (target_kcal - p_low * 4 - c_low * 4) / 9
    f_high = max(f_floor, min(f_ceiling, fat_room_g))
    if fat_room_g < f_ceiling - 0.05:
        fat_bound = "residual"
        fat_basis = (f"residual bound: {max(0.0, fat_room_g):.0f} g of energy left after "
                     f"protein and the prescribed carbs, under the {f_ceiling:.0f} g ceiling")
    if fat_room_g < f_floor:
        warnings.append(
            f"no calorie room for the fat floor: the protein and carb floors leave "
            f"{fat_room_g:.0f} g against a {f_floor:.0f} g floor")

    # 6. the carb ZONE. The low is the prescription (eased above if the day's energy
    #    could not hold it); the high is the prescription's high, clamped to what the
    #    energy allows so the top of the zone is reachable without breaching the fat
    #    floor. Carbs are no longer the shock absorber, so the day does not close by
    #    construction at the top - the cross-check below is what makes that visible
    #    instead of silent.
    c_high = max(c_low, min(c_hi_kg * rolling_weight, carbs_energy_allows))
    c_derived_low = c_low
    if race_week:
        # Race week is the one case where the energy follows the carbs rather than the
        # reverse, so the prescription's high stands whatever the residual says.
        c_high = max(c_high, c_hi_kg * rolling_weight)
    c_per_kg_low = c_derived_low / rolling_weight
    c_per_kg_high = c_high / rolling_weight
    carb_basis = (f"demand band {c_lo_kg}-{c_hi_kg} g/kg ({BAND_LABEL[band]}); "
                  f"lands {c_per_kg_low:.1f}-{c_per_kg_high:.1f} g/kg")
    if race_week:
        carb_basis = (f"race-week load {c_lo_kg}-{c_hi_kg} g/kg (prescription); "
                      f"lands {c_per_kg_low:.1f}-{c_per_kg_high:.1f} g/kg")
    elif carb_bound == "energy":
        carb_basis = (f"energy bound: the demand band wanted {c_lo_kg}-{c_hi_kg} g/kg, "
                      f"the day holds {c_per_kg_low:.1f}-{c_per_kg_high:.1f} g/kg")

    # 7. the cross-check, in two directions with two different causes - and therefore two
    #    different lists, because a warning that fires on every pre-long recovery day is a
    #    warning the athlete learns to ignore.
    # Measured against the protein FLOOR, not its high. Protein's high is not a ceiling -
    # eating past it is explicitly not an event - so counting it as absorbing capacity
    # would let a day claim to add up only on the assumption that he eats 208 g of protein
    # rather than the 183 g he was actually asked for. The floors are the instruction.
    unallocated = target_kcal - (p_low * 4 + c_high * 4 + f_high * 9)
    if not race_week:
        want_low = c_lo_kg * rolling_weight
        if carbs_energy_allows < want_low * (1 - CARB_DEMAND_TOLERANCE):
            # The day cannot hold its own prescription. Not an input error: a rest day
            # before a long ride has ~2,500 kcal of maintenance and the band wants
            # ~2,700 kcal of carbohydrate alone. A MODIFIER, beside the easing line.
            modifiers.append(
                f"energy maths and demand bands disagree by "
                f"{(want_low - carbs_energy_allows) * 4:.0f} kcal: the {BAND_LABEL[band]} "
                f"band wants {c_lo_kg}-{c_hi_kg} g/kg and this day's energy holds "
                f"{carbs_energy_allows / rolling_weight:.1f} g/kg at maintenance")
        elif unallocated > DAY_UNALLOCATED_WARN_FRACTION * target_kcal:
            # The other direction, and tested across the WHOLE day rather than on carbs
            # alone: with carbs prescribed rather than emergent, some energy routinely
            # sits above the carb band and the protein and fat ranges absorb it. That is
            # ordinary. What is NOT ordinary is a day whose energy cannot be allocated
            # even at the top of every zone, and the cause is near enough always the
            # session energy: a day with this much training in it does not have an easy
            # window ahead of it.
            warnings.append(
                f"energy maths and demand bands disagree by {unallocated:.0f} kcal; check "
                f"activity calories - {maintenance:.0f} kcal of maintenance against "
                f"{BAND_LABEL[band]} carbs of {c_lo_kg}-{c_hi_kg} g/kg")

    # 8. fibre: a target most days, a CEILING before hard sessions and at the race.
    #    Same day type can carry opposite bias, decided purely by the demand ahead.
    if race_week:
        fb_low, fb_high, fb_bias = RACE_WEEK_FIBRE_G[0], RACE_WEEK_FIBRE_G[1], BIAS_CEILING
        modifiers.append("fibre ceiling: race week")
    elif pre_long:
        fb_low, fb_high, fb_bias = 0, LOW_RESIDUE_CEILING_G, BIAS_CEILING
        modifiers.append("fibre flipped to a ceiling: long session tomorrow")
    elif is_long:
        fb_low, fb_high, fb_bias = 0, LOW_RESIDUE_CEILING_G, BIAS_CEILING
    elif key_ahead:
        # Looser than the pre-long ceiling on purpose. A threshold session is an hour of
        # hard work, not five hours of splanchnic hypoperfusion, so the residue argument
        # is real but weaker - and fibre is a weekly job that a 20 g ceiling twice a week
        # would quietly make unreachable.
        fb_low, fb_high, fb_bias = 0, FIBRE_CEILING_KEY_AHEAD_G, BIAS_CEILING
        modifiers.append("fibre flipped to a ceiling: quality session in the window")
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
        "carb_load_surplus_kcal": carb_load_surplus,
        "protein_g": _zone(p_low, p_high, BIAS_FLOOR,
                           f"{p_lo_kg:.1f}-{p_hi_kg:.1f} g/kg bodyweight, flexing with load"),
        "fat_g": dict(_zone(f_floor, f_high, BIAS_BAND, fat_basis),
                      bound=fat_bound,
                      kcal_share=_kcal_share(f_floor, f_high, 9, target_kcal)),
        "carb_g": dict(_zone(c_derived_low, c_high, BIAS_BAND, carb_basis),
                       bound=carb_bound,
                       kcal_share=_kcal_share(c_derived_low, c_high, 4, target_kcal)),
        # What the window ahead actually is, and which sessions decided it. The zone
        # numbers are unreadable without it: 8-12 g/kg of carbohydrate and a suppressed
        # deficit only make sense once the page can name the session they are for.
        "demand_ahead": {"tier": demand["tier"], "band": band, "when": demand["when"],
                         "sessions": demand["sessions"],
                         "carb_g_per_kg": [c_lo_kg, c_hi_kg],
                         "calendar_known": demand["calendar_known"],
                         "label": BAND_LABEL[band]},
        "carb_basis": carb_basis,
        "fat_basis": fat_basis,
        # PHASED, because the ceiling is about TIMING and the day total cannot say so.
        # Jamie: "can i have fibre after i have done my run? i understand low fibre before
        # but need to get it back at sometpoint?" - yes, and the model knew it while the
        # page did not: a 40 g dinner after the run read as 20 g over a ceiling, which is
        # the app telling him off for doing the right thing. On a day with a long session
        # of its own the ceiling applies UNTIL it is done; afterwards the ordinary floor
        # returns, because fibre is a weekly job and the residue reason has expired.
        "fibre_g": dict(_zone(fb_low, fb_high, fb_bias,
                              "ceiling before long sessions: residue and splanchnic flow"),
                        # An ORDINARY day's floor, not this day type's own entry: for a
                        # long day that entry IS the ceiling, which produced a "floor" of
                        # 0-20 - the same number twice, wearing the opposite label.
                        **({"after_session": _zone(EVERYDAY_FIBRE_G[0],
                                                   EVERYDAY_FIBRE_G[1],
                                                   BIAS_FLOOR,
                                                   "the residue reason expires once the "
                                                   "session is done; fibre is a weekly job"),
                            "phase_note": ("ceiling until the session is done, then back "
                                           "to the floor")}
                           if (session_today and not race_week) else {})),
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
    pattern from day_rules (2 long, 4 standard, 1 recovery).

    It calls zones() per day type rather than re-deriving the arithmetic. An earlier
    cut duplicated the floor and headroom maths here and immediately drifted: it
    priced the protein deficit bump unconditionally while zones() applies it only
    when headroom survives it, so recovery days were priced with a floor the engine
    never uses. This figure appears on the Peak tab AND in the bot, so the two must
    come from the same code path or the athlete sees two different projections."""
    mix = weekly_mix or {"long_ride": 1, "long_run": 1, "standard": 4, "recovery": 1}
    # Representative net training energy per day type. Approximations, and the only
    # approximations left in here: everything downstream comes from zones().
    NET_BY_TYPE = {"recovery": 0.0, "standard": 1446.0}
    weekly_deficit = 0.0
    per_day = {}
    for dtype, count in mix.items():
        if dtype in LONG_DAY_TYPES:
            per_day[dtype] = 0                          # never a deficit on long days
            continue
        z = zones(day_type=dtype, rolling_weight=current_kg, rmr=rmr,
                  sessions=[{"type": "Ride", "moving_time": 7200,
                             "calories": NET_BY_TYPE[dtype] + 2 * rmr / 24,
                             "average_watts": 200}] if NET_BY_TYPE[dtype] else [],
                  deficit_enabled=True, deficit_pct=deficit_pct,
                  tdee_multiplier=tdee_multiplier)
        per_day[dtype] = z["deficit_applied_kcal"]
        weekly_deficit += count * z["deficit_applied_kcal"]

    weeks = max(0, days_to_race) / 7.0
    loss_kg = weekly_deficit * weeks / KCAL_PER_KG_FAT
    projected = round(current_kg - loss_kg, 1)
    gap_kg = max(0.0, current_kg - target_kg)
    return {"gap_kg": round(gap_kg, 1),
            "projected_race_kg": projected,
            "projected_loss_kg": round(loss_kg, 1),
            "weekly_deficit_kcal": round(weekly_deficit),
            "deficit_by_day_type": per_day,
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


# --- what to reach for next (page spec, 10 Aug 2026) ------------------------

# Reference composition of an ordinary mixed meal, as a share of its calories. Used
# ONLY as a comparison: the point of the density table is that "40% of your remaining
# calories must be protein" means nothing until you know a normal meal is about 20%.
NORMAL_MEAL_KCAL_SHARE = {"protein_g": 0.20, "carb_g": 0.55, "fat_g": 0.25}
KCAL_PER_G = {"protein_g": 4, "carb_g": 4, "fat_g": 9, "fibre_g": 0}

# How far above the normal share a macro must sit before the callout calls it out.
DENSITY_HIGH = 1.4      # 40% denser than an ordinary meal
DENSITY_LOW = 0.5       # half an ordinary meal or less


def meal_requirement(totals: dict, z: dict, reserved: dict = None) -> dict:
    """What the rest of the day has to look like, computed here rather than in the page.

    Answers "what do I reach for next", which is a different question from "am I in
    trouble" and is NOT derived from pace deltas. Pace says where a macro sits relative
    to the clock; this says what the remaining calories have to be made of. A macro can
    be well ahead of pace and still need to dominate dinner.

    Everything the page shows is computed here on purpose. A rendering layer that does
    its own arithmetic produces plausible wrong numbers instead of visible errors, and
    the page has no way to signal that it guessed.

    Returns `headline` (the callout), `reason` (one line), and `macros` (the table)."""
    # `reserved` is fuel PRESCRIBED for a session that has not been taken yet. It is part
    # of the day's totals but it is not food he may eat now, so it comes out of the budget
    # the next meal is measured against - otherwise the day tells him to eat the run's
    # carbohydrate at lunch and then tells him to stop eating after the run, when recovery
    # is precisely when he should not.
    reserved = {k: float(v or 0) for k, v in (reserved or {}).items()}
    reserved_kcal = round(reserved.get("carb_g", 0) * 4 + reserved.get("protein_g", 0) * 4
                          + reserved.get("fat_g", 0) * 9)
    remaining_kcal = round((z.get("kcal_target") or 0) - (totals.get("kcal") or 0)
                           - reserved_kcal)
    out = {"remaining_kcal": remaining_kcal, "macros": {}, "headline": "", "reason": "",
           "at_target": remaining_kcal <= 0}
    if reserved_kcal:
        out["reserved_for_session"] = {**{k: round(v, 1) for k, v in reserved.items()
                                          if v},
                                       "kcal": reserved_kcal}

    for key in ("protein_g", "carb_g", "fat_g", "fibre_g"):
        zone = z.get(key)
        if not zone:
            continue
        # Counted as though the reserved fuel were already eaten: it is committed.
        eaten = float(totals.get(key) or 0) + reserved.get(key, 0.0)
        lo, hi, bias = zone["low"], zone["high"], zone["bias"]
        ceiling = bias == BIAS_CEILING
        # Against the zone MINIMUM, not the midpoint: a floor is satisfied at its floor,
        # and measuring to the middle would make a met floor look unmet.
        still = max(0.0, lo - eaten)
        headroom = None if bias == BIAS_FLOOR else max(0.0, hi - eaten)
        kpg = KCAL_PER_G[key]
        req_share = ((still * kpg / remaining_kcal)
                     if (remaining_kcal > 0 and kpg and still) else 0.0)
        normal = NORMAL_MEAL_KCAL_SHARE.get(key)
        ratio = (req_share / normal) if (normal and req_share) else 0.0
        # A macro with room LEFT can still need avoiding. Fat at 95 g against a 100 g
        # ceiling has met its floor, so a still-needed test alone called it "met" while
        # the next meal had 5 g of room - the spec's own "near zero fat" case. So the
        # headroom gets the same density treatment: its share of the remaining calories
        # against a normal meal's share.
        head_share = ((headroom * kpg / remaining_kcal)
                      if (headroom is not None and remaining_kcal > 0 and kpg) else None)
        head_ratio = (head_share / normal) if (head_share is not None and normal) else None
        if headroom is not None and headroom <= 0:
            # Already at or past the limit. Fibre at 21 g against a 20 g ceiling read
            # "limit", which understates a ceiling that has gone.
            density = "avoid"
        elif head_ratio is not None and head_ratio <= 0.25:
            density = "avoid"
        elif ceiling:
            density = "limit"
        elif not still:
            density = "met"
        elif ratio >= DENSITY_HIGH:
            density = "high"
        elif ratio and ratio <= DENSITY_LOW:
            density = "low"
        else:
            density = "normal"
        out["macros"][key] = {
            "eaten": round(eaten, 1), "zone_low": lo, "zone_high": hi, "bias": bias,
            "still_needed_g": round(still, 1),
            "headroom_g": None if headroom is None else round(headroom, 1),
            "required_share": round(req_share, 3) if req_share else 0.0,
            "headroom_share": None if head_share is None else round(head_share, 3),
            "normal_share": normal, "density": density,
            # Progress against the zone MINIMUM, capped for display only.
            "pct_of_floor": round(min(100.0, (eaten / lo * 100) if lo else 100.0), 1),
        }

    # The callout. Deterministic wording: the page must never phrase this itself.
    if remaining_kcal <= 0:
        out["headline"] = "You are at your energy target"
        out["reason"] = ("Anything else today sits on top of it. Protein and fibre are "
                         "still worth having if you are short.")
        return out

    m = out["macros"]
    name = {"protein_g": "protein", "carb_g": "carbs", "fat_g": "fat",
            "fibre_g": "fibre"}
    wants = [k for k, v in m.items()
             if v["density"] == "high" and k != "fibre_g"]
    avoids = [k for k, v in m.items() if v["density"] == "avoid"]
    # Below its floor with no urgency in the density terms. Still has to be SAID: an
    # earlier cut reported "every zone is on track" while fat sat under its floor.
    shorts = [k for k, v in m.items()
              if v["still_needed_g"] > 0 and v["bias"] != BIAS_CEILING
              and k not in wants]
    fibre = m.get("fibre_g") or {}
    fibre_short = fibre.get("still_needed_g", 0) > 0 and fibre.get("bias") != BIAS_CEILING
    fibre_ceiling = fibre.get("bias") == BIAS_CEILING

    parts = []
    if wants:
        wants.sort(key=lambda k: m[k]["required_share"] / (m[k]["normal_share"] or 1),
                   reverse=True)
        parts.append(", ".join(name[k] for k in wants))
    # Fibre on a ceiling day is described ONCE, as "low residue". Listing it in avoids
    # as well produced "near zero fibre; low residue", which says the same thing twice
    # and reads as two separate instructions.
    avoids_no_fibre = [k for k in avoids if not (k == "fibre_g" and fibre_ceiling)]
    if avoids_no_fibre:
        parts.append("near zero " + " and ".join(name[k] for k in avoids_no_fibre))
    if fibre_ceiling:
        parts.append("low residue")
    else:
        # Fibre reaches the HEADLINE only when it is the only thing outstanding. It
        # otherwise won by default on an empty day, headlining "Reach for fibre" while
        # 183 g of protein was the actual gap - the callout has to name what matters
        # most, and nothing is unusually dense at the start of a day.
        others_short = [k for k in shorts if k != "fibre_g"] + wants
        if fibre_short and not others_short:
            parts.append("fibre")

    if parts:
        out["headline"] = "Reach for " + "; ".join(parts)
    elif all(v["still_needed_g"] == 0 for v in m.values()):
        out["headline"] = ("Every zone is met" if remaining_kcal > 250
                           else "You are essentially there")
    elif shorts or wants:
        # Nothing is unusually dense, which is itself the answer: eat normally.
        out["headline"] = "A normal balanced day from here"
    else:
        out["headline"] = "Anything balanced works from here"

    bits = []
    for k in wants:
        bits.append(f"{m[k]['still_needed_g']:.0f} g {name[k]} still to find in "
                    f"{remaining_kcal:,} kcal")
    for k in avoids_no_fibre:
        hr = m[k].get("headroom_g")
        bits.append(f"{name[k]} has {hr:.0f} g of room left"
                    if hr else f"{name[k]} is at its ceiling")
    for k in shorts:
        if k == "fibre_g":
            continue
        bits.append(f"{m[k]['still_needed_g']:.0f} g {name[k]} under its floor")
    if fibre_short:
        bits.append(f"{fibre['still_needed_g']:.0f} g fibre to go")
    if fibre_ceiling and fibre.get("headroom_g") is not None:
        hr = fibre["headroom_g"]
        bits.append(f"fibre ceiling has {hr:.0f} g left" if hr
                    else f"fibre is at its {fibre['zone_high']:.0f} g ceiling")
    out["reason"] = ("; ".join(bits) if bits
                     else f"{remaining_kcal:,} kcal left and every zone is met")
    return out


# --- intervals.icu weight ingest (Jamie's call, 10 Aug 2026) -----------------

# How far below the running morning baseline a reading has to sit before it is treated
# as a sweat-rate weigh-in rather than a morning weight.
#
# This exists because ICU stores ONE UNTIMESTAMPED weight per day, so morning and
# post-session readings are indistinguishable by time. They are separable by physics:
# Jamie's own series runs 83.10, 80.58, 81.65, 84.89, 81.59, 84.00 over a fortnight, and
# nobody loses 3.3 kg of fat in three days. A drop that size is fluid.
#
# 1.5 kg is chosen against that series: it catches the 80.58 and 81.59 readings and
# recovers a morning-only mean of ~83.4 kg, which matches the ~83.3 kg computed by hand
# from timestamped data. Tighter (1.2) over-rejects and drifts the mean up; looser (2.0)
# lets the 80.58 through and drags it down. A genuine fat-loss trend moves ~0.2 kg a
# week, so nothing real is ever rejected by this.
SWEAT_DROP_KG = 1.5
ICU_BASELINE_DAYS = 14


def classify_icu_weights(rows, existing_by_date=None) -> list:
    """Tag each intervals.icu weight as morning or session_sweat, in date order.

    Returns [{date, value, tag, reason, source}] ready for the store. ICU is the best
    place these land - the scale syncs there automatically and Jamie weighs there
    anyway - but the series MIXES morning weights with sweat-rate weigh-ins, and a
    post-session reading in the rolling mean reads as progress that did not happen. The
    deficit is driven off that mean, so this filter is load-bearing rather than tidy.

    `existing_by_date` maps an ISO date to a weight the athlete logged DIRECTLY (via the
    bot, timestamped, so its provenance is known). Those always win: a known morning
    reading beats an inferred one, and the ICU value for that date is skipped entirely
    rather than second-guessed.

    The baseline is the median of ACCEPTED morning readings in the trailing window, not
    of everything. Including rejected readings would drag the baseline down and let the
    next sweat reading through - the same self-defeating loop the RHR guard avoids by
    excluding the days under test."""
    existing = existing_by_date or {}
    parsed = []
    for w in rows or []:
        d = _as_date(w.get("id") or w.get("date"))
        val = w.get("weight") or w.get("weight_kg")
        if d and val:
            parsed.append((d, float(val), w.get("bodyFat") or w.get("body_fat_pct")))
    parsed.sort()

    out, accepted = [], []
    for d, val, fat in parsed:
        iso = d.isoformat()
        if iso in existing:
            out.append({"date": iso, "value": val, "tag": "superseded",
                        "reason": "athlete logged a timestamped weight for this day",
                        "source": "intervals.icu", "body_fat_pct": fat})
            continue
        window = [v for dd, v in accepted if 0 <= (d - dd).days <= ICU_BASELINE_DAYS]
        baseline = None
        if window:
            s = sorted(window)
            mid = len(s) // 2
            baseline = s[mid] if len(s) % 2 else (s[mid - 1] + s[mid]) / 2
        if baseline is not None and val < baseline - SWEAT_DROP_KG:
            out.append({"date": iso, "value": val, "tag": "session_sweat",
                        "reason": f"{baseline - val:.1f} kg below the {baseline:.1f} kg "
                                  f"morning baseline, which is fluid rather than fat",
                        "source": "intervals.icu", "body_fat_pct": fat})
            continue
        accepted.append((d, val))
        out.append({"date": iso, "value": val, "tag": "morning",
                    "reason": ("first reading, nothing to compare against"
                               if baseline is None
                               else f"within {SWEAT_DROP_KG} kg of the "
                                    f"{baseline:.1f} kg baseline"),
                    "source": "intervals.icu", "body_fat_pct": fat})
    return out


# --- in-session fuelling, assessed separately (Jamie's call, 10 Aug 2026) -----

def in_session_requirement(*, session_minutes: float, carbs_in_session_g: float,
                           target_g_hr: float, alert_g_hr: float = None,
                           sport: str = "") -> dict | None:
    """Assess fuel taken DURING a session against a g/hr target, on its own terms.

    Why this exists, in Jamie's words: "I can over carb in the day and under in the run
    and it looks fine." He is right, and it was a real blind spot. In-session fuel was
    TAGGED and PROTECTED but never ASSESSED, so it counted toward the day's carb zone
    and a satisfied day total could hide a badly under-fuelled long run. A day figure
    and a session figure answer different questions: the day is an energy budget, the
    session is a delivery RATE, and a rate cannot be rescued by eating more at dinner.

    `target_g_hr` is passed in rather than computed here, because the prescription
    already exists in ironman-analysis/primitives/nutrition.py as a gap-closing ramp -
    fuel_target for rides, run_fuel_target for runs, which is ceilinged near 60 g/hr and
    NOT the 90 g/hr race-bike figure. Restating either would let the two drift.

    Returns None for a session too short to need fuelling, so nothing is flagged for a
    45-minute swim."""
    hours = (session_minutes or 0) / 60.0
    if hours <= 0 or session_minutes < 90:
        return None
    actual = (carbs_in_session_g or 0) / hours
    required = target_g_hr * hours
    alert = alert_g_hr if alert_g_hr is not None else target_g_hr * 0.85
    if actual >= target_g_hr:
        verdict = "on_target"
    elif actual >= alert:
        verdict = "acceptable"
    else:
        verdict = "under"
    return {
        "sport": sport,
        "session_minutes": round(session_minutes),
        "carbs_g": round(carbs_in_session_g or 0),
        "g_per_hr": round(actual, 1),
        "target_g_hr": round(target_g_hr, 1),
        "alert_g_hr": round(alert, 1),
        "required_g": round(required),
        "shortfall_g": max(0, round(required - (carbs_in_session_g or 0))),
        "verdict": verdict,
        # A rate, not a budget: this cannot be made good later in the day, which is the
        # whole reason it is assessed apart from the day's carb zone.
        "basis": "gap-closing ramp from the athlete's recent logged sessions",
    }


def split_carbs(totals: dict) -> dict:
    """Day carbs split into in-session and out-of-session.

    The day zone legitimately covers BOTH - fuel eaten on a run is real energy - but the
    two must be visible apart, or a large day total reads as success while the session
    inside it was under-fuelled."""
    total = float(totals.get("carb_g") or 0)
    in_sess = float(totals.get("in_session_carb_g") or 0)
    return {"total_g": round(total, 1), "in_session_g": round(in_sess, 1),
            "out_of_session_g": round(max(0.0, total - in_sess), 1),
            "in_session_share": (round(in_sess / total, 3) if total else 0.0)}
