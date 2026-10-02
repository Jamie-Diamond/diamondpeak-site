"""The athlete brief: athletes/<slug>/system_prompt.txt, cut to WHO the athlete is (Jamie,
2 Oct 2026: one job per layer).

How the coach works - tools, files, logging, charts, the planning-maths rules - is the
coach manual (lib/coach_manual.md), ONE file for every athlete, put in front of the brief
by engine.system_prompt_with_level. It used to be copied into every brief at sign-up from
onboarding/templates/system_prompt.txt, so each copy went stale on its own: Jamie's had
the planning-maths and structured-workout rules that Fred's and James's never got, and
the copies contradicted each other on git commits and on replying "Logged.".

A brief is now:
    About <name>: race or goal, background, injuries, max hours
    athletes/<slug>/reference/brief-notes.md, if present (athlete-specific notes, kept on
        the server - never in this public repo)
    the AUTO-SYNC block (scripts/refresh-athlete-prompts.py rewrites it daily)

    render(name, profile, cfg, notes="", autosync="") -> str
    rebuild(slug)        rewrite a brief from profile.json + athletes.json + notes, keeping
                         its AUTO-SYNC block; the old file is backed up first
"""
from __future__ import annotations

import json
import shutil
from datetime import datetime
from pathlib import Path
from string import Template

BASE = Path(__file__).resolve().parent.parent            # ClaudeCoach/
MANUAL = Path(__file__).resolve().parent / "coach_manual.md"
BEGIN = "### AUTO-SYNC:"
END = "### AUTO-SYNC: end ###"
NOTES = "brief-notes.md"


def manual(first_name: str, slug: str) -> str:
    """The coach manual for this athlete ($name, $slug filled in), or '' if it is missing."""
    try:
        return Template(MANUAL.read_text(encoding="utf-8")).safe_substitute(
            name=first_name, slug=slug).strip()
    except OSError:
        return ""


def _injuries(profile: dict) -> str:
    out = []
    for i in profile.get("injuries") or []:
        if not isinstance(i, dict):
            continue
        d = str(i.get("description") or "").strip()
        if not d or d.lower() in ("no", "none", "n/a"):
            continue
        p = str(i.get("protocol") or "").strip()
        loc = str(i.get("location") or "").strip()
        out.append(f"{d}{f' ({loc})' if loc and loc.lower() not in d.lower() else ''}"
                   f"{f' - {p}' if p else ''}")
    return "; ".join(out) or "none"


def _race_line(cfg: dict, profile: dict) -> str:
    g = cfg.get("goal") if isinstance(cfg.get("goal"), dict) else None
    if g and not cfg.get("race_date"):
        try:
            import goals
            label = goals.GOALS[g["type"]]["label"]
        except Exception:
            label = g.get("type", "goal")
        return f"No race booked. Goal: {label}."
    name = cfg.get("race_name") or profile.get("race_name")
    when = cfg.get("race_date") or profile.get("race_date")
    if not (name or when):
        return "No race booked."
    goal = profile.get("a_goal")
    return (f"Race: {name or 'race'}{f', {when}' if when else ''}."
            + (f" Goal: {goal}." if goal and goal not in ("—", "-") else ""))


def render(name: str, profile: dict, cfg: dict, notes: str = "", autosync: str = "") -> str:
    lines = [f"About {name}:", f"- {_race_line(cfg or {}, profile or {})}"]
    exp = str((profile or {}).get("experience") or "").strip()
    if exp:
        lines.append(f"- Background: {exp}")
    lines.append(f"- Injuries / constraints: {_injuries(profile or {})}")
    hrs = (profile or {}).get("max_hours_per_week")
    if hrs:
        lines.append(f"- Max training: {hrs} h/week")
    out = "\n".join(lines)
    if notes.strip():
        out += "\n\n" + notes.strip()
    if autosync.strip():
        out += "\n\n" + autosync.strip()
    return out + "\n"


def _autosync(text: str) -> str:
    if BEGIN in text and END in text:
        return text[text.index(BEGIN):text.index(END) + len(END)]
    return ""


def rebuild(slug: str, base: Path | None = None) -> dict:
    base = base or BASE
    d = base / "athletes" / slug
    sp = d / "system_prompt.txt"
    old = sp.read_text(encoding="utf-8") if sp.exists() else ""
    profile = json.loads((d / "profile.json").read_text()) if (d / "profile.json").exists() else {}
    cfg = (json.loads((base / "config" / "athletes.json").read_text()).get(slug) or {})
    notes_p = d / "reference" / NOTES
    notes = notes_p.read_text(encoding="utf-8") if notes_p.exists() else ""
    name = str(profile.get("name") or cfg.get("name") or slug)
    new = render(name, profile, cfg, notes, _autosync(old))
    if old:
        shutil.copy2(sp, sp.with_name(f"system_prompt.txt.bak-brief-{datetime.now():%Y%m%d%H%M%S}"))
    sp.write_text(new, encoding="utf-8")
    return {"athlete": slug, "before_bytes": len(old), "after_bytes": len(new)}


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--athlete", required=True)
    a = ap.parse_args()
    print(json.dumps(rebuild(a.athlete)))
