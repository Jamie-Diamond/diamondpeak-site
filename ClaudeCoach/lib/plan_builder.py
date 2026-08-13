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
  - run_fuel_target for >=60-min runs -> run-specific g/hr appended to the notes
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
from primitives.nutrition import (fuel_target, last_ride_g_hr,           # noqa: E402
                                  last_run_g_hr, recent_avg_g_hr,
                                  recent_run_avg_g_hr, run_fuel_target,
                                  RUN_TARGET_G_HR, LONG_RUN_MIN)
from primitives.validate_plan import validate_week             # noqa: E402
from primitives.blueprint import current_phase, tss_ceiling    # noqa: E402
import weekly_availability                                     # noqa: E402

ATHLETES = BASE / "config" / "athletes.json"
# Rides (Brick counts as a ride: its fuelling is bike fuelling carried into a run).
_LONG_FUEL_SPORTS = {"Ride", "GravelRide", "VirtualRide", "Brick"}
# Runs get their own set, threshold and target. They were previously excluded from
# the fuel note altogether, so the ONE sport Jamie's persistent rule names as the
# open fuelling focus ("prioritise run-fuelling data and nudge run carbs toward ~60
# g/hr") was the one sport that never received a fuelling instruction.
_LONG_RUN_FUEL_SPORTS = {"Run", "TrailRun", "VirtualRun"}


def _cfg(slug):
    return json.loads(ATHLETES.read_text())[slug]


def _blueprint(slug):
    p = BASE / "athletes" / slug / "reference" / "training-blueprint.json"
    try:
        return json.loads(p.read_text()) if p.exists() else {}
    except Exception:
        return {}


def _fuel_for(slug, cfg):
    """(ride_g_hr, run_g_hr). Two numbers because the two sports are at different
    places: the bike ramps toward the athlete's race target off a LONG-ride window,
    the run ramps toward the run TRAINING target off its own run window. Blending them
    would let a low run average drag the bike prescription (and vice versa).

    Both pass the MOST RECENT qualifying session as well as the average. Without it the
    ramp climbed off a six-session mean that creeps about 1 g/hr per session, so it could
    prescribe below what the athlete did last week and then sit still for weeks. The
    session notes the coach writes and the figure the nutrition bot shows come from this
    same call, so they cannot disagree."""
    sl = BASE / "athletes" / slug / "session-log.json"
    log = json.loads(sl.read_text()) if sl.exists() else []
    if isinstance(log, dict):
        log = log.get("sessions") or log.get("entries") or []
    ride = fuel_target(recent_avg_g_hr(log), int(cfg.get("nutrition_target_g_hr") or 90),
                      last_g_hr=last_ride_g_hr(log))
    run = run_fuel_target(recent_run_avg_g_hr(log),
                          cfg.get("nutrition_run_target_g_hr"),
                          last_g_hr=last_run_g_hr(log))
    return ride, run


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


