"""Calendar and activity lines for the chat's live-data block (telegram/bot.py prefetch_context).

Why (2 Oct 2026). The block listed only the next 8 planned events, with no ids, and the
last 5 activities with no ids or names. Eight events can be four days for an athlete
who trains twice a day, and every move or edit needs an event id, so the coach ran
icu_fetch.py for events or history on most planning turns (~10 tool calls a message),
each call re-sending the whole conversation. These lines carry what those fetches were
for: every planned session in the next 3 weeks with its id and load, last week's plan
marked done or not done, and the last 7 days of activities with ids and the numbers
asked about most.
"""
from __future__ import annotations

from datetime import date


def _family(t) -> str:
    t = str(t or "")
    return next((s for s in ("Ride", "Run", "Swim") if s in t), t)


def activity_lines(acts: list, limit: int = 12) -> list[str]:
    out = []
    for a in sorted(acts or [], key=lambda x: x.get("start_date_local", ""), reverse=True)[:limit]:
        bits = [(a.get("start_date_local") or "")[:10], f"{a.get('type', '?'):<12}",
                f"{round((a.get('moving_time') or 0) / 60)}min"]
        if a.get("distance"):
            bits.append(f"{a['distance'] / 1000:.1f}km")
        bits.append(f"Load={a.get('icu_training_load') or 0}")
        if a.get("average_heartrate"):
            bits.append(f"HR {round(a['average_heartrate'])}")
        if a.get("icu_intensity"):
            bits.append(f"IF {a['icu_intensity'] / 100:.2f}")
        if a.get("decoupling") is not None:
            bits.append(f"dec {a['decoupling']:.1f}%")
        bits.append(f"id={a.get('id')}")
        if a.get("name"):
            bits.append(f'"{a["name"]}"')
        out.append("  " + "  ".join(bits))
    return out


def planned_last_week(past_events: list, acts: list) -> list[str]:
    """Each planned workout of the last 7 days, done (paired by Intervals.icu, or an
    unpaired activity of the same sport that day) or not done."""
    paired = {str(e.get("paired_activity_id")) for e in past_events or [] if e.get("paired_activity_id")}
    spare = [a for a in acts or [] if str(a.get("id")) not in paired]
    out = []
    for e in sorted(past_events or [], key=lambda e: str(e.get("start_date_local"))):
        d = (e.get("start_date_local") or "")[:10]
        done = e.get("paired_activity_id")
        if not done:
            hit = next((a for a in spare if (a.get("start_date_local") or "")[:10] == d
                        and _family(a.get("type")) == _family(e.get("type"))), None)
            if hit:
                spare.remove(hit)
                done = hit.get("id")
        mark = f"done ({done})" if done else "NOT DONE"
        out.append(f"  {d}  {str(e.get('type') or ''):<12} {e.get('name') or ''}  "
                   f"load {e.get('load_target') or '?'}  id={e.get('id')}  {mark}")
    return out


def upcoming(events: list, limit: int = 40) -> list[str]:
    out = []
    for ev in (events or [])[:limit]:
        load = ev.get("load_target") or ev.get("icu_training_load")
        out.append(f"  {(ev.get('start_date_local') or '')[:10]}  "
                   f"{str(ev.get('type') or ev.get('category') or ''):<12} {ev.get('name') or ''}"
                   + (f"  load {load}" if load else "") + f"  id={ev.get('id')}")
    if len(events or []) > limit:
        out.append(f"  (+{len(events) - limit} more - fetch events for the rest)")
    return out
