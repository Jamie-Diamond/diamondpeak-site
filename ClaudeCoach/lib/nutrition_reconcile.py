#!/usr/bin/env python3
"""nutrition_reconcile.py - one truth for in-session fuel, across two bots.

THE PROBLEM THIS SOLVES
In-session fuel can be logged in either bot, and before this module neither knew about
the other:
  coach bot     -> athletes/<slug>/session-log.json, `nutrition_g_carb` per session
  nutrition bot -> athletes/<slug>/nutrition/YYYY-MM.json, entries with in_session=True

Nothing was double-counted, because the two stores never met. Both failure modes were
worse than a double-count, because both are silent:

  1. Fuel logged in the COACH is invisible to the nutrition bot, so the day is short by
     the whole ride. A 200 g carb ride is 800 kcal missing, which is enough to fire the
     under-fuelling guard falsely. A safety flag that cries wolf is worse than none.
  2. Fuel logged in the NUTRITION bot never reaches `nutrition_g_carb`, which has three
     live consumers: ironman-analysis/primitives/nutrition.py (recent_avg_g_hr, and
     therefore the gap-closing ramp toward the 90 g/hr race target), weekly-trend.py,
     and public_sanitise.py (the Peak fuelling tile). Starve those and
     `recent_avg_g_hr` returns None, so the ramp goes blind. Only 13 of 122 logged
     sessions carry a carb figure, so that signal was already thin.

OWNERSHIP RULE (Jamie's call, 10 Aug 2026)
The nutrition bot owns a day's in-session fuel IF it has any in_session entry for that
day; otherwise session-log owns it. Rationale: itemised gels and bottles beat a recalled
total, but old days and coach-only logging must keep working untouched. When the
nutrition bot owns it, the sum is WRITTEN BACK to session-log so the coach's ramp keeps
being fed either way.

Exactly one side is ever counted. The other is shown, never added.

THE ONE APPROXIMATION, STATED
session-log stores carbs but not calories, so folding coach-logged fuel into a day's
energy total means synthesising kcal as carbs x 4. In-ride fuel is very nearly pure
carbohydrate (gels, chews, drink mix), so the error is small, but it IS an
approximation and `energy_is_derived` says so on every merged total.
"""

import json
import os
import sys
import tempfile
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

KCAL_PER_G_CARB = 4
OWNER_NUTRITION = "nutrition_bot"
OWNER_SESSION_LOG = "session_log"


def _as_iso(d) -> str:
    if isinstance(d, (date, datetime)):
        return (d.date() if isinstance(d, datetime) else d).isoformat()
    return str(d)[:10]


def _session_log_path(athlete_dir) -> Path:
    return Path(athlete_dir) / "session-log.json"


def _load_session_log(athlete_dir):
    p = _session_log_path(athlete_dir)
    if not p.exists():
        return None, []
    try:
        data = json.loads(p.read_text() or "[]")
    except json.JSONDecodeError:
        return None, []
    if isinstance(data, list):
        return data, data
    for key in ("sessions", "entries"):
        if isinstance(data.get(key), list):
            return data, data[key]
    return data, []


def session_fuel_for_day(athlete_dir, day) -> dict:
    """What the COACH has for this day's in-session fuel.

    Returns {'carb_g', 'sodium_mg', 'hydration_ml', 'sessions': [...]}, with None for
    any field no session carried. None is not zero: an unlogged ride and a ride with
    no fuel are different facts, and treating the first as the second would make the
    fuelling ramp read a gap as a genuine zero."""
    iso = _as_iso(day)
    _, rows = _load_session_log(athlete_dir)
    carb = sodium = hydration = None
    sessions = []
    for r in rows:
        if not isinstance(r, dict) or (r.get("date") or "")[:10] != iso:
            continue
        c, s, h = (r.get("nutrition_g_carb"), r.get("nutrition_mg_sodium"),
                   r.get("hydration_ml"))
        if c is None and s is None and h is None:
            continue
        sessions.append({"activity_id": r.get("activity_id"), "name": r.get("name"),
                         "sport": r.get("sport"), "duration_min": r.get("duration_min"),
                         "carb_g": c, "sodium_mg": s, "hydration_ml": h})
        if c is not None:
            carb = (carb or 0) + float(c)
        if s is not None:
            sodium = (sodium or 0) + float(s)
        if h is not None:
            hydration = (hydration or 0) + float(h)
    return {"carb_g": carb, "sodium_mg": sodium, "hydration_ml": hydration,
            "sessions": sessions}


