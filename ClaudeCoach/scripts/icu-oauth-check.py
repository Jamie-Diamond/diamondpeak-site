#!/usr/bin/env python3
"""Prove an athlete's Peak OAuth link to Intervals.icu works end to end, with the token
only (never their API key): reads, a calendar write that is read back and removed, and
settings read and written back unchanged.

  python3 scripts/icu-oauth-check.py --athlete jamie

The calendar write is a NOTE (never sent to a watch) 30 days out, deleted again at once.
The settings write puts back the values already there, so nothing changes; it proves the
token is allowed to write settings. Exit 0 only when every step passed.
"""
import argparse
import json
import sys
from datetime import date, timedelta
from pathlib import Path

CC = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(CC / "lib"))

import icu_link  # noqa: E402
from icu_api import BASE_URL, IcuClient  # noqa: E402


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--athlete", required=True)
    p.add_argument("--with-key", action="store_true", help="test this script with the API key instead")
    a = p.parse_args()
    cfg = json.loads((CC / "config" / "athletes.json").read_text()).get(a.athlete, {})
    want = cfg.get("icu_athlete_id")
    tok = {"access_token": "", "scope": "(API key)"} if a.with_key else icu_link.load(slug=a.athlete)
    if not tok:
        print(f"{a.athlete}: no OAuth token (athletes/{a.athlete}/{icu_link.TOKENS_NAME})")
        return 2
    c = IcuClient("0", cfg.get("icu_api_key", "")) if a.with_key else IcuClient("0", token=tok["access_token"])

    def put_profile(fields):
        r = c.session.put(f"{BASE_URL}/athlete/0", json=fields, timeout=15)
        r.raise_for_status()
    rows, ok = [], True

    def step(name, fn):
        nonlocal ok
        try:
            detail = fn()
            rows.append(("PASS", name, detail))
        except Exception as e:
            ok = False
            rows.append(("FAIL", name, f"{type(e).__name__}: {e}"[:160]))

    def read_profile():
        prof = c._get("")
        if want and not icu_link.same_athlete(prof.get("id"), want):
            raise AssertionError(f"token is for {prof.get('id')}, athlete is {want}")
        return f"athlete {prof.get('id')}, scope {tok.get('scope')}"

    step("read: profile", read_profile)
    step("read: wellness 7 days", lambda: f"{len(c.get_wellness(days=7))} days")
    step("read: activities 7 days", lambda: f"{len(c.get_training_history(days=7))} activities")
    step("read: connections", lambda: ", ".join(k.replace("_connected", "")
                                                 for k, v in c._get("connections").items()
                                                 if k != "id" and v) or "none")

    day = (date.today() + timedelta(days=30)).isoformat()
    name = "Peak link check - deleting"

    def calendar_write():
        ev = c._post("events", {"category": "NOTE", "start_date_local": f"{day}T00:00:00",
                                "name": name, "description": "Written by Peak to check the link."})
        eid = ev.get("id")
        back = c.get_event(eid)
        if back.get("name") != name or str(back.get("start_date_local", ""))[:10] != day:
            raise AssertionError(f"read back {back.get('name')!r} on {back.get('start_date_local')}")
        c.delete_workout(eid)
        left = [e for e in c.get_events(day, day) if e.get("id") == eid]
        if left:
            raise AssertionError(f"event {eid} still there after delete")
        return f"note {eid} on {day}: written, read back, deleted, gone"

    step("calendar: write + verify + delete", calendar_write)

    def settings_read():
        ride = c.get_sport_settings("Ride")
        return f"Ride FTP {ride.get('ftp')}, LTHR {ride.get('lthr')}"

    step("settings: read", settings_read)

    def settings_write_unchanged():
        ride = c.get_sport_settings("Ride")
        c._put("sport-settings/Ride", {"ftp": ride.get("ftp")})
        prof = c._get("")
        put_profile({"icu_garmin_upload_workouts": prof.get("icu_garmin_upload_workouts")})
        after, prof2 = c.get_sport_settings("Ride"), c._get("")
        if after.get("ftp") != ride.get("ftp") or \
                prof2.get("icu_garmin_upload_workouts") != prof.get("icu_garmin_upload_workouts"):
            raise AssertionError("a value changed")
        return (f"FTP {ride.get('ftp')} and 'upload planned workouts' "
                f"({prof.get('icu_garmin_upload_workouts')}) written back unchanged")

    step("settings: write (same values)", settings_write_unchanged)

    for r in rows:
        print(f"{r[0]}  {r[1]:34} {r[2]}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
