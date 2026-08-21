"""
public_sanitise.py — allow-list sanitiser for the athlete data that is published
to the PUBLIC GitHub Pages site (diamondpeak.uk).

WHY THIS IS AN ALLOW-LIST
=========================
The previous mechanism (`_strip_private` in refresh-site-data.py) was a DENY-list:
it popped a handful of known-bad keys and published whatever was left. That is
why body weight, HRV and resting HR were served publicly from 8 May 2026 — they
were simply not on the pop list. A deny-list publishes every new upstream field
by default; the failure mode is silent and permanent.

Everything here is the opposite. A field reaches a public file ONLY if it is
named explicitly in the spec below. A new key appearing in the Intervals.icu
payload, or a new field added by post_process(), is dropped by default and needs
a deliberate edit here (and an owner decision) to be published.

WHAT THE OWNER APPROVED FOR PUBLICATION (27 Jul 2026)
   per-session avg_hr, norm_power, ctl/atl/tsb and history, session names,
   dates, first names, race names/dates, rpe, feel, threshold power/pace.
   `feel` was on that list and is NOT published - see the withheld list below.
   heatAccl, heatProtocol and decouplingTrend are published as performance
   metrics on the orchestrator-s classification, not on an owner decision.

NEVER PUBLISH (owner explicit)
   weight_kg, hrv, rhr.

WITHHELD PENDING AN OWNER DECISION — present upstream, deliberately absent from
the spec below. Do not add any of these without an explicit decision:
   profile.lthr                          threshold HEART RATE (the approval
                                         covers threshold power/pace, not HR)
   ctlProjection.sick_week               illness window
   currentState.watchdog_flags           automated alerts, HRV/illness driven
   currentState.open_actions[]           free-text coaching actions
   profile.prev_race.notes               free text
   sessionLog[].notes                    free text
   sessionLog[].injury_pain_during       clinical
   sessionLog[].injury_pain_next_morning clinical
   sessionLog[].feel                     free text the athlete types; observed
                                         values carry symptoms, sleep and pain.
                                         Numeric rpe is published instead. This
                                         one overrides an explicit owner KEEP.
   *.activity_id                         Intervals.icu / Strava record id
                                         (re-identifying, links to a platform
                                         profile), and sessionLog[].logged_at
"""

import sys
from pathlib import Path

# Callers already put lib/ on sys.path before importing this module, but the
# sanitiser must not be the thing that breaks if one ever does not.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import profile_fields  # noqa: E402

# ── spec vocabulary ──────────────────────────────────────────────────────────
_SCALAR_TYPES = (str, int, float, bool)


class _Scalar:
    """A leaf. Copied only if it is a scalar or None; anything else is dropped."""


class Records:
    """A list of dicts. Each item is pruned to `spec`; non-dicts are dropped."""

    def __init__(self, spec):
        self.spec = spec


class Derived:
    """A leaf whose published value is `fn(value)`, not the value itself.

    Added 4 Aug 2026 for the threshold-pace fields. Some profile values carry a
    COACH-FACING prose caveat after the number (Kathryn's run threshold read
    "5:08/km - DERIVED working estimate ... NOT field-tested ... LTHR 191 remains
    the primary intensity control"), and a plain `S` published the whole note to a
    PUBLIC repo. `profile_fields` already exists to stop exactly that class of leak
    at the athlete-facing boundary; this puts the same extraction on the publishing
    boundary, so the note can be as long as the coach needs without leaking.
    """

    def __init__(self, fn):
        self.fn = fn


class Mapping:
    """A dict with data-driven keys (athlete slugs). Each value pruned to `spec`."""

    def __init__(self, spec):
        self.spec = spec


class ScalarList:
    """A flat list of scalars - e.g. ["swim", "bike", "run"].

    Series cannot express this: it requires every row to itself be a list, so a list
    of plain strings is dropped entirely. Added rather than reshaping the data into a
    dict-of-true purely to fit the existing vocabulary, because the shape the app wants
    should not be decided by the sanitiser. Still an allow-list: non-scalar members are
    dropped, so this can never become a route for an object to pass.
    """


class Series:
    """A list of [date, value, ...] rows of scalars — e.g. a CTL history.

    Rows that are not lists of scalars are dropped, so a future upstream change
    that turns a row into an object cannot smuggle fields through.
    """


