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
  send_voice    kept for web turns (voice mode's own audio) and played in Peak.
  download_tg_file  a "web:<n>" file id returns the photo Peak uploaded, so a web photo
                goes through the bot's own image path (_image_reply_worker) unchanged.
  _submit       runs the reply worker on THIS thread (under the same per-chat lock)
                so its events stream back to this request.

One web turn per athlete at a time. A web turn and a Telegram turn for the same
athlete at the same instant are NOT serialised against each other (two processes);
both would complete, and the later history write wins.
"""
from __future__ import annotations

import base64
import itertools
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


class Busy(Exception):
    pass


class Sink:
    """Collects one web turn's output as (kind, text) events on a queue."""

    def __init__(self):
        self.q: queue.Queue = queue.Queue()
        self.placeholder_id = None
        self._next_id = 1_000_000
        self.last_message = ""
        self.voice_ogg = None

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
            elif text == "_On it..._":          # the photo path's holding line
                self.put("status", "Looking at the photo…")
            elif text:
                self.last_message = text
                self.put("message", text)
            return {"ok": True, "result": {"message_id": mid}}
        if method == "editMessageText" and text:
            if payload.get("message_id") == self.placeholder_id:
                self.put("status", text)
            else:
                self.last_message = text
                self.put("message", text)
        return {"ok": True, "result": {"message_id": payload.get("message_id") or self._id()}}


def _sink_for(chat_id):
    return _SINKS.get(str(chat_id or ""))


def _patch(b):
    orig_post, orig_photo, orig_voice = b.tg_post, b.send_photo, b.send_voice
    orig_download = b.download_tg_file
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


def chat_id_for(slug: str) -> str | None:
    for cid, a in bot().load_athletes().items():
        if a.get("slug") == slug:
            return str(cid)
    return None


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


def start_turn(slug: str, text: str = "", audio: bytes | None = None,
               image: bytes | None = None) -> Sink:
    """Run one web message through the Telegram coach on a background thread: text, a
    voice recording (transcribed first, and the reply spoken back), or a photo with
    `text` as its caption. Returns the Sink its events arrive on; the last event is
    always ('done', '')."""
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
        upload_id = None
        try:
            config = b.load_config()
            token = config["bot_token"]
            said = text
            if audio is not None:
                said = b.transcribe_voice(audio) or ""
                if not said:
                    sink.put("error", "Sorry, I couldn't make that out. Try again?")
                    return
                sink.put("heard", said)
            if image is not None:
                upload_id = f"web:{next(_upload_ids)}"
                _UPLOADS[upload_id] = image
                b.log(f"[{slug}] In (web photo): {said[:80]}")
                with b._chat_lock(chat_id):
                    b._image_reply_worker(token, chat_id, upload_id, said, athletes[chat_id], config)
            else:
                b.log(f"[{slug}] In (web{' voice' if audio is not None else ''}): {said[:80]}")
                b._route_text(token, chat_id, said, athletes, config)
            if audio is not None and (sink.last_message or sink.voice_ogg):
                sink.put("status", "Recording the reply…")
                mp3 = _speech_mp3(b, sink)
                if mp3:
                    sink.put("audio", base64.b64encode(mp3).decode())
        except Exception as e:
            b.log(f"[{slug}] web turn error: {e}")
            sink.put("error", "Sorry - I hit a snag answering that. Give it another go in a moment.")
        finally:
            if upload_id:
                _UPLOADS.pop(upload_id, None)
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
