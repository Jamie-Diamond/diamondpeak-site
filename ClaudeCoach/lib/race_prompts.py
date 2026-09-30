#!/usr/bin/env python3
"""Race-driven suggestions: nutrition tracking and heat training (Jamie, 30 Sep 2026).

"For any event over 90 mins total it suggests the Nutrition tracking, and reminds during
peak phase if they said no. Also heat: if the goal event might be hot, suggest; again in
peak if they say no." Hot = the venue's usual daytime high in race month is 21°C or more.
"Nutrition" here is SESSION FUELLING - carbs, salt and water logged after long sessions -
not the Food tab's full meal tracking, which has its own switch (nutrition_tracker).

One lifecycle for both, stored in the athlete's profile.json:

    not asked  ->  asked on the Sunday plan message, every week until answered
    yes        ->  the feature is switched on, through the same switches as Peak ->
                   Settings -> Coaching extras (lib/coaching_prefs): fuelling_coaching true /
                   heat_protocol true + race_conditions "hot", which arms the race heat block
                   and the question never returns
    no         ->  silent until the PEAK phase, asked once more there, then never again

The Sunday message shows the question (stage1 _race_prompt_lines); the bot records the
answer with `plan_tools.py race-prompt`. Nothing here sends a message by itself.
"""
from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request
from datetime import date
from pathlib import Path

import race_fitness as rf

BASE = Path(__file__).resolve().parent.parent            # ClaudeCoach/
LONG_RACE_MIN = 90          # nutrition tracking is suggested above this
HOT_C = 21.0                # "hot is 21+": usual race-month daytime high at the venue
TOPICS = ("nutrition", "heat")

# Typical whole-race hours per level-table event, for a race with no goal time. Sized
# near the middle of each event's levels; only compared against 90 minutes.
_TYPICAL_MIN = {"5k": 25, "10k": 50, "half_marathon": 110, "marathon": 240,
                "sprint": 85, "olympic": 165, "70_3": 330, "ironman": 720,
                "sportive": 360, "gravel": 360}


def race_minutes(cfg: dict, profile: dict | None = None):
    """Expected race duration in minutes: the goal time, else the level's goal band,
    else a typical time for the event. None when the event is unknown."""
    ev = rf.event_def(cfg, (cfg or {}).get("race_distance") or (cfg or {}).get("race_name"))
    if not ev:
        return None
    goal_s = rf.parse_goal_s(rf.athlete_goal(cfg, profile), ev)
    if goal_s:
        return goal_s / 60.0
    if ev.get("est_minutes"):
        return float(ev["est_minutes"])
    lv = rf.athlete_level(cfg, profile, ev)
    if lv and lv.get("goal_max_s"):
        return lv["goal_max_s"] / 60.0
    key = rf.levels_key((cfg or {}).get("race_distance") or (cfg or {}).get("race_name"))
    return float(_TYPICAL_MIN.get(key or "", 0)) or None


_EVENT_WORDS = re.compile(
    r"\b(marathon|half|ironman|im|70\.3|triathlon|tri|olympic|sprint|sportive|gran ?fondo|"
    r"gravel|race|parkrun|10k|5k|km|run|ride|challenge|\d+)\b", re.I)


def _place(cfg: dict) -> str:
    """Where the race is: `race_location` if set, else the race name minus event words
    ('Brighton Marathon' -> 'Brighton')."""
    if (cfg or {}).get("race_location"):
        return str(cfg["race_location"])
    return re.sub(r"\s+", " ", _EVENT_WORDS.sub(" ", str((cfg or {}).get("race_name") or ""))).strip(" -/,")


def venue_high_c(cfg: dict, timeout: float = 8.0):
    """Usual daytime high (°C) at the venue in race month: the mean of daily maxima for
    that month over the last five years (Open-Meteo geocoding + archive). None when the
    place or the weather cannot be found - the question then asks the athlete instead."""
    place, race = _place(cfg), (cfg or {}).get("race_date")
    if not place or not race:
        return None
    try:
        d = date.fromisoformat(str(race)[:10])
        q = urllib.parse.urlencode({"name": place, "count": 1})
        with urllib.request.urlopen(f"https://geocoding-api.open-meteo.com/v1/search?{q}",
                                    timeout=timeout) as r:
            hit = (json.loads(r.read()).get("results") or [None])[0]
        if not hit:
            return None
        highs = []
        for y in range(d.year - 5, d.year):
            start = date(y, d.month, 1)
            end = date(y + (d.month == 12), d.month % 12 + 1, 1)
            q = urllib.parse.urlencode({
                "latitude": hit["latitude"], "longitude": hit["longitude"],
                "start_date": start.isoformat(),
                "end_date": date.fromordinal(end.toordinal() - 1).isoformat(),
                "daily": "temperature_2m_max", "timezone": "UTC"})
            with urllib.request.urlopen(f"https://archive-api.open-meteo.com/v1/archive?{q}",
                                        timeout=timeout) as r:
                highs += [v for v in (json.loads(r.read()).get("daily") or {})
                          .get("temperature_2m_max") or [] if v is not None]
        return round(sum(highs) / len(highs), 1) if highs else None
    except Exception:
        return None


