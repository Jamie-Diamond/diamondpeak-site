#!/usr/bin/env python3
"""CLI wrapper around IcuClient — lets Claude call the Intervals.icu API via Bash.

Usage:
  python3 icu_fetch.py --athlete jamie --endpoint wellness --days 14
  python3 icu_fetch.py --athlete jamie --endpoint events --start 2026-05-11 --end 2026-06-01
  python3 icu_fetch.py --athlete jamie --endpoint activity_detail --activity-id i146909545
  python3 icu_fetch.py --athlete jamie --endpoint best_efforts --sport Ride --period 1y
  python3 icu_fetch.py --athlete jamie --endpoint power_curves --sport Ride --curves 90d
  python3 icu_fetch.py --athlete jamie --endpoint training_summary --start 2026-04-13 --end 2026-05-11
  python3 icu_fetch.py --athlete jamie --endpoint sport_settings --sport Ride

Outputs JSON to stdout. Exits non-zero on error.

CROSS-ATHLETE SCOPE. load_client resolves ANY slug in config/athletes.json for ANY
caller, so a chat session serving one athlete could write to another's calendar just by
typing their slug (the softer half of the 15 Jun cross-contamination incident; the MCP
half was closed by excluding the single-account IcuSync tools from the bot). When the
spawning process sets CC_ATHLETE_SCOPE, the four WRITE endpoints refuse a mismatched
--athlete. READS stay unrestricted: comparing athletes is legitimate coaching work and a
read cannot damage anyone. Absent the variable, behaviour is exactly as before, so
hand-runs and CLI use keep working.

AUTHORITY, AND WHY A CHAT WRITE PINS THE DAY. `--authority agreed` (the DEFAULT) means
"this write is the athlete's agreement", and a calendar write IS the evidence of
agreement — it already funnels through this one function, so pinning here is
deterministic and needs no prose parsing, no LLM discipline and no prompt rule. The date
written is pinned (lib/agreed_week) and no generator will touch that day again this week.
`--authority coach-auto` writes WITHOUT pinning: a readiness modulation is the coach's
own automated adjustment, not the athlete's agreement, and it is additionally bounded to
editing/pushing TODAY and forbidden from deleting anything at all
(scripts/daily-prescription.py is the caller).

  !! NEVER ROUTE lib/plan_builder.push THROUGH THIS CLI. !!
  Pin-on-write is safe only because plan_builder.push talks to IcuClient directly. Route
  the generator through here "for consistency" and every generated week would pin ITSELF,
  and the Sunday build would freeze permanently after one run — it would find all seven
  days agreed, propose nothing, and the athlete's plan would never change again. If you
  need a CLI path for the generator, give it --authority coach-auto and a scope check of
  its own; do not let it reach the pin site below.
"""

import argparse
import json
import os
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "lib"))

import agreed_week
from icu_api import IcuClient

CONFIG = ROOT / "config" / "athletes.json"

# How far either side of today to look when an event id has to be resolved to a DATE
# (a delete, or an edit whose payload carries no date). Wide enough to cover every week
# a pin can be filed against (_KEEP is 6 weeks) and one ICU call, not seven.
_LOOKUP_BACK_DAYS = 7
_LOOKUP_FWD_DAYS = 45

# The mutating endpoints. edit_activity belongs here too — renaming another athlete's
# activity is cross-contamination even though it touches no planned event.
WRITE_ENDPOINTS = frozenset({"push_workout", "edit_workout", "edit_activity",
                             "delete_workout"})


def scope_violation(slug: str, endpoint: str, scope: str | None = None) -> str | None:
    """The refusal message when `endpoint` would write to `slug` from a session scoped to
    another athlete, else None. Pure, so it can be tested without the network.

    scope defaults to CC_ATHLETE_SCOPE. An unset OR EMPTY variable means unscoped —
    an empty string must not read as "a scope that matches nobody", which would break
    every hand-run that happens to export it blank."""
    if scope is None:
        scope = os.environ.get("CC_ATHLETE_SCOPE", "")
    scope = (scope or "").strip()
    if not scope or endpoint not in WRITE_ENDPOINTS:
        return None
    if slug == scope:
        return None
    return (f"ERROR: refusing to write to '{slug}' from a session scoped to '{scope}'. "
            f"Reads of another athlete are fine; writes are not.")


