"""validate_plan.py — deterministic backstop for the planned training week.

Pure functions, no IO. The planner asks the LLM to build a week and self-check it
against hard constraints; this module lets the Python wrapper independently verify
the *result* (the events the LLM actually pushed to intervals.icu), so a breach
cannot reach the athlete just because the model graded its own homework
(remediation-plan WS E, Issue #2).

SCOPE — mechanical constraints ONLY. This validator checks things that are
unambiguously decidable from structured data:
  - wrong-day sessions (a sport scheduled on a day the athlete's day_rules forbid)
  - weekly planned-TSS over a cap
  - implied CTL ramp over a cap
DAY RULES ARE GUIDELINES, NOT INVARIANTS (28 Jul 2026). `*_days` describe an
athlete's normal weekly pattern, and the coach overrides them conversationally
("I told it this week to swim on wed, so we swim on wed"). Two mechanisms keep the
day checks honest without making them permanently red:
  - a DIRECTED deviation (recorded per session in the athlete's
    day-rules-overrides register, passed in as `day_overrides`) becomes a SOFT
    `{sport}_directed_day` advisory that still appears in the report;
  - an UNDIRECTED deviation stays a HARD `{sport}_forbidden_day` — a generator
    drifting off the pattern with nobody asking is a real defect;
  - overrides cannot move the pattern by stealth: DRIFT_THRESHOLD directed hits on
    the same sport+weekday inside DRIFT_WINDOW_DAYS raise a HARD
    `day_rules_drifted` telling the coach to amend the config instead.
A day may also be permitted only UNTIL a date, via a `<key>_expires` sidecar
(`bike_days_expires: {"Sat": "2026-09-05"}`): the same `[expires:DATE]` idea the
prose rules already have, closing the gap in CONFIG. It expires per EVENT DATE, so
this module stays pure and future weeks audit correctly.

It deliberately does NOT attempt to model the prose judgment in rules.md (fuelling,
pacing, KPIs, "quality run while ankle uncleared" — which needs reliable session
classification we don't have). Those remain the LLM's job. Adding a check here
means it must be decidable without interpretation.

SINGLE RULE SOURCE — day_rules must be the SAME structure the planner's prompt was
built from (athletes.json), never a second hardcoded copy. If the validator's rules
and the prompt's rules diverge, every correct plan false-blocks. The caller passes
the athlete's day_rules through; this module never invents them.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta

from primitives.load import compute_projected_ctl
from primitives.modulation import classify_session_type

# Weekday name → Python weekday() int (Mon=0). Accepts common abbreviations.
_DOW = {
    "mon": 0, "monday": 0, "tue": 1, "tues": 1, "tuesday": 1,
    "wed": 2, "weds": 2, "wednesday": 2, "thu": 3, "thur": 3, "thurs": 3, "thursday": 3,
    "fri": 4, "friday": 4, "sat": 5, "saturday": 5, "sun": 6, "sunday": 6,
}

# intervals.icu event `type` → the day-rule key that governs it. Sports not listed
# (Run, WeightTraining, …) are unrestricted unless the athlete adds a rule for them.
_SPORT_RULE = {
    "swim": "swim_days",
    "ride": "bike_days", "virtualride": "bike_days", "gravelride": "bike_days",
    "run": "run_days",
}

# ICU event type -> override-register family (the day_rules key minus `_days`), so
# Ride/VirtualRide/GravelRide share one family. Mirrors lib/day_overrides.FAMILIES.
_SPORT_FAMILY = {sport: key[:-len("_days")] for sport, key in _SPORT_RULE.items()}

# Anti-drift: an "exception" the coach directs again and again is not an exception,
# it is the pattern. Count directed overrides per sport+weekday over a rolling
# window and hard-fail once the same weekday has been directed this often, so
# day_rules cannot be moved by stealth and the audit cannot go quiet on the whole
# category. Self-clearing: entries age out of the window.
DRIFT_WINDOW_DAYS = 28
DRIFT_THRESHOLD = 3
_OVERRIDE_KEY_RE = re.compile(r"^(swim|bike|run):(\d{4}-\d{2}-\d{2})$")


def _to_weekday(name) -> int | None:
    return _DOW.get(str(name).strip().lower())


def _event_date(ev: dict) -> date | None:
    raw = ev.get("start_date_local") or ev.get("date") or ""
    try:
        return date.fromisoformat(str(raw)[:10])
    except ValueError:
        return None


def _planned_load(ev: dict) -> float:
    # Planned TSS lives in load_target; icu_training_load is the post-hoc actual.
    for k in ("load_target", "planned_training_load", "icu_training_load"):
        v = ev.get(k)
        if v is not None:
            try:
                return float(v)
            except (TypeError, ValueError):
                pass
    return 0.0


def _is_workout(ev: dict) -> bool:
    # Only planned workouts count; notes/races/fitness rows do not.
    cat = (ev.get("category") or "WORKOUT").upper()
    return cat == "WORKOUT"


@dataclass
class Violation:
    code: str          # machine key, e.g. "swim_forbidden_day"
    severity: str      # "hard" (block-worthy) | "soft" (warn)
    detail: str        # human-readable, names the offending session/number

    def __str__(self) -> str:
        return f"[{self.severity}] {self.code}: {self.detail}"


@dataclass
class WeekReport:
    week_start: date
    total_tss: float
    violations: list[Violation] = field(default_factory=list)
    # Hard checks that could NOT run because a required input was missing
    # (fail-noisy, audit P0-4: a silently disarmed check reads as a pass).
    # Callers must surface these — they are the difference between "validated"
    # and "not actually checked".
    skipped: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.violations

    @property
    def hard(self) -> list[Violation]:
        return [v for v in self.violations if v.severity == "hard"]


# -- Intensity-distribution drift ----------------------------------------------
# The blueprint's per-phase tables ("75% Z1–2 / 15% Z3 / 10% Z4–5") are weekly
# time-in-zone guidance, so the honest measurement is SEGMENT MINUTES, not sessions.
#
# WHY THIS WAS REWRITTEN (2026-07-28). The check used to bucket WHOLE SESSIONS via
# classify_session_type, which name-matches keywords like "tempo"/"sweetspot". So
# "Easy run + 10min tempo" (40 min, of which 10 are Z3) scored 100% QUALITY, and a
# perfectly-shaped week read as excess intensity. Measured on Jamie's pushed week of
# 2026-07-27 the old path said Run 60% easy vs a 78% target (a flag); the same week
# by segment minutes is 87% easy (on spec). The generous 12pp tolerance existed only
# to paper over that miscount — see DIST_TOLERANCE_PP below.
#
# The fix: a pushed workout carries workout_doc.steps (the STRUCTURE invariant already
# requires them), and every step carries the %FTP / %threshold-pace band the planner
# rendered from planned_tss._ZONE_BAND. That table is reversible — the band tuple is
# unique per bucket within a sport (see _STEP_BAND_BUCKET) — so a step's minutes can be
# credited to easy / Z3 / Z4-5 exactly. Sessions with no usable steps still fall back to
# the old whole-session name matching, and a sport that needed that fallback keeps the
# old loose tolerance, because the old evidence is all there is for it.
#
# The check fires on EXCESS QUALITY only — extra easy volume is never a safety problem,
# and undershooting load is the gap-check's job. Swims and bricks stay excluded: swim
# bands integrate low even for a threshold set, and bricks are mixed by definition.

_EASY_TYPES = {"bike_z2", "run_easy", "run_long"}
_QUALITY_TYPES = {"bike_threshold", "bike_vo2", "bike_race_pace", "run_quality"}
_SPORT_BUCKET = {"ride": "Bike", "virtualride": "Bike", "gravelride": "Bike", "run": "Run"}

# Fallback planned minutes when an event carries no moving_time (planned events
# often don't) — coarse, mirrors the planner's own per-type defaults.
_TYPE_FALLBACK_MIN = {
    "bike_z2": 90, "bike_threshold": 75, "bike_vo2": 75, "bike_race_pace": 90,
    "run_easy": 50, "run_long": 90, "run_quality": 60,
}

# Two tolerances, ONE rule: measurement fidelity sets the band. A sport measured
# entirely from structured steps is judged at SEGMENT_TOLERANCE_PP; a sport where any
# session had to be name-classified keeps DIST_TOLERANCE_PP, because whole-session
# matching credits a session's warmup/recovery minutes to quality and genuinely needs
# the slack. This is not a third arbitrary number - the loose band is retained ONLY
# where the loose measurement is, and retires with the last unstructured session.
DIST_TOLERANCE_PP = 12.0      # name-classified sports (legacy, deliberately generous)
SEGMENT_TOLERANCE_PP = 8.0    # fully segment-measured sports; matches plan_distribution
DIST_MIN_SESSIONS = 2      # don't judge a sport on a single session
DIST_MIN_MINUTES  = 120    # ...or on under two hours of planned work

# A step band whose tuple is absent from _ZONE_BAND falls back to a midpoint-IF cut,
# which is a GUESS. Above this share of a sport's minutes the sport is not trustworthy
# by segment and reverts to name classification (fail-noisy, never silently wrong).
DIST_MAX_UNKNOWN_FRAC = 0.15
# Σ step minutes must reconcile with the event's stated duration this closely, else the
# steps are partial (main set only) and the denominator would be wrong.
DIST_STEP_RECONCILE_FRAC = 0.10


def _easy_target_pct(dist_row) -> float | None:
    """Leading Z1–2 share from a row like '75% Z1–2 / 15% Z3 / 10% Z4–5'."""
    m = re.match(r"\s*(\d+(?:\.\d+)?)\s*%\s*Z1", str(dist_row or ""))
    return float(m.group(1)) if m else None


def dist_targets(dist_row) -> list[float] | None:
    """[Z1-2, Z3, Z4-5] percentages from '72% Z1–2 / 22% Z3 / 6% Z4–5', or None.

    The same row _easy_target_pct reads, parsed in full so the per-zone CEILING limb
    below can reuse zone_band_deviations - one tolerance source for both surfaces.
    Missing Z3 / Z4-5 figures read as 0 (a taper row often states only Z1-2)."""
    s = str(dist_row or "")
    easy = _easy_target_pct(s)
    if easy is None:
        return None
    z3 = re.search(r"(\d+(?:\.\d+)?)\s*%\s*Z\s*3", s, re.I)
    hi = re.search(r"(\d+(?:\.\d+)?)\s*%\s*Z\s*4", s, re.I)
    return [easy, float(z3.group(1)) if z3 else 0.0, float(hi.group(1)) if hi else 0.0]


# ── THE ONE distribution taxonomy (Task: three classifiers, one truth) ────────
# zone NAME -> distribution bucket, per sport. This is the single canonical map:
# lib/plan_distribution.py imports it rather than keeping its own copy (it used to
# hold a near-identical table, which is how three classifiers with three answers
# happened in the first place). Keys are lowercased, punctuation-stripped.
DIST_ZONE_BUCKET = {
    "run": {
        "recovery": "easy", "easy": "easy", "z1": "easy", "z2": "easy",
        "warmup": "easy", "cooldown": "easy", "aerobic": "easy",
        "long": "easy", "endurance": "easy",
        "steady": "z3", "z3": "z3", "tempo": "z3",
        "z4": "z45", "z5": "z45", "threshold": "z45", "css": "z45",
        "interval": "z45", "vo2": "z45", "hill": "z45", "sprint": "z45",
        "speed": "z45", "race": "z45",
    },
    "bike": {
        "recovery": "easy", "easy": "easy", "z1": "easy", "z2": "easy",
        "warmup": "easy", "cooldown": "easy", "endurance": "easy", "aerobic": "easy",
        "z3": "z3", "tempo": "z3", "sweetspot": "z3", "ss": "z3", "race": "z3",
        "z4": "z45", "z5": "z45", "threshold": "z45", "ftp": "z45",
        "vo2": "z45", "anaerobic": "z45", "sprint": "z45",
    },
}
_DIST_SPORT_KEY = {"run": "run", "trailrun": "run", "virtualrun": "run",
                   "bike": "bike", "ride": "bike", "virtualride": "bike",
                   "gravelride": "bike"}
# The Z2/Z3 and Z3/Z4 IF cuts used when a step band is NOT in the canonical table
# (planned_tss._band falls back to intensity±4pp for a segment given a raw "if").
# Identical to the cuts stage1-plan._zone_by_sport uses, so the generation-time and
# post-push classifiers agree on the boundaries as well as the buckets.
_DIST_IF_CUT = {"run": (0.85, 0.90), "bike": (0.76, 0.90)}


def norm_zone_name(zone) -> str:
    """'Z1-2' -> 'z1', 'VO2 max' -> 'vo2' - leading token, punctuation stripped."""
    z = re.sub(r"[^a-z0-9]", " ", str(zone or "").lower()).strip()
    return z.split()[0] if z else ""


def bucket_for_zone_name(sport: str, zone) -> str | None:
    """easy / z3 / z45 for a named zone in a sport, or None when unmappable."""
    key = _DIST_SPORT_KEY.get(str(sport or "").strip().lower())
    if not key:
        return None
    return DIST_ZONE_BUCKET[key].get(norm_zone_name(zone))


def _build_step_band_bucket():
    """{sport: {(lo, hi): bucket}} inverted from planned_tss._ZONE_BAND at import.

    Derived, never hand-maintained, so it cannot drift from the table the planner
    actually renders steps with. Asserted injective at bucket level: a tuple shared
    by two zones in DIFFERENT buckets would make the reverse map a coin-flip, and
    that must fail loudly at import rather than silently mis-credit minutes. (It is
    injective today: run easy/z2 = (78,88) but steady/z3 = (80,86) - overlapping
    ranges, distinct tuples, which is exactly why the tuple and not the midpoint is
    the key. The midpoints are both 83.)"""
    from primitives.planned_tss import _ZONE_BAND
    out: dict = {}
    for sport in ("run", "bike"):
        for zone, band in _ZONE_BAND.get(sport, {}).items():
            b = DIST_ZONE_BUCKET[sport].get(norm_zone_name(zone))
            if b is None:
                continue
            prior = out.setdefault(sport, {}).setdefault(tuple(band), b)
            if prior != b:
                raise AssertionError(
                    f"_ZONE_BAND {sport} band {band} maps to both {prior} and {b} - "
                    f"the step-band reverse index is not injective")
    return out


_STEP_BAND_BUCKET = _build_step_band_bucket()


def _step_band(step: dict):
    """(start, end) of a workout_doc step's power/pace/hr target, or None."""
    for key in ("power", "pace", "hr"):
        t = step.get(key)
        if isinstance(t, dict) and t.get("start") is not None and t.get("end") is not None:
            return (t["start"], t["end"])
    return None


