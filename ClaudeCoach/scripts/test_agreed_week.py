#!/usr/bin/env python3
"""Offline tests for the agreed week — plan authority, increment 2 (13 Aug 2026).
Run: python3 ClaudeCoach/scripts/test_agreed_week.py

WHAT THIS GUARDS. One invariant, stated once: an AGREED day's content survives every path
through the plan builder. Nothing here needs the network, an LLM or ICU — every check
drives a pure function or writes to a tmpdir, and the two that need a calendar client use
a stub that RECORDS what was asked of it.

The checks that matter most, and why each one exists:

  * pinned_dates() vs protected_dates(). A declared-unavailable day means "put nothing
    here"; a pin means "keep exactly what is here". Feed the availability union into
    push()'s delete-skip and a stale session on a declared-unavailable Friday survives on a
    day the athlete told us they cannot train. They must stay two sets.
  * The splice happens BEFORE quality injection and drops whatever the proposer put on an
    agreed day. If it ran after injection, quality would be sized against a partial week.
  * flex() must refuse a pinned session, and so must the long-session clamps and the run
    cap — the design names only flex(), but every one of them mutates minutes.
  * push() must skip pinned dates on the push list AND the delete list, and the pre-delete
    guard must FIRE (raise, not assert) if it is ever reached with a pinned date.
  * The proposer's target is the whole-week target MINUS the agreed load, while the
    whole-week gates keep seeing the whole-week number. Backwards, that produces a
    systematically light or heavy week for every athlete (design section 10).
"""
import ast
import copy
import json
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

_here = Path(__file__).resolve().parent
BASE = _here.parent
sys.path.insert(0, str(BASE / "lib"))
sys.path.insert(0, str(BASE / "ironman-analysis"))

# SILENCE ops_log FIRST, before anything that logs is imported. agreed_week._audit and
# weekly_availability._audit both skip logging when a test `base` is passed, but the
# production pin site (icu_fetch._pin_after_write) cannot pass one — it would otherwise
# write test slugs into the operator's real ops-alerts.log, which is the one file that has
# to stay trustworthy.
import ops_log                    # noqa: E402
ops_log.record_run = lambda *a, **k: None
ops_log.alert = lambda *a, **k: None
ops_log.log_outbound = lambda *a, **k: None

import agreed_week as aw          # noqa: E402
import icu_fetch                  # noqa: E402
import plan_builder as pb         # noqa: E402

FAILED = []


def check(name, cond):
    print(("PASS" if cond else "FAIL"), name)
    if not cond:
        FAILED.append(name)


WS = "2026-08-17"              # a Monday
TUE, THU, FRI = "2026-08-18", "2026-08-20", "2026-08-21"
SLUG = "testathlete"
TMP = tempfile.mkdtemp(prefix="agreedweek-test-")


def ride(minutes=240, load=212, segs=None):
    return aw.session_record("Ride", "Long ride 4h", minutes, load,
                             segs if segs is not None else [{"minutes": minutes,
                                                             "zone": "endurance"}])


# --- 1) pin / release / read round-trip ---------------------------------------------------
aw.pin(SLUG, THU, why="agreed in chat", session=ride(), base=TMP)
aw.pin(SLUG, FRI, why="no training Fri", session=None, base=TMP)

pins = aw.pins_for_week(SLUG, WS, base=TMP)
check("both pins come back for the week they were filed against", set(pins) == {THU, FRI})
check("the week is keyed by its MONDAY, so any day inside it resolves the same record",
      set(aw.pins_for_week(SLUG, "2026-08-22", base=TMP)) == {THU, FRI})
check("a different week sees nothing", aw.pins_for_week(SLUG, "2026-08-24", base=TMP) == {})
check("a None week_start resolves to nothing (never last week's)",
      aw.pins_for_week(SLUG, None, base=TMP) == {})
check("the session record survives the round trip verbatim",
      pins[THU]["session"]["segments"] == [{"minutes": 240, "zone": "endurance"}]
      and pins[THU]["session"]["load_target"] == 212
      and pins[THU]["session"]["coarse"] is False)
check("a rest-day pin stores session=None (nothing on the day IS the agreement)",
      pins[FRI]["session"] is None)
check("every pin records who and why", pins[THU]["why"] == "agreed in chat"
      and pins[THU]["by"] == "chat" and bool(pins[THU]["at"]))
check("pinned_dates() maps date -> why",
      aw.pinned_dates(SLUG, WS, base=TMP) == {THU: "agreed in chat", FRI: "no training Fri"})
check("pinned_load sums only the pins that carry a costed session (a rest day is 0)",
      aw.pinned_load(pins) == 212)

# Re-pinning one date must replace THAT date and touch no other.
aw.pin(SLUG, THU, why="moved it, still agreed", session=ride(210, 190), base=TMP)
_p = aw.pins_for_week(SLUG, WS, base=TMP)
check("re-pinning a date replaces that date's record only",
      _p[THU]["why"] == "moved it, still agreed" and _p[THU]["session"]["load_target"] == 190
      and set(_p) == {THU, FRI})

