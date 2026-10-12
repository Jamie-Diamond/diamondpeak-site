"""Keep model prompts under 100k tokens without dropping what the job uses (Jamie, 12 Oct
2026: Haiku 5.5 bills a prompt over 100k at 5x, "for others can we try to get it to 100k,
only if that doesn't impact quality").

Measured before this: the weekly summary pasted the whole current-state.md (Jamie's
watchdog-flag log alone was ~48k tokens, months of dated entries), raw Intervals.icu JSON
pretty-printed with every field, and then had the model Read the same file again to edit
it, so 41% of its calls passed 100k (max 268k). What a job reads is the same information
in fewer tokens here: recent dated log entries, the fields it uses, compact JSON.
"""
from __future__ import annotations

import json
import re
from datetime import date, timedelta

# A dated log line: a bullet, table row or bold lead starting with an ISO date.
_DATED = re.compile(r"^\s*(?:[-*|]\s*)?(?:\*\*)?\s*(\d{4}-\d{2}-\d{2})")


def recent_log(md: str, today: date, keep_days: int = 14) -> str:
    """current-state.md with dated log lines older than `keep_days` removed (and any
    indented lines that continue them). Undated lines - headings, standing facts,
    protocols, open actions - are kept whole. Each section that lost lines says how many."""
    if not md:
        return md
    cut = (today - timedelta(days=keep_days)).isoformat()
    out, dropped, skipping = [], 0, False

    def flush_note():
        nonlocal dropped
        if dropped:
            out.append(f"_({dropped} older dated entries omitted)_")
            dropped = 0
    for line in md.splitlines():
        if line.startswith("#"):
            flush_note()
            skipping = False
            out.append(line)
            continue
        m = _DATED.match(line)
        if m:
            skipping = m.group(1) < cut
            if skipping:
                dropped += 1
                continue
            out.append(line)
            continue
        if skipping and (line.startswith(("  ", "\t")) and line.strip()):
            continue                                   # continuation of a dropped entry
        skipping = False
        out.append(line)
    flush_note()
    return "\n".join(out)


def pick(rows, fields) -> list:
    """Only `fields` of each dict, empty values dropped."""
    out = []
    for r in rows or []:
        if isinstance(r, dict):
            out.append({k: r[k] for k in fields if r.get(k) not in (None, "", [], {})})
    return out


def compact(obj) -> str:
    """JSON without pretty-printing: the same data in about two-thirds of the tokens."""
    return json.dumps(obj, separators=(",", ":"), default=str, ensure_ascii=False)


ACTIVITY_FIELDS = ("id", "start_date_local", "type", "name", "moving_time", "elapsed_time",
                   "distance", "icu_training_load", "icu_intensity", "icu_average_watts",
                   "icu_weighted_avg_watts", "average_heartrate", "max_heartrate",
                   "decoupling", "total_elevation_gain", "average_speed", "pace",
                   "gap", "icu_zone_times", "icu_hr_zone_times", "trainer", "race",
                   "feel", "icu_rpe", "calories", "carbs_used", "description")
EVENT_FIELDS = ("id", "start_date_local", "category", "type", "name", "moving_time",
                "icu_training_load", "load_target", "description")
WELLNESS_FIELDS = ("id", "ctl", "atl", "rampRate", "hrv", "restingHR", "sleepSecs",
                   "sleepScore", "weight", "bodyFat", "vo2max", "steps", "wellness_finalized")
FITNESS_FIELDS = ("id", "date", "ctl", "atl", "rampRate", "load", "form")


def profile_summary(p: dict) -> dict:
    """The athlete profile reduced to who they are and their thresholds and zones."""
    p = p or {}
    out = {k: p[k] for k in ("name", "sex", "icu_weight", "weight", "height", "timezone")
           if p.get(k) not in (None, "")}
    sports = []
    for s in p.get("sportSettings") or []:
        sports.append({k: s[k] for k in ("types", "ftp", "indoor_ftp", "lthr", "max_hr",
                                         "threshold_pace", "pace_units", "hr_zones",
                                         "power_zones", "pace_zones")
                       if s.get(k) not in (None, "", [])})
    if sports:
        out["sports"] = sports
    return out
