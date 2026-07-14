#!/usr/bin/env python3
"""Stage-1 generator (two-stage planner) — DRY-RUN by default.

Pipeline: planning_brief (deterministic) -> LLM proposes the week's SHAPE only
(sport + time-at-intensity segments + notes, NO load/fuelling/structure maths) ->
plan_builder.build_sessions (deterministic render + load + fuel + validate) -> audit.

The LLM is tightly constrained: it may only use the session types and this-week doses
in the brief, must respect day_rules and the TID, and outputs pure JSON. All numbers
come from code. Nothing is pushed unless --push is given AND validation is clean.

  python3 stage1-plan.py --athlete kathryn            # dry-run, prints the built week
  python3 stage1-plan.py --athlete kathryn --push     # push (only if validation clean)
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE / "lib"))
sys.path.insert(0, str(BASE / "ironman-analysis"))

import claude_call                    # noqa: E402
import session_library as sl          # noqa: E402
import plan_builder as pb             # noqa: E402
from primitives.planned_tss import segment_if  # noqa: E402

_QUALITY_IF = 0.85   # a session with any segment at/above this is "quality" (fixed); else endurance
_FLEX_IF    = 0.75   # TSS-closing lever: only TRUE Z1-Z2 volume may be stretched.
                     # Tempo at IF 0.76-0.84 is "endurance" by the line above but
                     # stretching it to close a TSS shortfall inflates the grey
                     # zone (audit P2-8) — quality dose must come from the plan,
                     # never from gap-filling arithmetic.


def _is_endurance(sess: dict) -> bool:
    """True if the session is pure endurance (no quality main set) → its duration is the
    flexible lever for hitting the weekly TSS target. Quality sessions stay fixed."""
    segs = sess.get("segments") or []
    if not segs:
        return False
    sport = sess.get("sport", "")
    return all((seg.get("if") if seg.get("if") is not None else segment_if(sport, seg.get("zone")))
               < _QUALITY_IF for seg in segs)


def _set_total_minutes(sess: dict, target_min: int):
    """Scale a session's segments to a fixed total duration (for clamping key sessions)."""
    segs = sess.get("segments") or []
    cur = sum(s.get("minutes", 0) for s in segs)
    if cur > 0 and target_min and abs(cur - target_min) > 1:
        f = target_min / cur
        for s in segs:
            s["minutes"] = max(5, round(s["minutes"] * f))


def _is_long_run(s):
    return (s.get("sport") or "").lower() == "run" and "long" in (s.get("name") or "").lower()


def _is_long_ride(s):
    return (s.get("sport") or "").lower() in ("ride", "bike", "brick") and "long" in (s.get("name") or "").lower()


def _clamp_runs_to_cap(proposal: dict, mileage_cap_km: float, lr_cap, pace: float, run_min_cap=None, protect_long=False):
    """Scale ALL runs down so weekly run MINUTES stay under the ceiling (never up -
    mileage is a MAX), then re-clamp the long run to its own cap. Prefers the explicit
    minute cap (what validate_week enforces) over km x pace, so the closure lever and
    the validator can never drift (the km and minute caps use different implied paces)."""
    run_sessions = [s for s in proposal["sessions"] if (s.get("sport") or "").lower() == "run"]
    if not run_sessions:
        return
    def _tot():
        return sum(sum(sg.get("minutes", 0) for sg in s.get("segments", [])) for s in run_sessions)
    # Clamp to the STRICTER of the minute cap (validate_week) and the km cap turned
    # into minutes at the same pace audit_built uses (run_min/pace <= cap_km); their
    # floors diverge at low volume, so honouring only one can still breach the other.
    # int() floors it so the ceiling holds after rounding.
    _caps = [c for c in (run_min_cap, (mileage_cap_km * pace) if mileage_cap_km else None) if c]
    cap_min = int(min(_caps)) if _caps else None
    cur_min = _tot()
    if cap_min and cur_min > cap_min and cur_min > 0:
        long_min = sum(sum(sg.get("minutes", 0) for sg in s.get("segments", []))
                       for s in run_sessions if _is_long_run(s))
        nonlong = [s for s in run_sessions if not _is_long_run(s)]
        nonlong_min = sum(sum(sg.get("minutes", 0) for sg in s.get("segments", [])) for s in nonlong)
        room = cap_min - long_min
        if protect_long and room >= 0 and nonlong_min > room and nonlong_min > 0:
            # protect a PROGRESSING long run: shrink only the easy runs to fit the ceiling
            f = room / nonlong_min
            for s in nonlong:
                for seg in s.get("segments", []):
                    seg["minutes"] = max(15, round(seg["minutes"] * f))
        else:
            f = cap_min / cur_min
            for s in run_sessions:
                for seg in s.get("segments", []):
                    seg["minutes"] = max(15, round(seg["minutes"] * f))
    for s in run_sessions:
        if _is_long_run(s) and lr_cap:
            cur = sum(seg.get("minutes", 0) for seg in s.get("segments", []))
            if cur > lr_cap:
                _set_total_minutes(s, lr_cap)
    # Final trim: rounding and the 15-min floor can leave a few minutes over the ceiling;
    # shave the overage off the largest non-long run so the cap ALWAYS holds (caps win).
    if cap_min:
        guard = 0
        while _tot() > cap_min and guard < 100:
            guard += 1
            cand = [s for s in run_sessions if not _is_long_run(s)
                    and sum(sg.get("minutes", 0) for sg in s.get("segments", [])) > 15]
            if not cand:
                break
            s = max(cand, key=lambda s: sum(sg.get("minutes", 0) for sg in s.get("segments", [])))
            segs = [sg for sg in s.get("segments", []) if sg.get("minutes", 0) > 15]
            if not segs:
                break
            sg = max(segs, key=lambda x: x.get("minutes", 0))
            sg["minutes"] = max(15, sg["minutes"] - max(1, _tot() - cap_min))


