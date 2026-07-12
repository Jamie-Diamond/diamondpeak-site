#!/usr/bin/env python3
"""Layer 0/1 accessor — assembles the deterministic PLANNING BRIEF for an athlete.

This is the bridge between the encoded methodology (config/session-library.json) and
Stage-1 (the LLM that proposes the week). It resolves — with NO LLM — everything the
proposal must respect: event, phase, week-in-phase, the weekly TSS target, the per-sport
intensity distribution, the allowed session menu for the phase, the event's emphasised
sessions, and THIS WEEK's concrete progression for each available quality type (with the
ramp-in 'intro' step on first exposure). Stage-1 then proposes sessions within this brief;
Stage-2 (plan_builder) renders/validates.

Determinism here = the dosing gates from the methodology (§1b): phase sets the menu,
distribution caps the quality share, week-in-phase sets the progression stage.
"""
from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE / "ironman-analysis"))
sys.path.insert(0, str(BASE / "lib"))

from primitives.blueprint import current_phase            # noqa: E402
import plan_tools as pt                                    # noqa: E402
import thresholds as th                                    # noqa: E402

LIBRARY = BASE / "config" / "session-library.json"
ATHLETES = BASE / "config" / "athletes.json"
_PHASE_ORDER = ["base", "build", "build_late", "specific", "peak", "taper"]


def load_library() -> dict:
    return json.loads(LIBRARY.read_text())


def event_key(cfg: dict, profile: dict | None = None) -> str | None:
    """Map an athlete's race to a library event key from race_distance/race_name."""
    s = " ".join(str(x or "").lower() for x in (
        cfg.get("race_distance"), cfg.get("race_name"),
        (profile or {}).get("race_distance"), (profile or {}).get("race_name")))
    if "70.3" in s or "half iron" in s or "half-iron" in s or "ironman 70" in s:
        return "70_3"
    if "ironman" in s or ("full" in s and "iron" in s) or s.strip().startswith("im "):
        return "ironman"
    if "olympic" in s or "standard distance" in s:
        return "olympic"
    if "half mara" in s or "half-mara" in s or "21.1" in s:
        return "half_marathon"
    if "marathon" in s:
        return "marathon"
    if "10k" in s or "10 k" in s:
        return "10k"
    if "5k swim" in s or "5km swim" in s or "swim 5" in s:
        return "swim_5k"
    if "5k" in s or "5 k" in s:
        return "5k"
    if "sportive" in s or "gran fondo" in s or "granfondo" in s:
        return "sportive"
    return None


def _phase_at_or_before(p: str) -> int:
    p = "build" if p == "build_late" else p
    return _PHASE_ORDER.index(p) if p in _PHASE_ORDER else 0


def _resolve_progression(stype: dict, eff_week: int) -> dict | None:
    """This week's concrete dose, where eff_week = weeks since this type was UNLOCKED
    (1 = first exposure → ramp-in via 'intro'). Indexing by unlock-week, not phase-week,
    is what stops a late-unlocked type (e.g. VO2) jumping straight to its hardest dose."""
    prog = stype.get("progression")
    if not prog:
        return None
    if eff_week <= 1 and stype.get("intro"):
        return {**stype["intro"], "ramp_in": True}
    return prog[min(max(eff_week, 1) - 1, len(prog) - 1)]


