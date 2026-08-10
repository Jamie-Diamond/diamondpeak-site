#!/usr/bin/env python3
"""nutrition_engine.py - daily energy/macro targets, pacing and guardrails. PURE.

Step 1 of the nutrition tracker (spec v0.1, 10 Aug 2026; decisions in this
docstring supersede the spec where they differ). This module computes and
nothing else: no file I/O, no network, no ICU calls. Callers hand it activities,
wellness rows and the day's food entries; it hands back targets and flags. That
keeps it testable offline and lets both the nutrition bot and the Peak publish
step share one brain, the same split lib/engine.py made for the coach.

WHY THE RESTING SUBTRACTION IS NOT OPTIONAL
  target_kcal = base_tdee + net_session_kcal
  net_session_kcal = Σ(activity_kcal) − Σ(activity_hours) × rmr / 24
Device-reported activity calories already contain the resting energy the athlete
would have burned anyway during those hours. Adding raw activity kcal to a
full-day base double-counts by roughly 75–80 kcal per training hour, which at
Jamie's 12–16 hrs/week is a systematic 130–150 kcal/day overstatement - enough to
turn an intended deficit into maintenance without anyone noticing.

Note the input error this inherits: intervals.icu `calories` is kJ-derived and
sound for power-metered rides, but an HR-based estimate for runs and swims. The
target carries that uncertainty; `targets()` reports it in `kcal_confidence` so
the UI can say so rather than implying three significant figures.

DEFICIT IS ON - Jamie's explicit call, 10 Aug 2026
The spec shipped the deficit off (§5.1: 9–11% body fat, essential-fat floor,
12–16 hrs/week, RHR spike to 71/76 in early August). Jamie was shown that
reasoning and the arithmetic - 83.3→79.0 kg is ~33,000 kcal, and −350/day on
non-long days over 40 days buys ~1.4 kg, landing ~81.9 not 79.0 - and chose to
enable it anyway to chase `race_weight_kg`. Honoured. But three limits are
ceilings, not preferences, and hold regardless of the flag:
  1. never applied to long_run or long_ride days (fuelling a long session is not
     negotiable, and a deficit there costs the session that earns the fitness)
  2. suppressed entirely while the RHR guard is active (see rhr_guard) - a
     deficit stacked on an unresolved illness signal is the failure mode §10.2
     exists to prevent, and it already happened once in early August
  3. the low-energy-availability flag still fires at day close (§10.1) whether or
     not the deficit is deliberate

G/KG BASIS DIFFERS FROM THE FUELLING ENGINE, DELIBERATELY
plan_tools.py race-fuelling computes carbs/kg off profile `race_weight_kg`
(79.0), because it is planning for the body that will start the race. This module
uses the rolling 7-day mean of MORNING weights, because it is targeting the body
the athlete has today. The two will quote different g/kg figures for the same
day. That is correct for each purpose and must not be "fixed" by making them
agree; if you unify them, decide which question you are answering first.

The morning-only filter is structural, not cosmetic. Jamie weighs repeatedly on
long-ride days to measure sweat rate, and those readings sit 2–3 kg below morning
weight. Mixed into the mean they produce ~1.3 kg of standard deviation, and with
the deficit driven off rolling weight a post-ride 80.5 kg would read as 3 kg of
progress and silently distort the target. See rolling_weight_kg.

SODIUM HAS NO TARGET, ON PURPOSE
Jamie declined the (overdue) Precision Hydration sweat test - "anecdotally I'm a
salty sweater, that's what we have to work with". Self-reported saltiness
correlates only weakly with measured sweat [Na+], so the engine does NOT raise
the fuelling engine's 950 mg/L default on the anecdote. It reports a 950–1500
mg/L band tagged `assumed` and tracks intake against the low end. Descriptive.
In-session sodium already exists upstream as `nutrition_mg_sodium`; only DIETARY
sodium is new here, and this module never restates the in-session g/hr carb
targets either - those live in ironman-analysis/primitives/nutrition.py
(fuel_target / run_fuel_target) and are read, not duplicated.

FIBRE IS A CEILING SOMETIMES, AND THAT IS THE COUNTERINTUITIVE PART
High fibre is right on easy days and actively harmful before long sessions:
undigested residue plus reduced splanchnic blood flow is a leading cause of
race-day GI failure, and carb-loading on high-fibre food is mechanically
defeating. So the target flips to a ceiling the day BEFORE a long session, which
means looking one day ahead in the calendar, not just at today. `fibre_is_ceiling`
exists so the UI can render it as a limit rather than a progress bar - a red bar
on a low-fibre day would read as failure when it is compliance.
"""

