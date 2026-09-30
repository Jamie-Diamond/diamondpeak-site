"""Web chat: the Telegram coach, unchanged, with Peak as the transport.

The API process imports telegram/bot.py and runs a web message through the SAME
entry point a Telegram message takes (_route_text): fast paths, captures, onboarding,
the chat allowance, model routing, the engine, and every post-reply check. A button
tap goes through the bot's own dispatch_callback. Nothing about coaching is
re-implemented here, so the two can never become two different coaches.

Only the transport is swapped, and only inside this process (the running Telegram
bot is a separate process and is untouched):

  tg_post       calls addressed to a chat that has a web turn in flight are captured
                instead of sent: the "…" placeholder and its live status edits become
                `status` events, every other sendMessage becomes a `message` event
                (with its buttons). Anything addressed to another chat (e.g. a notice to
                Jamie) goes on as normal - to Telegram, or to that athlete's web outbox
                if they are web-only (see lib/outbox.py).
  stream_claude teed so the reply's visible text (inside <telegram>, as Telegram's own
                live view does) is also emitted as `draft` events; the checked reply
                (`message`) replaces it and is what history keeps.
  send_photo    a chart is shown inline (`photo`) and kept in the web outbox, not sent
                to Telegram.
  send_voice    voice mode's own audio is kept and played in Peak.
  download_tg_file  a "web:<n>" file id returns the photo Peak uploaded, so a web photo
                goes through the bot's own image path (_image_reply_worker) unchanged.
  _submit       runs the reply worker on THIS thread (under the same per-chat lock)
                so its events stream back to this request.

One web turn per chat at a time. A web turn and a Telegram turn for the same athlete
at the same instant are NOT serialised against each other (two processes); both
complete, and the later history write wins.
"""
from __future__ import annotations

import base64
import itertools
import json
import os
import queue
import re
import subprocess
import sys
import threading
from pathlib import Path

CC = Path(os.environ.get("CC_HOME") or Path(__file__).resolve().parent.parent)  # ClaudeCoach/

_SINKS: dict[str, "Sink"] = {}      # chat_id -> the web turn in flight
_SINKS_GUARD = threading.Lock()
_TURN = threading.local()          # .chat_id on the thread running a web turn
_bot = None
_bot_guard = threading.Lock()
_UPLOADS: dict[str, bytes] = {}    # "web:<n>" -> photo bytes, for the length of one turn
_upload_ids = itertools.count(1)

# Set by server.py: called (chat_id, reply_text) when every reply finishes, so it goes
# out as a phone notification. Whether the athlete is still looking can't be told
# reliably from here (Cloudflare holds the connection open after the app closes),
# so the phone decides: the service worker hides it while Peak is on screen.
on_reply_while_away = None


class Busy(Exception):
    pass


class Sink:
    """Collects one web turn's output as (kind, text, extra) events on a queue."""

    def __init__(self):
        self.q: queue.Queue = queue.Queue()
        self.placeholder_id = None
        self._next_id = 1_000_000
        self.last_message = ""
        self.voice_ogg = None
        self.consumer_gone = False
        self.messages: list[tuple[str, object]] = []   # (text, reply_markup) sent this turn
        self.chat_id = None
        self.edit_msg_id = None      # the tapped message, as the bot sees it
        self.edit_item = None        # ... and as Peak knows it (web-outbox id)

    def put(self, kind, text="", **extra):
        self.q.put((kind, text, extra))

    def _id(self):
        self._next_id += 1
        return self._next_id

    def _message(self, text, payload):
        self.last_message = text
        self.messages.append((text, payload.get("reply_markup")))
        self.put("message", text, buttons=_buttons(payload.get("reply_markup")))

    def _edit_tapped(self, text, payload):
        """The bot edited the message whose button was tapped: change it in place."""
        markup = payload.get("reply_markup")
        try:
            import outbox
            outbox.update(self.chat_id, self.edit_item, text, markup)
        except Exception:
            pass
        self.put("edit", text or "", item=self.edit_item, buttons=_buttons(markup))

    def handle(self, method, payload):
        payload = payload or {}
        text = payload.get("text") or ""
        if method == "sendMessage":
            mid = self._id()
            if text == "…" and payload.get("disable_notification") and self.placeholder_id is None:
                self.placeholder_id = mid       # the live status line
                self.put("status", "Thinking…")
            elif text == "_On it..._":          # the photo path's holding line
                self.put("status", "Looking at the photo…")
            elif text:
                self._message(text, payload)
            return {"ok": True, "result": {"message_id": mid}}
        tapped = self.edit_msg_id is not None and payload.get("message_id") == self.edit_msg_id
        if method == "editMessageReplyMarkup" and tapped:
            self._edit_tapped(None, payload)
        if method == "editMessageText" and text:
            if payload.get("message_id") == self.placeholder_id:
                self.put("status", text)
            elif tapped:
                self._edit_tapped(text, payload)
            else:
                self._message(text, payload)
        return {"ok": True, "result": {"message_id": payload.get("message_id") or self._id()}}


