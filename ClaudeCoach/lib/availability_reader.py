"""Reads a chat message for a weekly availability declaration - with the model, not regexes.

Replaces the regex tiers telegram/bot.py ran before the model (hours figure, day shape,
sport exclusion). On 4 Oct 2026 they misread two athletes in one morning: Fred's "By next
week do you mean the week starting the 12 october" was saved as 12 hours, and James's "swim
on Monday cycle on Wednesday. Nothing possible on Tuesday" as "bike Mon; rest Wed". Jamie:
"this is an example of cutting corners to save AI usage ... and we end up with a shit UX".
Every patch to those regexes fixed one phrasing and left the next one open.

So the model reads the message WITH the calendar and the last few turns of conversation,
and returns structured JSON. Code still owns everything the model must not: the date
arithmetic (the calendar is handed to it, it only picks a Monday from the list), the
sanity band on hours, which weeks may be written, the merge with what is already saved,
and the read-back the athlete sees, which is built from what was STORED.

A failed or unreadable model call saves nothing and returns None: the message still goes
to the chat model, which can ask.
"""
from __future__ import annotations

import json
import re
from datetime import date, timedelta

import weekly_availability as wa

DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
SPORTS = ("swim", "bike", "run")
_DAY_KEYS = ("swim_days", "bike_days", "run_days", "unavailable_days")
WEEKS_AHEAD = 3              # this week plus the next three can be declared
_RECENT_TURNS = 4
KINDS = ("declaration", "unclear", "clear", "none")

# Which messages are worth a model call. This only decides whether to LOOK: a message that
# passes is read by the model, which decides what it means. While the Sunday hours question
# is outstanding every message is read, because a bare "12" can be the answer.
_WORTH_READING_RE = re.compile(
    r"\bweek|\bweekend|\bmon(?:day)?\b|\btue(?:s|sday)?\b|\bwed(?:nesday)?\b|"
    r"\bthu(?:rs|rsday)?\b|\bfri(?:day)?\b|\bsat(?:urday)?\b|\bsun(?:day)?\b|"
    r"\d\s*(?:h|hr|hrs|hours?)\b|\bhours?\b|\baway\b|\btravel|\bholiday|\btrip\b|"
    r"\bvacation|\bbusy\b|\bavailab|\bdays?\s+off\b|\bfree\b|\bcan'?t\b|\bcannot\b|"
    r"\bunable\b|\bno\s+(?:swim|bike|cycl|rid|run)", re.I)


def worth_reading(text: str, ask_outstanding: bool = False) -> bool:
    t = (text or "").strip()
    if not t:
        return False
    return ask_outstanding or bool(_WORTH_READING_RE.search(t))


def _monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


def week_mondays(today: date) -> list[date]:
    m = _monday(today)
    return [m + timedelta(days=7 * i) for i in range(WEEKS_AHEAD + 1)]


def _fmt(d: date) -> str:
    return f"{d:%a} {d.day} {d:%b}"


def _calendar(today: date, baseline_weeks: set, saved: dict) -> str:
    names = ["this week", "next week", "the week after", "in three weeks"]
    lines = []
    for i, m in enumerate(week_mondays(today)):
        days = ", ".join(_fmt(m + timedelta(days=k)) for k in range(7))
        tag = ""
        if m in baseline_weeks:
            tag = ("  [BASELINE WEEK: the sign-up test week, already on the calendar. "
                   "Nothing for this week is saved here]")
        lines.append(f"- {names[i]}, week_start {m.isoformat()}: {days}{tag}")
        if saved.get(m):
            lines.append(f"    already saved for this week: {json.dumps(saved[m])}")
    return "\n".join(lines)


def _clip(s: str, head: int = 300, tail: int = 1200) -> str:
    s = (s or "").strip()
    return s if len(s) <= head + tail else f"{s[:head]} [...] {s[-tail:]}"


def _transcript(recent: list) -> str:
    out = []
    for e in (recent or [])[-_RECENT_TURNS:]:
        if (e.get("user") or "").strip():
            out.append(f"ATHLETE: {_clip(e['user'])}")
        if (e.get("assistant") or "").strip():
            out.append(f"COACH: {_clip(e['assistant'])}")
    return "\n".join(out) or "(none)"