S = _Scalar()
SERIES = Series()


def prune(value, spec):
    """Copy `value` into a new structure containing ONLY what `spec` names."""
    if isinstance(spec, _Scalar):
        return value if (value is None or isinstance(value, _SCALAR_TYPES)) else None
    if isinstance(spec, Derived):
        out = spec.fn(value)
        return out if (out is None or isinstance(out, _SCALAR_TYPES)) else None
    if isinstance(spec, ScalarList):
        if not isinstance(value, list):
            return None if value is None else []
        return [v for v in value if v is None or isinstance(v, _SCALAR_TYPES)]
    if isinstance(spec, Series):
        if not isinstance(value, list):
            return []
        rows = []
        for row in value:
            if isinstance(row, list) and all(
                c is None or isinstance(c, _SCALAR_TYPES) for c in row
            ):
                rows.append(list(row))
        return rows
    if isinstance(spec, Records):
        if not isinstance(value, list):
            return []
        return [prune(v, spec.spec) for v in value if isinstance(v, dict)]
    if isinstance(spec, Mapping):
        if not isinstance(value, dict):
            return {}
        return {str(k): prune(v, spec.spec) for k, v in value.items()}
    if isinstance(spec, dict):
        if not isinstance(value, dict):
            return {}
        return {k: prune(value[k], sub) for k, sub in spec.items() if k in value}
    raise TypeError(f"unsupported spec node: {spec!r}")


# ── specs ────────────────────────────────────────────────────────────────────
_SPORT_SERIES = {"Ride": SERIES, "Run": SERIES, "Swim": SERIES}

# Same three sports plus a Total across everything logged, which is the counterpart of
# overall CTL for the Hours chart. Total needs naming explicitly: this is a fixed-key
# spec, so a bucket that is not listed is silently dropped rather than published.
_DUR_SERIES = {"Total": SERIES, **_SPORT_SERIES}

# Publishes "5:08" out of "5:08/km - DERIVED working estimate ...", and leaves an
# already-clean value untouched.
_PACE_ONLY = Derived(profile_fields.pace)

_CTL_POINT = Records({"date": S, "ctl": S})

_PREDICTOR_ROW = {
    "label": S, "name": S, "ctl": S, "if": S,
    "swim_min": S, "t12_min": S, "bike_min": S, "bike_w": S,
    "run_min": S, "total_min": S,
}

