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


def planning_brief(slug: str, cfg: dict | None = None, today: date | None = None) -> dict:
    today = today or date.today()
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
    try:
        from icu_api import IcuClient
        w = IcuClient(cfg["icu_athlete_id"], cfg["icu_api_key"]).get_wellness(days=3)
        ctl = round(float(w[-1].get("ctl") or 0), 1) if w else None
    except Exception:
        pass
    req = pt.required_tss(cfg, ctl, today=today) if ctl else {}

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

    return {
        "athlete": slug,
        "event": ekey, "event_unknown": ekey is None,
        "phase": phase_name, "week_in_phase": week_in_phase,
        "weekly_tss_target": req.get("recommended_weekly_tss"),
        "tid_low_mod_high": event.get("tid", {}).get(phase_name) or event.get("tid", {}).get("base"),
        "distribution_by_sport": ph.get("distribution"),
        "emphasis": event.get("emphasis", []),
        "brick": event.get("brick"),
        "day_rules": cfg.get("day_rules"),
        "thresholds": {"ftp": thresh["ftp_watts"], "run": thresh["run_threshold_per_km"],
                       "swim_css": thresh["swim_css_per_100m"]},
        "available_sessions": available,
        "dosing_note": ("Start quality at this_week dose; ramp_in=true means first exposure — "
                        "ease in. Respect TID share + day_rules. No type outside available_sessions."),
    }


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--athlete", required=True)
    args = ap.parse_args()
    print(json.dumps(planning_brief(args.athlete), indent=1, ensure_ascii=False))