def _buttons(reply_markup) -> list:
    try:
        import outbox
        return outbox._buttons(reply_markup)
    except Exception:
        return []


def _sink_for(chat_id):
    return _SINKS.get(str(chat_id or ""))


def _patch(b):
    orig_post, orig_voice = b.tg_post, b.send_voice
    orig_photo, orig_download = b.send_photo, b.download_tg_file
    orig_stream = b.stream_claude

    def tg_post(token, method, payload):
        sink = _sink_for((payload or {}).get("chat_id"))
        return sink.handle(method, payload) if sink else orig_post(token, method, payload)

    def send_photo(token, chat_id, photo_bytes, *a, **k):
        sink = _sink_for(chat_id)
        if not sink:
            return orig_photo(token, chat_id, photo_bytes, *a, **k)
        sink.put("photo", base64.b64encode(photo_bytes).decode())
        try:
            import outbox
            outbox.record(chat_id, "", photo=photo_bytes, source="web-turn")
        except Exception:
            pass
        return {"ok": True}

    def send_voice(token, chat_id, ogg_bytes, *a, **k):
        sink = _sink_for(chat_id)
        if sink:
            sink.voice_ogg = ogg_bytes
            return None
        return orig_voice(token, chat_id, ogg_bytes, *a, **k)

    def download_tg_file(token, file_id):
        if str(file_id).startswith("web:"):
            return _UPLOADS.get(file_id)
        return orig_download(token, file_id)

    def stream_claude(*a, **k):
        sink = _sink_for(getattr(_TURN, "chat_id", None))
        for ev in orig_stream(*a, **k):
            if sink and ev and ev[0] == "chunk" and ev[1]:
                visible = b._telegram_visible(ev[1])
                if visible.strip():
                    sink.put("draft", visible)
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
    b.download_tg_file = download_tg_file
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


_FOOTER_RE = re.compile(r"\n_[^\n]*_\s*$")


def _speech_mp3(b, sink) -> bytes | None:
    """The reply as speech, the way Telegram voice mode makes it (a short spoken
    rewrite, Piper voice), as MP3 so every phone browser can play it."""
    ogg = sink.voice_ogg
    if not ogg:
        reply = _FOOTER_RE.sub("", sink.last_message or "").strip()
        spoken = b._clean_for_speech(b._spoken_rewrite(reply)) if reply else ""
        ogg = b.synthesize_voice(spoken) if spoken else None
    if not ogg:
        return None
    r = subprocess.run(["ffmpeg", "-loglevel", "error", "-i", "pipe:0", "-c:a", "libmp3lame",
                        "-b:a", "48k", "-f", "mp3", "pipe:1"],
                       input=ogg, capture_output=True, timeout=60)
    return r.stdout if r.returncode == 0 and r.stdout else None


_TAPPED_MSG_ID = 900_000_001      # what the bot sees as the tapped message's id


def _history_texts(chat_id) -> tuple[int, list]:
    try:
        import outbox
        slug = outbox.slug_for_chat(chat_id)
        h = json.loads((CC / "athletes" / slug / "telegram" / "history.json").read_text())
        return len(h), h
    except Exception:
        return 0, []


def _keep_unsaved(chat_id, sink, hist_before) -> None:
    """Anything the coach said this turn that history.json didn't keep (a logged-it
    confirmation, a follow-up question with buttons) goes to the web outbox, so it is
    still there when the chat reloads. Recorded quietly: the athlete is looking."""
    if not sink.messages:
        return
    _, after = _history_texts(chat_id)
    saved = {(e.get("assistant") or "").strip() for e in after[-6:] if isinstance(e, dict)}
    try:
        import outbox
        for text, markup in sink.messages:
            core = _FOOTER_RE.sub("", text).strip()
            if core and not any(core == s or core.startswith(s) or s.startswith(core)
                                for s in saved if s):
                outbox.record(chat_id, text, markup, source="web-turn")
    except Exception:
        pass