def reconcile_day_rules(default_rules: dict | None, availability: dict | None,
                        *, run_limited: bool = False) -> dict | None:
    """Reconcile the athlete's DEFAULT weekly day-shape against THIS week's
    availability (Phase 5a). The defaults are a sensible starting shape; the Sunday
    plan flexes them to the days actually available, and they stay ad-hoc adjustable
    (drop/update athletes/<slug>/this-week-availability.json any time).

    availability keys (all optional): swim_days / bike_days / run_days (replace the
    default day list for that sport this week) and unavailable_days (weekday abbrevs
    removed from every sport). For a RUN-LIMITED athlete the rehab structure is a
    FLOOR: swim_focus days are always kept, and run frequency is never increased
    beyond the default - reconciliation may relieve, never override, the spacing.

    NOTE: the deterministic validator (plan_builder -> validate_week) reads day_rules
    from athletes.json directly, so availability that only NARROWS/REMOVES days is
    validator-safe today; MOVING a sport to a new day is honoured by the proposer but
    would need plan_builder to consume the reconciled rules to also pass validation.
    """
    import json as _json
    dr = _json.loads(_json.dumps(default_rules or {}))
    if not availability:
        return default_rules
    for key in ("swim_days", "bike_days", "run_days"):
        v = availability.get(key)
        if isinstance(v, list):
            dr[key] = list(v)
    for d in (availability.get("unavailable_days") or []):
        for key in ("swim_days", "bike_days", "run_days"):
            if isinstance(dr.get(key), list):
                dr[key] = [x for x in dr[key] if str(x).lower() != str(d).lower()]
    if run_limited:
        base = default_rules or {}
        sf = base.get("swim_focus") or {}
        if sf and isinstance(dr.get("swim_days"), list):
            for wd in sf:
                if wd not in dr["swim_days"]:
                    dr["swim_days"].append(wd)
        if isinstance(base.get("run_days"), list) and isinstance(dr.get("run_days"), list):
            if len(dr["run_days"]) > len(base["run_days"]):
                dr["run_days"] = dr["run_days"][:len(base["run_days"])]
    return dr