def _weekly_tss_cap(slug, phase, week_start=None) -> float | None:
    """Weekly TSS hard ceiling for the load check, in precedence order.

    0. The hours the ATHLETE DECLARED for `week_start` (weekly_availability), put
       through the same hours x 100 x IF^2 maths. This is the figure Kathryn's rules
       have always required ("confirm each week's available hours via the Sunday
       check-in and build to that") and it outranks config because it describes the
       week actually being planned rather than an average written down once.
    1. Failing that, `profile.max_hours_per_week` — now a documented FALLBACK, not
       the primary source. It is a static description of a typical week and drifts:
       Jamie's 15 caps Peak at 778 TSS against an engine target of 918, though he
       demonstrably trains above it (CTL peak 117.5 last season).
    2. Failing that, the blueprint phase's own `tss_ceiling`. Kathryn has no
       hours ceiling by a permanent rule (10 Jul 2026), which left her weekly
       load entirely unchecked — twelve SKIPPED checks in one build. The phase
       ceiling is a LOAD bound, not a limit on how long she may train, so it
       arms the check without reinstating the hours cap that rule removed.

    None when none of them exists — taper phases carry no ceiling in any source by
    design, so a taper week still reports the check as skipped.

    `week_start` is OPTIONAL and defaults to None, which means "no declaration
    applies" and reproduces the pre-declaration behaviour EXACTLY. That default is
    load-bearing: lib/macro_projection.py:338 passes a ceiling lambda that discards
    its week (`lambda ws, phase: _weekly_tss_cap(slug, phase)`), and one real week's
    declaration must not be stretched across every projected week. Callers that know
    which single week they are bounding — plan_builder.build_sessions,
    plan_audit.audit_athlete, stage1-plan — pass it explicitly.

    ONE PLACE. The hours formula lives in primitives.blueprint.tss_ceiling and its
    precedence lives here; do not re-read max_hours_per_week to compute a ceiling
    elsewhere. lib/plan_tools.py cmd_validate (the inline `max_h`/`tss_ceiling`
    block, ~line 783) still holds a copy
    that predates this and is NOT declaration-aware — it is the manual
    `validate_plan` CLI, off the Sunday build path, and is owned by a concurrent
    ticket; see docs/weekly-hours-capture.md for the follow-up."""
    try:
        declared = weekly_availability.hours_for_week(slug, week_start)
        if declared and phase.get("name"):
            return tss_ceiling(float(declared), str(phase["name"]))
    except Exception:
        pass
    try:
        prof = json.loads((BASE / "athletes" / slug / "profile.json").read_text())
        max_h = prof.get("max_hours_per_week")
        if max_h and phase.get("name"):
            return tss_ceiling(float(max_h), str(phase["name"]))
    except Exception:
        pass
    try:
        ceil = (phase or {}).get("tss_ceiling")
        if ceil and float(ceil) > 0:
            return float(ceil)
    except Exception:
        pass
    return None


def cap_source(slug, phase, week_start=None) -> str:
    """Which bound in _weekly_tss_cap bit: "declared" | "hours" | "phase" | "none".

    Exists so the message an athlete reads can be honest about WHY the week is
    capped without any caller re-deriving the precedence (and drifting from it).
    """
    try:
        if weekly_availability.hours_for_week(slug, week_start) and phase.get("name"):
            return "declared"
    except Exception:
        pass
    try:
        prof = json.loads((BASE / "athletes" / slug / "profile.json").read_text())
        if prof.get("max_hours_per_week") and phase.get("name"):
            return "hours"
    except Exception:
        pass
    try:
        ceil = (phase or {}).get("tss_ceiling")
        if ceil and float(ceil) > 0:
            return "phase"
    except Exception:
        pass
    return "none"


