"""Planned-session TSS — deterministic, never LLM arithmetic.

The 11 Jun morning card showed "~35 TSS" for a swim whose plan event carried
load_target=60: the prompt pointed the model at icu_training_load (null on
planned events) and invited it to estimate. TSS now resolves here, in order:

  1. icu_training_load    — what ICU computes from the synced structured workout
                            (authoritative; this is the number that reaches Garmin)
  2. load_target          — the plan's own estimate (fallback; often the only number
                            for structure-less sessions like text swims)
  3. calculated           — duration × IF² × 100, IF from the coarse session
                            type (the same classifier the backstop uses)

IF values are session-AVERAGE intensities (a "CSS swim" hour includes rests
and drills, so its IF is well below the CSS pace itself) — standard estimates,
tune against logged history if they drift from ICU's post-hoc numbers.
"""
from __future__ import annotations

import re

from primitives.modulation import classify_session_type

_IF_BY_TYPE = {
    "bike_threshold": 0.90, "bike_vo2": 0.95, "bike_race_pace": 0.80, "bike_z2": 0.65,
    "run_quality": 0.85, "run_long": 0.75, "run_easy": 0.70,
    "brick": 0.75,
}
_SWIM_IF = [  # first keyword match on the name wins
    (("css", "threshold", "test"), 0.85),
    (("race",), 0.80),
    (("drill", "technique", "recovery"), 0.65),
]
_SWIM_IF_DEFAULT = 0.75
_STRENGTH_TSS_PER_MIN = 0.5

_DUR_DEFAULT_MIN = {
    "bike_threshold": 75, "bike_vo2": 75, "bike_race_pace": 90, "bike_z2": 90,
    "run_quality": 60, "run_long": 90, "run_easy": 50, "brick": 75,
    "swim": 45, "strength": 40,
}

# ── Segment-based planned TSS (the calculable path) ──────────────────────────
# A structured session is time-at-intensity; TSS = Σ (hours_i × IF_i²) × 100.
# Segment IFs below are intensity factors relative to threshold (CSS for swim,
# threshold pace for run, FTP for bike). Calibrated so a typical session
# integrates to the session-average IF seen in logged history (e.g. Jamie's CSS
# swims average ~0.90 → a WU/CSS-main/CD split integrates to ~0.88–0.92).
_ZONE_IF = {
    "swim": {"recovery": 0.60, "easy": 0.72, "warmup": 0.70, "cooldown": 0.65,
             "aerobic": 0.80, "drill": 0.68, "kick": 0.70, "pull": 0.85,
             "steady": 0.88, "race": 0.95, "css": 1.00, "threshold": 1.00,
             "speed": 1.08, "sprint": 1.12},
    "run":  {"recovery": 0.60, "easy": 0.83, "z2": 0.83, "warmup": 0.68,
             "cooldown": 0.62, "steady": 0.83, "z3": 0.83, "tempo": 0.88,
             "threshold": 0.97, "css": 0.97, "interval": 1.05, "vo2": 1.06,
             "hill": 0.95, "sprint": 1.15},
    "bike": {"recovery": 0.50, "z1": 0.55, "z2": 0.65, "endurance": 0.65,
             "warmup": 0.60, "cooldown": 0.55, "tempo": 0.80, "z3": 0.80,
             "sweetspot": 0.90, "ss": 0.90, "threshold": 0.95, "race": 0.80,
             "vo2": 1.05, "anaerobic": 1.20},
}
_ZONE_DEFAULT_IF = {"swim": 0.80, "run": 0.75, "bike": 0.68, "brick": 0.78}

