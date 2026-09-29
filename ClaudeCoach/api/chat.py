"""Web chat: the Telegram coach, unchanged, with Peak as the transport.

The API process imports telegram/bot.py and runs a web message through the SAME
entry point a Telegram message takes (_route_text): fast paths, captures, the chat
allowance, model routing, the engine, and every post-reply check. Nothing about
coaching is re-implemented here, so the two can never become two different coaches.

Only the transport is swapped, and only inside this process (the running Telegram
bot is a separate process and is untouched):

  tg_post       calls addressed to a chat that has a web turn in flight are captured
                instead of sent: the "…" placeholder and its live status edits become
                `status` events, every other sendMessage becomes a `message` event.
                Anything addressed to another chat (e.g. a notice to Jamie) still goes
                to Telegram for real.
  stream_claude teed so the reply's growing text is also emitted as `draft` events.
                Telegram never shows the draft; the web does, then replaces it with
                the checked reply (the `message`), which is what history keeps.
  send_photo    charts still go to Telegram, and the web is told so.
  send_voice    dropped for web turns; the text reply still arrives.
  _submit       runs the reply worker on THIS thread (under the same per-chat lock)
                so its events stream back to this request.

One web turn per athlete at a time. A web turn and a Telegram turn for the same
athlete at the same instant are NOT serialised against each other (two processes);
both would complete, and the later history write wins.
"""
from __future__ import annotations

import os
import queue
import sys
import threading
from pathlib import Path

CC = Path(os.environ.get("CC_HOME") or Path(__file__).resolve().parent.parent)  # ClaudeCoach/

_SINKS: dict[str, "Sink"] = {}      # chat_id -> the web turn in flight
_SINKS_GUARD = threading.Lock()
_TURN = threading.local()          # .chat_id on the thread running a web turn
_bot = None
_bot_guard = threading.Lock()


class Busy(Exception):
    pass


class Sink:
    """Collects one web turn's output as (kind, text) events on a queue."""

    def __init__(self):
        self.q: queue.Queue = queue.Queue()
        self.placeholder_id = None
        self._next_id = 1_000_000

    def put(self, kind, text=""):
        self.q.put((kind, text))

    def _id(self):
        self._next_id += 1
        return self._next_id

    def handle(self, method, payload):
        text = (payload or {}).get("text") or ""
        if method == "sendMessage":
            mid = self._id()
            if text == "…" and payload.get("disable_notification") and self.placeholder_id is None:
                self.placeholder_id = mid       # the live status line
                self.put("status", "Thinking…")
            elif text:
                self.put("message", text)
            return {"ok": True, "result": {"message_id": mid}}
        if method == "editMessageText" and text:
            if payload.get("message_id") == self.placeholder_id:
                self.put("status", text)
            else:
                self.put("message", text)
        return {"ok": True, "result": {"message_id": payload.get("message_id") or self._id()}}


def _sink_for(chat_id):
    return _SINKS.get(str(chat_id or ""))


def _patch(b):
    orig_post, orig_photo, orig_voice = b.tg_post, b.send_photo, b.send_voice
    orig_stream = b.stream_claude

    def tg_post(token, method, payload):
        sink = _sink_for((payload or {}).get("chat_id"))
        return sink.handle(method, payload) if sink else orig_post(token, method, payload)

    def send_photo(token, chat_id, photo_bytes, *a, **k):
        sink = _sink_for(chat_id)
        if sink:
            sink.put("message", "_Chart sent to Telegram._")
        return orig_photo(token, chat_id, photo_bytes, *a, **k)

    def send_voice(token, chat_id, ogg_bytes, *a, **k):
        if _sink_for(chat_id):
            return None
        return orig_voice(token, chat_id, ogg_bytes, *a, **k)

    def stream_claude(*a, **k):
        sink = _sink_for(getattr(_TURN, "chat_id", None))
        for ev in orig_stream(*a, **k):
            if sink and ev and ev[0] == "chunk" and ev[1]:
                sink.put("draft", ev[1])
            yield ev

    def _submit(worker, chat_id, *args):
        if not _sink_for(chat_id):
            return orig_submit(worker, chat_id, *args)
        with b._chat_lock(chat_id):
            prev = getattr(b._HELD_LOCK, "chat_id", None)
            b._HELD_LOCK.chat_id = chat_id
            try:
                worker(*args)
            finally:
                b._HELD_LOCK.chat_id = prev

    orig_submit = b._submit
    b.tg_post, b.send_photo, b.send_voice = tg_post, send_photo, send_voice
    b.stream_claude, b._submit = stream_claude, _submit


def bot():
    global _bot
    with _bot_guard:
        if _bot is None:
            for p in (CC / "telegram", CC / "lib"):
                if str(p) not in sys.path:
                    sys.path.insert(0, str(p))
            import bot as b
            _patch(b)
            _bot = b
        return _bot


def chat_id_for(slug: str) -> str | None:
    for cid, a in bot().load_athletes().items():
        if a.get("slug") == slug:
            return str(cid)
    return None


def start_turn(slug: str, text: str) -> Sink:
    """Run one web message through the Telegram coach on a background thread.
    Returns the Sink its events arrive on; the last event is always ('done', '')."""
    b = bot()
    athletes = b.load_athletes()
    chat_id = next((str(cid) for cid, a in athletes.items() if a.get("slug") == slug), None)
    if not chat_id:
        raise LookupError(slug)
    sink = Sink()
    with _SINKS_GUARD:
        if chat_id in _SINKS:
            raise Busy(slug)
        _SINKS[chat_id] = sink

    def work():
        _TURN.chat_id = chat_id
        try:
            config = b.load_config()
            b.log(f"[{slug}] In (web): {text[:80]}")
            b._route_text(config["bot_token"], chat_id, text, athletes, config)
        except Exception as e:
            b.log(f"[{slug}] web turn error: {e}")
            sink.put("error", "Sorry - I hit a snag answering that. Give it another go in a moment.")
        finally:
            _TURN.chat_id = None
            with _SINKS_GUARD:
                _SINKS.pop(chat_id, None)
            sink.put("done")

    threading.Thread(target=work, name=f"web-chat-{slug}", daemon=True).start()
    return sink


def history(slug: str, pairs: int = 30) -> list[dict]:
    """The shared Telegram/web conversation, oldest first."""
    import json
    f = CC / "athletes" / slug / "telegram" / "history.json"
    try:
        h = json.loads(f.read_text())
    except (OSError, ValueError):
        return []
    return [{"user": e.get("user") or "", "assistant": e.get("assistant") or "",
             "ts": e.get("ts"), "kind": e.get("kind", "text")}
            for e in h[-pairs:] if isinstance(e, dict)]
