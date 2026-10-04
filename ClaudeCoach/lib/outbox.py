"""Every coach message sent to an athlete, recorded for the web app - and the switch
that takes an athlete off Telegram.

Peak (coach.diamondpeak.uk) shows the coach's scheduled messages - morning card,
activity write-ups, weekly plan, check-ins, nudges - in its chat, with their buttons,
and sends a phone notification for each. Every Telegram sender calls record() with
what it is about to send; the web API (api/push.py) tails the file for notifications
and merges it into the chat timeline.

    athletes/<slug>/web-outbox.jsonl   one JSON object per line, append-only:
        {"id", "ts", "text", "buttons", "photo", "source", "fmt"}

telegram_on(chat_id) is the switch. It is False when:
  - the athlete's athletes.json entry has "telegram": false (moved to the web), or
  - the chat id is a web-only one ("web-..."), given to athletes who signed up in Peak.
A sender that gets False records the message and skips Telegram entirely.

Someone invited in Peak has no athlete folder until they finish the sign-up questions,
so their chat is kept in config/web-signup/<chat id>/ (same file names) and moved into
athletes/<slug>/ by adopt() when their profile is created. Their own answers are kept
there too, as {"who": "me"} entries, since no history.json exists for them yet.

Everything here is fail-soft: a recording failure must never stop a Telegram send.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import shutil
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent            # ClaudeCoach/
ATHLETES_CONFIG = BASE / "config" / "athletes.json"
OUTBOX_NAME = "web-outbox.jsonl"
MEDIA_DIR = "web-media"
KEEP_LINES = 400                                           # trimmed back to this at 2x
SIGNUP_DIR = BASE / "config" / "web-signup"                # gitignored
_WEB_CID = re.compile(r"^web-[0-9a-f]{6,32}$")


_CACHE = {"mtime": None, "data": {}}


def _athletes() -> dict:
    try:
        m = ATHLETES_CONFIG.stat().st_mtime
        if m != _CACHE["mtime"]:
            _CACHE["data"], _CACHE["mtime"] = json.loads(ATHLETES_CONFIG.read_text()), m
        return _CACHE["data"]
    except (OSError, ValueError):
        return {}


def slug_for_chat(chat_id) -> str | None:
    cid = str(chat_id or "")
    if not cid:
        return None
    for slug, a in _athletes().items():
        if isinstance(a, dict) and str(a.get("chat_id") or "") == cid:
            return slug
    return None


def _chat_dir(chat_id) -> Path | None:
    """Where this chat's messages live: its athlete's folder, or the sign-up folder of
    someone invited in Peak who has no athlete yet. None for any other chat."""
    cid = str(chat_id or "")
    slug = slug_for_chat(cid)
    if slug:
        return BASE / "athletes" / slug
    if _WEB_CID.match(cid):
        return SIGNUP_DIR / cid
    return None


def signing_up(chat_id) -> bool:
    """Invited in Peak and not coaching yet (answering the sign-up questions, or
    waiting for the coach to approve). Nothing else keeps their chat, so it is kept here."""
    cid = str(chat_id or "")
    if not _WEB_CID.match(cid):
        return False
    slug = slug_for_chat(cid)
    return not slug or not _athletes().get(slug, {}).get("active")


def telegram_on(chat_id) -> bool:
    """Should this chat still get Telegram messages? Unknown chats (e.g. Jamie's admin
    chat before it maps to an athlete) keep Telegram, so nothing goes quiet by accident."""
    cid = str(chat_id or "")
    if cid.startswith("web-"):
        return False
    slug = slug_for_chat(cid)
    if not slug:
        return True
    return _athletes().get(slug, {}).get("telegram", True) is not False


def _buttons(reply_markup) -> list:
    """inline_keyboard rows, keeping only what the web can act on."""
    rows = []
    for row in (reply_markup or {}).get("inline_keyboard") or []:
        out = []
        for b in row or []:
            if not isinstance(b, dict) or not b.get("text"):
                continue
            if b.get("callback_data"):
                out.append({"text": b["text"], "data": str(b["callback_data"])})
            elif b.get("url"):
                out.append({"text": b["text"], "url": str(b["url"])})
        if out:
            rows.append(out)
    return rows


_HTML = [(re.compile(r"</?b>"), "*"), (re.compile(r"</?i>"), "_"), (re.compile(r"</?code>"), "`"),
         (re.compile(r"<[^>]+>"), "")]


def _from_html(text: str) -> str:
    for pat, rep in _HTML:
        text = pat.sub(rep, text)
    return text.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")


def record(chat_id, text: str = "", reply_markup=None, photo: bytes | None = None,
           source: str = "", parse_mode: str = "", who: str = "", tab: str = "") -> str | None:
    """Append one outbound message for the athlete behind chat_id (who="me": one of
    their own sign-up answers). Returns its id, or None if the chat is not an
    athlete's (or anything went wrong). tab="dev": shown in the coach's Dev tab, not
    the coaching chat (api/chat.py is_dev)."""
    try:
        adir = _chat_dir(chat_id)
        if not adir:
            return None
        adir.mkdir(parents=True, exist_ok=True)
        mid = datetime.now().strftime("%Y%m%d%H%M%S") + "-" + secrets.token_hex(3)
        photo_name = None
        if photo:
            (adir / MEDIA_DIR).mkdir(exist_ok=True)
            photo_name = f"{mid}.png"
            (adir / MEDIA_DIR / photo_name).write_bytes(photo)
        if (parse_mode or "").upper() == "HTML":
            text = _from_html(text or "")
        entry = {"id": mid, "ts": datetime.now().isoformat(timespec="seconds"),
                 "text": text or "", "buttons": _buttons(reply_markup),
                 "photo": photo_name, "source": source or os.path.basename(
                     os.environ.get("CC_OUTBOX_SOURCE", "") or "")}
        if who:
            entry["who"] = who
        if tab:
            entry["tab"] = tab
        f = adir / OUTBOX_NAME
        with open(f, "a") as fh:
            fh.write(json.dumps(entry) + "\n")
        _trim(f)
        return mid
    except Exception:
        return None


def _trim(f: Path) -> None:
    try:
        lines = f.read_text().splitlines()
        if len(lines) > 2 * KEEP_LINES:
            f.write_text("\n".join(lines[-KEEP_LINES:]) + "\n")
    except OSError:
        pass


def patch(chat_id, item_id: str, change) -> bool:
    """Apply change(entry) to one recorded message and save it."""
    try:
        adir = _chat_dir(chat_id)
        if not adir or not item_id:
            return False
        f = adir / OUTBOX_NAME
        lines, hit = f.read_text().splitlines(), False
        for i, line in enumerate(lines):
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if e.get("id") == item_id:
                change(e)
                lines[i], hit = json.dumps(e), True
        if hit:
            tmp = f.with_suffix(".tmp")
            tmp.write_text("\n".join(lines) + "\n")
            tmp.replace(f)
        return hit
    except Exception:
        return False


def update(chat_id, item_id: str, text: str | None = None, reply_markup=None, **fields) -> bool:
    """Rewrite one recorded message in place, as Telegram's editMessageText does: a
    tapped RPE/pain button becomes "✓ Pain 0/10 logged" with the follow-up buttons.
    Extra fields (the Log it card's form/logged/drills) are stored alongside."""
    def change(e):
        if text is not None:
            e.setdefault("orig_text", e.get("text") or "")   # history keeps the original
            e["text"] = text
        e["buttons"] = _buttons(reply_markup)
        e.update(fields)
    return patch(chat_id, item_id, change)


def read(slug: str, limit: int = 60) -> list[dict]:
    try:
        lines = (BASE / "athletes" / slug / OUTBOX_NAME).read_text().splitlines()
    except OSError:
        return []
    out = []
    for line in lines[-limit:]:
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def read_chat(chat_id, limit: int = 60) -> list[dict]:
    adir = _chat_dir(chat_id)
    if not adir:
        return []
    try:
        lines = (adir / OUTBOX_NAME).read_text().splitlines()
    except OSError:
        return []
    out = []
    for line in lines[-limit:]:
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def media_path(chat_id, name: str) -> Path | None:
    adir = _chat_dir(chat_id)
    return adir / MEDIA_DIR / name if adir else None


def record_answer(chat_id, text: str) -> str | None:
    """One of the athlete's own sign-up answers, for a web chat with no athlete yet."""
    if slug_for_chat(chat_id) or not _WEB_CID.match(str(chat_id or "")):
        return None
    return record(chat_id, text, source="web-signup", who="me")


def clear_buttons(chat_id) -> None:
    """A sign-up question was answered: its buttons go, so a reload can't answer it twice."""
    adir = _chat_dir(chat_id)
    f = adir / OUTBOX_NAME if adir else None
    try:
        if not f or not f.exists():
            return
        lines, changed = f.read_text().splitlines(), False
        for i, line in enumerate(lines):
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if e.get("buttons"):
                e["buttons"], changed = [], True
                lines[i] = json.dumps(e)
        if changed:
            tmp = f.with_suffix(".tmp")
            tmp.write_text("\n".join(lines) + "\n")
            tmp.replace(f)
    except Exception:
        pass


def adopt(chat_id, slug: str) -> None:
    """Sign-up finished and the athlete folder exists: the sign-up chat (questions,
    answers, screenshots) moves into it, so it is still there once they are coaching."""
    cid = str(chat_id or "")
    src = SIGNUP_DIR / cid
    if not _WEB_CID.match(cid) or not src.is_dir():
        return
    adir = BASE / "athletes" / slug
    try:
        adir.mkdir(parents=True, exist_ok=True)
        if (src / OUTBOX_NAME).exists():
            with open(adir / OUTBOX_NAME, "a") as fh:
                fh.write((src / OUTBOX_NAME).read_text())
        if (src / MEDIA_DIR).is_dir():
            (adir / MEDIA_DIR).mkdir(exist_ok=True)
            for p in (src / MEDIA_DIR).iterdir():
                p.replace(adir / MEDIA_DIR / p.name)
        if (src / "strava_tokens.json").exists():           # lib/strava_link.py
            (src / "strava_tokens.json").replace(adir / "strava_tokens.json")
        shutil.rmtree(src, ignore_errors=True)
    except OSError:
        pass
