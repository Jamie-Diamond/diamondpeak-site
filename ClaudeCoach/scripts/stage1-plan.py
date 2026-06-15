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

import session_library as sl          # noqa: E402
import plan_builder as pb             # noqa: E402
from primitives.planned_tss import segment_if  # noqa: E402

_QUALITY_IF = 0.85   # a session with any segment at/above this is "quality" (fixed); else endurance


def _is_endurance(sess: dict) -> bool:
    """True if the session is pure endurance (no quality main set) → its duration is the
    flexible lever for hitting the weekly TSS target. Quality sessions stay fixed."""
    segs = sess.get("segments") or []
    if not segs:
        return False
    sport = sess.get("sport", "")
    return all((seg.get("if") if seg.get("if") is not None else segment_if(sport, seg.get("zone")))
               < _QUALITY_IF for seg in segs)


def close_to_target(athlete: str, proposal: dict, target, tol=0.06, max_iter=4):
    """Reliable TSS (1st objective): keep quality fixed, scale ENDURANCE durations until
    the week lands within `tol` of target. Zones/types untouched (2nd-objective opt)."""
    built = pb.build_sessions(athlete, proposal)
    if not target:
        return built
    for _ in range(max_iter):
        total = built["total_tss"]
        if abs(total - target) <= tol * target:
            break
        flex_load = sum(b["load_target"] for s, b in zip(proposal["sessions"], built["sessions"])
                        if _is_endurance(s))
        if flex_load <= 0:
            break
        factor = max(0.55, min(1.7, (flex_load + (target - total)) / flex_load))
        for s in proposal["sessions"]:
            if _is_endurance(s):
                for seg in s.get("segments", []):
                    seg["minutes"] = max(15, round(seg["minutes"] * factor))
        built = pb.build_sessions(athlete, proposal)
    return built

CLAUDE = shutil.which("claude") or "/usr/bin/claude"
PROJECT_DIR = str(BASE.parent)


def _next_monday(today: date) -> date:
    return today + timedelta(days=(7 - today.weekday()) % 7 or 7)


def audit_built(brief: dict, built: dict, target, proposal: dict) -> list:
    """Everything the week must satisfy before it's offered. Returns issue strings
    (empty = clean). Drives the iterate-until-clean loop."""
    import datetime as _dt
    issues = []
    if target and abs(built["total_tss"] - target) > 0.08 * target:
        d = built["total_tss"] - target
        issues.append(f"week TSS {built['total_tss']} vs target {target} ({d:+}) — "
                      f"{'add' if d < 0 else 'cut'} endurance volume")
    issues += [f"rule(hard): {v['msg']}" for v in built.get("hard", [])]
    issues += [f"rule(dist): {v['msg']}" for v in built.get("soft", [])]
    # swim_focus: the swim on a focus day must match the allowed type(s)
    sf = (brief.get("day_rules") or {}).get("swim_focus") or {}
    if sf:
        for s in proposal.get("sessions", []):
            if (s.get("sport") or "").lower() != "swim":
                continue
            wd = _dt.date.fromisoformat(s["date"]).strftime("%a")
            allowed = sf.get(wd)
            nm = (s.get("name") or "").lower()
            if allowed and not any(a in nm or a.replace("technique", "drill")[:4] in nm for a in allowed):
                issues.append(f"{wd} swim must be {allowed} (got '{s.get('name')}')")

    # ── RUN PROTOCOL (the gaps that produced the 22k run / illegal threshold run) ──
    runs = [s for s in built["sessions"] if s["sport"] == "Run"]
    run_min = sum(s["duration_min"] for s in runs)
    run_km = round(run_min / 5.3, 1)   # ~easy pace 5.3 min/km
    tgt_km = brief.get("run_mileage_target_km")
    if tgt_km and not (tgt_km * 0.85 <= run_km <= tgt_km * 1.15):
        issues.append(f"run mileage ~{run_km}km vs target {tgt_km}km — adjust run durations")
    rp = brief.get("run_protocol") or {}
    if rp.get("quality_allowed") is False:
        for s in proposal.get("sessions", []):
            if (s.get("sport") or "").lower() != "run":
                continue
            if any((seg.get("if") if seg.get("if") is not None else segment_if("run", seg.get("zone"))) >= 0.85
                   for seg in (s.get("segments") or [])):
                issues.append(f"run '{s.get('name')}' has quality intensity but run quality is NOT allowed (ankle)")
    lr = (brief.get("long_run_target") or {}).get("minutes")
    if lr and runs:
        longest = max(s["duration_min"] for s in runs)
        if longest > lr * 1.2:
            issues.append(f"long run {longest}min exceeds cap ~{lr}min (+10-15% rule)")

    # ── LONG RIDE must be present and protected ──
    lrt = brief.get("long_ride_target_min")
    rides = [s for s in built["sessions"] if s["sport"] in ("Ride", "Bike", "Brick")]
    if lrt and (not rides or max(s["duration_min"] for s in rides) < lrt * 0.85):
        have = max((s["duration_min"] for s in rides), default=0)
        issues.append(f"no protected long ride — longest ride {have}min < target ~{lrt}min")
    return issues


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
- RUNS: total ~run_mileage_target_km (≈ minutes/5.3 km); the longest run ≈ long_run_target.
  If run_protocol.quality_allowed is false, EVERY run is easy Z2 — NO tempo/threshold/interval/
  vo2 run (ankle gate). Honour run_protocol format (run-walk 5:30, HR cap).
