#!/usr/bin/env python3
"""The agreed week — DAY PINS the generators must not touch.

WHAT PROBLEM THIS SOLVES. Until now the week an athlete and the coach settled in chat
existed in exactly one place: the Intervals.icu calendar, with nothing anywhere recording
that those events were AGREED rather than generated. The Sunday build (stage1-plan
--push -> plan_builder.push) deletes every planned WORKOUT in the week it is planning and
re-pushes its own, so it could not tell the difference and treated the whole week as its
own. An agreed Thursday long ride vanished at 18:00 on Sunday and nobody could say why.

WHAT A PIN MEANS. One thing, and it is a statement about a DATE, not about a session:

    no generator may create, delete, move or resize an event on that date.

Whatever is on the calendar for that date - INCLUDING NOTHING - is what the athlete
agreed to. "Nothing on Friday" is therefore expressible as a pin on an empty date and
needs no special case.

WHY DAYS AND NOT SESSIONS. ICU's write unit is an event on a date; the athlete's own
language is per-day ("Thursday is the long ride", "nothing Friday"); and per-session
pinning would need stable session IDs, which cannot exist here because
plan_builder.push deletes and re-creates every event each week by design, so ICU ids
churn every Sunday. lib/day_overrides reached the same day+sport conclusion for the same
reason.

WHY THE RECORD STORES `segments` WHEN THE CALENDAR IS THE THING THE ATHLETE SEES. The
first instinct - store only the pin and read the session back off the calendar - does not
work, and this is the one place the design is forced. ICU events carry a RENDERED
description and a load, not segments, and there is no parser in the reverse direction. A
segment-less pinned session breaks the validator two ways:

  - plan_builder.build_sessions falls to its no-segments branch and RE-DERIVES TSS from
    sport + duration, which disagrees with the load already on the calendar, so the
    week's total (and the load gate that reads it) judges a fiction; and
  - stage1-plan's _overall_z3plus / _overall_high / _zone_by_sport all iterate
    `s["segments"]`, so a pinned 4h Z2 ride contributes ZERO minutes and the week reads
    far more quality-dense than it is. Worse, validate_plan.zone_band_deviations skips a
    sport under min_minutes=180, so missing minutes can silently DISARM the distribution
    check rather than merely skew it.

So the record carries sport / name / minutes / load_target / segments as agreed. On read,
load_target comes from the record (plan_builder short-circuits planned_session_tss for a
pinned session) so the validated total matches the calendar exactly.

COARSE PINS. When the writer had no segments to give us (see lib/icu_fetch.py) the record
stores a single segment at the sport's easy zone and is marked `"coarse": true`. A coarse
pin still PROTECTS the day, which is the point; its zone accounting is approximate, and
`coarse` exists so an operator can see which pins are which. A coarse pin with minutes 0
protects the day but contributes nothing at all to the accounting above - that is the
min_minutes hazard, so it is logged loudly at pin time.

TWO ACCESSORS, AND THE DIFFERENCE IS LOAD-BEARING.

  pinned_dates()    - PINS ONLY. "this was agreed; leave it exactly as it is." This is
                      what push() consults: skip on the push list AND on the delete list.
  protected_dates() - pins UNION weekly_availability's `unavailable_days` for the week.
                      "do not PLAN anything here." This is what the proposer is told.

They are deliberately not the same set. `unavailable_days` says the athlete CANNOT train
that day, which is the opposite of "keep what is there": if a stale event from a previous
build sits on a declared-unavailable Friday it must still be deleted, so an unavailable
day must never reach push()'s delete-skip list. Only pins do.

STORE. athletes/<slug>/agreed-plan.json, dated records keyed by Monday `week_start`,
atomic write, a _KEEP window and one ops_log line per write - the same mechanics as
lib/weekly_availability, in a SEPARATE FILE on purpose. weekly_availability.record()
replaces a week's declaration wholesale, which is why every capture has to hand-carry
prior_hours/prior_cons/prior_excl forward and why any capture that forgets one silently
drops it. Adding a fourth thing to carry forward would add a fourth way to lose it.

Pins EXPIRE WITH THEIR WEEK: records are consulted by week_start only, so a pin can never
leak into a later week. Explicit release is `release()` (CLI below, and the release button
on the replan card). Without an escape hatch a pin becomes a trap the athlete cannot get
out of, which is a new rage class rather than a fix for the old one.

CLI:
  python3 lib/agreed_week.py --slug jamie --week-start 2026-08-17            # show
  python3 lib/agreed_week.py --slug jamie --week-start 2026-08-17 --release  # release all
  python3 lib/agreed_week.py --slug jamie --pin 2026-08-20 --why "agreed in chat"
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import sys
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent          # ClaudeCoach/
FILENAME = "agreed-plan.json"

# Same window and same reasoning as weekly_availability._KEEP: three weeks would do (the
# build writes next week's before it begins), six is a cheap margin that also leaves a
# short readable history for "why was that day left alone?".
_KEEP = 6

# The easy zone each sport's coarse fallback segment is written at. These are
# TRAINING-SYSTEM names from primitives.planned_tss._ZONE_IF, not TID band labels -
# there are two zone vocabularies in this codebase and only one of them is safe to
# write here (a band label unknown to a table falls through to the sport default IF,
# which is the 9 Aug 2026 outage). segment_if accepts both, but these names are in the
# primary table for every sport, so they cannot drift onto a default.
_COARSE_ZONE = {"ride": "endurance", "bike": "endurance", "virtualride": "endurance",
                "gravelride": "endurance", "brick": "endurance",
                "run": "easy", "trailrun": "easy", "virtualrun": "easy",
                "swim": "easy", "openwaterswim": "easy"}


# ---------------------------------------------------------------------------
# store
# ---------------------------------------------------------------------------

def path_for(slug: str, base: Path | str | None = None) -> Path:
    return Path(base or BASE) / "athletes" / slug / FILENAME


def _monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


def _as_date(d: date | str) -> date:
    return date.fromisoformat(str(d)) if not isinstance(d, date) else d


def load_raw(slug: str, base: Path | str | None = None) -> dict:
    """The file as written, or {} when absent/unreadable. NEVER raises: an unparseable
    agreed-plan file must degrade to "nothing is pinned", not kill the Sunday build.

    Degrading to "nothing pinned" is the right direction only because the alternative is
    no week at all for the athlete. It is also why every pin write is logged: a file that
    stops parsing is invisible in behaviour and visible only in the log."""
    p = path_for(slug, base)
    try:
        return json.loads(p.read_text()) if p.exists() else {}
    except Exception:
        return {}


def _weeks(raw: dict) -> list[dict]:
    w = raw.get("weeks") if isinstance(raw, dict) else None
    return [x for x in w if isinstance(x, dict)] if isinstance(w, list) else []


def for_week(slug: str, week_start: date | str | None,
             base: Path | str | None = None) -> dict | None:
    """The whole week record for the week beginning `week_start`, or None.

    A None `week_start` resolves to None by design, mirroring
    weekly_availability.for_week: one real week's agreement must never be stretched
    across a projection of many."""
    if week_start is None:
        return None
    try:
        ws = _monday(_as_date(week_start))
    except Exception:
        return None
    for w in _weeks(load_raw(slug, base)):
        try:
            if _monday(_as_date(str(w.get("week_start")))) == ws:
                return w
        except Exception:
            continue
    return None


def pins_for_week(slug: str, week_start: date | str | None,
                  base: Path | str | None = None) -> dict:
    """{date: pin record} for that week. Pins only, never the availability union.

    Each record is {"why", "at", "by", "session": {...} | None}. A None session is a
    REST-DAY pin: the day is protected and there is nothing on it."""
    rec = for_week(slug, week_start, base) or {}
    pins = rec.get("pins")
    if not isinstance(pins, dict):
        return {}
    ws = _monday(_as_date(week_start))
    out = {}
    for d, p in pins.items():
        if not isinstance(p, dict):
            continue
        try:
            dd = _as_date(str(d))
        except Exception:
            continue
        # A pin can only ever apply to the week it is filed under. A record hand-edited
        # to carry a date from another week would otherwise protect a day in a week
        # nobody agreed anything about.
        if _monday(dd) != ws:
            continue
        out[dd.isoformat()] = p
    return dict(sorted(out.items()))


def pinned_dates(slug: str, week_start: date | str | None,
                 base: Path | str | None = None) -> dict:
    """{date: why} for PINS ONLY — the set push() must not write to and must not delete
    from. See the module docstring for why this is not protected_dates()."""
    return {d: str(p.get("why") or "agreed") for d, p in
            pins_for_week(slug, week_start, base).items()}


def pinned_dates_span(slug: str, start: date | str, end: date | str,
                      base: Path | str | None = None) -> dict:
    """{date: why} for every pin between `start` and `end` inclusive, across weeks.

    For the delete guard, which knows an event id and a rough window but not which week
    the event belongs to. Pins only — see pinned_dates()."""
    a, b = _as_date(start), _as_date(end)
    out = {}
    ws = _monday(a)
    while ws <= b:
        for d, why in pinned_dates(slug, ws, base).items():
            if a.isoformat() <= d <= b.isoformat():
                out[d] = why
        ws += timedelta(days=7)
    return dict(sorted(out.items()))


def protected_dates(slug: str, week_start: date | str | None,
                    base: Path | str | None = None,
                    availability_base: Path | str | None = None) -> dict:
    """{date: why} the PROPOSER must not plan on: pins UNION this week's declared
    `unavailable_days`.

    The availability half is REUSE, not invention: the day-shape capture already writes
    unavailable_days live, so declared rest days need no new parser here. It is included
    for the proposer and EXCLUDED from push()'s delete-skip (module docstring)."""
    out = dict(pinned_dates(slug, week_start, base))
    if week_start is None:
        return out
    try:
        import weekly_availability
        ws = _monday(_as_date(week_start))
        shape = weekly_availability.day_shape(
            slug, ws, availability_base if availability_base is not None else base) or {}
        for tok in (shape.get("unavailable_days") or []):
            canon = weekly_availability._canon_day(str(tok))
            if not canon:
                continue
            for i in range(7):
                d = ws + timedelta(days=i)
                if d.strftime("%a") == canon:
                    out.setdefault(d.isoformat(), "you said you are unavailable")
    except Exception:
        pass
    return dict(sorted(out.items()))