def planning_brief(slug: str, cfg: dict | None = None, today: date | None = None,
                   plan_start: date | None = None, availability: dict | None = None) -> dict:
    today = today or date.today()
    plan_start = plan_start or today
    if cfg is None:
        cfg = json.loads(ATHLETES.read_text())[slug]
    lib = load_library()

    profile = {}
    pp = BASE / "athletes" / slug / "profile.json"
    if pp.exists():
        try:
            profile = json.loads(pp.read_text())
        except Exception:
            pass

    ekey = event_key(cfg, profile)
    event = lib["events"].get(ekey, {})

    bp = pt._load_blueprint(slug)
    ph = current_phase(bp, today) or {}
    phase_name = (ph.get("name") or "base").lower()
    phase_name = "base" if phase_name not in lib["phases"] else phase_name
    start = ph.get("start")
    week_in_phase = (max(0, (today - date.fromisoformat(start[:10])).days) // 7 + 1) if start else 1

    # weekly TSS target (deterministic)
    thresh = th.get_thresholds(slug, cfg)
    ctl = None
    # Run CAPS (max, not target): weekly mileage and longest single run each capped at
    # their highest of the last 4 weeks × 1.15 (Jamie's rule — applies to both).
    weekly_mileage_cap_km = None
    weekly_run_min_cap = None
    long_run_cap_min = None
    last_week_tss = None
    try:
        from icu_api import IcuClient
        import datetime as _dt
        from collections import defaultdict
        _c = IcuClient(cfg["icu_athlete_id"], cfg["icu_api_key"])
        w = _c.get_wellness(days=3)
        ctl = round(float(w[-1].get("ctl") or 0), 1) if w else None
        wk_km, wk_longest = defaultdict(float), defaultdict(float)
        wk_tss = defaultdict(float)          # ALL sports — feeds the deload miss-trigger
        for a in _c.get_training_history(days=35):
            d = _dt.date.fromisoformat((a.get("start_date_local") or "")[:10])
            iso = d.isocalendar()[:2]
            wk_tss[iso] += float(a.get("icu_training_load") or 0)
            if (a.get("type") or "") != "Run":
                continue
            wk_km[iso] += (a.get("distance") or 0) / 1000
            wk_longest[iso] = max(wk_longest[iso], (a.get("moving_time") or 0) / 60)
        cur = today.isocalendar()[:2]
        prev = (today - _dt.timedelta(days=7)).isocalendar()[:2]
        if prev in wk_tss or wk_km or wk_longest:   # history fetch succeeded
            last_week_tss = round(wk_tss.get(prev, 0.0), 1)
        # Caps from the shared helper (plan_tools.run_caps) so the brief and the
        # validators can never drift apart again (audit P1-9): weekly km x1.10
        # per rules.md (25 km floor = top of the "normal" band), long run x1.15.
        _caps = pt.run_caps(_c, today)
        weekly_mileage_cap_km = _caps.get("weekly_km_cap")
        weekly_run_min_cap = _caps.get("weekly_min_cap")
        long_run_cap_min = _caps.get("long_run_min_cap")
    except Exception:
        pass
    req = pt.required_tss(cfg, ctl, today=today, last_week_tss=last_week_tss) if ctl else {}

    # phase menu ∩ event sports; resolve this-week progression for each quality type
    phase_cfg = lib["phases"].get(phase_name, {})
    menu = phase_cfg.get("menu", [])
    forbid = set(phase_cfg.get("forbid", []))
    vo2_late = phase_cfg.get("vo2") == "late_only"
    sports = event.get("sports") or ["swim", "bike", "run"]

    available = {}
    for sport in sports:
        types = lib["session_types"].get(sport, {})
        rows = []
        for name, st in types.items():
            if name.startswith("_") or not isinstance(st, dict):
                continue   # skip metadata keys (e.g. _pool_note)
            if name in forbid:
                continue
            vo2_unlock = (name == "vo2" and vo2_late)
            if vo2_unlock and week_in_phase < 3:
                continue
            if _phase_at_or_before(st.get("min_phase", "base")) > _phase_at_or_before(phase_name):
                continue
            row = {"type": name, "zone": st.get("zone"), "if": st.get("if"), "system": st.get("system")}
            # weeks since this type was unlocked: VO2 unlocks at build wk3, others at phase wk1.
            unlock_wk = 3 if vo2_unlock else 1
            eff_week = max(1, week_in_phase - unlock_wk + 1)
            dose = _resolve_progression(st, eff_week)
            if dose:
                row["this_week"] = dose
            rows.append(row)
        available[sport] = rows

    rp = cfg.get("run_protocol") or {}
    # --- Phase 5: per-athlete limiter signal (single-source) ------------------
    # Two limiter signals that ALREADY exist in the athlete's config/profile drive
    # every sport/intensity accommodation downstream (close_to_target's TSS-closing
    # lever, the minimum-quality floor, the availability reconciliation): an athlete
    # is "run-limited" if they carry an injury OR their protocol forbids run quality
    # (Jamie's ankle rehab). Single-sport athletes (Calum) always fall through to
    # bike-only closure regardless. NEVER a new global flag.
    injuries = profile.get("injuries") or []
    run_limited = bool(injuries) or (rp.get("quality_allowed") is False)
    single_sport = len(available) <= 1
    day_rules_effective = reconcile_day_rules(cfg.get("day_rules"), availability,
                                              run_limited=run_limited)
    # Conditional TSS-closing guidance for the Stage-1 proposer (Phase 5b): a limited
    # or single-sport athlete closes gaps with BIKE volume; everyone else spreads the
    # closure across sports and MUST carry the phase quality share, not easy bike alone.
    if run_limited:
        _closure = ("Close any weekly-TSS gap with BIKE volume, never a short week; runs stay EASY "
                    "(no run quality - ankle). Hit the OVERALL phase Z3+ budget (tid_low_mod_high) by "
                    "putting the quality the run cannot take onto the BIKE (and swim): SHAPE easy bike "
                    "minutes into tempo/threshold/VO2 WITHIN the same total load - do not add volume "
                    "to add intensity. ")
    elif single_sport:
        _closure = ("Close any weekly-TSS gap with BIKE volume, never a short week. Hit the OVERALL "
                    "phase Z3+ budget (tid_low_mod_high) on the bike by shaping easy minutes into "
                    "quality within the same total - the OVERALL budget governs, not the per-sport "
                    "bike row. ")
    else:
        _closure = ("Close any weekly-TSS gap with a BALANCED spread across the available sports. Hit "
                    "the OVERALL phase Z3+ budget (tid_low_mod_high) across the WEEK; the per-sport "
                    "TIDs are a SOFT preference for how to distribute it, NOT additive targets to sum. "
                    "When quality_allowed is true, SHAPE run quality within the mileage/long-run caps "
                    "(convert part of an EASY run to a short tempo/threshold; never add run minutes or "
                    "exceed the caps). If a sport cannot carry its share (caps/limits), move that "
                    "quality to the other capable sports so the OVERALL budget is still met. ")
    dosing_note = ("Build to weekly_tss_target - weekly_tss_floor is a HARD minimum (below "
                   "it the week detrains the athlete and validation rejects it; only "
                   "deload/taper weeks may sit under maintenance). " + _closure +
                   "PROTECT the long ride (~long_ride_target_min). Total run mileage must "
                   "NOT exceed weekly_run_mileage_cap_km and the longest run must NOT exceed "
                   "long_run_cap_min (these are MAX ceilings, +10-15% on the highest of the "
                   "last 4 weeks). The OVERALL phase TID (tid_low_mod_high) is the authoritative "
                   "intensity budget; the per-sport distribution_by_sport rows are a soft preference "
                   "for spending it and may be reallocated across sports under caps/limits. Obey "
                   "run_protocol (no quality if quality_allowed=false) and hard_rules. No type outside "
                   "available_sessions.")
    # Long-ride target (the protected key session): event bike demand × factor, capped.
    bike_min = (cfg.get("race_target_splits") or {}).get("bike_min")
    lr_factor = event.get("long_ride_factor", 0.9)
    long_ride_min = min(int(round((bike_min or 200) * lr_factor / 15) * 15),
                        240 if ekey == "ironman" else 300)
    # Athlete hard rules (protocol prose) — so the proposer obeys them, like the old coach did.
    hard_rules = ""
    rp_path = BASE / "athletes" / slug / "reference" / "rules.md"
    if rp_path.exists():
        try:
            hard_rules = rp_path.read_text()[:3500]
        except Exception:
            pass

    # Strength programme (opt-in via profile.strength_programme) — ported from the old
    # generate-plan.py so the two-stage engine doesn't silently drop Jamie's signed-off
    # (10 Jun) sessions or the EVERY-WEEK equipment ask. The proposer includes the
    # sessions (tier-C content by default); the weekly message carries the equipment ask.
    strength = None
    if profile.get("strength_programme"):
        smd = BASE / "blueprints" / "strength.md"
        strength = {
            "sessions_per_week": (cfg.get("day_rules") or {}).get("strength_max") or 2,
            "default_tier": "C (bodyweight + band — always possible, so strength is never dropped)",
            "placement": ("Wednesday spare slot first; a 2nd session after a swim day; "
                          "NEVER the day before the long ride; >=8h from any quality bike/run."),
            "content_each": "warm-up / main lifts / ankle block / core — write this into notes.",
            "guide": smd.read_text()[:1800] if smd.exists() else "",
        }

    # Durability (ported from generate-plan.py) — fatigue resistance is trained by working at
    # intensity on tired legs, not Z2 hours alone; in build/specific/peak the long ride must
    # FINISH WITH WORK. (Jamie's 2025 limiter: -60W on lap 2, 14.5% decoupling.)
    durability = None
    if phase_name in ("build", "build_late", "specific", "peak"):
        durability = ("The weekly LONG RIDE must FINISH WITH WORK, not just accumulate hours: put the "
                      "final portion at race intensity (early build = last 2x20min at race IF; progress "
                      "toward a continuous 60-90min race-IF finish by peak) and write it into the notes. "
                      "The Z2 body stays; only the closing block is at intensity (counts to the quality "
                      "share). Long RUNS keep their structure — no quality added unless the rules allow.")

    # Menstrual-cycle forecast (tracking athletes only) — Python-computed from the bot-logged
    # anchor, aligned to the PLANNED week. Shapes WHERE quality lands, never the total TSS.
    menstrual_forecast = None
    if profile.get("menstrual_tracking"):
        try:
            import menstrual as _mens
            _cl = _mens.forecast_block(slug, plan_start, 14, profile=profile)
            if _cl:
                menstrual_forecast = {
                    "phase_windows": _cl,
                    "apply": ("Where the day rules leave a choice, place the hardest quality "
                              "(threshold/VO2/race-pace) on FOLLICULAR/OVULATION days and prefer "
                              "Z2/easy/technique on MENSTRUAL days. Keep menstrual-day sessions but "
                              "frame them RPE-led. On LUTEAL days expect higher RPE/core temp — don't "
                              "stack the two hardest sessions back-to-back in late luteal; heat compounds "
                              "with luteal. Never break a HARD day rule for this, and do NOT cut the "
                              "week's TSS target because of cycle phase."),
                }
        except Exception:
            pass

    return {
        "athlete": slug,
        "event": ekey, "event_unknown": ekey is None,
        "phase": phase_name, "week_in_phase": week_in_phase,
        "weekly_tss_target": req.get("recommended_weekly_tss"),
        # HARD lower bound: min(phase requirement, 7 x CTL maintenance); 0 on
        # deload/taper. validate_week fails the week below it — a training week
        # must train the athlete.
        "weekly_tss_floor": req.get("weekly_tss_floor"),
        "maintenance_weekly_tss": req.get("maintenance_weekly_tss"),
        # deload/taper/normal — the note explains a reduced target so the Stage-1
        # LLM shapes the week accordingly instead of quietly padding volume back.
        "week_type": req.get("week_type") or phase_name,
        "week_note": req.get("note"),
        # Taper holds INTENSITY: taper row if configured, else the peak row —
        # never the base 85/10/5 mostly-easy split (audit P0-2: reverting taper
        # intensity to base is the opposite of taper consensus).
        "tid_low_mod_high": (event.get("tid", {}).get(phase_name)
                             or (event.get("tid", {}).get("peak") if phase_name == "taper" else None)
                             or event.get("tid", {}).get("base")),
        "distribution_by_sport": ph.get("distribution"),
        "emphasis": event.get("emphasis", []),
        "brick": event.get("brick"),
        "day_rules": day_rules_effective,
        "day_rules_default": cfg.get("day_rules"),
        "availability_applied": bool(availability),
        "injuries": injuries,
        "run_limited": run_limited,
        "single_sport": single_sport,
        "run_protocol": rp,
        "weekly_run_mileage_cap_km": weekly_mileage_cap_km,   # MAX (highest of last 4 wks ×1.15)
        "weekly_run_min_cap": weekly_run_min_cap,             # MAX weekly run MINUTES (validate_week cap)
        "long_run_cap_min": long_run_cap_min,                 # MAX single long run (×1.15)
        "long_ride_target_min": long_ride_min,
        "long_swim_target_m": event.get("long_swim_m"),  # OVERDISTANCE weekly long swim (70.3 ~3000, IM ~4500)
        "race_sim_m": event.get("swim_m"),               # EXACT race distance — race-sim rehearsal (70.3 1900, IM 3800)
        "strength_programme": strength,
        "durability": durability,
        "menstrual_forecast": menstrual_forecast,
        "thresholds": {"ftp": thresh["ftp_watts"], "run": thresh["run_threshold_per_km"],
                       "swim_css": thresh["swim_css_per_100m"]},
        "available_sessions": available,
        "hard_rules": hard_rules,
        "dosing_note": dosing_note,
    }


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--athlete", required=True)
    args = ap.parse_args()
    print(json.dumps(planning_brief(args.athlete), indent=1, ensure_ascii=False))
