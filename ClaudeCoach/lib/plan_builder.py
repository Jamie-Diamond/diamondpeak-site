#!/usr/bin/env python3
"""Two-stage plan builder — Stage 2 (deterministic).

The 15 Jun replan proved the single-LLM generator ignores instructions to render
structured workouts, use the fuel-target tool, and cap the long ride — it wrote
prose with hardcoded numbers. Lesson (same as the chat-path fix): take the
mechanical parts out of the LLM's hands.

Stage 1 (in generate-plan.py): the LLM proposes the week as structured DATA only:
  {"sessions": [
     {"date":"YYYY-MM-DD","sport":"Swim|Run|Ride|Brick|Strength",
      "name":"...","notes":"coaching prose",
      "segments":[{"minutes":N,"zone":"css|easy|sweetspot|..."}, ...]}  # omit for Strength
  ]}
It does NOT write loads, fuelling numbers, or structured-step text.

Stage 2 (HERE): for each session deterministically
  - render_workout(segments)         -> ICU structured steps (sync to Garmin)
  - tss from the render               -> load_target (ICU recomputes its own too)
  - fuel_target for >90-min rides     -> correct g/hr appended to the notes
then validate_week() the whole proposal and only push if it passes (hard rules).

Usage:
  python3 plan_builder.py --athlete kathryn --proposal proposal.json            # dry-run (default)
  python3 plan_builder.py --athlete kathryn --proposal proposal.json --push     # push to ICU
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, timedelta
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE / "ironman-analysis"))
sys.path.insert(0, str(BASE / "lib"))

from primitives.planned_tss import render_workout              # noqa: E402
from primitives.nutrition import fuel_target, recent_avg_g_hr  # noqa: E402
from primitives.validate_plan import validate_week             # noqa: E402
from primitives.blueprint import current_phase                 # noqa: E402

ATHLETES = BASE / "config" / "athletes.json"
_LONG_FUEL_SPORTS = {"Ride", "GravelRide", "VirtualRide", "Brick"}


def _cfg(slug):
    return json.loads(ATHLETES.read_text())[slug]


def _blueprint(slug):
    p = BASE / "athletes" / slug / "reference" / "training-blueprint.json"
    try:
        return json.loads(p.read_text()) if p.exists() else {}
    except Exception:
        return {}


def _fuel_for(slug, cfg):
    sl = BASE / "athletes" / slug / "session-log.json"
    log = json.loads(sl.read_text()) if sl.exists() else []
    return fuel_target(recent_avg_g_hr(log), int(cfg.get("nutrition_target_g_hr") or 90))


def build_sessions(slug: str, proposal: dict) -> dict:
    """Turn a Stage-1 proposal into push-ready, validated sessions. Pure except
    for reading athlete config/session-log; never pushes."""
    cfg = _cfg(slug)
    fuel = _fuel_for(slug, cfg)
    built, events = [], []
    for s in proposal.get("sessions", []):
        sport = s.get("sport", "")
        date_s = s.get("date")
        notes = (s.get("notes") or "").strip()
        segs = s.get("segments") or []
        if segs and sport not in ("Strength", "WeightTraining"):
            r = render_workout(sport, segs)
            desc, load, dur = r["description"], r["tss"], r["duration_min"]
        else:
            desc, load, dur = "", int(s.get("load") or 0), int(s.get("minutes") or 0)
        # fuel note for long rides
        if sport in _LONG_FUEL_SPORTS and dur >= 90:
            notes = (notes + f"\nFuel {fuel} g CHO/hr (progress toward "
                     f"{int(cfg.get('nutrition_target_g_hr') or 90)} race target); "
                     "eat from 15 min, every 25 min.").strip()
        built.append({"date": date_s, "sport": sport, "name": s.get("name", ""),
                      "duration_min": dur, "load_target": load,
                      "description": desc, "description_raw": notes})
        events.append({"start_date_local": f"{date_s}T00:00:00", "type": sport,
                       "category": "WORKOUT", "load_target": load})

    # Validate the whole week against the athlete's hard rules.
    ws = min((date.fromisoformat(e["start_date_local"][:10]) for e in events),
             default=date.today())
    ws -= timedelta(days=ws.weekday())
    dr = cfg.get("day_rules")
    phase = current_phase(_blueprint(slug), ws) or {}
    rep = validate_week(events, ws, day_rules=dr,
                        ramp_cap=float(cfg.get("max_ctl_ramp_per_week", 5.0)),
                        strength_max=(dr or {}).get("strength_max"),
                        distribution=phase.get("distribution"))
    hard = [{"code": v.code, "msg": str(v)} for v in rep.violations if v.severity == "hard"]
    soft = [{"code": v.code, "msg": str(v)} for v in rep.violations if v.severity != "hard"]
    return {"athlete": slug, "fuel_g_hr": fuel, "week_start": ws.isoformat(),
            "total_tss": round(rep.total_tss), "ok": not hard,
            "hard": hard, "soft": soft, "sessions": built}


def push(slug: str, built: dict, replace: bool = True):
    """Push the built week. replace=True first DELETES existing planned WORKOUT events
    in the target week so we don't duplicate the old plan. Returns {deleted, pushed}."""
    from icu_api import IcuClient
    from datetime import date as _d, timedelta as _td
    cfg = _cfg(slug)
    c = IcuClient(cfg["icu_athlete_id"], cfg["icu_api_key"])
    ws = _d.fromisoformat(built["week_start"])
    # Map proposal sports to valid intervals.icu event types (Bike/Brick/Strength are NOT
    # valid ICU types — Brick pushes as a Ride; the run leg is in description_raw).
    icu_type = {"Bike": "Ride", "Brick": "Ride", "Strength": "WeightTraining",
                "Weights": "WeightTraining"}
    # SAFE ORDERING: capture the OLD events, PUSH the new ones FIRST, and only delete the
    # old ones once every new push succeeded. If a push fails the old plan is left intact
    # (worst case: transient duplicates), so a failure can NEVER empty the week.
    old_ids = []
    if replace:
        old_ids = [e["id"] for e in c.get_events(ws.isoformat(), (ws + _td(days=6)).isoformat())
                   if e.get("category") == "WORKOUT" and e.get("id")]
    pushed = []
    for s in built["sessions"]:
        payload = {"sport": icu_type.get(s["sport"], s["sport"]),
                   "event_date": s["date"], "name": s["name"],
                   "description": s["description"], "description_raw": s["description_raw"],
                   "planned_training_load": s["load_target"]}
        r = c.push_workout(**payload)        # raises before any delete if a payload is bad
        pushed.append(r.get("id"))
    deleted = []
    for eid in old_ids:                       # only reached if ALL pushes succeeded
        try:
            c.delete_workout(eid); deleted.append(eid)
        except Exception:
            pass
    return {"deleted": deleted, "pushed": pushed}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--athlete", required=True)
    ap.add_argument("--proposal", required=True, help="path to Stage-1 proposal JSON")
    ap.add_argument("--push", action="store_true", help="actually push (default: dry-run)")
    args = ap.parse_args()
    proposal = json.loads(Path(args.proposal).read_text())
    built = build_sessions(args.athlete, proposal)
    if args.push:
        if not built["ok"]:
            print(json.dumps({"error": "validation failed — not pushing", **built}, indent=1))
            sys.exit(1)
        built["push_result"] = push(args.athlete, built)
    print(json.dumps(built, indent=1, ensure_ascii=False))


if __name__ == "__main__":
    main()