def close_to_target(athlete: str, proposal: dict, target, brief: dict, tol=0.06, max_iter=5):
    """Reliable TSS — but PROTECT key sessions. The long run and long ride are CLAMPED to
    their targets (never used to absorb TSS); quality is fixed by dose; only the OTHER easy
    endurance (easy runs, 2nd/easy rides) is scaled to land the week on target."""
    lr_cap = brief.get("long_run_cap_min")            # MAX long run
    lr_target = brief.get("long_run_target_min")      # PROGRESSING long-run target (near cap) or None
    lrd_min = brief.get("long_ride_target_min")
    mileage_cap_km = brief.get("weekly_run_mileage_cap_km")  # MAX weekly run km
    run_min_cap = brief.get("weekly_run_min_cap")     # MAX weekly run MINUTES (validate_week's cap)
    PACE = 5.3  # ~easy min/km (matches the audit's km estimate)

    # 1. Long ride clamped to its target. Long run: if the athlete has a PROGRESSING target
    #    (configured long-run floor) build it TO that target (up or down, bounded by the cap)
    #    so it CLIMBS instead of plateauing short; otherwise just clamp DOWN to the cap
    #    (never up) - unchanged for athletes without a target.
    for s in proposal["sessions"]:
        if _is_long_ride(s) and lrd_min:
            _set_total_minutes(s, lrd_min)
        elif _is_long_run(s):
            cur = sum(seg.get("minutes", 0) for seg in s.get("segments", []))
            if lr_target:
                _set_total_minutes(s, min(lr_target, lr_cap) if lr_cap else lr_target)
            elif lr_cap and cur > lr_cap:
                _set_total_minutes(s, lr_cap)

    # 2. Total run mileage is a CEILING: if over the weekly cap, scale runs down to it
    #    (protecting a progressing long run - shrink the easy runs first).
    if mileage_cap_km:
        _clamp_runs_to_cap(proposal, mileage_cap_km, lr_cap, PACE, run_min_cap,
                           protect_long=bool(lr_target))

    # 3. Close the weekly TSS gap. WHICH sessions absorb it is athlete-conditional
    #    (Phase 5b): a run-limited athlete (injury / no run quality, e.g. Jamie's ankle
    #    rehab) or a single-sport athlete (e.g. Calum) closes with BIKE volume only and a
    #    protected long ride; everyone else spreads the closure across BOTH bike and run
    #    easy endurance so the week is not ballooned with easy bike alone (Kathryn's skew).
    bike_only_closure = bool(brief.get("run_limited")) or bool(brief.get("single_sport"))
    def _all_true_z2(sess):
        segs = sess.get("segments") or []
        return bool(segs) and all(
            (sg.get("if") if sg.get("if") is not None else segment_if(sess.get("sport", ""), sg.get("zone")))
            <= _FLEX_IF for sg in segs)
    def flex(s):
        sport = (s.get("sport") or "").lower()
        if sport in ("bike", "ride"):
            # bike lever stays TRUE Z2 only (never stretch bike tempo into the grey zone
            # to fake TSS - audit P2-8); the long ride is protected.
            return _is_endurance(s) and _all_true_z2(s) and not _is_long_ride(s)
        if sport == "run" and not bike_only_closure:
            # non-limited athletes ALSO close with easy-run endurance so the gap is spread
            # across sports, not dumped onto easy bike alone; the long run is capped, never
            # used to absorb TSS, and run minutes are re-clamped to the mileage cap each
            # iteration below so this can never breach the ceiling.
            return _is_endurance(s) and not _is_long_run(s)
        return False
    built = pb.build_sessions(athlete, proposal)
    if not target:
        return built
    for _ in range(max_iter):
        total = built["total_tss"]
        if abs(total - target) <= tol * target:
            break
        flex_load = sum(b["load_target"] for s, b in zip(proposal["sessions"], built["sessions"]) if flex(s))
        if flex_load <= 0:
            break
        factor = max(0.4, min(2.2, (flex_load + (target - total)) / flex_load))
        for s in proposal["sessions"]:
            if flex(s):
                for seg in s.get("segments", []):
                    seg["minutes"] = max(15, round(seg["minutes"] * factor))
        # When runs share the closure, keep them under the weekly mileage ceiling each
        # iteration (mileage is a MAX) so distributing the gap can never breach the run
        # cap; the uncapped bike absorbs any remainder on the next pass.
        if not bike_only_closure and mileage_cap_km:
            _clamp_runs_to_cap(proposal, mileage_cap_km, lr_cap, PACE, run_min_cap)
        built = pb.build_sessions(athlete, proposal)
    return built

