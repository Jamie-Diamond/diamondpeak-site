"""What Peak's Goals page shows (Jamie, 1 Oct 2026): the season's A / B / C races,
each with its goal and what can honestly be predicted for it, and the phases of the
build to the A race.

    payload(slug, cfg, profile, data, today) -> {
      "aRace":  the next A race (a race dict as below) or None,
      "races":  every upcoming race, earliest first:
                {name, date, days, priority, distance, event, kind,
                 goal, goal_s,                 the race's own goal (A race: race_goal)
                 now_s,                        run races: Riegel from threshold pace
                 hold: {if_lo, if_hi, w_lo, w_hi, hours}   bike races: an intensity they
                                               could hold for the expected duration
                 tri: {now_min, raceday_min}   triathlon A race: the IM model's numbers},
      "phases": [{name, family, start, end}] to the A race (blueprint canonical_phases),
      "trackingOnly": bool,                    no plan, so no phases
      "goal": goals.view() for an athlete with a goal and no A race ahead (lib/goals.py),
      "goalAfterRace": the goal's label when an A race is ahead of it,
    }

Run-race predictions (Jamie, 1 Oct 2026: "do some research and do what you can"):
  Marathon: Tanda (2011), marathon pace from training: Pm (s/km) = 17.1 +
    140 exp(-0.0053 K) + 0.55 P, K = mean weekly running km and P = mean training pace
    (s/km) over 8 weeks; it explained ~77% of finish-time variance in recreational
    runners. "Now" uses the last 8 weeks; "race day" the weekly km the plan builds to
    (the event level for their goal), at today's training pace.
  5k / 10k / half: "now" is Riegel (exponent 1.06) from the tested threshold pace.
    "Race day" applies the same Tanda training gain, scaled by the race's share of a
    marathon (volume matters less the shorter the race). That blend is ours, and the
    page says it is an estimate.
Nothing else is invented: a race whose kind has no model gets no number, and the
page says so. Riegel and the IM model are the same maths the coach quotes in chat.
"""
from __future__ import annotations

import math
import re
import sys
from datetime import date
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent            # ClaudeCoach/
sys.path.insert(0, str(BASE / "ironman-analysis"))

# Intensity factor someone can hold for a ride of this many hours: standard
# endurance pacing guidance, the long end for a well-fuelled steady rider.
_IF_BY_HOURS = ((2, 0.85, 0.90), (3, 0.80, 0.85), (4, 0.75, 0.80), (6, 0.70, 0.75),
                (8, 0.65, 0.70), (10, 0.62, 0.67), (99, 0.58, 0.64))


_RUN_SPORTS = ("Run", "VirtualRun", "TrailRun", "Treadmill")


def training_volume(recent: list, today: date, days: int = 56) -> tuple:
    """(mean weekly running km, mean training pace s/km) over the last `days`."""
    km = secs = 0.0
    for a in recent or []:
        when = _d(a.get("date"))
        if (a.get("sport") in _RUN_SPORTS and when and 0 <= (today - when).days < days
                and (a.get("dist") or 0) > 0 and (a.get("dur") or 0) > 0):
            km += float(a["dist"])
            secs += float(a["dur"]) * 60
    if km < 1:
        return None, None
    return km / (days / 7), secs / km


def tanda_marathon_s(weekly_km: float, pace_s_km: float) -> int:
    """Tanda (2011): predicted marathon time in seconds."""
    pm = 17.1 + 140.0 * math.exp(-0.0053 * weekly_km) + 0.55 * pace_s_km
    return int(round(pm * 42.195))


def _d(v) -> date | None:
    try:
        return date.fromisoformat(str(v)[:10])
    except (TypeError, ValueError):
        return None


def _pace(v) -> str | None:
    """'5:04/km - DERIVED ...' -> '5:04'."""
    m = re.search(r"\b(\d{1,2}:\d{2})\b", str(v or ""))
    return m.group(1) if m else None


def hold_target(hours: float, ftp) -> dict:
    lo, hi = next((a, b) for h, a, b in _IF_BY_HOURS if hours <= h)
    out = {"if_lo": lo, "if_hi": hi, "hours": round(hours, 1)}
    try:
        f = float(ftp)
        out.update(w_lo=int(round(f * lo / 5) * 5), w_hi=int(round(f * hi / 5) * 5))
    except (TypeError, ValueError):
        pass
    return out


def _race_view(r: dict, a_race: dict | None, cfg: dict, profile: dict, data: dict,
               today: date) -> dict:
    import race_fitness as rf
    when = _d(r.get("date"))
    event = rf.levels_key(r.get("distance") or "") or rf.levels_key(r.get("name") or "")
    kind = rf.event_kind(event) if event else None
    is_a = a_race is not None and r is a_race
    goal = r.get("goal") or (cfg.get("race_goal") if is_a else None)
    v = {"name": r.get("name"), "date": r.get("date"), "days": (when - today).days if when else None,
         "priority": str(r.get("priority") or "").upper() or None, "distance": r.get("distance"),
         "event": event, "kind": kind, "goal": goal,
         "goal_s": rf.parse_goal_s(goal, event) if goal and event else None}
    if kind == "run":
        _run_predictions(v, event, a_race, cfg, profile, data, today, when)
    elif kind == "bike":
        mins = ((cfg.get("race_target_splits") or {}).get("bike_min") if is_a else None)
        hours = mins / 60 if mins else None
        if hours is None:
            lvl = rf.level_for_fitness(event, (data.get("kpi") or {}).get("ctl"))
            if lvl and lvl.get("hours"):
                hours = sum(lvl["hours"]) / 2
        if hours:
            v["hold"] = hold_target(hours, data.get("resolvedFtp") or profile.get("ftp_watts"))
    elif kind == "tri" and is_a:
        rows = (data.get("racePredictor") or {}).get("rows") or []
        if rows and str(profile.get("race_name") or "") == str(r.get("name") or ""):
            v["tri"] = {"now_min": rows[0].get("total_min"), "raceday_min": rows[-1].get("total_min")}
    return v