from datetime import date, datetime, timedelta

# --- athlete-independent constants ------------------------------------------

KCAL_PER_KG_FAT = 7700          # standard energy density of adipose tissue
NEAT_TEF_MULTIPLIER = 1.35      # RMR → non-training TDEE (NEAT + thermic effect)

PROTEIN_G_FLAT = 180            # ~2.4 g/kg FFM. Deliberately does NOT scale with
                                # load - it is the one target meant to stay flat,
                                # which is why the Block view charts it.
FAT_FLOOR_G = 80
FAT_CEILING_G = 95              # soft: a warning, never a reduction suggestion

DEFICIT_KCAL_DEFAULT = 350      # spec range −300..−400; mid-point
PACE_DEVIATION_FLAG = 15        # percentage points ahead of the day's pace

SWEAT_NA_ASSUMED_LOW = 950      # mg/L - matches plan_tools --sweat-na default
SWEAT_NA_ASSUMED_HIGH = 1500    # mg/L - high end of the population range

MICRO_WATCH = ("iron", "vitamin_d", "b12", "magnesium")

# Collagen protein is excluded from the protein total: ~15 g with no tryptophan
# and very little leucine, so counting it would inflate the one metric that is
# supposed to hold constant. It gets its own field, never the protein field.
NON_COUNTING_PROTEIN_SOURCES = ("collagen", "gelatin", "gelatine")

# --- day types (spec §4) -----------------------------------------------------
# carb_g_per_kg is a CROSS-CHECK band, not the source of the carb figure. Carbs
# are the remainder after protein and fat; if the remainder falls outside this
# band the engine warns rather than silently accepting, because a remainder built
# on misreported activity kcal can go absurd.

DAY_TYPES = {
    "recovery":  {"carb_g_per_kg": (3, 4), "fibre_g": (40, 45), "kcal_sanity": (2500, 2900)},
    "standard":  {"carb_g_per_kg": (5, 6), "fibre_g": (30, 35), "kcal_sanity": (3000, 3800)},
    "long_run":  {"carb_g_per_kg": (7, 8), "fibre_g": (0, 20),  "kcal_sanity": (4000, 4600)},
    "long_ride": {"carb_g_per_kg": (8, 9), "fibre_g": (0, 20),  "kcal_sanity": (5200, 6100)},
}
LONG_DAY_TYPES = ("long_run", "long_ride")

LOW_RESIDUE_CEILING_G = 20      # day before a long session
RACE_WEEK_FIBRE_G = 12          # spec says 10–15; take the mid-point

RIDE_SPORTS = ("Ride", "VirtualRide", "GravelRide", "MountainBikeRide")
RUN_SPORTS = ("Run", "VirtualRun", "TrailRun")


def _as_date(v):
    """Accept date, datetime or an ISO-ish string. Returns None on anything else.

    ICU hands back both `2026-08-10` and `2026-08-10T06:03:00` shapes depending on
    the endpoint, so every date entering this module goes through here."""
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


# --- anthropometrics --------------------------------------------------------

def age_years(dob, on: date | None = None) -> int:
    """Whole years, birthday-aware. Mifflin-St Jeor is sensitive enough at 5
    kcal/year that rounding to the nearest year is fine but truncating a
    fractional age is not."""
    dob = _as_date(dob)
    on = on or date.today()
    if not dob:
        raise ValueError("dob is required to compute age")
    return on.year - dob.year - ((on.month, on.day) < (dob.month, dob.day))


