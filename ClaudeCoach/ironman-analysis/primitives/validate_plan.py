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
# time-in-zone guidance. We can't see inside a session, but we CAN classify whole
# sessions easy vs quality (the same coarse classifier the prescription backstop
# uses) and flag a week whose easy share falls far below the phase's Z1–2 target.
# Tolerance is generous (interval sessions contain Z2 warmup/recovery the binary
# classification can't credit) and the check only fires on EXCESS QUALITY — extra
# easy volume is never a safety problem, and undershooting load is the gap-check's
# job. Swims and bricks are excluded: name-based quality detection is unreliable
# for swims, and bricks are mixed by definition.

_EASY_TYPES = {"bike_z2", "run_easy", "run_long"}
_QUALITY_TYPES = {"bike_threshold", "bike_vo2", "bike_race_pace", "run_quality"}
_SPORT_BUCKET = {"ride": "Bike", "virtualride": "Bike", "gravelride": "Bike", "run": "Run"}

# Fallback planned minutes when an event carries no moving_time (planned events
# often don't) — coarse, mirrors the planner's own per-type defaults.
_TYPE_FALLBACK_MIN = {
    "bike_z2": 90, "bike_threshold": 75, "bike_vo2": 75, "bike_race_pace": 90,
    "run_easy": 50, "run_long": 90, "run_quality": 60,
}

DIST_TOLERANCE_PP = 12.0   # percentage points below the Z1–2 target before flagging
DIST_MIN_SESSIONS = 2      # don't judge a sport on a single session
DIST_MIN_MINUTES  = 120    # ...or on under two hours of planned work


def _easy_target_pct(dist_row) -> float | None:
    """Leading Z1–2 share from a row like '75% Z1–2 / 15% Z3 / 10% Z4–5'."""
    m = re.match(r"\s*(\d+(?:\.\d+)?)\s*%\s*Z1", str(dist_row or ""))
    return float(m.group(1)) if m else None


def _check_distribution(week_events: list[dict], week_start: date,
                        distribution: dict, tolerance_pp: float) -> list["Violation"]:
    buckets: dict[str, dict[str, float]] = {}   # sport → {easy_min, total_min, n}
    for e in week_events:
        sport = _SPORT_BUCKET.get(str(e.get("type") or "").strip().lower())
        if not sport:
            continue
        st = classify_session_type(e.get("type", ""), e.get("name", ""))
        if st not in _EASY_TYPES and st not in _QUALITY_TYPES:
            continue
        mins = (float(e.get("moving_time") or 0) / 60) or _TYPE_FALLBACK_MIN.get(st, 60)
        b = buckets.setdefault(sport, {"easy": 0.0, "total": 0.0, "n": 0})
        b["total"] += mins
        b["n"] += 1
        if st in _EASY_TYPES:
            b["easy"] += mins

    out: list[Violation] = []
    for sport, b in buckets.items():
        target = _easy_target_pct((distribution or {}).get(sport))
        if target is None or b["n"] < DIST_MIN_SESSIONS or b["total"] < DIST_MIN_MINUTES:
            continue
        easy_pct = b["easy"] / b["total"] * 100
        if easy_pct < target - tolerance_pp:
            out.append(Violation(
                code="intensity_distribution",
                severity="soft",
                detail=(f"week of {week_start}: {sport} is {easy_pct:.0f}% easy by "
                        f"session time vs the phase target {target:.0f}% Z1–2 "
                        f"(tolerance −{tolerance_pp:.0f}pp) — too much quality planned"),
            ))
    return out


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
# Ankle-safe: never emit a "too little VO2" floor for the RUN (impact); the bike carries the
# top-end. Run Z4-5 CEILING still applies. (sport, zone) pairs whose FLOOR is suppressed.
_FLOOR_SUPPRESSED = {("Run", "high")}


def zone_band_deviations(per_sport, per_sport_targets, *, deload=False,
                        injury_bands=None, min_minutes=180):
    """Symmetric per-sport per-zone deviations vs [floor_edge, ceil_edge] (default
    [t - tol_lo, t + tol_hi] from _ZONE_BANDS). Returns [{sport, zone, kind, actual, target,
    dev}] (dev >= 0). Floors dropped on deload (ceilings stay); run Z4-5 floor suppressed;
    Z1-2 not banded. Single source for the advisory (validate) and the picker (stage1).
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
                if deload or (sport, zone) in _FLOOR_SUPPRESSED:
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


def validate_week(
    events: list[dict],
    week_start: date,
    *,
    day_rules: dict | None = None,
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
    week_end = week_start + timedelta(days=6)
    week_events = [
        e for e in events
        if _is_workout(e) and (_event_date(e) is not None)
        and week_start <= _event_date(e) <= week_end
    ]

    violations: list[Violation] = []
    skipped: list[str] = []

    # 1. Wrong-day sessions — a restricted sport scheduled on a forbidden weekday.
    for e in week_events:
        sport = str(e.get("type") or "").strip().lower()
        rule_key = _SPORT_RULE.get(sport)
        if not rule_key or rule_key not in rules:
            continue
        allowed = rules[rule_key]
        if not allowed:
            continue
        d = _event_date(e)
        if d.weekday() not in allowed:
            violations.append(Violation(
                code=f"{sport}_forbidden_day",
                severity="hard",
                detail=(f"{e.get('type')} on {d.isoformat()} ({d.strftime('%a')}) — "
                        f"'{e.get('name', '')}'; allowed days only: "
                        f"{sorted(allowed)} (Mon=0)"),
            ))

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
            _check_distribution(week_events, week_start, distribution, dist_tolerance_pp))

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
