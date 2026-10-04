"""One model read per chat message, for everything the bot records before it replies.

The bot records four things straight from chat: a week's availability, a new race, a
session the athlete moves off their usual days, and an open action they have finished,
booked or pushed back. Each used to have its own regex detector and parser in front of the
model. Availability went to the model first (lib/availability_reader.py, 4 Oct 2026, after
"the week starting the 12 october" was saved as 12 hours). Jamie then asked for the other
three to go the same way: the regexes save model calls and cost the athlete a misread.

One call reads all four, so a message costs one model read, not four. Code keeps what the
model must not own: which dates and items may be written, the off-pattern check, the
confirmation taps, and every read-back. A failed or unreadable call records nothing; the
chat model still sees the message.
"""
from __future__ import annotations

import json
import re
from datetime import date, timedelta

import availability_reader as ar

RACE_PRIORITIES = ("A", "B", "C")
DAY_SPORTS = ("swim", "bike", "run")
ACTION_STATUSES = ("done", "dropped", "booked", "ordered", "scheduled", "defer")
_MAX_RACE_DAYS = 730

# Whether a message is worth a read at all. This only decides whether to LOOK; the model
# decides what the message means. availability_reader.worth_reading covers days, weeks,
# hours and travel; these add race and to-do language.
_RACE_RE = re.compile(
    r"\brac(?:e|es|ing)\b|\bsportive|\btriathlon|\btri\b|\bmarathon|\bparkrun|"
    r"\bironman|\b70\.3\b|\b\d+\s?k\b|\bentered\b|\bsigned\s+up\b|\bevent\b|"
    r"\bfondo\b|\btime\s+trial\b|\bduathlon|\baquathlon", re.I)
_MOVE_RE = re.compile(r"\bmov(?:e|ed|ing)\b|\bswap|\bswitch|\binstead\b|\bshift", re.I)
_ACTION_RE = re.compile(
    r"\bdone\b|\bbooked\b|\bordered\b|\bfinished\b|\bcompleted?\b|\bsorted\b|\bcancel|"
    r"\bdrop|\bscheduled\b|\barranged\b|\bbought\b|\bgot\s+(?:the|my)\b|\bpostpone|"
    r"\bpush(?:ed|ing)?\b|\bdelay|\blater\b|\bdid\s+(?:the|my)\b|\bno\s+longer\b",
    re.I)


def worth_reading(text: str, *, ask_outstanding: bool = False,
                  has_actions: bool = False) -> bool:
    t = (text or "").strip()
    if not t:
        return False
    return (ar.worth_reading(t, ask_outstanding) or bool(_RACE_RE.search(t))
            or bool(_MOVE_RE.search(t)) or (has_actions and bool(_ACTION_RE.search(t))))


def _usual_days(day_rules: dict | None) -> str:
    bits = []
    for s in DAY_SPORTS:
        days = (day_rules or {}).get(f"{s}_days")
        if isinstance(days, list) and days:
            bits.append(f"{s} {'/'.join(str(d)[:3].title() for d in days)}")
    return "; ".join(bits) or "not set"