CLAUDE = shutil.which("claude") or "/usr/bin/claude"
PROJECT_DIR = str(BASE.parent)


def _next_monday(today: date) -> date:
    return today + timedelta(days=(7 - today.weekday()) % 7 or 7)


# Robust per-segment IF: the proposal may carry an explicit "if", a semantic zone
# (tempo/threshold/vo2 - handled by segment_if), or a bare TID band label (Z3/Z4/Z5,
# which segment_if does NOT map and would score as the sport default). The band-label
# fallback keeps quality from being misread as easy regardless of how the LLM encoded it.
_ZLABEL_IF = {
    "bike": {"z1": 0.55, "z2": 0.65, "z3": 0.80, "z4": 0.95, "z5": 1.05},
    "run":  {"z1": 0.60, "z2": 0.83, "z3": 0.83, "z4": 0.97, "z5": 1.06},
    "swim": {"z1": 0.60, "z2": 0.72, "z3": 0.85, "z4": 1.00, "z5": 1.08},
}


def _seg_if(sport: str, seg: dict) -> float:
    if seg.get("if") is not None:
        return seg["if"]
    sp = ("bike" if any(k in (sport or "").lower() for k in ("bike", "ride", "brick"))
          else "run" if "run" in (sport or "").lower()
          else "swim" if "swim" in (sport or "").lower() else "")
    z = (seg.get("zone") or "").lower().strip()
    if sp and z in _ZLABEL_IF[sp]:
        return _ZLABEL_IF[sp][z]
    return segment_if(sport, seg.get("zone"))


def _overall_z3plus(proposal: dict):
    """(Z3+ minutes, total minutes) across ALL sports, using each sport's own Z2/Z3
    boundary (bike/brick Z3 starts ~0.76; run and swim ~0.85)."""
    z3 = tot = 0.0
    for s in proposal.get("sessions", []):
        cut = 0.76 if (s.get("sport") or "").lower() in ("bike", "ride", "brick") else 0.85
        for seg in (s.get("segments") or []):
            m = seg.get("minutes", 0) or 0
            tot += m
            if (_seg_if(s.get("sport", ""), seg) or 0) >= cut:
                z3 += m
    return z3, tot


# High-zone (Z4-5 / VO2) sub-ceiling tolerance, shared by the advisory and the picker so
# both flag/deprioritise the SAME attempts. Mirrors validate_plan.check_intensity_budget.
_HIGH_BAND_PP = 3.0