# ── the OTHER zone vocabulary: canonical TID band labels ─────────────────────
# _ZONE_IF / _ZONE_BAND above are keyed by TRAINING SYSTEM (threshold, sweetspot,
# vo2, css). config/session-library.json keys the same intensities by canonical BAND
# LABEL (bike Z4 = 90-105% FTP, swim Z4 = 94.3-100% CSS ...), and the planning brief
# hands Stage 1 `zone: "Z4"` for every quality type - so proposals come back with band
# labels, not system names. Unmapped, they fell through to _ZONE_DEFAULT_IF: on
# 9 Aug 2026 every athlete's week hard-blocked on name_intensity_mismatch because
# a bike "Threshold 3x8" rendered its Z4 reps at 64-72% FTP (bike default 0.68) and a
# swim "CSS set" rendered its Z4 reps at 76-84% pace (swim default 0.80) - and the
# same fallback put every TID-labelled swim WARM-UP at 76-84% of CSS.
# These are ALIASES, deliberately not extra _ZONE_BAND keys: name_intensity_claim reads
# _ZONE_BAND to decide which claims to assert, and a new key there would ARM a new hard
# check (e.g. a swim "vo2" band) - a new hard rule in this path is how the outage happened.
# IFs agree with scripts/stage1-plan.py:_ZLABEL_IF, which scores the same labels for the
# distribution; the two tables must not drift.
_TID_ALIAS = {
    "bike": {"z4": "threshold", "z5": "vo2", "z6": "anaerobic", "z7": "anaerobic"},
    "run":  {"z1": "recovery", "z4": "threshold", "z5": "vo2", "z5a": "interval",
             "z5b": "vo2", "z5c": "sprint"},
    # swim Z5a is the library's swim `vo2` (if 1.05); it renders as `speed` (1.08) rather
    # than gaining a swim "vo2" band, for the arming reason above. The EASY end matters as
    # much: swim carries no z1/z2 band either, so a TID-labelled swim warm-up rendered at
    # 76-84% of CSS (the swim default 0.80) - near race pace, as a warm-up.
    "swim": {"z1": "recovery", "z2": "easy", "z3": "steady", "z4": "css",
             "z5": "speed", "z5a": "speed", "z5b": "speed", "z5c": "sprint"},
}
# Canonical %-ranges for those labels (config/session-library.json → canonical_zones).
# Used to decide whether a session NAME's claim lives inside the label's own band - see
# _refine_to_claim. Bare "z5" on run/swim spans all three Z5 subzones.
_TID_RANGE = {
    "bike": {"z1": (0, 55), "z2": (55, 75), "z3": (75, 90), "z4": (90, 105),
             "z5": (105, 120), "z6": (120, 150), "z7": (150, 999)},
    "run":  {"z1": (0, 77.5), "z2": (77.5, 87.7), "z3": (87.7, 94.3), "z4": (94.3, 100),
             "z5": (100, 999), "z5a": (100, 103.4), "z5b": (103.4, 111.5), "z5c": (111.5, 999)},
}
_TID_RANGE["swim"] = _TID_RANGE["run"]      # same %-of-CSS scale as run's %-of-pace


def _norm_sport(sport: str) -> str:
    s = (sport or "").lower()
    if "swim" in s:
        return "swim"
    if "run" in s:
        return "run"
    if "ride" in s or "bike" in s or "cycl" in s or "brick" in s:
        return "bike"
    return s


def segment_if(sport: str, zone: str) -> float:
    """IF for a named intensity zone in a sport. Accepts a training-system name
    (threshold/sweetspot/css) or a canonical TID band label (Z4/Z5a). Falls back to
    the sport default only for a zone in NEITHER vocabulary."""
    sp = _norm_sport(sport)
    z = (zone or "").lower().strip()
    table = _ZONE_IF.get(sp, {})
    if z not in table:
        z = _TID_ALIAS.get(sp, {}).get(z, z)
    return table.get(z, _ZONE_DEFAULT_IF.get(sp, 0.75))


# %-of-threshold bands per zone for STRUCTURED workout steps (what syncs to
# Garmin). Bike bands are %FTP (power); run/swim bands are %threshold-pace.
# Centred on the same intensities as _ZONE_IF so computed TSS and the pushed
# structure agree.
_ZONE_BAND = {
    "swim": {"recovery": (58, 66), "easy": (70, 80), "warmup": (66, 74),
             "cooldown": (60, 70), "aerobic": (76, 84), "drill": (62, 72),
             "kick": (64, 74), "pull": (82, 88), "steady": (84, 92),
             "race": (92, 98), "css": (97, 103), "threshold": (98, 104),
             "speed": (104, 112), "sprint": (108, 118)},
    "run":  {"recovery": (56, 64), "easy": (78, 88), "z2": (78, 88),
             "warmup": (64, 72), "cooldown": (58, 66), "steady": (80, 86),
             "z3": (80, 86), "tempo": (85, 91), "threshold": (95, 101),
             "css": (95, 101), "interval": (102, 108), "vo2": (103, 110),
             "hill": (92, 98), "sprint": (112, 120)},
    "bike": {"recovery": (45, 55), "z1": (50, 58), "z2": (60, 70),
             "endurance": (60, 70), "warmup": (55, 65), "cooldown": (50, 60),
             "tempo": (76, 84), "z3": (76, 84), "sweetspot": (88, 94),
             "ss": (88, 94), "threshold": (95, 102), "race": (76, 84),
             "vo2": (105, 118), "anaerobic": (118, 135)},
}


