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

  # Fixed weekly-summary caption text (Hrs / TSS-vs-floor / by-sport / Fitness-ramp)
  # — the ONE template, never re-typed from memory
  python3 plan_tools.py week-caption --athlete jamie --start 2026-08-18 --end 2026-08-24

  # Day-by-day CTL/ATL/TSB projection (seeds default to latest wellness)
  python3 plan_tools.py project --athlete jamie \
        --daily '[{"date":"2026-06-16","tss":113},{"date":"2026-06-17","tss":58}]'

  # What SHOULD this week's TSS be, given the phase CTL target
  python3 plan_tools.py required-tss --athlete jamie

  # Windowed/segment NP and W' balance — never hand-compute either from raw
  # streams (skips the 30s rolling average / has no recovery term; see
  # lib/np_curve.py and lib/wbal.py for why).
  python3 plan_tools.py windowed-np --athlete jamie --activity-id i12345 \
        --start 600 --end 900
  python3 plan_tools.py wbal --athlete jamie --activity-id i12345 \
        --cp-low 250 --cp-high 265 --wprime-j 20000

Every subcommand prints a single JSON object to stdout. On error it prints
{"error": "..."} and exits non-zero.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent          # ClaudeCoach/
FUELLING_CLI = BASE.parent / "js" / "fuelling-cli.js"  # shared JS engine bridge
WETSUIT_CLI = BASE.parent / "js" / "wetsuit-cli.js"    # shared JS engine bridge
sys.path.insert(0, str(BASE / "ironman-analysis"))
sys.path.insert(0, str(BASE / "lib"))

from primitives.planned_tss import (                            # noqa: E402
    race_tss, race_tss_from_prev_race, race_leg_distances,
    planned_session_tss, tss_from_segments, render_workout, segment_if,
    name_intensity_mismatch,
)
from primitives.load import (                                   # noqa: E402
    compute_required_tss,
    project_pmc_daily,
    derive_phase_ctl_targets,
)
from primitives.validate_plan import validate_week              # noqa: E402
from primitives.blueprint import current_phase                   # noqa: E402
from primitives.nutrition import fuel_target, recent_avg_g_hr   # noqa: E402
import fuel_basis                                               # noqa: E402
from rpe_context import SPORT_FAMILY                             # noqa: E402
import replan_gate                                               # noqa: E402
import race_fitness as rf                                        # noqa: E402
import goals as _goals                                           # noqa: E402
import baseline as _baseline                                     # noqa: E402

# The three subcommands lib/icu_fetch.py's push_workout/edit_workout gate on (the
# ad-hoc-replan fix, 24 Aug 2026 — see replan_gate.py). Running one of these for the
# CC_ATHLETE_SCOPE athlete is what "recomputed this turn" means; every other
# subcommand is either a read, a pure helper (sum, session-load) or already
# athlete-scoped some other way and does not clear the gate.
_RECOMPUTE_COMMANDS = frozenset({"tss", "session-for-load", "required-tss"})

# Non-endurance ICU types that all mean "the strength / other slot for that day".
# Kathryn's 27 Jul week had a COMPLETED "Cardio" against a PLANNED "Kettlebell";
# neither string appears in SPORT_FAMILY, so they read as two different sports and
# the planned one was counted a second time.
_STRENGTH_TYPES = frozenset({
    "WeightTraining", "Workout", "Strength", "Kettlebell", "Cardio",
    "Crossfit", "Yoga", "Pilates", "Elliptical", "StairStepper",
})


def _sport_family(icu_type: str) -> str:
    """Collapse an ICU activity/event `type` to the family a day's slot belongs to.

    Exists because week-TSS de-duplication compared RAW type strings, so a completed
    VirtualRide never matched its own planned Ride twin and both were summed. Jamie's
    standing rule already said to treat Gravel and Virtual Ride as subsets of Ride -
    it had simply never been implemented in code, and seven separate ad-hoc sport-family
    maps existed elsewhere in the tree while this path had none.

    Unknown types fall back to their own lowercased name, so anything not mapped
    de-duplicates exactly as it did before - no silent widening.
    """
    t = (icu_type or "").strip()
    if t in SPORT_FAMILY:
        return SPORT_FAMILY[t]
    if t in _STRENGTH_TYPES:
        return "strength"
    return t.lower() or "?"


def _already_completed(planned_type: str, completed_for_day: list) -> bool:
    """True when a completed activity already occupies this planned session's slot,
    i.e. the planned TSS must NOT be added on top. Compares FAMILIES, not raw type
    strings."""
    fam = _sport_family(planned_type)
    return any(_sport_family(c.get("sport")) == fam for c in completed_for_day or [])

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


# ── subcommand: session-for-load ───────────────────────────────────────────────
# Endurance-zone defaults used when the caller names no intensity. Deliberately
# conservative (steady aerobic) and ALWAYS echoed back, so the assumption is
# never silent.
_DEFAULT_ZONE = {"swim": "aerobic", "run": "easy", "bike": "z2", "brick": "brick"}


def cmd_session_for_load(args) -> dict:
    """Derive a session DURATION from a Load/TSS target, holding Load fixed.

    The inverse of `tss`: given a Load the athlete wants (e.g. "target 220
    Load"), compute how long the session must be at a chosen intensity —
    duration_min = load / (100 x IF^2) x 60 — so a value labelled TSS/Load can
    NEVER be silently written as minutes. Returns ready-to-render segments whose
    computed TSS ~= the target (whole-minute rounding aside); ICU recomputes its
    own load on push. This is the ONLY correct way to turn a Load target into a
    session — never hand-convert TSS to minutes."""
    from primitives.planned_tss import _norm_sport  # primitives already on path
    sport = args.sport
    load = args.load_target
    if load is None or load <= 0:
        raise SystemExit(_err("--load-target must be a positive number (the TSS/Load to hit)"))
    use_if = args.if_ is not None and not args.zone
    if use_if:
        intensity = float(args.if_)
        src = "explicit --if"
    else:
        zone = args.zone or _DEFAULT_ZONE.get(_norm_sport(sport))
        if not zone:
            raise SystemExit(_err("provide --if or --zone (no endurance default for this sport)"))
        intensity = segment_if(sport, zone)
        src = f"zone '{zone}'" + ("" if args.zone else " (default endurance zone)")
    if intensity <= 0:
        raise SystemExit(_err("intensity resolved to 0 — pass a valid --zone or --if"))
    duration_min = int(round((load / (100.0 * intensity ** 2) * 60.0) / 5.0) * 5)
    duration_min = max(15, duration_min)
    seg = ({"minutes": duration_min, "if": round(intensity, 3)} if use_if
           else {"minutes": duration_min, "zone": (args.zone or _DEFAULT_ZONE.get(_norm_sport(sport)))})
    check = tss_from_segments(sport, [seg])
    return {
        "sport": sport,
        "load_target": int(round(load)),
        "assumed_if": round(intensity, 3),
        "intensity_source": src,
        "duration_min": duration_min,
        "segments": [seg],
        "computed_tss": check["tss"],
        "note": (f"Duration DERIVED from the {int(round(load))} Load target (held fixed) at "
                 f"IF {round(intensity, 3)}: {duration_min} min. A Load target is NEVER minutes. "
                 f"To push: render-workout --sport {sport} --segments '<segments above>', then "
                 f"push_workout with that structured description (ICU recomputes the load)."),
    }


# ── subcommand: session-load ───────────────────────────────────────────────────
def cmd_session_load(args) -> dict:
    """The ONE authoritative Load for a planned session — icu_training_load,
    else load_target, else the deterministic calculation. There is exactly one
    Load per session; quote THIS number and never a second, self-computed one
    (the 183-vs-202 contradiction, 11 Jul)."""
    if args.event:
        try:
            ev = json.loads(args.event)
        except json.JSONDecodeError as e:
            raise SystemExit(_err(f"--event is not valid JSON: {e}"))
    elif args.athlete and args.date:
        cfg = _load_cfg(args.athlete)
        evs = _client(cfg).get_events(args.date, args.date) or []
        evs = [e for e in evs if (e.get("category") or "WORKOUT").upper() == "WORKOUT"]
        if args.sport:
            evs = [e for e in evs if (e.get("type") or "").lower() == args.sport.lower()]
        if not evs:
            return {"error": f"no planned workout on {args.date}"
                    + (f" for sport {args.sport}" if args.sport else "")}
        ev = evs[0]
    else:
        raise SystemExit(_err("provide --event <json> OR --athlete and --date"))
    r = planned_session_tss(ev)
    return {"name": r["name"], "sport": ev.get("type") or "",
            "date": (ev.get("start_date_local") or "")[:10] or args.date,
            "load": r["tss"], "load_source": r["source"], "duration_min": r["duration_min"],
            "note": "Single authoritative Load. Do not state any other Load figure for this session."}


# ── subcommand: sum ─────────────────────────────────────────────────────────────
def cmd_sum(args) -> dict:
    """Deterministic addition — never sum training numbers by hand (the 10 Jul
    '~120 each' that did not add to 815). Sums a JSON list of numbers."""
    try:
        vals = json.loads(args.values)
    except json.JSONDecodeError as e:
        raise SystemExit(_err(f"--values is not valid JSON: {e}"))
    if not isinstance(vals, list) or not vals or not all(isinstance(v, (int, float)) for v in vals):
        raise SystemExit(_err("--values must be a non-empty JSON list of numbers"))
    total = sum(vals)
    return {"values": vals, "count": len(vals),
            "total": round(total, 2) if any(isinstance(v, float) for v in vals) else total}


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
        if _already_completed(sport, completed.get(d, [])):
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


# ── subcommand: week-caption ────────────────────────────────────────────────────
# Sport families the caption reports as their own line. Gravel/Virtual/Trail etc.
# already fold into these via _sport_family (Jamie's standing Ride-grouping rule);
# strength/mobility is deliberately excluded (Jamie, 24 Aug 2026: "remove the
# strength... make it short so it fits as a caption").
_CAPTION_SPORT_LABELS = {"bike": "Ride", "run": "Run", "swim": "Swim"}


def cmd_week_caption(args) -> dict:
    """The ONE fixed weekly-summary caption template (Jamie, 24 Aug 2026): Hrs,
    TSS-vs-floor, one line per sport, Fitness ramp — short enough to fit as a photo
    caption, no strength line, no day-by-day detail. Was being re-typed from
    memory each week and drifted into 7 near-duplicate persistent-rules.md entries;
    this is now the single source of the format."""
    cfg = _load_cfg(args.athlete)
    week_start = date.fromisoformat(args.start)
    week_end = date.fromisoformat(args.end)
    if week_end < week_start:
        raise SystemExit(_err("--end must not be before --start"))

    client = _client(cfg)
    today = date.today()
    history = client.get_training_history(days=max(7, (today - week_start).days + 1))
    events = client.get_events(week_start.isoformat(), week_end.isoformat())

    completed = {}
    for a in history or []:
        d = (a.get("start_date_local") or "")[:10]
        if not (week_start.isoformat() <= d <= week_end.isoformat()):
            continue
        completed.setdefault(d, []).append({
            "sport": a.get("type") or "?",
            "tss": int(round(float(a.get("icu_training_load") or 0))),
            "min": round((a.get("moving_time") or 0) / 60),
            "km": round((a.get("distance") or 0) / 1000, 1),
        })

    by_day = {}
    for off in range((week_end - week_start).days + 1):
        d = (week_start + timedelta(days=off)).isoformat()
        by_day[d] = list(completed.get(d, []))

    for ev in events or []:
        d = (ev.get("start_date_local") or "")[:10]
        if d not in by_day:
            continue
        if ev.get("category") and ev.get("category") != "WORKOUT":
            continue
        sport = ev.get("type") or ""
        if _already_completed(sport, completed.get(d, [])):
            continue  # actual already counted
        r = _event_tss(ev)
        by_day[d].append({"sport": sport, "tss": r["tss"], "min": r["duration_min"], "km": 0.0})

    totals = {fam: {"n": 0, "min": 0, "km": 0.0, "tss": 0} for fam in _CAPTION_SPORT_LABELS}
    total_tss = total_min = 0
    for sessions in by_day.values():
        for s in sessions:
            total_tss += s["tss"]
            total_min += s["min"]
            fam = _sport_family(s["sport"])
            if fam in totals:
                t = totals[fam]
                t["n"] += 1
                t["min"] += s["min"]
                t["km"] += s["km"]
                t["tss"] += s["tss"]

    # CTL at the start and end of the window, for the "Fitness A -> B" line — the
    # floor also needs an end-of-window CTL to derive the phase's weekly_tss_floor.
    anchor = min(week_end, today)
    wellness = client.get_wellness(days=(anchor - week_start).days + 3,
                                    newest=anchor.isoformat())
    by_date = {(row.get("id") or "")[:10]: row for row in (wellness or [])}
    ctl_start = (by_date.get(week_start.isoformat()) or {}).get("ctl")
    ctl_end = (by_date.get(week_end.isoformat()) or (wellness[-1] if wellness else {})).get("ctl")

    floor = None
    if ctl_end:
        req = required_tss(cfg, round(float(ctl_end), 1), today=week_end, last_week_tss=None)
        floor = req.get("weekly_tss_floor")

    lines = [f"Hrs: {round(total_min / 60, 1)} · TSS: {total_tss}" +
             (f" (min {int(floor)})" if floor else "")]
    for fam, label in _CAPTION_SPORT_LABELS.items():
        t = totals[fam]
        if t["n"] == 0:
            continue
        lines.append(f"{label}: {t['n']}x, {round(t['min'] / 60, 1)}h, "
                      f"{round(t['km'], 1)}km, Load {t['tss']}")
    if ctl_start is not None and ctl_end is not None:
        lines.append(f"Fitness {round(float(ctl_start), 1)} -> {round(float(ctl_end), 1)}, "
                      f"ramp {round(float(ctl_end) - float(ctl_start), 1)}/wk")

    return {"athlete": args.athlete, "week_start": week_start.isoformat(),
            "week_end": week_end.isoformat(), "weekly_tss_floor": floor,
            "caption": "\n".join(lines)}


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

# Return-to-load step and the de-facto-deload query (Jamie + Kathryn, 10 Aug 2026).
#
# _MISS_TRIGGER only fires on a COLLAPSE (< 70% of maintenance). The far commoner case
# is a week that merely failed to build: Kathryn ran 474 against 584 planned with
# maintenance at 489, i.e. 97% of maintenance, so nothing tripped - and the next week's
# target was then computed purely from the CTL line, asking 690. That is +46% on what
# she had actually just done, off a week with no build in it, and it exceeded her
# highest realised ramp all season (+4.3 CTL). The engine sized the week from the plan
# while ignoring where the athlete actually was.
#
# So a week at or below maintenance limits the NEXT week's step up to this multiple of
# what was actually executed. Never below maintenance itself, or the cap would prescribe
# detraining, which is the opposite failure.
_RETURN_STEP = 1.30

# A week at or below maintenance IS an unload in all but name. When the cadence then
# schedules a deload on top, that is two consecutive down-weeks nobody chose, landing
# (for Kathryn) in the first week of her Peak phase while she sat 6 CTL below her race
# band. This does NOT auto-skip the deload - block_deload_weeks' contract is that
# recovery is never silently stripped - it attaches a question for the brief and the
# audit to surface. Jamie only got this corrected by arguing with the coach for an
# hour; Kathryn, who does not push back, silently got the wrong plan.
_DEFACTO_DELOAD_AT = 1.00   # last week <= 1.00 x maintenance = already a de facto unload

# Taper (blueprint: "70% -> 55% -> 40% of peak, maintain intensity"; audit P0-2 —
# volume steps down by weeks-to-race, intensity is HELD by the taper TID row).
# Pre-taper weekly load is approximated by the steady-state 7 x CTL (the load
# that holds current fitness); CTL decays slightly through the taper so the
# absolute numbers drift down a touch more — the safe direction.
_TAPER_FACTORS = {3: 0.70, 2: 0.55, 1: 0.40}
# A taper this far out is a plan_start left over from an earlier race, not a taper
# (a long phase_tss tail can legitimately reach 4-5 weeks, so this is generous).
_STALE_TAPER_DAYS = 56

# RACE WEEK (6 Sep 2026). The ladder above is a WHOLE-WEEK volume target, and in race
# week the whole week includes the race — the single biggest session of the year, and
# the one thing on the calendar that was never costed anywhere. validate_week only ever
# sees the built proposal, so the race sat outside the week total, outside the load cap
# and outside the CTL-ramp projection, and the ladder's final 40% step was handed to the
# planner as a TRAINING budget: for a CTL-110 Ironman athlete, ~300 TSS of training in
# the seven days around a ~540 TSS race. A unit error, not a coaching choice.
#
# So the race is deducted first and what remains is the training budget, with a floor:
# taper theory cuts duration, never frequency, and race week still needs its openers —
# short sessions carrying a few race-effort minutes. For any long-course race the
# deduction exceeds the whole-week figure and the floor is what gets prescribed, which
# is the right answer: openers, then race.
_RACE_WEEK_MIN = 0.15        # openers floor, as a fraction of the 7 x CTL maintenance load


# Every week type that is light BY DESIGN. One definition, because it is asked in five
# places for two different reasons and they must not drift:
#
#   - "do not force quality into this week" (stage1's minimum-quality floor, the
#     intensity-budget check and the zone-deviation ranking). A race week and a
#     post-race transition week are as much down-weeks as a deload is; typed only as
#     deload/taper, those three checks would have demanded a normal week's quality dose
#     in race week and in the week after an Ironman.
#   - "this week being light is not evidence of a MISSED week" (the miss-trigger and the
#     return-to-load step cap below), which must not cascade a recovery week off a week
#     that was prescribed light on purpose.
DOWN_WEEK_TYPES = ("deload", "taper", "race", "post_race", "baseline")


# Race intensity, per event, for the OPENERS guidance below. Long-course racing is done
# at or below the top of Z2 — Jamie's IM Italy bike was 230 W against an FTP of 307, i.e.
# 75% of FTP, the exact top of the Coggan Z2 band — so telling a long-course athlete to
# put "a few minutes at race effort" in their race-week openers prescribes a stimulus
# that is easier than their normal easy riding. Openers are priming, not training: they
# have to sit ABOVE race intensity to do anything at all. For short-course racing the
# opposite holds and race pace IS the sharpening intensity.
_LONG_COURSE_IF = 0.78     # at or below this, race intensity is not a sharpening stimulus