def bucket_for_step(sport: str, step: dict) -> str | None:
    """easy / z3 / z45 for one workout_doc step by EXACT band-tuple lookup, else None.

    Exact only, deliberately: a tuple absent from the table came from planned_tss._band's
    intensity fallback (pct±4 around 100×IF) and can only be classified by guessing at
    the midpoint. That guess is _bucket_from_band_midpoint's job, and step_bucket_minutes
    counts those minutes separately, so a sport built mostly of guesses is never reported
    as measured."""
    key = _DIST_SPORT_KEY.get(str(sport or "").strip().lower())
    band = _step_band(step or {})
    if not key or not band:
        return None
    exact = _STEP_BAND_BUCKET.get(key, {}).get(tuple(band))
    if exact:
        return exact
    return None


def _bucket_from_band_midpoint(sport: str, step: dict) -> str | None:
    key = _DIST_SPORT_KEY.get(str(sport or "").strip().lower())
    band = _step_band(step or {})
    if not key or not band:
        return None
    mid = (float(band[0]) + float(band[1])) / 200.0
    z3_cut, hi_cut = _DIST_IF_CUT[key]
    return "z45" if mid >= hi_cut else ("z3" if mid >= z3_cut else "easy")


def step_bucket_minutes(sport: str, event: dict) -> dict | None:
    """{easy, z3, z45, total, guessed_min, unbucketed_min} min from workout_doc.steps.

    Returns None when the event cannot be measured by segment, so the caller falls
    back to whole-session name classification. That happens when there are no steps,
    when no step carries a duration, or when Σ step minutes does not reconcile with
    the event's stated moving_time - a partially-stepped session would otherwise be
    scored against a denominator that is missing its easy minutes."""
    steps = ((event or {}).get("workout_doc") or {}).get("steps") or []
    if not steps:
        return None
    # guessed_min SHADOWS the bucket totals (it is not a fourth bucket): those minutes
    # are credited to a bucket by midpoint IF, and separately counted as not-measured so
    # the caller can refuse to trust a sport built mostly of guesses.
    agg = {"easy": 0.0, "z3": 0.0, "z45": 0.0, "total": 0.0,
           "guessed_min": 0.0, "unbucketed_min": 0.0}
    for st in steps:
        secs = st.get("duration")
        if not secs:
            continue                      # distance-only / open step: contributes nothing
        mins = float(secs) / 60.0
        agg["total"] += mins
        b = bucket_for_step(sport, st)
        if b is None:
            b = _bucket_from_band_midpoint(sport, st)
            agg["guessed_min"] += mins
        if b is None:
            agg["unbucketed_min"] += mins   # no target band at all - unusable
            continue
        agg[b] += mins
    if agg["total"] <= 0:
        return None
    stated = float((event or {}).get("moving_time") or 0) / 60.0
    if stated > 0 and abs(agg["total"] - stated) > stated * DIST_STEP_RECONCILE_FRAC:
        return None                        # partial structure - do not trust the split
    return agg