def build_prompt(text: str, *, today: date, recent: list | None = None,
                 asked_week: date | None = None, baseline_weeks: set | None = None,
                 saved: dict | None = None, sports: list | None = None) -> str:
    asked = (f"This morning's card asked how many hours they have for the week starting "
             f"{asked_week.isoformat()}, and they have not answered yet. The same card may "
             f"also ask for scores (pain 0-10, RPE) or weight, so a bare number is only an "
             f"hours answer if nothing else on the card fits it."
             if asked_week else "No hours question is waiting for an answer.")
    return f"""You read ONE chat message from an endurance athlete to their coach and decide whether it
DECLARES their training availability for a specific week. What you return is saved straight
into the file next week's plan is built from, so a wrong save costs the athlete a week of
training built to numbers they never gave. When in doubt, do not save.

Today is {_fmt(today)} {today.year}. The athlete's sports: {", ".join(sports or SPORTS)}.
Weeks that can be declared (use ONLY these week_start dates):
{_calendar(today, baseline_weeks or set(), saved or {})}

{asked}

Recent conversation, oldest first:
{_transcript(recent or [])}

The athlete's NEW message:
<<<
{text.strip()}
>>>

A DECLARATION is the athlete STATING what time they have in a week:
- hours available for the week ("12 hours next week"; a bare "12" answering the hours question)
- days they can or cannot train: travel, holidays, work ("away Thu to Sun", "nothing Tuesday")
- which sport goes on which day ("swim Monday, bike Wednesday")
- a sport dropped for the whole week ("no cycling this week")

NOT a declaration (kind "none"):
- any question, with or without a question mark, including asking what the coach meant
- a calendar DATE: "the 12th", "12 October", "w/c 12" are dates, NEVER hours
- training already done, one session's length ("a 2 hour ride"), a single day's time
- a pain, RPE, weight or test score; a hypothetical; anything else

kind "unclear": they are declaring availability but you cannot be sure what to save (it
contradicts something they said earlier, the week is ambiguous, a number could equally answer
another question, days conflict). Put the ONE short question to ask in "question".
kind "clear": they say what was saved for a week is wrong and should go, without giving the
replacement.

If the new message corrects or completes an earlier one in the conversation, return the
whole corrected week, not just the change.

Return ONLY this JSON, nothing else:
{{"kind": "declaration" | "unclear" | "clear" | "none",
  "weeks": [{{"week_start": "YYYY-MM-DD from the list above",
             "hours": <weekly hours as a number, or null if they gave none>,
             "constraints": "anything else that boxes the week in, in their own words, or empty",
             "swim_days": [...] or null, "bike_days": [...] or null,
             "run_days": [...] or null, "unavailable_days": [...] or null,
             "excluded_sports": ["swim" | "bike" | "run", ...]}}],
  "question": "for unclear only, else empty",
  "reason": "one short line: why"}}
Days are written Mon Tue Wed Thu Fri Sat Sun. null means they did not say; [] means they
said none. Put a day in a sport's list only if they put that sport on that day."""


def _days(v) -> list | None:
    if v is None:
        return None
    if not isinstance(v, list):
        return None
    out = []
    for x in v:
        d = str(x).strip()[:3].title()
        if d in DAYS and d not in out:
            out.append(d)
    return sorted(out, key=DAYS.index)