def _overall_high(proposal: dict):
    """(Z4-5 / VO2 minutes, total minutes) across ALL sports, using the canonical Z3/Z4
    boundary IF >= 0.90 (debrief.py Z4 = 0.90-1.05; unified across sports). This is the
    high-zone half of the Z3+ lump: protecting the low-VO2 IM shape needs the split inside
    Z3+, not just the total (see the high-zone sub-ceiling in check_intensity_budget)."""
    high = tot = 0.0
    for s in proposal.get("sessions", []):
        for seg in (s.get("segments") or []):
            m = seg.get("minutes", 0) or 0
            tot += m
            if (_seg_if(s.get("sport", ""), seg) or 0) >= 0.90:
                high += m
    return high, tot


def _attempt_rank(brief: dict, built: dict, target, proposal: dict):
    """Tie-breakers among equally-(un)blocked attempts (lower is better): smallest overall
    intensity-budget deviation |week Z3+ - phase budget|, then high-zone (Z4-5 / VO2) excess
    over the phase ceiling, then lowest Foster monotony, then closest-to-target TSS. The
    overall budget drives SELECTION and the VO2 term stops a VO2-heavy week being chosen over
    a sweetspot week when BOTH hit the overall budget (the lump alone cannot tell them apart)."""
    tid = brief.get("tid_low_mod_high")
    budget = (float(tid[1]) + float(tid[2])) if tid and len(tid) >= 3 else None
    high_target = float(tid[2]) if tid and len(tid) >= 3 else None
    z3, tot = _overall_z3plus(proposal)
    z3_pct = (z3 / tot * 100) if tot else 0.0
    budget_dev = abs(z3_pct - budget) if budget is not None else 0.0
    high, _ = _overall_high(proposal)
    high_pct = (high / tot * 100) if tot else 0.0
    # excess over the SAME ceiling the advisory triggers on (phase high + tolerance): among
    # equal-budget attempts this prefers the sweetspot-weighted week over the VO2-heavy one.
    high_over = max(0.0, high_pct - (high_target + _HIGH_BAND_PP)) if high_target is not None else 0.0
    mono = 0.0
    for v in built.get("soft", []):
        m = re.search(r"monotony\s+([\d.]+)", v.get("msg", ""))
        if m:
            mono = max(mono, float(m.group(1)))
    tss_off = abs(built["total_tss"] - target) if target else 0.0
    return (round(budget_dev, 1), round(high_over, 1), round(mono, 2), round(tss_off, 1))