def _session_bucket_minutes(sport_key: str, event: dict):
    """(buckets, measured) minutes for one Run/Bike event.

    measured=True  -> credited from structured steps (segment minutes).
    measured=False -> whole-session name classification, the legacy path: the WHOLE
                      session lands in easy or quality, warmup and recoveries included.
                      That overstatement is why such a sport keeps the loose tolerance.
    Returns (None, False) for a session that is neither easy nor quality by name (e.g.
    a brick run), preserving the old exclusion.
    """
    st0 = classify_session_type(event.get("type", ""), event.get("name", ""))
    if st0 == "brick":
        # A Run/Ride-typed session NAMED as a brick is excluded even though its steps
        # would now bucket cleanly: bricks are mixed by definition and the module has
        # always left them out, so crediting them here would silently change the
        # denominator of every comparison against the pre-segment figures. (Measured:
        # including Kathryn's 50-min "Brick run — tempo off the bike" for the week of
        # 2026-07-27 pushes her Run Z3 from 10% to 20% and manufactures a ceiling flag.)
        return (None, False)
    segs = step_bucket_minutes(sport_key, event)
    if segs and segs["total"] > 0:
        usable = segs["total"] - segs["unbucketed_min"]
        if usable > 0:
            return ({"easy": segs["easy"], "z3": segs["z3"], "z45": segs["z45"],
                     "total": usable, "guessed": segs["guessed_min"]}, True)
    st = st0
    if st not in _EASY_TYPES and st not in _QUALITY_TYPES:
        return (None, False)
    mins = (float(event.get("moving_time") or 0) / 60) or _TYPE_FALLBACK_MIN.get(st, 60)
    easy = mins if st in _EASY_TYPES else 0.0
    # A name-classified quality session cannot be split Z3 vs Z4-5, so its non-easy
    # minutes are deliberately left OUT of the per-zone buckets - and the presence of
    # any such session disables the per-zone limb for that sport (see below).
    return ({"easy": easy, "z3": 0.0, "z45": 0.0, "total": mins, "guessed": 0.0}, False)