def _band(sport: str, zone: str, intensity: float = None):
    """%-range for a segment.

    An explicit IF wins over a coarse TID BAND LABEL (Z3 spans 75-90% FTP; the number is
    the specific one, and it is what tss_from_segments already scores the segment at - so
    the pushed steps and the computed TSS agree, which the _ZONE_BAND docstring claims and
    this did not deliver). A zone named for a training SYSTEM keeps its band even against
    a conflicting IF: an IF that undershoots its own zone word would otherwise
    under-prescribe a named session and hard-block the week on the name's claim.
    Then the label alias, then the sport default.
    """
    sp = _norm_sport(sport)
    z = (zone or "").lower().strip()
    if intensity is not None and (z in _TID_RANGE.get(sp, {})
                                  or z not in _ZONE_BAND.get(sp, {})):
        pct = int(round(intensity * 100))
        return (max(pct - 4, 1), pct + 4)
    b = (_ZONE_BAND.get(sp, {}).get(z)
         or _ZONE_BAND.get(sp, {}).get(_TID_ALIAS.get(sp, {}).get(z, "")))
    if b:
        return b
    pct = int(round(segment_if(sport, zone) * 100))
    return (max(pct - 4, 1), pct + 4)


def _refine_to_claim(sport: str, zone: str, claim_zone: str | None) -> str:
    """A coarse TID band label, narrowed to the system the session NAME claims.

    A band label is a FAMILY, not a prescription: the library gives both `tempo` (0.80)
    and `sweetspot` (0.90, band 88-94) the label Z3, so "Sweetspot 2x20" arrived with
    Z3 segments and rendered at 76-84% FTP - short of the 88% its own name promises, and
    a hard block on the whole week (9 Aug 2026). Where the name names a system that lives
    INSIDE the label's canonical range, that system is the prescription.

    Bounded to the label's own family on purpose: a name claiming VO2 over Z2 segments is
    an over-claim, not a coarse label, and must keep failing the check.

    Bounded UPWARD only, too. Several systems share a family, so a "VO2 8x400" run whose
    reps are labelled Z5c (the library's `reps`, 112-120% pace) would otherwise be refined
    DOWN to the vo2 band's 103-110% - softening a prescription the proposal asked for, in
    a session that already satisfied its own claim.
    """
    if not claim_zone:
        return zone
    sp = _norm_sport(sport)
    z = (zone or "").lower().strip()
    rng = _TID_RANGE.get(sp, {}).get(z)
    claim_band = _ZONE_BAND.get(sp, {}).get(claim_zone)
    if not rng or not claim_band:
        return zone
    if not (rng[0] <= claim_band[0] < rng[1]):
        return zone
    raw = (_ZONE_BAND.get(sp, {}).get(z)
           or _ZONE_BAND.get(sp, {}).get(_TID_ALIAS.get(sp, {}).get(z, "")))
    return claim_zone if (raw is None or claim_band[0] > raw[0]) else zone


# ── name-vs-structure ────────────────────────────────────────────────────────
# A session NAME that claims an intensity its STEPS do not contain (4 Aug 2026:
# Kathryn's "VO2 Reps 6x3min + sweetspot" whose hardest step was 80-86% pace, a
# "CSS 5x400" swim of five aerobic blocks with no 400s, a brick "+ VO2" topping out
# at 84% FTP). It was invisible while her ICU run threshold was unset, because every
# "% Pace" step then resolved to nothing on the watch; with a threshold set, the same
# session actively prescribes ~7:00/km as "VO2". The steps are the prescription and
# the name is what the athlete reads, so a divergence is a defect either way.
#
# Each claim maps to the zone whose band the steps must REACH, resolved through
# _ZONE_BAND so a band edit cannot leave this check asserting a stale number. The
# bar is the band's LOWER edge: a "threshold" session must have a step reaching
# 95% (run), not the full 95-101%.
_NAME_CLAIMS = (
    ("vo2", "vo2"), ("v02", "vo2"), ("aerobic capacity", "vo2"),
    ("css", "css"), ("threshold", "threshold"), ("cruise", "threshold"),
    ("sweetspot", "sweetspot"), ("sweet spot", "sweetspot"),
    ("race-pace", "race"), ("race pace", "race"), ("tempo", "tempo"),
)
# %LTHR cannot be compared with %pace or %FTP: threshold HR is ~100% LTHR and VO2
# work barely exceeds it, so a VO2 step targeted by HR tops out near 102%. An
# HR-targeted step counts as reaching any claim once it is at threshold HR.
_HR_SATISFIES_AT_PCT = 97

