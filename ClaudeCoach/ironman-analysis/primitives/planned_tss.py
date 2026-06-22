"""Planned-session TSS — deterministic, never LLM arithmetic.

The 11 Jun morning card showed "~35 TSS" for a swim whose plan event carried
load_target=60: the prompt pointed the model at icu_training_load (null on
planned events) and invited it to estimate. TSS now resolves here, in order:

  1. load_target          — the plan's own number, authoritative
  2. icu_training_load    — set once ICU analyses a structured workout
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
    """IF for a named intensity zone in a sport. Falls back to the sport default."""
    sp = _norm_sport(sport)
    return _ZONE_IF.get(sp, {}).get((zone or "").lower().strip(),
                                    _ZONE_DEFAULT_IF.get(sp, 0.75))


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
    sp = _norm_sport(sport)
    b = _ZONE_BAND.get(sp, {}).get((zone or "").lower().strip())
    if b:
        return b
    pct = int(round((intensity if intensity is not None
                     else segment_if(sport, zone)) * 100))
    return (max(pct - 4, 1), pct + 4)


def render_workout(sport: str, segments: list) -> dict:
    """Render time-at-intensity segments into an Intervals.icu STRUCTURED workout
    (the `description` text push_workout sends → parsed into steps → synced to
    Garmin). Bike steps are %FTP power; run/swim are %threshold pace.

    segments: flat {"minutes":N,"zone":"css"} (or "if"), and/or repeat blocks
    {"repeat":N,"steps":[...]}. Returns {description, tss, duration_min, steps}.
    `tss` is the deterministic estimate (ICU recomputes its own on push)."""
    sp = _norm_sport(sport)
    suffix = "" if sp == "bike" else " Pace"   # bare % = power; "% Pace" = pace
    lines, flat = [], []

    def _line(seg):
        """Return the structured-text line for a segment, or None; never mutates flat."""
        mins = int(round(float(seg.get("minutes") or seg.get("min") or 0)))
        if mins <= 0:
            return None, 0
        zone = seg.get("zone")
        lo, hi = (_band(sport, zone, float(seg["if"])) if seg.get("if") is not None
                  else _band(sport, zone))
        # NO trailing zone label: a token like "Z2" makes ICU parse the step as power
        # ZONE 2 and discard the explicit %-range target (-> empty chart). The %-range
        # IS the target; the human zone name lives in description_raw.
        return f"- {mins}m {lo}-{hi}%{suffix}", mins

    def _flat(seg, mins):
        flat.append({"minutes": mins, "zone": seg.get("zone"), "if": seg.get("if")})

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

    for field, source in (("load_target", "plan"), ("icu_training_load", "icu")):
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
        rows.append(f"- {r['name']} — {r['duration_min']} min · {r['tss']} TSS ({src})")
    return "\n".join(rows)