def _state(profile: dict, topic: str) -> dict:
    return ((profile or {}).get("race_prompts") or {}).get(topic) or {}


def _already_on(profile: dict, topic: str) -> bool:
    p = profile or {}
    if topic == "nutrition":                       # coaching_prefs: absent means ON
        return p.get("fuelling_coaching") is not False
    return (p.get("race_conditions") == "hot" and p.get("heat_protocol") is not False
            and not p.get("heat_silent"))


def due(topic: str, cfg: dict, profile: dict, phase: str, high_c=None) -> dict | None:
    """The question to put on this week's plan message, or None.

    {"topic", "stage": "first" | "peak_reminder", "reason"}. `high_c` is the venue's
    race-month high for heat (None = unknown, so the question asks the athlete)."""
    if topic not in TOPICS or _already_on(profile, topic):
        return None
    mins = race_minutes(cfg, profile)
    if topic == "nutrition":
        if not mins or mins <= LONG_RACE_MIN:
            return None
        reason = f"your race is about {int(round(mins / 60 * 10)) / 10:g} h"
    else:
        if high_c is not None and high_c < HOT_C:
            return None
        reason = (f"the usual race-month high there is {high_c:g}°C" if high_c is not None
                  else "it might be warm on race day")
    st = _state(profile, topic)
    if not st.get("answer"):
        return {"topic": topic, "stage": "first", "reason": reason, "high_c": high_c}
    if st["answer"] == "no" and (phase or "").lower() == "peak" and not st.get("peak_reminded"):
        return {"topic": topic, "stage": "peak_reminder", "reason": reason, "high_c": high_c}
    return None


def message_line(q: dict) -> str:
    """The Sunday-message line for one due question."""
    again = q["stage"] == "peak_reminder"
    if q["topic"] == "nutrition":
        return ("🍌 _" + ("Peak phase now: worth a second look. " if again else "")
                + f"As {q['reason']}, race fuelling will matter. Want fuelling coaching back on "
                "(carbs, salt and water targets, and a quick log after long sessions)? Reply "
                "*fuelling yes* or *fuelling no*._")
    unknown = q.get("high_c") is None
    return ("🔥 _" + ("Peak phase now: worth a second look. " if again else "")
            + (f"As {q['reason']}, " if not unknown else "Will your race be 21°C or warmer? If so, ")
            + "heat training (sauna / hot baths / warm sessions) in the last 4 weeks would "
            "help. Want it in your plan? Reply *heat yes* or *heat no*._")


def record(profile: dict, topic: str, answer: str, today=None) -> dict:
    """Apply the athlete's answer to `profile` (returned, not written)."""
    if topic not in TOPICS or answer not in ("yes", "no"):
        raise ValueError("topic must be nutrition|heat, answer yes|no")
    p = dict(profile or {})
    rp = dict(p.get("race_prompts") or {})
    st = dict(rp.get(topic) or {})
    st.update({"answer": answer, "answered": (today or date.today()).isoformat()})
    rp[topic] = st
    p["race_prompts"] = rp
    # The SAME switches Peak -> Settings -> Coaching extras writes (lib/coaching_prefs).
    if topic == "nutrition":
        p["fuelling_coaching"] = answer == "yes"
    elif answer == "yes":
        p["heat_protocol"] = True
        p["race_conditions"] = "hot"
        p.pop("heat_silent", None)
    else:
        p["heat_protocol"] = False
    return p


def mark_peak_reminded(profile: dict, topic: str, today=None) -> dict:
    p = dict(profile or {})
    rp = dict(p.get("race_prompts") or {})
    st = dict(rp.get(topic) or {})
    st["peak_reminded"] = (today or date.today()).isoformat()
    rp[topic] = st
    p["race_prompts"] = rp
    return p


def venue_high_cached(slug: str, cfg: dict):
    """venue_high_c, cached per race in athletes/<slug>/race-climate.json so the Sunday
    build does not re-fetch five years of weather every week."""
    f = BASE / "athletes" / slug / "race-climate.json"
    key = f"{(cfg or {}).get('race_name')}|{(cfg or {}).get('race_date')}|{_place(cfg)}"
    try:
        cache = json.loads(f.read_text())
    except Exception:
        cache = {}
    if key in cache:
        return cache[key]
    v = venue_high_c(cfg)
    if v is not None:
        cache[key] = v
        try:
            f.write_text(json.dumps(cache, indent=1))
        except Exception:
            pass
    return v


def due_for_week(slug: str, cfg: dict, profile: dict, phase: str, week_type: str) -> list:
    """The questions for this week's plan message. Training weeks of a race block only:
    never in the off-season, post-race recovery, a taper or race week."""
    if (week_type or "") in ("offseason", "post_race", "taper", "race") or not cfg.get("race_date"):
        return []
    out = []
    q = due("nutrition", cfg, profile, phase)
    if q:
        out.append(q)
    if not _already_on(profile, "heat") and _state(profile, "heat").get("answer") != "yes":
        high = venue_high_cached(slug, cfg)
        q = due("heat", cfg, profile, phase, high_c=high)
        if q:
            out.append(q)
    return out