def bot_in_session_totals(store, day) -> dict:
    """What the NUTRITION bot has for this day's in-session fuel."""
    entries = [e for e in (store.get_day(day).get("entries") or [])
               if e.get("in_session")]
    if not entries:
        return {"carb_g": None, "sodium_mg": None, "kcal": None, "count": 0}
    return {"carb_g": round(sum(float(e.get("carb_g") or 0) for e in entries), 1),
            "sodium_mg": round(sum(float(e.get("dietary_sodium_mg") or 0)
                                   for e in entries)),
            "kcal": round(sum(float(e.get("kcal") or 0) for e in entries), 1),
            "count": len(entries)}


def reconcile(store, athlete_dir, day) -> dict:
    """Decide who owns this day's in-session fuel and report both sides.

    Never sums the two. `owner` says which figure is authoritative and `other` carries
    the one that is not, so a disagreement is visible instead of averaged away."""
    coach = session_fuel_for_day(athlete_dir, day)
    bot = bot_in_session_totals(store, day)
    owner = OWNER_NUTRITION if bot["count"] else OWNER_SESSION_LOG
    if owner == OWNER_NUTRITION:
        authoritative = {"carb_g": bot["carb_g"], "sodium_mg": bot["sodium_mg"],
                         "kcal": bot["kcal"], "hydration_ml": coach["hydration_ml"]}
        other = {"carb_g": coach["carb_g"], "sodium_mg": coach["sodium_mg"]}
    else:
        carb = coach["carb_g"]
        authoritative = {
            "carb_g": carb, "sodium_mg": coach["sodium_mg"],
            # Synthesised: session-log carries no calories. See the module docstring.
            "kcal": round(carb * KCAL_PER_G_CARB, 1) if carb is not None else None,
            "hydration_ml": coach["hydration_ml"]}
        other = {"carb_g": bot["carb_g"], "sodium_mg": bot["sodium_mg"]}
    disagrees = (other.get("carb_g") is not None
                 and authoritative.get("carb_g") is not None
                 and abs(other["carb_g"] - authoritative["carb_g"]) > 1)
    return {"owner": owner, "fuel": authoritative, "other_side": other,
            "disagrees": disagrees,
            "energy_is_derived": owner == OWNER_SESSION_LOG,
            "sessions": coach["sessions"]}


def merged_totals(store, athlete_dir, day) -> dict:
    """The day's totals with in-session fuel counted EXACTLY once.

    When the nutrition bot owns the day, its own entries already include the fuel and
    nothing is added. When session-log owns it, the coach's figures are folded in so the
    day is not short by the whole ride - which is what made the under-fuelling guard
    fire falsely."""
    totals = dict(store.day_totals(day))
    rec = reconcile(store, athlete_dir, day)
    totals["fuel_owner"] = rec["owner"]
    totals["fuel_disagrees"] = rec["disagrees"]
    totals["in_session_from_coach"] = False
    if rec["owner"] == OWNER_SESSION_LOG and rec["fuel"]["carb_g"]:
        fuel = rec["fuel"]
        totals["kcal"] = round((totals.get("kcal") or 0) + (fuel["kcal"] or 0), 1)
        totals["carb_g"] = round((totals.get("carb_g") or 0) + (fuel["carb_g"] or 0), 1)
        if fuel.get("sodium_mg"):
            totals["dietary_sodium_mg"] = round((totals.get("dietary_sodium_mg") or 0)
                                                + fuel["sodium_mg"])
        totals["in_session_kcal"] = round((totals.get("in_session_kcal") or 0)
                                          + (fuel["kcal"] or 0), 1)
        totals["in_session_carb_g"] = round((totals.get("in_session_carb_g") or 0)
                                            + (fuel["carb_g"] or 0), 1)
        totals["in_session_from_coach"] = True
        totals["energy_is_derived"] = True
    totals["hydration_ml"] = rec["fuel"].get("hydration_ml")
    return totals


