"""What is holding the athlete back from their run goal (Jamie, 17 Oct 2026: "I want the
app to be able to say: your fitness is the issue, or it's your durability or efficiency").

Every row is MEASURED from training (lib/performance.py) and set against what the goal
time physically needs. No row passes or fails on a training prescription (Jamie, 17 Oct:
"I don't like a hard 'do 80k and it's all ok'"): weekly km only appears in "how to fix".

  FITNESS     Running fitness score (42-day running load, with the blueprint's cycling
              credit) against where it should be at this point of the build: the race-day
              band (config/event-levels.json) x the phase's share (race_fitness.PHASE_FRACTION).
  ENGINE      VO2max (Garmin's estimate, else the heart-rate one) against the VDOT the goal
              time needs (Daniels & Gilbert).
  THRESHOLD   Against Daniels' threshold pace for that VDOT (THRESHOLD_FRAC of it). With
              running power: critical power from the last 90 days' efforts, against the
              watts that pace needs at the athlete's own economy. Without: pace at threshold
              HR from the speed-on-HR line.
  EFFICIENCY  Running economy, speed per W/kg, against the athlete's own last 3 months
              (power only: Garmin power reads differently from Stryd, so never against
              anyone else's numbers).
  ENDURANCE   The most the athlete has held for a long stretch in training (last 90 days:
              power for half the goal time, else pace for half the distance) against goal
              pace. Training isn't a max effort, so short of it reads "not shown yet", never
              red; it turns green once a session shows it.
  DURABILITY  How much the athlete fades late in their own long runs: power:HR drift and
              cost fade (more watts for the same speed) with power, else pace:HR drift;
              under DRIFT_OK is the usual "aerobically sound" mark.

POWER FIRST (Jamie, 17 Oct 2026: "when we have running power we should be using that where
possible and pace as a fallback / way of communicating"): every power number is also said
as a pace, at the athlete's economy.

Efficiency and durability overlap: poor economy makes every km cost more, so it fades
sooner too (Jamie, 17 Oct 2026). Durability is drift against the start of the same run, so
an uneconomical but steady runner shows on efficiency only; when both flag, efficiency is
named as the cause.

The limiter is the area furthest short. Within GREEN of what's needed is fine, past RED is
red. Thresholds are judgement, here so they can be tuned.
"""
from __future__ import annotations

import run_model as rm

GREEN = 0.03
RED = 0.10
THRESHOLD_FRAC = 0.88            # Daniels: threshold pace ~88% of VDOT
DRIFT_OK = 5.0                   # % pace:HR drift
DRIFT_SCALE = 20.0               # % points of drift over DRIFT_OK that count as 100% short
ENDURANCE_MIN_M, ENDURANCE_MAX_M = 5000.0, 30000.0

LABELS = {"fitness": "Fitness", "engine": "Engine (VO2max)", "threshold": "Threshold",
          "efficiency": "Efficiency (economy)", "endurance": "Endurance at race pace",
          "durability": "Durability"}


def _km(m: float) -> str:
    return f"{m / 1000:.1f}".rstrip("0").rstrip(".")


def _pace(s) -> str:
    s = int(round(s))
    return f"{s // 60}:{s % 60:02d}"


def _status(short) -> str:
    if short is None:
        return "unknown"
    return "green" if short <= GREEN else "amber" if short <= RED else "red"


def threshold_pace_for(vdot: float) -> float:
    """Seconds per km at THRESHOLD_FRAC of a VDOT (Daniels' oxygen cost of speed)."""
    target = THRESHOLD_FRAC * vdot
    lo, hi = 100.0, 500.0                        # metres per minute
    for _ in range(60):
        v = (lo + hi) / 2
        if -4.60 + 0.182258 * v + 0.000104 * v * v > target:
            hi = v
        else:
            lo = v
    return 60000.0 / v


def _row(key, you, need, short, source=None, status=None, label=None, why=None) -> dict:
    s = None if short is None else round(max(0.0, short), 3)
    return {"key": key, "label": label or LABELS[key], "you": you, "need": need, "short": s,
            "status": status or _status(s), "source": source, "why": why}


def _power_at(best: dict, secs: float):
    """(secs, watts): the longest curve point up to `secs` (keys may be str after JSON)."""
    pts = sorted((int(k), w) for k, w in (best or {}).items() if w and int(k) <= secs + 1)
    return pts[-1] if pts else (None, None)


def _endurance_effort(dist_m: float, training: list) -> dict | None:
    """The training effort at the longest distance up to half the race (5-30 km)."""
    target = min(ENDURANCE_MAX_M, max(ENDURANCE_MIN_M, dist_m / 2))
    fits = [e for e in training or [] if e.get("dist_m") and e["dist_m"] <= target + 1]
    return max(fits, key=lambda e: e["dist_m"]) if fits else None