def _check_distribution(week_events: list[dict], week_start: date,
                        distribution: dict, tolerance_pp: float,
                        skipped: list[str] | None = None) -> list["Violation"]:
    buckets: dict[str, dict[str, float]] = {}
    for e in week_events:
        sport = _SPORT_BUCKET.get(str(e.get("type") or "").strip().lower())
        if not sport:
            continue
        got, measured = _session_bucket_minutes(str(e.get("type") or ""), e)
        if not got:
            continue
        b = buckets.setdefault(sport, {"easy": 0.0, "z3": 0.0, "z45": 0.0,
                                       "total": 0.0, "n": 0, "named_min": 0.0,
                                       "guessed": 0.0})
        for k in ("easy", "z3", "z45", "total"):
            b[k] += got[k]
        b["guessed"] += got["guessed"]
        b["n"] += 1
        if not measured:
            b["named_min"] += got["total"]

    out: list[Violation] = []
    for sport, b in buckets.items():
        row = (distribution or {}).get(sport)
        target = _easy_target_pct(row)
        if target is None or b["n"] < DIST_MIN_SESSIONS or b["total"] < DIST_MIN_MINUTES:
            if skipped is not None:
                if target is None:
                    reason = f"no {sport} distribution target configured for this phase"
                else:
                    reason = (f"only {b['n']:.0f} session(s) / {b['total']:.0f} min planned "
                              f"(need >= {DIST_MIN_SESSIONS} sessions and "
                              f"{DIST_MIN_MINUTES} min)")
                skipped.append(f"week of {week_start}: {sport} intensity distribution "
                               f"check NOT RUN — {reason}")
            continue
        # Measurement fidelity decides the tolerance AND whether the per-zone limb runs.
        guess_frac = (b["guessed"] / b["total"]) if b["total"] else 0.0
        fully_measured = b["named_min"] <= 0 and guess_frac <= DIST_MAX_UNKNOWN_FRAC
        tol = SEGMENT_TOLERANCE_PP if fully_measured else tolerance_pp
        basis = "segment minutes" if fully_measured else "session time"
        if skipped is not None and not fully_measured:
            why = []
            if b["named_min"] > 0:
                why.append(f"{b['named_min']:.0f} of {b['total']:.0f} min from sessions "
                           f"with no usable structured steps (name-classified whole)")
            if guess_frac > DIST_MAX_UNKNOWN_FRAC:
                why.append(f"{guess_frac*100:.0f}% of minutes on step bands absent from "
                           f"the canonical table (zone inferred from the band midpoint)")
            skipped.append(f"week of {week_start}: {sport} intensity distribution measured "
                           f"loosely — " + "; ".join(why) + f" — judged at "
                           f"−{tol:.0f}pp, per-zone ceilings not asserted")
        easy_pct = b["easy"] / b["total"] * 100
        if easy_pct < target - tol:
            out.append(Violation(
                code="intensity_distribution",
                severity="soft",
                detail=(f"week of {week_start}: {sport} is {easy_pct:.0f}% easy by "
                        f"{basis} vs the phase target {target:.0f}% Z1–2 "
                        f"(tolerance −{tol:.0f}pp) — too much quality planned"),
            ))
        # ── per-zone CEILING limb: the same _ZONE_BANDS tolerances the generation-time
        # gate uses (zone_band_deviations), applied to the calendar-derived split. This
        # closes the hole the easy-share check alone leaves: swapping Z3 for Z4-5 keeps
        # the easy share identical while stacking VO2. CEILINGS ONLY (deload=True drops
        # the floors) - plan_audit has no week_type to tell a deload week from a normal
        # one, and a floor asserted on a deload/luteal-adjusted week is a manufactured
        # false positive of exactly the kind this commit removes. Under-dosed quality is
        # reported by lib/plan_distribution's INSUFFICIENT direction, not here.
        tgts = dist_targets(row)
        if not fully_measured or not tgts:
            continue
        realised = {sport: {"z3_pct": b["z3"] / b["total"] * 100,
                            "high_pct": b["z45"] / b["total"] * 100,
                            "min": b["total"]}}
        for d in zone_band_deviations(realised, {sport: tgts}, deload=True,
                                      min_minutes=DIST_MIN_MINUTES * 2):
            if d["kind"] != "ceiling":
                continue
            label = "Z3" if d["zone"] == "z3" else "Z4–5"
            out.append(Violation(
                code=f"intensity_distribution_{'z3' if d['zone'] == 'z3' else 'vo2'}_high",
                severity="soft",
                detail=(f"week of {week_start}: {sport} {label} is {d['actual']:.0f}% of "
                        f"segment minutes vs the phase target {d['target']:.0f}% "
                        f"(over the ceiling by {d['dev']:.0f}pp) — move the excess to a "
                        f"lower zone in the same sport"),
            ))
    return out


# -- Repeat escalation (a flag pushed past every week is not a flag) -----------
# A soft advisory that recurs week after week is materially different from a
# one-off: the first is a plan the athlete keeps overriding, the second is noise.
# plan_audit's baseline is COUNT-keyed, so a flag that never changes count is
# accepted forever and never resurfaces. This adds an explicit streak so the
# fourth consecutive week reads differently from the first.
#
# PURE by design (this module never does IO): the caller owns the store, passes
# the prior streaks in and writes the returned ones back. Escalation raises the
# WORDING and emits a distinct code, NEVER the severity - severity "hard" blocks a
# push and would change plan_audit's hard_fail, and the standing rule is that only
# safety ceilings block. An escalated advisory is loud, not blocking.

DIST_ESCALATE_AFTER = 3    # consecutive weeks carrying the same code before escalating


def escalate_repeats(violations: list["Violation"], streaks: dict | None,
                     *, escalate_after: int = DIST_ESCALATE_AFTER):
    """(violations, new_streaks) with recurring soft codes escalated in wording.

    streaks: {code: consecutive_runs_seen} from the previous run. A code present
    now increments; a code absent is dropped (the streak breaks on a clean run, so
    an intermittent flag never accumulates into a false escalation). At or above
    escalate_after, an extra Violation with code "<code>_persistent" is appended
    naming the streak length, so the reader sees "3 weeks running" not "again".
    """
    prior = dict(streaks or {})
    seen = {}
    out = list(violations or [])
    for v in violations or []:
        if v.severity != "soft":
            continue
        seen[v.code] = int(prior.get(v.code, 0) or 0) + 1
    for code, n in sorted(seen.items()):
        if n < escalate_after:
            continue
        out.append(Violation(
            code=f"{code}_persistent", severity="soft",
            detail=(f"{code} has now fired {n} runs in a row — it is being pushed past "
                    f"every week rather than acted on. Either change the plan or change "
                    f"the phase target it is being judged against"),
        ))
    return out, seen