def _planned_weekly_km(a_race: dict | None, cfg: dict):
    """Weekly running km the plan builds to: the marathon/half level for their goal."""
    import race_fitness as rf
    if not a_race:
        return None
    ev = rf.levels_key(a_race.get("distance") or "") or rf.levels_key(a_race.get("name") or "")
    if rf.event_kind(ev) != "run":
        return None
    goal = a_race.get("goal") or cfg.get("race_goal")
    row = rf.level_for_time(ev, rf.parse_goal_s(goal, ev)) if goal else None
    band = (row or {}).get("run_km")
    return sum(band) / 2 if band else None


def _run_predictions(v, event, a_race, cfg, profile, data, today, when) -> None:
    import race_fitness as rf
    km = rf._race_km(event) or 0
    k_now, pace = training_volume(data.get("recent"), today)
    k_plan = _planned_weekly_km(a_race, cfg)
    # Weekly km expected by THIS race: from today's to the planned volume, reached
    # three weeks before the A race (the taper), so an early C race sees little of it.
    k_then = k_now
    if k_now is not None and k_plan and a_race and when:
        a_date = _d(a_race["date"])
        build_end = a_date.toordinal() - 21
        span = max(1, build_end - today.toordinal())
        frac = min(1.0, max(0.0, (when.toordinal() - today.toordinal()) / span))
        k_then = k_now + (max(k_plan, k_now) - k_now) * frac
    gain = None
    if k_now is not None and pace:
        m_now = tanda_marathon_s(k_now, pace)
        m_then = tanda_marathon_s(k_then, pace)
        gain = m_then / m_now                                    # < 1 = faster
        v["volume"] = {"k_now": round(k_now, 1), "k_race": round(k_then, 1),
                       "pace_s_km": round(pace)}
    if event == "marathon" and gain is not None:
        v["now_s"], v["raceday_s"] = m_now, m_then
        v["method"] = "tanda"
        return
    v["now_s"] = rf.predicted_run_time_s(event, _pace(profile.get("run_threshold_pace_per_km")))
    v["method"] = "riegel"
    if v["now_s"] and gain is not None:
        share = min(1.0, km / 42.195)
        v["raceday_s"] = int(round(v["now_s"] * (1 - share * (1 - gain))))


def payload(slug: str, cfg: dict, profile: dict, data: dict, today: date | None = None) -> dict:
    import planning_pause
    import races as races_lib
    today = today or date.today()
    cfg, profile, data = cfg or {}, profile or {}, data or {}
    try:
        all_races = races_lib.load_races(slug, config={slug: cfg})
    except Exception:
        all_races = []
    upcoming = sorted((r for r in all_races
                       if _d(r.get("date")) and _d(r.get("date")) >= today and not r.get("completed")),
                      key=lambda r: r["date"])
    a_race = next((r for r in upcoming if str(r.get("priority") or "").upper() == "A"), None)
    views = [_race_view(r, a_race, cfg, profile, data, today) for r in upcoming]
    tracking = planning_pause.is_paused(slug, cfg)
    phases = []
    if a_race and not tracking:
        try:
            from primitives.blueprint import canonical_phases
            ps = canonical_phases(_d(cfg.get("plan_start")), cfg.get("phase_tss"), _d(a_race["date"]))
            phases = [{"name": p["name"], "family": p["family"], "start": p["start"].isoformat(),
                       "end": p["end"].isoformat()} for p in ps]
        except Exception:
            phases = []
    a_view = next((v for v in views if a_race and v["name"] == a_race.get("name")
                   and v["date"] == a_race.get("date")), None)
    out = {"aRace": a_view, "races": views, "phases": phases, "trackingOnly": tracking}
    # A goal without a race (lib/goals.py): the goal card replaces the race hero while no A
    # race is ahead; with one ahead, the page just says the goal resumes after it.
    try:
        import goals as goals_lib
        if goals_lib.goal_cfg(cfg) and not tracking:
            if a_race:
                out["goalAfterRace"] = goals_lib.GOALS[cfg["goal"]["type"]]["label"]
            else:
                import plan_tools as _pt
                out["goal"] = goals_lib.view(cfg, profile, slug, today,
                                             ctl_now=(data.get("kpi") or {}).get("ctl"))
                # No race ahead but still in the last race's recovery weeks.
                out["goal"]["waiting"] = not _pt.goal_active(cfg, today)
    except Exception:
        pass
    return out