# A hand-edited record whose pin date belongs to another week must not protect a day in a
# week nobody agreed anything about.
_raw = json.loads(aw.path_for(SLUG, TMP).read_text())
_raw["weeks"][0]["pins"]["2026-09-02"] = {"why": "typo", "at": "x", "by": "x", "session": None}
aw.path_for(SLUG, TMP).write_text(json.dumps(_raw))
check("a pin dated outside its own week record is ignored",
      set(aw.pins_for_week(SLUG, WS, base=TMP)) == {THU, FRI})

check("an unreadable store degrades to 'nothing pinned' rather than raising", (
    aw.path_for(SLUG, TMP).write_text("{not json"),
    aw.pins_for_week(SLUG, WS, base=TMP) == {} and aw.load_raw(SLUG, TMP) == {},
)[1])

# rebuild the store for the rest of the run
aw.path_for(SLUG, TMP).unlink()
aw.pin(SLUG, THU, why="agreed in chat", session=ride(), base=TMP)
aw.pin(SLUG, FRI, why="no training Fri", session=None, base=TMP)

released = aw.release(SLUG, WS, dates=[FRI], base=TMP)
check("releasing one date returns it and leaves the others pinned",
      released == [FRI] and set(aw.pins_for_week(SLUG, WS, base=TMP)) == {THU})
check("the released week record is KEPT with released_at, so who-dropped-what is answerable",
      bool((aw.for_week(SLUG, WS, base=TMP) or {}).get("released_at")))
check("releasing with no dates releases the whole week",
      aw.release(SLUG, WS, base=TMP) == [THU]
      and aw.pins_for_week(SLUG, WS, base=TMP) == {})
check("releasing an already-empty week is a no-op, not an error",
      aw.release(SLUG, WS, base=TMP) == [])

# A pin AFTER a release re-opens the week: that is a new agreement, not a revoked one.
aw.pin(SLUG, THU, why="agreed in chat", session=ride(), base=TMP)
check("pinning after a release re-arms the week",
      set(aw.pins_for_week(SLUG, WS, base=TMP)) == {THU}
      and (aw.for_week(SLUG, WS, base=TMP) or {}).get("released_at") is None)

# THE _KEEP WINDOW MUST NOT BE ABLE TO DROP A LIVE PIN. The obvious "keep the six latest
# weeks" is safe for weekly_availability (declarations are written for the imminent week)
# and WRONG here: a pin can be filed against any date the chat model touches, so agreeing a
# race-week session two months out would trim away next week's pin and the Sunday build
# hours later would rebuild the day the athlete just agreed.
_past = [{"week_start": (date.fromisoformat(WS) - timedelta(days=7 * i)).isoformat(),
          "pins": {}} for i in range(1, 12)]
_future = [{"week_start": (date.fromisoformat(WS) + timedelta(days=7 * i)).isoformat(),
            "pins": {}} for i in range(0, 10)]
_kept = aw._trim(_past + _future, today=date.fromisoformat(WS))
check("every week from this Monday onward survives the trim, however many there are",
      [w["week_start"] for w in _kept if w["week_start"] >= WS]
      == [w["week_start"] for w in _future])
check(f"at most _KEEP={aw._KEEP} PAST weeks are kept, and they are the most recent ones",
      [w["week_start"] for w in _kept if w["week_start"] < WS]
      == [w["week_start"] for w in _past[:aw._KEEP]][::-1])
check("a week with an unparseable week_start is treated as past, so it can never hold a "
      "live week out of the store",
      aw._trim([{"week_start": "not-a-date", "pins": {}}] + _future,
               today=date.fromisoformat(WS))[0]["week_start"] == "not-a-date")
# End to end through pin(): a far-future pin must not evict the imminent one.
aw.pin(SLUG, THU, why="agreed in chat", session=ride(), base=TMP)
for i in range(1, 9):
    aw.pin(SLUG, date.fromisoformat(WS) + timedelta(days=7 * i), why="w", base=TMP)
check("pinning eight future weeks does NOT evict the pin for the week about to be planned",
      set(aw.pins_for_week(SLUG, WS, base=TMP)) >= {THU})
aw.path_for(SLUG, TMP).unlink()
aw.pin(SLUG, THU, why="agreed in chat", session=ride(), base=TMP)
aw.pin(SLUG, FRI, why="no training Fri", session=None, base=TMP)

# --- 2) protected_dates unions the availability declaration ------------------------------
# A REAL weekly_availability declaration in the same tmpdir, not a monkeypatch: the union
# has to work against the file that module actually writes.
import weekly_availability as wa   # noqa: E402
wa.record(SLUG, WS, hours=12, source="test", base=TMP,
          unavailable_days=["Wed"], run_days=["Tue"])

prot = aw.protected_dates(SLUG, WS, base=TMP)
check("protected_dates unions the pins with the declared unavailable day",
      set(prot) == {"2026-08-19", THU, FRI})
check("the unavailable day carries a reason the athlete would recognise",
      "unavailable" in prot["2026-08-19"])
check("a pin's own reason is not overwritten by the union", prot[THU] == "agreed in chat")
check("pinned_dates() does NOT include the unavailable day — that set is for push(), and a "
      "stale event on a declared-unavailable day must still be DELETED",
      set(aw.pinned_dates(SLUG, WS, base=TMP)) == {THU, FRI})
check("pinned_load ignores the availability union entirely (an unavailable day costs 0)",
      aw.pinned_load(aw.pins_for_week(SLUG, WS, base=TMP)) == 212)