def payload_date(fields: dict) -> str | None:
    """The calendar date a write payload lands on, or None when it names none.

    Three spellings are accepted because three callers use three: `event_date` (the
    IcuClient signature), `start_date_local` (the raw ICU field, what plan_builder
    builds) and `date` (what the daily-prescription prompt has always shown — which
    would in fact have raised TypeError on push_workout(**fields), so it is normalised
    to event_date in main() rather than merely tolerated here)."""
    for k in ("event_date", "start_date_local", "date"):
        v = (fields or {}).get(k)
        if v:
            return str(v)[:10]
    return None


def authority_violation(endpoint: str, authority: str, event_date: str | None,
                        today: str | None = None) -> str | None:
    """The refusal message when a coach-auto write exceeds its remit, else None. Pure.

    coach-auto is scripts/daily-prescription.py: SAME-DAY modulation of a session that is
    already there. Moving or removing days is not its job, and both are how a 05:00 cron
    silently loses a week.
      - delete_workout: refused outright.
      - push/edit: refused for any date other than today. A payload with NO date is
        allowed — that is an edit in place, which cannot move a session."""
    if (authority or "").strip() != "coach-auto":
        return None
    if endpoint == "delete_workout":
        return ("ERROR: --authority coach-auto may not delete calendar events. It may "
                "edit or replace TODAY's session in place; removing a day is the "
                "planner's job, not the daily prescription's.")
    if endpoint in ("push_workout", "edit_workout") and event_date:
        t = today or date.today().isoformat()
        if str(event_date)[:10] != t:
            return (f"ERROR: --authority coach-auto may only write TODAY ({t}); this "
                    f"payload targets {str(event_date)[:10]}. A readiness modulation "
                    f"adjusts today's session — it does not move other days.")
    return None


def _resolve_event_date(client, event_id, start: date, end: date) -> tuple[bool, str | None]:
    """(lookup_succeeded, date) for `event_id` within [start, end].

    ICU has no single-event endpoint, so this is one get_events call over the window. The
    boolean matters: "the window was read and this id is not in it" (True, None) means the
    event is not on any date in the window and is safe to touch, whereas "the read FAILED"
    (False, None) means we know nothing — and the caller fails closed on that."""
    try:
        events = client.get_events(start.isoformat(), end.isoformat()) or []
    except Exception as e:
        print(f"WARNING: could not read the calendar to check this event's date ({e!r})",
              file=sys.stderr)
        return False, None
    want = str(event_id)
    for e in events:
        if str(e.get("id")) == want:
            d = str(e.get("start_date_local") or e.get("date") or "")[:10]
            # Found but dateless is NOT "not on a pinned date" — it is "unknown", and
            # unknown must fail closed at the guard, so report the lookup as failed.
            return bool(d), (d or None)
    return True, None


PIN_REFUSED_EXIT = 5     # distinct from 2 (cross-athlete / authority remit) and 1 (usage)


def _guard_delete(slug: str, client, event_id) -> None:
    """Refuse a delete that would remove an event from an AGREED day. Exits non-zero with
    a message the model can read and relay; returns silently when the delete is fine.

    This is the ONLY guard on the chat path's ability to remove a session, and one of the
    two candidate causes of the 10 Aug empty calendar (the other being a build that
    refused to write). NOTHING is pinned for most athletes most weeks, and that case takes
    the fast path with no extra ICU call and behaviour identical to before."""
    today = date.today()
    try:
        pinned = agreed_week.pinned_dates_span(
            slug, today - timedelta(days=_LOOKUP_BACK_DAYS),
            today + timedelta(days=_LOOKUP_FWD_DAYS))
    except Exception as e:
        print(f"WARNING: could not read the agreed-week record ({e!r}) — proceeding",
              file=sys.stderr)
        return
    if not pinned:
        return
    ok, d = _resolve_event_date(client, event_id,
                               today - timedelta(days=_LOOKUP_BACK_DAYS),
                               today + timedelta(days=_LOOKUP_FWD_DAYS))
    if ok and d is None:
        # The window covering every pinned date was read successfully and this event is
        # not in it, so it cannot be on a pinned date. Allowed.
        return
    # d is None here only when the lookup FAILED, and delete_refusal fails closed on that.
    msg = agreed_week.delete_refusal(d, pinned)
    if msg:
        print(msg, file=sys.stderr)
        sys.exit(PIN_REFUSED_EXIT)