def build_sessions(slug: str, proposal: dict) -> dict:
    """Turn a Stage-1 proposal into push-ready, validated sessions. Pure except
    for reading athlete config/session-log; never pushes."""
    cfg = _cfg(slug)
    fuel, run_fuel = _fuel_for(slug, cfg)
    built, events = [], []
    for s in proposal.get("sessions", []):
        sport = s.get("sport", "")
        date_s = s.get("date")
        notes = (s.get("notes") or "").strip()
        segs = s.get("segments") or []
        # PINNED = an agreed day, spliced in from athletes/<slug>/agreed-plan.json by
        # stage1-plan. It is already on the calendar and will NOT be pushed (see push()).
        # It is built and validated anyway because the week's totals, ramp, run volume and
        # zone distribution must include it or the validator judges a fiction.
        pinned = bool(s.get("pinned"))
        if segs and sport not in ("Strength", "WeightTraining"):
            # NAME passed in: it disambiguates a coarse TID band label (the library gives
            # both tempo and sweetspot the label Z3), so "Sweetspot 2x20" renders at 88-94%
            # FTP instead of 76-84% and stops hard-blocking on name_intensity_mismatch.
            r = render_workout(sport, segs, s.get("name", ""))
            desc, load, dur = r["description"], r["tss"], r["duration_min"]
        else:
            # No structured segments (Strength, or a session Stage 1 left unstructured):
            # derive TSS deterministically from sport + duration. NEVER s.get("load") — the
            # Stage-1 LLM's number is exactly the guessed load the two-stage design removes.
            dur  = int(s.get("minutes") or s.get("duration_min") or 0)
            load = planned_session_tss({"type": sport, "name": s.get("name", ""),
                                        "moving_time": dur * 60})["tss"]
            desc = ""
        if pinned:
            # THE AGREED FIGURE WINS. The load on the calendar is the load the athlete
            # agreed to; re-deriving it here (from a coarse pin's single segment, or from
            # sport+duration) would put a number in the week's total that disagrees with
            # the calendar, and the load gate would then judge a week nobody has.
            # Duration likewise comes from the record when the pin carries no segments.
            if s.get("load_target"):
                load = int(s["load_target"])
            if not segs:
                dur = int(s.get("minutes") or s.get("duration_min") or dur or 0)
        # fuel note for long rides. Skipped for a pinned session: it is never pushed, so
        # the note would reach nobody, and it must not appear to change an agreed session.
        if pinned:
            pass
        elif sport in _LONG_FUEL_SPORTS and dur >= 90:
            notes = (notes + f"\nFuel {fuel} g CHO/hr (progress toward "
                     f"{int(cfg.get('nutrition_target_g_hr') or 90)} race target); "
                     "eat from 15 min, every 25 min.").strip()
        # fuel note for long runs — deliberately makes NO reference to the race-day
        # figure. Jamie's rule states the 90 g/hr target is "NOT a training minimum -
        # do not flag or compare easy/Z2 nutrition to it", so the run note cites the
        # run target only.
        elif sport in _LONG_RUN_FUEL_SPORTS and dur >= LONG_RUN_MIN:
            notes = (notes + f"\nFuel {run_fuel} g CHO/hr on this run (run-fuelling "
                     f"target {int(cfg.get('nutrition_run_target_g_hr') or RUN_TARGET_G_HR)} "
                     "g/hr); start by 20 min, then every 20-25 min. Log what you took.").strip()
        built.append({"date": date_s, "sport": sport, "name": s.get("name", ""),
                      "duration_min": dur, "load_target": load,
                      "description": desc, "description_raw": notes,
                      "pinned": pinned})
        events.append({"start_date_local": f"{date_s}T00:00:00", "type": sport,
                       "category": "WORKOUT", "load_target": load,
                       "moving_time": dur * 60, "pinned": pinned,
                       # `description` = the RENDERED steps, so validate_week can check
                       # the name's intensity claim against the structure. Without it
                       # that check silently no-ops (4 Aug 2026).
                       "name": s.get("name", ""), "description": desc,
                       "description_raw": notes})

    # Validate the whole week against the athlete's hard rules.
    ws = min((date.fromisoformat(e["start_date_local"][:10]) for e in events),
             default=date.today())
    ws -= timedelta(days=ws.weekday())
    # Validate against the day rules THIS WEEK actually has, not the standing config: the
    # athlete's declaration for the week outranks day_rules, so a declared move (Jamie's
    # "Thursday long ride" against bike_days [Fri,Sat,Sun]) must not come back as a hard
    # `ride_forbidden_day`. It would fail every attempt of a week built exactly as he asked
    # - the "no clean week EXISTS" loop stage1-plan.py documents for Kathryn's cap.
    dr, _decl_conflicts = weekly_availability.effective_day_rules(
        slug, ws, cfg.get("day_rules"),
        run_limited=((cfg.get("run_protocol") or {}).get("quality_allowed") is False))
    for _c in _decl_conflicts:
        print(f"[plan_builder:{slug}] declaration vs day_rules - {_c}", file=sys.stderr)
    phase = current_phase(_blueprint(slug), ws) or {}
    # ARM the hard checks (audit P0-4): without ctl_today the ramp check silently
    # no-ops, and without weekly_tss_cap the load check does. Both inputs are
    # sourced here, but NEITHER IS GUARANTEED, so the validator's "a breach cannot
    # reach the athlete" claim does not hold unconditionally: ctl_today needs a
    # live ICU call, and the load cap needs an hours ceiling or a blueprint phase
    # ceiling (a taper has neither). A missing input lands in rep.skipped and is
    # logged loudly — that log line is the only thing between a skipped check and
    # an unchecked week reaching the athlete.
    import plan_tools as _pt
    try:
        _caps = _pt.run_caps(_pt._client(cfg), ws, run_protocol=cfg.get("run_protocol"))
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
                        weekly_tss_cap=_weekly_tss_cap(slug, phase, week_start=ws),
                        weekly_tss_floor=_floor,
                        run_week_min_cap=_caps.get("weekly_min_cap"),
                        run_long_min_cap=_caps.get("long_run_min_cap"),
                        ctl_today=_ctl,
                        ramp_cap=float(cfg.get("max_ctl_ramp_per_week", 5.0)),
                        strength_max=(dr or {}).get("strength_max"),
                        distribution=phase.get("distribution"),
                        # Absolute ride ceiling (Jamie, 4 Aug 2026: no benefit past 5h,
                        # close the gap with intensity below it). Per-athlete override.
                        long_ride_max_min=int(cfg.get("long_ride_max_min") or 300))
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