# A step line, e.g. "- 5m 97-101% Pace 94-100% LTHR". ICU accepts more than one
# target on a step, so each %-range is read SEPARATELY with its own unit word: the
# first version of this matched once per line and read "Pace ... LTHR" as one HR
# target, which mis-scored every dual-target step.
_STEP_LINE_RE = re.compile(r"^-\s*\d+(?:\.\d+)?\s*[ms]\b", re.IGNORECASE)
_TARGET_RE = re.compile(r"(\d+)\s*-\s*(\d+)\s*%\s*([A-Za-z]*)", re.IGNORECASE)


_SPORT_WORDS = {"run": "run", "ride": "bike", "bike": "bike", "cycl": "bike",
                "swim": "swim"}


def _claims_another_sport(name_low: str, at: int, word: str, own: str) -> bool:
    """True when this claim belongs to a DIFFERENT sport's leg of the session.

    A brick lives on a Ride event but is named for both legs: "Brick: Z2 ride 70min
    + tempo run off the bike" claims tempo for the RUN, and the bike steps rightly
    have none. Without this, every brick name trips the check.

    The sport word must be ADJACENT to the claim ("tempo run", "VO2 bike"). A wider
    window would swallow the brick idiom "tempo off the bike" on the run leg itself,
    where the claim really is the run's.
    """
    after = name_low[at + len(word):at + len(word) + 12]
    before = name_low[max(0, at - 8):at]
    for sw, fam in _SPORT_WORDS.items():
        if fam == own:
            continue
        if re.match(rf"\s*{sw}", after) or re.search(rf"{sw}\w*\s*$", before):
            return True
    return False


def name_intensity_claim(sport: str, name: str) -> tuple[str, int] | None:
    """(claimed word, minimum % the steps must reach) for a session name, or None.

    The most demanding claim in the name wins, so "VO2 + sweetspot" is judged on
    VO2. A claim the sport has no band for (a "sweetspot" swim) is not asserted, and
    nor is one attached to another sport's leg.
    """
    low = (name or "").lower()
    own = _norm_sport(sport)
    bands = _ZONE_BAND.get(own, {})
    best = None
    for word, zone in _NAME_CLAIMS:
        if zone not in bands:
            continue
        at = low.find(word)
        while at != -1:
            if not _claims_another_sport(low, at, word, own):
                need = bands[zone][0]
                if best is None or need > best[1]:
                    best = (word, need)
                break
            at = low.find(word, at + 1)
    return best


def claimed_zone(sport: str, name: str) -> str | None:
    """The _ZONE_BAND zone a session name claims ("Sweetspot 2x20" -> "sweetspot"), or
    None. Same claim resolution as name_intensity_mismatch uses, so what the renderer
    builds and what the check demands can never come from two different readings."""
    claim = name_intensity_claim(sport, name)
    return dict(_NAME_CLAIMS)[claim[0]] if claim else None


def top_step_pct(description: str) -> tuple[int | None, int | None]:
    """(hardest %pace-or-%FTP target, hardest %LTHR target) over rendered steps.

    Reads the TOP of each step's range, so a 97-101% step counts as 101.
    """
    top_main = top_hr = None
    for line in (description or "").splitlines():
        line = line.strip()
        if not _STEP_LINE_RE.match(line):
            continue
        for m in _TARGET_RE.finditer(line):
            hi, unit = int(m.group(2)), (m.group(3) or "").lower()
            if unit in ("lthr", "hr", "maxhr"):
                top_hr = hi if top_hr is None else max(top_hr, hi)
            else:
                top_main = hi if top_main is None else max(top_main, hi)
    return top_main, top_hr


