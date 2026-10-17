#!/usr/bin/env python3
"""Live threshold resolver — the single source of truth for an athlete's current
FTP / run threshold / swim CSS, for ALL athletes.

Why this exists (15 Jun):
  * Thresholds must TRACK intervals.icu's eFTP and stay current, not be hardcoded
    (Jamie's bike was 316 in the prompt, 300 in static settings, but live eFTP is
    297). Every prescription/zone/load depends on the right number.
  * intervals.icu stores threshold_pace in METRES/SECOND (the `pace_units` field is
    only the DISPLAY unit). Reading 4.132 as "min/km" gives 4:08 — wrong; as m/s it
    is 4:02/km, which matches. Centralising the conversion here stops that class of bug.

Resolution order:
  FTP        : live eFTP (wellness sportInfo) → static sport-settings ftp → cfg.ftp
  run thresh : Run sport-settings threshold_pace (m/s) → None (athlete hasn't set one)
  swim CSS   : Swim sport-settings threshold_pace (m/s) → None
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE / "lib"))
ATHLETES = BASE / "config" / "athletes.json"

_EFTP_SPORTS = ("Ride", "VirtualRide", "Cycling")


def _pace_str(mps, dist_m):
    if not mps:
        return None
    s = dist_m / mps
    return f"{int(s // 60)}:{s % 60:04.1f}"


def get_thresholds(slug: str, cfg: dict | None = None, client=None) -> dict:
    """Current thresholds for an athlete, pulled live from intervals.icu.
    Never raises for missing data — absent thresholds come back as None with a note."""
    if cfg is None:
        cfg = json.loads(ATHLETES.read_text())[slug]
    if client is None:
        from icu_api import IcuClient
        client = IcuClient(cfg["icu_athlete_id"], cfg["icu_api_key"])

    ride = client.get_sport_settings("Ride") or {}
    run = client.get_sport_settings("Run") or {}
    swim = client.get_sport_settings("Swim") or {}
    if isinstance(ride, list):
        ride = ride[0] if ride else {}

    # FTP — eFTP first (live, auto-updating), else static, else config.
    eftp = None
    wellness = client.get_wellness(days=3)
    if wellness:
        for si in (wellness[-1].get("sportInfo") or []):
            if si.get("type") in _EFTP_SPORTS and si.get("eftp"):
                eftp = round(float(si["eftp"]))
                break
    static_ftp = ride.get("ftp")
    if eftp:
        ftp, ftp_source = eftp, "eftp"
    elif static_ftp:
        ftp, ftp_source = int(static_ftp), "static"
    else:
        ftp, ftp_source = cfg.get("ftp_watts"), "config"

    # Pace thresholds — threshold_pace is METRES/SECOND.
    run_mps = run.get("threshold_pace")
    swim_mps = swim.get("threshold_pace")

    notes = []
    if ftp_source != "eftp":
        notes.append(f"FTP from {ftp_source} (no live eFTP) — value {ftp}")
    if not run_mps:
        notes.append("no run threshold set in ICU — run pace zones unavailable (use HR/RPE)")
    if not swim_mps:
        notes.append("no swim CSS set in ICU — swim pace zones unavailable")

    return {
        "athlete": slug,
        "ftp_watts": ftp, "ftp_source": ftp_source, "eftp": eftp, "static_ftp": static_ftp,
        "run_threshold_mps": run_mps,
        "run_threshold_per_km": (_pace_str(run_mps, 1000) + "/km") if run_mps else None,
        "swim_css_mps": swim_mps,
        "swim_css_per_100m": (_pace_str(swim_mps, 100) + "/100m") if swim_mps else None,
        "notes": notes,
    }


def sync_ftp_from_eftp(slug: str, cfg: dict | None = None, client=None,
                       min_delta_w: int = 2, min_pct: float = 0.01,
                       apply: bool = False) -> dict:
    """Keep the ICU *configured* FTP equal to live eFTP, UP AND DOWN (Jamie, 15 Oct 2026:
    "eFTP synced automatically"; it was raise-only and never scheduled, so the plan read
    eFTP while the zones the athlete rides by came from a stale FTP 4-8% higher).

    intervals.icu's power zones are % of the configured FTP, so they follow the write.
    Accepted trade-off: eFTP sags after an easy fortnight, and the zones sag with it.
    Guarded by a meaningful-delta floor (>=min_delta_w AND >=min_pct) so daily noise
    doesn't churn the setting. apply=False is a dry-run. eFTP is computed from the power
    curve independently of FTP, so the write can't feed back into it."""
    if cfg is None:
        cfg = json.loads(ATHLETES.read_text())[slug]
    if client is None:
        from icu_api import IcuClient
        client = IcuClient(cfg["icu_athlete_id"], cfg["icu_api_key"])
    t = get_thresholds(slug, cfg, client)
    eftp, static = t["eftp"], t["static_ftp"]
    out = {"athlete": slug, "eftp": eftp, "static_ftp": static,
           "changed": False, "applied": False, "reason": ""}
    if not eftp or not static:
        out["reason"] = "no eFTP" if not eftp else "no static FTP to compare"
        return out
    delta = eftp - static
    floor = max(min_delta_w, static * min_pct)
    if abs(delta) < floor:
        out["reason"] = f"eFTP {eftp} within {round(floor)}W of FTP {static}"
        return out
    out.update(changed=True, new_ftp=eftp,
               reason=f"{'raise' if delta > 0 else 'lower'} {static} -> {eftp} ({delta:+d}W to eFTP)")
    if apply:
        client._put("sport-settings/Ride", {"ftp": eftp})
        out["applied"] = True
    return out