TRAINING_DATA_SPEC = {
    "generated":   S,
    "resolvedFtp": S,
    # The athlete's weekly CTL ramp guide. A configured coaching threshold,
    # not a measurement - the app states it instead of hard-coding 5.
    "rampCap":     S,
    # This job's own cron schedule in words. Operational metadata, no physiology.
    "refreshCadence": S,

    # kpi: ctl/atl/tsb/ramp7d only. hrv and rhr are NOT named, so they cannot pass.
    "kpi": {"ctl": S, "atl": S, "tsb": S, "ramp7d": S},

    # profile: weight_kg and lthr are NOT named, so they cannot pass.
    "profile": {
        "a_goal": S, "b_goal": S,
        "ftp_watts": S,
        "race_name": S, "race_date": S, "race_distance": S,
        "prev_race_date": S, "prev2_race_date": S, "prev2_race_name": S,
        # Pace ONLY, never the coach note that may follow it — see Derived.
        "run_threshold_pace_per_km": _PACE_ONLY, "swim_css_per_100m": _PACE_ONLY,
        "prev_race": {
            "name": S, "date": S,
            "swim_time": S, "t1t2_time": S,
            "bike_time": S, "bike_np_watts": S, "bike_if": S, "bike_vi": S,
            "run_time": S, "run_pace": S, "total_time": S,
        },
        # 2023 Barcelona, same shape and same class of data as prev_race - split
        # times and a bike NP off a race that is already named publicly. `notes` is
        # NOT named here, matching prev_race, and neither is `source`.
        "prev2_race": {
            "name": S, "date": S,
            "swim_time": S, "t1t2_time": S,
            "bike_time": S, "bike_np_watts": S, "bike_if": S, "bike_vi": S,
            "run_time": S, "run_pace": S, "total_time": S,
        },
        "race_targets": {
            "swim_time": S, "swim_pace": S, "swim_pace_per_100m": S,
            "swim_gain": S, "swim_how": S,
            "t1t2_time": S, "t1t2_gain": S, "t1t2_how": S,
            "bike_time": S, "bike_np_watts_target_if": S,
            "bike_gain": S, "bike_how": S,
            "run_time": S, "run_pace": S, "run_gain": S, "run_how": S,
            "total_time": S,
        },
    },

    "fitnessThis":  SERIES,
    "fitnessPrev":  SERIES,
    "fitnessPrev2": SERIES,
    "fitnessBySport": {
        "current": _SPORT_SERIES, "prev": _SPORT_SERIES, "prev2": _SPORT_SERIES,
    },
    # Rolling training hours/week, same three seasons and same per-sport split as
    # fitnessBySport. Moving time is training volume, not a wellness measurement -
    # the same class of data as the TSS already published in loadChart.
    "durationBySport": {
        "current": _DUR_SERIES, "prev": _DUR_SERIES, "prev2": _DUR_SERIES,
    },

    # sick_week is NOT named, so the illness window cannot pass.
    "ctlProjection": {
        "current_trend":     _CTL_POINT,
        "planned_build":     _CTL_POINT,
        "planned_sessions":  _CTL_POINT,
        "target_milestones": Records({"date": S, "ctl": S, "label": S}),
        "race_date":         S,
        "target_ctl_min":    S,
        "target_ctl_max":    S,
        # Forward projection drawn on the Fitness chart. planned_load is prescribed
        # session TSS off the calendar and the ramp figures are CTL rates - training
        # load, the same class as loadChart, with no wellness field among them.
        "projection": {
            "plan":                  Records({"date": S, "ctl": S, "planned_load": S}),
            "plan_days":             S,
            "linear":                _CTL_POINT,
            "linear_slope_per_week": S,
            "ramp_4wk_per_week":     S,
            "ramp_cap_per_week":     S,
            "extend_to":             S,
            "basis":                 S,
        },
    },

    "loadChart": Records({
        "date": S, "projected": S, "tsb": S,
        "activities": Records({"sport": S, "name": S, "dur": S, "tss": S, "status": S}),
    }),
    "planVsActual": Records({
        "week_num": S, "week_start": S, "planned_tss": S, "actual_tss": S,
        # week_type ("build"/"specific"/"deload"/"taper"/"race") and in_progress are
        # both plan metadata, not physiology — no wellness field is added here.
        "week_type": S, "in_progress": S,
    }),
    # np/npPrev are NORMALISED power at the same durations, computed from power
    # streams because intervals.icu has no mean-maximal NP curve. Same class of data
    # as w/wPrev - a wattage over a duration, no wellness field involved.
    "powerCurve": Records({"t": S, "label": S, "w": S, "wPrev": S,
                           "np": S, "npPrev": S}),
    # Dates and a day count describing the comparison window - no physiology.
    "powerCurveWindow": {
        "days": S, "now_from": S, "now_to": S,
        "prev_from": S, "prev_to": S, "label": S,
        "np_basis": S, "np_pending": S,
    },

    # The athlete's focus sports, e.g. ["swim","bike","run"] or ["bike"]. A coaching
    # preference read off the blueprint, not a measurement.
    "sports": ScalarList(),

    # Time in zones by sport against the blueprint's phase target. Minutes and
    # percentages of TRAINING time plus the zone labels - no HR, no wellness. `basis`
    # is a per-sport word ("power", "grade-adjusted pace") stating what the split was
    # measured on, which is published because the number is not interpretable without
    # it. Window and sport keys are data-driven, hence Mapping.
    "zoneDistribution": {
        "phase":   S,
        "basis":   Mapping(S),
        "min_coverage": S,
        "windows": Mapping({"from": S, "to": S, "label": S}),
        "sports":  Mapping(Mapping({
            "minutes": S, "sessions": S, "unclassified_sessions": S,
            # basis is the word "power"/"heart rate"/"grade-adjusted pace" - what the
            # split was measured on, per sport. coverage is the share of moving time
            # that could be classified. Both are stated because a zone split is not
            # interpretable without them, and neither is a wellness measurement.
            "basis": S, "coverage": S,
            "target_stated": S,
            "bands": Records({"label": S, "target": S, "minutes": S, "actual": S}),
            "zones": Mapping(S),
        })),
    },
    "racePredictor": {
        "anchor": _PREDICTOR_ROW,
        "rows":   Records(_PREDICTOR_ROW),
    },
    "recent": Records({
        # `shape` is a route OUTLINE normalised into a unit box: no coordinates, no
        # scale, no origin (lib/route_shape.py). A GPS track would publish Jamie's home
        # address several times a week on a public URL; this cannot be geolocated
        # because the absolute frame is discarded before it is written, not hidden.
        # SERIES, so any row that is not a list of scalars is dropped.
        "shape": SERIES,
        "date": S, "name": S, "sport": S, "dur": S, "dist": S, "pace": S,
        "hr": S,          # per-session average HR — owner-approved (same field
                          # as sessionLog.avg_hr, different key name upstream)
        "powAvg": S, "powNp": S, "tss": S,
    }),
    # activity_id deliberately omitted from both logs.
    "swimLog": Records({
        "date": S, "name": S, "distance_m": S, "duration_min": S,
        "pace_per_100m": S, "tss": S,
    }),
    "weekCalendar": Records({
        "date": S, "name": S, "sport": S, "detail": S,
        "duration_min": S, "tss": S, "planned_tss": S, "status": S, "key": S,
    }),
    # progressData.carb: PUBLISHED on an explicit owner decision, 3 Aug 2026
    # ("carb intake can be published and should be"). It was previously withheld
    # pending exactly that decision. Field-level audit before unlocking: date,
    # g_per_hr, sport, dur, name - a fuelling rate per ride and the ride it came
    # from. Nothing clinical, no free text beyond the session title, which is
    # already published in recent[] and weekCalendar[]. `carb` and `g_per_hr`
    # were removed from FORBIDDEN_KEYS in the same change, because a tripwire
    # that fires on an approved field would block every future write.
    # sessionLog[].nutrition_g_carb stays withheld - see the header.
    "progressData": {
        "ftp": S,
        "carb": Records({
            "date": S, "g_per_hr": S, "sport": S, "dur": S, "name": S,
        }),
        "rides": Records({
            "date": S, "name": S, "dur": S, "np": S, "hr": S, "ef": S, "vi": S,
        }),
        "runs": Records({
            "date": S, "name": S, "dur": S, "dist": S, "pace": S, "hr": S, "ef": S,
        }),
    },
    # Heat acclimation and aerobic decoupling are training-adaptation metrics of
    # the same class as CTL/ATL/TSB and the approved per-session avg_hr - they
    # describe the response to training load, not a clinical state. Field-level
    # audit found nothing clinical in either: heatAccl is a score, a decay
    # constant, dated exposure rows and a method label ("hot bath", "outdoor
    # ride"); heatProtocol is dates, session counts and a weekly target band.
    # Restored 28 Jul 2026 on the orchestrator-s classification, NOT on an owner
    # decision - flag for confirmation.
    "heatAccl": {
        "current": S, "peak": S, "peak_date": S,
        "entries": S, "tau_days": S,
        "daily":  SERIES,   # [date, score]
        "events": SERIES,   # [date, dose, pct, label]; label is the exposure
                            # method, needed for the chart tooltips
    },
    "heatProtocol": {
        "last_session_date": S, "protocol_start_date": S,
        "sessions_cumulative": S, "sessions_this_week": S,
        "target_max": S, "target_min": S,
    },
    # activity_id stays out - it is an Intervals.icu record id, not a metric.
    "decouplingTrend": Records({
        "date": S, "name": S, "duration_min": S, "if": S,
        "decoupling_pct": S, "tss": S,
    }),
    "sessionLog": Records({
        "date": S, "name": S, "sport": S,
        "duration_min": S, "distance_km": S, "pace_per_100m": S,
        "avg_hr": S, "avg_power": S, "norm_power": S,
        # `rpe` is a bounded 1-10 integer and is published. `feel` is NOT: it is
        # free text the athlete types into Telegram, unreviewed before it would
        # be served. Observed values include "serious cramp issues", "felt
        # terrible but probably due to lack of sleep" and "No real pain, just
        # legs didn-t find a rhythm" - symptoms, sleep and pain, in a field whose
        # name suggests a mood enum. Withheld 28 Jul 2026; this overrides an
        # explicit owner KEEP, so it needs the owner to confirm or restore it.
        "rpe": S, "tss": S, "stub": S,
        # nutrition_g_carb + hydration_ml: PUBLISHED on an explicit owner decision,
        # 3 Aug 2026 - carbs ("carb intake can be published and should be") and then
        # per-activity fuelling in the app ("if i click on an activity i should be
        # able to see the summary and heat/carb/sodium/water"). Both are currently
        # null for every logged session, so the app shows "not logged" rather than a
        # number; they are unlocked now so a value flows through the moment one is
        # recorded, instead of silently vanishing at the sanitiser.
        #
        # NOTE for whoever reads this next: there is NO sodium field anywhere
        # upstream. The only occurrence of the word is currentState.open_actions
        # ("Book Precision Hydration sweat-sodium test", still pending), which is
        # itself withheld. Sodium cannot be published because it is not measured.
        # nutrition_mg_sodium added 3 Aug 2026. Jamie asked why sodium was absent -
        # the honest answer was that nothing ever captured it: there was no field in
        # the session-log schema and no prompt asking for it, so the figure was
        # discussed in chat and then discarded. Field + capture added together;
        # publishing it is the same decision as carbs and water.
        #
        # This is sodium CONSUMED (mg). Sweat sodium CONCENTRATION (mg/L, from the
        # Precision Hydration test, still unbooked) is a different measurement and is
        # NOT published - it is a physiological trait, not a training input.
        "nutrition_g_carb": S, "hydration_ml": S, "nutrition_mg_sodium": S,
    }),
}

