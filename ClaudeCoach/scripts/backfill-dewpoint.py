#!/usr/bin/env python3
"""One-off: backfill dew-point humidity multiplier onto existing auto heat-log
entries. Keeps the stored temp_mult and hr_strain_mult, fetches peak dew-point
from Open-Meteo for each activity's GPS+window, and recomputes
dose = base_dose * temp_mult * hr_strain_mult * humidity_mult.
"""
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE / "lib"))
import heat as heat_lib
from strava_client import StravaClient

SLUG = "jamie"
LOG = BASE / "athletes" / SLUG / "heat-log.json"


def get_latlng_and_start(icu_id: str, sc: StravaClient):
    import subprocess
    out = subprocess.check_output([
        "python3", str(BASE / "lib" / "icu_fetch.py"),
        "--athlete", SLUG, "--endpoint", "activity_detail",
        "--activity-id", icu_id,
    ])
    det = json.loads(out)
    strava_id = det.get("strava_id")
    start = det.get("start_date")
    if not strava_id:
        return None, start
    sd = sc.get_activity_detail(str(strava_id))
    return sd.get("start_latlng"), sd.get("start_date") or start


def main():
    entries = json.loads(LOG.read_text())
    sc = StravaClient(SLUG)
    changed = 0
    for e in entries:
        if e.get("method") != "outdoor session (auto)":
            continue
        if e.get("dew_point_c") is not None:
            continue
        icu_id = e.get("activity_id")
        if not icu_id:
            continue
        latlng, start = get_latlng_and_start(icu_id, sc)
        if not latlng or len(latlng) != 2 or not start:
            print(f"  skip {e['date']} {icu_id}: no GPS", file=sys.stderr)
            continue
        start_raw = str(start).replace("Z", "")
        start_dt = datetime.fromisoformat(start_raw)
        end_dt = start_dt + timedelta(minutes=e.get("duration_min") or 0)
        try:
            _, dews = heat_lib.fetch_ambient_weather(
                float(latlng[0]), float(latlng[1]),
                start_dt.strftime("%Y-%m-%dT%H:%M:%S"),
                end_dt.strftime("%Y-%m-%dT%H:%M:%S"),
            )
        except Exception as ex:
            print(f"  skip {e['date']} {icu_id}: weather fail {ex}", file=sys.stderr)
            continue
        if not dews:
            print(f"  skip {e['date']} {icu_id}: no dew data", file=sys.stderr)
            continue
        dew = round(max(dews), 1)
        _, _, humidity_mult = heat_lib.dose_multipliers(
            e["temperature_c"], dew_point_c=dew)
        base = e["base_dose"]
        temp_mult = e["temp_mult"]
        strain_mult = e["hr_strain_mult"]
        old_dose = e["dose"]
        new_dose = round(base * temp_mult * strain_mult * humidity_mult, 2)
        e["dew_point_c"] = dew
        e["humidity_mult"] = humidity_mult
        e["dose"] = new_dose
        strain_label = f"HR{e['avg_hr']}" if e.get("avg_hr") else "TSS"
        # rebuild context dose tail
        ctx = e.get("context", "")
        head = ctx.split("; dose")[0]
        if "; dew" not in head:
            head += f"; dew {dew}°C"
        e["context"] = (f"{head}; dose {base}×T{temp_mult}×S{strain_mult}"
                        f"×H{humidity_mult}={new_dose} ({strain_label})")
        e["backfilled_dewpoint"] = "2026-06-29"
        changed += 1
        print(f"  {e['date']} {icu_id}: dew {dew}°C  H×{humidity_mult}  "
              f"dose {old_dose} → {new_dose}")
    LOG.write_text(json.dumps(entries, indent=2) + "\n")
    print(f"Updated {changed} entries.")


if __name__ == "__main__":
    main()