def start_turn(chat_id: str, label: str = "", text: str = "", audio: bytes | None = None,
               image: bytes | None = None, button: str | None = None,
               item: str | None = None) -> Sink:
    """Run one web message through the Telegram coach on a background thread: text, a
    voice recording (transcribed first, and the reply spoken back), a photo with `text`
    as its caption, or a button tap (`button` = its callback data). `chat_id` is the
    athlete's (or, mid-signup, their web chat id). Returns the Sink its events arrive
    on; the last event is always ('done', '')."""
    b = bot()
    chat_id = str(chat_id)
    label = label or chat_id
    sink = Sink()
    sink.chat_id = chat_id
    if button is not None and item:
        sink.edit_msg_id, sink.edit_item = _TAPPED_MSG_ID, item
    with _SINKS_GUARD:
        if chat_id in _SINKS:
            raise Busy(label)
        _SINKS[chat_id] = sink

    def work():
        _TURN.chat_id = chat_id
        upload_id = None
        hist_before = _history_texts(chat_id)[0]
        try:
            config = b.load_config()
            token = config["bot_token"]
            athletes = b.load_athletes()
            said = text
            if audio is not None:
                said = b.transcribe_voice(audio) or ""
                if not said:
                    sink.put("error", "Sorry, I couldn't make that out. Try again?")
                    return
                sink.put("heard", said)
            if button is not None:
                b.log(f"[{label}] Tap (web): {button[:60]}")
                if not b.dispatch_callback(token, chat_id, button, sink.edit_msg_id, athletes, config):
                    b._route_text(token, chat_id, button, athletes, config)
            elif image is not None:
                if chat_id not in athletes:
                    sink.put("error", "Photos work once your account is active.")
                    return
                upload_id = f"web:{next(_upload_ids)}"
                _UPLOADS[upload_id] = image
                b.log(f"[{label}] In (web photo): {said[:80]}")
                with b._chat_lock(chat_id):
                    b._image_reply_worker(token, chat_id, upload_id, said, athletes[chat_id], config)
            else:
                b.log(f"[{label}] In (web{' voice' if audio is not None else ''}): {said[:80]}")
                b._route_text(token, chat_id, said, athletes, config)
            if audio is not None and (sink.last_message or sink.voice_ogg):
                sink.put("status", "Recording the reply…")
                mp3 = _speech_mp3(b, sink)
                if mp3:
                    sink.put("audio", base64.b64encode(mp3).decode())
        except Exception as e:
            b.log(f"[{label}] web turn error: {e}")
            sink.put("error", "Sorry - I hit a snag answering that. Give it another go in a moment.")
        finally:
            _keep_unsaved(chat_id, sink, hist_before)
            if upload_id:
                _UPLOADS.pop(upload_id, None)
            _TURN.chat_id = None
            with _SINKS_GUARD:
                _SINKS.pop(chat_id, None)
            if sink.last_message and on_reply_while_away:
                try:
                    on_reply_while_away(chat_id, sink.last_message)
                except Exception:
                    pass
            sink.put("done")

    threading.Thread(target=work, name=f"web-chat-{label}", daemon=True).start()
    return sink


# The questions bot._handle_drill asks on the athlete's behalf (older entries predate
# kind="drill", so they are recognised by their opening words).
_DRILL_PREFIXES = ("Analyse the interval structure of activity", "Review the nutrition for activity",
                   "Analyse the heart rate data for activity", "Find the 3 most similar past sessions to activity")


def timeline(slug: str | None, limit: int = 80) -> list[dict]:
    """The chat as Peak shows it, oldest first: the shared Telegram/web conversation
    (history.json) merged with every scheduled coach message (web-outbox.jsonl, which
    carries buttons and photos). A scheduled message is ALSO in history.json with no
    user side; that copy is dropped when the outbox has the same text."""
    if not slug:
        return []
    adir = CC / "athletes" / slug
    try:
        hist = json.loads((adir / "telegram" / "history.json").read_text())
    except (OSError, ValueError):
        hist = []
    out = []
    try:
        for line in (adir / "web-outbox.jsonl").read_text().splitlines()[-limit:]:
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
    except OSError:
        pass
    out_texts = {(o.get(k) or "").strip() for o in out for k in ("text", "orig_text") if o.get(k)}

    items, last_ts = [], ""
    for e in hist if isinstance(hist, list) else []:
        if not isinstance(e, dict):
            continue
        ts = e.get("ts") or last_ts        # older entries carry no time; keep their order
        last_ts = ts
        user, coach = e.get("user") or "", e.get("assistant") or ""
        if e.get("kind") == "drill" or user.startswith(_DRILL_PREFIXES):
            user = ""                       # a button's own question, not something they wrote
        if user:
            items.append({"who": "me", "text": ("📷 " if e.get("kind") == "image" else "") + user,
                          "ts": ts})
        if coach and (user or e.get("kind") == "drill" or coach.strip() not in out_texts):
            items.append({"who": "coach", "text": coach, "ts": ts})
    for o in out:
        items.append({"who": "coach", "text": o.get("text") or "", "ts": o.get("ts") or "",
                      "buttons": o.get("buttons") or [], "photo": o.get("photo"),
                      "id": o.get("id")})
    items.sort(key=lambda i: i["ts"] or "")     # stable: equal times keep history order
    return items[-limit:]