def audit_built(brief: dict, built: dict, target, proposal: dict):
    """Audit the built week. Returns (blocking, advisory).

    BLOCKING = safety ceilings + hard rules + key structure (mileage/long-run/CTL-ramp/
    TSS caps, wrong-day rules, the ankle run-quality hard-stop, a missing long ride) -
    these prevent a push. ADVISORY = quality/shape/monotony nudges - they drive the
    iterate-to-clean loop as feedback but NEVER block the push (checks advise; only
    safety ceilings block). Both lists are surfaced and fed back to the proposer."""
    import datetime as _dt
    blocking, advisory = [], []

    # Weekly TSS vs target: advisory (main gates load_on_target +/-12% and the hard weekly
    # TSS cap/floor separately) - here it only nudges the proposer toward the number.
    if target and abs(built["total_tss"] - target) > 0.08 * target:
        d = built["total_tss"] - target
        advisory.append(f"week TSS {built['total_tss']} vs target {target} ({d:+}) - "
                        f"{'add' if d < 0 else 'cut'} endurance volume")

    # Hard validate_week rules (wrong-day, TSS cap/floor, CTL ramp, run-volume caps,
    # distance/duration mismatch) are safety ceilings -> BLOCK. Soft ones (monotony,
    # intensity-distribution, strength cap) ADVISE.
    blocking += [f"rule(hard): {v['msg']}" for v in built.get("hard", [])]
    advisory += [f"rule(soft): {v['msg']}" for v in built.get("soft", [])]

    # swim_focus type on a focus day: advisory (a preference, not a safety ceiling).
    sf = (brief.get("day_rules") or {}).get("swim_focus") or {}
    if sf:
        for s in proposal.get("sessions", []):
            if (s.get("sport") or "").lower() != "swim":
                continue
            wd = _dt.date.fromisoformat(s["date"]).strftime("%a")
            allowed = sf.get(wd)
            nm = (s.get("name") or "").lower()
            if allowed and not any(a in nm or a.replace("technique", "drill")[:4] in nm for a in allowed):
                advisory.append(f"{wd} swim should be {allowed} (got '{s.get('name')}')")

    # ── RUN volume ceilings + ankle hard-stop (SAFETY -> BLOCK) ──
    runs = [s for s in built["sessions"] if s["sport"] == "Run"]
    run_min = sum(s["duration_min"] for s in runs)
    run_km = round(run_min / 5.3, 1)   # ~easy pace 5.3 min/km
    cap_km = brief.get("weekly_run_mileage_cap_km")   # MAX weekly run km ceiling
    if cap_km and run_km > cap_km:
        blocking.append(f"run mileage ~{run_km}km EXCEEDS cap {cap_km}km (+10-15% max) - cut run durations")
    rp = brief.get("run_protocol") or {}
    if rp.get("quality_allowed") is False:
        for s in proposal.get("sessions", []):
            if (s.get("sport") or "").lower() != "run":
                continue
            if any((_seg_if("run", seg) or 0) >= _QUALITY_IF for seg in (s.get("segments") or [])):
                blocking.append(f"run '{s.get('name')}' has quality intensity but run quality is NOT allowed (ankle)")
    lrc = brief.get("long_run_cap_min")   # MAX single long run
    if lrc and runs and max(s["duration_min"] for s in runs) > lrc:
        blocking.append(f"long run {max(s['duration_min'] for s in runs)}min EXCEEDS cap {lrc}min (+10-15% max)")
    lrt_run = brief.get("long_run_target_min")   # PROGRESSING target (configured athletes)
    if lrt_run and runs:
        _longest = max(s["duration_min"] for s in runs)
        if _longest < lrt_run * 0.85:
            advisory.append(f"long run {_longest}min is under the progressing target ~{lrt_run}min - "
                            f"build the long run toward its climbing cap (do not plateau short)")

    # ── LONG RIDE must be present (key session -> BLOCK) ──
    lrt = brief.get("long_ride_target_min")
    rides = [s for s in built["sessions"] if s["sport"] in ("Ride", "Bike", "Brick")]
    if lrt and (not rides or max(s["duration_min"] for s in rides) < lrt * 0.85):
        have = max((s["duration_min"] for s in rides), default=0)
        blocking.append(f"no protected long ride - longest ride {have}min < target ~{lrt}min")

    # ── INTENSITY BUDGET (ADVISORY: drives the loop, never blocks) ──
    # The athlete's OVERALL phase TID (brief.tid_low_mod_high) is the intensity budget: the
    # week's TOTAL Z3+ share must land within a band of it. The per-sport TIDs are only a
    # soft preference for HOW to spend the budget and are FUNGIBLE across sports (see the
    # dosing_note), so we check the TOTAL and never independently max each per-sport TID
    # (which double-counted and blew the total, e.g. bike 30% + swim 35% + run 22% -> 37%
    # overall vs a 20% intent). Z3+ is measured per sport with that sport's own Z2/Z3
    # boundary (bike Z3/tempo starts ~0.76; run and swim Z3 start ~0.85). A share a sport
    # cannot carry (run-limited / run-capped / single-sport) simply shifts onto the capable
    # sports; the total still governs. The ankle run-quality hard-stop above still BLOCKS.
    z3_min, tot_min = _overall_z3plus(proposal)
    high_min, _ = _overall_high(proposal)
    try:
        from primitives.validate_plan import check_intensity_budget
        for v in check_intensity_budget(z3_min, tot_min, brief.get("tid_low_mod_high"),
                                        high_min=high_min):
            advisory.append(f"rule(quality): {v.detail}")
    except Exception:
        pass

    return blocking, advisory


