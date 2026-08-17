#!/usr/bin/env python3
"""rest_day.py — reserve the weekly rest day DURING construction.

WHY THIS EXISTS. `validate_week` hard-fails a seven-day week (`no_rest_day`), and the
Stage-1 prompt instructs the proposer to leave a day empty, but instruction is not
constraint: every attempt can come back 7/7, in which case the picker ranks among
seven-day weeks and the best of them is the one that gets audited. Reserving the day
here makes a 7/7 proposal unreachable instead of merely rejected.

ONE POLICY SOURCE. The required count is `validate_plan.REST_DAYS_MIN`, the same name
`validate_week`'s `rest_days_min` default points at, and the LOADED-DAY test is the
validator's own: a day is loaded when its planned load is above zero, not when it
carries an entry. So zero-load mobility never costs a rest day here either, and this
module cannot come to a different verdict about the finished week than the check that
grades it.

WHAT IT WILL NOT DO, in order of how much damage the alternative causes:
  * A PINNED day is never chosen. A pin is the athlete's own agreement, so it is not
    the generator's to empty — including a rest-day pin (`session: None`), which needs
    no choosing because a day with nothing on it is already rest.
  * A WAIVED week is left alone. The rule's escape hatch is a stated reason, and
    `validate_week` downgrades to a soft `no_rest_day_waived` when it gets one;
    reserving anyway would overrule a decision the athlete recorded.
  * KEY SESSIONS lose last. Days are ranked by `is_key_fn` first and planned load
    second, so a protected long ride is only emptied when nothing cheaper exists.
  * The freed load is NOT reallocated here. `close_to_target` already redistributes to
    non-pinned easy endurance inside the per-sport caps, and a week that lands under
    its floor surfaces as a hard `weekly_tss_floor`. A second reallocator would be a
    parallel copy of apportionment logic that could disagree with the first.

It never ADDS load and never touches a pinned session, so the worst case is a lighter
week that is reported light — not a silent one.
"""
from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE / "ironman-analysis"))

from primitives.validate_plan import REST_DAYS_MIN  # noqa: E402


def _day(s) -> str:
    return str(s.get("date") or "")[:10]


def week_dates(week_start: date | str) -> list:
    ws = date.fromisoformat(str(week_start)[:10]) if not isinstance(week_start, date) \
        else week_start
    return [(ws + timedelta(days=i)).isoformat() for i in range(7)]


def loaded_days(built: dict, week: list | None = None) -> dict:
    """{date: total planned load} for the days carrying load, validator's predicate.

    Summed per DAY, not per session: a day holding a bike and a run is one loaded day
    and reserving it costs both, which is the arithmetic the choice has to be made on.

    `week` CONFINES it to those seven dates, exactly as validate_week confines itself to
    [week_start, week_start+6]. Without that the arithmetic breaks rather than merely
    drifting: a proposal carrying an eighth date (the Stage-1 model writes the dates, and
    nothing on this path checks them against the grid) gives `7 - 8` loaded days, a
    negative rest count, and two days reserved to fix a week that only needed one.
    """
    out: dict[str, float] = {}
    for b in (built.get("sessions") or []):
        d = _day(b)
        if not d or (week is not None and d not in week):
            continue
        try:
            load = float(b.get("load_target") or 0)
        except (TypeError, ValueError):
            load = 0.0
        out[d] = out.get(d, 0.0) + load
    return {d: v for d, v in out.items() if v > 0}


