#!/usr/bin/env python3
"""Backfill heat-log.json from an athlete's activity history.

Why this exists
---------------
`activity-watcher._credit_heat_exposure` logs an acclimation dose per activity, but
only when `heat.state(slug)["active"]` is true AT THE MOMENT the activity syncs. An
athlete whose heat protocol is switched on later therefore has a permanent hole: every
qualifying session before the switch is simply absent, and the 14-day dose figure the
coach reasons from reads far too low.

Calum was in exactly that state on 2026-08-03 - `profile.heat_protocol: false` plus
blueprint `active: false` meant `_credit_heat_exposure` returned at its first line for
his entire history, leaving a 3-byte heat-log. Turning tracking on fixes the future
and nothing else.

It also answers the diagnostic question for Kathryn, whose log stops on 2026-07-17
while she has trained regularly since: run this with --dry-run and if it finds
qualifying sessions after that date, logging was broken; if it finds none, the weather
simply dropped below the gate and there was never anything to log.

Deliberately reuses heat.exposure_entry - the SAME function activity-watcher uses - so
a backfilled entry is byte-identical to a live-credited one. It does not reimplement
the dose maths, the indoor/trainer exclusions or the Open-Meteo sensor correction.

Usage
-----
  python3 scripts/backfill-heat.py --athlete calum --days 120 --dry-run
  python3 scripts/backfill-heat.py --athlete calum --days 120

Idempotent: existing entries are keyed by activity_id and never duplicated or
overwritten. Always writes a .bak before changing anything.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE / "lib"))

import heat as heat_lib  # noqa: E402


def _fetch_history(slug: str, days: int) -> list:
    """Activity summaries via the shared icu_fetch bridge (same path the watcher uses)."""
    r = subprocess.run(
        [sys.executable, str(BASE / "lib" / "icu_fetch.py"),
         "--athlete", slug, "--endpoint", "history", "--days", str(days)],
        capture_output=True, text=True, timeout=180)
    if r.returncode != 0:
        raise SystemExit(f"icu_fetch failed for {slug}: {(r.stderr or '').strip()[:400]}")
    data = json.loads(r.stdout or "[]")
    if isinstance(data, dict):
        data = data.get("activities") or data.get("data") or []
    return data or []


def _latlng_from_streams(activity_id):
    """Recover GPS from the streams endpoint when the summary lacks it - the same
    fallback activity-watcher wires in, so the weather correction behaves identically."""
    try:
        r = subprocess.run(
            [sys.executable, str(BASE / "lib" / "icu_fetch.py"),
             "--athlete", _SLUG, "--endpoint", "streams", "--activity", str(activity_id)],
            capture_output=True, text=True, timeout=90)
        if r.returncode != 0:
            return None
        st = json.loads(r.stdout or "{}")
        streams = st if isinstance(st, list) else (st.get("streams") or [])
        for s in streams:
            if s.get("type") == "latlng":
                lat = (s.get("data") or [None])[0]
                lon = (s.get("data2") or [None])[0]
                if lat is not None and lon is not None:
                    return [lat, lon]
    except Exception:
        return None
    return None


_SLUG = ""


def main() -> int:
    global _SLUG
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--athlete", required=True)
    ap.add_argument("--days", type=int, default=120,
                    help="how far back to look (default 120)")
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would be added, write nothing")
    args = ap.parse_args()
    _SLUG = args.athlete

    adir = BASE / "athletes" / args.athlete
    if not adir.is_dir():
        raise SystemExit(f"unknown athlete {args.athlete!r}")
    profile = {}
    pf = adir / "profile.json"
    if pf.exists():
        profile = json.loads(pf.read_text())

    state = heat_lib.state(args.athlete, profile)
    print(f"[{args.athlete}] heat state: active={state['active']} "
          f"in_window={state['in_protocol_window']} silent={state['silent']}")
    if not state["active"]:
        print(f"[{args.athlete}] heat tracking is OFF (blueprint env_protocols.heat."
              f"active, or profile heat_protocol: false). Turn tracking on before "
              f"backfilling, or the log you build will not be read.", file=sys.stderr)
        return 2

    log_f = adir / "heat-log.json"
    try:
        existing = json.loads(log_f.read_text()) if log_f.exists() else []
    except Exception:
        existing = []
    if not isinstance(existing, list):
        existing = []
    have = {str(e.get("activity_id")) for e in existing if e.get("activity_id")}
    print(f"[{args.athlete}] existing entries: {len(existing)}")

    acts = _fetch_history(args.athlete, args.days)
    print(f"[{args.athlete}] activities fetched: {len(acts)} over {args.days} days")

    added, skipped_have, no_dose = [], 0, 0
    for act in acts:
        aid = str(act.get("id") or "")
        if aid and aid in have:
            skipped_have += 1
            continue
        try:
            entry = heat_lib.exposure_entry(act, latlng_fallback=_latlng_from_streams)
        except Exception as e:
            print(f"  ! {aid}: exposure_entry failed: {e}", file=sys.stderr)
            continue
        if not entry:
            no_dose += 1
            continue
        entry.setdefault("activity_id", aid)
        entry["backfilled"] = True
        added.append(entry)

    print(f"[{args.athlete}] already logged: {skipped_have}; "
          f"did not qualify (indoor / <25C / too short / no temp): {no_dose}; "
          f"NEW: {len(added)}")
    for e in sorted(added, key=lambda x: str(x.get("date"))):
        print(f"    + {e.get('date')} {e.get('temperature_c')}C "
              f"{e.get('duration_min')}min dose={e.get('dose')} ({e.get('context')})")

    if not added:
        print(f"[{args.athlete}] nothing to backfill.")
        return 0
    if args.dry_run:
        print(f"[{args.athlete}] DRY RUN - wrote nothing.")
        return 0

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    if log_f.exists():
        (adir / f"heat-log.json.bak-backfill-{stamp}").write_text(log_f.read_text())
    merged = sorted(existing + added, key=lambda x: str(x.get("date") or ""))
    log_f.write_text(json.dumps(merged, indent=2) + "\n")
    print(f"[{args.athlete}] wrote {len(merged)} entries "
          f"({len(added)} new) to {log_f.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