# -- Overall intensity budget (Phase 5 redesign) ------------------------------
# The blueprint carries BOTH an overall phase TID (e.g. build [80,11,9] = 20% Z3+) and
# per-sport TIDs (Bike 30% / Swim 35% / Run 22% Z3+) that cannot average to the overall.
# RECONCILIATION: the OVERALL TID is authoritative - it is the hard intensity budget for
# the week's TOTAL time-at-intensity. The per-sport TIDs are only a soft preference for HOW
# to spend that budget and are FUNGIBLE across sports (a share a sport cannot carry -
# run-limited, run-capped, single-sport - moves to the capable sports). So we check the
# TOTAL Z3+ share against the overall budget and NEVER independently max each per-sport TID
# (which double-counts and blows the total). Advisory: it drives the iterate loop, never
# blocks the push (only safety ceilings block).

# -- Per-sport per-zone symmetric bands (Phase 5.5) ---------------------------
# One tolerance source (pp) per zone: (floor_tol, ceiling_tol). Z1-2 is the residual and is
# deliberately NOT banded - a Z1-2 ceiling means "too much easy, cut it", which we reject
# (extra easy volume is never a safety problem). Sanity vs the per-sport targets (e.g. IM bike
# 72/22/6): Z3 band 17-27% (target 22), Z4-5 band 3-9% (target 6) - a normal specific week
# sits mid-band. All soft/advisory; only pre-existing safety ceilings ever hard-block.
_ZONE_BANDS = {"z3": (5.0, 5.0), "high": (3.0, 3.0)}
_ZONE_TGT_IDX = {"z3": 1, "high": 2}
_ZONE_PCT_KEY = {"z3": "z3_pct", "high": "high_pct"}


def zone_band_deviations(per_sport, per_sport_targets, *, deload=False,
                        injury_bands=None, min_minutes=180):
    """Symmetric per-sport per-zone deviations vs [floor_edge, ceil_edge] (default
    [t - tol_lo, t + tol_hi] from _ZONE_BANDS). Returns [{sport, zone, kind, actual, target,
    dev}] (dev >= 0). Floors dropped on deload (ceilings stay). The run Z4-5 floor is
    GENERIC (healthy runners are nudged toward their run-VO2 target like the bike); it is
    suppressed only per-athlete via an injury physio_cap of 0 (below). Z1-2 not banded.
    Phase 5.6: injury_bands {sport:{zone:{floor,ceiling,cap,hard}}} overrides the edges for an
    injured athlete's affected zones; a HARD (cap 0, not physio-cleared) zone emits NO soft
    deviation here - stage1 hard-gates it (blocking)."""
    out = []
    inj = injury_bands or {}
    for sport, tgt in (per_sport_targets or {}).items():
        r = (per_sport or {}).get(sport)
        if not r or not tgt or len(tgt) < 3 or (r.get("min") or 0) < min_minutes / 2:
            continue
        for zone, (tol_lo, tol_hi) in _ZONE_BANDS.items():
            band = (inj.get(sport) or {}).get(zone)
            if band and band.get("hard"):
                continue                                   # not cleared -> hard-gated in stage1
            actual = float(r.get(_ZONE_PCT_KEY[zone]) or 0)
            target = float(tgt[_ZONE_TGT_IDX[zone]])
            ceil_edge = band["ceiling"] if band else target + tol_hi
            floor_edge = band["floor"] if band else target - tol_lo
            if actual > ceil_edge:
                out.append({"sport": sport, "zone": zone, "kind": "ceiling",
                            "actual": actual, "target": target, "dev": actual - ceil_edge})
            elif actual < floor_edge:
                if deload:
                    continue
                out.append({"sport": sport, "zone": zone, "kind": "floor",
                            "actual": actual, "target": target, "dev": floor_edge - actual})
    return out


def check_intensity_budget(z3plus_min: float, total_min: float, overall_tid,
                           *, band_pp: float = 4.0, high_min: float | None = None,
                           high_band_pp: float = 3.0, per_sport: dict | None = None,
                           per_sport_targets: dict | None = None,
                           per_sport_week: dict | None = None, rolling: bool = False,
                           spike_pp: float = 8.0, deload: bool = False,
                           injury_bands: dict | None = None,
                           min_minutes: float = 180) -> list["Violation"]:
    """Phase 5.3 per-sport / per-zone advisory (soft; only safety ceilings block).

    The overall phase TID is DERIVED (volume-weighted sum of the per-sport rows), so it is
    a CROSS-CHECK here, not the gate. The GATE is per-sport: Z3 (sweetspot/tempo) and Z4-5
    (VO2/threshold) are checked SEPARATELY per sport against distribution_targets - a week
    can no longer pass the overall while stacking VO2 where sweetspot was wanted (the lump
    bug). VO2 is the capped lever and is EVENT- and SPORT-specific: the impact sports
    (bike/run) carry the low IM ceiling; swim floats (its 'high' is CSS/threshold).

    z3plus_min/high_min = overall Z3+/Z4-5 minutes; overall_tid = derived [low, mod, high].
    per_sport = {sport: {"z3_pct","high_pct","min"}} realised; per_sport_targets =
    {sport: [low, mod, high]} from the brief."""
    out: list["Violation"] = []
    # -- overall Z3+ cross-check (informational; per-sport bands govern) --
    if overall_tid and len(overall_tid) >= 3 and total_min and total_min >= min_minutes:
        target = float(overall_tid[1]) + float(overall_tid[2])
        pct = z3plus_min / total_min * 100
        if pct < target - band_pp and not deload:
            out.append(Violation(code="intensity_budget_low", severity="soft",
                detail=(f"week is {pct:.0f}% Z3+ vs the derived phase overall ~{target:.0f}% - ADD "
                        f"quality in the sports that can carry it (per-sport zone targets govern)")))
        elif pct > target + band_pp:
            out.append(Violation(code="intensity_budget_high", severity="soft",
                detail=(f"week is {pct:.0f}% Z3+ vs the derived phase overall ~{target:.0f}% - trim "
                        f"quality back toward the per-sport zone targets")))
        if high_min is not None:
            hi_t = float(overall_tid[2]); hi_p = high_min / total_min * 100
            if hi_p > hi_t + high_band_pp:
                out.append(Violation(code="intensity_vo2_high", severity="soft",
                    detail=(f"week Z4-5 is {hi_p:.0f}% vs the derived overall ~{hi_t:.0f}% - shift "
                            f"quality to Z3 sweetspot; the low-VO2 shape is deliberate")))
    # -- per-sport per-zone SYMMETRIC bands (Phase 5.5): THE GATE, separate per zone (never
    #    lumped). Floor AND ceiling from _ZONE_BANDS; floors off on deload; run Z4-5 floor
    #    suppressed (ankle-safe); Z1-2 residual. per_sport is the 2-week rolling aggregate. --
    _win = "2-week avg" if rolling else "this week"
    _lbl = {"z3": "Z3 sweetspot", "high": "Z4-5 VO2"}
    _code = {("z3", "ceiling"): "sweetspot_high", ("z3", "floor"): "sweetspot_low",
             ("high", "ceiling"): "vo2_high", ("high", "floor"): "vo2_low"}
    for d in zone_band_deviations(per_sport, per_sport_targets, deload=deload,
                                  injury_bands=injury_bands, min_minutes=min_minutes):
        sp, zone, kind = d["sport"], d["zone"], d["kind"]
        code = f"{_code[(zone, kind)]}_{sp.lower()}"
        if kind == "ceiling":
            detail = (f"{sp} {_lbl[zone]} is {d['actual']:.0f}% ({_win}) vs target {d['target']:.0f}% "
                      f"- over the ceiling; move the excess to a lower zone in the same sport, or to "
                      f"a sport that can carry it (preserve zone TYPE)")
        else:
            detail = (f"{sp} {_lbl[zone]} is {d['actual']:.0f}% ({_win}) vs target {d['target']:.0f}% "
                      f"- under the floor; add a small {_lbl[zone]} touch in this sport (within caps) "
                      f"toward its target")
        out.append(Violation(code=code, severity="soft", detail=detail))
    # -- single-week VO2 SPIKE ceiling (Phase 5.4): even if the 2-week average is in-cap, one
    #    week dumping a big VO2 block is a hard/injury spike. Soft; skipped on deload. --
    if per_sport_week and not deload:
        for sport, tgt in (per_sport_targets or {}).items():
            r = per_sport_week.get(sport)
            if not r or len(tgt) < 3 or (r.get("min") or 0) < min_minutes / 2:
                continue
            hi_t, hi_p = float(tgt[2]), float(r.get("high_pct") or 0)
            if hi_p > hi_t + spike_pp:
                out.append(Violation(code=f"vo2_spike_{sport.lower()}", severity="soft",
                    detail=(f"{sport} Z4-5 is {hi_p:.0f}% THIS week vs ceiling {hi_t:.0f}% - a single-"
                            f"week VO2 spike (>+{spike_pp:.0f}pp); spread the quality across the "
                            f"block even if the 2-week average is fine")))
    return out