def name_intensity_mismatch(sport: str, name: str, description: str) -> dict | None:
    """The session's name claims an intensity its steps never reach. None if fine.

    Returns {claim, required, found}. Silent when the name makes no claim, or when
    there are no parseable steps at all - an unstructured session is a different
    defect, already caught as STRUCTURE/no-steps.
    """
    claim = name_intensity_claim(sport, name)
    if not claim:
        return None
    word, need = claim
    top_main, top_hr = top_step_pct(description)
    if top_main is None and top_hr is None:
        return None
    # HR can only SATISFY a claim when it is the sole target on the steps. Since the
    # house style is now a pace target with an HR guardrail on every quality step,
    # letting HR win outright would switch this check off for exactly the sessions it
    # exists to police: "VO2 6x3min" over "- 3m 80-86% Pace 94-100% LTHR" would pass.
    if top_main is not None:
        if top_main >= need:
            return None
    elif top_hr >= _HR_SATISFIES_AT_PCT:
        return None
    found = top_main if top_main is not None else top_hr
    return {"claim": word, "required": need, "found": found}


def render_workout(sport: str, segments: list, name: str = "") -> dict:
    """Render time-at-intensity segments into an Intervals.icu STRUCTURED workout
    (the `description` text push_workout sends → parsed into steps → synced to
    Garmin). Bike steps are %FTP power; run/swim are %threshold pace.

    segments: flat {"minutes":N,"zone":"css"} (or "if"), and/or repeat blocks
    {"repeat":N,"steps":[...]}. Returns {description, tss, duration_min, steps}.
    `tss` is the deterministic estimate (ICU recomputes its own on push).

    `name` is OPTIONAL and only narrows a coarse TID band label to the system the name
    claims (_refine_to_claim); omitting it reproduces the old behaviour exactly, which is
    why lib/plan_tools.py's manual `render-workout` CLI is left as it is (off the Sunday
    build path, and owned by a concurrent ticket)."""
    sp = _norm_sport(sport)
    suffix = "" if sp == "bike" else " Pace"   # bare % = power; "% Pace" = pace
    claim_zone = claimed_zone(sport, name) if name else None
    lines, flat = [], []

    def _zone_of(seg):
        """The segment's zone, with a coarse TID label narrowed to the name's claim.
        An explicit `if` is left to speak for itself (it is the more specific target and
        _band prefers it), so refinement never contradicts a number the proposal gave."""
        zone = seg.get("zone")
        if seg.get("if") is not None:
            return zone
        return _refine_to_claim(sport, zone, claim_zone)

    def _line(seg):
        """Return the structured-text line for a segment, or None; never mutates flat.

        Sub-minute segments (e.g. a 15-20s swim rest) used to round to 0 minutes
        and silently vanish from the pushed workout, leaving no rest cue on the
        watch (confirmed 2026-07-09). Durations under 60s now render in seconds —
        ICU's structured-text format accepts "Ns" steps same as "Nm".
        """
        raw_min = float(seg.get("minutes") or seg.get("min") or 0)
        secs = int(round(raw_min * 60))
        if secs <= 0:
            return None, 0
        zone = _zone_of(seg)
        lo, hi = (_band(sport, zone, float(seg["if"])) if seg.get("if") is not None
                  else _band(sport, zone))
        # NO trailing zone label: a token like "Z2" makes ICU parse the step as power
        # ZONE 2 and discard the explicit %-range target (-> empty chart). The %-range
        # IS the target; the human zone name lives in description_raw.
        if secs < 60:
            return f"- {secs}s {lo}-{hi}%{suffix}", secs / 60.0
        mins = int(round(secs / 60.0))
        return f"- {mins}m {lo}-{hi}%{suffix}", mins

    def _flat(seg, mins):
        # The REFINED zone, so the computed TSS is the load of the steps actually pushed.
        flat.append({"minutes": mins, "zone": _zone_of(seg), "if": seg.get("if")})

    # Render FLAT — one line per interval, no "Nx" header (ICU's API collapses
    # repeat-header blocks to unique steps; a fully-expanded flat list parses into
    # the correct step sequence and ICU then auto-computes the load — verified).
    for seg in segments:
        if seg.get("repeat") and seg.get("steps"):
            for _ in range(int(seg["repeat"])):
                for sub in seg["steps"]:
                    line, mins = _line(sub)
                    if line:
                        lines.append(line)
                        _flat(sub, mins)
        else:
            line, mins = _line(seg)
            if line:
                lines.append(line)
                _flat(seg, mins)

    calc = tss_from_segments(sport, [{"minutes": s["minutes"], "zone": s["zone"],
                                      **({"if": s["if"]} if s.get("if") is not None else {})}
                                     for s in flat])
    return {"description": "\n".join(lines), "tss": calc["tss"],
            "duration_min": calc["duration_min"], "avg_if": calc["avg_if"],
            "steps": len([l for l in lines if l.strip().startswith("-")])}


