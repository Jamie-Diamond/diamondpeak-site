"""Ride indoors (Jamie, 1 Oct 2026: "give riders the option to ride indoors for rides
below 2hrs ... suggest the type of Zwift workout for them").

Any planned ride under MAX_MIN minutes, today or later, can be switched to indoors from
Peak (api/server.py /api/indoor). The switch changes the Intervals.icu event's type to
VirtualRide; the session itself (steps, load, notes) is unchanged. Intervals.icu uploads
planned workouts to Zwift for the next 7 days when the athlete has connected Zwift with
"upload workouts" on (athlete fields zwift_user_id / zwift_upload_workouts, checked on
the VM 1 Oct 2026), so the session then appears in Zwift under Workouts -> Custom ->
Intervals.icu, ready for ERG. VirtualRide also picks up an indoor FTP set in
Intervals.icu's VirtualRide sport settings.

The choice is kept per date in athletes/<slug>/indoor-rides.json, and
IcuClient.push_workout re-applies it (sticky_type), so a re-plan of that day keeps the
ride indoors instead of quietly sending it back outside.
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent            # ClaudeCoach/
MAX_MIN = 120
FILE = "indoor-rides.json"
OUTDOOR, INDOOR = "Ride", "VirtualRide"

# Zwift-style workout type from the session's own name / notes, most specific first.
_KINDS = (("VO2 max", r"vo2|max aerobic|\bmap\b"),
          ("threshold", r"threshold|over.?under|ftp|\bz4\b"),
          ("sweet-spot", r"sweet.?spot|\bss\b|\bz3\b"),
          ("tempo", r"tempo"),
          ("recovery", r"recovery|easy spin|\bz1\b"),
          ("endurance", r"endurance|aerobic|steady|\bz2\b|base"))


def _path(slug: str, base: Path | None = None) -> Path:
    return (base or BASE) / "athletes" / slug / FILE


def load(slug: str, base: Path | None = None) -> dict:
    try:
        d = json.loads(_path(slug, base).read_text())
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(slug: str, d: dict, base: Path | None = None, today: date | None = None) -> None:
    cut = (today or date.today()).isoformat()
    d = {k: v for k, v in d.items() if k >= cut}            # past days need no memory
    p = _path(slug, base)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(d, indent=1))


def is_indoor(slug: str, day: str, base: Path | None = None) -> bool:
    return str(day)[:10] in load(slug, base)


def minutes(ev: dict) -> int | None:
    v = ev.get("moving_time") or ev.get("duration") or (ev.get("workout_doc") or {}).get("duration")
    try:
        return round(int(v) / 60) if v else None
    except (TypeError, ValueError):
        return None


def eligible(ev: dict, today: date | None = None) -> bool:
    """A planned ride, today or later, shorter than MAX_MIN (longer rides stay outside)."""
    today = today or date.today()
    if (ev.get("category") or "WORKOUT") != "WORKOUT":
        return False
    if (ev.get("type") or "") not in (OUTDOOR, INDOOR):
        return False
    if str(ev.get("start_date_local") or "")[:10] < today.isoformat():
        return False
    m = minutes(ev)
    return m is not None and m < MAX_MIN


def kind(ev: dict) -> str:
    text = f"{ev.get('name') or ''} {ev.get('description') or ''}".lower()
    for label, pat in _KINDS:
        if re.search(pat, text):
            return label
    return "endurance"


def suggestion(ev: dict) -> str:
    m = minutes(ev) or 60
    k = kind(ev)
    return f"Or ride one of Zwift's own {k} workouts of about {int(round(m / 5) * 5)} min."


def zwift_status(profile: dict | None) -> str:
    """"on" (connected, uploading workouts), "no_upload" (connected, upload off) or "off"."""
    p = profile or {}
    if not p.get("zwift_user_id"):
        return "off"
    return "on" if p.get("zwift_upload_workouts") else "no_upload"


def where_text(status: str) -> str:
    if status == "on":
        return ("It's in Zwift now, under Workouts → Custom → Intervals.icu (and on "
                "the home screen on the day). Ride it in ERG mode.")
    if status == "no_upload":
        return ("Zwift is linked but not receiving workouts: in Intervals.icu Settings, tick "
                "Upload planned workouts in the Zwift box and it appears in Zwift.")
    return ("To ride it in Zwift automatically: in Intervals.icu Settings, tap Connect on the "
            "Zwift box and allow it. Then it appears under Workouts → Custom.")


def sticky_type(athlete_id: str, sport: str, event_date: str, base: Path | None = None) -> str:
    """The type to write for a planned ride: VirtualRide on a day the athlete chose to ride
    indoors. Used by IcuClient.push_workout so a re-plan keeps the choice."""
    if sport != OUTDOOR or not athlete_id:
        return sport
    try:
        cfg = json.loads(((base or BASE) / "config" / "athletes.json").read_text())
    except (OSError, ValueError):
        return sport
    slug = next((s for s, a in cfg.items() if isinstance(a, dict)
                 and str(a.get("icu_athlete_id") or "") == str(athlete_id)), None)
    return INDOOR if slug and is_indoor(slug, event_date, base) else sport


def switch(slug: str, client, event_id: str, indoor: bool, base: Path | None = None,
           today: date | None = None) -> dict:
    """Move one planned ride indoors (or back outside). Returns what Peak shows."""
    today = today or date.today()
    events = client.get_events(today.isoformat(), (today.replace(year=today.year + 1)).isoformat(),
                               category="WORKOUT") or []
    ev = next((e for e in events if str(e.get("id")) == str(event_id)), None)
    if not ev:
        raise LookupError("that session isn't on the calendar any more")
    if not eligible(ev, today):
        raise ValueError(f"only planned rides under {MAX_MIN // 60} hours can move indoors")
    day = str(ev.get("start_date_local"))[:10]
    client.edit_workout(ev["id"], type=INDOOR if indoor else OUTDOOR)
    d = load(slug, base)
    if indoor:
        d[day] = {"event_id": str(ev["id"]), "name": ev.get("name"),
                  "set": datetime.now().isoformat(timespec="seconds")}
    else:
        d.pop(day, None)
    _save(slug, d, base, today)
    if not indoor:
        return {"indoor": False, "date": day, "message": "Back outdoors."}
    try:
        status = zwift_status(client.get_athlete_profile())
    except Exception:
        status = "off"
    return {"indoor": True, "date": day, "zwift": status, "message": where_text(status),
            "suggestion": suggestion(ev)}