def mifflin_st_jeor(weight_kg: float, height_m: float, dob, sex: str = "M",
                    on: date | None = None) -> float:
    """RMR, kcal/day. Male: 10×kg + 6.25×cm − 5×age + 5 (female: −161).

    Recompute on weight change rather than caching - at 83.3 kg this returns
    ~1,845, and a 4 kg swing moves it 40 kcal, which is inside the noise of the
    activity-kcal input but not worth carrying as a stale constant."""
    if not weight_kg or not height_m:
        raise ValueError("weight_kg and height_m are required")
    base = 10 * weight_kg + 6.25 * (height_m * 100) - 5 * age_years(dob, on)
    return base + (5 if (sex or "M").upper().startswith("M") else -161)


def base_tdee(rmr: float, multiplier: float = NEAT_TEF_MULTIPLIER) -> float:
    """Non-training TDEE: RMR plus NEAT and the thermic effect of food. Training
    energy is added separately by net_session_kcal, never folded in here."""
    return rmr * multiplier


def rolling_weight_kg(measurements, on: date | None = None, days: int = 7):
    """Mean of MORNING weights over the trailing `days`. None if none available.

    Only the first weight reading of each day after 04:00 local counts; anything
    later on the same date is a sweat-rate weigh-in and is excluded. Entries may
    arrive pre-tagged (`tag` == 'session_sweat'), in which case the tag wins and
    no time reasoning is needed.

    Never decide off a single reading: individual scale readings carry roughly
    ±2.5 kg of 95% uncertainty, which is half the gap this athlete is chasing."""
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
        # keep the earliest reading of the day
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


def classify_day(sessions, athlete_race_date=None, on: date | None = None) -> str:
    """Day type from the day's sessions (planned events or completed activities).

    Longest single session of each discipline decides it, not the daily total: two
    90-minute rides is a standard day, one 4-hour ride is not. Ride is checked
    before run so a brick with a 4-hour ride and a 30-minute run reads long_ride,
    which is the correct fuelling and fibre call."""
    ride_max = max([_duration_min(s) for s in sessions or []
                    if _sport(s) in RIDE_SPORTS] or [0])
    run_max = max([_duration_min(s) for s in sessions or []
                   if _sport(s) in RUN_SPORTS] or [0])
    total = sum(_duration_min(s) for s in sessions or [])

    if ride_max >= 240:
        return "long_ride"
    if run_max >= 120:
        return "long_run"
    if total > 120:
        return "standard"
    if total >= 60:
        return "standard"
    return "recovery"


def net_session_kcal(sessions, rmr: float) -> tuple[float, str]:
    """Training energy above resting, plus a confidence label.

    Returns (kcal, confidence). Confidence is 'measured' only when every session
    contributing energy is power-metered (rides with average_watts); otherwise
    'estimated', because ICU derives run and swim calories from heart rate at
    roughly ±15–20%. The caller surfaces this so the day's target is not read as
    exact."""
    gross = 0.0
    hours = 0.0
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


def fibre_target(day_type: str, tomorrow_type: str | None = None,
                 days_to_race: int | None = None) -> tuple[int, bool]:
    """Returns (grams, is_ceiling).

    Three cases in priority order: race week is a ceiling, the day before a long
    session is a ceiling regardless of today's own type, and a long day is itself
    a ceiling. Everything else is a target to reach. The boolean is what stops the
    UI drawing a progress bar that reads failure as failure when it is compliance."""
    if days_to_race is not None and 0 <= days_to_race <= 7:
        return RACE_WEEK_FIBRE_G, True
    if tomorrow_type in LONG_DAY_TYPES:
        return LOW_RESIDUE_CEILING_G, True
    if day_type in LONG_DAY_TYPES:
        return LOW_RESIDUE_CEILING_G, True
    lo, hi = DAY_TYPES[day_type]["fibre_g"]
    return int((lo + hi) / 2), False