check("pinned_dates_span finds pins across weeks without knowing which week they are in",
      set(aw.pinned_dates_span(SLUG, "2026-08-01", "2026-09-30", base=TMP)) == {THU, FRI})
check("pinned_dates_span honours its bounds",
      aw.pinned_dates_span(SLUG, "2026-08-01", "2026-08-19", base=TMP) == {})


# --- 3) the coarse fallback ---------------------------------------------------------------
c = aw.session_record("Ride", "Endurance ride", 90, 60, segments=None)
check("no segments -> a single easy segment, marked coarse",
      c["coarse"] is True and c["segments"] == [{"minutes": 90, "zone": "endurance"}])
check("the coarse zone is a name from the PRIMARY zone table for the sport (a TID band "
      "label would fall through to the sport default IF — the 9 Aug outage)", (
          __import__("primitives.planned_tss", fromlist=["x"]).segment_if("Ride", "endurance")
          == 0.65))
check("run and swim coarse pins land on their own easy zone",
      aw.session_record("Run", "", 40, 30)["segments"][0]["zone"] == "easy"
      and aw.session_record("Swim", "", 40, 30)["segments"][0]["zone"] == "easy")
c0 = aw.session_record("Ride", "no duration known", 0, None, segments=None)
check("a coarse pin with NO duration still protects the day but carries no segments — it "
      "contributes nothing to zone accounting, which is why pin() logs it loudly",
      c0["coarse"] is True and c0["segments"] == [] and c0["minutes"] == 0)
check("an unknown sport cannot invent a zone", aw.session_record("Kayak", "", 60, 40)["segments"] == [])
check("segments present -> NOT coarse, and minutes are derived when not given",
      aw.session_record("Ride", "x", None, 100,
                        [{"minutes": 60, "zone": "endurance"},
                         {"minutes": 30, "zone": "sweetspot"}]) ==
      {"sport": "Ride", "name": "x", "minutes": 90, "load_target": 100, "coarse": False,
       "segments": [{"minutes": 60, "zone": "endurance"},
                    {"minutes": 30, "zone": "sweetspot"}]})
check("pin_sessions never returns a session for a rest-day pin",
      [s["date"] for s in aw.pin_sessions(aw.pins_for_week(SLUG, WS, base=TMP))] == [THU])


# --- 4) the delete refusal ---------------------------------------------------------------
PIN = {THU: "agreed in chat"}
check("deleting on a pinned date is refused", bool(aw.delete_refusal(THU, PIN)))
check("the refusal names the date and how to get out of it",
      THU in aw.delete_refusal(THU, PIN) and "--release" in aw.delete_refusal(THU, PIN))
check("deleting on any other date is allowed", aw.delete_refusal(TUE, PIN) is None)
check("a datetime-shaped date still matches", bool(aw.delete_refusal(THU + "T00:00:00", PIN)))
check("with NOTHING pinned nothing is refused, even an unknown date",
      aw.delete_refusal(None, {}) is None and aw.delete_refusal(THU, {}) is None)
check("an UNKNOWN date fails CLOSED while something is pinned (a readable refusal is "
      "recoverable in one turn; a destroyed agreed session is not)",
      bool(aw.delete_refusal(None, PIN)))

# The lookup that feeds it: (lookup_ok, date), so "read the window, id absent" is
# distinguishable from "the read failed".
class _StubClient:
    def __init__(self, events, boom=False):
        self.events, self.boom, self.asked = events, boom, []

    def get_events(self, start, end, category=None):
        self.asked.append((start, end))
        if self.boom:
            raise RuntimeError("ICU down")
        return self.events

    def delete_workout(self, eid):
        self.asked.append(("delete", eid))

    def push_workout(self, **payload):
        self.asked.append(("push", payload.get("event_date")))
        return {"id": f"new-{payload.get('event_date')}"}


_ev = [{"id": "e1", "start_date_local": f"{THU}T00:00:00", "category": "WORKOUT"},
       {"id": "e2", "start_date_local": f"{TUE}T00:00:00", "category": "WORKOUT"}]
d1, d2 = date.fromisoformat("2026-08-10"), date.fromisoformat("2026-09-20")
check("a known event resolves to its date",
      icu_fetch._resolve_event_date(_StubClient(_ev), "e1", d1, d2) == (True, THU))
check("an event absent from a window that WAS read reports (True, None) = safe to touch",
      icu_fetch._resolve_event_date(_StubClient(_ev), "nope", d1, d2) == (True, None))
check("a FAILED read reports (False, None) so the guard can fail closed",
      icu_fetch._resolve_event_date(_StubClient(_ev, boom=True), "e1", d1, d2) == (False, None))
check("an event found without a date is 'unknown', not 'safe'",
      icu_fetch._resolve_event_date(_StubClient([{"id": "e3"}]), "e3", d1, d2) == (False, None))


# --- 5) coach-auto's remit ----------------------------------------------------------------
V = icu_fetch.authority_violation
check("coach-auto may not delete at all", bool(V("delete_workout", "coach-auto", None)))
check("coach-auto may write today",
      V("push_workout", "coach-auto", "2026-08-13", "2026-08-13") is None)
check("coach-auto may not write another date",
      bool(V("push_workout", "coach-auto", "2026-08-20", "2026-08-13")))
check("coach-auto may not write YESTERDAY either",
      bool(V("edit_workout", "coach-auto", "2026-08-12", "2026-08-13")))
