#!/usr/bin/env python3
"""plan_tools.py — deterministic planning maths for the conversational coach.

The Telegram chat path is a headless `claude` CLI that, left to itself, does
TSS / CTL / weekly-total arithmetic by hand and gets it wrong (see
docs/planning-chat-bypass-diagnosis.md, 14 Jun 2026 conversation). This CLI
exposes the SAME tested primitives the Sunday plan generator uses, so the model
never has to compute a training number itself.

All maths is delegated to ironman-analysis/primitives — this file only marshals
inputs and prints JSON. Never reimplement load maths here.

Usage:
  # Per-session and whole-week TSS (build-a-week — the generative case)
  python3 plan_tools.py tss --sessions '[{"sport":"Run","minutes":50,"name":"Z2 run"},
                                          {"sport":"Ride","minutes":240,"name":"Z2 ride"}]'
  python3 plan_tools.py tss --sport Swim --minutes 60 --name "CSS swim"

  # Deterministic weekly roll-up from the live calendar (completed + planned)
  python3 plan_tools.py week-tss --athlete jamie [--week-start 2026-06-15]

  # Day-by-day CTL/ATL/TSB projection (seeds default to latest wellness)
  python3 plan_tools.py project --athlete jamie \
        --daily '[{"date":"2026-06-16","tss":113},{"date":"2026-06-17","tss":58}]'

  # What SHOULD this week's TSS be, given the phase CTL target
  python3 plan_tools.py required-tss --athlete jamie

Every subcommand prints a single JSON object to stdout. On error it prints
{"error": "..."} and exits non-zero.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent          # ClaudeCoach/
sys.path.insert(0, str(BASE / "ironman-analysis"))
sys.path.insert(0, str(BASE / "lib"))

from primitives.planned_tss import (                            # noqa: E402
    planned_session_tss, tss_from_segments, render_workout,
)
from primitives.load import (                                   # noqa: E402
    compute_required_tss,
    project_pmc_daily,
    derive_phase_ctl_targets,
)
from primitives.validate_plan import validate_week              # noqa: E402
from primitives.blueprint import current_phase                  # noqa: E402
from primitives.nutrition import fuel_target, recent_avg_g_hr   # noqa: E402

ATHLETES_CONFIG = BASE / "config" / "athletes.json"


# ── helpers ──────────────────────────────────────────────────────────────────
def _load_cfg(slug: str) -> dict:
    athletes = json.loads(ATHLETES_CONFIG.read_text())
    if slug not in athletes:
        raise SystemExit(_err(f"unknown athlete '{slug}'"))
    return athletes[slug]


def _client(cfg: dict):
    from icu_api import IcuClient
    return IcuClient(cfg["icu_athlete_id"], cfg["icu_api_key"])


def _err(msg: str) -> str:
    return json.dumps({"error": msg})


def _monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


def _event_tss(ev: dict) -> dict:
    """Resolve a planned event to TSS via the tested primitive."""
    return planned_session_tss({
        "type": ev.get("type") or ev.get("sport") or "",
        "name": ev.get("name") or "",
        "moving_time": (ev.get("minutes") * 60) if ev.get("minutes") else ev.get("moving_time"),
        "load_target": ev.get("load_target"),
        "icu_training_load": ev.get("icu_training_load"),
    })


# ── subcommand: tss ────────────────────────────────────────────────────────────
def cmd_tss(args) -> dict:
    # Calculable path: time-at-intensity segments → TSS = Σ(hours × IF²) × 100.
    # This is the preferred way to SET a session's load_target.
    if args.segments:
        try:
            segs = json.loads(args.segments)
        except json.JSONDecodeError as e:
            raise SystemExit(_err(f"--segments is not valid JSON: {e}"))
        if not args.sport:
            raise SystemExit(_err("--segments requires --sport (swim/run/bike)"))
        return tss_from_segments(args.sport, segs)
    if args.sessions:
        try:
            sessions = json.loads(args.sessions)
        except json.JSONDecodeError as e:
            raise SystemExit(_err(f"--sessions is not valid JSON: {e}"))
        if not isinstance(sessions, list):
            raise SystemExit(_err("--sessions must be a JSON list"))
    elif args.sport and args.minutes:
        sessions = [{"sport": args.sport, "minutes": args.minutes, "name": args.name or ""}]
    else:
        raise SystemExit(_err("provide --sessions <json> OR --sport and --minutes"))

    out, total = [], 0
    for s in sessions:
        r = _event_tss(s)
        total += r["tss"]
        out.append({"name": r["name"] or s.get("sport", ""),
                    "sport": s.get("sport", ""),
                    "duration_min": r["duration_min"],
                    "tss": r["tss"],
                    "source": r["source"]})
    return {"sessions": out, "total_tss": total}


# ── subcommand: week-tss ───────────────────────────────────────────────────────
def cmd_week_tss(args) -> dict:
    cfg = _load_cfg(args.athlete)
    week_start = (date.fromisoformat(args.week_start) if args.week_start
                  else _monday(date.today()))
    week_end = week_start + timedelta(days=6)
    today = date.today()

    client = _client(cfg)
    history = client.get_training_history(days=max(7, (today - week_start).days + 1))
    events = client.get_events(week_start.isoformat(), week_end.isoformat())

    # Index completed activities by (date, sport) — actuals always win.
    completed = {}
    for a in history or []:
        d = (a.get("start_date_local") or "")[:10]
        if not (week_start.isoformat() <= d <= week_end.isoformat()):
            continue
        sport = a.get("type") or "?"
        tss = int(round(float(a.get("icu_training_load") or 0)))
        completed.setdefault(d, []).append(
            {"sport": sport, "tss": tss,
             "min": round((a.get("moving_time") or 0) / 60),
             "status": "completed", "name": a.get("name") or ""})

    days = {}
    for off in range(7):
        d = (week_start + timedelta(days=off)).isoformat()
        days[d] = list(completed.get(d, []))

    # Planned events only for days/sports with no completed actual.
    for ev in events or []:
        d = (ev.get("start_date_local") or "")[:10]
        if d not in days:
            continue
        if ev.get("category") and ev.get("category") != "WORKOUT":
            continue
        sport = ev.get("type") or ""
        if any(c["sport"] == sport for c in completed.get(d, [])):
            continue  # actual already counted
        r = _event_tss(ev)
        days[d].append({"sport": sport, "tss": r["tss"], "min": r["duration_min"],
                        "status": "planned", "name": r["name"], "tss_source": r["source"]})

    by_day = []
    total = completed_total = planned_total = 0
    for d in sorted(days):
        day_tss = sum(s["tss"] for s in days[d])
        total += day_tss
        completed_total += sum(s["tss"] for s in days[d] if s["status"] == "completed")
        planned_total += sum(s["tss"] for s in days[d] if s["status"] == "planned")
        by_day.append({"date": d, "weekday": (week_start + timedelta(
            days=(date.fromisoformat(d) - week_start).days)).strftime("%a"),
            "tss": day_tss, "sessions": days[d]})

    return {"athlete": args.athlete, "week_start": week_start.isoformat(),
            "total_tss": total, "completed_tss": completed_total,
            "planned_tss": planned_total, "by_day": by_day}


def week_rollup_summary(history: list, events: list, week_start: date, today: date) -> dict:
    """Pure: this week's TSS = completed-to-date (actuals) + planned-remaining.
    Planned events on a day/sport already completed are ignored. Reuses data the
    caller already fetched, so it adds no network round-trip."""
    week_end = week_start + timedelta(days=6)
    in_week = lambda d: week_start.isoformat() <= d <= week_end.isoformat()

    completed = 0
    done_keys = set()
    for a in history or []:
        d = (a.get("start_date_local") or "")[:10]
        if in_week(d):
            completed += int(round(float(a.get("icu_training_load") or 0)))
            done_keys.add((d, a.get("type") or "?"))

    planned = 0
    for ev in events or []:
        d = (ev.get("start_date_local") or "")[:10]
        if not in_week(d) or d < today.isoformat():
            continue  # only future/remaining planned days
        if ev.get("category") and ev.get("category") != "WORKOUT":
            continue
        if (d, ev.get("type") or "") in done_keys:
            continue
        planned += _event_tss(ev)["tss"]

    return {"week_start": week_start.isoformat(),
            "completed_to_date_tss": completed,
            "planned_remaining_tss": planned,
            "projected_week_tss": completed + planned}


# ── subcommand: project ────────────────────────────────────────────────────────
def cmd_project(args) -> dict:
    cfg = _load_cfg(args.athlete)
    try:
        daily = json.loads(args.daily)
    except json.JSONDecodeError as e:
        raise SystemExit(_err(f"--daily is not valid JSON: {e}"))
    if not isinstance(daily, list) or not daily:
        raise SystemExit(_err("--daily must be a non-empty JSON list of {date,tss}"))

    seed_ctl, seed_atl = args.seed_ctl, args.seed_atl
    if seed_ctl is None or seed_atl is None:
        w = _client(cfg).get_wellness(days=3)
        if not w:
            raise SystemExit(_err("no wellness data to seed CTL/ATL; pass --seed-ctl/--seed-atl"))
        last = w[-1]
        seed_ctl = round(float(last.get("ctl") or 0), 1) if seed_ctl is None else seed_ctl
        seed_atl = round(float(last.get("atl") or 0), 1) if seed_atl is None else seed_atl

    tss_seq = [float(d.get("tss") or 0) for d in daily]
    proj = project_pmc_daily(seed_ctl, seed_atl, tss_seq)
    rows = [{"date": daily[i].get("date"), "tss": int(round(tss_seq[i])),
             **proj[i]} for i in range(len(daily))]
    return {"athlete": args.athlete, "seed_ctl": seed_ctl, "seed_atl": seed_atl,
            "days": rows, "end": rows[-1]}


# ── subcommand: required-tss ───────────────────────────────────────────────────
_PHASES = ("base", "build", "specific", "peak")


def required_tss(cfg: dict, ctl_today: float, today: date | None = None) -> dict:
    """Pure: weekly TSS needed to hit the current phase's CTL target on time,
    plus the ramp-capped safe ceiling. Returns {"error": ...} if the athlete has
    no defensible CTL basis (no fabricated targets — mirrors generate-plan.py).
    Shared by the CLI and prefetch_context so chat and tool agree."""
    today = today or date.today()
    ctl_targets = cfg.get("ctl_targets") or {}
    phase_ctl = ctl_targets.get("phase_ctl") or {}
    if not phase_ctl and not ctl_targets.get("race_min"):
        return {"error": "no CTL target configured — plan from availability, not a TSS target"}
    if not cfg.get("plan_start"):
        return {"error": "no plan_start configured"}

    plan_start = date.fromisoformat(cfg["plan_start"])
    ptss = cfg.get("phase_tss") or {}
    ends = {"base": ptss.get("base_end_week", 6), "build": ptss.get("build_end_week", 10),
            "specific": ptss.get("specific_end_week", 14), "peak": ptss.get("peak_end_week", 17)}
    week_now = max(1, (today - plan_start).days // 7 + 1)
    phase = next((p for p in _PHASES if week_now <= ends[p]), "taper")
    if phase == "taper":
        return {"phase": "taper/race", "ctl_today": ctl_today, "training_week": week_now,
                "note": "past the last build phase — taper logic applies, no build target"}

    # Derive phase CTL milestones from race_min when not explicitly configured
    # (mirrors generate-plan.py so athletes with a race_min but no phase_ctl — e.g.
    # Kathryn — still get a defensible target rather than None).
    ctl_source = "configured"
    if not phase_ctl and ctl_targets.get("race_min") and ctl_today:
        phase_ctl = derive_phase_ctl_targets(
            ctl_today, int(ctl_targets["race_min"]), plan_start,
            ends["base"], ends["build"], ends["specific"], ends["peak"],
            float(cfg.get("max_ctl_ramp_per_week", 5.0)),
            float(cfg.get("taper_overshoot", 1.15)), today=today)
        ctl_source = "derived_from_race_min"

    target_ctl = phase_ctl.get(phase)
    if target_ctl is None:
        return {"error": f"phase '{phase}' has no CTL target (no phase_ctl and no race_min basis)"}
    weeks_remaining = max(1, ends[phase] - week_now + 1)
    required = compute_required_tss(ctl_today, target_ctl, weeks_remaining)

    out = {"phase": phase, "training_week": week_now, "ctl_today": ctl_today,
           "phase_target_ctl": target_ctl, "ctl_target_source": ctl_source,
           "weeks_to_phase_end": weeks_remaining, "required_weekly_tss": required}
    max_ramp = cfg.get("max_ctl_ramp_per_week")
    if max_ramp:
        safe = compute_required_tss(ctl_today, ctl_today + float(max_ramp), 1)
        out.update({"max_ctl_ramp_per_week": float(max_ramp),
                    "ramp_capped_weekly_tss": safe,
                    "recommended_weekly_tss": min(required, safe),
                    "note": (f"To reach {phase} CTL {target_ctl} by week {ends[phase]} needs "
                             f"~{required} TSS/wk; the +{max_ramp}/wk ramp cap allows at most "
                             f"~{safe} TSS/wk. Prescribe ~{min(required, safe)} this week.")})
    else:
        out["recommended_weekly_tss"] = required
    return out


def cmd_required_tss(args) -> dict:
    cfg = _load_cfg(args.athlete)
    ctl_today = args.ctl_today
    if ctl_today is None:
        w = _client(cfg).get_wellness(days=3)
        if not w:
            raise SystemExit(_err("no wellness data for current CTL; pass --ctl-today"))
        ctl_today = round(float(w[-1].get("ctl") or 0), 1)
    return {"athlete": args.athlete, **required_tss(cfg, ctl_today)}


# ── subcommand: validate ───────────────────────────────────────────────────────
def _load_blueprint(slug: str) -> dict:
    p = BASE / "athletes" / slug / "reference" / "training-blueprint.json"
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:
            return {}
    return {}


def cmd_validate(args) -> dict:
    """Hard-check a proposed week against the athlete's rules — the same backstop
    the Sunday generator uses (day_rules, CTL ramp, strength cap, intensity
    distribution). Mirrors generate-plan.py's validate call: NO weekly_tss_cap
    (the required-tss target is a floor, not a ceiling)."""
    cfg = _load_cfg(args.athlete)
    try:
        week = json.loads(args.week)
    except json.JSONDecodeError as e:
        raise SystemExit(_err(f"--week is not valid JSON: {e}"))
    if not isinstance(week, list) or not week:
        raise SystemExit(_err("--week must be a non-empty JSON list of {date,sport,tss}"))

    # Map the lightweight input to the event shape validate_week expects.
    events = [{"start_date_local": s.get("date"), "type": s.get("sport") or s.get("type"),
               "category": "WORKOUT",
               "load_target": s.get("tss") if s.get("tss") is not None else s.get("load_target")}
              for s in week]
    week_start = _monday(min(date.fromisoformat(e["start_date_local"][:10]) for e in events))

    ctl_today = args.ctl_today
    if ctl_today is None:
        w = _client(cfg).get_wellness(days=3)
        ctl_today = round(float(w[-1].get("ctl") or 0), 1) if w else None

    day_rules = cfg.get("day_rules")
    phase = current_phase(_load_blueprint(args.athlete), week_start) or {}
    rep = validate_week(
        events, week_start,
        day_rules=day_rules, ctl_today=ctl_today,
        ramp_cap=float(cfg.get("max_ctl_ramp_per_week", 5.0)),
        strength_max=(day_rules or {}).get("strength_max"),
        distribution=phase.get("distribution"),
    )
    viol = [{"code": v.code, "severity": v.severity, "message": str(v)} for v in rep.violations]
    return {"athlete": args.athlete, "week_start": week_start.isoformat(),
            "total_tss": round(rep.total_tss),
            "ok": not any(v["severity"] == "hard" for v in viol),
            "hard": [v for v in viol if v["severity"] == "hard"],
            "soft": [v for v in viol if v["severity"] != "hard"]}


def cmd_fuel_target(args) -> dict:
    """Deterministic fuelling prescription (g/hr) for >90-min sessions — gap-closing
    ramp toward the athlete's race target (aggressive <60, careful >=60). Replaces
    the old avg+10 guess."""
    cfg = _load_cfg(args.athlete)
    race_target = int(cfg.get("nutrition_target_g_hr") or 90)
    sl_path = BASE / "athletes" / args.athlete / "session-log.json"
    session_log = json.loads(sl_path.read_text()) if sl_path.exists() else []
    avg = recent_avg_g_hr(session_log)
    target = fuel_target(avg, race_target)
    zone = "no logs yet" if avg is None else ("aggressive ramp (<60)" if avg < 60 else "careful ramp (>=60)")
    return {"athlete": args.athlete,
            "recent_avg_g_hr": round(avg, 1) if avg is not None else None,
            "race_target_g_hr": race_target, "prescribed_g_hr": target,
            "note": f"{zone}: prescribe {target} g/hr now (race target {race_target})"}


def cmd_render_workout(args) -> dict:
    """Render time-at-intensity segments into an ICU STRUCTURED workout string.
    Push the returned `description` via icu_fetch push_workout so it syncs to the
    athlete's Garmin as a follow-along workout (ICU computes its own load)."""
    try:
        segs = json.loads(args.segments)
    except json.JSONDecodeError as e:
        raise SystemExit(_err(f"--segments is not valid JSON: {e}"))
    if not args.sport:
        raise SystemExit(_err("--sport required (swim/run/bike)"))
    r = render_workout(args.sport, segs)
    r["how_to_push"] = ("pass description=<the description field above> to "
                        "icu_fetch.py push_workout (put coaching prose in description_raw)")
    return r


