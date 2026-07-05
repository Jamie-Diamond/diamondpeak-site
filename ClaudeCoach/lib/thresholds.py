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
    """Keep the ICU *configured* FTP tracking live eFTP — RAISE-ONLY.

    The planner already reads eFTP-first, but intervals.icu's static `ftp` (which
    drives the zones the athlete sees in ICU and on their Garmin) does not auto-update
    — there is no athlete- or sport-level auto-apply flag in the API. So we mirror
    ICU's own native semantics: bump the configured FTP up to eFTP when eFTP sets a
    new high, and NEVER cut it because eFTP decayed over an easy week (eFTP falling
    means 'no recent hard effort', not lost fitness). Guarded by a meaningful-delta
    floor (≥min_delta_w AND ≥min_pct) so noise doesn't churn the setting. apply=False
    is a dry-run. eFTP is computed from the power curve independently of FTP, so the
    write can't create a feedback loop."""
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
    if delta <= 0:
        out["reason"] = f"eFTP {eftp} not above static {static} (raise-only — left as-is)"
        # Downward-drift ALERT (audit P1-8): raise-only means a detraining athlete
        # keeps stale-high zones forever with no signal. Flag — never auto-cut
        # (no-test policy: the cut is a coaching conversation, not an automation).
        drift_pct = (static - eftp) / static * 100 if static else 0.0
        if drift_pct >= 7.0:
            out["downward_drift_pct"] = round(drift_pct, 1)
            try:
                from ops_log import alert
                alert("thresholds",
                      f"eFTP {eftp}W sits {drift_pct:.0f}% below configured FTP {static}W — "
                      f"zones may be stale-high (detraining or a long gap since hard riding). "
                      f"Review with the athlete before any cut.", athlete=slug)
            except Exception:
                pass
        return out
    if delta < max(min_delta_w, static * min_pct):
        out["reason"] = f"eFTP {eftp} only +{delta}W over {static} — below {max(min_delta_w, round(static*min_pct))}W floor"
        return out
    out.update(changed=True, new_ftp=eftp,
               reason=f"raise {static} -> {eftp} (+{delta}W new eFTP high)")
    if apply:
        client._put("sport-settings/Ride", {"ftp": eftp})
        out["applied"] = True
    return out


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--athlete")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--sync-ftp", action="store_true",
                    help="raise ICU configured FTP up to eFTP (new high); dry-run unless --apply")
    ap.add_argument("--apply", action="store_true", help="actually write the FTP change")
    ap.add_argument("--notify", action="store_true",
                    help="Telegram the athlete when an FTP change is applied")
    args = ap.parse_args()
    athletes = json.loads(ATHLETES.read_text())
    slugs = ([s for s, c in athletes.items() if c.get("active", True)] if args.all
             else [args.athlete])
    if args.sync_ftp:
        res = [sync_ftp_from_eftp(s, athletes[s], apply=args.apply) for s in slugs]
        for r in res:
            tag = "APPLIED" if r["applied"] else ("WOULD CHANGE" if r["changed"] else "no change")
            print(f"{r['athlete']:9} [{tag}] {r['reason']}")
            if args.notify and r["applied"]:
                import subprocess
                cid = athletes[r["athlete"]].get("chat_id")
                if cid:
                    msg = (f"📈 *FTP auto-update* — your cycling FTP is now *{r['new_ftp']}W* "
                           f"(was {r['static_ftp']}W). intervals.icu's estimated FTP set a new high, "
                           "so I've updated it; your power zones and planned sessions now use this.")
                    subprocess.run(["python3", str(BASE / "telegram/notify.py"),
                                    "--chat-id", str(cid), msg], check=False)
    else:
        print(json.dumps([get_thresholds(s, athletes[s]) for s in slugs], indent=1))