def _pin_after_write(args, client, fields: dict, result, segments) -> None:
    """Record the pin for a write made under `--authority agreed`. Never raises.

    A pin failure must NOT fail the write, which has already happened: raising here would
    tell the model its push failed and invite a retry, which duplicates the event. The
    failure is reported on stderr and to ops_log instead — under-pinning is a week lost
    quietly, so it must be visible.

    The pin's session is read from the EVENT ICU RETURNED wherever possible, falling back
    to the payload. That matters most for edit_workout, whose payload is often just a name
    or a description: the record has to describe the whole session on that day, not the
    fragment that was amended."""
    if (args.authority or "agreed") != "agreed":
        return
    try:
        ev = result if isinstance(result, dict) else {}
        d = str(ev.get("start_date_local") or "")[:10] or payload_date(fields)
        if not d and args.event_id:
            today = date.today()
            _ok, d = _resolve_event_date(client, args.event_id,
                                         today - timedelta(days=_LOOKUP_BACK_DAYS),
                                         today + timedelta(days=_LOOKUP_FWD_DAYS))
        if not d:
            raise ValueError("could not establish the date this write landed on")
        secs = ev.get("moving_time") or fields.get("moving_time") or 0
        mins = int(round(float(secs) / 60)) if secs else int(
            fields.get("minutes") or fields.get("duration_min") or 0)
        load = (ev.get("load_target") or fields.get("planned_training_load")
                or fields.get("load_target"))
        sess = agreed_week.session_record(
            sport=(ev.get("type") or fields.get("sport") or fields.get("type") or ""),
            name=(ev.get("name") or fields.get("name") or ""),
            minutes=mins, load_target=load, segments=segments)
        agreed_week.pin(args.athlete, d, why="agreed in chat", by="chat", session=sess)
        print(f"[icu_fetch] {d} is now AGREED (pinned) — no plan build will rebuild that "
              f"day this week. Release it with lib/agreed_week.py --release if that "
              f"changes.", file=sys.stderr)
    except Exception as e:
        print(f"WARNING: the write succeeded but the day could NOT be pinned ({e!r}) — a "
              f"plan build may rebuild it", file=sys.stderr)
        try:
            import ops_log
            ops_log.alert("icu_fetch", f"write succeeded but pin FAILED ({e!r}) — that "
                          f"day is unprotected from the next build", athlete=args.athlete)
        except Exception:
            pass