# -- Distance/duration internal consistency (walk-run sessions) ----------------
# A run session whose NAME states a distance ("5k") and whose walk-run cycle
# count implies a different one ("50 min 5x9:1 walk-run" ~ 8.5km) reached an
# athlete with nothing catching it (11 May 2026 feedback: Jamie had to point out
# the mismatch twice) — the same "no programmatic check" class as the long-run
# progression cap. No athlete-specific pace is threaded through the plan
# builder, so this uses conservative generic run/walk pace bands and a wide
# tolerance: it only catches gross mismatches, not fine pacing disagreements.

_WALK_RUN_RE  = re.compile(r"(\d+)\s*x\s*(\d+)\s*:\s*(\d+)", re.IGNORECASE)
_DISTANCE_RE  = re.compile(r"(\d+(?:\.\d+)?)\s*k(?:m)?\b", re.IGNORECASE)
_RUN_PACE_MIN_PER_KM  = 6.5    # conservative easy-run pace
_WALK_PACE_MIN_PER_KM = 12.0   # brisk-walk pace
DIST_DURATION_TOLERANCE = 0.35  # ±35% — generic pace bands, not athlete-specific


def _implied_walk_run_km(reps: int, run_min: float, walk_min: float) -> float:
    run_km = (reps * run_min) / _RUN_PACE_MIN_PER_KM
    walk_km = (reps * walk_min) / _WALK_PACE_MIN_PER_KM
    return run_km + walk_km


def _check_distance_duration(week_events: list[dict]) -> list["Violation"]:
    out: list[Violation] = []
    for e in week_events:
        if str(e.get("type") or "").strip().lower() != "run":
            continue
        name = str(e.get("name") or "")
        text = f"{name} {e.get('description_raw') or ''}"
        wr = _WALK_RUN_RE.search(text)
        dm = _DISTANCE_RE.search(name)
        if not wr or not dm:
            continue
        reps, run_min, walk_min = int(wr.group(1)), float(wr.group(2)), float(wr.group(3))
        labelled_km = float(dm.group(1))
        if labelled_km <= 0:
            continue
        implied_km = _implied_walk_run_km(reps, run_min, walk_min)
        drift = abs(implied_km - labelled_km) / labelled_km
        if drift > DIST_DURATION_TOLERANCE:
            out.append(Violation(
                code="distance_duration_mismatch",
                severity="hard",
                detail=(f"'{name}': labelled {labelled_km:g}km but {reps}x"
                        f"{run_min:g}:{walk_min:g} walk-run implies ~{implied_km:.1f}km "
                        "— distance and duration/interval labels disagree"),
            ))
    return out


_STRENGTH_KW = ("strength", "kettlebell", "s&c", "conditioning", "weight")


def _is_strength(ev: dict) -> bool:
    """A strength session — by type (WeightTraining) or name (some are typed Workout)."""
    if str(ev.get("type") or "").strip().lower() == "weighttraining":
        return True
    nm = str(ev.get("name") or "").lower()
    return any(k in nm for k in _STRENGTH_KW)


def _normalise_day_rules(day_rules: dict | None) -> dict[str, set[int]]:
    """Turn {'swim_days': ['Tue','Thu'], …} into {'swim_days': {1,3}, …}.

    Only list-valued keys (the *_days) are day rules; scalar keys like
    strength_max are config that lives in the same dict and is skipped here."""
    out: dict[str, set[int]] = {}
    for key, days in (day_rules or {}).items():
        if not isinstance(days, (list, tuple, set)):
            continue
        wd = {_to_weekday(d) for d in days}
        wd.discard(None)
        out[key] = wd
    return out


def _dated_day_rules(day_rules: dict | None) -> dict[str, dict[int, date]]:
    """Parse the `<key>_expires` sidecars: {'bike_days': {5: date(2026, 9, 5)}}.

    A sidecar is a DICT, and `_normalise_day_rules` skips non-list values, so this
    encoding is a no-op for any code path that has not been taught about it — which
    matters, because config/athletes.json is hand-edited on the live box and is not
    deployed with the code. Unparseable day names or dates are dropped; a dropped
    entry means the day stays permanently permitted, i.e. exactly today's
    behaviour, never a new failure."""
    out: dict[str, dict[int, date]] = {}
    for key, val in (day_rules or {}).items():
        if not (isinstance(key, str) and key.endswith("_expires") and isinstance(val, dict)):
            continue
        base = key[: -len("_expires")]
        days: dict[int, date] = {}
        for name, until in val.items():
            wd = _to_weekday(name)
            if wd is None:
                continue
            try:
                days[wd] = date.fromisoformat(str(until)[:10])
            except (TypeError, ValueError):
                continue
        if days:
            out[base] = days
    return out