check("coach-auto may edit in place (a payload with no date cannot move a session)",
      V("edit_workout", "coach-auto", None, "2026-08-13") is None)
check("the default authority is unrestricted by this check (it is the AGREED path)",
      V("delete_workout", "agreed", None) is None
      and V("push_workout", "agreed", "2026-08-20", "2026-08-13") is None)
check("the refusal explains what coach-auto IS for, so the model can comply not retry",
      "today" in V("push_workout", "coach-auto", "2026-08-20", "2026-08-13").lower())

check("payload_date reads all three spellings, event_date first",
      icu_fetch.payload_date({"event_date": "2026-08-20"}) == "2026-08-20"
      and icu_fetch.payload_date({"start_date_local": "2026-08-20T00:00:00"}) == "2026-08-20"
      and icu_fetch.payload_date({"date": "2026-08-20"}) == "2026-08-20"
      and icu_fetch.payload_date({}) is None)

_fetch_src = (BASE / "lib" / "icu_fetch.py").read_text()
check("icu_fetch carries the 'never route plan_builder.push through this CLI' guard",
      "NEVER ROUTE lib/plan_builder.push THROUGH THIS CLI" in _fetch_src)
check("the daily prescription passes --authority coach-auto",
      "--authority coach-auto" in (BASE / "scripts" / "daily-prescription.py").read_text())
_dp = (BASE / "scripts" / "daily-prescription.py").read_text()
check("the daily-prescription prompt states the no-delete / today-only rules too (the "
      "code enforces them, but the model should not be surprised by a refusal)",
      "may NOT delete a calendar event" in _dp and "and nothing else" in _dp)
check("the daily-prescription push payload uses event_date, not date — push_workout(**fields) "
      "raises TypeError on `date`, so the documented call could never have worked",
      '"event_date":"{today}"' in _dp and '"date":"{today}"' not in _dp)


# --- 5b) the pin site itself: what a chat write records ----------------------------------
# _pin_after_write is where a calendar write becomes an agreement. Driven with a stub
# client and the store pointed at the tmpdir; PIN_SLUG is separate so it cannot disturb the
# fixtures above.
PIN_SLUG = "pinsite"


class _Args:
    def __init__(self, **kw):
        self.athlete, self.authority, self.event_id = PIN_SLUG, "agreed", None
        self.__dict__.update(kw)


_aw_base = aw.BASE
try:
    aw.BASE = Path(TMP)
    # A push with segments: the exact pin.
    icu_fetch._pin_after_write(
        _Args(), _StubClient([]),
        {"sport": "Ride", "event_date": THU, "name": "Long ride 4h",
         "planned_training_load": 212},
        {"start_date_local": f"{THU}T00:00:00", "type": "Ride", "name": "Long ride 4h",
         "load_target": 212, "moving_time": 14400},
        [{"minutes": 240, "zone": "endurance"}])
    _rec = aw.pins_for_week(PIN_SLUG, WS)[THU]
    check("a chat push_workout pins the date it wrote", bool(_rec))
    check("the pin records sport, name, minutes, load and segments as written",
          _rec["session"] == {"sport": "Ride", "name": "Long ride 4h", "minutes": 240,
                              "load_target": 212, "coarse": False,
                              "segments": [{"minutes": 240, "zone": "endurance"}]})
    check("the pin is attributed to the chat", _rec["by"] == "chat" and bool(_rec["why"]))

    # No segments: the coarse fallback, minutes recovered from the event's moving_time.
    icu_fetch._pin_after_write(
        _Args(), _StubClient([]), {"sport": "Run", "event_date": TUE, "name": "Easy run"},
        {"start_date_local": f"{TUE}T00:00:00", "type": "Run", "name": "Easy run",
         "load_target": 45, "moving_time": 2700}, None)
    _c = aw.pins_for_week(PIN_SLUG, WS)[TUE]["session"]
    check("a write with no --segments still pins the day, marked coarse",
          _c["coarse"] is True and _c["minutes"] == 45
          and _c["segments"] == [{"minutes": 45, "zone": "easy"}])

    # An edit whose payload is only a name: the DATE and the rest of the session come from
    # the event ICU returned, not from the fragment that was amended.
    icu_fetch._pin_after_write(
        _Args(event_id="e9"), _StubClient([]), {"name": "Renamed"},
        {"start_date_local": f"{FRI}T00:00:00", "type": "Swim", "name": "Renamed",
         "load_target": 40, "moving_time": 1800}, None)
    _e = aw.pins_for_week(PIN_SLUG, WS)[FRI]["session"]
    check("an edit_workout pins the date from the returned event, and records the WHOLE "
          "session rather than the amended fragment",
          _e["sport"] == "Swim" and _e["minutes"] == 30 and _e["load_target"] == 40)

    # coach-auto must not pin.
    _before = set(aw.pins_for_week(PIN_SLUG, WS))
    icu_fetch._pin_after_write(
        _Args(authority="coach-auto"), _StubClient([]),
        {"sport": "Ride", "event_date": "2026-08-19"},
        {"start_date_local": "2026-08-19T00:00:00", "type": "Ride"}, None)
    check("a coach-auto write pins NOTHING (a readiness modulation is not the athlete's "
          "agreement)", set(aw.pins_for_week(PIN_SLUG, WS)) == _before)

    # A pin failure must not fail the write, which has already happened: raising would tell
    # the model its push failed and invite a retry, duplicating the event.
    _boom = aw.pin
    try:
        aw.pin = lambda *a, **k: (_ for _ in ()).throw(OSError("disk full"))
        icu_fetch._pin_after_write(
            _Args(), _StubClient([]), {"sport": "Ride", "event_date": THU},
            {"start_date_local": f"{THU}T00:00:00", "type": "Ride"}, None)
        check("a failed pin does not raise — the write already landed", True)
    except Exception:
        check("a failed pin does not raise — the write already landed", False)
    finally:
        aw.pin = _boom

    # No date anywhere: nothing is pinned, and it must not crash.
    icu_fetch._pin_after_write(_Args(), _StubClient([]), {"sport": "Ride"}, {}, None)
    check("a write whose date cannot be established pins nothing and does not crash",
          "2026-08-23" not in aw.pins_for_week(PIN_SLUG, WS))