def tss_from_segments(sport: str, segments: list) -> dict:
    """Calculable planned TSS from time-at-intensity.

    segments: list of {"minutes": N, "zone": "css"} and/or {"minutes": N, "if": F}.
    An explicit `if` wins; otherwise the zone is looked up for the sport. Returns
    {tss, duration_min, avg_if, segments:[{minutes, if, zone, tss}]}.
    This is the deterministic source planners should use to SET load_target —
    never an LLM guess, never a flat per-session rate.
    """
    rows, total_tss, total_min = [], 0.0, 0.0
    for seg in segments:
        mins = float(seg.get("minutes") or seg.get("min") or 0)
        if mins <= 0:
            continue
        zone = seg.get("zone")
        intensity = float(seg["if"]) if seg.get("if") is not None else segment_if(sport, zone)
        seg_tss = mins / 60.0 * intensity ** 2 * 100.0
        total_tss += seg_tss
        total_min += mins
        rows.append({"minutes": round(mins), "if": round(intensity, 3),
                     "zone": zone, "tss": round(seg_tss, 1)})
    avg_if = (total_tss / (total_min / 60.0 * 100.0)) ** 0.5 if total_min else 0.0
    return {"tss": int(round(total_tss)), "duration_min": int(round(total_min)),
            "avg_if": round(avg_if, 3), "segments": rows}


def _duration_min(event: dict, session_type: str) -> int:
    mt = event.get("moving_time")
    if mt:
        return int(float(mt) / 60)
    name = str(event.get("name") or "").lower()
    m = re.search(r"(\d+)\s*hr(?:\s*(\d+)\s*min)?", name)
    if m:
        return int(m.group(1)) * 60 + int(m.group(2) or 0)
    m = re.search(r"~?\s*(\d+)\s*min", name)
    if m:
        return int(m.group(1))
    return _DUR_DEFAULT_MIN.get(session_type, 60)


def planned_session_tss(event: dict) -> dict:
    """{tss, source, duration_min, name} for a planned WORKOUT event.
    source ∈ plan | icu | calculated."""
    name = str(event.get("name") or "")
    st = classify_session_type(event.get("type", ""), name)
    dur = _duration_min(event, st)

    for field, source in (("icu_training_load", "icu"), ("load_target", "plan")):
        v = event.get(field)
        if v:
            return {"tss": int(round(float(v))), "source": source,
                    "duration_min": dur, "name": name}

    if st == "strength":
        tss = dur * _STRENGTH_TSS_PER_MIN
    elif st == "swim":
        nl = name.lower()
        intensity = next((i for kws, i in _SWIM_IF if any(k in nl for k in kws)),
                         _SWIM_IF_DEFAULT)
        tss = dur / 60 * intensity ** 2 * 100
    else:
        intensity = _IF_BY_TYPE.get(st, 0.70)
        tss = dur / 60 * intensity ** 2 * 100
    return {"tss": int(round(tss)), "source": "calculated",
            "duration_min": dur, "name": name}


def hourly_rates_line() -> str:
    """Standard TSS-per-hour rates derived from the IF table — for prompts that
    quote lever estimates (e.g. '+30 min Z2 ride ≈ +21 TSS'), so prose rates can
    never drift from the calculation."""
    rates = {st: round(i ** 2 * 100) for st, i in _IF_BY_TYPE.items()}
    swim_css = round(_SWIM_IF[0][1] ** 2 * 100)
    swim_easy = round(_SWIM_IF_DEFAULT ** 2 * 100)
    return (f"Z2 ride {rates['bike_z2']}/hr · threshold ride {rates['bike_threshold']}/hr · "
            f"easy run {rates['run_easy']}/hr · quality run {rates['run_quality']}/hr · "
            f"long run {rates['run_long']}/hr · CSS swim {swim_css}/hr · easy swim {swim_easy}/hr · "
            f"brick {rates['brick']}/hr")


def planned_sessions_block(events: list[dict]) -> str:
    """Prompt-ready lines for today's planned workouts, or '' when none."""
    rows = []
    for e in events or []:
        if (e.get("category") or "WORKOUT").upper() != "WORKOUT":
            continue
        r = planned_session_tss(e)
        src = {"plan": "from plan", "icu": "from ICU", "calculated": "calculated"}[r["source"]]
        rows.append(f"- {r['name']} — {r['duration_min']} min · {r['tss']} Load ({src})")
    return "\n".join(rows)