def parse(raw: str, today: date) -> dict | None:
    """The model's reply, validated. None when it is not usable JSON."""
    m = re.search(r"\{.*\}", raw or "", re.S)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
    except ValueError:
        return None
    if not isinstance(obj, dict):
        return None
    kind = obj.get("kind") if obj.get("kind") in KINDS else "none"
    allowed = set(week_mondays(today))
    weeks, bad_week = [], False
    for w in obj.get("weeks") or []:
        if not isinstance(w, dict):
            continue
        try:
            ws = date.fromisoformat(str(w.get("week_start"))[:10])
        except ValueError:
            ws = None
        if ws not in allowed:
            bad_week = True
            continue
        hours = w.get("hours")
        try:
            hours = None if hours in (None, "") else float(hours)
        except (TypeError, ValueError):
            hours = None
        rec = {"week_start": ws, "hours": hours,
               "constraints": str(w.get("constraints") or "").strip()[:300],
               "excluded_sports": sorted({s for s in (w.get("excluded_sports") or [])
                                          if s in SPORTS})}
        for k in _DAY_KEYS:
            rec[k] = _days(w.get(k))
        weeks.append(rec)
    out = {"kind": kind, "weeks": weeks,
           "question": str(obj.get("question") or "").strip(),
           "reason": str(obj.get("reason") or "").strip()}
    if kind in ("declaration", "clear") and not weeks:
        # It said "save" but named no week we can write: ask rather than guess.
        out.update(kind="unclear",
                   question=out["question"] or "Which week do you mean?")
    elif kind == "declaration" and bad_week:
        out["reason"] = (out["reason"] + " (a week outside the next four was dropped)").strip()
    if out["kind"] == "unclear" and not out["question"]:
        out["question"] = "Which week is that for, and what should I save?"
    return out


def _has_content(w: dict) -> bool:
    return (w.get("hours") is not None or bool(w.get("constraints"))
            or bool(w.get("excluded_sports"))
            or any(w.get(k) is not None for k in _DAY_KEYS))


def merged(prior: dict | None, w: dict) -> dict:
    """The record to store: the new statement over what was already saved for the week.
    record() REPLACES a week, so anything this message did not mention is carried forward."""
    p = prior or {}
    out = {"hours": w["hours"] if w.get("hours") is not None else p.get("hours"),
           "constraints": w.get("constraints") or p.get("constraints") or ""}
    named = set(p.get("declared_days") or [])
    for k in _DAY_KEYS:
        if w.get(k) is not None:
            out[k] = w[k]
            named |= set(w[k])
        elif isinstance(p.get(k), list):
            out[k] = p[k]
    # A sport excluded earlier stays excluded unless this message puts it on a day.
    excl = set(w.get("excluded_sports") or [])
    excl |= {s for s in (p.get("excluded_sports") or []) if not w.get(f"{s}_days")}
    for s in excl:
        out[f"{s}_days"] = []
    if excl:
        out["excluded_sports"] = sorted(excl)
    if named:
        out["declared_days"] = sorted(named, key=DAYS.index)
    if p.get("rest_day_waiver"):
        out["rest_day_waiver"] = p["rest_day_waiver"]
    return out


def describe(rec: dict) -> str:
    """One line of what is saved for a week, for the read-back."""
    bits = []
    if rec.get("hours") is not None:
        bits.append(f"{rec['hours']:g} hours")
    for k, label in (("swim_days", "swim"), ("bike_days", "bike"), ("run_days", "run"),
                     ("unavailable_days", "nothing")):
        if rec.get(k):
            bits.append(f"{label} {'/'.join(rec[k])}")
    for s in rec.get("excluded_sports") or []:
        bits.append(f"no {s}")
    return "; ".join(bits)


def read(text: str, *, today: date, call, **ctx) -> dict | None:
    """Ask the model. `call(prompt) -> str` is injected (claude_call in the bot, a stub in
    tests). None when the call fails or the reply is unusable: save nothing."""
    try:
        raw = call(build_prompt(text, today=today, **ctx))
    except Exception:
        return None
    r = parse(raw, today)
    if r and r["kind"] == "declaration":
        r["weeks"] = [w for w in r["weeks"] if _has_content(w)]
        if not r["weeks"]:
            r["kind"] = "none"
    return r


def saved_for(slug: str, today: date) -> dict:
    """What is already declared for each week the reader may write, for the prompt."""
    out = {}
    for m in week_mondays(today):
        d = wa.for_week(slug, m)
        if d:
            out[m] = {k: v for k, v in d.items()
                      if k in ("hours", "constraints") + wa.DAY_SHAPE_KEYS}
    return out