def diagnose(dist_m: float, goal_s: int, goal: str, run: dict, fitness: dict | None,
             phase_frac: float | None = None) -> dict | None:
    """{"rows": [...], "limiter": key | None, "headline", "fix", "foot"} for a run goal,
    or None without a goal time or training data. `phase_frac`: how much of the race-day
    fitness band is due by now (None: the whole band)."""
    if not dist_m or not goal_s or not run:
        return None
    need_vdot = rm.vdot(dist_m, goal_s)
    goal_pace = goal_s / (dist_m / 1000)
    rows = []

    if fitness and fitness.get("now") is not None:
        due = fitness["lo"] * (phase_frac or 1.0)
        need = (f"~{due:.0f} by now · {fitness['lo']}–{fitness['hi']} race day"
                if phase_frac and phase_frac < 1 else f"{fitness['lo']}–{fitness['hi']}")
        rows.append(_row("fitness", f"{fitness['now']:.0f}", need, (due - fitness["now"]) / due))
    else:
        rows.append(_row("fitness", "—", "—", None))

    eng = run.get("engine")
    if eng:
        rows.append(_row("engine", f"{eng['vo2max']:.0f}", f"~{need_vdot:.0f}",
                         (need_vdot - eng["vo2max"]) / need_vdot, source=eng["source"]))
    else:
        rows.append(_row("engine", "—", f"~{need_vdot:.0f}", None))

    need_pace = threshold_pace_for(need_vdot)
    pw, eco = run.get("power"), run.get("economy")
    kg = (pw or {}).get("weight")
    e = (eco or {}).get("value")
    if pw and e and kg:
        thr_speed = e * pw["cp"] / kg
        need_w = (1000 / need_pace) / e * kg
        rows.append(_row("threshold", f"{pw['cp']} W (≈{_pace(1000 / thr_speed)}/km)",
                         f"~{need_w:.0f} W ({_pace(need_pace)}/km)", (need_w - pw["cp"]) / need_w,
                         source="power", label="Threshold power"))
        change = e / eco["prev"] - 1 if eco.get("prev") else None
        rows.append(_row("efficiency", f"{e:.2f} m/s per W/kg" +
                         (f" · {change * 100:+.0f}% on before" if change is not None else ""),
                         "holding or better", None if change is None else -change, source="power"))
    else:
        thr = run.get("threshold")
        if thr:
            rows.append(_row("threshold", f"{_pace(thr['pace_s_km'])}/km", f"~{_pace(need_pace)}/km",
                             (thr["pace_s_km"] - need_pace) / need_pace, source=thr["source"]))
        else:
            rows.append(_row("threshold", "—", f"~{_pace(need_pace)}/km", None))
        rows.append(_row("efficiency", "needs running power", "—", None))

    eff = None
    secs, watts = _power_at((pw or {}).get("best"), goal_s / 2) if pw and e and kg else (None, None)
    if secs and secs >= 600:
        need_goal_w = (1000 / goal_pace) / e * kg
        short = (need_goal_w - watts) / need_goal_w
        eff = {"secs": secs, "watts": watts, "pace": 1000 / (e * watts / kg)}
        rows.append(_row("endurance", f"{watts:.0f} W for {secs // 60} min (≈{_pace(eff['pace'])}/km)",
                         f"~{need_goal_w:.0f} W ({_pace(goal_pace)}/km) for {_km(dist_m)} km", short,
                         source="power, last 90 days", status="green" if short <= GREEN else "unshown"))
    else:
        effort = _endurance_effort(dist_m, run.get("training"))
        if effort:
            pace = effort["time_s"] / (effort["dist_m"] / 1000)
            short = (pace - goal_pace) / goal_pace
            eff = {"dist_m": effort["dist_m"], "pace": pace}
            rows.append(_row("endurance", f"{_pace(pace)}/km for {_km(effort['dist_m'])} km",
                             f"{_pace(goal_pace)}/km for {_km(dist_m)} km", short,
                             source="training, last 90 days",
                             status="green" if short <= GREEN else "unshown"))
        else:
            rows.append(_row("endurance", "—", f"{_pace(goal_pace)}/km for {_km(dist_m)} km", None))

    dur = run.get("durability")
    if dur and dur.get("drift_pct") is not None:
        parts = [(max(0.0, dur["drift_pct"] - DRIFT_OK) / DRIFT_SCALE, "drift")]
        you = [f"{dur['drift_pct']:+.0f}% drift"]
        if dur.get("cost_fade_pct") is not None:
            parts.append((max(0.0, dur["cost_fade_pct"] - DRIFT_OK) / DRIFT_SCALE, "cost"))
            you.append(f"{dur['cost_fade_pct']:+.0f}% cost")
        short, why = max(parts)
        rows.append(_row("durability", " · ".join(you) + f" · {dur['longest_km']:.0f} km longest",
                         f"under {DRIFT_OK:.0f}%" + (" each" if len(parts) > 1 else " drift"), short,
                         source=f"{dur.get('source') or 'pace'}, {dur['n']} long runs", why=why))
    else:
        rows.append(_row("durability", "no long runs lately" if not dur else "—",
                         f"under {DRIFT_OK:.0f}% drift", None))

    by = {r["key"]: r for r in rows}
    flagged = [r for r in rows if r["status"] in ("amber", "red")]
    limiter = max(flagged, key=lambda r: r["short"])["key"] if flagged else None
    also = None
    if limiter == "durability" and any(by[k]["status"] in ("amber", "red") for k in ("efficiency",)):
        limiter, also = "efficiency", "It also shows up late in your long runs."
    unshown = by["endurance"]["status"] == "unshown"
    if limiter is None and unshown:
        limiter = "endurance"
    return {"rows": rows, "limiter": limiter,
            "headline": _headline(limiter, by, goal, fitness, run, eff, goal_pace, also),
            "fix": _fix(limiter, run, dist_m),
            "levers": run_levers(dist_m, goal_s, run),
            "foot": _foot(run)}