def build_prompt(slug: str, brief: dict, week_start: date, feedback: str = "") -> str:
    grid = "\n".join(f"  {(week_start + timedelta(days=i)).isoformat()} = "
                     f"{(week_start + timedelta(days=i)).strftime('%A')}" for i in range(7))
    return f"""You are proposing {slug}'s training week starting Monday {week_start.isoformat()}.

Output ONLY a JSON object, no prose, no markdown fence:
{{"sessions": [{{"date":"YYYY-MM-DD","sport":"Swim|Bike|Run|Brick|Strength",
  "name":"short name","notes":"coaching prose (cues, purpose)",
  "segments":[{{"minutes":N,"zone":"<zone from the menu>"}}, ...]}}]}}

HARD RULES — you propose the SHAPE only; code computes all load/fuelling/structure:
- Use ONLY session types and zones from AVAILABLE SESSIONS below. Do NOT invent types.
- For a quality session, build its main set from that type's "this_week" dose (reps×min);
  wrap with an easy warm-up and cool-down. If "ramp_in" is true, keep it conservative.
- Respect DAY RULES: swim_days/bike_days/run_days set which sports go on which day, and
  place an easy/rest day. If day_rules has "swim_focus" (or run_focus/bike_focus) mapping a
  weekday to allowed session type(s), that day's session of that sport MUST be one of those
  types — e.g. swim_focus {{"Tue":["technique","speed"],"Thu":["css"]}} means Tue swim is a
  skills/speed session and Thu swim is the CSS set, never the reverse.
- Aim the week near the WEEKLY TSS TARGET and follow the intensity split (TID = low/mod/high %).
- PROTECT THE LONG RIDE: include one Ride of ~long_ride_target_min as the week's KEY session.
- RUNS: total run mileage must NOT exceed weekly_run_mileage_cap_km (≈ minutes/5.3 km) and the
  longest run must NOT exceed long_run_cap_min — these are MAX ceilings (+10-15% on the highest of
  the last 4 weeks). Plan at or under them. If long_run_target_min is set, make the weekly LONG RUN a
  PROGRESSING session built toward it (climbing week-on-week), not a static short run - near the target,
  within the caps. If run_protocol.quality_allowed is false, EVERY run is
  easy Z2 — NO tempo/threshold/interval/vo2 run (ankle gate). Honour run_protocol format.
- OBEY hard_rules (the athlete's protocol) absolutely — they override anything else here.
- Swim sets: express in minutes (not metres). Strength: omit segments.
- SWIM ENDURANCE scales to the event: the weekly LONG swim is OVERDISTANCE — build toward
  long_swim_target_m, which is BEYOND race distance (70.3 ~3000m, IM ~4500m); overdistance is normal.
  The RACE-SIM (race_pace) is the rehearsal at EXACT race distance = race_sim_m (70.3 1900m, IM 3800m).
  CSS/speed reps stay as their progression doses.
- STRENGTH: if the brief has a non-null "strength_programme", include EXACTLY its
  sessions_per_week Strength sessions, placed per its "placement" rule, with the session
  content (warm-up / main lifts / ankle / core, default its tier) written into "notes".
  Give each Strength session "minutes": 40 (no segments, no load — code/ICU handle load).
- DURABILITY: if the brief has a non-null "durability", apply it to the long ride (closing block
  at race intensity), expressed in the long ride's segments + notes.
- MENSTRUAL: if the brief has a non-null "menstrual_forecast", follow its "apply" guidance when
  PLACING quality vs easy sessions across the week (never breaking a day rule, never cutting TSS).
- Do NOT output load_target, TSS numbers, or %FTP/pace targets — code derives them.

DATE GRID:
{grid}
{feedback}
PLANNING BRIEF (authoritative, deterministic):
{json.dumps(brief, indent=1)}
"""


