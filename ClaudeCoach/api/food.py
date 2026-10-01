"""Peak's Food tab chat (Jamie, 1 Oct 2026): the FOOD bot's own brain, not the coach's.

telegram/nutrition_bot.py is imported and its handlers run unchanged; only the
transport is swapped, the way api/chat.py does it for the coach:
  - lib/tg.send / tg.post called from a web turn's thread go to that turn's Sink
    (any other caller in this process gets the real functions)
  - download_photo returns the uploaded image instead of fetching it from Telegram
  - a voice note is transcribed with the coach bot's Whisper, then handled as text

The conversation is kept in athletes/<slug>/food-chat.jsonl, both sides, separate from
the coach's chat. The Telegram food bot keeps running until Jamie switches it off; both
share the food store and its persisted pending confirmation, so "Log it" works from
either side.
"""
from __future__ import annotations

import json
import os
import queue
import secrets
import sys
import tempfile
import threading
from datetime import datetime
from pathlib import Path

CODE = Path(__file__).resolve().parent.parent                                   # the code
CC = Path(os.environ.get("CC_HOME") or CODE)                                     # the data
CHAT_NAME = "food-chat.jsonl"
MEDIA_DIR = "web-media"
KEEP_LINES = 400

_nb = None
_guard = threading.Lock()
_ctx: dict = {}
_busy: set = set()
_busy_guard = threading.Lock()
_TURN = threading.local()


class Busy(Exception):
    pass


class Sink:
    def __init__(self):
        self.q: queue.Queue = queue.Queue()
        self.sent: list = []            # (text, reply_markup)
        self._n = 2_000_000

    def put(self, kind, text="", **extra):
        self.q.put((kind, text, extra))

    def message(self, text, markup=None):
        if not text:
            return
        self.sent.append((text, markup))
        self.put("message", text, buttons=_buttons(markup))

    def next_id(self):
        self._n += 1
        return self._n


def _buttons(markup) -> list:
    try:
        import outbox
        return outbox._buttons(markup)
    except Exception:
        return []


def nb():
    """nutrition_bot, imported once, with its Telegram transport swapped for web turns."""
    global _nb
    with _guard:
        if _nb is not None:
            return _nb
        for p in (CODE / "telegram", CODE / "lib"):
            if str(p) not in sys.path:
                sys.path.insert(0, str(p))
        import nutrition_bot as m
        import tg
        orig_send, orig_post, orig_download = tg.send, tg.post, m.download_photo

        def send(token, chat_id, text, reply_markup=None, parse_mode="Markdown", log=print):
            sink = getattr(_TURN, "sink", None)
            if sink is None:
                return orig_send(token, chat_id, text, reply_markup=reply_markup,
                                 parse_mode=parse_mode, log=log)
            sink.message(text, reply_markup)
            return {"ok": True, "result": {"message_id": sink.next_id()}}

        def post(token, method, payload, log=print, timeout=10):
            sink = getattr(_TURN, "sink", None)
            if sink is None:
                return orig_post(token, method, payload, log=log, timeout=timeout)
            if method == "sendMessage":
                sink.message((payload or {}).get("text"), (payload or {}).get("reply_markup"))
                return {"ok": True, "result": {"message_id": sink.next_id()}}
            return {"ok": True, "result": {}}

        def download_photo(ctx, file_id, token):
            if str(file_id).startswith("web:"):
                return getattr(_TURN, "photo_path", None)
            return orig_download(ctx, file_id, token)

        tg.send, tg.post, m.download_photo = send, post, download_photo
        _nb = m
        return m


def enabled(slug: str) -> bool:
    try:
        return bool(json.loads((CC / "athletes" / slug / "profile.json").read_text())
                    .get("nutrition_tracker"))
    except (OSError, ValueError):
        return False


def _context(slug: str):
    m = nb()
    if slug not in _ctx:
        cfg = dict(m.load_config())
        cfg["athlete"] = slug
        _ctx[slug] = (m.Context(cfg), cfg["bot_token"])
    return _ctx[slug]


# ── the conversation, as Peak shows it ──

def _file(slug: str) -> Path:
    return CC / "athletes" / slug / CHAT_NAME