def _atomic_write(p: Path, payload: dict) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=".aw-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(payload, f, indent=1, ensure_ascii=False)
            f.write("\n")
        os.replace(tmp, p)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _trim(weeks: list[dict], today: date | None = None) -> list[dict]:
    """Bound the store WITHOUT ever discarding a week that has not happened yet.

    The obvious `weeks[-_KEEP:]` — which is what weekly_availability does — is WRONG here,
    and the difference is a lost week. A declaration is written for the imminent week, so
    "keep the six latest" and "keep the six that matter" coincide. A PIN can be filed
    against any date the chat model touches: agree a race-week session eight weeks out and
    w/c next Monday becomes the oldest of seven and gets trimmed, so the Sunday build hours
    later sees nothing pinned and rebuilds the day the athlete just agreed.

    So: every week from this Monday onward is kept (all of them are live), plus at most
    _KEEP past weeks for the short readable history. Growth is bounded by how many future
    weeks anyone actually pins, and each of those is load-bearing."""
    t = _monday(today or date.today())
    ordered = sorted(weeks, key=lambda w: str(w.get("week_start")))

    def _ws(w):
        try:
            return _monday(_as_date(str(w.get("week_start"))))
        except Exception:
            return None
    # A week with an unparseable week_start is treated as past: it cannot protect a day
    # (pins_for_week resolves nothing from it) so it must not hold a live week out.
    past = [w for w in ordered if not _ws(w) or _ws(w) < t]
    live = [w for w in ordered if _ws(w) and _ws(w) >= t]
    return past[-_KEEP:] + live