def pinned_dates_for(slug: str, built: dict) -> set:
    """The dates in this week that are AGREED and must not be written or deleted.

    Read back from lib/agreed_week rather than derived from built["sessions"], for one
    reason that matters: a REST-DAY pin ("nothing on Friday") has no session to derive a
    flag from, and it needs protecting most of all — the whole point is that the day stays
    empty and nothing gets pushed onto it. The union with the pinned session dates is
    belt-and-braces: protecting a day we already validated as pinned can only ever be
    safe, while missing one destroys an agreement.

    PINS ONLY. weekly_availability's `unavailable_days` are deliberately NOT included: a
    declared-unavailable day means "put nothing here", so a stale event sitting on it must
    still be DELETED. Protecting it from deletion would leave a session on a day the
    athlete told us they cannot train. agreed_week.protected_dates() is the wider set and
    belongs to the proposer, not here.

    READ AT PUSH TIME, deliberately, even though stage1-plan already read the pins ~45
    minutes earlier when the build started. A pin created DURING the build is then still
    honoured on the delete list, so the agreed session survives; the cost is that the same
    date can also receive a new push (the day was not pinned when the week was spliced), so
    that one day can end up with two events. That is the right way round: a duplicate is
    visible and one tap to fix, while deleting a session the athlete agreed forty minutes
    ago is the failure this whole ticket exists to stop."""
    out = {str(s.get("date"))[:10] for s in built.get("sessions", []) if s.get("pinned")}
    try:
        import agreed_week
        out |= set(agreed_week.pinned_dates(slug, built["week_start"]))
    except Exception as e:
        print(f"[plan_builder:{slug}] WARN could not read agreed-plan.json ({e!r}) — "
              f"only the pins already spliced into this build are protected", file=sys.stderr)
    return {d for d in out if d}


def assert_deletable(slug: str, event_id, event_date: str, pinned) -> None:
    """Raise unless `event_id` on `event_date` may be deleted. Called immediately before
    every single delete in push().

    Deliberately a RAISE, not an `assert`: python -O strips asserts, and this is the last
    thing standing between a bug in the filter above and an agreed session being destroyed.

    Reaching here with a pinned date is a BUG, not a condition to handle, so it stops the
    run rather than skipping the delete — a silent skip would leave a doubled week and no
    signal. It fires BEFORE anything is destroyed, and the new events are already pushed, so
    the athlete is left with a duplicate to tidy rather than a hole."""
    if str(event_date)[:10] in (pinned or set()):
        raise RuntimeError(
            f"[plan_builder:{slug}] REFUSING to delete event {event_id} on {event_date}: "
            f"that day is AGREED with the athlete. Reaching this line means the "
            f"pinned-date filter in push() was bypassed — fix that, do not relax this.")


