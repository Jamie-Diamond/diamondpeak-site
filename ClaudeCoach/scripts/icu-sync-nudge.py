#!/usr/bin/env python3
"""Daily nudge to an athlete whose activities have stopped reaching Intervals.icu.

Calum's rides stopped arriving on 1 Aug 2026: the last one through is the Isle of Wight
ride that morning, from Garmin Connect. Jamie, 2 Oct 2026: send him the fix every day
until it's fixed. VM crontab, once a day:

    python3 ClaudeCoach/scripts/icu-sync-nudge.py

For each entry in NUDGES it asks Intervals.icu for any activity newer than the last one
that came through. One found means the sync is fixed: nothing is sent, and a line in
ops-alerts.log says the entry and its crontab line can go. None found means the message
goes into the athlete's Peak chat, with a phone notification, at most once a day.
If Intervals.icu can't be reached, nothing is sent: a missed day is better than a nag
about a sync that may already be working.

Manual activities don't count as fixed: the coach chat can log one by hand.

To stop early, delete the athlete's entry below.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

CC = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(CC / "lib"))
import ops_log  # noqa: E402
import outbox   # noqa: E402
from icu_api import IcuClient  # noqa: E402

ATHLETES = CC / "config" / "athletes.json"
SOURCE = "coach-sync-check"        # the first send (2 Oct, by hand) used this too
MIN_GAP = timedelta(hours=20)      # once a day, even if cron drifts a few minutes

NUDGES = {
    "calum": {
        "last_seen": "2026-08-01T07:54:01",   # Isle of Wight Road Cycling
        "text": (
            "*Your rides stopped reaching Intervals.icu on 1 August.* Nothing has come "
            "through since, not even Tour de Stations, so I can't see your training.\n\n"
            "To fix it, in Intervals.icu go to *Settings*, then *Connections*. Disconnect "
            "*Garmin* and connect it again, with _Download activities_ ticked. Then tap "
            "_Download old data_ under activities to bring in August to now.\n\n"
            "If you've switched to a different bike computer or watch since August, tell "
            "me which one instead."
        ),
    },
}


def log(msg: str) -> None:
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


def newer_activities(a: dict, last_seen: str) -> list:
    client = IcuClient(a["icu_athlete_id"], a["icu_api_key"])
    newest = (datetime.now() + timedelta(days=1)).date().isoformat()
    acts = client._get("activities", {"oldest": last_seen[:10], "newest": newest})
    return [x for x in acts or []
            if (x.get("start_date_local") or "") > last_seen and x.get("source") != "MANUAL"]


def sent_recently(slug: str, now: datetime) -> bool:
    f = CC / "athletes" / slug / outbox.OUTBOX_NAME
    try:
        lines = f.read_text().splitlines()
    except OSError:
        return False
    for line in reversed(lines):
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if e.get("source") == SOURCE:
            return now - datetime.fromisoformat(e["ts"]) < MIN_GAP
    return False


def main() -> int:
    athletes = json.loads(ATHLETES.read_text())
    now = datetime.now()
    for slug, n in NUDGES.items():
        a = athletes.get(slug)
        if not isinstance(a, dict) or not a.get("active"):
            log(f"{slug}: not an active athlete, skipped")
            continue
        try:
            newer = newer_activities(a, n["last_seen"])
        except Exception as exc:
            ops_log.alert("icu-sync-nudge", f"Intervals.icu check failed ({exc!r}), "
                                            "nudge not sent today", athlete=slug)
            log(f"{slug}: Intervals.icu check failed: {exc!r}")
            continue
        if newer:
            first = min(x.get("start_date_local") or "" for x in newer)
            ops_log.alert("icu-sync-nudge", f"activities reaching Intervals.icu again "
                                            f"({len(newer)} since {n['last_seen'][:10]}, "
                                            f"first {first[:10]}): fixed, nudge stopped. "
                                            "Delete its NUDGES entry and crontab line",
                          athlete=slug)
            log(f"{slug}: fixed, {len(newer)} new activities, nothing sent")
            continue
        if sent_recently(slug, now):
            log(f"{slug}: still broken, already nudged in the last {MIN_GAP}")
            continue
        mid = outbox.record(a["chat_id"], n["text"], source=SOURCE)
        if mid:
            log(f"{slug}: still broken, nudge sent ({mid})")
        else:
            ops_log.alert("icu-sync-nudge", "could not record the nudge in Peak chat",
                          athlete=slug)
            log(f"{slug}: outbox.record failed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