# ── Where the minutes are (Jamie, 17 Oct 2026: "minutes per fix on every card") ─────
# What a realistic gain in each lever is worth on race day, each on its own (they overlap,
# so they don't add up). Gains are a block's worth, judgement: threshold +3%, economy +2%,
# VO2max +2 (only up to 10 km: past that, threshold decides), 10 km more running a week
# (Vickers & Vertosick 2016's mileage term, scaled down for shorter races).

def _lever(label, minutes, why=None) -> dict | None:
    return {"label": label, "minutes": round(minutes), "why": why} if minutes and minutes >= 0.5 else None


def run_levers(dist_m: float, goal_s, run: dict) -> list:
    ev = next((k for k, m in rm.STANDARD_M.items() if abs(m - dist_m) < 500), None)
    t = ((run.get("predictions") or {}).get(ev) or {}).get("now_s") or goal_s
    if not t:
        return []
    out = [_lever("Threshold +3%", t * (1 - 1 / 1.03) / 60, "threshold sessions"),
           _lever("Economy +2%", t * (1 - 1 / 1.02) / 60, "strides, hills, strength")]
    vo2 = (run.get("engine") or {}).get("vo2max")
    if vo2 and dist_m <= 10500:
        out.append(_lever("VO2max +2", t * (1 - 1 / (1 + 2 / vo2)) / 60, "VO2max intervals"))
    v = dist_m / t
    dv = 0.06423826 * (10 / rm.KM_PER_MILE) / 10 * min(1.0, dist_m / 42195)
    out.append(_lever("10 km more running a week", (t - dist_m / (v + dv)) / 60, "volume"))
    return sorted([x for x in out if x], key=lambda x: -x["minutes"])


def _headline(limiter, by, goal, fitness, run, eff, goal_pace, also) -> str:
    g = f"for {goal}" if goal else "for your goal"
    if limiter is None and not any(x["status"] in ("green", "amber", "red", "unshown") for x in by.values()):
        return "Not enough measured yet to say what's holding you back."
    if limiter is None:
        return f"Nothing is holding you back {g} on these numbers."
    r = by[limiter]
    if limiter == "fitness":
        return f"Fitness is the issue: your running fitness is {r['you']}, against {r['need']}."
    if limiter == "engine":
        return f"Your engine is the issue: VO2max {r['you']} against {r['need']} {g}."
    if limiter == "threshold":
        return f"Threshold is the issue: {r['you']} against {r['need']} {g}."
    if limiter == "efficiency":
        drop = r["short"] * 100
        return (f"Efficiency is the issue: your economy is {drop:.0f}% down on your last few months."
                + (f" {also}" if also else ""))
    if limiter == "durability":
        d = run["durability"]
        if r["why"] == "cost":
            return (f"Durability is the issue: late in long runs you need {d['cost_fade_pct']:.0f}% more "
                    f"power for the same pace (under {DRIFT_OK:.0f}% is sound).")
        return (f"Durability is the issue: you drift {d['drift_pct']:.0f}% late in long runs "
                f"(under {DRIFT_OK:.0f}% is sound).")
    have = [x["label"].split(" (")[0].lower() for x in by.values()
            if x["key"] in ("engine", "threshold") and x["status"] == "green"]
    lead = f"You have the {' and '.join(have)} {g}. " if have else ""
    if eff and eff.get("secs"):
        best = f"Your best {eff['secs'] // 60} min in training is {r['you'].split(' for')[0]} (≈{_pace(eff['pace'])}/km)."
    elif eff:
        best = f"Your fastest {_km(eff['dist_m'])} km in training is {_pace(eff['pace'])}/km."
    else:
        best = ""
    return f"{lead}Not shown yet: holding {_pace(goal_pace)}/km for long. {best}".strip()