_OPENERS_LONG = ("Openers are SHORT efforts ABOVE race intensity — 3-6 min TOTAL of "
                 "threshold/VO2 in bursts of 1-3 min with full recovery, inside an "
                 "otherwise easy 30-45 min. Race intensity for this event sits at or "
                 "below the top of Z2, so 'a few minutes at race effort' is NOT a "
                 "stimulus and does not prime anything: go above it, briefly. Race-pace "
                 "work this week is for pacing and fuelling rehearsal only, at most "
                 "10-15 min, and is not the sharpening.")
_OPENERS_SHORT = ("Openers are SHORT efforts at or just above race intensity — 5-8 min "
                  "total in bursts of 1-2 min with full recovery, inside an otherwise "
                  "easy 30-45 min. For this event race pace IS the sharpening intensity.")


def race_intensity(cfg: dict, profile: dict | None = None) -> float:
    """Best available whole-race bike IF, for deciding what 'openers' should mean."""
    pr = ((profile or {}).get("prev_race") or {})
    try:
        if pr.get("bike_if"):
            return float(pr["bike_if"])
    except (TypeError, ValueError):
        pass
    from primitives.planned_tss import _RACE_PROFILE, _RACE_PROFILE_DEFAULT, _race_event_key
    return _RACE_PROFILE.get(_race_event_key(cfg.get("race_distance") or ""),
                             _RACE_PROFILE_DEFAULT)[1]


def _same_distance(prev_race: dict, cfg: dict) -> bool:
    """Is the athlete's previous race the same distance as the one being planned?

    This gate has to be POSITIVE, not merely "no evidence against": borrowing a 70.3's
    cost for a full Ironman would UNDER-price race week by ~240 TSS, which is the exact
    failure being fixed. Three signals, cheapest first, and the last is the one that
    actually catches a mismatch — a stated distance is often absent and a name is
    unreliable ("IM Italy" vs "IM Italy Emilia-Romagna" is the same race).
    """
    dist = str(prev_race.get("distance") or "").strip().lower()
    want = str(cfg.get("race_distance") or "").strip().lower()
    if dist and want:
        return dist == want
    a = str(prev_race.get("name") or "").strip().lower()
    b = str(cfg.get("race_name") or "").strip().lower()
    if a and b and (a in b or b in a):
        return True
    # Duration plausibility against the event's typical hours: a 70.3 is never within a
    # third of an Ironman's day.
    from primitives.planned_tss import (_RACE_PROFILE, _RACE_PROFILE_DEFAULT,
                                        _race_event_key, _hhmm_min)
    hours = _RACE_PROFILE.get(_race_event_key(want), _RACE_PROFILE_DEFAULT)[0]
    total = _hhmm_min(prev_race.get("total_time"))
    if total is None:
        total = sum(filter(None, (_hhmm_min(prev_race.get(k))
                                  for k in ("swim_time", "bike_time", "run_time")))) or None
    return bool(total and abs(total / 60 - hours) / hours <= 0.35)


def race_load(cfg: dict, profile: dict | None = None) -> tuple:
    """(tss, source) for this athlete's race, best source first:

      explicit   `race_tss` in athletes.json — a stated figure always wins
      prev_race  what they ACTUALLY cost themselves last time at this distance
      duration   `race_expected_hours` x the event's whole-race IF
      event_default

    prev_race outranks the event default because a real race beats a table: Jamie's
    IM Italy came to 556 TSS summed per leg, against a 539 default that was never his.
    It is only used when the previous race was the SAME distance.
    """
    if cfg.get("race_tss"):
        return race_tss("", expected_tss=cfg["race_tss"])
    pr = (profile or {}).get("prev_race") or {}
    if pr and _same_distance(pr, cfg):
        swim_m, run_km = race_leg_distances(cfg.get("race_distance") or cfg.get("race_name") or "")
        got = race_tss_from_prev_race(
            pr, css_per_100m=(profile or {}).get("swim_css_per_100m"),
            run_threshold_pace_per_km=(profile or {}).get("run_threshold_pace_per_km"),
            swim_m=swim_m, run_km=run_km)
        if got:
            return got
    return race_tss(cfg.get("race_distance") or cfg.get("race_name") or "",
                    expected_hours=cfg.get("race_expected_hours"))

# POST-RACE TRANSITION (6 Sep 2026).
#
# There was no branch for "the race has happened". `week_now` simply kept counting past
# peak_end_week, so every week after race day resolved to the TAPER branch, and the taper
# branch clamps days_to_race at 0 -> weeks_to_race 1. The consequences all landed on the
# athlete: the week was prescribed as a race-week taper ("hold INTENSITY, keep race-pace
# sharpness"), the note told them their race was in 1 week when it was weeks behind them,
# and because current_phase clamped to the Taper phase whose tss_ceiling is None, the
# validator's weekly load cap was SKIPPED entirely — the one week of the year with no
# upper bound on load was the week after an Ironman.
#
# A finished A-race with nothing configured after it is not a taper and not a training
# block: it is a transition. Volume comes back gradually off ~7 x CTL (a figure that
# falls on its own as CTL decays, so this cannot ratchet), intensity stays off, and the
# note asks for the next race rather than inventing a target to chase.
_TRANSITION_FACTORS = {1: 0.30, 2: 0.45, 3: 0.60}

# Week 4 onward: work toward a MAINTENANCE CTL, not a percentage (Jamie, 6 Sep 2026).
#
# The first cut of this held at 65% of 7 x CTL, recomputed each week off the CURRENT
# CTL. That is not a hold: 7 x CTL is precisely the load that keeps CTL level, so a
# fixed fraction of it prescribes less every week as CTL falls, and CTL chases the
# target down. Simulated from CTL 45 it goes 45 -> 23 in twelve weeks and keeps going;
# the asymptote is zero. A "maintenance" setting that detrains the athlete to nothing
# is worse than no setting at all, because it reads as deliberate.
#
# So the off-season default is a TARGET, handled exactly like a phase CTL target: the
# weekly load that converges CTL on `maintenance_ctl` over _MAINTENANCE_CONVERGE_WEEKS,
# bounded by the athlete's ramp cap. Above the target the prescription sits below
# maintenance and CTL comes down to it; below the target it builds gently back up to it.
# Either way it settles and stays there instead of drifting.
#
# The NUMBER is athlete-dependent (Jamie: "TBC, athlete dependent") and is set per
# athlete as ctl_targets.maintenance_ctl. Until someone sets it, it is DERIVED at
# _MAINTENANCE_FRACTION of the athlete's own peak/race CTL and flagged as provisional,
# because the two obvious "safe" fallbacks are both wrong:
#
#   - hold CTL where the recovery weeks left it. For a CTL-115 Ironman athlete that
#     is a hold at ~88 (76% of race fitness) on ~615 TSS/wk — near race training, with
#     no race, indefinitely. Maintenance is obviously far below race fitness.
#   - decay by a fixed fraction of 7 x CTL. That has no floor at all (see above).
#
# Deriving from THEIR peak keeps it athlete-dependent, lands in the 55-70% of peak that
# off-season maintenance normally sits in, and the provisional flag puts the real number
# in front of the coach instead of burying the assumption.
_MAINTENANCE_CONVERGE_WEEKS = 4
_MAINTENANCE_FRACTION = 0.60


# POST-RACE RECOVERY HOLD (27 Sep 2026). Weeks 1-3 after an A-race were recovery by the
# calendar alone, and week 4 flipped straight into the off-season block (or the
# maintenance hold) whether or not the athlete felt ready. Jamie, after IM Italy: "until
# you tell Coach you're ready, your weekly plan is easy aerobic only, with no bricks, no
# quality and no 'X% off target' messages."
#
# Opt-in per athlete (`post_race_hold: true` in athletes.json), so nobody else's
# transition changes under them. While the hold is on and the athlete has not said
# `post_race_ready` on or after the race date, week 4 onward stays a post_race week at
# the week-3 recovery level instead of starting the next block. Saying ready (plan_tools
# post-race-ready) releases it; the block after it runs exactly as configured. It never
# SHORTENS weeks 1-3: ready only ends the hold, it does not skip recovery.
def recovery_race(cfg: dict, today=None):
    """(race_s, race_d) of the finished A-race the post-race state hangs off, or (None, None).

    Setting the NEXT A-race moves `race_date` into the future (Brighton, 29 Sep 2026), and
    every post-race check read only `race_date`, so the finished race vanished: recovery
    weeks, the hold and the off-season block all switched off, and week_now (still counted
    from the OLD plan_start) fell through to the taper branch 27 weeks out. The finished
    race is the latest past A-race in the registry (or a past `race_date`), and it governs
    until a NEW plan_start after it has ARRIVED: a block set to start in November does not
    end October's recovery early."""
    today = today or date.today()
    cands = []
    for raw in [{"date": cfg.get("race_date"), "priority": "A"}] + list(cfg.get("races") or []):
        if not isinstance(raw, dict) or str(raw.get("priority") or "").upper() != "A":
            continue
        try:
            d = date.fromisoformat(str(raw.get("date"))[:10])
        except ValueError:
            continue
        if d < today:
            cands.append(d)
    if not cands:
        return None, None
    race_d = max(cands)
    try:
        ps = date.fromisoformat(cfg["plan_start"]) if cfg.get("plan_start") else None
    except ValueError:
        ps = None
    if ps and race_d < ps <= today:
        return None, None            # the next block has started; that race is history
    return race_d.isoformat(), race_d


def post_race_ready_date(cfg: dict):
    """The date the athlete said they were ready to train again, or None."""
    raw = cfg.get("post_race_ready")
    try:
        return date.fromisoformat(str(raw)[:10]) if raw else None
    except ValueError:
        return None


def post_race_hold_active(cfg: dict, race_d, as_of=None) -> bool:
    """True when this athlete holds recovery after `race_d` until they say ready.

    `as_of` is the day being planned (a week's Monday). A ready date AFTER it does not
    release that week: saying ready on a Wednesday starts the block the next Monday, the
    same date set_post_race_ready reports, rather than relabelling a week in progress."""
    if not cfg.get("post_race_hold") or race_d is None:
        return False
    ready = post_race_ready_date(cfg)
    if not ready or ready < race_d:
        return True
    return bool(as_of and ready > as_of)