def extract_json(text: str) -> dict:
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError("no JSON object found in model output")
    return json.loads(m.group(0))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--athlete", required=True)
    ap.add_argument("--push", action="store_true")
    ap.add_argument("--notify", action="store_true", help="message the athlete on completion")
    ap.add_argument("--week-start", help="Monday YYYY-MM-DD to plan (default: next Monday)")
    ap.add_argument("--model", default="claude-sonnet-5")
    ap.add_argument("--max-attempts", type=int, default=3)
    ap.add_argument("--override-json", metavar="PATH",
                    help="skip LLM generation; use this JSON file as the session proposal")
    ap.add_argument("--availability", metavar="PATH",
                    help="this week's availability JSON to flex day_rules (Phase 5a); "
                         "defaults to athletes/<slug>/this-week-availability.json if present")
    args = ap.parse_args()

    cfg = json.loads((BASE / "config" / "athletes.json").read_text())[args.athlete]
    today = date.today()
    week_start = date.fromisoformat(args.week_start) if args.week_start else _next_monday(today)
    # today=week_start, NOT the run date: the Sunday cron plans NEXT week, and
    # phase / week_in_phase / required-tss / deload detection must all be
    # evaluated for the week being planned. Planning with the run date briefed
    # next week against THIS week's (lower) requirement — the 5 Jul 2026 bug
    # that planned 581 TSS into a week needing 816.
    # Per-week availability (Phase 5a): flex the default day_rules to this week if the
    # athlete has told us their availability. Ad-hoc adjustable: an explicit --availability
    # file, else a standing athletes/<slug>/this-week-availability.json (chat can write it).
    _avail = None
    _avail_path = (Path(args.availability) if args.availability
                   else BASE / "athletes" / args.athlete / "this-week-availability.json")
    if _avail_path.exists():
        try:
            _avail = json.loads(_avail_path.read_text())
        except Exception:
            _avail = None
    brief = sl.planning_brief(args.athlete, cfg, today=week_start, plan_start=week_start,
                              availability=_avail)
    if brief.get("event_unknown"):
        print(json.dumps({"error": f"event unknown for {args.athlete} — cannot plan"}))
        sys.exit(1)

    target = brief["weekly_tss_target"]
    # The CTL requirement can exceed the athlete's own hours ceiling (kathryn,
    # 5 Jul 2026: required 640 vs cap 509+10% — every attempt hard-failed and no
    # clean week EXISTS). Aim at the biggest legal week instead and tell the
    # athlete about the conflict; silently failing forever helps nobody.
    tss_cap = pb._weekly_tss_cap(args.athlete, {"name": brief.get("phase")})
    if target and tss_cap and target > tss_cap:
        brief["weekly_tss_target_required"] = target
        brief["target_capped_by_hours"] = round(tss_cap)
        target = int(tss_cap)
        brief["weekly_tss_target"] = target
    override_path = Path(args.override_json) if args.override_json else None
    if override_path and override_path.exists():
        proposal = json.loads(override_path.read_text())
        built = close_to_target(args.athlete, proposal, target, brief)
        blocking, advisory = audit_built(brief, built, target, proposal)
        attempts = [f"override: {len(blocking)} blocking / {len(advisory)} advisory"
                    + (f" - {blocking + advisory}" if (blocking or advisory) else " - CLEAN")]
        best = (built, blocking, advisory, proposal)
    else:
        # ITERATE UNTIL CLEAN (iterative planning is fine — Jamie 15 Jun): propose → load-close
        # → audit; if issues, feed them back and re-propose. Keep the best attempt.
        feedback = ""
        best = None
        best_rank = None
        attempts = []
        for attempt in range(args.max_attempts):
            prompt = build_prompt(args.athlete, brief, week_start, feedback)
            proc = claude_call.run_claude(
                prompt, model=args.model, fallback=[claude_call.OPUS],
                cwd=PROJECT_DIR, timeout=540, label=args.athlete,
            )
            try:
                proposal = extract_json(proc.stdout.strip())
            except Exception as e:
                attempts.append(f"attempt {attempt+1}: parse error {e}")
                continue
            built = close_to_target(args.athlete, proposal, target, brief)
            blocking, advisory = audit_built(brief, built, target, proposal)
            all_issues = blocking + advisory
            _z3, _tot = _overall_z3plus(proposal)
            _z3pct = round(_z3 / _tot * 100) if _tot else 0
            attempts.append(f"attempt {attempt+1}: {len(blocking)} blocking / {len(advisory)} advisory "
                            f"/ {_z3pct}% Z3+" + (f" - {all_issues}" if all_issues else " - CLEAN"))
            # SELECTION: fewest blocking first, then the overall intensity budget DRIVES it -
            # smallest |week Z3+ - phase budget|, then lowest monotony, then closest-to-target
            # TSS - so an in-budget attempt beats an over-budget one when both are equally
            # (un)blocked, instead of just keeping the first 0-blocking attempt.
            rank = (len(blocking),) + _attempt_rank(brief, built, target, proposal)
            if best is None or rank < best_rank:
                best, best_rank = (built, blocking, advisory, proposal), rank
            if not all_issues:
                break
            # Feed BOTH back: the proposer must CLEAR blocking (safety ceilings/rules) and
            # should improve advisory (quality/shape) - advisory never blocks the push.
            feedback = ("\nPREVIOUS ATTEMPT - CLEAR the blocking checks and improve the advisory "
                        "ones:\n- " + "\n- ".join(all_issues) + "\n")
        if best is None:
            print(json.dumps({"error": "no parseable proposal after retries", "attempts": attempts}))
            sys.exit(1)
    built, blocking, advisory, proposal = best
    n_blocking = len(blocking)

    load_pct_off = (round((built["total_tss"] - target) / target * 100, 1) if target else None)
    load_on_target = (target is None) or abs(load_pct_off) <= 12
    overall_ok = built["ok"] and load_on_target and n_blocking == 0
    summary = {
        "attempts": attempts,
        "athlete": args.athlete, "week_start": built["week_start"],
        "event": brief["event"], "phase": brief["phase"], "week_in_phase": brief["week_in_phase"],
        "target_tss": target, "built_total_tss": built["total_tss"],
        "load_pct_off_target": load_pct_off, "load_on_target": load_on_target,
        "fuel_g_hr": built["fuel_g_hr"], "rules_ok": built["ok"], "ready_to_push": overall_ok,
        "hard": built["hard"], "soft": built["soft"],
        "blocking_issues": blocking, "advisories": advisory,
        "sessions": [{"date": s["date"], "sport": s["sport"], "name": s["name"],
                      "load": s["load_target"], "min": s["duration_min"],
                      "structured": bool(s["description"])} for s in built["sessions"]],
    }
    if args.push:
        if not overall_ok:
            summary["pushed"] = False
            summary["reason"] = f"not ready (rules_ok={built['ok']}, load_on_target={load_on_target}) — not pushing"
            if args.notify and cfg.get("chat_id"):
                # hard entries are {code, msg} dicts — joining them raw crashed here
                # (masked every clean-week failure until 5 Jul 2026)
                why = (blocking[0] if blocking
                       else (f"load {load_pct_off}% off target" if not load_on_target else "audit failed"))
                _notify(cfg["chat_id"], f"⚠️ Couldn't generate a clean week ({why}). Your existing plan is unchanged.")
        else:
            summary["push_result"] = pb.push(args.athlete, built)
            if override_path and override_path.exists():
                try:
                    override_path.unlink()
                except Exception:
                    pass
            if args.notify and cfg.get("chat_id"):
                _notify(cfg["chat_id"], _week_message(brief, built))
    print(json.dumps(summary, indent=1, ensure_ascii=False))