def write_back(athlete_dir, day, carb_g=None, sodium_mg=None, log=print,
               allow_clear: bool = False) -> dict:
    """Push the nutrition bot's in-session totals into session-log's own fields, so the
    coach's fuelling ramp keeps working when fuel is logged in the nutrition bot.

    Writes to the LONGEST session of the day, because g/hr is computed per session and
    spreading one day's fuel across a ride and a swim would understate the ride's rate.
    Only ever touches the three nutrition fields; never any device-sourced value.

    Returns {'written': bool, 'reason': ...}. A missing session is not an error: the
    activity may not have synced yet, and the caller can retry on the next log."""
    iso = _as_iso(day)
    if carb_g is None and sodium_mg is None:
        # A CORRECTION HAS TO BE ABLE TO UNDO THE WRITE.
        #
        # On 11 Aug the model flagged a bowl of breakfast oats as in-session, so 37 g went
        # into session-log as in-run carbohydrate and fed the g/hr ramp. Moving the entry
        # back out left the 37 g behind: with nothing in-session there was "nothing to
        # write", so the erroneous figure was permanent and the fix was silently partial.
        #
        # Clearing is safe precisely because the bot stamps nutrition_source when it
        # writes. Only rows it owns are touched; a figure the coach or the device put
        # there is never erased.
        if not allow_clear:
            return {"written": False, "reason": "nothing to write"}
        return _clear_bot_fuel(athlete_dir, iso, log=log)
    doc, rows = _load_session_log(athlete_dir)
    if doc is None:
        return {"written": False, "reason": "no session log"}
    todays = [r for r in rows if isinstance(r, dict) and (r.get("date") or "")[:10] == iso]
    if not todays:
        return {"written": False, "reason": "no session logged for that day yet"}
    target = max(todays, key=lambda r: float(r.get("duration_min") or 0))
    before = {"nutrition_g_carb": target.get("nutrition_g_carb"),
              "nutrition_mg_sodium": target.get("nutrition_mg_sodium")}
    if carb_g is not None:
        target["nutrition_g_carb"] = int(round(carb_g))
    if sodium_mg is not None:
        target["nutrition_mg_sodium"] = int(round(sodium_mg))
    target["nutrition_source"] = "nutrition_bot"
    _atomic_write(_session_log_path(athlete_dir), doc)
    log(f"session-log fuel written back for {iso}: {before} -> "
        f"{{'nutrition_g_carb': {target.get('nutrition_g_carb')}, "
        f"'nutrition_mg_sodium': {target.get('nutrition_mg_sodium')}}}")
    return {"written": True, "activity_id": target.get("activity_id"),
            "session": target.get("name"), "before": before}


def _clear_bot_fuel(athlete_dir, iso: str, log=print) -> dict:
    """Remove fuel figures THIS bot wrote for a day, and nothing else."""
    doc, rows = _load_session_log(athlete_dir)
    if doc is None:
        return {"written": False, "reason": "no session log"}
    cleared = []
    for r in rows:
        if not isinstance(r, dict) or (r.get("date") or "")[:10] != iso:
            continue
        if r.get("nutrition_source") != "nutrition_bot":
            continue          # somebody else's figure; leave it alone
        before = {"nutrition_g_carb": r.get("nutrition_g_carb"),
                  "nutrition_mg_sodium": r.get("nutrition_mg_sodium")}
        if before["nutrition_g_carb"] is None and before["nutrition_mg_sodium"] is None:
            continue
        r["nutrition_g_carb"] = None
        r["nutrition_mg_sodium"] = None
        r.pop("nutrition_source", None)
        cleared.append({"session": r.get("name"), "before": before})
    if not cleared:
        return {"written": False, "reason": "nothing of ours to clear"}
    _atomic_write(_session_log_path(athlete_dir), doc)
    log(f"session-log fuel CLEARED for {iso}: {cleared}")
    return {"written": True, "cleared": cleared}


def _atomic_write(path: Path, payload) -> None:
    """session-log.json is read by cron jobs and the coach bot while this writes, and it
    is 122 sessions of irreplaceable history, so a torn file is not survivable."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".slog-", suffix=".json")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(payload, fh, indent=2)
            fh.write("\n")
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