def get_ctl_on_date(slug: str, date_str: str, cfg: dict | None = None, client=None) -> float | None:
    """Actual CTL for a single past date, straight off intervals.icu wellness — for
    validating a hand-set CTL figure (e.g. race_predictor's anchor_ctl) against what the
    athlete's fitness really was on that date, rather than trusting a manually entered
    number. Never raises: any lookup failure (no config, no network, day not yet
    synced) returns None."""
    try:
        if cfg is None:
            cfg = json.loads(ATHLETES.read_text())[slug]
        if client is None:
            from icu_api import IcuClient
            client = IcuClient(cfg["icu_athlete_id"], cfg["icu_api_key"])
        rows = client.get_wellness(days=1, newest=date_str)
        return float(rows[-1]["ctl"]) if rows and rows[-1].get("ctl") is not None else None
    except Exception:
        return None


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--athlete")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--sync-ftp", action="store_true",
                    help="set ICU configured FTP to eFTP (up or down); dry-run unless --apply")
    ap.add_argument("--apply", action="store_true", help="actually write the FTP change")
    ap.add_argument("--notify", action="store_true",
                    help="Telegram the athlete when an FTP change is applied")
    args = ap.parse_args()
    athletes = json.loads(ATHLETES.read_text())
    slugs = ([s for s, c in athletes.items() if c.get("active", True)] if args.all
             else [args.athlete])
    if args.sync_ftp:
        # The applying run (cron 04:50) writes the heartbeat coach_alert.DELIVERABLES
        # watches (17 Oct 2026). "thresholds" is FINDING-class there, so a failure is
        # stamped FAILURE explicitly or it would read as a run.
        import ops_log
        try:
            res = [sync_ftp_from_eftp(s, athletes[s], apply=args.apply) for s in slugs]
        except Exception as exc:
            if args.apply:
                ops_log.record_run("thresholds", ok=False, outcome=ops_log.FAILURE,
                                   detail=f"ftp sync failed: {type(exc).__name__}: {exc}"[:200])
            raise
        if args.apply:
            ops_log.record_run("thresholds", ok=True, detail="ftp sync ok")
        for r in res:
            tag = "APPLIED" if r["applied"] else ("WOULD CHANGE" if r["changed"] else "no change")
            print(f"{r['athlete']:9} [{tag}] {r['reason']}")
        moved = [r for r in res if r["applied"]]
        if args.notify and moved:
            # One Dev-tab line for the coach, not the athletes (their zones just follow).
            from outbox import record
            lines = "\n".join(f"- {r['athlete'].title()}: {r['static_ftp']}W \u2192 *{r['new_ftp']}W*"
                               for r in moved)
            record(athletes["jamie"].get("chat_id"),
                   f"FTP synced to eFTP in Intervals.icu (power zones follow):\n{lines}",
                   source="dev-session", parse_mode="Markdown", tab="dev")
    else:
        print(json.dumps([get_thresholds(s, athletes[s]) for s in slugs], indent=1))


def estimate_run_threshold_from_gap(client, lthr: float | None = None,
                                    days: int = 90,
                                    untrusted: set | None = None) -> dict | None:
    """Passive run-threshold estimate (audit P1-8, no-test regime): linear fit of
    grade-adjusted speed (ICU `gap`, m/s) against average HR over steady runs,
    extrapolated to LTHR. FLAG-ONLY — the configured threshold is never changed
    automatically. Returns None when the data cannot support an estimate
    (needs >=6 steady runs >=30 min with GAP+HR, >=15 bpm HR spread, R^2 >= 0.5,
    positive slope). `untrusted`: optional ICU activity ids whose HR must not be used
    (hr_quality.untrusted_ids); None means no filtering."""
    pts = []
    for a in client.get_training_history(days=days) or []:
        if (a.get("type") or "") not in ("Run", "TrailRun", "VirtualRun"):
            continue
        if (a.get("moving_time") or 0) < 1800:
            continue
        gap, hr, dec = a.get("gap"), a.get("average_heartrate"), a.get("decoupling")
        # 27 Sep 2026: a run whose HR lib/hr_quality.py judged bad/none is dropped like a
        # run with no HR — a cadence-locked 175 would drag the whole fit. An activity not
        # in the quality log is unaffected.
        if untrusted and str(a.get("id")) in untrusted:
            hr = None
        if not gap or not hr:
            continue
        if dec is not None and abs(float(dec)) > 8:      # drifting runs poison the fit
            continue
        if not lthr:
            lthr = a.get("lthr")
        pts.append((float(hr), float(gap)))
    if len(pts) < 6 or not lthr:
        return None
    hs, vs = [h for h, _ in pts], [v for _, v in pts]
    if max(hs) - min(hs) < 15:
        return None                                       # extrapolation unstable
    n = len(pts)
    mh, mv = sum(hs) / n, sum(vs) / n
    sxx = sum((h - mh) ** 2 for h in hs)
    sxy = sum((h - mh) * (v - mv) for h, v in pts)
    if sxx == 0:
        return None
    b = sxy / sxx
    a0 = mv - b * mh
    ss_res = sum((v - (a0 + b * h)) ** 2 for h, v in pts)
    ss_tot = sum((v - mv) ** 2 for v in vs)
    r2 = 1 - ss_res / ss_tot if ss_tot else 0.0
    if r2 < 0.5 or b <= 0:
        return None
    v_thr = a0 + b * float(lthr)
    if v_thr <= 0:
        return None
    sec_km = 1000.0 / v_thr
    return {"pace_s_per_km": round(sec_km),
            "pace": f"{int(sec_km // 60)}:{int(sec_km % 60):02d}",
            "n_runs": n, "r2": round(r2, 2), "lthr_used": round(float(lthr)),
            "hr_range": [round(min(hs)), round(max(hs))]}