- OBEY hard_rules (the athlete's protocol) absolutely — they override anything else here.
- Swim sets: express in minutes (not metres). Strength: omit segments.
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
    ap.add_argument("--model", default="claude-sonnet-4-6")
    ap.add_argument("--max-attempts", type=int, default=3)
    args = ap.parse_args()

    cfg = json.loads((BASE / "config" / "athletes.json").read_text())[args.athlete]
    today = date.today()
    week_start = _next_monday(today)
    brief = sl.planning_brief(args.athlete, cfg, today=today)
    if brief.get("event_unknown"):
        print(json.dumps({"error": f"event unknown for {args.athlete} — cannot plan"}))
        sys.exit(1)

    target = brief["weekly_tss_target"]
    # ITERATE UNTIL CLEAN (iterative planning is fine — Jamie 15 Jun): propose → load-close
    # → audit; if issues, feed them back and re-propose. Keep the best attempt.
    feedback = ""
    best = None
    attempts = []
    for attempt in range(args.max_attempts):
        prompt = build_prompt(args.athlete, brief, week_start, feedback)
        proc = subprocess.run([CLAUDE, "-p", prompt, "--model", args.model,
                               "--no-session-persistence"],
                              capture_output=True, text=True, cwd=PROJECT_DIR, timeout=300)
        try:
            proposal = extract_json(proc.stdout.strip())
        except Exception as e:
            attempts.append(f"attempt {attempt+1}: parse error {e}")
            continue
        built = close_to_target(args.athlete, proposal, target)
        issues = audit_built(brief, built, target, proposal)
        attempts.append(f"attempt {attempt+1}: {len(issues)} issue(s)" + (f" — {issues}" if issues else " — CLEAN"))
        if best is None or len(issues) < best[1]:
            best = (built, len(issues), proposal)
        if not issues:
            break
        feedback = ("\nPREVIOUS ATTEMPT FAILED THESE CHECKS — fix them this time:\n- "
                    + "\n- ".join(issues) + "\n")
    if best is None:
        print(json.dumps({"error": "no parseable proposal after retries", "attempts": attempts}))
        sys.exit(1)
    built, n_issues, proposal = best

    load_pct_off = (round((built["total_tss"] - target) / target * 100, 1) if target else None)
    load_on_target = (target is None) or abs(load_pct_off) <= 12
    overall_ok = built["ok"] and load_on_target and n_issues == 0
    summary = {
        "attempts": attempts,
        "athlete": args.athlete, "week_start": built["week_start"],
        "event": brief["event"], "phase": brief["phase"], "week_in_phase": brief["week_in_phase"],
        "target_tss": target, "built_total_tss": built["total_tss"],
        "load_pct_off_target": load_pct_off, "load_on_target": load_on_target,
        "fuel_g_hr": built["fuel_g_hr"], "rules_ok": built["ok"], "ready_to_push": overall_ok,
        "hard": built["hard"], "soft": built["soft"],
        "sessions": [{"date": s["date"], "sport": s["sport"], "name": s["name"],
                      "load": s["load_target"], "min": s["duration_min"],
                      "structured": bool(s["description"])} for s in built["sessions"]],
    }
    if args.push:
        if not overall_ok:
            summary["pushed"] = False
            summary["reason"] = f"not ready (rules_ok={built['ok']}, load_on_target={load_on_target}) — not pushing"
        else:
            summary["push_result"] = pb.push(args.athlete, built)
    print(json.dumps(summary, indent=1, ensure_ascii=False))


if __name__ == "__main__":
    main()