def _fix(limiter, run, dist_m) -> str | None:
    km = run.get("weeklyKm")
    if limiter == "fitness":
        return (f"More running builds it: you're at {km:.0f} km a week." if km is not None
                else "More running builds it.")
    if limiter == "endurance":
        return ("Race-pace long runs show it." if dist_m >= 21000
                else "A race-pace session or a time trial shows it.")
    if limiter == "engine":
        return "VO2max intervals (3-5 min hard) raise it."
    if limiter == "threshold":
        return "Threshold sessions (3x10 min, 2x20 min) raise it."
    if limiter == "efficiency":
        return "Strides, hill reps and strength work help it."
    if limiter == "durability":
        return "Longer steady runs with a strong finish, fuelled, build it."
    return None


def _foot(run) -> str:
    bits = ["Each measured from your training against what the goal time needs."]
    if run.get("power") and run.get("economy"):
        bits.append("Threshold power is critical power from your last 90 days of efforts. Economy is "
                    "speed per watt against your own last 3 months: Garmin power reads differently "
                    "from Stryd, so it isn't compared with anyone else. Paces are your watts at your economy.")
    else:
        thr = run.get("threshold") or {}
        if thr.get("source") == "heart rate":
            bits.append(f"Threshold pace is estimated from your runs' heart rate (they reach "
                        f"{thr['hr_top']} bpm, threshold {thr['lthr']}): a hard 5k or 30-minute "
                        f"effort would measure it.")
        elif thr.get("source"):
            bits.append("Threshold pace is the one set in Intervals, not measured here.")
    eng = run.get("engine") or {}
    if eng.get("source") == "Garmin":
        bits.append("VO2max is Garmin's estimate.")
    elif eng.get("source"):
        bits.append("VO2max is estimated from your runs' heart rate.")
    return " ".join(bits)


# ── Bike: what the course demands (lib/course.py) ─────────────────────────────
#
# Outdoor ride times aren't predicted (wind, drafting: Jamie, 17 Oct 2026); what the course
# needs is. CLIMBING: the steady power on the climbs at which every cut-off leaves no more
# than REST_KMH to find on the flats and descents (a bunch on the flat, the descents),
# against what the rider can hold on climbs in a long day (CLIMB_IF of FTP). DAY LENGTH:
# the longest ride shown against the time the finish cut-off allows. DURABILITY and
# EFFICIENCY need power outdoors: without a power meter the card says so rather than guess
# (Jamie, 17 Oct 2026: "yes, needs power meter!"). FUELLING: carbs logged per hour on long
# rides against FUEL_G_H, the evidence-based range for rides over 2.5 h (Jeukendrup 2014).

REST_KMH = 30.0
CLIMB_IF = 0.80
DAY_SHOWN = 0.75                 # longest ride at this share of the day reads as shown
FUEL_G_H = (60, 90)

BIKE_LABELS = {"fitness": "Fitness", "climbing": "Climbing power", "day": "Day length",
               "durability": "Durability", "fuelling": "Fuelling"}


def _brow(key, you, need, short, status=None, source=None, why=None) -> dict:
    s = None if short is None else round(max(0.0, short), 3)
    return {"key": key, "label": BIKE_LABELS[key], "you": you, "need": need, "short": s,
            "status": status or _status(s), "source": source, "why": why}


def _hm(minutes) -> str:
    m = int(round(minutes))
    return f"{m // 60}h{m % 60:02d}"


