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


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--athlete")
    ap.add_argument("--all", action="store_true")
    args = ap.parse_args()
    athletes = json.loads(ATHLETES.read_text())
    slugs = ([s for s, c in athletes.items() if c.get("active", True)] if args.all
             else [args.athlete])
    print(json.dumps([get_thresholds(s, athletes[s]) for s in slugs], indent=1))
