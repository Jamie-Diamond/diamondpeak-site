#!/usr/bin/env python3
"""
Weekly trend aggregation — writes athlete-summary.json for dashboard charts.
Run after weekly-summary.py (Sunday evenings) or on demand.

Usage: python3 weekly-trend.py [--athlete jamie] [--weeks 12]
"""
import json, sys
from datetime import date, timedelta
from pathlib import Path
from statistics import mean

BASE = Path(__file__).parent.parent
sys.path.insert(0, str(BASE / "lib"))

from icu_api import IcuClient

ATHLETES_CONFIG = BASE / "config/athletes.json"
WEEKS_DEFAULT   = 12


def _load_client(slug: str):
    cfg = json.loads(ATHLETES_CONFIG.read_text())
    a = cfg[slug]
    return IcuClient(a["icu_athlete_id"], a["icu_api_key"])


def _monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


def _read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text())
    except Exception:
        return default if default is not None else []


def _sport_bucket(sport: str) -> str:
    s = (sport or "").lower()
    if "swim" in s:            return "swim"
    if "run" in s or "walk" in s: return "run"
    if "ride" in s or "cycle" in s: return "bike"
    if "strength" in s or "weight" in s: return "strength"
    return "other"


def build_summary(slug: str = "jamie", weeks: int = WEEKS_DEFAULT) -> dict:
    adir    = BASE / "athletes" / slug
    profile = _read_json(adir / "profile.json", {})
    sessions = _read_json(adir / "session-log.json")
    swim_log = _read_json(adir / "swim-log.json")
    heat_log = _read_json(adir / "heat-log.json")

    client = _load_client(slug)
    # Wellness: need HRV, sleep, weight — fetch enough history
    wellness_rows = client.get_wellness(weeks * 7 + 7)
    # Fitness: CTL/ATL/TSB series
    fitness_rows  = client.get_fitness(
        start_date=(date.today() - timedelta(days=weeks * 7 + 7)).isoformat(),
        end_date=date.today().isoformat(),
    )

    # Index wellness by date
    wellness_by_date = {}
    for r in (wellness_rows or []):
        d = r.get("date") or r.get("id", "")[:10]
        if d:
            wellness_by_date[d] = r

    # Index fitness by date
    fitness_by_date = {}
    for r in (fitness_rows or []):
        d = r.get("date") or r.get("id", "")[:10]
        if d:
            fitness_by_date[d] = r

    # Index sessions by date
    sessions_by_date: dict[str, list] = {}
    for s in sessions:
        d = s.get("date", "")
        if d:
            sessions_by_date.setdefault(d, []).append(s)

    # Index heat log by date
    heat_by_date: set[str] = {h.get("date", "") for h in heat_log}

    # Swim progression (all swim sessions with pace data, sorted asc)
    swim_prog = sorted(
        [
            {"date": s.get("date"), "pace_per_100m": s.get("pace_per_100m"),
             "distance_m": round((s.get("distance_km") or 0) * 1000)}
            for s in sessions
            if _sport_bucket(s.get("sport", "")) == "swim"
            and s.get("pace_per_100m") is not None
        ],
        key=lambda x: x["date"],
    )

    today = date.today()
    week_data = []

    for i in range(weeks - 1, -1, -1):
        wk_start = _monday(today) - timedelta(weeks=i)
        wk_end   = wk_start + timedelta(days=6)
        wk_end   = min(wk_end, today)

        # Daily iteration
        tss_by_bucket: dict[str, float] = {"swim": 0, "bike": 0, "run": 0, "strength": 0, "other": 0}
        hours_total = 0.0
        session_count = 0
        pain_scores: list[float] = []
        fuel_sessions_count = 0
        fuel_ghr_vals: list[float] = []
        heat_count = 0
        planned_tss = 0
        rpe_vals: list[float] = []

        # Wellness aggregates for the week
        hrv_vals: list[float] = []
        sleep_vals: list[float] = []
        weight_vals: list[float] = []

        d = wk_start
        while d <= wk_end:
            ds = d.isoformat()
            for s in sessions_by_date.get(ds, []):
                bucket = _sport_bucket(s.get("sport", ""))
                tss = s.get("tss") or 0
                tss_by_bucket[bucket] = tss_by_bucket.get(bucket, 0) + tss
                hours_total += (s.get("duration_min") or 0) / 60
                session_count += 1

                # Pain (run ankle)
                pain = s.get("ankle_pain_during") or s.get("injury_pain_during")
                if pain is not None:
                    pain_scores.append(float(pain))

                # Fuelling (rides/runs > 60 min)
                if (s.get("duration_min") or 0) > 60 and s.get("nutrition_g_carb") is not None:
                    fuel_sessions_count += 1
                    ghr = s["nutrition_g_carb"] / ((s["duration_min"] or 60) / 60)
                    fuel_ghr_vals.append(ghr)

                # RPE
                if s.get("rpe") is not None:
                    rpe_vals.append(float(s["rpe"]))

            if ds in heat_by_date:
                heat_count += 1

            w = wellness_by_date.get(ds, {})
            hrv = w.get("hrv") or w.get("hrvSDNN")
            if hrv is not None:
                hrv_vals.append(float(hrv))
            sleep = w.get("hrsSleep") or w.get("sleepSecs")
            if sleep is not None:
                sleep_hrs = sleep / 3600 if sleep > 24 else sleep
                sleep_vals.append(float(sleep_hrs))
            weight = w.get("weight")
            if weight is not None:
                weight_vals.append(float(weight))

            d += timedelta(days=1)

        # CTL/ATL/TSB at end of week
        wk_end_ds = wk_end.isoformat()
        fit_end   = fitness_by_date.get(wk_end_ds, {})
        ctl_end   = fit_end.get("ctl") or fit_end.get("atl_fitness")
        atl_end   = fit_end.get("atl") or fit_end.get("atl_load")
        tsb_end   = fit_end.get("form") or fit_end.get("atl_form")
        if ctl_end is None and atl_end is not None and tsb_end is not None:
            ctl_end = atl_end - tsb_end

        tss_total = sum(tss_by_bucket.values())

        week_data.append({
            "week_start":       wk_start.isoformat(),
            "tss_total":        round(tss_total, 1),
            "tss_swim":         round(tss_by_bucket["swim"], 1),
            "tss_bike":         round(tss_by_bucket["bike"], 1),
            "tss_run":          round(tss_by_bucket["run"], 1),
            "tss_strength":     round(tss_by_bucket["strength"], 1),
            "hours_total":      round(hours_total, 2),
            "sessions":         session_count,
            "ctl_end":          round(ctl_end, 1) if ctl_end is not None else None,
            "atl_end":          round(atl_end, 1) if atl_end is not None else None,
            "tsb_end":          round(tsb_end, 1) if tsb_end is not None else None,
            "hrv_avg":          round(mean(hrv_vals), 1) if hrv_vals else None,
            "sleep_avg":        round(mean(sleep_vals), 2) if sleep_vals else None,
            "weight_avg":       round(mean(weight_vals), 1) if weight_vals else None,
            "pain_avg":         round(mean(pain_scores), 2) if pain_scores else None,
            "heat_sessions":    heat_count,
            "rpe_avg":          round(mean(rpe_vals), 1) if rpe_vals else None,
            "fuelling_logged":  fuel_sessions_count,
            "fuelling_ghr_avg": round(mean(fuel_ghr_vals), 1) if fuel_ghr_vals else None,
        })

    bests = {
        "swim_css_per100m": profile.get("swim_css_per_100m"),
        "bike_ftp_w":       profile.get("ftp_watts"),
        "run_lthr":         profile.get("lthr"),
    }

    return {
        "generated":        date.today().isoformat(),
        "athlete":          slug,
        "weeks":            week_data,
        "swim_progression": swim_prog,
        "bests":            bests,
    }


def main():
    import argparse, subprocess
    p = argparse.ArgumentParser()
    p.add_argument("--athlete", default="jamie")
    p.add_argument("--weeks",   type=int, default=WEEKS_DEFAULT)
    args = p.parse_args()

    summary = build_summary(args.athlete, args.weeks)

    out_path = BASE / "athletes" / args.athlete / "athlete-summary.json"
    out_path.write_text(json.dumps(summary, indent=2))
    print(f"Wrote {out_path}")

    # Gitignored (athlete data) — no commit needed; refresh-site-data.py will pick it up
    print(json.dumps({"generated": summary["generated"], "weeks_computed": len(summary["weeks"])}, indent=2))


if __name__ == "__main__":
    main()
