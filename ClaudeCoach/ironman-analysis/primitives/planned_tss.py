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
