"""The Today note: the morning card, moved out of the chat to the top of Peak's Today tab
(Jamie, 15 Oct 2026: "move the morning card into the today section at the top ... refresh
at midnight and then update itself when the wellness data comes through. If you click on
it this should take you to the chat where you can talk about it, but it no longer comes
through the chat"; then "everyone now with the notifications").

    athletes/<slug>/today-note.json
        {"date", "text", "stage", "ts", "opened"}
        stage  "plan"      written just after midnight, from the plan and the load
               "wellness"  rewritten once last night's sleep / HRV / resting HR landed
               "final"     no wellness by the last morning run: the plan version stands

One phone notification per day, when the note reaches "wellness" or "final" (an outbox
line with source PING, which the chat never shows). Tapping the note puts it into the chat
once per version (source CARD, no notification), so the coach can talk about it.
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
PING = "today-ping"        # the notification line: never shown in the chat
CARD = "today-card"        # the note put into the chat on a tap: no notification
STAGES = ("plan", "wellness", "final")


def _path(slug: str) -> Path:
    return BASE / "athletes" / slug / "today-note.json"


def read(slug: str, day: date | None = None) -> dict | None:
    """Today's note (or `day`'s), else None."""
    try:
        n = json.loads(_path(slug).read_text())
    except (OSError, ValueError):
        return None
    return n if n.get("date") == (day or date.today()).isoformat() else None


def write(slug: str, text: str, stage: str, day: date | None = None) -> dict:
    n = {"date": (day or date.today()).isoformat(), "text": text, "stage": stage,
         "ts": datetime.now().isoformat(timespec="seconds"), "opened": None}
    p = _path(slug)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(n, indent=1))
    tmp.replace(p)
    return n


def set_stage(slug: str, stage: str) -> None:
    n = read(slug)
    if n:
        write(slug, n["text"], stage)


def headline(text: str, limit: int = 120) -> str:
    """The note's first sentence, plain, for the notification."""
    t = re.sub(r"[*_`]+", "", text or "")
    t = re.sub(r"\s+", " ", t).strip()
    m = re.match(r"(.+?[.!?])(\s|$)", t)
    t = m.group(1) if m else t
    return t if len(t) <= limit else t[: limit - 1].rstrip() + "…"


def ping(slug: str, chat_id, text: str) -> None:
    """The day's one notification (api/push.py opens Today for this source)."""
    import outbox
    outbox.record(chat_id, headline(text), source=PING)


def mark_opened(slug: str) -> dict | None:
    """The note as the chat should show it, the first time this version is tapped; None
    if it was already put into the chat (or there is no note)."""
    n = read(slug)
    if not n or n.get("opened") == n["ts"]:
        return None
    n["opened"] = n["ts"]
    p = _path(slug)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(n, indent=1))
    tmp.replace(p)
    return n
