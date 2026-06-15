#!/usr/bin/env python3
"""Plan audit (Layer 4) — the self-check / trust mechanism.

Asserts the planning invariants against an athlete's LIVE intervals.icu calendar,
so the SYSTEM catches drift instead of the athlete finding it in a session. Run
standalone (cron) or right after a generation/replan.

Invariants (per the planning architecture doc):
  STRUCTURE   every Swim/Run/Ride/Brick session has structured workout steps
              (workout_doc.steps non-empty) — i.e. it will sync to Garmin as a
              follow-along workout, not a prose note.
  FUELLING    every >90-min ride/brick states the deterministic fuel-target g/hr.
  LONG_RIDE   no ride exceeds the event-anchored long-ride ceiling.
  WEEKLY_LOAD weekly planned TSS is within tolerance of the phase target.
  RULES       day_rules / CTL ramp / strength cap / intensity distribution
              (delegated to validate_week — the same backstop the generator uses).

Usage:
  python3 plan_audit.py --athlete jamie            # current + next week
  python3 plan_audit.py --all                      # every active athlete
Exit code 0 = clean, 1 = at least one hard invariant failed (for cron alerting).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date, timedelta
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE / "ironman-analysis"))
sys.path.insert(0, str(BASE / "lib"))

from primitives.validate_plan import validate_week            # noqa: E402
from primitives.blueprint import current_phase                # noqa: E402
from primitives.nutrition import fuel_target, recent_avg_g_hr  # noqa: E402
import plan_tools as pt                                        # noqa: E402

ATHLETES = BASE / "config" / "athletes.json"
_STRUCTURED_SPORTS = {"Swim", "Run", "Ride", "GravelRide", "VirtualRide", "Brick"}
_FUEL_SPORTS = {"Ride", "GravelRide", "VirtualRide", "Brick"}
_LOAD_TOLERANCE = 0.15


def _client(cfg):
    from icu_api import IcuClient
    return IcuClient(cfg["icu_athlete_id"], cfg["icu_api_key"])


def _dur_min(ev) -> int:
    """Best-effort planned duration in minutes: moving_time, else parse the name."""
    mt = ev.get("moving_time")
    if mt:
        return int(mt / 60)
    name = (ev.get("name") or "") + " " + (ev.get("description") or "")
    m = re.search(r"(\d+)\s*h(?:r|our)?s?\s*(?:(\d+)\s*m)?", name, re.I)
    if m:
        return int(m.group(1)) * 60 + int(m.group(2) or 0)
    m = re.search(r"(\d+)\s*min", name, re.I)
    return int(m.group(1)) if m else 0


def audit_athlete(slug: str, cfg: dict, weeks: int = 2) -> dict:
    client = _client(cfg)
    today = date.today()
    win_start = today - timedelta(days=today.weekday())
    win_end = win_start + timedelta(days=7 * weeks - 1)
    events = [e for e in client.get_events(win_start.isoformat(), win_end.isoformat())
              if e.get("category") == "WORKOUT"]
    wellness = client.get_wellness(days=3)
    ctl = round(float(wellness[-1].get("ctl") or 0), 1) if wellness else None
    sl_path = BASE / "athletes" / slug / "session-log.json"
    fuel = fuel_target(recent_avg_g_hr(json.loads(sl_path.read_text()) if sl_path.exists() else []),
                       int(cfg.get("nutrition_target_g_hr") or 90))
    bike_min = (cfg.get("race_target_splits") or {}).get("bike_min")
    lr_ceiling = min(int(round(bike_min * 1.15 / 15) * 15), 300) if bike_min else None

    fails = {"STRUCTURE": [], "FUELLING": [], "LONG_RIDE": [], "WEEKLY_LOAD": [], "RULES": []}

    for e in events:
        sport = e.get("type") or ""
        nm = (e.get("name") or "")[:48]
        steps = len((e.get("workout_doc") or {}).get("steps") or [])
        desc = e.get("description") or ""
        dur = _dur_min(e)
        if sport in _STRUCTURED_SPORTS and steps == 0:
            fails["STRUCTURE"].append(f"{nm} ({sport}) — no structured steps")
        if sport in _FUEL_SPORTS and dur >= 90:
            g = re.findall(r"(\d+)\s*g\s*(?:CHO\s*)?/?\s*hr", desc, re.I)
            if not g:
                fails["FUELLING"].append(f"{nm} — no fuelling stated (expect {fuel} g/hr)")
            elif not any(int(x) == fuel for x in g):
                fails["FUELLING"].append(f"{nm} — states {g} g/hr, expected {fuel}")
        if sport in _FUEL_SPORTS and lr_ceiling and dur > lr_ceiling:
            fails["LONG_RIDE"].append(f"{nm} — {dur}min > {lr_ceiling}min event ceiling")

    # Per-week: load vs target + validate_week rules.
    for wk in range(weeks):
        ws = win_start + timedelta(days=7 * wk)
        wk_evs = [e for e in events if ws.isoformat() <= (e.get("start_date_local") or "")[:10]
                  <= (ws + timedelta(days=6)).isoformat()]
        total = sum(int(e.get("load_target") or 0) for e in wk_evs)
        if ctl:
            req = pt.required_tss(cfg, ctl, today=ws)
            tgt = req.get("recommended_weekly_tss")
            if tgt and abs(total - tgt) > tgt * _LOAD_TOLERANCE:
                fails["WEEKLY_LOAD"].append(
                    f"week {ws}: {total} TSS vs target ~{tgt} (>{int(_LOAD_TOLERANCE*100)}% off)")
        dr = cfg.get("day_rules")
        phase = current_phase(pt._load_blueprint(slug), ws) or {}
        rep = validate_week(wk_evs, ws, day_rules=dr, ctl_today=ctl,
                            ramp_cap=float(cfg.get("max_ctl_ramp_per_week", 5.0)),
                            strength_max=(dr or {}).get("strength_max"),
                            distribution=phase.get("distribution"))
        for v in rep.violations:
            if v.severity == "hard" or v.code == "intensity_distribution":
                fails["RULES"].append(f"week {ws}: {v}")

    hard = any(fails[k] for k in ("STRUCTURE", "LONG_RIDE", "RULES"))  # fuelling/load = warn
    return {"athlete": slug, "window": f"{win_start}..{win_end}", "fuel_target": fuel,
            "long_ride_ceiling_min": lr_ceiling, "ok": not any(fails.values()),
            "hard_fail": hard, "fails": {k: v for k, v in fails.items() if v}}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--athlete")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--weeks", type=int, default=2)
    args = ap.parse_args()
    athletes = json.loads(ATHLETES.read_text())
    slugs = ([s for s, c in athletes.items() if c.get("active", True)] if args.all
             else [args.athlete])
    reports, any_hard = [], False
    for slug in slugs:
        try:
            r = audit_athlete(slug, athletes[slug], args.weeks)
        except Exception as e:
            r = {"athlete": slug, "error": f"{type(e).__name__}: {e}", "hard_fail": True}
        any_hard = any_hard or r.get("hard_fail")
        reports.append(r)
    print(json.dumps(reports, indent=1, ensure_ascii=False))
    sys.exit(1 if any_hard else 0)


if __name__ == "__main__":
    main()