def coarse_session(sport: str, name: str = "", minutes=None, load_target=None) -> dict:
    """The single-segment fallback for a writer that gave us no segments.

    Marked coarse so the approximation is visible. minutes may legitimately be 0 (a
    push_workout payload carries no duration unless the caller passes moving_time) — the
    day is still protected, but a 0-minute pin contributes NOTHING to the zone
    accounting, so pin() logs that case loudly."""
    mins = int(minutes or 0)
    zone = _COARSE_ZONE.get((sport or "").lower().strip())
    segs = [{"minutes": mins, "zone": zone}] if (mins > 0 and zone) else []
    return {"sport": sport, "name": name or "", "minutes": mins,
            "load_target": (int(load_target) if load_target else None),
            "coarse": True, "segments": segs}


def session_record(sport: str, name: str = "", minutes=None, load_target=None,
                   segments=None) -> dict:
    """The `session` half of a pin record. Segments present -> an exact pin; absent ->
    the coarse fallback above."""
    segs = [s for s in (segments or []) if isinstance(s, dict)]
    if not segs:
        return coarse_session(sport, name, minutes, load_target)
    mins = int(minutes or 0) or int(sum((s.get("minutes") or 0) for s in segs))
    return {"sport": sport, "name": name or "", "minutes": mins,
            "load_target": (int(load_target) if load_target else None),
            "coarse": False, "segments": copy.deepcopy(segs)}