SITE_DATA_SPEC = {
    "updated": S,
    "athletes": Mapping({
        "first_name": S,
        "race_name": S, "race_date": S,
        "ctl": S, "atl": S, "tsb": S,
        "ctl_history": SERIES,
    }),
}


# ── second line of defence ───────────────────────────────────────────────────
# The allow-list IS the mechanism. This is a tripwire, not the guard: if a
# forbidden key ever appears in a pruned payload it means the spec above was
# edited wrongly, and the write must fail rather than publish.
FORBIDDEN_KEYS = {
    "weight_kg", "hrv", "rhr", "lthr",
    "weight_readings", "weightTrend", "kg",
    "sick_week", "watchdog_flags", "open_actions",
    "notes", "injury_pain_during", "injury_pain_next_morning",
    "ankle_pain_during", "ankle_pain_next_morning",
    # Removed 3 Aug 2026 as each became an approved field (see the spec): "carb",
    # "g_per_hr", "hydration_ml", "nutrition_g_carb". A tripwire on an approved key
    # blocks every write, so the two lists must not disagree.
    "feel",
    "activity_id", "logged_at",
    "icu_api_key", "icu_athlete_id", "telegram_chat_id",
}


def find_forbidden(obj, path=""):
    """Return a list of dotted paths at which a FORBIDDEN_KEYS key appears."""
    hits = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = f"{path}.{k}" if path else k
            if k in FORBIDDEN_KEYS:
                hits.append(p)
            hits.extend(find_forbidden(v, p))
    elif isinstance(obj, list):
        for item in obj:
            hits.extend(find_forbidden(item, path + "[]"))
    return hits


# ── public API ───────────────────────────────────────────────────────────────
def sanitise_training_data(data: dict) -> dict:
    return prune(data, TRAINING_DATA_SPEC)


def sanitise_site_data(data: dict) -> dict:
    return prune(data, SITE_DATA_SPEC)


def write_public_json(payload: dict, out_path) -> None:
    """Write a sanitised payload, refusing if the tripwire fires.

    `out_path` is an ABSOLUTE path supplied by the caller. The two ETLs resolve
    BASE differently (refresh-site-data.py -> ClaudeCoach/, refresh-public-data.py
    -> diamondpeak-site/), so this module never computes output paths itself.
    """
    import json
    from pathlib import Path

    hits = find_forbidden(payload)
    if hits:
        raise ValueError(
            f"refusing to write {out_path}: forbidden keys present: {hits}"
        )
    p = Path(out_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(payload, separators=(",", ":")))
