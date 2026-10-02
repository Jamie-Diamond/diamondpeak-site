"""Buttons on the evening check-in's "Did the [session] happen today?" (2 Oct 2026).

Jamie had to type "No" to it, and the chat then misread the answer. Now the question
carries ✅ Yes / ❌ No, and No offers 📅 Reschedule (pick one of the next six days) or
⏭ Skip (logged as missed). scripts/evening-checkin.py attaches the first pair; the taps
are handled in telegram/bot.py _handle_did_happen, the same in Telegram and Peak.

Callback data, all under Telegram's 64-byte limit:
    did:y:<slug>:<event id>             it happened
    did:n:<slug>:<event id>             it didn't: offer Reschedule / Skip
    did:r:<slug>:<event id>             Reschedule: offer the days
    did:m:<slug>:<event id>:<date>      move it to that date
    did:s:<slug>:<event id>             skip it
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta
from pathlib import Path

PREFIX = "did:"
VERBS = ("y", "n", "r", "m", "s")
DAYS_OFFERED = 6
_ASK_RE = re.compile(r"\bdid\b.*\bhappen\b", re.IGNORECASE | re.DOTALL)


def is_ask(text: str) -> bool:
    """The check-in's Case B question, as evening-checkin.py words it."""
    return bool(_ASK_RE.search(text or ""))


def find_event(text: str, events: list) -> dict | None:
    """The planned session the question names: the longest event name found in the
    text, so "Strength B" never wins over "Strength B — split squat". Events already
    paired with an activity are not candidates. If the question shortened the name,
    the only unpaired session of the day is the one it is about; with two or more,
    None (no buttons) rather than a guess."""
    low = (text or "").lower()
    open_ = [e for e in events or [] if not e.get("paired_activity_id")]
    hits = [e for e in open_
            if (e.get("name") or "").strip() and e["name"].strip().lower() in low]
    if hits:
        return max(hits, key=lambda e: len(e["name"].strip()))
    return open_[0] if len(open_) == 1 else None


def _cb(verb, slug, event_id, day=None) -> str:
    return ":".join(["did", verb, str(slug), str(event_id)] + ([day] if day else []))


def parse(data: str):
    """(verb, slug, event id, date or None), or None if it is not one of ours."""
    if not (data or "").startswith(PREFIX):
        return None
    parts = data.split(":")
    if len(parts) not in (4, 5) or parts[1] not in VERBS or not parts[3]:
        return None
    if (parts[1] == "m") != (len(parts) == 5):
        return None
    if len(parts) == 5:
        try:
            date.fromisoformat(parts[4])
        except ValueError:
            return None
    return parts[1], parts[2], parts[3], (parts[4] if len(parts) == 5 else None)


def ask_markup(slug, event_id) -> dict:
    return {"inline_keyboard": [[
        {"text": "✅ Yes", "callback_data": _cb("y", slug, event_id)},
        {"text": "❌ No", "callback_data": _cb("n", slug, event_id)}]]}


def no_markup(slug, event_id) -> dict:
    return {"inline_keyboard": [[
        {"text": "📅 Reschedule", "callback_data": _cb("r", slug, event_id)},
        {"text": "⏭ Skip", "callback_data": _cb("s", slug, event_id)}]]}


def day_options(event_day: date, today: date | None = None) -> list[date]:
    """The next six days from the day after the session (or from today, if the button
    is tapped a day or more late)."""
    start = max(event_day + timedelta(days=1), today or date.today())
    return [start + timedelta(days=i) for i in range(DAYS_OFFERED)]


def day_markup(slug, event_id, event_day: date, today: date | None = None) -> dict:
    days = [{"text": d.strftime("%a ") + str(d.day),
             "callback_data": _cb("m", slug, event_id, d.isoformat())}
            for d in day_options(event_day, today)]
    return {"inline_keyboard": [days[:3], days[3:],
                                [{"text": "⏭ Skip instead", "callback_data": _cb("s", slug, event_id)}]]}


def nice_day(d: date) -> str:
    return d.strftime("%a ") + str(d.day) + d.strftime(" %b")


def event_day(event: dict) -> date | None:
    try:
        return date.fromisoformat(str(event.get("start_date_local") or "")[:10])
    except ValueError:
        return None


def moved_start(event: dict, day: str) -> str:
    """The new start_date_local: the new date, the session's own time of day."""
    t = str(event.get("start_date_local") or "")[11:19] or "00:00:00"
    return f"{day}T{t}"


def record_skip(adir: Path, event: dict) -> bool:
    """Log the session as missed in current-state.json, where the coach logs a missed
    session by hand. Once per event. False if it could not be written."""
    f = Path(adir) / "current-state.json"
    try:
        st = json.loads(f.read_text()) if f.exists() else {}
        missed = st.get("missed_sessions")
        if not isinstance(missed, list):
            missed = []
        eid = str(event.get("id") or "")
        if not any(str(m.get("event_id") or "") == eid for m in missed if isinstance(m, dict)):
            missed.append({"date": str(event.get("start_date_local") or "")[:10],
                           "session": event.get("name") or "",
                           "event_id": eid,
                           "reason": "skipped (tapped Skip on the evening check-in)",
                           "logged_at": datetime.now().isoformat(timespec="seconds")})
        st["missed_sessions"] = missed
        f.write_text(json.dumps(st, indent=2, ensure_ascii=False))
        return True
    except Exception:
        return False