def _week_message(brief: dict, built: dict) -> str:
    import datetime as _dt
    target = brief.get("weekly_tss_target")
    header = f"*Week of {built['week_start']}* — {brief.get('phase','')} · {built['total_tss']} TSS"
    if target:
        header += f" (target {target})"
    lines = [header]
    floor = brief.get("weekly_tss_floor")
    if floor and built["total_tss"] < floor * 0.95:
        lines.insert(0, f"🔥 *UNDER-TRAINING WEEK*: {built['total_tss']} TSS is below the "
                        f"{floor} floor — this week does not train you. Flagged, not hidden.")
    req = brief.get("weekly_tss_target_required")
    if req:
        lines.append(f"⚠️ _Phase requires ~{req} TSS but your weekly-hours ceiling caps the "
                     f"plan at ~{brief.get('target_capped_by_hours')}. Fitness will build "
                     f"slower than the blueprint — raise max_hours_per_week to close the gap._")
    for s in built["sessions"]:
        wd = _dt.date.fromisoformat(s["date"]).strftime("%a")
        dur = f" {s['duration_min']}min" if s["duration_min"] else ""
        lines.append(f"{wd}: {s['name']}{dur}")
    lines.append("_Synced to your calendar/Garmin._")
    # EVERY-WEEK equipment ask (strength programme, signed off 10 Jun) — travel changes
    # availability, so we ask each week and tailor the pushed sessions when you answer.
    if brief.get("strength_programme"):
        lines.append("")
        lines.append("💪 *Strength* — what equipment do you have this week? "
                     "(full gym / dumbbells-kettlebells / bodyweight only). "
                     "Reply and I'll tailor the sessions.")
    return "\n".join(lines)


def _notify(chat_id, text):
    try:
        subprocess.run(["python3", str(BASE / "telegram" / "notify.py"),
                        "--chat-id", str(chat_id), text],
                       cwd=PROJECT_DIR, timeout=30, capture_output=True)
    except Exception:
        pass


if __name__ == "__main__":
    main()