def push(slug: str, built: dict, replace: bool = True):
    """Push the built week. replace=True first DELETES existing planned WORKOUT events
    in the target week so we don't duplicate the old plan. Returns {deleted, pushed}.

    AGREED DAYS ARE UNTOUCHABLE HERE. Pinned dates are skipped on the push list (those
    sessions are already on the calendar; re-pushing would churn ICU ids and re-sync
    Garmin for no reason) AND on the delete list, with an explicit check before every
    single delete. That check is the last line of defence before something irreversible,
    so it raises rather than asserting — `python -O` strips asserts, and this must survive
    any interpreter flag."""
    from icu_api import IcuClient
    from datetime import date as _d, timedelta as _td
    cfg = _cfg(slug)
    c = IcuClient(cfg["icu_athlete_id"], cfg["icu_api_key"])
    ws = _d.fromisoformat(built["week_start"])
    pinned = pinned_dates_for(slug, built)
    # Map proposal sports to valid intervals.icu event types (Bike/Brick/Strength are NOT
    # valid ICU types — Brick pushes as a Ride; the run leg is in description_raw).
    icu_type = {"Bike": "Ride", "Brick": "Ride", "Strength": "WeightTraining",
                "Weights": "WeightTraining"}
    # SAFE ORDERING: capture the OLD events, PUSH the new ones FIRST, and only delete the
    # old ones once every new push succeeded. If a push fails the old plan is left intact
    # (worst case: transient duplicates), so a failure can NEVER empty the week.
    old_ids = []          # [(event_id, date)] — the date is carried so the pre-delete
                          # check below can be made against the event itself, not against
                          # a set computed somewhere else and trusted.
    if replace:
        old_ids = [(e["id"], str(e.get("start_date_local") or "")[:10])
                   for e in c.get_events(ws.isoformat(), (ws + _td(days=6)).isoformat())
                   if e.get("category") == "WORKOUT" and e.get("id")
                   and str(e.get("start_date_local") or "")[:10] not in pinned]
    pushed = []
    for s in built["sessions"]:
        if s.get("pinned"):
            # Already on the calendar, by the athlete's agreement. Re-pushing it would
            # duplicate or churn it and re-sync Garmin for no reason.
            continue
        payload = {"sport": icu_type.get(s["sport"], s["sport"]),
                   "event_date": s["date"], "name": s["name"],
                   "description": s["description"], "description_raw": s["description_raw"],
                   "planned_training_load": s["load_target"]}
        r = c.push_workout(**payload)        # raises before any delete if a payload is bad
        pushed.append(r.get("id"))
    deleted = []
    failed = []
    for eid, edate in old_ids:                # only reached if ALL pushes succeeded
        assert_deletable(slug, eid, edate, pinned)   # raises before anything irreversible
        try:
            c.delete_workout(eid); deleted.append(eid)
        except Exception as e:
            # A swallowed delete is benign ONCE (worst case a duplicate event), but a
            # systematically failing delete doubles the week silently, which is how the
            # 22 Jun doubled week went unnoticed. Keep the swallow, name the casualty.
            failed.append(eid)
            print(f"[plan_builder:{slug}] delete of old event {eid} FAILED "
                  f"({e!r}) - it stays on the calendar as a duplicate", file=sys.stderr)
    return {"deleted": deleted, "pushed": pushed, "delete_failed": failed,
            # Named in the result so "why is Thursday not in the push list?" is answerable
            # from the run output alone.
            "agreed_days_left_alone": sorted(pinned)}


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