finally:
    aw.BASE = _aw_base


# --- 6) the splice ------------------------------------------------------------------------
PINS = aw.pins_for_week(SLUG, WS, base=TMP)      # THU costed ride, FRI rest
PROPOSAL = {"sessions": [
    {"date": TUE, "sport": "Swim", "name": "CSS set",
     "segments": [{"minutes": 60, "zone": "z4"}]},
    {"date": THU, "sport": "Run", "name": "Tempo run - PROPOSER IGNORED THE CLAUSE",
     "segments": [{"minutes": 50, "zone": "tempo"}]},
    {"date": FRI, "sport": "Ride", "name": "Junk ride on an agreed rest day",
     "segments": [{"minutes": 60, "zone": "endurance"}]},
]}
spliced, notes = aw.splice_pinned(PROPOSAL, PINS)
_by = {s["date"]: s for s in spliced["sessions"]}
check("the proposed session on the pinned date is DROPPED",
      _by[THU]["sport"] == "Ride" and "IGNORED" not in _by[THU]["name"])
check("the pin record's own session is spliced in, flagged pinned",
      _by[THU]["pinned"] is True and _by[THU]["segments"] == [{"minutes": 240, "zone": "endurance"}]
      and _by[THU]["load_target"] == 212 and _by[THU]["minutes"] == 240)
check("a REST-day pin drops what was proposed and adds nothing", FRI not in _by)
check("days the generator owns are untouched", _by[TUE]["name"] == "CSS set"
      and not _by[TUE].get("pinned"))
check("the week stays in date order", [s["date"] for s in spliced["sessions"]] == [TUE, THU])
check("the input proposal is NOT mutated (the caller keeps its own copy)",
      len(PROPOSAL["sessions"]) == 3
      and PROPOSAL["sessions"][1]["name"].endswith("IGNORED THE CLAUSE"))
check("dropping a proposed session is NOTED — it means the prompt clause was disobeyed and "
      "that must be visible in the attempts log, not swallowed",
      any("dropped" in n for n in notes) and any(THU in n for n in notes))
check("the rest-day pin is named in the notes too", any("rest" in n for n in notes))
check("splice_pinned is IDEMPOTENT — this is what lets it be re-run after quality "
      "injection to revert anything the injector did to an agreed day",
      aw.splice_pinned(spliced, PINS)[0] == spliced)
check("no pins -> the proposal comes back unchanged",
      aw.splice_pinned(PROPOSAL, {})[0]["sessions"] == PROPOSAL["sessions"])

# The splice must run BEFORE quality injection, asserted against the source: after it, the
# injector would size the week's quality against a proposal missing the agreed days.
_s1 = (BASE / "scripts" / "stage1-plan.py").read_text()
check("stage1-plan splices after the winner is picked and BEFORE inject_quality",
      _s1.index("agreed_week.splice_pinned") < _s1.index("_qi.inject_quality"))
check("the splice is in _plan/main, NOT inside build_sessions or close_to_target (it would "
      "run 15x per build)",
      "agreed_week.splice_pinned" not in (BASE / "lib" / "plan_builder.py").read_text()
      and _s1.index("agreed_week.splice_pinned") > _s1.index("def _plan("))
check("the pins are read once, in _plan",
      _s1.count("agreed_week.pins_for_week") == 1
      and _s1.index("agreed_week.pins_for_week") > _s1.index("def _plan("))
check("stage1-plan re-splices after injection to revert any reach into an agreed day",
      _s1.index("_qi.inject_quality") < _s1.rindex("agreed_week.splice_pinned"))


# --- 7) the shaping levers refuse a pinned session ---------------------------------------
import importlib.util   # noqa: E402
_spec = importlib.util.spec_from_file_location("s1p", BASE / "scripts" / "stage1-plan.py")
s1p = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(s1p)

_brief = {"long_ride_target_min": 180, "long_run_cap_min": 90, "weekly_run_min_cap": 100,
          "weekly_run_mileage_cap_km": 18}
_pin_ride = {"date": THU, "sport": "Ride", "name": "Long ride 4h", "pinned": True,
             "segments": [{"minutes": 240, "zone": "endurance"}]}
_free_ride = {"date": TUE, "sport": "Ride", "name": "Endurance ride",
              "segments": [{"minutes": 90, "zone": "endurance"}]}
