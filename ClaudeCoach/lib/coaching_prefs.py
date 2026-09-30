"""Coaching extras an athlete can switch off (Jamie, 30 Sep 2026): heat training and
fuelling coaching (carbs, salt, fluids). Asked at sign-up; switched later in Peak ->
Settings -> Coaching extras (api/server.py /api/prefs).

    athletes/<slug>/profile.json
      heat_protocol: false       heat training off: lib/heat.py's existing switch
      fuelling_coaching: false   no carbs / sodium / fluid targets in sessions or
                                 messages, and no carbs question after a session

Absent means ON, so every athlete from before these switches is unchanged.

Where fuelling is switched off:
  lib/plan_builder.py         no fuel note on long rides / runs
  lib/plan_audit.py           no FUELLING check (it would flag the missing note)
  scripts/activity-watcher.py no carbs buttons or carbs question; write-up told
  lib/engine.py               chat: prompt_note() in the system prompt
  morning / evening check-in, night-before brief, weekly summary: prompt_note()
  scripts/generate-race-plan.py  no fuelling table
"""
from __future__ import annotations

import json
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent            # ClaudeCoach/

FUELLING_OFF_NOTE = (
    "FUELLING COACHING IS OFF for this athlete (their choice; they can switch it back on in "
    "Peak -> Settings -> Coaching extras). Leave out every carbs, sodium and fluid target, "
    "every fuelling line and every fuelling question. If they ask about fuelling directly, "
    "answer briefly.")


def _profile_path(slug: str) -> Path:
    return BASE / "athletes" / slug / "profile.json"


def _profile(slug: str) -> dict:
    try:
        return json.loads(_profile_path(slug).read_text())
    except (OSError, ValueError):
        return {}


def fuelling_on(slug: str | None) -> bool:
    return not slug or _profile(slug).get("fuelling_coaching") is not False


def heat_on(slug: str | None) -> bool:
    """heat_silent (lib/heat.py: modelled but never surfaced) reads as off to the athlete."""
    p = _profile(slug) if slug else {}
    return not slug or (p.get("heat_protocol") is not False and not p.get("heat_silent"))


def prefs(slug: str) -> dict:
    return {"heat": heat_on(slug), "fuelling": fuelling_on(slug)}


def set_prefs(slug: str, heat: bool | None = None, fuelling: bool | None = None) -> dict:
    """Write the switches into profile.json (atomic). Only the ones given change."""
    path = _profile_path(slug)
    p = _profile(slug)
    if heat is not None:
        p["heat_protocol"] = bool(heat)
        if heat:
            p.pop("heat_silent", None)
    if fuelling is not None:
        p["fuelling_coaching"] = bool(fuelling)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(p, indent=2))
    tmp.replace(path)
    return prefs(slug)


def prompt_note(slug: str | None) -> str:
    """Appended to a prompt that may otherwise add fuelling: "" when fuelling is on."""
    return "" if fuelling_on(slug) else "\n\n" + FUELLING_OFF_NOTE