def main():
    p = argparse.ArgumentParser(description="Deterministic planning maths for ClaudeCoach")
    sub = p.add_subparsers(dest="cmd", required=True)

    pt = sub.add_parser("tss", help="TSS for a session/list, or calculable from time-at-intensity segments")
    pt.add_argument("--sessions"); pt.add_argument("--sport")
    pt.add_argument("--minutes", type=int); pt.add_argument("--name")
    pt.add_argument("--segments", help='JSON list of {"minutes":N,"zone":"css"} or {"minutes":N,"if":F}')

    pw = sub.add_parser("week-tss", help="deterministic weekly roll-up from the calendar")
    pw.add_argument("--athlete", required=True); pw.add_argument("--week-start")

    pp = sub.add_parser("project", help="day-by-day CTL/ATL/TSB projection")
    pp.add_argument("--athlete", required=True); pp.add_argument("--daily", required=True)
    pp.add_argument("--seed-ctl", type=float); pp.add_argument("--seed-atl", type=float)

    pr = sub.add_parser("required-tss", help="weekly TSS needed for the phase CTL target")
    pr.add_argument("--athlete", required=True); pr.add_argument("--ctl-today", type=float)

    pv = sub.add_parser("validate", help="hard-check a proposed week against the athlete's rules")
    pv.add_argument("--athlete", required=True); pv.add_argument("--week", required=True)
    pv.add_argument("--ctl-today", type=float)

    prw = sub.add_parser("render-workout", help="segments -> ICU structured workout text (syncs to Garmin)")
    prw.add_argument("--sport", required=True)
    prw.add_argument("--segments", required=True)

    pf = sub.add_parser("fuel-target", help="deterministic g/hr fuelling prescription for >90-min sessions")
    pf.add_argument("--athlete", required=True)

    args = p.parse_args()
    handler = {"tss": cmd_tss, "week-tss": cmd_week_tss, "project": cmd_project,
               "required-tss": cmd_required_tss, "validate": cmd_validate,
               "render-workout": cmd_render_workout, "fuel-target": cmd_fuel_target}[args.cmd]
    try:
        result = handler(args)
    except SystemExit:
        raise
    except Exception as e:
        print(_err(f"{type(e).__name__}: {e}"))
        sys.exit(1)
    print(json.dumps(result, indent=1))


if __name__ == "__main__":
    main()