def build_prompt(text: str, *, today: date, recent: list | None = None,
                 asked_week: date | None = None, baseline_weeks: set | None = None,
                 saved: dict | None = None, sports: list | None = None,
                 day_rules: dict | None = None, actions: list | None = None) -> str:
    asked = (f"This morning's card asked how many hours they have for the week starting "
             f"{asked_week.isoformat()}, and they have not answered yet. The same card may "
             f"also ask for scores (pain 0-10, RPE) or weight, so a bare number is only an "
             f"hours answer if nothing else on the card fits it."
             if asked_week else "No hours question is waiting for an answer.")
    acts = "\n".join(f"{i}. {a}" for i, a in enumerate(actions or [])) or "(none)"
    return f"""You read ONE chat message from an endurance athlete to their coach. Before the coach
replies, the system records four kinds of fact from chat. Decide which, if any, this message
states. What you return is written into the athlete's plan files, so a wrong record costs
them real training. When in doubt, record nothing: the coach still reads the message.

Today is {ar._fmt(today)} {today.year}. The athlete's sports: {", ".join(sports or ar.SPORTS)}.
Their usual days: {_usual_days(day_rules)}.
Weeks (use ONLY these week_start dates for availability and day changes):
{ar._calendar(today, baseline_weeks or set(), saved or {})}

{asked}

Their open to-do items, by number:
{acts}

Recent conversation, oldest first:
{ar._transcript(recent or [])}

The athlete's NEW message:
<<<
{text.strip()}
>>>

Never record from a question (with or without a question mark), a hypothetical, a plan they
are only thinking about, or a report of something that already happened, unless the
section below says otherwise. A calendar date ("the 12th", "12 October") is never an hours
figure or a count.

1. AVAILABILITY: they STATE what time they have in a week: hours for the week ("12 hours
   next week", or a bare "12" answering the hours question), days they can or cannot train
   (travel, holidays, work), which sport goes on which day across the week, or a sport
   dropped for the whole week. kind "unclear" when they are declaring availability but you
   cannot be sure what to save (contradicts something earlier, ambiguous week or days):
   give the ONE short question to ask. kind "clear" when they say what was saved for a week
   is wrong, without the replacement. If the message corrects or completes an earlier one,
   return the whole corrected week.

2. RACE: they say they are racing, have entered or signed up for a specific event on a
   specific future date. Not a race already done, not "thinking about", not one with no
   date. priority only if they said it (A, B or C, or plainly "my main/A race").

3. DAY CHANGE: they direct ONE specific session onto ONE specific day in the weeks above
   that is off their usual days: "I'll swim Wednesday instead", "move Saturday's ride to
   Sunday". sport is the session's sport, date the day it goes TO. A whole week's shape is
   AVAILABILITY, not this.

4. ACTION: they say one of the numbered to-do items above has changed: done, dropped (no
   longer doing it), booked, ordered, scheduled, or pushed back (status "defer", with the
   new date in defer_to if they gave one, else null). items lists the numbers they plainly
   mean. An intention ("I'll book it") is not a change.

Return ONLY this JSON, nothing else:
{{"availability": {{"kind": "declaration" | "unclear" | "clear" | "none",
                   "weeks": [{{"week_start": "YYYY-MM-DD", "hours": <number or null>,
                              "constraints": "anything else that boxes the week in, their words, or empty",
                              "swim_days": [...] or null, "bike_days": [...] or null,
                              "run_days": [...] or null, "unavailable_days": [...] or null,
                              "excluded_sports": [...]}}],
                   "question": "for unclear only", "reason": "one short line"}},
  "race": {{"found": true | false, "name": "event name", "date": "YYYY-MM-DD",
           "priority": "A" | "B" | "C" | null, "reason": "one short line"}},
  "day_change": {{"found": true | false, "sport": "swim" | "bike" | "run",
                 "date": "YYYY-MM-DD", "reason": "one short line"}},
  "action": {{"found": true | false, "items": [<numbers>],
             "status": "done" | "dropped" | "booked" | "ordered" | "scheduled" | "defer",
             "defer_to": "YYYY-MM-DD" or null, "reason": "one short line"}}}}
Days are written Mon Tue Wed Thu Fri Sat Sun. null means they did not say; [] means none."""


def _iso(v) -> date | None:
    try:
        return date.fromisoformat(str(v)[:10])
    except (TypeError, ValueError):
        return None


def _race(obj, today: date) -> dict:
    obj = obj if isinstance(obj, dict) else {}
    out = {"found": False, "name": "", "date": None, "priority": None,
           "reason": str(obj.get("reason") or "")}
    d = _iso(obj.get("date"))
    name = str(obj.get("name") or "").strip()[:120]
    if obj.get("found") is True and name and d and today <= d <= today + timedelta(
            days=_MAX_RACE_DAYS):
        pri = str(obj.get("priority") or "").upper()
        out.update(found=True, name=name, date=d.isoformat(),
                   priority=pri if pri in RACE_PRIORITIES else None)
    return out


def _day_change(obj, today: date) -> dict:
    obj = obj if isinstance(obj, dict) else {}
    out = {"found": False, "sport": None, "date": None, "reason": str(obj.get("reason") or "")}
    d = _iso(obj.get("date"))
    weeks = ar.week_mondays(today)
    in_range = d is not None and today <= d < weeks[-1] + timedelta(days=7)
    if obj.get("found") is True and obj.get("sport") in DAY_SPORTS and in_range:
        out.update(found=True, sport=obj["sport"], date=d.isoformat())
    return out


def _action(obj, today: date, n_actions: int) -> dict:
    obj = obj if isinstance(obj, dict) else {}
    out = {"found": False, "items": [], "status": None, "defer_to": None,
           "reason": str(obj.get("reason") or "")}
    items = []
    for i in obj.get("items") or []:
        if isinstance(i, int) and 0 <= i < n_actions and i not in items:
            items.append(i)
    status = obj.get("status")
    if obj.get("found") is True and items and status in ACTION_STATUSES:
        d = _iso(obj.get("defer_to")) if status == "defer" else None
        out.update(found=True, items=items[:4], status=status,
                   defer_to=d.isoformat() if d and d > today else None)
    return out


def parse(raw: str, today: date, n_actions: int = 0) -> dict | None:
    m = re.search(r"\{.*\}", raw or "", re.S)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
    except ValueError:
        return None
    if not isinstance(obj, dict):
        return None
    return {"availability": ar.drop_empty(ar.parse_obj(obj.get("availability"), today)),
            "race": _race(obj.get("race"), today),
            "day_change": _day_change(obj.get("day_change"), today),
            "action": _action(obj.get("action"), today, n_actions)}


def read(text: str, *, today: date, call, **ctx) -> dict | None:
    """Ask the model. `call(prompt) -> str` is injected. None on a failed or unusable call:
    record nothing."""
    try:
        raw = call(build_prompt(text, today=today, **ctx))
    except Exception:
        return None
    return parse(raw, today, len(ctx.get("actions") or []))