def diagnose_bike(course: dict | None, bike: dict | None, fitness: dict | None,
                  phase_frac: float | None = None, goal: str | None = None) -> dict | None:
    """The "What's holding you back" card for a bike event: rows measured from training
    against what the course demands. Without a course only fitness and the measures are
    shown, and the card asks for the route."""
    import course as cs
    if not bike and not fitness:
        return None
    bike = bike or {}
    rows = []
    if fitness and fitness.get("now") is not None:
        due = fitness["lo"] * (phase_frac or 1.0)
        need = (f"~{due:.0f} by now · {fitness['lo']}–{fitness['hi']} race day"
                if phase_frac and phase_frac < 1 else f"{fitness['lo']}–{fitness['hi']}")
        rows.append(_brow("fitness", f"{fitness['now']:.0f}", need, (due - fitness["now"]) / due))
    else:
        rows.append(_brow("fitness", "—", "—", None))

    ftp, kg = bike.get("ftp"), bike.get("weight")
    binding, need_w = None, None
    if course and ftp and kg:
        hold = CLIMB_IF * ftp
        need_w = cs.climb_power_for(course, kg, REST_KMH)
        at_hold = cs.needs(course, hold, kg)
        binding = (max(at_hold, key=lambda n: n["rest_kmh"] if n["rest_kmh"] is not None else 999)
                   if at_hold else None)
        if need_w:
            rows.append(_brow("climbing", f"{ftp} W FTP ({ftp / kg:.1f} W/kg) · ~{hold:.0f} W on long-day climbs",
                              f"~{need_w:.0f} W on the climbs ({need_w / kg:.1f} W/kg)",
                              (need_w - hold) / need_w,
                              source="indoor power" if not bike.get("outdoor", {}).get("with_power") else "power"))
        else:
            rows.append(_brow("climbing", f"{ftp} W FTP", "—", None))
    else:
        rows.append(_brow("climbing", f"{ftp} W FTP" if ftp else "—",
                          "send the route (GPX)" if not course else "—", None))

    if course:
        fin = next((c for c in course.get("checkpoints") or [] if c.get("type") == "finish"), None)
        day_h = (cs._clock_min(fin["clock"]) - cs._clock_min(course["start"])) / 60 if fin else None
        if day_h and bike.get("longest_h") is not None and course.get("start"):
            short = (DAY_SHOWN * day_h - bike["longest_h"]) / (DAY_SHOWN * day_h)
            rows.append(_brow("day", f"{bike['longest_h']:.1f} h longest ride (90 days)",
                              f"up to {day_h:.1f} h to the finish cut-off", short,
                              status="green" if short <= GREEN else "unshown"))
        else:
            rows.append(_brow("day", "—", "—", None))
    else:
        rows.append(_brow("day", f"{bike.get('longest_h', 0):.1f} h longest ride" if bike.get("longest_h") else "—",
                          "send the route (GPX)", None))

    out_rides = bike.get("outdoor") or {}
    no_meter = out_rides.get("rides") and not out_rides.get("with_power")
    dur = bike.get("durability")
    if dur:
        rows.append(_brow("durability", f"{dur['drift_pct']:+.0f}% drift on 3h+ rides",
                          f"under {DRIFT_OK:.0f}%", max(0.0, dur["drift_pct"] - DRIFT_OK) / DRIFT_SCALE,
                          source=f"power, {dur['n']} long rides"))
    else:
        rows.append(_brow("durability", "needs a power meter" if no_meter else "no long rides with power",
                          f"under {DRIFT_OK:.0f}% drift", None, why="power" if no_meter else None))

    fuel = bike.get("fuel")
    if fuel:
        rows.append(_brow("fuelling", f"{fuel['g_per_h']} g carbs an hour", f"{FUEL_G_H[0]}–{FUEL_G_H[1]} g an hour",
                          (FUEL_G_H[0] - fuel["g_per_h"]) / FUEL_G_H[0], source=f"{fuel['n']} long rides"))
    else:
        rows.append(_brow("fuelling", "not logged on long rides", f"{FUEL_G_H[0]}–{FUEL_G_H[1]} g an hour", None))

    by = {r["key"]: r for r in rows}
    flagged = [r for r in rows if r["status"] in ("amber", "red")]
    limiter = max(flagged, key=lambda r: r["short"])["key"] if flagged else None
    if limiter is None and by["day"]["status"] == "unshown":
        limiter = "day"
    head = _bike_headline(limiter, by, course, bike, binding, need_w)
    return {"rows": rows, "limiter": limiter, "headline": head,
            "fix": _bike_fix(limiter, course, bike), "levers": bike_levers(course, bike),
            "foot": _bike_foot(course, bike, no_meter)}