def reserve(proposal: dict, built: dict, pins: dict | None = None, *,
            week_start: date | str | None = None,
            rest_days_min: int = REST_DAYS_MIN, waiver: str | None = None,
            is_key_fn=None) -> tuple[dict, list, list]:
    """Clear the lowest-value unpinned day(s) so the week carries its rest days.

    Returns `(proposal, reserved_dates, notes)`. `reserved_dates` non-empty means the
    proposal was changed and the caller must rebuild it; `notes` is prose for the run
    output and the advisories, and can be non-empty on an unchanged proposal — that is
    the case worth having, because "no rest day could be reserved" is exactly what the
    athlete needs told rather than resolved by picking something anyway.

    `built` must be the build of THIS `proposal` (they pair by index, as elsewhere on
    this path), because the load a day carries is only known after the render.
    `week_start` confines the arithmetic to one week; omitting it judges whatever dates
    the sessions carry, which is only safe when they are known to be one week's worth.
    `is_key_fn` takes a proposal session and returns True for a protected session.
    """
    sessions = proposal.get("sessions") or []
    built_sessions = built.get("sessions") or []
    if rest_days_min <= 0 or not sessions:
        return proposal, [], []
    if len(sessions) != len(built_sessions):
        # The index pairing below is the only way to know what a proposal session COSTS,
        # and a build that no longer lines up would silently attribute one session's load
        # to another — a wrong day chosen for reasons nothing in the output would explain.
        # A visible no-op beats that: the validator still hard-fails a 7/7 week.
        return proposal, [], [
            f"rest day NOT reserved: the built week has {len(built_sessions)} sessions "
            f"against the proposal's {len(sessions)}, so per-day load cannot be "
            f"attributed. Rebuild before reserving"]

    week = week_dates(week_start) if week_start else None
    day_load = loaded_days(built, week)
    days_in_week = len(week) if week else 7
    shortfall = rest_days_min - (days_in_week - len(day_load))
    if shortfall <= 0:
        return proposal, [], []

    if (waiver or "").strip():
        # Recorded, not waved through: say which week is training through and why, so a
        # waiver shows up in the run output rather than only as a downgraded violation.
        return proposal, [], [
            f"{len(day_load)} of {days_in_week} days carry load and no rest day was "
            f"reserved — TRAINING THROUGH by recorded reason: "
            f"{str(waiver).strip()[:200]}"]

    pinned = {str(d)[:10] for d in (pins or {})}
    # Pinned by the RECORD and pinned in the proposal are both honoured. The record is
    # the authority, but a session already flagged mid-build must not be emptied either.
    for s in sessions:
        if s.get("pinned"):
            pinned.add(_day(s))

    key_days = set()
    if is_key_fn:
        for s in sessions:
            try:
                if is_key_fn(s):
                    key_days.add(_day(s))
            except Exception:
                continue

    # Cheapest first: a day holding no protected session, then the least load. The date
    # breaks ties so the choice is deterministic rather than dependent on dict order.
    candidates = sorted((d for d in day_load if d not in pinned),
                        key=lambda d: (d in key_days, day_load[d], d))
    chosen = candidates[:shortfall]
    if not chosen:
        blocked = sorted(d for d in day_load if d in pinned)
        return proposal, [], [
            f"could NOT reserve a rest day: all {len(day_load)} loaded days are agreed "
            f"with the athlete ({', '.join(blocked)}) and an agreed day is not the "
            f"generator's to clear. The week needs {rest_days_min} rest day — that is a "
            f"conversation to have, not a session to drop unasked"]

    # Only the LOADED sessions go. A zero-load mobility entry may sit on a rest day by
    # the rule's own wording, so leaving it is honouring the rule, not an omission.
    notes, freed = [], 0.0
    for s, b in list(zip(sessions, built.get("sessions") or [])):
        if _day(s) not in chosen:
            continue
        try:
            load = float(b.get("load_target") or 0)
        except (TypeError, ValueError):
            load = 0.0
        if load <= 0:
            continue
        proposal["sessions"].remove(s)
        freed += load
    for d in chosen:
        notes.append(f"reserved {_weekday(d)} {d} as the rest day "
                     f"(lowest-value unpinned day, {day_load[d]:.0f} load freed)")
        if d in key_days:
            # Said loudly because it is the outcome nobody would choose: the cheapest day
            # ranking is a preference, "not a pinned day" is a hard constraint, and when
            # every cheaper day is agreed the only day left can be a protected one. The
            # athlete needs to know a key session went, so they can offer a different day.
            notes.append(f"{_weekday(d)} {d} carried a KEY session and was still the only "
                         f"day available — every cheaper day is agreed with the athlete. "
                         f"Worth asking them which day they would rather rest")
    if len(chosen) < shortfall:
        notes.append(f"only {len(chosen)} of the {shortfall} rest days needed could be "
                     f"reserved — every other loaded day is agreed with the athlete")
    if freed:
        # Deliberately does NOT promise the load is recovered. close_to_target offers it
        # to the easy endurance the generator owns, within the per-sport caps — and when
        # every remaining day is agreed there is nothing to stretch, so the week simply
        # lands lighter. Claiming reallocation here would be the quiet under-training the
        # floor check exists to catch.
        notes.append(f"{freed:.0f} load came off the week; it is offered back to the easy "
                     f"endurance on the days the generator owns, within their caps. A week "
                     f"that still lands short is reported as under its floor, not quietly "
                     f"trained")
    return proposal, chosen, notes


def _weekday(d: str) -> str:
    try:
        return date.fromisoformat(d).strftime("%a")
    except ValueError:
        return "?"
