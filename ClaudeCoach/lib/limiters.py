"""What is holding the athlete back from their run goal (Jamie, 17 Oct 2026: "I want the
app to be able to say: your fitness is the issue, or it's your durability or efficiency").

Four areas, each measured from TRAINING (lib/performance.py), each against what the goal
time needs:

  FITNESS     Running fitness (with the blueprint's cycling credit) and weekly km, against
              the race-day band and weekly km for the goal's level (config/event-levels.json,
              the same tables the plan is built from).
  ENGINE      VO2max (Garmin's estimate, else the heart-rate one) against the VDOT the goal
              time needs (Daniels & Gilbert).
  EFFICIENCY  Threshold pace, fresh, against Daniels' threshold pace for that VDOT
              (THRESHOLD_FRAC of it): how well the engine is turned into speed - economy
              and how much of VO2max can be held.
  DURABILITY  How much the athlete drops late in their own long runs (pace:HR drift; under
              DRIFT_OK is the usual "aerobically sound" mark) and their longest run against
              the level's long run.

Efficiency and durability overlap: poor economy makes every km cost more, so it fades
sooner too (Jamie, 17 Oct 2026). Durability is measured against the start of the same
run, so an uneconomical but steady runner shows on efficiency only; when both flag and
the durability problem is drift, efficiency is named as the cause.

The limiter is the area furthest short of the goal. An area within GREEN of what's needed
is fine, past RED it is red. Thresholds are judgement, here so they can be tuned.
"""
from __future__ import annotations

import run_model as rm

GREEN = 0.03
RED = 0.10
THRESHOLD_FRAC = 0.88            # Daniels: threshold pace ~88% of VDOT
DRIFT_OK = 5.0                   # % pace:HR drift
DRIFT_SCALE = 20.0               # % points of drift over DRIFT_OK that count as 100% short

LABELS = {"fitness": "Fitness", "engine": "Engine (VO2max)", "efficiency": "Efficiency",
          "durability": "Durability"}


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


def _row(key, you, need, short, source=None, why=None) -> dict:
    return {"key": key, "label": LABELS[key], "you": you, "need": need,
            "short": None if short is None else round(max(0.0, short), 3),
            "status": _status(None if short is None else max(0.0, short)),
            "source": source, "why": why}


def diagnose(dist_m: float, goal_s: int, goal: str, run: dict, fitness: dict | None,
             level: dict | None) -> dict | None:
    """{"rows": [...], "limiter": key | None, "headline", "foot"} for a run goal, or None
    without a goal time or training data."""
    if not dist_m or not goal_s or not run:
        return None
    need_vdot = rm.vdot(dist_m, goal_s)
    rows = []

    # Fitness: running fitness and weekly km against the goal level.
    km = run.get("weeklyKm")
    km_band = (level or {}).get("run_km")
    parts, you, need, why = [], [], [], None
    if fitness and fitness.get("now") is not None:
        parts.append(((fitness["lo"] - fitness["now"]) / fitness["lo"], "fitness"))
        you.append(f"{fitness['now']:.0f} fitness")
        need.append(f"{fitness['lo']}–{fitness['hi']}")
    if km is not None and km_band:
        parts.append(((km_band[0] - km) / km_band[0], "km"))
        you.append(f"{km:.0f} km a week")
        need.append(f"{km_band[0]}–{km_band[1]} km")
    if parts:
        short, why = max(parts)
        rows.append(_row("fitness", " · ".join(you), " · ".join(need), short, why=why))
    else:
        rows.append(_row("fitness", "—", "—", None))

    eng = run.get("engine")
    if eng and need_vdot:
        rows.append(_row("engine", f"{eng['vo2max']:.0f}", f"~{need_vdot:.0f}",
                         (need_vdot - eng["vo2max"]) / need_vdot, source=eng["source"]))
    else:
        rows.append(_row("engine", "—", f"~{need_vdot:.0f}" if need_vdot else "—", None))

    thr = run.get("threshold")
    need_pace = threshold_pace_for(need_vdot) if need_vdot else None
    if thr and need_pace:
        rows.append(_row("efficiency", f"{_pace(thr['pace_s_km'])}/km threshold",
                         f"~{_pace(need_pace)}/km", (thr["pace_s_km"] - need_pace) / need_pace,
                         source=thr["source"]))
    else:
        rows.append(_row("efficiency", "—", f"~{_pace(need_pace)}/km" if need_pace else "—", None))

    dur = run.get("durability")
    long_band = (level or {}).get("long_run_km")
    if dur:
        parts, you, need = [], [], []
        if dur.get("drift_pct") is not None:
            parts.append((max(0.0, dur["drift_pct"] - DRIFT_OK) / DRIFT_SCALE, "drift"))
            you.append(f"{dur['drift_pct']:+.0f}% drift")
            need.append(f"under {DRIFT_OK:.0f}%")
        if long_band:
            parts.append(((long_band[0] - dur["longest_km"]) / long_band[0], "long"))
            you.append(f"{dur['longest_km']:.0f} km longest")
            need.append(f"{long_band[0]}–{long_band[1]} km")
        if parts:
            short, why = max(parts)
            rows.append(_row("durability", " · ".join(you), " · ".join(need), short,
                             source=f"{dur['n']} long runs", why=why))
        else:
            rows.append(_row("durability", "—", "—", None))
    else:
        rows.append(_row("durability", "no long runs lately",
                         f"{long_band[0]}–{long_band[1]} km" if long_band else "—", None))

    by = {r["key"]: r for r in rows}
    flagged = [r for r in rows if r["status"] in ("amber", "red")]
    limiter = max(flagged, key=lambda r: r["short"])["key"] if flagged else None
    also = None
    if (limiter == "durability" and by["durability"]["why"] == "drift"
            and by["efficiency"]["status"] in ("amber", "red")):
        limiter, also = "efficiency", "It also shows up late in your long runs."
    return {"rows": rows, "limiter": limiter,
            "headline": _headline(limiter, by, goal, fitness, km_band, long_band, run, also),
            "foot": _foot(by, run)}


def _headline(limiter, by, goal, fitness, km_band, long_band, run, also) -> str:
    g = f"for {goal}" if goal else "for your goal"
    if limiter is None:
        return f"Nothing is holding you back {g} on these numbers."
    r = by[limiter]
    if limiter == "fitness":
        if r["why"] == "km":
            s = f"Fitness is the issue: {run['weeklyKm']:.0f} km a week against {km_band[0]}–{km_band[1]} {g}."
        else:
            s = f"Fitness is the issue: running fitness {fitness['now']:.0f} against {fitness['lo']}–{fitness['hi']} {g}."
    elif limiter == "engine":
        s = f"Your engine is the issue: VO2max {r['you']} against {r['need']} {g}."
    elif limiter == "efficiency":
        s = f"Efficiency is the issue: {r['you']} against {r['need']} {g}."
    elif r["why"] == "drift":
        s = (f"Durability is the issue: you drift {run['durability']['drift_pct']:.0f}% late in long "
             f"runs (under {DRIFT_OK:.0f}% is sound).")
    else:
        s = (f"Durability is the issue: longest run {run['durability']['longest_km']:.0f} km "
             f"against {long_band[0]}–{long_band[1]} {g}.")
    return s + (f" {also}" if also else "")


def _foot(by, run) -> str:
    bits = ["Each against what the goal time needs; the biggest gap is named."]
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