def pin(slug: str, day: date | str, *, why: str, by: str = "chat",
        session: dict | None = None, base: Path | str | None = None) -> dict:
    """Pin one DATE. Returns the stored pin record.

    Idempotent by date: re-pinning replaces that date's record (the newest agreement is
    the agreement) and never touches any other date. A pin for a date lands in the record
    for THAT date's Monday, so it can only ever restrict the week it names."""
    d = _as_date(day)
    ws = _monday(d)
    rec = {"why": str(why or "agreed").strip() or "agreed",
           "at": datetime.now().isoformat(timespec="seconds"),
           "by": str(by or "chat"),
           "session": copy.deepcopy(session) if session else None}

    raw = load_raw(slug, base)
    weeks = [w for w in _weeks(raw)
             if str(w.get("week_start")) != ws.isoformat()]
    prior = next((w for w in _weeks(raw)
                  if str(w.get("week_start")) == ws.isoformat()), None)
    pins = dict((prior or {}).get("pins") or {})
    replaced = pins.get(d.isoformat())
    pins[d.isoformat()] = rec
    weeks.append({"week_start": ws.isoformat(), "pins": dict(sorted(pins.items())),
                  # A pin AFTER a release re-opens the week: the athlete released it and
                  # then agreed something new, which is a new agreement, not a revoked one.
                  "released_at": None})
    out = {k: v for k, v in (raw.items() if isinstance(raw, dict) else []) if k != "weeks"}
    out["weeks"] = _trim(weeks)
    _atomic_write(path_for(slug, base), out)
    _audit(slug, f"PINNED {d.isoformat()} (w/c {ws.isoformat()}) by {rec['by']}: "
                 f"{rec['why']}"
                 + (f" — {_pin_summary(rec)}" if rec.get("session") else " — rest day")
                 + (f" REPLACED a pin from {replaced.get('at')}" if replaced else ""),
           base)
    s = rec.get("session") or {}
    if s and (s.get("coarse") or not s.get("segments")):
        blind = ("" if s.get("minutes") else
                 " and NO DURATION either, so it contributes NOTHING to the zone and "
                 "distribution accounting (validate_plan skips a sport under 180 "
                 "minutes, so missing minutes can DISARM that check rather than skew it)")
        _audit(slug, f"COARSE PIN {d.isoformat()}: no segments were supplied{blind} — "
                     f"zone accounting for this day is approximate", base)
    return rec


def release(slug: str, week_start: date | str, *, dates=None, by: str = "",
            base: Path | str | None = None) -> list[str]:
    """Release pins for a week. `dates=None` releases ALL of them. Returns the dates
    released (empty when there was nothing pinned).

    The week record is KEPT with `released_at` set rather than deleted, so "who dropped
    what we agreed, and when" is answerable afterwards — the question that was
    unanswerable on 10 Aug."""
    ws = _monday(_as_date(week_start))
    raw = load_raw(slug, base)
    prior = next((w for w in _weeks(raw)
                  if str(w.get("week_start")) == ws.isoformat()), None)
    pins = dict((prior or {}).get("pins") or {})
    if not pins:
        return []
    want = ({_as_date(d).isoformat() for d in dates} if dates is not None
            else set(pins))
    gone = sorted(d for d in pins if d in want)
    for d in gone:
        pins.pop(d, None)
    weeks = [w for w in _weeks(raw) if str(w.get("week_start")) != ws.isoformat()]
    weeks.append({"week_start": ws.isoformat(), "pins": dict(sorted(pins.items())),
                  "released_at": datetime.now().isoformat(timespec="seconds")})
    out = {k: v for k, v in (raw.items() if isinstance(raw, dict) else []) if k != "weeks"}
    out["weeks"] = _trim(weeks)
    _atomic_write(path_for(slug, base), out)
    _audit(slug, f"RELEASED {len(gone)} pin(s) for w/c {ws.isoformat()} "
                 f"({', '.join(gone) or 'none'}){f' by {by}' if by else ''} — those days "
                 f"are now the generator's to rebuild", base)
    return gone


def _pin_summary(rec: dict) -> str:
    s = rec.get("session") or {}
    if not s:
        return "rest day"
    bits = [str(s.get("sport") or "?"), str(s.get("name") or "")]
    if s.get("minutes"):
        bits.append(f"{int(s['minutes'])}min")
    if s.get("load_target"):
        bits.append(f"{int(s['load_target'])} Load")
    if s.get("coarse"):
        bits.append("COARSE")
    return " ".join(b for b in bits if b)