def _normalise_overrides(day_overrides: dict | None) -> dict[str, str]:
    """Keep only well-formed `family:YYYY-MM-DD` -> non-empty-prose entries.

    Fails CLOSED, like lib/day_overrides.load: a malformed register grants nothing,
    so a check can never be silenced by a broken file."""
    out = {}
    for k, v in (day_overrides or {}).items():
        if _OVERRIDE_KEY_RE.match(str(k)) and isinstance(v, str) and v.strip():
            out[str(k)] = v.strip()
    return out


def _drift_violations(overrides: dict[str, str], week_end: date) -> list["Violation"]:
    """HARD when the same sport+weekday has been DIRECTED DRIFT_THRESHOLD times in
    the DRIFT_WINDOW_DAYS ending at `week_end`. The remedy named in the detail is to
    amend day_rules — the config should describe what the athlete actually does."""
    win_start = week_end - timedelta(days=DRIFT_WINDOW_DAYS - 1)
    tally: dict[tuple[str, int], list[date]] = {}
    for k in overrides:
        fam, iso = k.split(":", 1)
        try:
            d = date.fromisoformat(iso)
        except ValueError:
            continue
        if win_start <= d <= week_end:
            tally.setdefault((fam, d.weekday()), []).append(d)
    out = []
    for (fam, wd), dates in sorted(tally.items()):
        if len(dates) < DRIFT_THRESHOLD:
            continue
        dow = (win_start + timedelta(days=(wd - win_start.weekday()) % 7)).strftime("%a")
        out.append(Violation(
            code="day_rules_drifted", severity="hard",
            detail=(f"{fam} has been coach-directed onto {dow} {len(dates)} times in the "
                    f"last {DRIFT_WINDOW_DAYS} days ({', '.join(d.isoformat() for d in sorted(dates))}) "
                    f"— that is the pattern, not an exception. Amend "
                    f"day_rules.{fam}_days (or add a dated {fam}_days_expires entry) "
                    f"instead of overriding it every week")))
    return out


