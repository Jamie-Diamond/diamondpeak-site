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


def close_to_target(athlete: str, proposal: dict, target, brief: dict, tol=0.06, max_iter=5):
    """Reliable TSS — but PROTECT key sessions. The long run and long ride are CLAMPED to
    their targets (never used to absorb TSS); quality is fixed by dose; only the OTHER easy
    endurance (easy runs, 2nd/easy rides) is scaled to land the week on target."""
    lr_cap = brief.get("long_run_cap_min")            # MAX long run
    lrd_min = brief.get("long_ride_target_min")
    mileage_cap_km = brief.get("weekly_run_mileage_cap_km")  # MAX weekly run km
    PACE = 5.3  # ~easy min/km (matches the audit's km estimate)

    # 1. Long ride clamped to its target; long run CLAMPED DOWN to its cap (never up).
    for s in proposal["sessions"]:
        if _is_long_ride(s) and lrd_min:
            _set_total_minutes(s, lrd_min)
        elif _is_long_run(s) and lr_cap:
            cur = sum(seg.get("minutes", 0) for seg in s.get("segments", []))
            if cur > lr_cap:
                _set_total_minutes(s, lr_cap)

    # 2. Total run mileage is a CEILING: if over the weekly cap, scale ALL runs down to it.
    #    (Never scale runs UP — mileage is a max, not a target.)
    if mileage_cap_km:
        run_sessions = [s for s in proposal["sessions"] if (s.get("sport") or "").lower() == "run"]
        cur_min = sum(sum(seg.get("minutes", 0) for seg in s.get("segments", [])) for s in run_sessions)
        cap_min = mileage_cap_km * PACE
        if cur_min > cap_min and cur_min > 0:
            f = cap_min / cur_min
            for s in run_sessions:
                for seg in s.get("segments", []):
                    seg["minutes"] = max(15, round(seg["minutes"] * f))
            # re-clamp the long run in case scaling pushed it (it won't, but safe)
            for s in run_sessions:
                if _is_long_run(s) and lr_cap:
                    cur = sum(seg.get("minutes", 0) for seg in s.get("segments", []))
                    if cur > lr_cap:
                        _set_total_minutes(s, lr_cap)

    # 3. TSS is closed with BIKE volume only (endurance rides, not the long ride, not runs).
    flex = lambda s: (_is_endurance(s) and (s.get("sport") or "").lower() in ("bike", "ride")
                      and not _is_long_ride(s))
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
    cap_km = brief.get("weekly_run_mileage_cap_km")   # MAX (highest of last 4 wks ×1.15)
    if cap_km and run_km > cap_km:
        issues.append(f"run mileage ~{run_km}km EXCEEDS cap {cap_km}km (+10-15% max) — cut run durations")
    rp = brief.get("run_protocol") or {}
    if rp.get("quality_allowed") is False:
        for s in proposal.get("sessions", []):
            if (s.get("sport") or "").lower() != "run":
                continue
            if any((seg.get("if") if seg.get("if") is not None else segment_if("run", seg.get("zone"))) >= 0.85
                   for seg in (s.get("segments") or [])):
                issues.append(f"run '{s.get('name')}' has quality intensity but run quality is NOT allowed (ankle)")
    lrc = brief.get("long_run_cap_min")   # MAX single long run (×1.15)
    if lrc and runs and max(s["duration_min"] for s in runs) > lrc:
        issues.append(f"long run {max(s['duration_min'] for s in runs)}min EXCEEDS cap {lrc}min (+10-15% max)")

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
- RUNS: total run mileage must NOT exceed weekly_run_mileage_cap_km (≈ minutes/5.3 km) and the
  longest run must NOT exceed long_run_cap_min — these are MAX ceilings (+10-15% on the highest of
  the last 4 weeks). Plan at or under them. If run_protocol.quality_allowed is false, EVERY run is
  easy Z2 — NO tempo/threshold/interval/vo2 run (ankle gate). Honour run_protocol format.
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
    ap.add_argument("--notify", action="store_true", help="message the athlete on completion")
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
                              capture_output=True, text=True, cwd=PROJECT_DIR, timeout=540)
        try:
            proposal = extract_json(proc.stdout.strip())
        except Exception as e:
            attempts.append(f"attempt {attempt+1}: parse error {e}")
            continue
        built = close_to_target(args.athlete, proposal, target, brief)
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
            if args.notify and cfg.get("chat_id"):
                _notify(cfg["chat_id"], f"⚠️ Couldn't generate a clean week ({', '.join(built['hard'][:1]) or 'audit failed'}). Your existing plan is unchanged.")
        else:
            summary["push_result"] = pb.push(args.athlete, built)
            if args.notify and cfg.get("chat_id"):
                _notify(cfg["chat_id"], _week_message(brief, built))
    print(json.dumps(summary, indent=1, ensure_ascii=False))


def _week_message(brief: dict, built: dict) -> str:
    import datetime as _dt
    lines = [f"*Week of {built['week_start']}* — {brief.get('phase','')} · {built['total_tss']} TSS"]
    for s in built["sessions"]:
        wd = _dt.date.fromisoformat(s["date"]).strftime("%a")
        dur = f" {s['duration_min']}min" if s["duration_min"] else ""
        lines.append(f"{wd}: {s['name']}{dur}")
    lines.append("_Synced to your calendar/Garmin._")
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