check("flex() has an explicit pinned clause, first",
      "def flex(s):\n        if _pinned(s):" in _s1)

# flex is a closure inside close_to_target, so it is exercised through the real function
# with a stubbed build (no ICU, no config) rather than called directly.
_calls = {"n": 0}


def _fake_build(slug, proposal):
    _calls["n"] += 1
    sess = [{"date": s["date"], "sport": s["sport"], "name": s["name"],
             "duration_min": sum(sg.get("minutes", 0) for sg in s.get("segments", [])),
             "load_target": sum(sg.get("minutes", 0) for sg in s.get("segments", [])),
             "description": "", "description_raw": "", "pinned": bool(s.get("pinned"))}
            for s in proposal["sessions"]]
    return {"athlete": slug, "week_start": WS, "fuel_g_hr": 0,
            "total_tss": sum(s["load_target"] for s in sess), "ok": True,
            "hard": [], "soft": [], "skipped_checks": [], "sessions": sess}


_orig_build = s1p.pb.build_sessions
s1p.pb.build_sessions = _fake_build
try:
    prop = {"sessions": [copy.deepcopy(_pin_ride), copy.deepcopy(_free_ride)]}
    # Target far above what is there: the closure loop will stretch everything it MAY.
    s1p.close_to_target(SLUG, prop, 800, {})
    _after = {s["date"]: sum(sg["minutes"] for sg in s["segments"]) for s in prop["sessions"]}
    check("close_to_target stretched the free ride to chase the target", _after[TUE] > 90)
    check("close_to_target did NOT stretch the pinned ride", _after[THU] == 240)

    # Step 1 clamps: the long-ride target would normally RESIZE a long ride to 180 min.
    prop = {"sessions": [copy.deepcopy(_pin_ride)]}
    s1p.close_to_target(SLUG, prop, None, {"long_ride_target_min": 180})
    check("the long-ride clamp does not resize a pinned long ride (240 min stands against "
          "a 180 min target)",
          sum(sg["minutes"] for sg in prop["sessions"][0]["segments"]) == 240)

    # The run cap: a pinned run's minutes count toward the ceiling but cannot be scaled or
    # dropped; the free runs absorb the whole reduction.
    prop = {"sessions": [
        {"date": THU, "sport": "Run", "name": "Agreed long run", "pinned": True,
         "segments": [{"minutes": 80, "zone": "easy"}]},
        {"date": TUE, "sport": "Run", "name": "Easy run",
         "segments": [{"minutes": 60, "zone": "easy"}]}]}
    s1p._clamp_runs_to_cap(prop, None, None, 5.3, run_min_cap=100)
    _m = {s["date"]: sum(sg["minutes"] for sg in s["segments"]) for s in prop["sessions"]}
    check("the run cap leaves a pinned run at its agreed length", _m.get(THU) == 80)
    check("the free run absorbs the whole cut and the ceiling holds",
          sum(_m.values()) <= 100 and _m.get(TUE, 0) < 60)

    # Pinned easy run + a PROGRESSING long run to protect + free easy runs: the agreed
    # minutes and the long run both come out of the room first, and only the free easy runs
    # are scaled.
    prop = {"sessions": [
        {"date": THU, "sport": "Run", "name": "Agreed easy run", "pinned": True,
         "segments": [{"minutes": 40, "zone": "easy"}]},
        {"date": "2026-08-23", "sport": "Run", "name": "Long run",
         "segments": [{"minutes": 90, "zone": "easy"}]},
        {"date": TUE, "sport": "Run", "name": "Easy run",
         "segments": [{"minutes": 50, "zone": "easy"}]},
        {"date": "2026-08-19", "sport": "Run", "name": "Easy run 2",
         "segments": [{"minutes": 40, "zone": "easy"}]}]}
    s1p._clamp_runs_to_cap(prop, None, 120, 5.3, run_min_cap=160, protect_long=True)
    _m = {s["date"]: sum(sg["minutes"] for sg in s["segments"]) for s in prop["sessions"]}
    check("with a pinned run AND a protected long run, both keep their minutes and only the "
          "free easy runs absorb the cut",
          _m.get(THU) == 40 and _m.get("2026-08-23") == 90 and sum(_m.values()) <= 160)
    check("the free easy runs were the ones scaled (or dropped) to fit",
          _m.get(TUE, 0) < 50 or TUE not in _m)

    # A pinned run alone OVER the cap must not be shrunk. The ceiling is breached and the
    # validator will say so — cutting what the athlete agreed is not the fix.
    prop = {"sessions": [{"date": THU, "sport": "Run", "name": "Agreed long run",
                          "pinned": True,
                          "segments": [{"minutes": 150, "zone": "easy"}]}]}
    s1p._clamp_runs_to_cap(prop, None, None, 5.3, run_min_cap=100)
    check("a pinned run over the cap is left alone and left in the week (reported, not cut)",
          len(prop["sessions"]) == 1
          and sum(sg["minutes"] for sg in prop["sessions"][0]["segments"]) == 150)
finally:
    s1p.pb.build_sessions = _orig_build


# --- 8) the reduced-target arithmetic ----------------------------------------------------
check("the proposer's target is the whole week minus the agreed load",
      aw.reduced_target(820, PINS) == 820 - 212)
check("a rest-day pin reduces nothing",
      aw.reduced_target(820, {FRI: PINS[FRI]}) == 820)