def _record(slug: str, who: str, text: str = "", buttons=None, photo: str | None = None) -> None:
    f = _file(slug)
    f.parent.mkdir(parents=True, exist_ok=True)
    entry = {"id": datetime.now().strftime("%Y%m%d%H%M%S") + "-" + secrets.token_hex(3),
             "ts": datetime.now().isoformat(timespec="seconds"), "who": who,
             "text": text or "", "buttons": buttons or [], "photo": photo}
    with open(f, "a") as fh:
        fh.write(json.dumps(entry) + "\n")
    try:
        lines = f.read_text().splitlines()
        if len(lines) > 2 * KEEP_LINES:
            f.write_text("\n".join(lines[-KEEP_LINES:]) + "\n")
    except OSError:
        pass


def _clear_buttons(slug: str) -> None:
    """A newer message answers the last offer: old Log it / No buttons go."""
    f = _file(slug)
    try:
        rows = [json.loads(l) for l in f.read_text().splitlines() if l.strip()]
    except (OSError, ValueError):
        return
    if any(r.get("buttons") for r in rows):
        for r in rows:
            r["buttons"] = []
        tmp = f.with_suffix(".tmp")
        tmp.write_text("".join(json.dumps(r) + "\n" for r in rows))
        tmp.replace(f)


def history(slug: str, limit: int = 80) -> list:
    try:
        rows = [json.loads(l) for l in _file(slug).read_text().splitlines()[-limit:] if l.strip()]
    except (OSError, ValueError):
        return []
    return [{"who": r.get("who"), "text": r.get("text") or "", "ts": r.get("ts"),
             "buttons": r.get("buttons") or [], "photo": r.get("photo")} for r in rows]


# ── one web turn ──

_TAPS = {"confirm": "Log it", "cancel": "No"}


def start_turn(slug: str, text: str = "", image: bytes | None = None,
               audio: bytes | None = None, button: str | None = None) -> Sink:
    with _busy_guard:
        if slug in _busy:
            raise Busy(slug)
        _busy.add(slug)
    sink = Sink()

    def work():
        _TURN.sink = sink
        tmp = None
        try:
            m = nb()
            ctx, token = _context(slug)
            chat_id = f"peak-food-{slug}"
            said = text
            _clear_buttons(slug)
            if audio is not None:
                from chat import bot as coach_bot
                said = coach_bot().transcribe_voice(audio) or ""
                if not said:
                    sink.put("error", "Sorry, I couldn't make that out. Try again?")
                    return
                sink.put("heard", said)
            if button in _TAPS:
                _record(slug, "me", _TAPS[button])
                pend = m.get_pending(ctx.store)
                m.set_inbound(ctx, "[tapped Log it]" if button == "confirm" else "[tapped No]")
                if button == "confirm" and pend:
                    m.commit_pending(ctx, pend, ctx.local_today(), token, chat_id)
                elif button == "confirm":
                    sink.message("There's nothing waiting to log.")
                else:
                    m.clear_pending(ctx.store)
                    sink.message("Dropped it.")
            elif image is not None:
                name = datetime.now().strftime("%Y%m%d%H%M%S") + "-" + secrets.token_hex(3) + ".png"
                media = CC / "athletes" / slug / MEDIA_DIR
                media.mkdir(parents=True, exist_ok=True)
                (media / name).write_bytes(image)
                _record(slug, "me", said, photo=name)
                fd, tmp = tempfile.mkstemp(prefix="nut-web-", suffix=".jpg")
                with os.fdopen(fd, "wb") as fh:
                    fh.write(image)
                _TURN.photo_path = Path(tmp)
                m.handle_photo(ctx, "web:1", said or "", ctx.local_today(), token, chat_id)
            else:
                _record(slug, "me", ("🎙 " if audio is not None else "") + said)
                m.handle_text(ctx, said, token, chat_id)
        except Exception as e:
            try:
                nb().log(f"[{slug}] web food turn failed: {type(e).__name__}: {e}")
            except Exception:
                pass
            sink.put("error", "Something went wrong logging that. Nothing was saved.")
        finally:
            for t, markup in sink.sent:
                _record(slug, "bot", t, _buttons(markup))
            if tmp and os.path.exists(tmp):
                os.unlink(tmp)
            _TURN.sink = None
            _TURN.photo_path = None
            with _busy_guard:
                _busy.discard(slug)
            sink.put("done")

    threading.Thread(target=work, name=f"food-{slug}", daemon=True).start()
    return sink