def validate_week(
    events: list[dict],
    week_start: date,
    *,
    day_rules: dict | None = None,
    day_overrides: dict | None = None,
    weekly_tss_cap: float | None = None,
    weekly_tss_floor: float | None = None,
    tss_tolerance: float = 0.10,
    ctl_today: float | None = None,
    ramp_cap: float | None = None,
    strength_max: int | None = None,
    run_week_min_cap: float | None = None,
    run_long_min_cap: float | None = None,
    monotony_threshold: float = 2.0,
    distribution: dict | None = None,
    dist_tolerance_pp: float = DIST_TOLERANCE_PP,
) -> WeekReport:
    """Validate the planned sessions for the 7 days starting `week_start`.

    Only events whose date falls in [week_start, week_start+6] and that are
    WORKOUTs are considered. Every check is OPT-IN: a constraint is only asserted
    when its input is supplied (day_rules / weekly_tss_cap / ramp_cap), so a caller
    that has no structured day_rules yet simply gets the ramp/TSS checks.

    Returns a WeekReport (total_tss + violations + skipped). The caller decides
    what to do with hard violations (warn vs block) — this module never has side
    effects. A hard check whose input is missing is recorded in report.skipped
    rather than silently passing (fail-noisy): "validated" must never be
    conflated with "not actually checked".
    """
    rules = _normalise_day_rules(day_rules)
    dated = _dated_day_rules(day_rules)
    overrides = _normalise_overrides(day_overrides)
    week_end = week_start + timedelta(days=6)
    week_events = [
        e for e in events
        if _is_workout(e) and (_event_date(e) is not None)
        and week_start <= _event_date(e) <= week_end
    ]

    violations: list[Violation] = []
    skipped: list[str] = []

    # 1. Off-pattern sessions — a restricted sport scheduled on a day the athlete's
    #    day_rules do not (or no longer) permit. HARD when nobody asked for it;
    #    SOFT and still reported when the coach directed it (see module docstring).
    for e in week_events:
        sport = str(e.get("type") or "").strip().lower()
        rule_key = _SPORT_RULE.get(sport)
        if not rule_key or rule_key not in rules:
            continue
        allowed = rules[rule_key]
        if not allowed:
            continue
        d = _event_date(e)
        until = (dated.get(rule_key) or {}).get(d.weekday())
        if d.weekday() in allowed and (until is None or d <= until):
            continue
        if d.weekday() in allowed:
            # The day IS listed, but only as a time-boxed exception that has now run
            # out. Say so explicitly — "allowed days only: [...]" would be a lie
            # about a config the coach can read.
            reason = (f"day_rules.{rule_key} permits {d.strftime('%a')} only until "
                      f"{until.isoformat()} ({rule_key}_expires) and that dated "
                      f"exception has EXPIRED")
        else:
            reason = f"allowed days only: {sorted(allowed)} (Mon=0)"
        where = (f"{e.get('type')} on {d.isoformat()} ({d.strftime('%a')}) — "
                 f"'{e.get('name', '')}'")
        note = overrides.get(f"{_SPORT_FAMILY[sport]}:{d.isoformat()}")
        if note:
            violations.append(Violation(
                code=f"{sport}_directed_day",
                severity="soft",
                detail=f"{where}; off-pattern ({reason}) but COACH-DIRECTED: {note[:200]}",
            ))
        else:
            violations.append(Violation(
                code=f"{sport}_forbidden_day",
                severity="hard",
                detail=f"{where}; {reason}",
            ))

    violations.extend(_drift_violations(overrides, week_end))

    # 2. Weekly planned-TSS cap.
    total_tss = sum(_planned_load(e) for e in week_events)
    if weekly_tss_cap is None or weekly_tss_cap <= 0:
        skipped.append("weekly_tss_cap check SKIPPED — no cap supplied (pass the "
                       "blueprint hours ceiling); the week's total load is UNCHECKED")
    if weekly_tss_cap is not None and weekly_tss_cap > 0:
        ceiling = weekly_tss_cap * (1 + tss_tolerance)
        if total_tss > ceiling:
            violations.append(Violation(
                code="weekly_tss_cap",
                severity="hard",
                detail=(f"planned {total_tss:.0f} TSS in week of {week_start} "
                        f"exceeds cap {weekly_tss_cap:.0f} "
                        f"(+{tss_tolerance:.0%} tolerance = {ceiling:.0f})"),
            ))

    # 2b. Weekly planned-TSS FLOOR — an under-training week is as much a plan
    #     failure as an over-training one: a "training coach" must plan weeks
    #     that train the athlete (Jamie, 5 Jul 2026 — the 581-vs-816 week).
    #     Semantics: None = caller supplied nothing (recorded as skipped, loud);
    #     0 = explicitly no floor (deload/taper — legitimate unload weeks);
    #     >0 = hard violation below floor (5% grace).
    if weekly_tss_floor is None:
        skipped.append("weekly_tss_floor check SKIPPED — no floor supplied (pass the "
                       "required-tss floor, or 0 for deload/taper); under-training "
                       "is UNCHECKED")
    elif weekly_tss_floor > 0:
        floor = weekly_tss_floor * 0.95
        if total_tss < floor:
            ctl_note = ""
            if ctl_today is not None and ctl_today > 0:
                projected = compute_projected_ctl(ctl_today, total_tss, 1)
                ctl_note = (f"; fitness would go {ctl_today:.1f} → {projected:.1f} CTL "
                            f"({projected - ctl_today:+.1f}) — this week DETRAINS the athlete")
            violations.append(Violation(
                code="weekly_tss_floor",
                severity="hard",
                detail=(f"UNDER-TRAINING: planned {total_tss:.0f} TSS in week of "
                        f"{week_start} is below the floor {weekly_tss_floor:.0f} "
                        f"(-5% grace = {floor:.0f}){ctl_note}. Add volume or "
                        f"explicitly declare a deload/taper week"),
            ))

    # 3. Implied CTL ramp cap — project this week's load forward from today's CTL.
    if not (ctl_today is not None and ctl_today > 0 and ramp_cap is not None and ramp_cap > 0):
        missing = "ctl_today" if (ctl_today is None or ctl_today <= 0) else "ramp_cap"
        skipped.append(f"ctl_ramp check SKIPPED — no {missing} supplied; the week's "
                       "ramp rate is UNCHECKED")
    if ctl_today is not None and ctl_today > 0 and ramp_cap is not None and ramp_cap > 0:
        projected = compute_projected_ctl(ctl_today, total_tss, 1)
        ramp = projected - ctl_today
        if ramp > ramp_cap + 0.5:   # half-CTL grace for rounding
            violations.append(Violation(
                code="ctl_ramp",
                severity="hard",
                detail=(f"week of {week_start}: {total_tss:.0f} TSS implies "
                        f"+{ramp:.1f} CTL/wk (from {ctl_today:.1f}), over cap "
                        f"+{ramp_cap:.1f}"),
            ))

    # 4. Strength-session weekly cap (composition quality — soft; logged in warn mode,
    #    not block-worthy on its own). Only asserted when strength_max is supplied.
    if strength_max is not None and strength_max >= 0:
        n_strength = sum(1 for e in week_events if _is_strength(e))
        if n_strength > strength_max:
            violations.append(Violation(
                code="strength_over_cap",
                severity="soft",
                detail=(f"week of {week_start}: {n_strength} strength sessions "
                        f"planned, over the cap of {strength_max}"),
            ))

    # 5. Intensity-distribution drift (soft) — excess planned quality vs the
    #    blueprint phase's Z1–2 share. Only asserted when a distribution is supplied.
    if distribution:
        violations.extend(
            _check_distribution(week_events, week_start, distribution,
                                dist_tolerance_pp, skipped))

    # 6. Distance/duration internal consistency — walk-run sessions whose stated
    #    distance and stated run/walk cycle count imply different totals.
    violations.extend(_check_distance_duration(week_events))

    # Run-volume ceilings (audit P1-9): these previously lived only in the
    # Stage-1 generation path, so chat-path pushes and manual edits bypassed
    # them. Minutes-based here (planned events carry duration, not distance).
    run_events = [e for e in week_events
                  if str(e.get("type") or "").lower() in ("run", "trailrun", "virtualrun")]
    if run_events:
        mins = [float(e.get("moving_time") or 0) / 60.0 for e in run_events]
        if any(m <= 0 for m in mins):
            skipped.append(f"{sum(1 for m in mins if m <= 0)} run event(s) carry no "
                           "duration — run-volume checks incomplete")
        known = [m for m in mins if m > 0]
        if run_week_min_cap is None and known:
            skipped.append("run_weekly_volume check SKIPPED — no weekly run cap "
                           "supplied; run volume growth is UNCHECKED")
        elif run_week_min_cap and known and sum(known) > run_week_min_cap:
            violations.append(Violation(
                code="run_weekly_volume", severity="hard",
                detail=(f"planned run volume {sum(known):.0f}min exceeds cap "
                        f"{run_week_min_cap:.0f}min (<=10% w/w growth rule)")))
        if run_long_min_cap:
            for e, m in zip(run_events, mins):
                if m > run_long_min_cap:
                    violations.append(Violation(
                        code="run_long_volume", severity="hard",
                        detail=(f"'{e.get('name', '')}' {m:.0f}min exceeds long-run "
                                f"cap {run_long_min_cap:.0f}min (best of last 4 wks x1.15)")))

    # Foster monotony (soft): high mean/SD of daily load — same-size days with no
    # real rest multiply strain even under an in-cap weekly total (audit P1-3).
    if total_tss > 0 and len(week_events) >= 3:
        daily = [0.0] * 7
        for e in week_events:
            daily[(_event_date(e) - week_start).days] += _planned_load(e)
        mean = sum(daily) / 7.0
        var = sum((x - mean) ** 2 for x in daily) / 7.0
        sd = var ** 0.5
        monotony = (mean / sd) if sd > 0 else float("inf")
        if monotony > monotony_threshold:
            violations.append(Violation(
                code="monotony", severity="soft",
                detail=(f"week monotony {monotony if monotony != float('inf') else 99:.1f} "
                        f"(mean {mean:.0f}/sd {sd:.0f} daily TSS) over {monotony_threshold} — "
                        f"vary day sizes / protect a rest day (strain "
                        f"{total_tss * min(monotony, 99):.0f})")))

    return WeekReport(week_start=week_start, total_tss=total_tss,
                      violations=violations, skipped=skipped)


def validate_plan(
    events: list[dict],
    week_starts: list[date],
    **kwargs,
) -> list[WeekReport]:
    """Validate each week in a multi-week window; returns one WeekReport per week."""
    return [validate_week(events, ws, **kwargs) for ws in week_starts]