check("a pin with no load_target reduces nothing (never guess a day's cost)",
      aw.reduced_target(820, {THU: {"session": {"sport": "Ride", "minutes": 240}}}) == 820)
check("the reduction floors at 0 and never goes negative",
      aw.reduced_target(100, PINS) == 0)
check("no target stays no target", aw.reduced_target(None, PINS) is None
      and aw.reduced_target(0, PINS) == 0)

_pb_brief = {"weekly_tss_target": 820, "phase": "build"}
_p = s1p.proposer_brief(_pb_brief, aw.protected_dates(SLUG, WS, base=TMP), 608)
check("the PROPOSER's brief carries the reduced target", _p["weekly_tss_target"] == 608)
check("the whole-week brief is NOT mutated — the gates and the athlete's message read it",
      _pb_brief["weekly_tss_target"] == 820)
check("the proposer's brief names the agreed days", set(_p["agreed_days"]) ==
      {"2026-08-19", THU, FRI})
check("with nothing protected the brief is passed through untouched, so an empty "
      "agreed_days list is never serialised into the prompt",
      s1p.proposer_brief(_pb_brief, {}, 820) is _pb_brief)
check("the prompt clause is empty when nothing is protected", aw.brief_clause({}) == "")
_clause = aw.brief_clause(aw.protected_dates(SLUG, WS, base=TMP))
check("the prompt clause tells the proposer to plan NOTHING on the agreed days",
      "Propose NOTHING" in _clause and THU in _clause)
check("the prompt clause says the target already accounts for the agreed days, so the "
      "proposer does not add load to make up for what it cannot see",
      "already" in _clause.lower() and "agreed days" in _clause.lower())
check("the clause reaches the prompt text", '_agreed_clause' in _s1
      and _s1.index('brief.get("_agreed_clause")') > _s1.index("DATE GRID"))
check("the whole-week gates still read the whole-week target",
      "load_on_target = (target is None) or abs(load_pct_off) <= 12" in _s1
      and "load_pct_off = (round((built[\"total_tss\"] - target) / target * 100, 1)" in _s1)
check("the post-splice rebuild and audit use the WHOLE-week target",
      "built = close_to_target(args.athlete, proposal, target, brief)\n"
      "        blocking, advisory = audit_built(brief, built, target, proposal)" in _s1)


# --- 9) plan_builder: the pinned session's load, and push() ------------------------------
check("build_sessions carries `pinned` into both the sessions and the ICU events",
      '"pinned": pinned' in (BASE / "lib" / "plan_builder.py").read_text())

# build_sessions reads athlete config, a session log and (via plan_tools) ICU. Only the
# load short-circuit is under test here, so config, fuel and the validation tail are
# stubbed — what matters is the number that reaches built["sessions"].
_orig = {"cfg": pb._cfg, "fuel": pb._fuel_for}
pb._cfg = lambda slug: {"icu_athlete_id": "x", "icu_api_key": "y",
                        "nutrition_target_g_hr": 90}
pb._fuel_for = lambda slug, cfg: (80, 60)
class _Rep:
    violations, skipped, total_tss = [], [], 0


_orig_validate = pb.validate_week
pb.validate_week = lambda events, ws, **kw: _Rep()
try:
    out = pb.build_sessions(SLUG, {"sessions": [
        # A pinned session whose AGREED load (212) differs from anything the renderer
        # would derive from 240 min of endurance riding.
        {"date": THU, "sport": "Ride", "name": "Long ride 4h", "pinned": True,
         "load_target": 212, "minutes": 240,
         "segments": [{"minutes": 240, "zone": "endurance"}]},
        # The same session UNPINNED, to prove the short-circuit is what changed it.
        {"date": TUE, "sport": "Ride", "name": "Long ride 4h", "load_target": 212,
         "segments": [{"minutes": 240, "zone": "endurance"}]},
    ]})
    _b = {s["date"]: s for s in out["sessions"]}
    check("a pinned session's load is the AGREED figure from the record, not a re-derivation "
          "(otherwise the week's total disagrees with the calendar and the load gate judges "
          "a week nobody has)", _b[THU]["load_target"] == 212)
    check("an unpinned session still gets its load from the render, never from the proposal",
          _b[TUE]["load_target"] != 212)
    check("`pinned` reaches built['sessions']",
          _b[THU]["pinned"] is True and _b[TUE]["pinned"] is False)
    check("a pinned long ride gets no fuel note appended (it is never pushed, so the note "
          "would reach nobody)", "Fuel" not in _b[THU]["description_raw"]
          and "Fuel" in _b[TUE]["description_raw"])

    # A COARSE pin with no segments: the load still comes from the record rather than from
    # planned_session_tss's sport+duration guess.
    out2 = pb.build_sessions(SLUG, {"sessions": [
        {"date": THU, "sport": "Ride", "name": "Agreed ride", "pinned": True,
         "load_target": 150, "minutes": 120, "segments": []}]})
    check("a coarse pin with no segments still reports its agreed load and duration",
          out2["sessions"][0]["load_target"] == 150
          and out2["sessions"][0]["duration_min"] == 120)
finally:
    pb.validate_week = _orig_validate
    pb._cfg, pb._fuel_for = _orig["cfg"], _orig["fuel"]


# push(): a stub client records what was asked of it.
class _PushClient(_StubClient):
    pass