def bike_levers(course, bike) -> list:
    """Minutes off the climbs to the binding cut-off: +10 W of FTP, 3 kg lighter (physics)."""
    import course as cs
    if not course or not course.get("start") or not bike or not bike.get("ftp") or not bike.get("weight"):
        return []
    kg, hold = bike["weight"], CLIMB_IF * bike["ftp"]
    at = cs.needs(course, hold, kg)
    if not at:
        return []
    km = max(at, key=lambda n: n["rest_kmh"] if n["rest_kmh"] is not None else 999)["km"]
    base = cs.climbing_split(course["ele"], 0, km, hold, kg + cs.KIT_KG)[0]
    w10 = base - cs.climbing_split(course["ele"], 0, km, hold + CLIMB_IF * 10, kg + cs.KIT_KG)[0]
    kg3 = base - cs.climbing_split(course["ele"], 0, km, hold, kg - 3 + cs.KIT_KG)[0]
    return sorted([x for x in (_lever("FTP +10 W", w10 / 60, f"on the climbs to km {km:g}"),
                               _lever("3 kg lighter", kg3 / 60, f"on the climbs to km {km:g}")) if x],
                  key=lambda x: -x["minutes"])


def _bike_headline(limiter, by, course, bike, binding, need_w) -> str:
    if not course:
        return "Send the route (a GPX) and the time limits in the chat, and the coach works out what this race needs."
    if limiter is None and not course.get("start"):
        return "Send the start time and cut-offs in the chat, and the coach works out what this route needs."
    if limiter is None and not any(r["status"] in ("green", "amber", "red", "unshown") for r in by.values()):
        return "Not enough measured yet to say what's holding you back."
    if limiter is None:
        return "Nothing is holding you back on these numbers."
    r = by[limiter]
    if limiter == "climbing" and binding:
        hold = CLIMB_IF * bike["ftp"]
        return (f"Climbing power is the issue: at ~{hold:.0f} W the climbs to {binding['label']} take "
                f"{_hm(binding['climb_min'])}, so the other {binding['rest_km']:.0f} km would need "
                f"{binding['rest_kmh']:.0f} km/h to make {binding['clock']}"
                + (" (miss it and it's a shorter route)" if binding.get("on_miss") else "")
                + f". About {need_w:.0f} W on the climbs makes it at {REST_KMH:.0f} km/h.")
    if limiter == "fitness":
        return f"Fitness is the issue: your fitness is {r['you']}, against {r['need']}."
    if limiter == "day":
        return f"Not shown yet: a day this long. Your longest ride lately is {r['you'].split(' longest')[0]}; the cut-offs allow {r['need'].split('up to ')[1].split(' to')[0]}."
    if limiter == "durability":
        return f"Durability is the issue: you drift {bike['durability']['drift_pct']:.0f}% on rides over 3 hours."
    return f"Fuelling is the issue: {r['you']} on long rides, against {r['need']}."


def _bike_fix(limiter, course, bike) -> str | None:
    import course as cs
    if limiter == "climbing" and course and bike.get("ftp") and bike.get("weight"):
        kg, hold = bike["weight"], CLIMB_IF * bike["ftp"]
        km = next((c["km"] for c in course.get("checkpoints") or [] if c.get("type") == "cutoff"), None) or 100
        base = cs.climbing_split(course["ele"], 0, km, hold, kg + cs.KIT_KG)[0]
        w10 = base - cs.climbing_split(course["ele"], 0, km, hold + CLIMB_IF * 10, kg + cs.KIT_KG)[0]
        kg3 = base - cs.climbing_split(course["ele"], 0, km, hold, kg - 3 + cs.KIT_KG)[0]
        return (f"Raise FTP and carry less: each 10 W of FTP saves ~{w10 / 60:.0f} min on the climbs to "
                f"km {km:g}, and each 3 kg saves ~{kg3 / 60:.0f} min.")
    if limiter == "day":
        return "Build the longest ride over the season, fuelled as on the day."
    if limiter == "fitness":
        return "More riding builds it."
    if limiter == "durability":
        return "Long steady rides, fuelled, with the last hour at target power."
    if limiter == "fuelling":
        return "Practise 60-90 g of carbs an hour on long rides."
    return None


def _bike_foot(course, bike, no_meter) -> str:
    import course as cs
    bits = []
    if course:
        bits.append(f"Climb times are physics (no drafting on a climb); the flats and descents aren't "
                    f"predicted, only the speed they'd need. Assumes {CLIMB_IF:.0%} of FTP on the climbs, "
                    f"cut-offs from the gun ({course.get('year', '')}), {cs.STOP_MIN_PER_FEED} min at each feed.")
    if no_meter:
        bits.append("Your outdoor rides have no power, so FTP comes from indoor sessions and durability "
                    "can't be measured: a power meter on the road bike would fill those in.")
    bits.append("Fuelling counts carbs logged on rides over 3 hours.")
    return " ".join(b.strip() for b in bits)