def load_client(slug: str) -> IcuClient:
    athletes = json.loads(CONFIG.read_text())
    if slug not in athletes:
        print(f"ERROR: athlete '{slug}' not in config/athletes.json", file=sys.stderr)
        sys.exit(1)
    a = athletes[slug]
    return IcuClient(a["icu_athlete_id"], a["icu_api_key"])


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--athlete", required=True)
    p.add_argument("--endpoint", required=True,
                   choices=["profile", "sport_settings", "fitness", "wellness",
                            "history", "events", "activity_detail", "extended_metrics",
                            "streams", "best_efforts", "power_curves", "training_summary",
                            "push_workout", "edit_workout", "edit_activity", "delete_workout"])
    p.add_argument("--days",        type=int, default=14)
    p.add_argument("--start",       default=None, help="oldest date YYYY-MM-DD")
    p.add_argument("--end",         default=None, help="newest date YYYY-MM-DD")
    p.add_argument("--sport",       default=None, help="e.g. Ride, Run, Swim")
    p.add_argument("--period",      default="1y",  help="for best_efforts: 4w 6w 3m 6m 1y 2y all")
    p.add_argument("--curves",      default="90d", help="for power_curves: 90d 1y s0 s1 all")
    p.add_argument("--activity-id", default=None, dest="activity_id")
    p.add_argument("--event-id",    default=None, dest="event_id")
    p.add_argument("--newest",      default=None, help="future end date for fitness projection")
    p.add_argument("--payload",     default=None,
                   help="JSON string for push_workout / edit_workout fields")
    p.add_argument("--authority",   default="agreed", choices=["agreed", "coach-auto"],
                   help="agreed (default): this write is the athlete's agreement, so the "
                        "date is PINNED and no generator will rebuild it this week. "
                        "coach-auto: an automated same-day adjustment — writes today "
                        "only, never deletes, never pins.")
    p.add_argument("--segments",    default=None,
                   help="JSON array of {minutes, zone} for the session being written. "
                        "Recorded into the pin so the validator can account for the day's "
                        "zone minutes. Absent, the pin is COARSE (single easy segment) — "
                        "it still protects the day, but its zone accounting is "
                        "approximate. If you rendered the workout with plan_tools "
                        "render-workout, pass the same segments here.")
    args = p.parse_args()

    # Refuse BEFORE load_client: a session scoped to one athlete must never even load
    # another athlete's API key.
    _viol = scope_violation(args.athlete, args.endpoint)
    if _viol:
        print(_viol, file=sys.stderr)
        sys.exit(2)

    ep = args.endpoint

    # Payload parsed ONCE and BEFORE the authority check, which reads its date. A
    # malformed payload used to surface as a bare json traceback out of the branch below.
    fields: dict = {}
    if args.payload:
        try:
            fields = json.loads(args.payload)
        except Exception as e:
            print(f"ERROR: --payload is not valid JSON ({e})", file=sys.stderr)
            sys.exit(1)
        if not isinstance(fields, dict):
            print("ERROR: --payload must be a JSON object", file=sys.stderr)
            sys.exit(1)
    # `date` is the spelling the daily-prescription prompt has shown since it was written,
    # and push_workout(**fields) raises TypeError on it (event_date is required and date
    # is not a parameter) — so that documented call has never been able to work. Accept it
    # as an alias for any in-flight model habit rather than only fixing the prompt.
    if ep == "push_workout" and fields.get("date") and not fields.get("event_date"):
        fields["event_date"] = str(fields.pop("date"))

    _aviol = authority_violation(ep, args.authority, payload_date(fields))
    if _aviol:
        print(_aviol, file=sys.stderr)
        sys.exit(2)

    segments = None
    if args.segments:
        try:
            segments = json.loads(args.segments)
        except Exception as e:
            print(f"ERROR: --segments is not valid JSON ({e})", file=sys.stderr)
            sys.exit(1)
        if not isinstance(segments, list):
            print("ERROR: --segments must be a JSON array", file=sys.stderr)
            sys.exit(1)

    client = load_client(args.athlete)
    if ep == "profile":
        result = client.get_athlete_profile()
    elif ep == "sport_settings":
        result = client.get_sport_settings(args.sport)
    elif ep == "fitness":
        result = client.get_fitness(args.days, newest=args.newest)
    elif ep == "wellness":
        result = client.get_wellness(args.days, newest=args.newest)
    elif ep == "history":
        result = client.get_training_history(args.days, sport=args.sport)
    elif ep == "events":
        if not args.start or not args.end:
            print("ERROR: --start and --end required for events", file=sys.stderr)
            sys.exit(1)
        result = client.get_events(args.start, args.end, category=args.sport)
    elif ep == "activity_detail":
        if not args.activity_id:
            print("ERROR: --activity-id required", file=sys.stderr)
            sys.exit(1)
        result = client.get_activity_detail(args.activity_id)
    elif ep == "extended_metrics":
        if not args.activity_id:
            print("ERROR: --activity-id required", file=sys.stderr)
            sys.exit(1)
        result = client.get_extended_metrics(args.activity_id)
    elif ep == "streams":
        if not args.activity_id:
            print("ERROR: --activity-id required", file=sys.stderr)
            sys.exit(1)
        result = client.get_activity_streams(args.activity_id)
    elif ep == "best_efforts":
        result = client.get_best_efforts(args.sport or "Ride", args.period)
    elif ep == "power_curves":
        result = client.get_power_curves(args.sport or "Ride", args.curves)
    elif ep == "training_summary":
        if not args.start or not args.end:
            print("ERROR: --start and --end required for training_summary", file=sys.stderr)
            sys.exit(1)
        result = client.get_training_summary(args.start, args.end, sport=args.sport)
    elif ep == "push_workout":
        if not args.payload:
            print("ERROR: --payload JSON required for push_workout", file=sys.stderr)
            sys.exit(1)
        result = client.push_workout(**fields)
        _pin_after_write(args, client, fields, result, segments)
    elif ep == "edit_workout":
        if not args.event_id or not args.payload:
            print("ERROR: --event-id and --payload required for edit_workout", file=sys.stderr)
            sys.exit(1)
        result = client.edit_workout(args.event_id, **fields)
        _pin_after_write(args, client, fields, result, segments)
    elif ep == "edit_activity":
        if not args.activity_id or not args.payload:
            print("ERROR: --activity-id and --payload required for edit_activity", file=sys.stderr)
            sys.exit(1)
        result = client.edit_activity(args.activity_id, **fields)
        # NOT pinned: an activity is training that has already happened. Renaming or
        # annotating it says nothing about what the coming week should contain.
    elif ep == "delete_workout":
        if not args.event_id:
            print("ERROR: --event-id required for delete_workout", file=sys.stderr)
            sys.exit(1)
        _guard_delete(args.athlete, client, args.event_id)   # exits non-zero on a pin
        client.delete_workout(args.event_id)
        result = {"deleted": args.event_id}

    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