def post_race_block_week(cfg: dict, race_d, weeks_since: int) -> int:
    """Week number within the block that follows post-race recovery (1 = its first week).

    Normally week 4 after the race is block week 1. When a recovery hold released later,
    the block starts the first Monday on/after the ready date, so its intro/ramp-in weeks
    are not skipped by counting from week 4 (new-york, 27 Sep 2026)."""
    start = max(_TRANSITION_FACTORS) + 1
    ready = post_race_ready_date(cfg) if cfg.get("post_race_hold") else None
    if ready and race_d and ready >= race_d:
        first_monday = ready + timedelta(days=(7 - ready.weekday()) % 7)
        start = max(start, (first_monday - race_d).days // 7 + 1)
    return max(1, weeks_since - start + 1)


def all_bookings(cfg: dict, slug: str | None = None) -> list:
    """Every booked session (tests, PB attempts): the top-level `bookings`, which apply in
    any training block, plus the older `offseason.bookings`. Jamie, 29 Sep 2026: the
    tests and PB attempts carry on into the Brighton marathon block."""
    out = [b for b in (cfg.get("bookings") or []) if isinstance(b, dict)]
    out += [b for b in ((offseason_cfg(cfg) or {}).get("bookings") or []) if isinstance(b, dict)]
    # A goal block's end-of-block tests (lib/goals.py), only in weeks the goal is running:
    # an A race ahead, or its recovery weeks, own the calendar instead.
    out += goal_test_bookings(cfg, slug)
    return out


def held_bookings(cfg: dict, race_d, until) -> list:
    """Off-season bookings dated inside a recovery hold (week 4 after the race up to, not
    including, the Monday the block starts). They were never planned, so they are
    surfaced to be re-dated rather than silently dropped."""
    if not race_d:
        return []
    until = until if isinstance(until, date) else date.fromisoformat(str(until)[:10])
    hold_from = race_d + timedelta(days=7 * max(_TRANSITION_FACTORS))
    out = []
    for b in all_bookings(cfg):
        if not isinstance(b, dict):
            continue
        try:
            d = date.fromisoformat(str(b.get("date") or b.get("week_start"))[:10])
        except ValueError:
            continue
        if _monday(hold_from) <= d < until:
            out.append(b)
    return sorted(out, key=lambda b: str(b.get("date") or b.get("week_start")))


def in_post_race_recovery(cfg: dict, today=None) -> bool:
    """True in the recovery weeks after an A-race: weeks 1-3 for everyone, and on past
    week 3 while a recovery hold is active. The daily surfaces use it so a recovery week's
    high Form is never read as "good day for quality work"."""
    today = today or date.today()
    _, race_d = recovery_race(cfg, today)
    if not race_d:
        return False
    # Judged on the PLANNED week (its Monday), so the daily card agrees with the calendar:
    # a Saturday in week 3 is still a recovery day even though it is 21 days after.
    wk = max(_monday(today), race_d + timedelta(days=1))
    weeks_since = (wk - race_d).days // 7 + 1
    return (weeks_since <= max(_TRANSITION_FACTORS)
            or post_race_hold_active(cfg, race_d, as_of=wk))


def a_race_ahead(cfg: dict, today=None) -> bool:
    """True when an A race (the config's race_date or an A in the registry) is today or later."""
    today = today or date.today()
    for raw in [{"date": cfg.get("race_date"), "priority": "A"}] + list(cfg.get("races") or []):
        if not isinstance(raw, dict) or str(raw.get("priority") or "").upper() != "A":
            continue
        try:
            if date.fromisoformat(str(raw.get("date"))[:10]) >= today:
                return True
        except ValueError:
            continue
    return False


def goal_active(cfg: dict, today=None) -> bool:
    """True when this week is planned as a GOAL BLOCK week (lib/goals.py): the athlete has a
    goal, no A race ahead, is not in a race's recovery weeks, and has no off-season block
    configured for after that race (the off-season block wins)."""
    if not _goals.goal_cfg(cfg):
        return False
    today = today or date.today()
    if a_race_ahead(cfg, today) or in_post_race_recovery(cfg, today):
        return False
    _, race_d = recovery_race(cfg, today)
    return not (race_d and offseason_cfg(cfg))


def goal_start(cfg: dict, slug: str | None = None, day=None):
    """Monday the goal's CURRENT run of blocks started, as seen from `day`: block 1's
    Monday, or after an A race, the first Monday out of its recovery weeks (the goal
    restarts at block 1 week 1 rather than resuming mid-block). None without a goal."""
    base = _goals.block_start(cfg, slug)
    if not base:
        return None
    day = day or date.today()
    _, race_d = recovery_race(cfg, day)
    if race_d and race_d >= base - timedelta(days=7):
        m = _monday(race_d) + timedelta(days=7)
        for _ in range(60):
            if not in_post_race_recovery(cfg, m):
                break
            m += timedelta(days=7)
        return max(base, m)
    return base


def _goal_test_booking(test: dict, pos: dict) -> dict:
    return {"week_start": pos["week_start"], "sport": test["sport"],
            "name": f"{test['name']} (end of block {pos['block']})",
            "match": test["match"], "goal_test": True}


def goal_test_bookings(cfg: dict, slug: str | None = None, today=None,
                       weeks: int = 60) -> list:
    """The goal's end-of-block tests as bookings ({week_start, sport, name, match}), for
    the weeks from ~6 weeks back to `weeks` ahead in which the goal is running. [] for a
    goal with no test, or whose sport the plan does not cover."""
    g = _goals.goal_cfg(cfg)
    test = _goals.GOALS[g["type"]].get("test") if g else None
    if not test:
        return []
    focus = _goals.GOALS[g["type"]].get("sport")
    if focus and focus not in _goals.sports_for(cfg):
        return []
    base = _goals.block_start(cfg, slug)
    if not base:
        return []
    w = max(base, _monday(today or date.today()) - timedelta(weeks=6))
    out = []
    for _ in range(weeks):
        if goal_active(cfg, w):
            pos = _goals.position(goal_start(cfg, slug, w), w, _goals.block_weeks(cfg))
            if pos["kind"] == "test":
                out.append(_goal_test_booking(test, pos))
        w += timedelta(days=7)
    return out


def _goal_week(cfg: dict, ctl_today, today: date, last_week_tss=None,
               slug: str | None = None, profile: dict | None = None) -> dict:
    """required_tss for a GOAL BLOCK week. Load weeks: maintain converges on the band's
    middle; the others add the goal's Fitness ramp (capped by the athlete's own ramp cap).
    Easier and test weeks drop to easy_factor x maintenance with no floor. A load week
    after a collapsed one (< _MISS_TRIGGER of maintenance) becomes an easier week, and a
    load week after one at or under maintenance steps up at most _RETURN_STEP, as in a
    race block."""
    g = _goals.goal_cfg(cfg)
    gd = _goals.GOALS[g["type"]]
    start = goal_start(cfg, slug, today)
    nwk = _goals.block_weeks(cfg)
    pos = _goals.position(start, today, nwk)
    prev = _goals.position(start, today - timedelta(days=7), nwk)
    # Last week's load is evidence of a missed week only if last week was a load week. The
    # sign-up baseline week is light on purpose (tests plus easy), but position() reads it
    # as the lead-in, i.e. block 1 week 1 "load".
    prev_load = prev["kind"] == "load" and not (
        slug and _baseline.blocks_week(slug, _monday(today) - timedelta(days=7)))
    kind, why = pos["kind"], ""
    # This week's goal test (from the same start as `pos`), plus any hand-booked tests /
    # PB attempts that fall this week.
    gtest = gd.get("test")
    focus = gd.get("sport")
    test = (_goal_test_booking(gtest, pos) if gtest and kind == "test"
            and (not focus or focus in _goals.sports_for(cfg, profile)) else None)
    books = [b for b in week_bookings(cfg, today) if not b.get("goal_test")] + (
        [test] if test else [])
    out = {"phase": "goal", "week_type": "goal", "goal": g["type"],
           "goal_label": gd["label"], "goal_block": pos["block"], "goal_week": pos["week"],
           "goal_week_kind": kind, "goal_distribution": _goals.distribution(cfg, profile),
           "goal_sports": _goals.sports_for(cfg, profile),
           "training_week": pos["training_week"], "ctl_today": ctl_today,
           "needs_next_race": False, "bookings": books}
    if not ctl_today:
        out.update({"required_weekly_tss": None, "recommended_weekly_tss": None,
                    "weekly_tss_floor": 0,
                    "note": _goals.week_note(g, pos, kind, "?", 0, 0) + (
                        " No Fitness (CTL) is available yet, so there is no load target: "
                        "plan from their availability, mostly easy.")})
        return out
    ctl = float(ctl_today)
    maint = int(round(7.0 * ctl))
    out["maintenance_weekly_tss"] = maint
    if (kind == "load" and prev_load and last_week_tss is not None
            and float(last_week_tss) < _MISS_TRIGGER * maint):
        kind, why = "easy", (f"last week's executed load ({int(last_week_tss)} TSS) was under "
                             f"{int(_MISS_TRIGGER * 100)}% of maintenance")
    max_ramp = float(cfg.get("max_ctl_ramp_per_week") or _goals.DEFAULT_RAMP_CAP)
    lo_hi = _goals.band(cfg, ctl) if g["type"] == "maintain" else None
    if kind in ("easy", "test"):
        target = int(round(maint * gd["easy_factor"]))
        floor = 0
        out["week_type"] = "deload"          # a down-week everywhere DOWN_WEEK_TYPES is read
        out["goal_easy"] = True
    else:
        if lo_hi:
            mid = (lo_hi[0] + lo_hi[1]) / 2.0
            target = compute_required_tss(ctl, mid, _MAINTENANCE_CONVERGE_WEEKS)
            target = min(target, compute_required_tss(ctl, ctl + max_ramp, 1))
            floor = min(target, compute_required_tss(ctl, lo_hi[0], _MAINTENANCE_CONVERGE_WEEKS))
            out.update({"ctl_band": list(lo_hi), "in_band": lo_hi[0] <= ctl <= lo_hi[1]})
        else:
            ramp = min(float(gd["ramp"]), max_ramp)
            target = compute_required_tss(ctl, ctl + ramp, 1)
            floor = maint
            out["goal_ramp_per_week"] = ramp
        if (last_week_tss is not None and prev_load
                and float(last_week_tss) <= _DEFACTO_DELOAD_AT * maint):
            step = max(maint, int(round(float(last_week_tss) * _RETURN_STEP)))
            if step < target:
                out.update({"uncapped_weekly_tss": target, "return_step_cap": step})
                why = (f"RETURN TO LOAD: last week executed {int(last_week_tss)} TSS, at or "
                       f"below maintenance, so step to ~{step} rather than ~{target}.")
                target = step
        floor = min(floor, target)
    out.update({"goal_week_kind": kind, "required_weekly_tss": target,
                "recommended_weekly_tss": target, "weekly_tss_floor": floor,
                "note": _goals.week_note(g, pos, kind, target, floor, ctl, why=why,
                                         band_lo_hi=lo_hi,
                                         test_name=(test or {}).get("name"))})
    return out


# Chat side of the hold. Injected into the athlete's system prompt by lib/engine.py (same
# route as planning_pause), because the scheduled scripts can read the flag but only the
# bot hears the athlete say "ready".
_HOLD_PROMPT = (
    "POST-RACE RECOVERY MODE is ON for {name} (race {race}). Every plan stays easy aerobic "
    "only, with no quality and no bricks, until {name} says they are ready. Weeks 1-3 after "
    "the race stay recovery regardless. When {name} says they are ready to train again "
    "(for example 'ready' or 'start the off-season'), FIRST run `python3 "
    "ClaudeCoach/lib/plan_tools.py post-race-ready --athlete {slug}`, then confirm in one "
    "or two lines: their {next_block} starts on the block_starts date in its output, and "
    "name every entry in bookings_to_redate as needing a new date. Never set it from your "
    "own read of how they feel, and never judge a recovery week's load against a target."
)


def recovery_hold_prompt_block(slug: str, first_name: str = "", path=None,
                               today=None) -> str:
    """The system-prompt block for an athlete in a post-race recovery hold; '' otherwise."""
    try:
        cfg = (json.loads(Path(path or ATHLETES_CONFIG).read_text()) or {}).get(slug) or {}
        today = today or date.today()
        race_s, race_d = recovery_race(cfg, today)
    except Exception:
        return ""
    if not race_d or not post_race_hold_active(cfg, race_d, as_of=today):
        return ""
    return _HOLD_PROMPT.format(
        name=first_name or slug.title(), race=race_s, slug=slug,
        next_block="off-season block" if offseason_cfg(cfg) else "normal training")


def maintenance_ctl(cfg: dict):
    """(ctl, source) for the athlete's off-season CTL target.

    source is "configured" (ctl_targets.maintenance_ctl or a top-level
    maintenance_ctl), "derived" (a provisional _MAINTENANCE_FRACTION of their peak
    phase CTL, or of race_min), or None when there is no basis for either.
    """
    ct = cfg.get("ctl_targets") or {}
    band = maintenance_band(cfg)
    if band:
        return (band[0] + band[1]) / 2.0, "configured"
    v = ct.get("maintenance_ctl", cfg.get("maintenance_ctl"))
    if v is not None:
        try:
            return float(v), "configured"
        except (TypeError, ValueError):
            pass
    basis = (ct.get("phase_ctl") or {}).get("peak") or ct.get("race_min")
    if basis:
        try:
            return round(float(basis) * _MAINTENANCE_FRACTION), "derived"
        except (TypeError, ValueError):
            pass
    return None, None


def maintenance_band(cfg: dict):
    """(lo, hi) from ctl_targets.maintenance_ctl_band, or None when not configured.

    A band rather than one number (Jamie, 24 Sep 2026: "keep fitness between 65-75 in
    the off season"). The weekly load still aims at ONE number, the band's midpoint,
    because two edges give the generator nothing to aim at; the edges set the floor
    and are what the note tells the coach to hold.
    """
    raw = (cfg.get("ctl_targets") or {}).get("maintenance_ctl_band")
    try:
        lo, hi = float(raw[0]), float(raw[1])
    except (TypeError, ValueError, IndexError, KeyError):
        return None
    return (lo, hi) if 0 < lo < hi else None


# OFF-SEASON BLOCK (24 Sep 2026). Without an `offseason` block in athletes.json, week 4
# onward after the race stays the maintenance hold above: a down-week, easy, "one quality
# touch a week at most, no progression". With one, it is a TRAINING block that keeps
# Fitness inside the maintenance band while working on something other than volume
# (Jamie: power and speed, run PBs, FTP and CSS up). It is not a down-week, so the
# quality prescription, the quality injector and the zone floors all switch back on.
#
# Default per-sport split for a power/speed off-season: polarised, with a bigger Z4-5
# share than any Ironman phase because the volume is lower and the point is top-end.
# Overridable per athlete as offseason.distribution.
# Swim: CSS (IF ~1.0) is bucketed Z4-5 by stage1's classifier (>= 0.90), so the CSS focus
# lives in the Z4-5 share here, not in a "Z3–4" middle bucket that the classifier never fills.
OFFSEASON_DISTRIBUTION = {
    "Swim": "65% Z1–2 / 10% Z3 / 25% Z4–5",
    "Bike": "70% Z1–2 / 12% Z3 / 18% Z4–5",
    "Run":  "75% Z1–2 / 10% Z3 / 15% Z4–5",
}


def offseason_cfg(cfg: dict):
    """The athlete's `offseason` block, or None when they have not opted in."""
    oc = cfg.get("offseason")
    return oc if isinstance(oc, dict) and oc else None


def offseason_bookings(cfg: dict, week_start) -> list:
    """The booked sessions (tests, PB attempts) that fall in the week of `week_start`.

    A booking carries either an exact `date` (a parkrun is a Saturday) or a
    `week_start` (a test that may go on any legal day that week). Returned sorted, in
    the week's own dates, so the brief and the validator read the same list.
    """
    ws = _monday(week_start if isinstance(week_start, date)
                 else date.fromisoformat(str(week_start)[:10]))
    we = ws + timedelta(days=6)
    out = []
    for b in all_bookings(cfg):
        if not isinstance(b, dict):
            continue
        try:
            if b.get("date"):
                d = date.fromisoformat(str(b["date"])[:10])
                if ws <= d <= we:
                    out.append(dict(b, date=d.isoformat()))
            elif b.get("week_start"):
                if _monday(date.fromisoformat(str(b["week_start"])[:10])) == ws:
                    out.append(dict(b, week_start=ws.isoformat()))
        except ValueError:
            continue
    return sorted(out, key=lambda b: b.get("date") or b.get("week_start"))


# Bookings are not off-season-only any more; this is the name new callers should use.
week_bookings = offseason_bookings


# Easy pace the engine uses to turn run minutes into km (stage1's audit and caps use the
# same 5.3 min/km), so a distance floor and the mileage arithmetic cannot disagree.
EASY_RUN_PACE_MIN_PER_KM = 5.3


def min_run_minutes(cfg: dict):
    """The athlete's shortest allowed EASY run, in minutes, or None when unset.

    From run_protocol.min_run_km (Jamie, 27 Sep 2026: "5k is the min run length") at
    EASY_RUN_PACE_MIN_PER_KM, or run_protocol.min_run_min if stated in minutes. A run
    below it is dropped rather than shrunk; one proposed below it is lengthened to it.
    """
    rp = cfg.get("run_protocol") or {}
    try:
        if rp.get("min_run_km"):
            return int(-(-float(rp["min_run_km"]) * EASY_RUN_PACE_MIN_PER_KM // 1))
        if rp.get("min_run_min"):
            return int(rp["min_run_min"])
    except (TypeError, ValueError):
        pass
    return None


def _book_bucket(sport) -> str:
    s = str(sport or "").lower()
    for bucket, keys in (("bike", ("bike", "ride")), ("run", ("run",)), ("swim", ("swim",))):
        if any(k in s for k in keys):
            return bucket
    return s


def booking_matches(booking: dict, session: dict) -> bool:
    """Is `session` (a proposal or built session) the booked one?

    Same sport, on the booked date when there is one, and carrying the booking's
    `match` token (e.g. "FTP", "5k") in its name when one is set. One definition,
    shared by the stage-1 validator and the quality injector, so the session the
    validator insists on is the same one the injector keeps its hands off.
    """
    if _book_bucket(session.get("sport")) != _book_bucket(booking.get("sport")):
        return False
    if booking.get("date") and str(session.get("date") or "")[:10] != booking["date"]:
        return False
    token = str(booking.get("match") or "").strip().lower()
    if not token and not booking.get("date"):
        token = str(booking.get("name") or "").strip().lower()
    return (not token) or token in str(session.get("name") or "").lower()


# Down-week placement is a BLOCK decision, not a counter (macro projection, 27 Jul
# 2026: Kathryn's cadence deload landed on week 16 of 18, leaving only two loading
# weeks between the unload and the taper while she was projected 1.6 CTL short of
# race_min). A deload abutting the taper unloads twice into race day: the taper IS
# the unload. So a deload must leave MORE than this many loading weeks before the
# taper; one that does not is shifted EARLIER. The count is preserved — recovery is
# not optional, only its position is negotiable.
# macro_projection imports this constant rather than restating 2, so the flag and
# the placement rule cannot drift apart.
LATE_LOADING_WINDOW = 2


def _phase_ends(cfg: dict) -> dict:
    """Last training week of each phase, from cfg. Shared by required_tss and
    block_deload_weeks so phase boundaries have one definition."""
    ptss = cfg.get("phase_tss") or {}
    build_end = ptss.get("build_end_week", 10)
    return {"base": ptss.get("base_end_week", 6), "build": build_end,
            # No Specific phase unless configured: the old default (14) could sit
            # ABOVE a configured peak_end_week, swallowing the taper — Calum's
            # race week resolved to "specific" instead of taper.
            "specific": ptss.get("specific_end_week", build_end),
            "peak": ptss.get("peak_end_week", 17)}


def _week_monday(plan_start: date, week: int) -> date:
    """Monday of training week `week` (weeks are counted from plan_start in 7-day
    strides, exactly as required_tss counts them, then snapped to that week's
    Monday so it keys the same way as deload_skip_weeks / manual_easy_weeks)."""
    return _monday(plan_start + timedelta(days=7 * (week - 1)))


def block_deload_weeks(cfg: dict) -> dict:
    """Pure: block-aware deload placement for the WHOLE plan, from cfg alone.

    Returns {"n": int, "window": int, "cadence": [wk], "weeks": {wk: reason},
             "moves": [{"from": wk, "to": wk}], "unmoved_late": [wk]}.

    Starts from the every-Nth-week cadence (`deload_every_n_weeks`, honouring
    `deload_skip_weeks`), then REPOSITIONS — never deletes — any deload sitting
    inside the final LATE_LOADING_WINDOW loading weeks before the taper. The
    destination is the latest earlier week that is free (not another down-week,
    not a skip week, not a manual easy week), not adjacent to another down-week,
    far enough from the taper itself, and WITHIN ONE CADENCE PERIOD of the week it
    came from — a deload dragged further than that stops being the same block's
    recovery and starts oscillating load/recover week about. If no such week
    exists the deload stays put and is reported in `unmoved_late`: a block that
    cannot be repaired is reported, not silently stripped of its recovery.

    NOTHING here removes a down-week. Where a late deload cannot be relocated —
    including the case where the week beside it is already a declared
    `manual_easy_weeks` down-week, so the block would unload for a fortnight — it
    stays and is reported. Dropping it might well be the right call there (it is
    what Jamie's hand-written `deload_skip_weeks` did on 16 Jul 2026), but that is a
    coaching judgement to make per athlete through that same override, not a rule
    that quietly deletes recovery.

    Depends on cfg ONLY (never on `today`), so a week's classification is stable
    for the whole block: the same week reads the same way in the Sunday build, in
    the audit, in the projection, and in required_tss's own `today - 7` lookback.
    """
    n = int(cfg.get("deload_every_n_weeks", _DELOAD_EVERY_N) or 0)
    out = {"n": n, "window": LATE_LOADING_WINDOW, "cadence": [], "weeks": {},
           "moves": [], "unmoved_late": []}
    if not n or not cfg.get("plan_start"):
        return out
    plan_start = date.fromisoformat(cfg["plan_start"])
    last = int(_phase_ends(cfg)["peak"])            # final training week; taper follows
    skips = set(cfg.get("deload_skip_weeks") or [])
    easy = set()
    for ew in (cfg.get("manual_easy_weeks") or []):
        easy.add(ew if isinstance(ew, str) else ew.get("week_start"))

    def monday(w: int) -> str:
        return _week_monday(plan_start, w).isoformat()

    # Manual easy weeks are down-weeks too: they must not be counted as loading
    # weeks before the taper, and must not be chosen as a deload destination.
    easy_wks = {w for w in range(1, last + 1) if monday(w) in easy}
    cadence = [w for w in range(1, last + 1)
               if w % n == 0 and monday(w) not in skips]
    out["cadence"] = list(cadence)

    def loading_after(w: int, downs: set) -> int:
        return len([x for x in range(w + 1, last + 1)
                    if x not in downs and x not in easy_wks])

    downs = set(cadence)
    for d in sorted(cadence, reverse=True):
        if d not in downs or loading_after(d, downs) > LATE_LOADING_WINDOW:
            continue
        dest = None
        # never drag a deload further than one cadence period from its own block
        for e in range(d - 1, max(1, d - n), -1):   # week 1 is never a deload
            if e in downs or e in easy_wks or monday(e) in skips:
                continue
            others = (downs - {d}) | easy_wks
            if (e - 1) in others or (e + 1) in others:
                continue                            # no back-to-back down-weeks
            if loading_after(e, (downs - {d}) | {e}) <= LATE_LOADING_WINDOW:
                continue                            # still too close to the taper
            dest = e
            break
        if dest is None:
            out["unmoved_late"].append(d)
            continue
        downs = (downs - {d}) | {dest}
        out["moves"].append({"from": d, "to": dest})

    moved_to = {m["to"]: m["from"] for m in out["moves"]}
    for w in sorted(downs):
        if w in moved_to:
            out["weeks"][w] = (
                f"scheduled deload (block-placed week {w}, moved earlier from cadence "
                f"week {moved_to[w]}: a deload there would leave only "
                f"{LATE_LOADING_WINDOW} loading week(s) before the taper, unloading "
                f"twice into race day)")
        else:
            out["weeks"][w] = f"scheduled deload (every {n}th training week; week {w})"
    return out


def sport_ctl_cached(slug: str | None, max_age_days: int = 2):
    """{"run", "ride", "swim"} Fitness from the dashboard's per-sport cache
    (refresh-site-data, every 15 min), or None when missing or stale. No API call."""
    if not slug:
        return None
    try:
        d = json.loads((BASE / "athletes" / slug / "fitness-bysport-cache.json").read_text())
        if (date.today() - date.fromisoformat(str(d.get("date"))[:10])).days > max_age_days:
            return None
        cur = d.get("current") or {}
        return {k.lower(): float((cur.get(k) or [[None, 0.0]])[-1][1])
                for k in ("Run", "Ride", "Swim")}
    except Exception:
        return None


# RUN-LED WEEKS (Jamie, 1 Oct 2026: "the planning could end up pushing me to train more on
# the bike when it's not relevant"). For a RUN race the total-Fitness line is the wrong
# thing to chase: the gap between it and what running can safely grow by gets filled with
# bike. So the week is built from RUNNING: enough run load to reach the level's running
# target (race_fitness, half of cycling credited), grown at most 15% a week over the
# load that holds current running Fitness (the +10-15% run rule in load terms), and bike /
# swim only top the week up to keep TOTAL Fitness at the athlete's own floor. Total phase
# targets become that floor, never a target the week chases.
_RUN_LOAD_GROWTH = 1.15


def _run_led(cfg: dict, profile: dict | None, slug: str | None, phase: str,
             ctl_today: float, weeks_remaining: int):
    ev = rf.event_def(cfg, cfg.get("race_distance") or cfg.get("race_name"))
    if not ev or ev.get("kind") != "run":
        return None
    sc = sport_ctl_cached(slug)
    if not sc:
        return None
    if profile is None:
        try:
            profile = json.loads((BASE / "athletes" / slug / "profile.json").read_text())
        except Exception:
            profile = {}
    lv = rf.athlete_level(cfg, profile, ev, fitness=sc["run"])
    has_specific = "specific_end_week" in (cfg.get("phase_tss") or {})
    nxt = {"base": "build", "build": "specific" if has_specific else "peak",
           "specific": "peak", "peak": "taper"}.get(phase, "taper")
    rng = rf.fitness_range(ev, lv["level"], nxt)
    if not rng:
        return None
    credit = sc["ride"] * rf.BIKE_CREDIT
    run_target = max((rng[0] + rng[1]) / 2 - credit, rng[0] * rf.MIN_RUN_SHARE)
    hold_run = int(round(7 * sc["run"]))
    w_run = compute_required_tss(sc["run"], run_target, weeks_remaining)
    w_run = max(hold_run, min(w_run, int(round(hold_run * _RUN_LOAD_GROWTH))))
    tfloor = rf.total_floor(cfg)
    w_floor = (compute_required_tss(float(ctl_today), float(tfloor), _MAINTENANCE_CONVERGE_WEEKS)
               if tfloor else 0)
    w_cross = max(0, w_floor - w_run)
    rec = w_run + w_cross
    return {
        "run_led": True, "run_weekly_tss": w_run, "cross_training_tss": w_cross,
        "running_ctl": round(sc["run"], 1), "running_target_ctl": round(run_target, 1),
        "bike_credit": round(credit, 1), "total_floor": tfloor,
        "recommended_weekly_tss": rec, "required_weekly_tss": rec,
        "weekly_tss_floor": min(rec, int(round(max(0.9 * w_run, 0.9 * w_floor)))),
        "note": (f"RUN-LED WEEK ({ev.get('label')}, level {lv['level']} {lv['label']}): "
                 f"~{w_run} of the week's load is RUNNING (running Fitness {sc['run']:g} "
                 f"toward {run_target:.0f} by the end of {phase}, growing at most "
                 f"{int((_RUN_LOAD_GROWTH - 1) * 100)}% a week); bike / swim ~{w_cross} "
                 f"only, to keep total Fitness at the athlete's floor"
                 + (f" ({tfloor:g})" if tfloor else "") + ". Never add bike to make up "
                 "running the caps will not allow - a short week is better.")}


# The sign-up baseline week: the tests plus easy training by feel. A down-week's load,
# but not a rest week - Jamie: a 0 floor is wrong (4 Oct 2026). Half of maintenance keeps
# Fitness from sliding while the tests are still done fresh.
_BASELINE_TARGET = 0.70
_BASELINE_FLOOR = 0.50


def _baseline_week(ctl_today) -> dict:
    out = {"phase": "baseline", "week_type": "baseline", "ctl_today": ctl_today,
           "needs_next_race": False, "bookings": []}
    if not ctl_today:
        out.update({"required_weekly_tss": None, "recommended_weekly_tss": None,
                    "weekly_tss_floor": None,
                    "note": ("BASELINE WEEK: the sign-up tests plus easy training by feel. "
                             "No Fitness (CTL) is available yet, so there is no load target.")})
        return out
    maint = int(round(7.0 * float(ctl_today)))
    target, floor = int(round(maint * _BASELINE_TARGET)), int(round(maint * _BASELINE_FLOOR))
    out.update({"maintenance_weekly_tss": maint, "required_weekly_tss": target,
                "recommended_weekly_tss": target, "weekly_tss_floor": floor,
                "note": (f"BASELINE WEEK: the sign-up tests plus easy training by feel. "
                         f"Aim for ~{target} TSS, not under ~{floor} (half of maintenance, "
                         f"so Fitness holds while the tests are done fresh).")})
    return out


def required_tss(cfg: dict, ctl_today: float, today: date | None = None,
                 last_week_tss: float | None = None,
                 profile: dict | None = None, slug: str | None = None) -> dict:
    """Pure: weekly TSS needed to hit the current phase's CTL target on time,
    plus the ramp-capped safe ceiling — with deload and taper branches. Returns
    {"error": ...} if the athlete has no defensible CTL basis (no fabricated
    targets — mirrors generate-plan.py). Shared by the CLI, the weekly brief,
    the plan audit and prefetch_context so every path agrees.

    last_week_tss (optional): last completed week's ACTUAL total load; when the
    athlete executed under 70% of prescription, this week becomes a recovery
    week (blueprint: "missed >30% -> next week is recovery")."""
    today = today or date.today()
    # SIGN-UP BASELINE WEEK (lib/baseline.py). Checked first: the week is built by
    # baseline.schedule, not by phase logic, and without this branch it fell through to a
    # goal week, was demoted to "easy" off James's post-Ironman week and the coach told him
    # "the floor for this baseline week is 0" (4 Oct 2026).
    if slug and _baseline.blocks_week(slug, _monday(today)):
        return _baseline_week(ctl_today)
    # GOAL BLOCK (lib/goals.py): no A race ahead and a goal set, so the week is planned
    # toward the goal. Checked first: such an athlete has no race CTL targets at all.
    if goal_active(cfg, today):
        return _goal_week(cfg, ctl_today, today, last_week_tss=last_week_tss, slug=slug,
                          profile=profile)
    ctl_targets = cfg.get("ctl_targets") or {}
    phase_ctl = ctl_targets.get("phase_ctl") or {}
    if not phase_ctl and not ctl_targets.get("race_min"):
        return {"error": "no CTL target configured — plan from availability, not a TSS target"}
    if not cfg.get("plan_start"):
        return {"error": "no plan_start configured"}

    plan_start = date.fromisoformat(cfg["plan_start"])
    ends = _phase_ends(cfg)
    week_now = max(1, (today - plan_start).days // 7 + 1)
    phase = next((p for p in _PHASES if week_now <= ends[p]), "taper")

    # POST-RACE first: a race in the PAST is never a taper (see _TRANSITION_FACTORS).
    # Checked ahead of the phase branches, not inside the taper one, so it also catches a
    # stale plan_start that leaves week_now inside 'peak' after race day.
    race_s, race_d = recovery_race(cfg, today)
    if race_d:
        days_since = (today - race_d).days
        weeks_since = days_since // 7 + 1
        maint = 7.0 * float(ctl_today or 0)      # the load that HOLDS current CTL
        stale = weeks_since > max(_TRANSITION_FACTORS)
        mct, mct_source = maintenance_ctl(cfg)
        f = _TRANSITION_FACTORS.get(weeks_since)
        # Past week 3 with the athlete not yet ready: stay at the week-3 recovery level.
        hold = stale and post_race_hold_active(cfg, race_d, as_of=today)
        if hold:
            f = _TRANSITION_FACTORS[max(_TRANSITION_FACTORS)]
        if not stale or hold:
            # Weeks 1-3 (and a held week 4+): recovery. A fraction of maintenance, by design.
            target = int(round(maint * f)) if maint else None
        elif not maint:
            target = None
        elif mct is not None:
            # Off-season: converge on the configured maintenance CTL, ramp-capped.
            target = compute_required_tss(float(ctl_today), mct,
                                          _MAINTENANCE_CONVERGE_WEEKS)
            max_ramp = cfg.get("max_ctl_ramp_per_week")
            if max_ramp:
                target = min(target, compute_required_tss(
                    float(ctl_today), float(ctl_today) + float(max_ramp), 1))
        else:
            # Maintenance CTL not configured (TBC): hold where they are, and say so.
            target = int(round(maint))
        out = {"phase": "transition", "week_type": "post_race",
               "training_week": week_now, "ctl_today": ctl_today,
               "race_date": race_s, "days_since_race": days_since,
               "weeks_since_race": weeks_since, "transition_factor": f,
               "weekly_tss_floor": 0,          # unloading is the point; no under-training floor
               "needs_next_race": stale and not hold,
               "recovery_hold": hold,
               # From week 3, tell a held athlete how to end it (the weekly message).
               "ready_prompt": bool(post_race_hold_active(cfg, race_d, as_of=today)
                                    and weeks_since >= max(_TRANSITION_FACTORS)),
               "next_block": ("off-season block" if offseason_cfg(cfg)
                              else "normal training"),
               "held_bookings": offseason_bookings(cfg, today) if hold else [],
               "maintenance_ctl": mct,
               "maintenance_ctl_source": mct_source,
               "needs_maintenance_target": bool(stale and not hold and mct_source != "configured"),
               "maintenance_weekly_tss": int(round(maint)) if maint else None,
               "required_weekly_tss": target, "recommended_weekly_tss": target}
        oc = offseason_cfg(cfg)
        band = maintenance_band(cfg)
        if stale and oc and target is not None and not hold:
            # OFF-SEASON BLOCK: a training week, not a down-week (see OFFSEASON_DISTRIBUTION).
            # Floor = the load that would take CTL to the band's bottom edge over the same
            # convergence window, so a week under it is heading out of the band.
            floor = 0
            if band:
                floor = min(target, compute_required_tss(
                    float(ctl_today), band[0], _MAINTENANCE_CONVERGE_WEEKS))
            ow = post_race_block_week(cfg, race_d, weeks_since)
            books = offseason_bookings(cfg, today)
            # A B-priority booking (race_fitness.PRIORITY_RULES) makes its week lighter,
            # the same "3-5 easier days" a B-race gets anywhere else. C does not.
            b_race = next((b for b in books if str(b.get("priority") or "").upper() == "B"), None)
            if b_race:
                fct = rf.PRIORITY_RULES["B"]["race_week_factor"]
                target = int(round(target * fct))
                floor = 0
                out.update({"required_weekly_tss": target, "recommended_weekly_tss": target,
                            "b_race_week": b_race.get("name"), "b_race_factor": fct})
            band_s = (f"between {band[0]:g} and {band[1]:g}" if band
                      else f"near {mct if mct is not None else float(ctl_today):g}")
            book_s = ""
            if books:
                book_s = (" BOOKED THIS WEEK (must be in the plan, on the stated date where "
                          "one is given): " + "; ".join(
                              f"{b.get('name') or b.get('kind')} ({b.get('sport')}, "
                              f"{b.get('date') or 'any legal day'})" for b in books)
                          + ". Each is a MAX effort and counts as that sport's quality for the "
                          "week. Keep the listed easy days (booking_easy_dates) free of hard "
                          "work: a C attempt gets an easy day either side, a B attempt three.")
                if b_race:
                    book_s += (f" This is a B-RACE WEEK ({b_race.get('name')}): load is "
                               f"{int(rf.PRIORITY_RULES['B']['race_week_factor'] * 100)}% of "
                               "a normal off-season week.")
            out.update({
                "phase": "offseason", "week_type": "offseason",
                "offseason_week": ow, "ctl_band": list(band) if band else None,
                "in_band": (band[0] <= float(ctl_today) <= band[1]) if band else None,
                "weekly_tss_floor": floor,
                "needs_next_race": False,
                "bookings": books,
            })
            out["note"] = (
                f"OFF-SEASON BLOCK, week {ow} ({days_since} days after {race_s}). Keep "
                f"Fitness (CTL) {band_s} (now {float(ctl_today):g}): prescribe ~{target} "
                f"TSS this week, not under ~{floor}. Focus: "
                f"{oc.get('focus') or 'power and speed'}. This is a TRAINING week, not a "
                "recovery week: 2-3 quality sessions spread across the sports (run speed, "
                "bike FTP/VO2, swim CSS), everything else easy. No protected long ride and "
                "no long-run progression; volume is not the point." + book_s
                + " There is no race countdown: do not reference one.")
            return out
        if target is None:
            out["note"] = ("POST-RACE transition (race was "
                           f"{days_since} days ago) and no CTL available, so no volume "
                           "target could be computed. Prescribe easy aerobic only. This is "
                           "NOT a taper: do not reference an upcoming race or a countdown.")
        elif hold:
            out["note"] = (f"POST-RACE RECOVERY HOLD, week {weeks_since} after {race_s}: the "
                           "athlete has NOT yet said they are ready to train, so recovery "
                           f"continues. Prescribe ~{target} TSS ({int(f * 100)}% of the "
                           f"~{int(round(maint))} TSS maintenance load). Easy aerobic only: "
                           "no VO2, no threshold, no sweetspot, no bricks, no long-session "
                           "progression; frequency and enjoyment over load. The next block "
                           "starts the first week after they say they are ready. Do not "
                           "reference a race countdown.")
            if out["held_bookings"]:
                out["note"] += (" Booked sessions that fall this week are ON HOLD with it "
                                "and must NOT be scheduled: " + "; ".join(
                                    f"{b.get('name') or b.get('kind')} ({b.get('sport')})"
                                    for b in out["held_bookings"]) + ".")
        elif stale and mct is not None:
            _dir = ("hold" if abs(float(ctl_today) - mct) < 1
                    else ("come down to" if float(ctl_today) > mct else "build back to"))
            _prov = ("" if mct_source == "configured" else
                     f" This {mct:g} is PROVISIONAL — derived as "
                     f"{int(_MAINTENANCE_FRACTION * 100)}% of their peak race fitness "
                     "because no maintenance CTL is configured. Ask what they actually want "
                     "to hold between goals and set ctl_targets.maintenance_ctl.")
            out["note"] = (f"OFF-SEASON MAINTENANCE, {days_since} days after {race_s} with no "
                           f"next race configured. Work toward maintenance Fitness (CTL) "
                           f"{mct:g} — {_dir} it over ~{_MAINTENANCE_CONVERGE_WEEKS} weeks: "
                           f"prescribe ~{target} TSS this week (current CTL "
                           f"{float(ctl_today):g}). This is a maintenance target, not a "
                           "block: keep frequency and enjoyment, one quality touch a week at "
                           "most, no progression." + _prov + " Ask for the next race and "
                           "regenerate the blueprint before prescribing a training block "
                           "again. Do not reference a race countdown.")
        elif stale:
            out["note"] = (f"POST-RACE, and the last configured race ({race_s}) was "
                           f"{days_since} days ago with nothing after it. No maintenance "
                           f"Fitness (CTL) target is configured for this athlete, so hold "
                           f"where they are: ~{target} TSS, the load that keeps CTL at "
                           f"{float(ctl_today):g}. Maintain, do NOT build. ASK what "
                           "maintenance fitness they want to hold between goals (set "
                           "ctl_targets.maintenance_ctl) and what the next race is — until "
                           "one of those is answered there is no target to work toward. Do "
                           "not reference a race countdown.")
        else:
            out["note"] = (f"POST-RACE TRANSITION, week {weeks_since} after {race_s}: "
                           f"prescribe ~{target} TSS ({int(f * 100)}% of the "
                           f"~{int(round(maint))} TSS maintenance load). Easy aerobic only "
                           "— no VO2, no threshold, no long-session progression; frequency "
                           "and enjoyment over load. Recovery from the race IS the week's "
                           "work. This is NOT a taper: there is no upcoming race, so never "
                           "mention a countdown or race-week sharpening.")
        return out

    race_s = cfg.get("race_date")
    race_d = date.fromisoformat(race_s) if race_s else None
    if phase == "taper" and race_d and (race_d - today).days > _STALE_TAPER_DAYS:
        # The week count ran off the end of the phases but the race is not close: the
        # plan_start belongs to an earlier race. Never taper on that (29 Sep 2026: a
        # 27-weeks-out marathon read as taper). No target until the block is re-anchored.
        return {"error": f"plan_start {cfg.get('plan_start')} predates the plan for the "
                         f"race on {race_s} ({(race_d - today).days // 7} weeks away): set "
                         "a new plan_start and phase_tss for this block before prescribing "
                         "a load. This is NOT a taper."}

    if phase == "taper":
        # Shaped taper: stepped volume targets so the load checks stay ENGAGED
        # in the most consequential weeks (previously no target -> every audit
        # disengaged and volume was left to LLM discretion).
        if not race_s or not ctl_today:
            missing = "race_date" if not race_s else "ctl_today"
            return {"phase": "taper/race", "ctl_today": ctl_today, "training_week": week_now,
                    "week_type": "taper",
                    "note": f"taper, but no {missing} available — volume target could not "
                            "be computed; step down toward race day, hold intensity"}
        days_to_race = max(0, (race_d - today).days)
        weeks_to_race = max(1, -(-days_to_race // 7))          # ceil
        factor = _TAPER_FACTORS.get(min(weeks_to_race, 3), _TAPER_FACTORS[3])
        pre_taper_weekly = 7.0 * float(ctl_today)
        whole_week = int(round(pre_taper_weekly * factor))
        out = {"phase": "taper", "week_type": "taper", "training_week": week_now,
               "ctl_today": ctl_today, "race_date": race_s,
               "weeks_to_race": weeks_to_race, "taper_factor": factor,
               "weekly_tss_floor": 0,   # taper: unloading is the point
               "required_weekly_tss": whole_week, "recommended_weekly_tss": whole_week,
               "note": (f"TAPER, race in {weeks_to_race} wk: volume stepped to "
                        f"{int(factor * 100)}% of the ~{int(round(pre_taper_weekly))} TSS "
                        f"maintenance load (70/55/40 step-down). Hold INTENSITY — keep "
                        f"sharpness at reduced dose, keep session frequency; cut "
                        f"duration, never intensity. Sharpness means THRESHOLD and above "
                        f"in short doses"
                        + (" — for this event race pace sits at or below the top of Z2, "
                           "so race-pace work is pacing and fuelling rehearsal, not "
                           "intensity, and does not count toward holding sharpness."
                           if race_intensity(cfg, profile) <= _LONG_COURSE_IF else "."))}

        # RACE WEEK: cost the race and prescribe only what is left (see _RACE_WEEK_MIN).
        if days_to_race <= 6:
            rt, rt_src = race_load(cfg, profile)
            floor = int(round(pre_taper_weekly * _RACE_WEEK_MIN))
            training = max(floor, whole_week - rt)
            out.update({
                "week_type": "race",
                "race_in_week": True,
                "race_tss_estimate": rt,
                "race_tss_source": rt_src,
                "whole_week_tss_incl_race": whole_week,
                "training_at_floor": training == floor,
                "required_weekly_tss": training,
                "recommended_weekly_tss": training,
                "note": (
                    f"RACE WEEK — the race is on {race_s} ({days_to_race} day"
                    f"{'' if days_to_race == 1 else 's'} away) and it is the week's load: "
                    f"~{rt} TSS ({rt_src.replace('_', ' ')}). The whole-week taper figure is "
                    f"~{whole_week} TSS, so the TRAINING budget is ~{training} TSS"
                    + (" — the openers floor, because the race alone exceeds the week's "
                       "whole-week figure. " if training == floor else " — what is left "
                       "after the race. ")
                    + ("Keep session FREQUENCY and cut all duration. "
                       + (_OPENERS_LONG
                          if race_intensity(cfg, profile) <= _LONG_COURSE_IF
                          else _OPENERS_SHORT)
                       + " Nothing on race day itself, and the day before is rest or one "
                         "short opener. Do NOT schedule a long ride, a long run, or any "
                         "full quality session.")),
            })
        return out

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
    # and a genuinely missed week (< 70% of MAINTENANCE executed) converts this
    # week to recovery.
    rec = out["recommended_weekly_tss"]
    out["week_type"] = phase
    run_led = _run_led(cfg, profile, slug, phase, ctl_today, weeks_remaining)
    if run_led:
        out.update(run_led)
        rec = run_led["recommended_weekly_tss"]
    maintenance_now = int(round(7 * float(ctl_today))) if ctl_today else None
    surplus_drift = False
    if not run_led and maintenance_now and rec is not None and int(rec) < maintenance_now:
        # FITTER THAN THE GOAL (Jamie, 29 Sep 2026): current Fitness is above this
        # phase's target, so the CTL line asks for LESS than holding it. That used to be
        # prescribed silently (with the floor below then contradicting it at 7 x CTL).
        # It is the athlete's call: hold and shift the mix, let it drift to their floor,
        # or raise the goal. Until they say, hold.
        choice = rf.surplus_choice(cfg)
        tfloor = rf.total_floor(cfg)
        out["fitness_surplus"] = {"ctl": float(ctl_today), "phase_target_ctl": target_ctl,
                                  "total_floor": tfloor, "choice": choice}
        if choice == "drift":
            drift_to = max(float(target_ctl), tfloor or 0.0)
            rec = min(maintenance_now,
                      compute_required_tss(ctl_today, drift_to, weeks_remaining))
            surplus_drift = True
            s_note = (f" FITTER THAN THE GOAL: Fitness {float(ctl_today):g} is above the "
                      f"{phase} target {target_ctl}. The athlete chose to let it drift down "
                      f"toward {drift_to:g} (never below their floor): ~{rec} TSS, with the "
                      "freed time going into speed/quality in the goal sport.")
        else:
            rec = maintenance_now
            if choice == "hold_shift":
                s_note = (f" FITTER THAN THE GOAL: Fitness {float(ctl_today):g} is above the "
                          f"{phase} target {target_ctl}. The athlete chose to HOLD it (~{rec} "
                          "TSS) and shift the mix toward the goal sport: more of the goal "
                          "sport, let the other sports fade (e.g. more running, less swimming).")
            elif choice == "raise_goal":
                s_note = (f" FITTER THAN THE GOAL: Fitness {float(ctl_today):g} is above the "
                          f"{phase} target {target_ctl}. The athlete wants to RAISE THE GOAL: "
                          f"holding (~{rec} TSS) until the new goal and its targets are set.")
            else:
                out["needs_surplus_choice"] = True
                s_note = (f" FITTER THAN THE GOAL: Fitness {float(ctl_today):g} is above the "
                          f"{phase} target {target_ctl}, so holding it this week (~{rec} TSS). "
                          "ASK the athlete which they want: (1) hold total Fitness and shift the "
                          "mix toward the goal sport, (2) let it drift down to their floor"
                          + (f" ({tfloor:g})" if tfloor else "") + " and put the freed time "
                          "into speed, or (3) raise the goal. Record the answer with "
                          "plan_tools.py fitness-choice.")
        out.update({"recommended_weekly_tss": rec,
                    "note": (out.get("note") or "") + s_note})
    # UNDER-TRAINING floor (Jamie, 5 Jul 2026): a training week must at least
    # reach maintenance (7 x CTL) even when the phase-required TSS is lower than
    # that — otherwise a light phase target would silently let the floor sit
    # below maintenance and detrain the athlete. So the floor is the HIGHER of
    # the phase requirement and maintenance (max, NOT min — required_weekly_tss
    # only governs, i.e. is allowed to sit below maintenance, when CTL is
    # genuinely below the phase target and rec is already >= maintenance).
    # Deload/taper weeks overwrite this with 0 (explicitly no floor).
    maintenance = int(round(7 * float(ctl_today))) if ctl_today else None
    out["maintenance_weekly_tss"] = maintenance

    # RETURN-TO-LOAD CAP (see _RETURN_STEP). Applied to the CTL-derived recommendation
    # before the floor is taken off it, so the floor tracks the week actually being
    # prescribed rather than one the athlete was never going to be asked for.
    # Deliberately NOT applied to the deload/easy/taper branches below: those set their
    # own reduced figure and capping a down-week's step-up is meaningless.
    # A prior INTRINSIC down-week (scheduled deload, B-race taper, race week) is light BY
    # DESIGN and must not read as "no build happened". Without this guard the cap fires
    # off every deload and ratchets the return week down to 1.30x the deload itself -
    # Kathryn's 27 Jul went 636 -> 515 off her 20 Jul deload - which defeats the entire
    # point of unloading. This is the same trap, and the same fix, as the miss-trigger
    # branch below; classification is shared so the two cannot disagree about what
    # counts as a planned down-week.
    _prev_type_cache = {}

    def _prev_week_type():
        if "t" not in _prev_type_cache:
            _prev_type_cache["t"] = required_tss(
                cfg, ctl_today, today=today - timedelta(days=7),
                last_week_tss=None).get("week_type")
        return _prev_type_cache["t"]

    last_at_or_below_maint = (
        last_week_tss is not None and maintenance
        and float(last_week_tss) <= _DEFACTO_DELOAD_AT * maintenance
        and _prev_week_type() not in DOWN_WEEK_TYPES)
    if last_at_or_below_maint and rec:
        step_cap = max(int(maintenance), int(round(float(last_week_tss) * _RETURN_STEP)))
        if step_cap < int(rec):
            out.update({
                "uncapped_weekly_tss": int(rec),
                "return_step_cap": step_cap,
                "recommended_weekly_tss": step_cap,
                "note": (f"RETURN TO LOAD: last week executed {int(last_week_tss)} TSS, at or "
                         f"below maintenance (~{maintenance}), so no build happened. The phase "
                         f"line wants ~{int(rec)} this week, which is "
                         f"+{round((int(rec) / max(1.0, float(last_week_tss)) - 1) * 100)}% on "
                         f"what was actually done. Step to ~{step_cap} "
                         f"(+{int((_RETURN_STEP - 1) * 100)}% on last week's actual) instead and "
                         f"rebuild from there; chasing the line off an unloaded week is how a "
                         f"week gets missed outright."),
            })
            rec = out["recommended_weekly_tss"]

    out["weekly_tss_floor"] = (run_led["weekly_tss_floor"] if run_led else
                               ((int(rec) if surplus_drift else max(int(rec), maintenance))
                                if (rec and maintenance) else None))
    # Manual easy-week override (B-race taper etc.): a hand-declared week that the
    # mechanical every-Nth-week cadence doesn't know about. Keyed on the Monday of
    # the week `today` falls in, so it survives regardless of training-week drift.
    # Takes precedence over the scheduled-deload/recovery logic below. Semantics =
    # taper (hold intensity, cut volume, no floor), not accumulation deload.
    week_monday = (today - timedelta(days=today.weekday())).isoformat()
    # B races in the race registry get the B-race week automatically (race_fitness
    # PRIORITY_RULES); a hand-declared manual_easy_weeks entry for the week still wins.
    auto_b = []
    for r in (cfg.get("races") or []):
        try:
            rd = date.fromisoformat(str(r.get("date"))[:10])
        except ValueError:
            continue
        if (str(r.get("priority") or "").upper() == "B"
                and (rd - timedelta(days=rd.weekday())).isoformat() == week_monday):
            auto_b.append({"week_start": week_monday,
                           "reason": f"B race: {r.get('name') or 'race'} on {rd.isoformat()}",
                           "factor": rf.PRIORITY_RULES["B"]["race_week_factor"]})
    for ew in list(cfg.get("manual_easy_weeks") or []) + auto_b:
        if isinstance(ew, str):
            ew = {"week_start": ew}
        if ew.get("week_start") == week_monday:
            ef = float(ew.get("factor", 0.6))
            reason = ew.get("reason", "manually declared easy week")
            out.update({
                "week_type": "taper",
                "easy_week_reason": reason,
                "full_week_tss": rec,
                "weekly_tss_floor": 0,   # easy week: unloading is the point
                "recommended_weekly_tss": int(round(rec * ef)),
                "required_weekly_tss": int(round(rec * ef)),
                "note": (f"EASY WEEK ({reason}): prescribe ~{int(round(rec * ef))} TSS "
                         f"({int(ef * 100)}% of the normal ~{rec}). Hold intensity, cut "
                         f"volume; keep session frequency. This is a planned down-week, "
                         f"not under-training — no floor applies.")})
            return out

    factor = float(cfg.get("deload_factor", _DELOAD_FACTOR))
    deload_why = None
    # Placement is delegated to block_deload_weeks: the cadence proposes, the block
    # decides (28 Jul 2026). The raw `week_now % n` test no longer lives here, and
    # nor does the deload_skip_weeks set — that athlete override (Jamie, 16 Jul
    # 2026: "the cadence doesn't know the actual periodisation") is applied inside
    # the placement function, along with the late-loading-window repair.
    _placement = block_deload_weeks(cfg)
    if week_now in _placement["weeks"]:
        deload_why = _placement["weeks"][week_now]
        _from = next((m["from"] for m in _placement["moves"] if m["to"] == week_now), None)
        if _from is not None:
            out["deload_moved_from_week"] = _from
        # De-facto-deload QUERY (see _DEFACTO_DELOAD_AT). The deload still stands - this
        # only attaches the question, for the Monday brief and the audit to put in front
        # of a human. Two consecutive down-weeks may well be right; what must not happen
        # is it going unremarked because the cadence cannot see what was executed.
        if last_at_or_below_maint:
            out["deload_may_be_redundant"] = {
                "last_week_tss": int(last_week_tss),
                "maintenance_weekly_tss": int(maintenance),
                "question": (
                    f"Scheduled deload this week, but last week executed "
                    f"{int(last_week_tss)} TSS against maintenance ~{int(maintenance)} - "
                    f"at or below it, so that week was already a de facto deload. "
                    f"Deloading again stacks two unchosen down-weeks. Skip this one and "
                    f"load through, or keep it?"),
            }
    # Genuine-miss recovery (fix, 15 Jul 2026): reference the athlete's SUSTAINABLE
    # maintenance load (~7×CTL), NOT 70% of this week's aspirational ramp-capped target.
    # Realistic execution routinely lands under the ramp-capped target, and a PLANNED
    # deload week (prescribed ~62%) sits under it by design — referencing the target made
    # both read as 'missed', firing spurious recovery weeks and cascading a 2nd deload off
    # every scheduled deload (downward ratchet). Only a true collapse (< 70% of
    # maintenance) recovers — and NEVER off a prior INTRINSIC planned down-week.
    elif (last_week_tss is not None and maintenance
          and float(last_week_tss) < _MISS_TRIGGER * maintenance):
        # A scheduled deload, a B-race taper (manual_easy_weeks) or a race week is
        # prescribed light BY DESIGN, so its reduced load must not read as a 'miss' and
        # cascade a spurious recovery the next week (scheduled-deload cascade: 15 Jul;
        # Jamie's Dorney B-race taper tripped this at specific-phase CTL 105: 16 Jul).
        # Classify the prior week by recomputing it — required_tss is pure and the inner
        # call passes last_week_tss=None, so it skips THIS branch (bounded recursion).
        # This also subsumes the old scheduled-deload arithmetic guard (prev type=deload).
        if _prev_week_type() not in DOWN_WEEK_TYPES:
            deload_why = (f"recovery week: last week's executed load "
                          f"({int(last_week_tss)} TSS) was under {int(_MISS_TRIGGER * 100)}% "
                          f"of maintenance (~{int(_MISS_TRIGGER * maintenance)})")
    if deload_why:
        out.update({
            "week_type": "deload",
            "deload_reason": deload_why,
            "full_week_tss": rec,
            "weekly_tss_floor": 0,   # deload: unloading is the point
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
    lo_d = monday - timedelta(days=7)
    hi_d = monday - timedelta(days=1)
    lo = lo_d.isoformat()
    hi = hi_d.isoformat()
    # The window must actually be OVER. Building a week EARLY makes the lookback the week
    # currently in progress, and a barely-started week reads as a collapse: a dry run on
    # Monday 10 Aug 2026 for w/c 17 Aug summed 10-16 Aug at 33 TSS, tripped _MISS_TRIGGER
    # (< 70% of the 489 maintenance), and turned a PEAK week into a 303 TSS recovery week
    # (489 x 0.62) for an athlete whose deload had been deliberately removed. Returning
    # None means "unknown", which the miss-trigger already treats as do-not-fire, so an
    # off-cadence rebuild simply gets no recovery conversion rather than a wrong one.
    #
    # `hi_d == date.today()` is ALLOWED, and must be: the Sunday 18:00 cron builds the
    # following Monday, so its window ends on that same Sunday. Rejecting it would
    # disable the trigger on the one cadence that actually uses it. Only a window
    # reaching into the FUTURE is refused.
    if hi_d > date.today():
        return None
    try:
        # get_training_history(days=N) counts back from the REAL today, not `today`, so
        # the lookback must be anchored there — otherwise evaluating a past week_start
        # truncates the window and silently under-sums it into a false deload trigger
        # (8 Aug 2026: --date 2026-08-03 read 495 for a 929 TSS week).
        anchor = max(today, date.today())
        hist = client.get_training_history(days=(anchor - (monday - timedelta(days=7))).days + 1)
        return round(sum(float(a.get("icu_training_load") or 0) for a in hist or []
                         if lo <= (a.get("start_date_local") or "")[:10] <= hi), 1)
    except Exception:
        return None


_RUN_CAP_PACE_MIN_PER_KM = 5.3  # convert a km floor to a minute floor (matches stage1 PACE)


def run_caps(client, today: date | None = None, run_protocol: dict | None = None) -> dict:
    """Run-volume MAX ceilings from the last 4 completed weeks. Floors + ramp are
    PER-ATHLETE from run_protocol (defaults preserve prior behaviour): weekly_km_floor
    (default 25), long_run_km_floor (default none), increase_pct (default +10% weekly /
    +15% long run). The floor is a BASE that gets ramped - cap = max(recent-4wk-max,
    floor) x (1 + increase_pct) - so a big week ageing out of the window cannot make the
    cap sag below the athlete's established base (Kathryn's 31 km). Minute floors are
    derived from the km floor via one pace, so the km and minute caps cannot diverge.
    Returns km caps (weekly brief) AND minute caps (validate_week). All-None on fetch
    failure - callers must surface a skipped check, never a silent pass (audit P1-9)."""
    today = today or date.today()
    rp = run_protocol or {}
    wk_km_floor = float(rp.get("weekly_km_floor", 25.0))
    _lr_km_floor = rp.get("long_run_km_floor")
    lr_min_floor = float(_lr_km_floor) * _RUN_CAP_PACE_MIN_PER_KM if _lr_km_floor is not None else 0.0
    _inc = rp.get("increase_pct")
    wk_mult = (1 + float(_inc) / 100) if _inc is not None else 1.10
    lr_mult = (1 + float(_inc) / 100) if _inc is not None else 1.15
    try:
        wk_km, wk_min, wk_long = {}, {}, {}
        for a in client.get_training_history(days=35) or []:
            if (a.get("type") or "") not in ("Run", "TrailRun", "VirtualRun"):
                continue
            d = date.fromisoformat((a.get("start_date_local") or "")[:10])
            iso = d.isocalendar()[:2]
            wk_km[iso] = wk_km.get(iso, 0.0) + (a.get("distance") or 0) / 1000
            m = (a.get("moving_time") or 0) / 60
            wk_min[iso] = wk_min.get(iso, 0.0) + m
            wk_long[iso] = max(wk_long.get(iso, 0.0), m)
        cur = today.isocalendar()[:2]
        weeks = [k for k in sorted(wk_km) if k != cur][-4:]
        if not weeks:
            return {"weekly_km_cap": None, "weekly_min_cap": None, "long_run_min_cap": None}
        rmax_km = max(wk_km[k] for k in weeks)
        rmax_min = max(wk_min[k] for k in weeks)
        rmax_long = max(wk_long[k] for k in weeks)
        return {
            # floor is a BASE that gets ramped (max(recent, floor) x mult), not a floor on
            # the result - so an aged-out big week cannot sag the cap below the base.
            "weekly_km_cap": round(max(rmax_km, wk_km_floor) * wk_mult, 1),
            "weekly_min_cap": round(max(rmax_min, wk_km_floor * _RUN_CAP_PACE_MIN_PER_KM) * wk_mult),
            "long_run_min_cap": round(max(rmax_long, lr_min_floor) * lr_mult),
        }
    except Exception:
        return {"weekly_km_cap": None, "weekly_min_cap": None, "long_run_min_cap": None}


def cmd_required_tss(args) -> dict:
    cfg = _load_cfg(args.athlete)
    client = _client(cfg)
    ctl_today = args.ctl_today
    if ctl_today is None:
        w = client.get_wellness(days=3)
        if not w:
            raise SystemExit(_err("no wellness data for current CTL; pass --ctl-today"))
        ctl_today = round(float(w[-1].get("ctl") or 0), 1)
    eval_day = date.fromisoformat(args.date) if getattr(args, "date", None) else None
    return {"athlete": args.athlete,
            **required_tss(cfg, ctl_today, today=eval_day, slug=args.athlete,
                           last_week_tss=last_week_actual_tss(client, today=eval_day))}


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
    cap, intensity distribution). The weekly_tss_cap is a hard upper bound, distinct
    from the required-tss target, which is a floor.

    The cap is resolved by plan_builder._weekly_tss_cap — the SAME function the
    Sunday build and the 06:25 audit use — so this CLI cannot disagree with them
    about where the limit is. It used to hold an inline copy of the hours maths
    reading profile.max_hours_per_week directly, which made it blind to a week the
    athlete had declared hours for and so reported a stale, LOWER cap."""
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
               "moving_time": (s.get("minutes") or 0) * 60 or None,
               "load_target": s.get("tss") if s.get("tss") is not None else s.get("load_target")}
              for s in week]
    week_start = _monday(min(date.fromisoformat(e["start_date_local"][:10]) for e in events))

    ctl_today = args.ctl_today
    if ctl_today is None:
        w = _client(cfg).get_wellness(days=3)
        ctl_today = round(float(w[-1].get("ctl") or 0), 1) if w else None

    day_rules = cfg.get("day_rules")
    phase = current_phase(_load_blueprint(args.athlete), week_start) or {}
    # ONE cap resolver, not a second copy of the hours maths. Imported inside the
    # function because plan_builder defers its own import of this module (see
    # plan_builder.py:237) — a module-level import here would couple the two at
    # import time for no gain.
    #
    # `week_start` is passed, and that is the whole point: it makes this CLI
    # declaration-aware. It also brings one deliberate behaviour change — an athlete
    # with NO profile.max_hours_per_week (Kathryn, by the permanent 10 Jul 2026 rule)
    # previously got tss_cap = None here and the load check was reported SKIPPED;
    # she now gets the blueprint phase's own tss_ceiling and the check is ARMED. That
    # is the same arming decision plan_builder already made, and a phase ceiling is a
    # LOAD bound, not a reinstated limit on how long she may train.
    tss_cap = None
    try:
        from plan_builder import _weekly_tss_cap
        tss_cap = _weekly_tss_cap(args.athlete, phase, week_start=week_start)
    except Exception:
        pass
    # Under-training floor for the week being validated (0 = deload/taper,
    # None = could not be computed and the check is recorded as skipped).
    tss_floor = None
    if ctl_today:
        try:
            req = required_tss(cfg, ctl_today, today=week_start,
                               last_week_tss=last_week_actual_tss(_client(cfg), today=week_start))
            tss_floor = req.get("weekly_tss_floor")
        except Exception:
            tss_floor = None

    caps = run_caps(_client(cfg), week_start, run_protocol=cfg.get("run_protocol"))
    rep = validate_week(
        events, week_start,
        day_rules=day_rules, ctl_today=ctl_today,
        weekly_tss_cap=tss_cap,
        weekly_tss_floor=tss_floor,
        run_week_min_cap=caps.get("weekly_min_cap"),
        run_long_min_cap=caps.get("long_run_min_cap"),
        ramp_cap=float(cfg.get("max_ctl_ramp_per_week", 5.0)),
        strength_max=(day_rules or {}).get("strength_max"),
        distribution=phase.get("distribution"),
        # Absolute ride ceiling, same arming pattern as plan_builder.py: `or 300`
        # keeps this CLI backstop from silently skipping the check for an athlete
        # with no per-athlete override, which is exactly how it drifted out of
        # parity with the Sunday generator (4 Aug 2026 RIDE CEILING rule).
        long_ride_max_min=int(cfg.get("long_ride_max_min") or 300),
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
    the old avg+10 guess.

    --g-hr N --save records the training figure a NON-RACE athlete agreed with the coach
    (training_fuel_g_hr, lib/fuel_basis.py): it becomes what every prescription, check-in
    and weekly summary works toward instead of the default training level."""
    cfg = _load_cfg(args.athlete)
    saved = None
    if getattr(args, "save", False):
        if not args.g_hr or not 20 <= args.g_hr <= 120:
            raise SystemExit(_err("--save needs --g-hr between 20 and 120"))
        athletes = json.loads(ATHLETES_CONFIG.read_text())
        athletes[args.athlete]["training_fuel_g_hr"] = int(args.g_hr)
        ATHLETES_CONFIG.write_text(json.dumps(athletes, indent=2) + "\n")
        cfg = athletes[args.athlete]
        saved = int(args.g_hr)
    race_target = fuel_basis.ceiling(cfg)       # a non-racer's own figure, never 90
    sl_path = BASE / "athletes" / args.athlete / "session-log.json"
    session_log = json.loads(sl_path.read_text()) if sl_path.exists() else []
    avg = recent_avg_g_hr(session_log)
    target = fuel_target(avg, race_target)
    zone = "no logs yet" if avg is None else ("aggressive ramp (<60)" if avg < 60 else "careful ramp (>=60)")
    return {"athlete": args.athlete,
            "recent_avg_g_hr": round(avg, 1) if avg is not None else None,
            "race_target_g_hr": race_target, "prescribed_g_hr": target,
            "non_race": fuel_basis.non_race(cfg),
            "agreed_training_g_hr": fuel_basis.agreed(cfg), "saved": saved,
            "note": f"{zone}: prescribe {target} g/hr now ({fuel_basis.label(cfg)} {race_target})"}


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


# ── subcommand: race-predict ───────────────────────────────────────────────────
def _fmt_hm(minutes) -> str:
    m = int(round(minutes))
    return f"{m // 60}:{m % 60:02d}"


def cmd_race_predict(args) -> dict:
    """IM race prediction (Now / Race day / Target) from the shared model in
    lib/race_predictor.py — IF ∝ √CTL anchored to the previous race, FTP held
    fixed, IF capped at 0.75. Same model the website overview uses. Use these
    numbers, never invent splits."""
    from race_predictor import race_predictor
    prof_p = BASE / "athletes" / args.athlete / "profile.json"
    if not prof_p.exists():
        raise SystemExit(_err(f"no profile.json for '{args.athlete}'"))
    profile = json.loads(prof_p.read_text())
    ctl = getattr(args, "ctl", None)
    if ctl is None:
        td_p = BASE / "athletes" / args.athlete / "training-data.json"
        if td_p.exists():
            try:
                ctl = json.loads(td_p.read_text()).get("kpi", {}).get("ctl")
            except Exception:
                ctl = None
    if ctl is None:
        raise SystemExit(_err("no current CTL — training-data.json missing kpi.ctl; pass --ctl"))
    rp = race_predictor(profile, ctl)
    if rp is None:
        raise SystemExit(_err("race predictor inputs missing — profile needs prev_race "
                              "(bike_if, bike_np_watts, times) and race_predictor (anchor_ctl)"))
    for row in rp["rows"]:
        row["total"] = _fmt_hm(row["total_min"])
        row["bike"] = _fmt_hm(row["bike_min"])
        row["run"] = _fmt_hm(row["run_min"])
        row["swim"] = _fmt_hm(row["swim_min"])
    rp["anchor"]["total"] = _fmt_hm(rp["anchor"]["total_min"])
    rp["athlete"] = args.athlete
    rp["current_ctl"] = round(float(ctl), 1)
    rp["model"] = "IF ∝ √CTL vs previous-race anchor; FTP fixed; IF cap 0.75"
    return rp


# ── subcommand: ctl-sweep ──────────────────────────────────────────────────────
def cmd_ctl_sweep(args) -> dict:
    """CTL-sensitivity sweep: how much a race-day CTL / predicted outcome moves
    across a band of ramp-rate assumptions. NEVER hand-derive this — it re-uses
    the exact `project` (project_pmc_daily) and `race-predict` (race_predictor)
    machinery so the answer is one deterministic table, not a re-narrated
    trajectory each time the question comes up.

    For each ramp step (CTL points/week) in [--ramp-low, --ramp-high], the
    weekly TSS that ramp implies (compute_required_tss, same as required-tss's
    ramp cap) is held constant and projected day-by-day from today's seed
    CTL/ATL to race day (project_pmc_daily). The resulting race-day CTL is then
    fed through the shared race predictor to get a predicted total time."""
    from race_predictor import race_predictor
    cfg = _load_cfg(args.athlete)
    client = _client(cfg)

    seed_ctl, seed_atl = args.seed_ctl, args.seed_atl
    if seed_ctl is None or seed_atl is None:
        w = client.get_wellness(days=3)
        if not w:
            raise SystemExit(_err("no wellness data to seed CTL/ATL; pass --seed-ctl/--seed-atl"))
        last = w[-1]
        seed_ctl = round(float(last.get("ctl") or 0), 1) if seed_ctl is None else seed_ctl
        seed_atl = round(float(last.get("atl") or 0), 1) if seed_atl is None else seed_atl

    race_s = args.race_date or cfg.get("race_date")
    if not race_s:
        raise SystemExit(_err("no race_date configured; pass --race-date"))
    race = date.fromisoformat(race_s)
    today = date.fromisoformat(args.date) if getattr(args, "date", None) else date.today()
    days_to_race = (race - today).days
    if days_to_race <= 0:
        raise SystemExit(_err("race_date is not in the future"))

    lo, hi, step = args.ramp_low, args.ramp_high, args.ramp_step
    if lo > hi or step <= 0:
        raise SystemExit(_err("--ramp-low must be <= --ramp-high and --ramp-step must be > 0"))

    prof_p = BASE / "athletes" / args.athlete / "profile.json"
    if not prof_p.exists():
        raise SystemExit(_err(f"no profile.json for '{args.athlete}'"))
    profile = json.loads(prof_p.read_text())

    rows = []
    n_steps = int(round((hi - lo) / step)) + 1
    for i in range(n_steps):
        ramp = round(lo + i * step, 2)
        weekly_tss = compute_required_tss(seed_ctl, seed_ctl + ramp, 1)
        daily_tss = [weekly_tss / 7.0] * days_to_race
        race_day = project_pmc_daily(seed_ctl, seed_atl, daily_tss)[-1]
        row = {"ramp_ctl_per_week": ramp, "weekly_tss": weekly_tss,
               "race_day_ctl": race_day["ctl"], "race_day_tsb": race_day["tsb"]}
        rp = race_predictor(profile, race_day["ctl"])
        if rp:
            row["predicted_total"] = _fmt_hm(rp["rows"][0]["total_min"])
            row["predicted_total_min"] = rp["rows"][0]["total_min"]
        rows.append(row)

    out = {"athlete": args.athlete, "seed_ctl": seed_ctl, "seed_atl": seed_atl,
           "race_date": race_s, "days_to_race": days_to_race,
           "ramp_band": {"low": lo, "high": hi, "step": step}, "rows": rows,
           "race_day_ctl_range": {"min": min(r["race_day_ctl"] for r in rows),
                                  "max": max(r["race_day_ctl"] for r in rows)}}
    totals = [r["predicted_total_min"] for r in rows if "predicted_total_min" in r]
    if totals:
        out["predicted_total_range"] = {"min": _fmt_hm(min(totals)), "max": _fmt_hm(max(totals)),
                                        "spread_min": max(totals) - min(totals)}
    else:
        out["note"] = ("race predictor inputs missing (profile needs prev_race + "
                       "race_predictor blocks) — race-day CTL range only, no time prediction")
    return out


# ── subcommand: sweat-rate ─────────────────────────────────────────────────────
def cmd_sweat_rate(args) -> dict:
    """Sweat rate from a pre/post weigh-in (the standard field test): total loss =
    weight drop + fluid drunk; rate = loss / hours. With --save, writes sweat_ml_hr
    into config/athletes.json so race-fuelling / fuel-check pick it up automatically
    — the fuelling engine's hydration and sodium numbers are only as good as this
    input. Same maths as the web sweat-rate calculator."""
    cfg = _load_cfg(args.athlete)
    pre, post = float(args.pre), float(args.post)
    fluid_ml = float(args.fluid or 0)
    minutes = float(args.minutes)
    if minutes <= 0 or pre <= 0 or post <= 0:
        raise SystemExit(_err("implausible inputs — check pre/post kg and minutes"))
    total_loss_ml = (pre - post) * 1000 + fluid_ml
    rate = total_loss_ml / (minutes / 60.0)
    if rate <= 0:
        raise SystemExit(_err("computed sweat rate <= 0 — weight gain exceeded fluid intake; check the numbers"))
    pct_loss = (pre - post) / pre * 100
    status = ("well hydrated" if pct_loss < 1 else
              "mild dehydration" if pct_loss < 2 else
              "significant dehydration — fluid plan too light" if pct_loss < 3 else
              "serious dehydration — do not repeat this fluid plan")
    out = {"athlete": args.athlete,
           "sweat_rate_ml_hr": round(rate),
           "total_loss_ml": round(total_loss_ml),
           "body_weight_loss_pct": round(pct_loss, 1),
           "status": status,
           "previous_sweat_ml_hr": cfg.get("sweat_ml_hr"),
           "drink_rec_ml_per_15min": round(rate / 4),
           "saved": False}
    if getattr(args, "save", False):
        athletes = json.loads(ATHLETES_CONFIG.read_text())
        athletes[args.athlete]["sweat_ml_hr"] = round(rate)
        ATHLETES_CONFIG.write_text(json.dumps(athletes, indent=2) + "\n")
        out["saved"] = True
        out["note"] = ("sweat_ml_hr updated — race-fuelling and fuel-check now use it. "
                       "Log conditions (temp, intensity): hot-day rates don't transfer to cool days.")
    return out


# ── subcommand: wetsuit ────────────────────────────────────────────────────────
def cmd_wetsuit(args) -> dict:
    """Cervia race-day water temperature + wetsuit-legality prediction. Runs the
    SHARED JS engine (js/wetsuit-engine.js — the exact code behind the web
    predictor) via js/wetsuit-cli.js, which also fetches live Adriatic SST from
    the Open-Meteo Marine API. Use these numbers, never guess water temps."""
    if not WETSUIT_CLI.exists():
        raise SystemExit(_err(f"wetsuit engine not found at {WETSUIT_CLI}"))
    params = {}
    if getattr(args, "year", None):
        params["raceYear"] = args.year
    if getattr(args, "day", None):
        params["raceDayOfSept"] = args.day
    try:
        proc = subprocess.run(
            ["node", str(WETSUIT_CLI), "live", json.dumps(params)],
            capture_output=True, text=True, timeout=30)
    except FileNotFoundError:
        raise SystemExit(_err("node is not installed — the wetsuit engine needs Node.js"))
    except subprocess.TimeoutExpired:
        raise SystemExit(_err("wetsuit engine timed out (Open-Meteo fetch)"))
    out = (proc.stdout or "").strip()
    if not out:
        raise SystemExit(_err(f"wetsuit engine returned nothing (stderr: {proc.stderr.strip()[:200]})"))
    data = json.loads(out)
    if isinstance(data, dict) and data.get("error"):
        raise SystemExit(_err(f"wetsuit engine: {data['error']}"))
    p = data["prediction"]
    m5 = p.get("m5")
    return {
        "race": f"IRONMAN Italy Cervia — {p['raceDayOfSept']} Sep {p['raceYear']}",
        "predicted_official_water_temp_c": round(p["ensemble"]["temp"], 1),
        "range_c": [round(p["ensemble"]["lo"], 1), round(p["ensemble"]["hi"], 1)],
        "prob_ag_wetsuit_pct": round(p["prob"]["ag"]),
        "prob_pro_wetsuit_pct": round(p["prob"]["pro"]),
        "verdict": p["verdict"],
        "thresholds_c": {"ag": 24.5, "pro": 21.9},
        "live_sst": data.get("live"),
        "live_anomaly_method_active": m5 is not None,
        "live_anomaly": ({"days_until_race": m5["daysUntilRace"],
                          "confidence": m5["confidence"],
                          "anomaly_c": round(m5["liveAnomaly"], 2)} if m5 else
                         "inactive — activates within 30 days of the race"),
        "summer_anomaly": ({"anomaly_now_c": round(p["m7"]["anomalyNow"], 2),
                            "retained_at_race_c": round(p["m7"]["anomalyAtRace"], 2),
                            "note": "sea vs same dates 2023-25; seasonal persistence e-folding ~65d"}
                           if p.get("m7") else "inactive (race <30 days: live anomaly supersedes)"),
        "forecast_method_active": p.get("m6") is not None,
        "forecast_method": ({"race_day_forecast_c": round(p["m6"]["temp"], 1),
                             "gap_days": p["m6"]["gapDays"]} if p.get("m6") else
                            "inactive — activates within ~8 days of the race (physical ocean model; "
                            "highest-weight method when live)"),
        "bora_wind": (({"ne_wind_days": p["wind"]["boraDays"],
                        "mean_shift_c": p["wind"]["shift"],
                        "note": p["wind"]["note"]} if p["wind"]["boraDays"] else
                       "no significant NE wind in the pre-race window")
                      if p.get("wind") else "wind forecast not yet in range (~15 days)"),
        "model_skill_backtest": {
            "mae_c": round(p["backtest"]["mae"], 2),
            "wetsuit_call_accuracy_pct": round(p["backtest"]["callAccuracy"] * 100),
            "n_years": p["backtest"]["n"],
            "sd_floored_to_backtest": p["ensemble"]["calibration"]["sdFloorApplied"],
            "meaning": ("Weeks out, the model's real error is ~2C and the quoted probability is "
                        "calibrated to that — treat far-out verdicts as genuinely uncertain. "
                        "Race-week forecast + live anomaly are what settle the answer."),
        },
        "methods": [{"name": m["name"], "temp_c": round(m["temp"], 1)} for m in p["methods"]],
        "note": ("Ensemble of up to 6 methods, bias-corrected (official race-morning readings "
                 "average ~0.6C cooler than satellite SST), uncertainty floored at backtested "
                 "error, Bora cold-tail adjustment in race week."),
        "web_predictor": "https://diamondpeak.uk/cycling/cervia-wetsuit.html",
        "model_data_updated": p.get("dataUpdated"),
    }


def _extract_watts(streams):
    for s in (streams or []):
        if s.get("type") == "watts":
            return s.get("data")
    return None


# ── subcommand: windowed-np ─────────────────────────────────────────────────────
def cmd_windowed_np(args) -> dict:
    """NP for one segment of a ride (--start/--end in seconds from ride start),
    using the same 30s-rolling-mean^4 method as the power-curve cache — never
    hand-average watts for a segment in chat. Also computes whole-ride NP the same
    way and reconciles it against Intervals.icu's own recorded icu_weighted_avg_watts
    for the activity; a mismatch beyond a few watts means the streams don't line up
    with what ICU scored (trimmed/re-synced ride, wrong activity id), and the
    windowed figure is flagged rather than handed over as if it were trustworthy."""
    import np_curve
    cfg = _load_cfg(args.athlete)
    client = _client(cfg)
    watts = _extract_watts(client.get_activity_streams(args.activity_id))
    if not watts:
        raise SystemExit(_err(f"activity {args.activity_id} has no watts stream"))
    start, end = int(args.start), int(args.end)
    seg_np = np_curve.np_for_window(watts, start, end)
    if seg_np is None:
        raise SystemExit(_err(
            f"window [{start},{end}) is invalid or shorter than the 30s NP smoothing "
            f"window (stream length {len(watts)}s)"))
    whole_ride_np = np_curve.np_for_ride(watts)
    icu_np = client.get_activity_detail(args.activity_id).get("icu_weighted_avg_watts")
    reconciled, note = True, None
    if icu_np and whole_ride_np:
        delta = abs(whole_ride_np - icu_np)
        if delta > 5:
            reconciled = False
            note = (f"computed whole-ride NP ({whole_ride_np}W) differs from ICU's "
                     f"recorded NP ({icu_np}W) by {delta:.0f}W — streams may not match "
                     "this activity (re-synced/trimmed ride); treat the windowed "
                     "figure as unverified")
    return {"athlete": args.athlete, "activity_id": args.activity_id,
            "window_s": [start, end], "windowed_np_w": seg_np,
            "whole_ride_np_w": whole_ride_np, "icu_recorded_np_w": icu_np,
            "reconciled": reconciled, "note": note}


# ── subcommand: wbal ────────────────────────────────────────────────────────────
def cmd_wbal(args) -> dict:
    """W' balance (Skiba differential model) swept across a CP band, for one
    activity — never hand-compute joules-above-CP from raw streams in chat, and
    never substitute a plain running total of joules above threshold for W'bal
    (it has no recovery term and silently overstates fatigue). CP is rarely known
    to the exact watt, so this scans [--cp-low, --cp-high] rather than one guessed
    value; --wprime-j must come from a known/measured figure, never assumed."""
    import wbal as wbal_mod
    cfg = _load_cfg(args.athlete)
    client = _client(cfg)
    watts = _extract_watts(client.get_activity_streams(args.activity_id))
    if not watts:
        raise SystemExit(_err(f"activity {args.activity_id} has no watts stream"))
    cp_low, cp_high, w_prime_j = float(args.cp_low), float(args.cp_high), float(args.wprime_j)
    if cp_low <= 0 or cp_high < cp_low or w_prime_j <= 0:
        raise SystemExit(_err("implausible inputs — check --cp-low/--cp-high/--wprime-j"))
    sweep = wbal_mod.sweep_cp(watts, cp_low, cp_high, w_prime_j,
                              step=max(1, int(args.cp_step or 5)))
    if not sweep:
        raise SystemExit(_err("stream too short or empty for a W'bal curve"))
    worst_cp = min(sweep, key=lambda cp: sweep[cp]["min_j"])
    return {"athlete": args.athlete, "activity_id": args.activity_id,
            "cp_range_w": [cp_low, cp_high], "wprime_j": w_prime_j,
            "sweep": sweep, "worst_case": {"cp_w": worst_cp, **sweep[worst_cp]}}


# ── subcommand: post-race-ready ────────────────────────────────────────────────
def set_post_race_ready(slug: str, when=None, undo: bool = False, path=None) -> dict:
    """Record (or clear, with undo) the athlete saying they are ready to train again
    after an A-race. Backs up athletes.json first. Only meaningful for an athlete with
    `post_race_hold` on; for anyone else it is recorded but changes nothing."""
    import shutil
    p = Path(path or ATHLETES_CONFIG)
    athletes = json.loads(p.read_text())
    if slug not in athletes:
        raise SystemExit(_err(f"unknown athlete '{slug}'"))
    cfg = athletes[slug]
    before = cfg.get("post_race_ready")
    if undo:
        cfg.pop("post_race_ready", None)
    else:
        d = when if isinstance(when, date) else (
            date.fromisoformat(str(when)[:10]) if when else date.today())
        cfg["post_race_ready"] = d.isoformat()
    shutil.copy2(p, p.with_name(p.name + f".bak-post-race-ready-{date.today().isoformat()}"))
    p.write_text(json.dumps(athletes, indent=2) + "\n")
    race_s, race_d = recovery_race(cfg)
    ready = post_race_ready_date(cfg)
    first_monday = (ready + timedelta(days=(7 - ready.weekday()) % 7)) if ready else None
    return {"athlete": slug, "post_race_ready": cfg.get("post_race_ready"),
            "was": before, "post_race_hold": bool(cfg.get("post_race_hold")),
            "hold_active": post_race_hold_active(cfg, race_d),
            "block_starts": first_monday.isoformat() if first_monday else None,
            # Tell the athlete these need a new date: they fell inside the hold.
            "bookings_to_redate": (held_bookings(cfg, race_d, first_monday)
                                   if first_monday else [])}


def cmd_post_race_ready(args) -> dict:
    return set_post_race_ready(args.athlete, when=args.date, undo=args.undo)


# ── subcommand: fitness-choice ─────────────────────────────────────────────────
def set_fitness_choice(slug: str, choice: str = None, clear: bool = False, path=None) -> dict:
    """Record (or clear) how the athlete wants to handle being fitter than their goal
    needs (race_fitness.SURPLUS_CHOICES). Backs up athletes.json first."""
    import shutil
    if not clear and choice not in rf.SURPLUS_CHOICES:
        raise SystemExit(_err(f"choice must be one of {sorted(rf.SURPLUS_CHOICES)}"))
    p = Path(path or ATHLETES_CONFIG)
    athletes = json.loads(p.read_text())
    if slug not in athletes:
        raise SystemExit(_err(f"unknown athlete '{slug}'"))
    cfg = athletes[slug]
    before = cfg.get("fitness_surplus_choice")
    if clear:
        cfg.pop("fitness_surplus_choice", None)
    else:
        cfg["fitness_surplus_choice"] = choice
    shutil.copy2(p, p.with_name(p.name + f".bak-fitness-choice-{date.today().isoformat()}"))
    p.write_text(json.dumps(athletes, indent=2) + "\n")
    return {"athlete": slug, "fitness_surplus_choice": cfg.get("fitness_surplus_choice"),
            "was": before,
            "means": rf.SURPLUS_CHOICES.get(cfg.get("fitness_surplus_choice"))}


def cmd_fitness_choice(args) -> dict:
    return set_fitness_choice(args.athlete, choice=args.choice, clear=args.clear)


# ── subcommand: bespoke-event ──────────────────────────────────────────────────
def set_bespoke_event(slug: str, name: str = None, swim_km=0, bike_km=0, run_km=0,
                      clear: bool = False, path=None) -> dict:
    """Store (or clear) a temporary blueprint for a non-standard race, blended from the
    nearest standard events (race_fitness.bespoke_event). Bound to the athlete's current
    race_name, so it stops applying the moment a different race is set."""
    import shutil
    p = Path(path or ATHLETES_CONFIG)
    athletes = json.loads(p.read_text())
    if slug not in athletes:
        raise SystemExit(_err(f"unknown athlete '{slug}'"))
    cfg = athletes[slug]
    if clear:
        cfg.pop("bespoke_event", None)
        out = {"athlete": slug, "bespoke_event": None}
    else:
        try:
            ev = rf.bespoke_event(name or cfg.get("race_name") or "Bespoke event",
                                  swim_km=swim_km, bike_km=bike_km, run_km=run_km)
        except ValueError as e:
            raise SystemExit(_err(str(e)))
        ev["race_name"] = cfg.get("race_name")
        cfg["bespoke_event"] = ev
        out = {"athlete": slug, "blend": rf.describe_blend(ev),
               "levels": [(lv["level"], lv["label"]) for lv in ev["levels"]],
               "taper_days": ev["taper_days"], "notes": ev.get("notes") or []}
    shutil.copy2(p, p.with_name(p.name + f".bak-bespoke-event-{date.today().isoformat()}"))
    p.write_text(json.dumps(athletes, indent=2) + "\n")
    return out


def cmd_bespoke_event(args) -> dict:
    return set_bespoke_event(args.athlete, name=args.name, swim_km=args.swim_km,
                             bike_km=args.bike_km, run_km=args.run_km, clear=args.clear)


# ── subcommand: race-level ─────────────────────────────────────────────────────
def race_level(slug: str, path=None) -> dict:
    """The athlete's level for their current race (blueprint §4.5) and the numbers that
    come with it. Read-only: the Sunday plan applies it; this is what the coach quotes."""
    cfg = _load_cfg(slug) if path is None else json.loads(Path(path).read_text())[slug]
    profile = {}
    try:
        profile = json.loads((BASE / "athletes" / slug / "profile.json").read_text())
    except Exception:
        pass
    ev = rf.event_def(cfg, cfg.get("race_distance") or cfg.get("race_name"))
    if not ev:
        return {"athlete": slug, "error": f"no level table for '{cfg.get('race_name')}' - "
                "use bespoke-event for a non-standard distance"}
    lv = rf.athlete_level(cfg, profile, ev)
    return {"athlete": slug, "race": cfg.get("race_name"), "event": ev.get("label"),
            "level": lv["level"], "label": lv["label"], "source": lv["source"],
            "numbers": {k: v for k, v in lv.items()
                        if k in ("run_km", "hours", "long_run_km", "long_ride_h",
                                 "fitness_at_taper")},
            "fitness_kind": "running" if ev.get("kind") == "run" else "total",
            "taper_days": ev.get("taper_days"),
            **({"blend": rf.describe_blend(ev)} if ev.get("blended_from") else {})}


def cmd_race_level(args) -> dict:
    return race_level(args.athlete)


# ── subcommand: race-prompt ────────────────────────────────────────────────────
def set_race_prompt(slug: str, topic: str, answer: str, base=None) -> dict:
    """Record the athlete's answer to the fuelling or heat suggestion (lib/race_prompts).
    yes switches the feature on; no is asked once more when the peak phase starts."""
    import shutil
    import race_prompts as rpm
    p = Path(base or BASE) / "athletes" / slug / "profile.json"
    prof = json.loads(p.read_text())
    try:
        new = rpm.record(prof, topic, answer)
    except ValueError as e:
        raise SystemExit(_err(str(e)))
    shutil.copy2(p, p.with_name(p.name + f".bak-race-prompt-{date.today().isoformat()}"))
    p.write_text(json.dumps(new, indent=2, ensure_ascii=False) + "\n")
    out = {"athlete": slug, "topic": topic, "answer": answer,
           "fuelling_coaching": new.get("fuelling_coaching"),
           "heat_protocol": new.get("heat_protocol"),
           "race_conditions": new.get("race_conditions")}
    if topic == "heat" and answer == "yes":
        out["next"] = (f"arm the heat block: python3 ClaudeCoach/scripts/generate-blueprint.py "
                       f"--athlete {slug} --skip-events")
    if answer == "no":
        out["next"] = "asked once more when the peak phase starts"
    return out


def cmd_race_prompt(args) -> dict:
    return set_race_prompt(args.athlete, args.topic, args.answer)


# ── subcommand: race-setup ─────────────────────────────────────────────────────
# NEXT-RACE SETUP (Jamie, 30 Sep 2026): "the coach bot can do this with the athlete as one
# event finishes" - the block for the next A-race (phase weeks, Fitness targets, blueprint)
# is set up in the conversation, not by hand in a dev session. Preview first; --apply only
# on the athlete's OK. One function, so the numbers the athlete agreed are the numbers
# written.
_RUN_CROSS_TRAINING = 1.3      # total ~ running x 1.3 for a runner who keeps bike / swim


def _block_weeks(weeks: int, taper_days, long_course: bool) -> dict:
    """phase_tss end-weeks for a block of `weeks` (blueprint §1.1 shape): taper from the
    event, peak 2, build up to 8, a 3-week specific phase for long-course events with
    room for it, base gets the rest."""
    taper = max(1, -(-int(taper_days[1]) // 7)) if taper_days else 2
    rest = max(0, weeks - taper - 2)
    build = min(8, round(rest * 0.45))
    specific = 3 if long_course and rest >= 14 else 0
    base = max(0, rest - build - specific)
    out = {"base_end_week": base, "build_end_week": base + build}
    if specific:
        out["specific_end_week"] = base + build + specific
    out["peak_end_week"] = base + build + specific + 2
    return out


def race_setup(slug: str, apply: bool = False, today=None, path=None,
               into_taper: float | None = None) -> dict:
    """Plan the block for the athlete's current A-race: level, phase weeks, total
    Fitness targets (phase_ctl, race_min) and, with apply, write them and rebuild the
    blueprint. Keeps an existing future plan_start (e.g. after a recovery hold)."""
    import math
    import shutil
    today = today or date.today()
    p = Path(path or ATHLETES_CONFIG)
    athletes = json.loads(p.read_text())
    if slug not in athletes:
        raise SystemExit(_err(f"unknown athlete '{slug}'"))
    cfg = athletes[slug]
    try:
        race_d = date.fromisoformat(str(cfg.get("race_date"))[:10])
    except ValueError:
        raise SystemExit(_err("no race_date - set the next race first"))
    if race_d <= today:
        raise SystemExit(_err(f"race_date {race_d} is not in the future - set the next race first"))
    profile = {}
    try:
        profile = json.loads((BASE / "athletes" / slug / "profile.json").read_text())
    except Exception:
        pass
    ev = rf.event_def(cfg, cfg.get("race_distance") or cfg.get("race_name"))
    if not ev:
        raise SystemExit(_err(f"no blueprint for '{cfg.get('race_name')}' - run bespoke-event first"))
    lv = rf.athlete_level(cfg, profile, ev)

    # Block start: keep a plan_start that is still ahead (a recovery hold or an agreed
    # date), else the coming Monday.
    ps = None
    try:
        ps = date.fromisoformat(str(cfg.get("plan_start"))[:10])
    except ValueError:
        pass
    if not ps or ps <= today:
        ps = today + timedelta(days=(7 - today.weekday()) % 7 or 7)
    weeks = max(1, -(-((race_d - ps).days + 1) // 7))      # race week included
    long_course = ev.get("kind") in ("run", "tri") and (
        (ev.get("distance_km") or 0) >= 21 or rf.levels_key(ev.get("label")) in ("70_3", "ironman")
        or (ev.get("est_minutes") or 0) >= 240)
    phases = _block_weeks(weeks, ev.get("taper_days"), long_course)

    # Total Fitness into the taper. Tri / bike: the level's range. Run race: the level's
    # RUNNING range, x1.3 when they keep cycling / swimming days (Jamie's Brighton: sub-3
    # running + cycling ~105). Never under the athlete's own total floor. Rounded to 5.
    mid = sum(lv["fitness_at_taper"]) / 2
    if ev.get("kind") == "run":
        dr = cfg.get("day_rules") or {}
        mid *= _RUN_CROSS_TRAINING if (dr.get("bike_days") or dr.get("swim_days")) else 1.0
    floor = rf.total_floor(cfg) or 0
    top = max(float(floor), 5 * round(mid / 5))
    # The athlete's own number wins (Jamie, 1 Oct 2026: Brighton peak 95, not the 105 the
    # x1.3 default gives). Stored, so re-running the setup keeps it.
    own = into_taper if into_taper is not None else (cfg.get("ctl_targets") or {}).get("into_taper_total")
    if own:
        top = max(float(floor), float(own))
    frac = rf.PHASE_FRACTION
    phase_ctl = {"base": round(top * frac["build"]),
                 "build": round(top * (frac["specific"] if "specific_end_week" in phases
                                       else frac["peak"]))}
    if "specific_end_week" in phases:
        phase_ctl["specific"] = round(top * frac["peak"])
    phase_ctl["peak"] = round(top)
    taper_w = weeks - phases["peak_end_week"]
    race_min = round(top * (1 - 0.035 * max(1, taper_w)))

    def monday(w):
        return (ps + timedelta(days=7 * w)).isoformat()
    windows = {"base": [ps.isoformat(), monday(phases["base_end_week"])]}
    prev = phases["base_end_week"]
    for name in ("build", "specific", "peak"):
        k = f"{name}_end_week"
        if k in phases:
            windows[name] = [monday(prev), monday(phases[k])]
            prev = phases[k]
    windows["taper"] = [monday(prev), race_d.isoformat()]

    out = {"athlete": slug, "race": cfg.get("race_name"), "race_date": race_d.isoformat(),
           "event": ev.get("label"), "level": lv["level"], "level_label": lv["label"],
           "level_source": lv["source"], "plan_start": ps.isoformat(), "weeks": weeks,
           "phase_tss": phases, "phase_windows": windows,
           "fitness_targets": {"phase_ctl": phase_ctl, "race_min": race_min,
                               "into_taper": round(top), "kind": "total"},
           "weekly_volume": lv.get("run_km") or lv.get("hours"),
           "volume_unit": "km/week running" if "run_km" in lv else "hours/week",
           "long_session": {k: lv[k] for k in ("long_run_km", "long_ride_h") if k in lv},
           "taper_days": ev.get("taper_days"), "applied": False}
    if ev.get("kind") == "run":
        out["running_into_taper"] = lv["fitness_at_taper"]
    if not apply:
        out["next"] = ("show the athlete level, block dates, weekly volume and Fitness targets; "
                       f"on their OK run race-setup --athlete {slug} --apply")
        return out
    shutil.copy2(p, p.with_name(p.name + f".bak-race-setup-{date.today().isoformat()}"))
    cfg["plan_start"] = ps.isoformat()
    cfg["phase_tss"] = phases
    ct = dict(cfg.get("ctl_targets") or {})
    ct.update({"phase_ctl": phase_ctl, "race_min": race_min})
    if into_taper is not None:
        ct["into_taper_total"] = float(into_taper)
    cfg["ctl_targets"] = ct
    p.write_text(json.dumps(athletes, indent=2) + "\n")
    out["applied"] = True
    if path is None:                      # the real config: rebuild the blueprint to match
        r = subprocess.run(["python3", str(BASE / "scripts" / "generate-blueprint.py"),
                            "--athlete", slug, "--skip-events"], cwd=str(BASE.parent),
                           capture_output=True, text=True, timeout=180)
        out["blueprint"] = "rebuilt" if r.returncode == 0 else f"FAILED: {r.stderr[-300:]}"
    return out


def cmd_race_setup(args) -> dict:
    return race_setup(args.athlete, apply=args.apply, into_taper=args.into_taper)


# ── subcommand: goal-setup ─────────────────────────────────────────────────────
def goal_setup(slug: str, goal: str | None = None, apply: bool = False, today=None,
               path=None, sports=None, start=None, clear: bool = False,
               ctl=None, block_weeks: int | None = None) -> dict:
    """Set (or with clear, remove) the athlete's goal without a race (lib/goals.py).
    A preview by default: what the blocks, tests and this week's load would be. With
    apply, writes athletes.json `goal`, block 1 starting `start` or the coming Monday."""
    import shutil
    today = today or date.today()
    p = Path(path or ATHLETES_CONFIG)
    athletes = json.loads(p.read_text())
    if slug not in athletes:
        raise SystemExit(_err(f"unknown athlete '{slug}'"))
    cfg = athletes[slug]
    if clear:
        if not apply:
            return {"athlete": slug, "would_clear": cfg.get("goal"), "applied": False,
                    "next": f"on their OK run goal-setup --athlete {slug} --clear --apply"}
        shutil.copy2(p, p.with_name(p.name + f".bak-goal-setup-{today.isoformat()}"))
        old = cfg.pop("goal", None)
        p.write_text(json.dumps(athletes, indent=2) + "\n")
        return {"athlete": slug, "cleared": old, "applied": True}
    if goal not in _goals.GOALS:
        raise SystemExit(_err(f"--goal must be one of {', '.join(_goals.GOAL_ORDER)}"))
    profile = {}
    try:
        profile = json.loads((BASE / "athletes" / slug / "profile.json").read_text())
    except Exception:
        pass
    if ctl is None and path is None:
        try:
            w = _client(cfg).get_wellness(days=3)
            ctl = round(float(w[-1].get("ctl") or 0), 1) if w else None
        except Exception:
            ctl = None
    if isinstance(sports, str):
        sports = _goals.parse_sports(sports)
    first = start if isinstance(start, date) else (
        date.fromisoformat(start) if start else today + timedelta(days=(7 - today.weekday()) % 7 or 7))
    g = _goals.make(goal, today=today, start=first, sports=sports, ctl=ctl, profile=profile)
    if block_weeks:                      # 5 = 4 build weeks + 1 easier, for an experienced athlete
        g["block_weeks"] = max(_goals.MIN_BLOCK_WEEKS, min(_goals.MAX_BLOCK_WEEKS, int(block_weeks)))
    trial = dict(cfg, goal=g)
    gd = _goals.GOALS[goal]
    # The first week the goal would actually run: a race ahead, its recovery (or a recovery
    # hold) and an off-season block all come first.
    begins = next((w for w in (date.fromisoformat(g["start"]) + timedelta(weeks=i)
                               for i in range(80)) if goal_active(trial, w)), None)
    out = {"athlete": slug, "goal": goal, "label": gd["label"],
           "sports": _goals.sports_for(trial, profile),
           "week_shape": list(_goals.shape(_goals.block_weeks(trial))),
           "zone_split": _goals.distribution(trial, profile),
           "ctl_today": ctl, "applied": False}
    if begins:
        b1 = goal_start(trial, slug, begins)
        out["block_1"] = [b1.isoformat(),
                          (b1 + timedelta(weeks=_goals.block_weeks(trial), days=-1)).isoformat()]
        out["tests"] = [b["week_start"] + ": " + b["name"]
                        for b in goal_test_bookings(trial, slug, today=begins)[:2]]
        if ctl and begins == date.fromisoformat(g["start"]):
            wk = required_tss(trial, ctl, today=begins, slug=slug, profile=profile)
            out["first_week_tss"] = wk.get("recommended_weekly_tss")
    if g.get("ctl_band"):
        out["fitness_band"] = g["ctl_band"]
    if a_race_ahead(cfg, today):
        out["note"] = ("An A race is ahead, so the race block runs first; the goal starts "
                       "after its recovery weeks.")
    if not begins:
        out["note"] = ("The goal would not start in the next 80 weeks: "
                       + ("an off-season block is configured for after the race and wins"
                          if offseason_cfg(cfg) else
                          "a post-race recovery hold is on until they say they're ready"
                          if cfg.get("post_race_hold") else "a race or its recovery comes first")
                       + ". Say so; don't apply it without asking.")
    if not apply:
        out["next"] = (f"tell them the goal, the block shape and the first test in a few "
                       f"lines; on their OK run goal-setup --athlete {slug} --goal {goal} --apply")
        return out
    shutil.copy2(p, p.with_name(p.name + f".bak-goal-setup-{today.isoformat()}"))
    cfg["goal"] = g
    p.write_text(json.dumps(athletes, indent=2) + "\n")
    out["applied"] = True
    return out


def cmd_goal_setup(args) -> dict:
    return goal_setup(args.athlete, args.goal, apply=args.apply, sports=args.sports,
                      start=args.start, clear=args.clear, block_weeks=args.block_weeks)


# Chat side of the choice: only the bot hears the athlete answer the Sunday question.
_SURPLUS_PROMPT = (
    "FITTER THAN THE GOAL: when {name}'s Fitness is above what their goal needs, the "
    "weekly plan asks how to handle it. When {name} answers, FIRST run `python3 "
    "ClaudeCoach/lib/plan_tools.py fitness-choice --athlete {slug} --choice <c>` with "
    "(the weekly message numbers them 1, 2, 3) 1 = hold_shift (hold total Fitness, shift the mix toward the goal sport, e.g. more "
    "running and let swimming fade), 2 = drift (let it drift down to their floor and put the "
    "freed time into speed) or 3 = raise_goal; `--clear` to undo. Never choose for them.{cur}"
    "\nRACE LEVEL AND NON-STANDARD RACES: `plan_tools.py race-level --athlete {slug}` gives "
    "their level for the race (from their goal time; blueprint §4.5) and its weekly volume, "
    "longest session and Fitness targets - quote those, never a one-size number. If they name "
    "a race that is not a standard distance (a 30k run, a 4 km swim + 10 km bike), FIRST run "
    "`plan_tools.py bespoke-event --athlete {slug} --name <race> --run-km/--bike-km/--swim-km "
    "<km>` and tell them what it was blended from (its `blend` line) and any `notes`."
    "\nFUELLING / HEAT QUESTIONS: when they answer the plan message's *fuelling yes/no* "
    "(carbs, salt and water after long sessions - NOT the Food tab) or *heat yes/no*, FIRST "
    "run `plan_tools.py race-prompt --athlete {slug} --topic nutrition|heat --answer yes|no`, "
    "then run any `next` command it returns."
    "\nNEXT RACE SETUP: when a race is finished and {name} names the next A-race (or changes "
    "race or goal time), set the race, then run `plan_tools.py race-setup --athlete {slug}` "
    "(a PREVIEW), walk them through it in a few lines (level, block dates, peak weekly "
    "volume, longest session, Fitness into the taper) and ONLY on their OK run it again "
    "with `--apply`, which writes the block and rebuilds the blueprint. Never apply without "
    "their OK."
)


def fitness_choice_prompt_block(slug: str, first_name: str = "", path=None) -> str:
    """The system-prompt block telling the bot how to record the athlete's answer."""
    try:
        cfg = (json.loads(Path(path or ATHLETES_CONFIG).read_text()) or {}).get(slug) or {}
    except Exception:
        return ""
    c = rf.surplus_choice(cfg)
    cur = f" Current answer: {c} ({rf.SURPLUS_CHOICES[c]})." if c else ""
    return _SURPLUS_PROMPT.format(name=first_name or slug.title(), slug=slug, cur=cur)


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
    name = getattr(args, "name", "") or ""
    r = render_workout(args.sport, segs, name)
    # Refuse to hand back a description whose steps don't back up the session NAME
    # (validate_week and the daily audit both catch this, but only AFTER the mismatch
    # has already reached push_workout / the athlete's watch). Checking here, before
    # the CLI door hands the description off to be pushed, is the only point that can
    # stop it rather than just report it next day.
    mm = name_intensity_mismatch(args.sport, name, r["description"])
    if mm:
        raise SystemExit(_err(
            f"name/steps mismatch: '{name}' claims {mm['claim']} but the hardest "
            f"rendered step is {mm['found']}%, short of {mm['required']}% — fix the "
            f"segments (or --name) before pushing"))
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

    psl = sub.add_parser("session-for-load",
                         help="derive a session DURATION from a Load/TSS target (Load held fixed)")
    psl.add_argument("--sport", required=True)
    psl.add_argument("--load-target", type=float, required=True, dest="load_target")
    psl.add_argument("--if", type=float, dest="if_", help="session-average intensity factor")
    psl.add_argument("--zone", help="named zone (e.g. z2, css, threshold); default endurance zone if omitted")
    psl.add_argument("--name")

    psd = sub.add_parser("session-load", help="the ONE authoritative Load for a planned session")
    psd.add_argument("--athlete"); psd.add_argument("--date", help="YYYY-MM-DD")
    psd.add_argument("--sport"); psd.add_argument("--event", help="event JSON (alternative to --athlete/--date)")

    psm = sub.add_parser("sum", help="deterministic addition of a JSON list of numbers")
    psm.add_argument("--values", required=True, help='JSON list of numbers, e.g. [120,118,95]')

    pw = sub.add_parser("week-tss", help="deterministic weekly roll-up from the calendar")
    pw.add_argument("--athlete", required=True); pw.add_argument("--week-start")

    pwc = sub.add_parser("week-caption",
                         help="fixed Hrs/TSS-vs-floor/by-sport/Fitness-ramp weekly caption text")
    pwc.add_argument("--athlete", required=True)
    pwc.add_argument("--start", required=True, help="Monday of the week, YYYY-MM-DD")
    pwc.add_argument("--end", required=True, help="Sunday of the week, YYYY-MM-DD")

    pp = sub.add_parser("project", help="day-by-day CTL/ATL/TSB projection")
    pp.add_argument("--athlete", required=True); pp.add_argument("--daily", required=True)
    pp.add_argument("--seed-ctl", type=float); pp.add_argument("--seed-atl", type=float)

    pr = sub.add_parser("required-tss", help="weekly TSS needed for the phase CTL target")
    pr.add_argument("--athlete", required=True); pr.add_argument("--ctl-today", type=float)
    pr.add_argument("--date", help="evaluate for this date's week (YYYY-MM-DD; default today)")

    pv = sub.add_parser("validate", help="hard-check a proposed week against the athlete's rules")
    pv.add_argument("--athlete", required=True); pv.add_argument("--week", required=True)
    pv.add_argument("--ctl-today", type=float)

    prw = sub.add_parser("render-workout", help="segments -> ICU structured workout text (syncs to Garmin)")
    prw.add_argument("--sport", required=True)
    prw.add_argument("--segments", required=True)
    # --name is not cosmetic: render_workout narrows a COARSE band label to the system the
    # NAME claims (planned_tss._refine_to_claim), because the library gives both `tempo`
    # and `sweetspot` the label Z3. Without it, "Sweetspot 2x20" over Z3 segments renders
    # at 84% FTP and then hard-blocks name_intensity_mismatch at validation - the same
    # outage that cost all three athletes their week on 9 Aug 2026, reached by the CLI door.
    prw.add_argument("--name", default="", help="session name; narrows a coarse Z-label to "
                                                "the system the name claims")

    pf = sub.add_parser("fuel-target", help="deterministic g/hr fuelling prescription for >90-min sessions")
    pf.add_argument("--athlete", required=True)
    pf.add_argument("--g-hr", type=int, dest="g_hr", help="agreed training figure (non-race athletes)")
    pf.add_argument("--save", action="store_true", help="save --g-hr as training_fuel_g_hr")

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

    prp = sub.add_parser("race-predict", help="IM race prediction Now/Race-day/Target (IF ∝ √CTL, shared model)")
    prp.add_argument("--athlete", required=True)
    prp.add_argument("--ctl", type=float, help="current CTL (default: training-data.json kpi.ctl)")

    pcs = sub.add_parser("ctl-sweep",
                         help="CTL-sensitivity sweep: race-day CTL + predicted-outcome range "
                              "across a band of ramp-rate assumptions (reuses project + race-predict)")
    pcs.add_argument("--athlete", required=True)
    pcs.add_argument("--seed-ctl", type=float); pcs.add_argument("--seed-atl", type=float)
    pcs.add_argument("--race-date", help="YYYY-MM-DD (default: cfg race_date)")
    pcs.add_argument("--date", help="evaluate from this date (YYYY-MM-DD; default today)")
    pcs.add_argument("--ramp-low", type=float, default=2.0, dest="ramp_low",
                     help="lowest ramp-rate to sweep, CTL points/week (default 2.0)")
    pcs.add_argument("--ramp-high", type=float, default=6.0, dest="ramp_high",
                     help="highest ramp-rate to sweep, CTL points/week (default 6.0)")
    pcs.add_argument("--ramp-step", type=float, default=1.0, dest="ramp_step",
                     help="step between swept ramp-rates (default 1.0)")

    psr = sub.add_parser("sweat-rate", help="sweat rate from a pre/post weigh-in; --save updates the athlete's sweat_ml_hr")
    psr.add_argument("--athlete", required=True)
    psr.add_argument("--pre", type=float, required=True, help="pre-session weight kg")
    psr.add_argument("--post", type=float, required=True, help="post-session weight kg")
    psr.add_argument("--fluid", type=float, help="fluid drunk during, ml")
    psr.add_argument("--minutes", type=float, required=True, help="session duration")
    psr.add_argument("--save", action="store_true", help="write result to config/athletes.json sweat_ml_hr")

    pws = sub.add_parser("wetsuit", help="Cervia water-temp + wetsuit-legality prediction with live SST (shared engine)")
    pws.add_argument("--year", type=int, help="race year (default: next upcoming edition)")
    pws.add_argument("--day", type=int, help="race day of September (default: known race date)")

    pls = sub.add_parser("log-strength", help="log CrossFit/gym as real ICU load (manual activity via mark-as-done)")
    pls.add_argument("--athlete", required=True)
    pls.add_argument("--minutes", type=int, default=60)
    pls.add_argument("--rpe", type=int, help="1-10; default 7 (est IF = 0.55 + 0.025 x RPE)")
    pls.add_argument("--date", help="YYYY-MM-DD; default today")
    pls.add_argument("--name")

    ppr = sub.add_parser("post-race-ready",
                         help="athlete says they are ready to train after an A-race: ends the recovery hold")
    ppr.add_argument("--athlete", required=True)
    ppr.add_argument("--date", help="YYYY-MM-DD; default today")
    ppr.add_argument("--undo", action="store_true", help="clear it (back into the recovery hold)")

    pfc = sub.add_parser("fitness-choice",
                         help="how the athlete wants to handle being fitter than their goal needs")
    pfc.add_argument("--athlete", required=True)
    pfc.add_argument("--choice", choices=sorted(rf.SURPLUS_CHOICES))
    pfc.add_argument("--clear", action="store_true", help="forget the answer (the plan holds and asks again)")

    pbe = sub.add_parser("bespoke-event",
                         help="temporary blueprint for a non-standard race, blended from the nearest standard events")
    pbe.add_argument("--athlete", required=True)
    pbe.add_argument("--name")
    pbe.add_argument("--swim-km", type=float, default=0, dest="swim_km")
    pbe.add_argument("--bike-km", type=float, default=0, dest="bike_km")
    pbe.add_argument("--run-km", type=float, default=0, dest="run_km")
    pbe.add_argument("--clear", action="store_true")

    prp2 = sub.add_parser("race-prompt", help="record the athlete's answer to the fuelling / heat suggestion")
    prp2.add_argument("--athlete", required=True)
    prp2.add_argument("--topic", required=True, choices=["nutrition", "heat"])
    prp2.add_argument("--answer", required=True, choices=["yes", "no"])

    prs = sub.add_parser("race-setup", help="plan the block for the next A-race (preview; --apply to write it)")
    prs.add_argument("--athlete", required=True)
    prs.add_argument("--apply", action="store_true")
    prs.add_argument("--into-taper", type=float, dest="into_taper",
                     help="the athlete's own total Fitness into the taper (overrides the level default; kept)")

    pgs = sub.add_parser("goal-setup", help="a goal without a race: keep fitness, FTP, run / swim faster, fitter (preview; --apply)")
    pgs.add_argument("--athlete", required=True)
    pgs.add_argument("--goal", choices=list(_goals.GOAL_ORDER))
    pgs.add_argument("--sports", help="e.g. bike,run (default: the profile's sports)")
    pgs.add_argument("--start", help="Monday block 1 starts (default: the coming Monday)")
    pgs.add_argument("--clear", action="store_true", help="remove the goal")
    pgs.add_argument("--block-weeks", type=int, dest="block_weeks",
                     help="block length: 4 = 3 build + 1 easier (default), 5 = 4 + 1")
    pgs.add_argument("--apply", action="store_true")

    prl = sub.add_parser("race-level", help="the athlete's level for their race and its numbers (blueprint §4.5)")
    prl.add_argument("--athlete", required=True)

    pnp = sub.add_parser("windowed-np", help="NP for one segment of a ride, reconciled against ICU's own recorded NP")
    pnp.add_argument("--athlete", required=True)
    pnp.add_argument("--activity-id", required=True, dest="activity_id")
    pnp.add_argument("--start", type=int, required=True, help="segment start, seconds from ride start")
    pnp.add_argument("--end", type=int, required=True, help="segment end, seconds from ride start")

    pwb = sub.add_parser("wbal", help="W' balance (Skiba model) swept across a CP band, for one activity")
    pwb.add_argument("--athlete", required=True)
    pwb.add_argument("--activity-id", required=True, dest="activity_id")
    pwb.add_argument("--cp-low", type=float, required=True, dest="cp_low")
    pwb.add_argument("--cp-high", type=float, required=True, dest="cp_high")
    pwb.add_argument("--wprime-j", type=float, required=True, dest="wprime_j",
                     help="anaerobic work capacity, joules — known/measured, never guessed")
    pwb.add_argument("--cp-step", type=int, dest="cp_step", help="CP sweep increment, watts (default 5)")

    args = p.parse_args()
    handler = {"tss": cmd_tss, "session-for-load": cmd_session_for_load,
               "session-load": cmd_session_load, "sum": cmd_sum,
               "week-tss": cmd_week_tss, "week-caption": cmd_week_caption, "project": cmd_project,
               "required-tss": cmd_required_tss, "validate": cmd_validate,
               "render-workout": cmd_render_workout, "fuel-target": cmd_fuel_target,
               "race-fuelling": cmd_race_fuelling, "fuel-check": cmd_fuel_check,
               "wetsuit": cmd_wetsuit, "race-predict": cmd_race_predict,
               "ctl-sweep": cmd_ctl_sweep,
               "sweat-rate": cmd_sweat_rate, "log-strength": cmd_log_strength,
               "windowed-np": cmd_windowed_np, "wbal": cmd_wbal,
               "post-race-ready": cmd_post_race_ready,
               "fitness-choice": cmd_fitness_choice,
               "bespoke-event": cmd_bespoke_event, "race-level": cmd_race_level,
               "race-prompt": cmd_race_prompt, "race-setup": cmd_race_setup,
               "goal-setup": cmd_goal_setup}[args.cmd]
    try:
        result = handler(args)
    except SystemExit:
        raise
    except Exception as e:
        print(_err(f"{type(e).__name__}: {e}"))
        sys.exit(1)
    if args.cmd in _RECOMPUTE_COMMANDS:
        # No-ops outside a chat turn (CC_ATHLETE_SCOPE / CC_TURN_ID unset), so a hand
        # run at a terminal behaves exactly as before.
        replan_gate.mark_recomputed(os.environ.get("CC_ATHLETE_SCOPE", ""),
                                    os.environ.get("CC_TURN_ID", ""))
    print(json.dumps(result, indent=1))


if __name__ == "__main__":
    main()