# ── Triathlon (lib/tri.py) ────────────────────────────────────────────────────
#
# Each leg predicted from the athlete's own measures and set against the split the goal
# needs; the leg costing the most minutes is the limiter. Status by % over the split:
# within TRI_GREEN fine, past TRI_RED red. Validated on Cervia 2026, 8 weeks out:
# Jamie swim +2.4%, bike -3.6% (a windier year), run +16.5% (his off-the-bike factor
# carried two Ironman runs with walk breaks, which he fixed); James swim -1.4%, bike +3.5%.

TRI_GREEN, TRI_RED = 0.02, 0.06
TRI_LABELS = {"fitness": "Fitness", "swim": "Swim", "bike": "Bike", "run": "Run off the bike",
              "durability": "Durability", "fuelling": "Fuelling"}


def _trow(key, you, need, short, minutes=None, status=None, source=None) -> dict:
    s = None if short is None else round(max(0.0, short), 3)
    st = status or ("unknown" if s is None else "green" if s <= TRI_GREEN else "amber" if s <= TRI_RED else "red")
    return {"key": key, "label": TRI_LABELS[key], "you": you, "need": need, "short": s,
            "minutes": None if minutes is None else round(minutes), "status": st, "source": source}


def _hms(s) -> str:
    s = int(round(s))
    return f"{s // 3600}:{s % 3600 // 60:02d}"


def diagnose_tri(kind: str, splits: dict | None, legs: dict, fitness: dict | None,
                 phase_frac: float | None = None, durability: dict | None = None,
                 fuel: dict | None = None, goal: str | None = None) -> dict | None:
    """`legs`: {"swim": {"time_s", "you"}, "bike": {...}, "run": {...}} - predicted seconds and
    a "you" line saying how (lib/tri.py via season_view). `splits`: the goal's seconds per leg
    (tri.goal_splits) or None - then the legs are shown without a verdict."""
    rows = []
    if fitness and fitness.get("now") is not None:
        due = fitness["lo"] * (phase_frac or 1.0)
        need = (f"~{due:.0f} by now · {fitness['lo']}–{fitness['hi']} race day"
                if phase_frac and phase_frac < 1 else f"{fitness['lo']}–{fitness['hi']}")
        short = max(0.0, (due - fitness["now"]) / due)
        rows.append(_trow("fitness", f"{fitness['now']:.0f}", need, short,
                          status="green" if short <= GREEN else "amber" if short <= RED else "red"))
    else:
        rows.append(_trow("fitness", "—", "—", None))
    for leg in ("swim", "bike", "run"):
        p, need = (legs.get(leg) or {}).get("time_s"), (splits or {}).get(leg)
        you = (legs.get(leg) or {}).get("you") or "—"
        if p and need:
            rows.append(_trow(leg, you, _hms(need), (p - need) / need, (p - need) / 60,
                              source=(legs.get(leg) or {}).get("source")))
        else:
            rows.append(_trow(leg, you, _hms(need) if need else "no goal split", None,
                              source=(legs.get(leg) or {}).get("source")))
    d = durability or {}
    if d.get("drift_pct") is not None:
        rows.append(_trow("durability", f"{d['drift_pct']:+.0f}% drift on long sessions", f"under {DRIFT_OK:.0f}%",
                          max(0.0, d["drift_pct"] - DRIFT_OK) / DRIFT_SCALE,
                          status="green" if d["drift_pct"] <= DRIFT_OK else "amber" if d["drift_pct"] <= 10 else "red"))
    else:
        rows.append(_trow("durability", "—", f"under {DRIFT_OK:.0f}%", None))
    if fuel:
        short = max(0.0, (FUEL_G_H[0] - fuel["g_per_h"]) / FUEL_G_H[0])
        rows.append(_trow("fuelling", f"{fuel['g_per_h']} g carbs an hour", f"{FUEL_G_H[0]}–{FUEL_G_H[1]} g an hour",
                          short, status="green" if short <= GREEN else "amber" if short <= RED else "red"))
    else:
        rows.append(_trow("fuelling", "not logged on long sessions", f"{FUEL_G_H[0]}–{FUEL_G_H[1]} g an hour", None))
    legs_flagged = [r for r in rows if r["key"] in ("swim", "bike", "run") and r["status"] in ("amber", "red")]
    other = [r for r in rows if r["key"] not in ("swim", "bike", "run") and r["status"] in ("amber", "red")]
    sev = {"red": 2, "amber": 1}
    limiter = (max(legs_flagged, key=lambda r: r["minutes"] or 0)["key"] if legs_flagged
               else max(other, key=lambda r: (sev[r["status"]], r["short"] or 0))["key"] if other else None)
    by = {r["key"]: r for r in rows}
    g = f"for {goal}" if goal else "for your goal"
    if not splits:
        head = "Set a goal time (or target splits) and the coach shows which leg costs you most."
    elif limiter in ("swim", "bike", "run"):
        r = by[limiter]
        head = (f"{r['label']} is the issue: about {_hms(legs[limiter]['time_s'])} against "
                f"{r['need']} {g} (+{r['minutes']} min).")
    elif limiter:
        head = f"{by[limiter]['label']} is the issue: {by[limiter]['you']}, against {by[limiter]['need']}."
    elif not any(r["status"] in ("green", "amber", "red") for r in rows):
        head = "Not enough measured yet to say what's holding you back."
    else:
        head = f"Nothing is holding you back {g} on these numbers."
    fix = {"swim": "CSS sets and open-water swims with sighting.",
           "bike": "Long rides at race power, and FTP work in the build.",
           "run": "Runs off long rides, the race fuelling practised, and a bike paced to leave a run.",
           "fitness": "More training builds it.", "durability": "Long steady sessions, fuelled, with a strong finish.",
           "fuelling": "Practise 60-90 g of carbs an hour on long sessions."}.get(limiter)
    foot = ("Each leg from your own numbers: swim from CSS and how much slower your race swims are; "
            "bike from your past rides on this course (or the course and a typical age-group position "
            "the first time), wind moving it by 5-10% either way; run from your open-run fitness and how "
            "much your past triathlon runs slowed.")
    levers = sorted([x for x in (_lever(l["label"], l["minutes"], l.get("why"))
                                 for leg in legs.values() for l in leg.get("levers") or []) if x],
                    key=lambda x: -x["minutes"])
    return {"rows": rows, "limiter": limiter, "headline": head, "fix": fix, "levers": levers, "foot": foot}