def _audit(slug: str, detail: str, base: Path | str | None) -> None:
    """One ops-alerts line per write. Best-effort, never raises.

    Skipped when `base` is passed, which only tests do, so the suite never writes to the
    operator's alert log (same contract as weekly_availability._audit). The 10 Aug
    day-shape incident is the reason every write here is logged: a store whose writes are
    invisible cannot be diagnosed after the fact, and this one can silence the Sunday
    build for a whole day."""
    if base is not None:
        return
    try:
        import ops_log
        ops_log.record_run("agreed-week", athlete=slug, ok=True, detail=detail)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# the honoured build — pure helpers over pin records
# ---------------------------------------------------------------------------
# These live HERE and not in scripts/stage1-plan.py for one blunt reason: `import
# stage1-plan` is a syntax error, so anything defined there can only be tested through
# importlib against a module whose import chain reaches the LLM caller, the session
# library and the primitives. They are pure functions over pin records; this is where
# they belong and where the tests can drive them directly.

def pinned_load(pins: dict) -> int:
    """Total agreed Load across a week's pins. Rest-day pins and pins whose session
    carries no load_target contribute 0 — a day we cannot cost must not be allowed to
    reduce the proposer's target by a guess."""
    tot = 0
    for p in (pins or {}).values():
        s = (p or {}).get("session") or {}
        try:
            tot += int(s.get("load_target") or 0)
        except (TypeError, ValueError):
            continue
    return tot


def reduced_target(target, pins: dict):
    """The number the PROPOSER is aimed at: the whole-week target minus the load already
    sitting on the pinned days.

    WHOLE-WEEK GATES MUST NOT USE THIS. stage1-plan's load_on_target +/-12% gate and
    close_to_target's final tolerance are computed on the WHOLE week, which after the
    splice includes the pinned days. Only the proposer's brief takes this reduced number,
    because the proposer is being asked to fill the days it OWNS. Getting that backwards
    produces a systematically light or heavy week (design section 10).

    Floored at 0, never None-propagating: a target of None (no target this week) stays
    None."""
    if not target:
        return target
    return max(0, int(round(target - pinned_load(pins))))


def pin_sessions(pins: dict) -> list[dict]:
    """The pinned days as PROPOSAL sessions, flagged `pinned`, in date order.

    Rest-day pins yield nothing — there is no session to validate, and the day is
    protected by pinned_dates() at push time, not by a session here."""
    out = []
    for d, p in sorted((pins or {}).items()):
        s = (p or {}).get("session") or {}
        if not s or not s.get("sport"):
            continue
        segs = copy.deepcopy(s.get("segments") or [])
        mins = int(s.get("minutes") or 0) or int(sum((sg.get("minutes") or 0) for sg in segs))
        out.append({
            "date": d, "sport": s.get("sport"), "name": s.get("name") or "",
            "notes": f"Agreed with you ({p.get('why') or 'agreed in chat'}) — "
                     f"already on your calendar, left exactly as it is.",
            "segments": segs, "minutes": mins,
            # load_target is the figure ON THE CALENDAR as agreed. plan_builder
            # short-circuits its TSS derivation to this for a pinned session so the
            # validated week total matches the calendar exactly.
            "load_target": (int(s["load_target"]) if s.get("load_target") else None),
            "pinned": True, "coarse": bool(s.get("coarse")),
        })
    return out


def splice_pinned(proposal: dict, pins: dict) -> tuple[dict, list[str]]:
    """Return (proposal with the pinned days replaced by their pin records, notes).

    Any proposed session landing on a PINNED date is DROPPED and the pin's own session
    spliced in. Rest-day pins drop whatever was proposed and add nothing.

    WHERE THIS IS CALLED MATTERS. Immediately after the winning attempt is picked and
    BEFORE quality_inject, so that injection, close_to_target and audit_built all see the
    WHOLE week. quality_inject exists to pull per-sport per-zone distribution toward
    target; run it on a proposal that excludes the pinned days and it sizes quality
    against a partial week and over-injects into the days it owns. Splicing first also
    means the pinned sessions count toward TSS, ramp, run volume and distribution — or
    the validator judges a fiction (design section 4).

    Idempotent, so it is safe to call again after quality_inject to revert any injection
    that reached into a pinned day.

    A dropped proposed session is NOTED, not silently swallowed: it means the proposer
    ignored the prompt clause telling it those days are taken, and that is worth seeing
    in the attempts log rather than discovering in a month."""
    out = copy.deepcopy(proposal or {})
    pinned = set((pins or {}).keys())
    kept, dropped = [], []
    for s in (out.get("sessions") or []):
        if str(s.get("date") or "")[:10] in pinned:
            dropped.append(f"{s.get('date')} {s.get('sport')} '{s.get('name')}'")
            continue
        kept.append(s)
    spliced = pin_sessions(pins)
    out["sessions"] = sorted(kept + spliced, key=lambda s: str(s.get("date") or ""))
    notes = []
    if dropped:
        notes.append("proposer planned on agreed days (dropped): " + "; ".join(dropped))
    if spliced:
        notes.append("spliced " + str(len(spliced)) + " agreed session(s): "
                     + "; ".join(f"{s['date']} {s['sport']}" for s in spliced))
    rest = [d for d in sorted(pinned) if d not in {s["date"] for s in spliced}]
    if rest:
        notes.append("agreed rest/empty day(s) left untouched: " + ", ".join(rest))
    return out, notes


