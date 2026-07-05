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
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent          # ClaudeCoach/
FUELLING_CLI = BASE.parent / "js" / "fuelling-cli.js"  # shared JS engine bridge
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
from primitives.blueprint import current_phase, tss_ceiling     # noqa: E402
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

# Deload (blueprint: "3 weeks load, 1 week recovery at 60-65%"; audit P0-1 —
# the forward plan must unload, not just the reactive watchdog).
_DELOAD_EVERY_N = 4         # every Nth training week (cfg: deload_every_n_weeks)
_DELOAD_FACTOR = 0.62       # 60-65% of the normal prescription (cfg: deload_factor)
_MISS_TRIGGER = 0.70        # last week < 70% executed → this week is recovery

# Taper (blueprint: "70% -> 55% -> 40% of peak, maintain intensity"; audit P0-2 —
# volume steps down by weeks-to-race, intensity is HELD by the taper TID row).
# Pre-taper weekly load is approximated by the steady-state 7 x CTL (the load
# that holds current fitness); CTL decays slightly through the taper so the
# absolute numbers drift down a touch more — the safe direction.
_TAPER_FACTORS = {3: 0.70, 2: 0.55, 1: 0.40}


def required_tss(cfg: dict, ctl_today: float, today: date | None = None,
                 last_week_tss: float | None = None) -> dict:
    """Pure: weekly TSS needed to hit the current phase's CTL target on time,
    plus the ramp-capped safe ceiling — with deload and taper branches. Returns
    {"error": ...} if the athlete has no defensible CTL basis (no fabricated
    targets — mirrors generate-plan.py). Shared by the CLI, the weekly brief,
    the plan audit and prefetch_context so every path agrees.

    last_week_tss (optional): last completed week's ACTUAL total load; when the
    athlete executed under 70% of prescription, this week becomes a recovery
    week (blueprint: "missed >30% -> next week is recovery")."""
    today = today or date.today()
    ctl_targets = cfg.get("ctl_targets") or {}
    phase_ctl = ctl_targets.get("phase_ctl") or {}
    if not phase_ctl and not ctl_targets.get("race_min"):
        return {"error": "no CTL target configured — plan from availability, not a TSS target"}
    if not cfg.get("plan_start"):
        return {"error": "no plan_start configured"}

    plan_start = date.fromisoformat(cfg["plan_start"])
    ptss = cfg.get("phase_tss") or {}
    build_end = ptss.get("build_end_week", 10)
    ends = {"base": ptss.get("base_end_week", 6), "build": build_end,
            # No Specific phase unless configured: the old default (14) could sit
            # ABOVE a configured peak_end_week, swallowing the taper — Calum's
            # race week resolved to "specific" instead of taper.
            "specific": ptss.get("specific_end_week", build_end),
            "peak": ptss.get("peak_end_week", 17)}
    week_now = max(1, (today - plan_start).days // 7 + 1)
    phase = next((p for p in _PHASES if week_now <= ends[p]), "taper")
    if phase == "taper":
        # Shaped taper: stepped volume targets so the load checks stay ENGAGED
        # in the most consequential weeks (previously no target -> every audit
        # disengaged and volume was left to LLM discretion).
        race_s = cfg.get("race_date")
        if not race_s or not ctl_today:
            missing = "race_date" if not race_s else "ctl_today"
            return {"phase": "taper/race", "ctl_today": ctl_today, "training_week": week_now,
                    "week_type": "taper",
                    "note": f"taper, but no {missing} available — volume target could not "
                            "be computed; step down toward race day, hold intensity"}
        race = date.fromisoformat(race_s)
        days_to_race = max(0, (race - today).days)
        weeks_to_race = max(1, -(-days_to_race // 7))          # ceil
        factor = _TAPER_FACTORS.get(min(weeks_to_race, 3), _TAPER_FACTORS[3])
        pre_taper_weekly = 7.0 * float(ctl_today)
        target = int(round(pre_taper_weekly * factor))
        return {"phase": "taper", "week_type": "taper", "training_week": week_now,
                "ctl_today": ctl_today, "race_date": race_s,
                "weeks_to_race": weeks_to_race, "taper_factor": factor,
                "required_weekly_tss": target, "recommended_weekly_tss": target,
                "note": (f"TAPER, race in {weeks_to_race} wk: volume stepped to "
                         f"{int(factor * 100)}% of the ~{int(round(pre_taper_weekly))} TSS "
                         f"maintenance load (70/55/40 step-down). Hold INTENSITY — keep "
                         f"race-pace/threshold sharpness at reduced dose, keep session "
                         f"frequency; cut duration, never intensity. Race week: the race "
                         f"itself is most of the load.")}

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

    # Deload branch (audit P0-1): the smooth CTL-chase never unloaded — classic
    # accumulation/overuse pattern. Every Nth training week steps down to ~62%,
    # and a badly missed week (<70% executed) converts this week to recovery.
    rec = out["recommended_weekly_tss"]
    out["week_type"] = phase
    n = int(cfg.get("deload_every_n_weeks", _DELOAD_EVERY_N) or 0)
    factor = float(cfg.get("deload_factor", _DELOAD_FACTOR))
    deload_why = None
    if n and week_now % n == 0:
        deload_why = f"scheduled deload (every {n}th training week; week {week_now})"
    elif last_week_tss is not None and rec and float(last_week_tss) < _MISS_TRIGGER * rec:
        deload_why = (f"recovery week: last week's executed load "
                      f"({int(last_week_tss)} TSS) was under {int(_MISS_TRIGGER * 100)}% "
                      f"of prescription (~{rec})")
    if deload_why:
        out.update({
            "week_type": "deload",
            "deload_reason": deload_why,
            "full_week_tss": rec,
            "recommended_weekly_tss": int(round(rec * factor)),
            "note": (f"DELOAD WEEK ({deload_why}): prescribe ~{int(round(rec * factor))} TSS "
                     f"({int(factor * 100)}% of the normal ~{rec}). Keep session frequency "
                     f"and one short quality touch; cut volume. Adaptation happens in the "
                     f"unload — do not chase the CTL line this week.")})
    return out


def last_week_actual_tss(client, today: date | None = None) -> float | None:
    """Sum of actual training load over the last COMPLETED Mon-Sun week.
    None on fetch failure (miss-trigger then simply doesn't run); 0.0 for a
    genuinely empty week (which correctly converts this week to recovery)."""
    today = today or date.today()
    monday = _monday(today)
    lo = (monday - timedelta(days=7)).isoformat()
    hi = (monday - timedelta(days=1)).isoformat()
    try:
        hist = client.get_training_history(days=(today - (monday - timedelta(days=7))).days + 1)
        return round(sum(float(a.get("icu_training_load") or 0) for a in hist or []
                         if lo <= (a.get("start_date_local") or "")[:10] <= hi), 1)
    except Exception:
        return None


def cmd_required_tss(args) -> dict:
    cfg = _load_cfg(args.athlete)
    client = _client(cfg)
    ctl_today = args.ctl_today
    if ctl_today is None:
        w = client.get_wellness(days=3)
        if not w:
            raise SystemExit(_err("no wellness data for current CTL; pass --ctl-today"))
        ctl_today = round(float(w[-1].get("ctl") or 0), 1)
    return {"athlete": args.athlete,
            **required_tss(cfg, ctl_today, last_week_tss=last_week_actual_tss(client))}


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
    the Sunday generator uses (day_rules, CTL ramp, weekly TSS ceiling, strength
    cap, intensity distribution). The weekly_tss_cap here is the blueprint HOURS
    ceiling (max_hours_per_week x 100 x IF^2) — a hard upper bound, distinct from
    the required-tss target, which is a floor."""
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
    tss_cap = None
    try:
        prof_p = BASE / "athletes" / args.athlete / "profile.json"
        max_h = (json.loads(prof_p.read_text()) if prof_p.exists() else {}).get("max_hours_per_week")
        if max_h and phase.get("name"):
            tss_cap = tss_ceiling(float(max_h), str(phase["name"]))
    except Exception:
        pass
    rep = validate_week(
        events, week_start,
        day_rules=day_rules, ctl_today=ctl_today,
        weekly_tss_cap=tss_cap,
        ramp_cap=float(cfg.get("max_ctl_ramp_per_week", 5.0)),
        strength_max=(day_rules or {}).get("strength_max"),
        distribution=phase.get("distribution"),
    )
    viol = [{"code": v.code, "severity": v.severity, "message": str(v)} for v in rep.violations]
    return {"athlete": args.athlete, "week_start": week_start.isoformat(),
            "total_tss": round(rep.total_tss),
            "ok": not any(v["severity"] == "hard" for v in viol),
            "hard": [v for v in viol if v["severity"] == "hard"],
            "soft": [v for v in viol if v["severity"] != "hard"],
            "skipped_checks": rep.skipped}


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


def _fuelling_engine(cmd: str, params: dict) -> dict:
    """Run the SHARED JS fuelling engine (js/fuelling-engine.js — the exact code
    behind the web planner) via Node. Physiology lives there, not here, so the
    coach and the planner never diverge. cmd is targets|check|caps."""
    if not FUELLING_CLI.exists():
        raise SystemExit(_err(f"fuelling engine not found at {FUELLING_CLI}"))
    try:
        proc = subprocess.run(
            ["node", str(FUELLING_CLI), cmd, json.dumps(params)],
            capture_output=True, text=True, timeout=20)
    except FileNotFoundError:
        raise SystemExit(_err("node is not installed — the fuelling engine needs Node.js"))
    except subprocess.TimeoutExpired:
        raise SystemExit(_err("fuelling engine timed out"))
    out = (proc.stdout or "").strip()
    if not out:
        raise SystemExit(_err(f"fuelling engine returned nothing (stderr: {proc.stderr.strip()[:200]})"))
    data = json.loads(out)
    if isinstance(data, dict) and data.get("error"):
        raise SystemExit(_err(f"fuelling engine: {data['error']}"))
    return data


def _race_hours(cfg) -> float:
    """Total race duration (hours) from the athlete's race_target_splits."""
    s = cfg.get("race_target_splits") or {}
    total = (s.get("swim_min", 0) + s.get("bike_min", 0)
             + s.get("run_min", 0) + s.get("t1t2_min", 0))
    return (total / 60.0) if total else 0.0


def _body_and_sweat(slug, cfg, args):
    """Body weight and sweat inputs: CLI overrides, then athlete profile, then
    config, then evidence-based defaults."""
    prof = {}
    pp = BASE / "athletes" / slug / "profile.json"
    if pp.exists():
        try:
            prof = json.loads(pp.read_text())
        except Exception:
            prof = {}
    wt = (getattr(args, "weight", None)
          or prof.get("race_weight_kg") or prof.get("weight_kg")
          or cfg.get("race_weight_kg") or 75.0)
    sweat = getattr(args, "sweat", None) or cfg.get("sweat_ml_hr") or 1000.0
    sweat_na = getattr(args, "sweat_na", None) or cfg.get("sweat_na_mg_l") or 950.0
    return float(wt), float(sweat), float(sweat_na)


def cmd_race_fuelling(args) -> dict:
    """Evidence-based race fuelling targets (carb/fluid/sodium/caffeine) from the
    athlete's race duration, body weight and sweat data. Runs the shared engine —
    use these numbers, never invent your own. Points athletes to the web planner
    for the interactive per-leg schedule."""
    cfg = _load_cfg(args.athlete)
    rH = args.hours if getattr(args, "hours", None) else _race_hours(cfg)
    if not rH:
        raise SystemExit(_err("no race duration — set race_target_splits in config or pass --hours"))
    wt, sweat, sweat_na = _body_and_sweat(args.athlete, cfg, args)
    t = _fuelling_engine("targets", {
        "raceHours": rH, "bodyKg": wt, "sweatMlHr": sweat,
        "sweatNaMgL": sweat_na, "gutTrained": bool(args.gut_trained)})
    t["athlete"] = args.athlete
    t["race_name"] = cfg.get("race_name")
    t["inputs"] = {"body_kg": wt, "sweat_ml_hr": sweat, "sweat_na_mg_l": sweat_na}
    t["web_planner"] = "https://diamondpeak.uk/cycling/fuelling-calculator.html"
    return t


def cmd_fuel_check(args) -> dict:
    """Red-flag review of an intended fuelling rate against the evidence (glucose
    transporter cap, glucose:fructose ratio, hydration, sodium, caffeine). Runs
    the shared engine. Gut-backlog and fuelling-gap checks need the web planner."""
    cfg = _load_cfg(args.athlete)
    rH = args.hours if getattr(args, "hours", None) else _race_hours(cfg)
    if not rH:
        raise SystemExit(_err("no race duration — set race_target_splits in config or pass --hours"))
    wt, sweat, sweat_na = _body_and_sweat(args.athlete, cfg, args)
    carb = args.carb if args.carb is not None else 0.0
    glu = args.glucose if args.glucose is not None else carb * 0.6
    fru = args.fructose if args.fructose is not None else max(0.0, carb - glu)
    data = _fuelling_engine("check", {
        "carbGHr": carb, "glucoseGHr": glu, "fructoseGHr": fru,
        "fluidMlHr": args.fluid or 0.0, "sodiumMgHr": args.sodium or 0.0,
        "caffeineTotalMg": args.caffeine or 0.0, "raceHours": rH,
        "bodyKg": wt, "sweatMlHr": sweat, "sweatNaMgL": sweat_na,
        "gutTrained": bool(args.gut_trained)})
    flags = data.get("flags", [])
    return {"athlete": args.athlete, "race_hours": round(rH, 2),
            "gut_trained": bool(args.gut_trained),
            "risks": [f for f in flags if f["level"] == "risk"],
            "warnings": [f for f in flags if f["level"] == "warn"],
            "flags": flags}


# ── subcommand: log-strength ───────────────────────────────────────────────────
def cmd_log_strength(args) -> dict:
    """Log a non-device training session (CrossFit / gym / kettlebells) as REAL
    load: push a WeightTraining event for that day and mark it done, which makes
    intervals.icu create the matching manual activity — so CTL/ATL genuinely see
    the work (5 Jul 2026 decision: Calum's CrossFit must feed the load model).
    TSS is deterministic from duration + RPE (est IF = 0.55 + 0.025 x RPE), never
    an LLM guess: 60 min at RPE 7 ~ 53 TSS, matching the agreed 40-60 band."""
    cfg = _load_cfg(args.athlete)
    client = _client(cfg)
    d = args.date or date.today().isoformat()
    minutes = int(args.minutes or 60)
    rpe = max(1, min(10, int(args.rpe if args.rpe is not None else 7)))
    est_if = 0.55 + 0.025 * rpe
    tss = int(round(minutes / 60.0 * 100 * est_if * est_if))
    name = args.name or f"CrossFit ({minutes}min, RPE {rpe})"
    act = client.create_manual_activity(
        sport="WeightTraining", start_date_local=f"{d}T18:00:00", name=name,
        moving_time_s=minutes * 60, training_load=tss,
        description=f"logged via plan_tools log-strength: est IF {est_if:.2f} from RPE {rpe}")
    return {"athlete": args.athlete, "date": d, "minutes": minutes, "rpe": rpe,
            "est_if": round(est_if, 3), "tss": tss, "activity_id": act.get("id"),
            "undo": f"delete_activity('{act.get('id')}')",
            "status": "manual activity created — counts toward CTL/ATL"}


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

    prf = sub.add_parser("race-fuelling", help="evidence-based race carb/fluid/sodium/caffeine targets (shared engine)")
    prf.add_argument("--athlete", required=True)
    prf.add_argument("--hours", type=float, help="race duration (defaults to race_target_splits)")
    prf.add_argument("--weight", type=float, help="body weight kg (defaults to profile race_weight_kg)")
    prf.add_argument("--sweat", type=float, help="sweat rate ml/hr (default 1000)")
    prf.add_argument("--sweat-na", type=float, dest="sweat_na", help="sweat sodium mg/L (default 950)")
    prf.add_argument("--gut-trained", action="store_true", help="raise caps to 72/48 (120 g/hr)")

    pfc = sub.add_parser("fuel-check", help="red-flag review of an intended fuelling rate (shared engine)")
    pfc.add_argument("--athlete", required=True)
    pfc.add_argument("--carb", type=float, help="total carb g/hr")
    pfc.add_argument("--glucose", type=float, help="glucose g/hr (default 60%% of carb)")
    pfc.add_argument("--fructose", type=float, help="fructose g/hr (default carb - glucose)")
    pfc.add_argument("--fluid", type=float, help="fluid ml/hr")
    pfc.add_argument("--sodium", type=float, help="sodium mg/hr")
    pfc.add_argument("--caffeine", type=float, help="total caffeine mg")
    pfc.add_argument("--hours", type=float)
    pfc.add_argument("--weight", type=float); pfc.add_argument("--sweat", type=float)
    pfc.add_argument("--sweat-na", type=float, dest="sweat_na")
    pfc.add_argument("--gut-trained", action="store_true")

    pls = sub.add_parser("log-strength", help="log CrossFit/gym as real ICU load (manual activity via mark-as-done)")
    pls.add_argument("--athlete", required=True)
    pls.add_argument("--minutes", type=int, default=60)
    pls.add_argument("--rpe", type=int, help="1-10; default 7 (est IF = 0.55 + 0.025 x RPE)")
    pls.add_argument("--date", help="YYYY-MM-DD; default today")
    pls.add_argument("--name")

    args = p.parse_args()
    handler = {"tss": cmd_tss, "week-tss": cmd_week_tss, "project": cmd_project,
               "required-tss": cmd_required_tss, "validate": cmd_validate,
               "render-workout": cmd_render_workout, "fuel-target": cmd_fuel_target,
               "race-fuelling": cmd_race_fuelling, "fuel-check": cmd_fuel_check,
               "log-strength": cmd_log_strength}[args.cmd]
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