# --- the main target computation --------------------------------------------

def targets(*, day_type: str, rolling_weight: float, rmr: float,
            sessions=None, tomorrow_type: str | None = None,
            days_to_race: int | None = None,
            deficit_enabled: bool = False,
            deficit_kcal: int = DEFICIT_KCAL_DEFAULT,
            rhr_guard_active: bool = False,
            tdee_multiplier: float = NEAT_TEF_MULTIPLIER) -> dict:
    """The day's numbers, plus every warning the inputs justify.

    Carbs are the REMAINDER after protein and fat, then cross-checked against the
    day-type g/kg band. A remainder outside the band is surfaced as a warning and
    the figure is still returned - clamping it would hide a bad activity-kcal
    input, which is the thing the warning exists to catch.

    FAT IS PINNED TO THE FLOOR when setting targets, and FAT_CEILING_G plays no
    part in the arithmetic. The spec gives fat a floor of 80 g and a soft ceiling
    of 95 g, but only one of the two can coexist with "carbs are the remainder":
    pinning fat higher on a big day would take those calories straight out of the
    carbs that fuel the session. So the floor sets the target and the ceiling is
    advisory only, applied to LOGGED intake as a warning. The consequence to know:
    on a long_ride day the carb figure absorbs the entire remainder above 80 g of
    fat, which is why the g/kg cross-check is doing the real sanity work there
    rather than the kcal range."""
    if day_type not in DAY_TYPES:
        raise ValueError(f"unknown day_type {day_type!r}")

    net, kcal_confidence = net_session_kcal(sessions, rmr)
    maintenance = base_tdee(rmr, tdee_multiplier) + net

    warnings = []
    deficit_applied = 0
    if deficit_enabled:
        if day_type in LONG_DAY_TYPES:
            warnings.append("deficit suppressed: long session day, fuelling takes priority")
        elif rhr_guard_active:
            warnings.append("deficit suppressed: resting HR elevated, holding maintenance")
        else:
            deficit_applied = int(deficit_kcal)

    target_kcal = round(maintenance - deficit_applied)

    lo, hi = DAY_TYPES[day_type]["kcal_sanity"]
    if not (lo * 0.85 <= target_kcal <= hi * 1.15):
        warnings.append(
            f"target {target_kcal} kcal is outside the {day_type} sanity range "
            f"{lo}–{hi}; check activity calories")

    fat_g = FAT_FLOOR_G
    carb_kcal = target_kcal - (PROTEIN_G_FLAT * 4) - (fat_g * 9)
    carb_g = max(0, round(carb_kcal / 4))

    band_lo, band_hi = DAY_TYPES[day_type]["carb_g_per_kg"]
    carb_per_kg = round(carb_g / rolling_weight, 2) if rolling_weight else None
    if carb_per_kg is not None and not (band_lo <= carb_per_kg <= band_hi):
        warnings.append(
            f"carb remainder {carb_g} g = {carb_per_kg} g/kg, outside the "
            f"{day_type} band {band_lo}–{band_hi} g/kg")

    fibre_g, fibre_is_ceiling = fibre_target(day_type, tomorrow_type, days_to_race)

    return {
        "day_type": day_type,
        "kcal_target": target_kcal,
        "kcal_maintenance": round(maintenance),
        "kcal_confidence": kcal_confidence,
        "net_session_kcal": net,
        "deficit_applied_kcal": deficit_applied,
        "protein_target_g": PROTEIN_G_FLAT,
        "fat_floor_g": FAT_FLOOR_G,
        "fat_ceiling_g": FAT_CEILING_G,
        "carb_target_g": carb_g,
        "carb_g_per_kg": carb_per_kg,
        "carb_band_g_per_kg": [band_lo, band_hi],
        "fibre_target_g": fibre_g,
        "fibre_is_ceiling": fibre_is_ceiling,
        "sodium_basis": {
            "sweat_na_mg_l": [SWEAT_NA_ASSUMED_LOW, SWEAT_NA_ASSUMED_HIGH],
            "confidence": "assumed",
            "note": "no sweat test; band not a target",
        },
        "weight_basis_kg": rolling_weight,
        "warnings": warnings,
    }