_built = {"week_start": WS, "sessions": [
    {"date": TUE, "sport": "Swim", "name": "CSS", "duration_min": 60, "load_target": 55,
     "description": "", "description_raw": "", "pinned": False},
    {"date": THU, "sport": "Ride", "name": "Long ride 4h", "duration_min": 240,
     "load_target": 212, "description": "", "description_raw": "", "pinned": True},
]}
_events = [{"id": "old-tue", "start_date_local": f"{TUE}T00:00:00", "category": "WORKOUT"},
           {"id": "old-thu", "start_date_local": f"{THU}T00:00:00", "category": "WORKOUT"},
           {"id": "old-fri", "start_date_local": f"{FRI}T00:00:00", "category": "WORKOUT"},
           {"id": "a-race", "start_date_local": f"{FRI}T00:00:00", "category": "RACE"}]

_stub = _PushClient(_events)
_orig_cfg = pb._cfg
pb._cfg = lambda slug: {"icu_athlete_id": "x", "icu_api_key": "y"}
_orig_pins = pb.pinned_dates_for
pb.pinned_dates_for = lambda slug, built: {THU, FRI}
import icu_api as _icu_api          # noqa: E402
_orig_client = _icu_api.IcuClient
_icu_api.IcuClient = lambda aid, key: _stub
try:
    res = pb.push(SLUG, copy.deepcopy(_built))
    check("push() pushes only the days the generator owns",
          [a for a in _stub.asked if a and a[0] == "push"] == [("push", TUE)])
    check("push() deletes the old event on a day it owns", "old-tue" in res["deleted"])
    check("push() does NOT delete the old event on a PINNED day (it is the agreed session)",
          "old-thu" not in res["deleted"])
    check("push() does not delete on a pinned EMPTY day either — 'nothing on Friday' is an "
          "agreement about the day, and a rest-day pin has no session to derive a flag from",
          "old-fri" not in res["deleted"])
    check("push() names the days it left alone in its result",
          res["agreed_days_left_alone"] == sorted({THU, FRI}))
    check("a non-WORKOUT event is still never touched", "a-race" not in res["deleted"])

finally:
    pb.pinned_dates_for = _orig_pins
    pb._cfg = _orig_cfg
    _icu_api.IcuClient = _orig_client

# THE PRE-DELETE GUARD. push() derives its pinned set once, so the guard can only be
# reached by a future edit that breaks the filter — which is exactly what it is for. It is
# therefore tested as the function it is, plus a source check that push() calls it before
# every delete and that it raises rather than asserting (python -O strips asserts).
_raised = None
try:
    pb.assert_deletable(SLUG, "old-thu", f"{THU}T00:00:00", {THU})
except RuntimeError as e:
    _raised = str(e)
check("assert_deletable RAISES for an event on a pinned date", bool(_raised))
check("the message names the event, the date and that the day is agreed",
      "old-thu" in _raised and THU in _raised and "AGREED" in _raised)
check("assert_deletable passes an event on a day the generator owns",
      pb.assert_deletable(SLUG, "old-tue", f"{TUE}T00:00:00", {THU}) is None)
check("assert_deletable passes when nothing is pinned",
      pb.assert_deletable(SLUG, "x", THU, set()) is None
      and pb.assert_deletable(SLUG, "x", THU, None) is None)
_pb_src = (BASE / "lib" / "plan_builder.py").read_text()
check("the guard is a raise, not an assert (python -O must not be able to strip it)",
      "raise RuntimeError(" in _pb_src.split("def assert_deletable")[1].split("def push")[0]
      and "assert " not in _pb_src.split("def assert_deletable")[1].split("def push")[0])
check("push() calls the guard inside the delete loop, before the delete",
      _pb_src.index("assert_deletable(slug, eid, edate, pinned)")
      < _pb_src.index("c.delete_workout(eid)"))

# pinned_dates_for itself: pins only, never the availability union. Pointed at the tmpdir
# store, since the real one for this slug does not exist.
_orig_pb_base = pb.BASE
try:
    import agreed_week as _aw2
    _orig_aw_base = _aw2.BASE
    _aw2.BASE = Path(TMP)
    got = pb.pinned_dates_for(SLUG, {"week_start": WS, "sessions": []})
    check("pinned_dates_for returns the pins, including the rest day, with no sessions in "
          "the build at all", got == {THU, FRI})
    check("pinned_dates_for EXCLUDES the declared-unavailable day — that day's stale event "
          "must still be deleted", "2026-08-19" not in got)
finally:
    _aw2.BASE = _orig_aw_base
    pb.BASE = _orig_pb_base


# --- 10) everything touched still parses -------------------------------------------------
for rel in ("lib/agreed_week.py", "lib/icu_fetch.py", "lib/plan_builder.py",
            "scripts/stage1-plan.py", "scripts/daily-prescription.py",
            "scripts/test_agreed_week.py"):
    p = BASE / rel
    try:
        ast.parse(p.read_text(), filename=str(p))
        ok = True
    except SyntaxError as e:
        ok = False
        print(f"     {rel}: {e}")
    check(f"{rel} parses", ok)


if FAILED:
    print(f"\n{len(FAILED)} FAILED")
    for f in FAILED:
        print("  -", f)
    sys.exit(1)
print("\nall checks passed")
