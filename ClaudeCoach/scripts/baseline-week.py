#!/usr/bin/env python3
"""Baseline block CLI (lib/baseline.py holds the logic and the why).

  start   --athlete X [--first-day YYYY-MM-DD] [--dry-run] [--no-notify]
          Schedule the block, push it to Intervals.icu, tell the athlete. Run by the
          bot on /approve; safe to run by hand for an athlete whose state is pending.
  book    --athlete X --sport bike|run|swim --date YYYY-MM-DD
          Put one test on the calendar later (a missed or rejected test). Its result
          is read automatically by the activity watcher, like the block's own tests.
  record  --athlete X --sport bike|run|swim --value 245 | 4:30 | 1:45
          An athlete-stated tested figure (athlete-stated figures are law): writes it to
          Intervals.icu and marks the sport tested.
  status  --athlete X
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE / "lib"))

import baseline as bl           # noqa: E402
from icu_api import IcuClient   # noqa: E402

NOTIFY = BASE / "telegram/notify.py"


def _cfg(slug):
    return json.loads((BASE / "config/athletes.json").read_text())[slug]


def _profile(slug):
    p = BASE / "athletes" / slug / "profile.json"
    try:
        return json.loads(p.read_text())
    except Exception:
        return {}


def _client(cfg):
    return IcuClient(cfg["icu_athlete_id"], cfg["icu_api_key"])


def _notify(chat_id, text):
    if not chat_id:
        return False
    try:
        return subprocess.run(["python3", str(NOTIFY), "--chat-id", str(chat_id), text],
                              timeout=20).returncode == 0
    except Exception:
        return False


def _pace_to_mps(pace: str, per_m: int) -> float | None:
    try:
        m, s = pace.strip().split(":")
        secs = int(m) * 60 + float(s)
        return round(per_m / secs, 4) if secs > 0 else None
    except Exception:
        return None


def _values_for(family: str, value: str) -> dict:
    """An athlete-stated figure as the fields bl.apply writes."""
    if family == "bike":
        return {"ftp": int(round(float(value)))}
    if family == "run":
        mps = _pace_to_mps(value, 1000)
        return {"threshold_pace": value.strip(), "threshold_mps": mps} if mps else {}
    mps = _pace_to_mps(value, 100)
    return {"css": value.strip(), "css_mps": mps} if mps else {}


def start_message(first_name: str, days: list, st: dict) -> str:
    tests = [d for d in days if d["kind"] == "test"]
    w_end = date.fromisoformat(st["window"]["end"])
    lines = [f"{first_name}, your first week is a *baseline week*. Before any plan, we "
             f"measure where you are, so your zones are real numbers, not guesses."]
    if tests:
        lines.append("")
        for d in tests:
            dt = date.fromisoformat(d["date"])
            name = bl.TESTS[d["protocol"]]["name"].replace("Baseline: ", "")
            lines.append(f"• {dt:%a %d %b}: {bl.LABEL[d['family']]} {name}")
    lines.append("")
    lines.append("Every other day is easy, by feel. Everything is on your Intervals.icu "
                 "calendar with full instructions. Go into each test rested and pace it "
                 "evenly.")
    untestable = [f for f in st["sports"]
                  if not (st["sports_state"].get(f) or {}).get("test")
                  and st["sports_state"][f]["confidence"] != "tested"]
    if "bike" in untestable:
        lines.append("There's no bike test: without a power meter or a chest strap there's "
                     "nothing reliable to measure, so bike sessions stay by feel.")
    lines.append(f"\nYour training plan starts Mon {w_end + timedelta(days=1):%d %b}.")
    return "\n".join(lines)


def cmd_start(args):
    slug = args.athlete
    st = bl.load(slug)
    if not st:
        sys.exit(f"{slug}: no baseline.json (an established athlete, or onboarded before "
                 f"27 Sep 2026). Nothing to do.")
    if st.get("status") != "pending" and not args.force:
        sys.exit(f"{slug}: baseline is already {st.get('status')}. Use --force to reschedule.")
    cfg, prof = _cfg(slug), _profile(slug)
    first = date.fromisoformat(args.first_day) if args.first_day else date.today() + timedelta(days=1)
    days = bl.schedule(st, first, prof.get("training_days"), prof.get("max_hours_per_week"))
    workouts = [(d, bl.workout_for(d)) for d in days]
    if args.dry_run:
        for d, w in workouts:
            print(d["date"], d["kind"], (w or {}).get("name", "rest"))
        print(json.dumps(st["window"]))
        return
    client = _client(cfg)
    # Athlete-stated tested numbers that Intervals.icu does not hold yet go there first,
    # so a tested sport's sessions carry real targets from the first plan.
    for f, ss in st["sports_state"].items():
        if ss.get("confidence") == "tested" and ss.get("value_source") == "athlete":
            v = ss.get("values") or {}
            raw = v.get("ftp") or v.get("threshold_pace") or v.get("css")
            if raw:
                bl.apply(slug, f, _values_for(f, str(raw)), client, source="stated_test", st=st)
    pushed = []
    for d, w in workouts:
        if not w:
            continue
        ev = client.push_workout(w["sport"], w["date"], w["name"], description=w["description"],
                                 description_raw=w["description_raw"],
                                 planned_training_load=w["load"])
        pushed.append(ev.get("id"))
        if d["kind"] == "test":
            st["sports_state"][d["family"]]["test"]["icu_event_id"] = ev.get("id")
    st["status"] = "active"
    st["events"] = pushed
    bl.save(slug, st)
    first_name = (prof.get("name") or cfg.get("name") or slug).split()[0]
    msg = start_message(first_name, days, st)
    if not args.no_notify:
        _notify(cfg.get("chat_id"), msg)
    print(json.dumps({"athlete": slug, "window": st["window"], "pushed": len(pushed)}))


def cmd_book(args):
    slug, fam = args.athlete, args.sport
    st = bl.load(slug)
    if not st or fam not in st.get("sports", []):
        sys.exit(f"{slug}: no baseline for {fam}.")
    ss = st["sports_state"][fam]
    proto = (ss.get("test") or {}).get("protocol") or bl.protocol_for(
        fam, st.get("has_power"), st.get("hr_source"))
    if not proto:
        sys.exit(f"{slug}: no {fam} test possible (bike without power or a strap).")
    day = {"date": args.date, "family": fam, "kind": "test", "protocol": proto,
           "ramp_start_w": bl.ramp_start_watts((ss.get("values") or {}).get("ftp"))}
    w = bl.workout_for(day)
    ev = _client(_cfg(slug)).push_workout(w["sport"], w["date"], w["name"],
                                          description=w["description"],
                                          description_raw=w["description_raw"],
                                          planned_training_load=w["load"])
    ss["test"] = {"protocol": proto, "status": "scheduled", "date": args.date,
                  "icu_event_id": ev.get("id")}
    bl.save(slug, st)
    print(json.dumps({"athlete": slug, "booked": fam, "date": args.date, "event": ev.get("id")}))


def cmd_record(args):
    slug, fam = args.athlete, args.sport
    vals = _values_for(fam, args.value)
    if not vals:
        sys.exit(f"could not read {args.value!r} as a {fam} figure")
    payload = bl.apply(slug, fam, vals, _client(_cfg(slug)), source="stated_test")
    print(json.dumps({"athlete": slug, "sport": fam, "set": payload}))


def cmd_status(args):
    st = bl.load(args.athlete)
    print(json.dumps(st, indent=2) if st else f"{args.athlete}: no baseline (established athlete)")


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("start")
    s.add_argument("--athlete", required=True)
    s.add_argument("--first-day")
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("--no-notify", action="store_true")
    s.add_argument("--force", action="store_true")
    b = sub.add_parser("book")
    b.add_argument("--athlete", required=True)
    b.add_argument("--sport", required=True, choices=bl.FAMILIES)
    b.add_argument("--date", required=True)
    r = sub.add_parser("record")
    r.add_argument("--athlete", required=True)
    r.add_argument("--sport", required=True, choices=bl.FAMILIES)
    r.add_argument("--value", required=True)
    t = sub.add_parser("status")
    t.add_argument("--athlete", required=True)
    args = ap.parse_args()
    {"start": cmd_start, "book": cmd_book, "record": cmd_record, "status": cmd_status}[args.cmd](args)


if __name__ == "__main__":
    main()