# ── The plan nudge ─────────────────────────────────────────────────────────────
#
# Jamie, 17 Oct 2026: "lets not throw away the blueprints, it's just something to subtly
# influence it". The blueprint keeps the volume, phases, zone targets, TSS and the long
# sessions; the limiter only steers WHICH quality session fills the quality minutes and
# what the long session finishes with. Fitness has no nudge: volume is the blueprint's job.

NUDGES = {
    "run": {
        "engine": "Run quality: give the Z4-5 share to VO2 reps (3-5 min at 3k-5k effort) rather than shorter speed work.",
        "threshold": "Run quality: give the Z3 share to threshold work (cruise intervals, 2x15-20 min) rather than tempo variety.",
        "efficiency": "Run: add strides (6-8 x 20 s) to two easy runs and make the Z4-5 touch hill reps; almost no extra load.",
        "endurance": "Long run: when it allows, finish the last 20-30 min at goal race pace (inside its cap).",
        "durability": "Long run: steady and fuelled as on race day, the final 15-20 min at goal pace.",
    },
    "bike": {
        "climbing": "Bike quality: threshold work as seated climbing repeats (or low-cadence big-gear efforts indoors) rather than flat sweetspot.",
        "day": "Long ride: fuelled exactly as on the day (60-90 g carbs an hour), steady to the end.",
        "durability": "Long ride: the closing block at race effort.",
        "fuelling": "Long ride: practise 60-90 g of carbs an hour and log it.",
    },
    "tri": {
        "swim": "Swim: the CSS set stays the key swim; add sighting every 6-8 strokes on the long swim.",
        "bike": "Bike: the long ride's closing block at race power (NP target) rather than sweetspot.",
        "run": "Run: a short run straight off the long ride (15-30 min at race pace) when a brick fits the day rules.",
        "durability": "Long ride and long run: steady, fuelled, finishing at race effort.",
        "fuelling": "Long ride: practise 60-90 g of carbs an hour and log it.",
    },
}


def card_type(card: dict) -> str:
    keys = {r.get("key") for r in (card or {}).get("rows") or []}
    return "bike" if "climbing" in keys else "tri" if "swim" in keys else "run"


def nudge(card: dict | None) -> dict | None:
    """{"area", "headline", "nudge"} for the weekly planning brief, or None when nothing is
    flagged, the area has no nudge, or there is no card."""
    if not card or not card.get("limiter"):
        return None
    row = next((r for r in card.get("rows") or [] if r.get("key") == card["limiter"]), {})
    if row.get("status") not in ("amber", "red", "unshown"):
        return None
    text = NUDGES[card_type(card)].get(card["limiter"])
    if not text:
        return None
    return {"area": card["limiter"], "headline": card.get("headline"), "nudge": text}
