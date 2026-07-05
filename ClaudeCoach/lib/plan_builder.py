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

from primitives.planned_tss import render_workout, planned_session_tss  # noqa: E402
from primitives.nutrition import fuel_target, recent_avg_g_hr  # noqa: E402
from primitives.validate_plan import validate_week             # noqa: E402
from primitives.blueprint import current_phase, tss_ceiling    # noqa: E402

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


def _ctl_today(cfg) -> float | None:
    """Live CTL for the ramp hard check. None (-> check recorded as skipped) on
    ICU failure rather than raising — a validation must never kill a build."""
    try:
        from icu_api import IcuClient
        w = IcuClient(cfg["icu_athlete_id"], cfg["icu_api_key"]).get_wellness(days=3)
        for e in reversed(w or []):
            if e.get("ctl") is not None:
                return float(e["ctl"])
    except Exception:
        pass
    return None


def _weekly_tss_cap(slug, phase) -> float | None:
    """Blueprint hours ceiling (max_hours_per_week x 100 x IF^2) for the TSS hard
    check — same maths the blueprint document displays (primitives.blueprint)."""
    try:
        prof = json.loads((BASE / "athletes" / slug / "profile.json").read_text())
        max_h = prof.get("max_hours_per_week")
        if max_h and phase.get("name"):
            return tss_ceiling(float(max_h), str(phase["name"]))
    except Exception:
        pass
    return None


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
            # No structured segments (Strength, or a session Stage 1 left unstructured):
            # derive TSS deterministically from sport + duration. NEVER s.get("load") — the
            # Stage-1 LLM's number is exactly the guessed load the two-stage design removes.
            dur  = int(s.get("minutes") or s.get("duration_min") or 0)
            load = planned_session_tss({"type": sport, "name": s.get("name", ""),
                                        "moving_time": dur * 60})["tss"]
            desc = ""
        # fuel note for long rides
        if sport in _LONG_FUEL_SPORTS and dur >= 90:
            notes = (notes + f"\nFuel {fuel} g CHO/hr (progress toward "
                     f"{int(cfg.get('nutrition_target_g_hr') or 90)} race target); "
                     "eat from 15 min, every 25 min.").strip()
        built.append({"date": date_s, "sport": sport, "name": s.get("name", ""),
                      "duration_min": dur, "load_target": load,
                      "description": desc, "description_raw": notes})
        events.append({"start_date_local": f"{date_s}T00:00:00", "type": sport,
                       "category": "WORKOUT", "load_target": load,
                       "moving_time": dur * 60,
                       "name": s.get("name", ""), "description_raw": notes})

    # Validate the whole week against the athlete's hard rules.
    ws = min((date.fromisoformat(e["start_date_local"][:10]) for e in events),
             default=date.today())
    ws -= timedelta(days=ws.weekday())
    dr = cfg.get("day_rules")
    phase = current_phase(_blueprint(slug), ws) or {}
    # ARM the hard checks (audit P0-4): without ctl_today the ramp check silently
    # no-ops, and without weekly_tss_cap the load check does — the validator's
    # "a breach cannot reach the athlete" guarantee was void. Both inputs are
    # sourced here; a missing one lands in rep.skipped and is surfaced loudly.
    import plan_tools as _pt
    try:
        _caps = _pt.run_caps(_pt._client(cfg), ws)
    except Exception:
        _caps = {"weekly_min_cap": None, "long_run_min_cap": None}
    # Under-training floor for the week BEING PLANNED (today=ws, not run date):
    # min(phase requirement, 7 x CTL maintenance); 0 on deload/taper. A week
    # below it hard-fails — a plan that detrains must never push silently.
    _floor = None
    _ctl = _ctl_today(cfg)
    if _ctl:
        try:
            _lw = _pt.last_week_actual_tss(_pt._client(cfg), today=ws)
            _floor = _pt.required_tss(cfg, _ctl, today=ws,
                                      last_week_tss=_lw).get("weekly_tss_floor")
        except Exception:
            _floor = None
    rep = validate_week(events, ws, day_rules=dr,
                        weekly_tss_cap=_weekly_tss_cap(slug, phase),
                        weekly_tss_floor=_floor,
                        run_week_min_cap=_caps.get("weekly_min_cap"),
                        run_long_min_cap=_caps.get("long_run_min_cap"),
                        ctl_today=_ctl,
                        ramp_cap=float(cfg.get("max_ctl_ramp_per_week", 5.0)),
                        strength_max=(dr or {}).get("strength_max"),
                        distribution=phase.get("distribution"))
    hard = [{"code": v.code, "msg": str(v)} for v in rep.violations if v.severity == "hard"]
    soft = [{"code": v.code, "msg": str(v)} for v in rep.violations if v.severity != "hard"]
    if rep.skipped:
        for s in rep.skipped:
            print(f"[plan_builder:{slug}] WARN {s}", file=sys.stderr)
        try:
            from ops_log import alert
            alert("plan_builder", "; ".join(rep.skipped), athlete=slug)
        except Exception:
            pass
    return {"athlete": slug, "fuel_g_hr": fuel, "week_start": ws.isoformat(),
            "total_tss": round(rep.total_tss), "ok": not hard,
            "hard": hard, "soft": soft, "skipped_checks": rep.skipped,
            "sessions": built}


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