# --- pacing (spec §7) -------------------------------------------------------

def pacing(entries, tgt: dict) -> dict:
    """Front-load detection: is a macro running ahead of the day's overall pace?

    The problem this solves is spending the whole fat budget by lunch on
    individually sensible foods. `deviation` is percentage points of the macro's
    own consumption ahead of energy consumption, so it is scale-free.

    Suppressed entirely on long days and on any day containing in-session fuel:
    carbs legitimately spike mid-session while fat stays flat, which produces a
    permanent false positive. A flag that always fires is a flag nobody reads."""
    if tgt["day_type"] in LONG_DAY_TYPES or any(e.get("in_session") for e in entries or []):
        return {"suppressed": True, "reason": "long session day or in-session fuel present",
                "flags": []}

    kcal = sum(float(e.get("kcal") or 0) for e in entries or [])
    if not kcal or not tgt["kcal_target"]:
        return {"suppressed": False, "flags": []}
    reference = kcal / tgt["kcal_target"] * 100

    fields = {"protein": ("protein_g", "protein_target_g"),
              "carb": ("carb_g", "carb_target_g"),
              "fat": ("fat_g", "fat_floor_g"),
              "fibre": ("fibre_g", "fibre_target_g")}
    flags = []
    for name, (entry_key, target_key) in fields.items():
        target = tgt.get(target_key) or 0
        if not target:
            continue
        consumed = sum(float(e.get(entry_key) or 0) for e in entries or [])
        deviation = round(consumed / target * 100 - reference, 1)
        if deviation >= PACE_DEVIATION_FLAG:
            flags.append({
                "macro": name,
                "deviation_pp": deviation,
                "consumed": round(consumed, 1),
                "target": target,
                # A flag alone is not actionable: the cause is almost never one bad
                # item, it is four or five reasonable ones stacking.
                "contributors": rank_contributors(entries, entry_key),
            })
    return {"suppressed": False, "pace_reference_pct": round(reference, 1), "flags": flags}


def rank_contributors(entries, field: str, top: int = 5) -> list:
    """The day's entries ranked by their contribution to one macro. In-session fuel
    is labelled so the UI can show it while never offering it for reduction."""
    rows = [{"name": e.get("resolved_name") or e.get("raw_text") or "?",
             "amount": round(float(e.get(field) or 0), 1),
             "in_session": bool(e.get("in_session")),
             "protected": bool(e.get("in_session"))}
            for e in entries or [] if float(e.get(field) or 0) > 0]
    rows.sort(key=lambda r: r["amount"], reverse=True)
    return rows[:top]


# --- guardrails (spec §10) --------------------------------------------------

def underfuel_flag(entries, tgt: dict, rmr: float) -> dict | None:
    """Low energy availability at day close: consumed < RMR + half the training cost.

    Fires whether or not the deficit is deliberate. In lean male endurance athletes
    low energy availability suppresses testosterone, impairs recovery and raises
    infection risk, so this is the one check a deliberate deficit must not silence.
    Three or more in a week is the escalation the tracking page should shout about."""
    consumed = sum(float(e.get("kcal") or 0) for e in entries or [])
    floor = rmr + 0.5 * (tgt.get("net_session_kcal") or 0)
    if consumed >= floor:
        return None
    return {"type": "underfuel", "severity": "high",
            "consumed_kcal": round(consumed), "floor_kcal": round(floor),
            "shortfall_kcal": round(floor - consumed),
            "message": "Energy intake below the low-availability floor for today's training."}