def brief_clause(protected: dict) -> str:
    """The prompt clause naming the agreed days. Empty string when nothing is protected,
    so an empty list is never serialised into the prompt (same rule stage1-plan already
    applies to declared_hours: a config-shaped key one paraphrase away from the athlete).
    """
    if not protected:
        return ""
    days = "\n".join(f"  {d} ({date.fromisoformat(d).strftime('%a')}) — {why}"
                     for d, why in sorted(protected.items()))
    return ("\nAGREED DAYS — ALREADY SETTLED WITH THE ATHLETE:\n" + days
            + "\nThese dates are already agreed with the athlete and will be filled by "
              "code. Propose NOTHING on them — not a session, not a rest note. Plan the "
              "remaining days only. The WEEKLY TSS TARGET you have been given ALREADY "
              "accounts for what is on the agreed days, so aim at it with the days you "
              "own and do not add load to make up for the days you cannot see.\n")


def delete_refusal(event_date, pinned: dict) -> str | None:
    """The refusal message when deleting an event on `event_date` would break a pin, else
    None. Pure, so the guard can be tested without the network.

    `event_date` None means "we could not establish which date this event is on". That
    FAILS CLOSED while anything is pinned: a readable refusal is recoverable in one turn,
    a silently destroyed agreed session is not. With nothing pinned there is nothing to
    protect and the delete goes through unchanged."""
    if not pinned:
        return None
    if not event_date:
        return ("ERROR: refusing to delete — this athlete has agreed (pinned) days this "
                f"week ({', '.join(sorted(pinned))}) and I could not establish which "
                "date this event is on. Release the pin first if you really mean to "
                "remove it.")
    d = str(event_date)[:10]
    if d not in pinned:
        return None
    return (f"ERROR: refusing to delete the event on {d} — that day is AGREED with the "
            f"athlete ({pinned[d]}). Pinned days are not the generator's or the coach's "
            f"to remove. If the athlete has changed their mind, release the pin "
            f"(lib/agreed_week.py --slug <slug> --week-start <Monday> --release) and say "
            f"so, then write.")


# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--slug", required=True)
    ap.add_argument("--week-start", help="Monday YYYY-MM-DD (default: this Monday)")
    ap.add_argument("--release", action="store_true", help="release the week's pins")
    ap.add_argument("--dates", help="comma-separated dates to release (default: all)")
    ap.add_argument("--pin", help="pin this date (YYYY-MM-DD)")
    ap.add_argument("--why", default="agreed with the athlete")
    ap.add_argument("--by", default="cli")
    args = ap.parse_args()
    if args.pin:
        rec = pin(args.slug, args.pin, why=args.why, by=args.by)
        print(json.dumps({"pinned": args.pin, "record": rec}, indent=1))
        return
    ws = _monday(_as_date(args.week_start) if args.week_start else date.today())
    if args.release:
        gone = release(args.slug, ws,
                       dates=[d.strip() for d in args.dates.split(",")] if args.dates else None,
                       by=args.by)
        print(json.dumps({"released": gone, "week_start": ws.isoformat()}, indent=1))
        return
    print(json.dumps({"week_start": ws.isoformat(),
                      "pins": pins_for_week(args.slug, ws),
                      "protected": protected_dates(args.slug, ws),
                      "pinned_load": pinned_load(pins_for_week(args.slug, ws))},
                     indent=1, ensure_ascii=False))


if __name__ == "__main__":
    sys.path.insert(0, str(BASE / "lib"))
    main()