def rhr_guard(wellness, on: date | None = None, baseline_days: int = 30,
              threshold: float = 1.10, consecutive: int = 2) -> dict:
    """Resting HR elevated above its own 30-day baseline for N consecutive days.

    Returns {'active': bool, ...}. When active, callers must force maintenance -
    see the deficit ceilings in the module docstring. Jamie's baseline is ~52 bpm
    and readings of 71 and 76 occurred in early August; a deficit through that is
    the exact stacking this guard exists to stop.

    The baseline deliberately excludes the days being tested, otherwise a
    sustained elevation drags its own reference upward and the guard stops firing
    precisely when it matters most.

    The tested window is the newest `consecutive` rows that EXIST, searched over a
    slightly wider calendar window, not the last `consecutive` calendar days. An
    earlier cut used the calendar days directly, which made the guard unfirable at
    consecutive >= 3: ICU's current-day wellness row is often absent or
    unfinalised, so the window could never hold enough rows and the guard silently
    never fired. A guard that cannot fire is worse than no guard, because it reads
    as coverage. Contiguity is still enforced (`span`), so two elevated readings
    five days apart do not count as consecutive.

    Reading an unfinalised row is safe here: `restingHR` is device-sourced and
    ICU does not recompute it. What `_wellness_row_finalized` protects is
    CTL/ATL/Form, which this guard never touches, so no finalisation filter is
    wanted - do not add one."""
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

    # Newest `consecutive` rows that exist, tolerating up to 2 missing days.
    search = [(d, v) for d, v in rows if (on - d).days <= consecutive + 1]
    tested = search[:consecutive]
    if len(tested) < consecutive:
        return {"active": False, "reason": "insufficient recent resting HR data"}

    span = (tested[0][0] - tested[-1][0]).days + 1
    if span > consecutive + 1:
        return {"active": False, "reason": "recent readings too scattered to call consecutive"}

    oldest_tested = tested[-1][0]
    baseline_pool = [v for d, v in rows
                     if 0 < (oldest_tested - d).days <= baseline_days]
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


def race_weight_projection(current_kg: float, target_kg: float, days_to_race: int,
                           deficit_kcal: int = DEFICIT_KCAL_DEFAULT,
                           deficit_day_fraction: float = 5 / 7) -> dict:
    """What the enabled deficit will actually deliver by race day.

    Exists so the app never shows a race-weight target it cannot reach. The
    deficit applies on roughly 5 days in 7 because long days are excluded, so the
    honest projection is well short of the gap: 83.3→79.0 over 40 days needs about
    −830 kcal every single day, which the ceilings block by design."""
    gap_kg = max(0.0, current_kg - target_kg)
    effective_days = max(0, days_to_race) * deficit_day_fraction
    loss_kg = (effective_days * deficit_kcal) / KCAL_PER_KG_FAT
    projected = round(current_kg - loss_kg, 1)
    return {"gap_kg": round(gap_kg, 1),
            "projected_race_kg": projected,
            "projected_loss_kg": round(loss_kg, 1),
            "reaches_target": projected <= target_kg,
            "shortfall_kg": round(max(0.0, projected - target_kg), 1),
            "required_daily_kcal_to_reach": (
                round(gap_kg * KCAL_PER_KG_FAT / max(1, days_to_race))
                if gap_kg else 0)}


# --- protein accounting -----------------------------------------------------

def counting_protein_g(entries) -> tuple[float, float]:
    """Returns (counting_g, non_counting_g).

    Collagen and gelatin are excluded from the protein total: ~15 g of protein
    with no tryptophan and very little leucine, so summing it would inflate the
    one target meant to stay near-constant. It is still stored and shown, just
    never added to the 180 g."""
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
    'unknown'. Plant diversity carries the breadth signal instead (see plants.py).
    """
    taken = {}
    for e in supplement_entries or []:
        name = (e.get("nutrient") or e.get("resolved_name") or "").lower().replace(" ", "_")
        for key in MICRO_WATCH:
            if key in name:
                taken[key] = {"state": "supplemented",
                              "dose": e.get("dose"), "unit": e.get("unit")}
    return {key: taken.get(key, {"state": "not_supplemented", "dose": None, "unit": None})
            for key in MICRO_WATCH}
