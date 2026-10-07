"""Dev chat: Peak's second chat tab, for building the system rather than coaching.

Jamie, 4 Oct 2026: "2 tabs in my chat so i can have an 'athlete chat' and a second one
for the dev i do from the app ... the second chat also has the power to restart the VM
and API on demand." Coach only.

Unlike api/chat.py this does NOT go through telegram/bot.py: no coaching prompt, no
athlete context, no history.json. It is the claude CLI working in the repo (CLAUDE.md
applies, as in any dev session) with its own resumed session and its own history:

    athletes/<slug>/dev-chat/history.jsonl   every message, append-only
    athletes/<slug>/dev-chat/session.json    the CLI session the next message resumes

Restarts are done by this process, never by the model: the chat CLI is barred from
systemctl (lib/engine.py DISALLOWED_TOOLS) because restarting the service from inside
it kills the reply mid-sentence. The model ends a reply with <restart>api</restart> or
<restart>vm</restart> instead. An API restart is scheduled once the reply is saved; a
VM reboot only ever comes from a tap (the reply carries a confirm button). Both run as
a transient systemd unit, outside the API's own cgroup, through
scripts/api-nightly-restart.py, which first waits for athletes' replies in flight.
"""
from __future__ import annotations

import json
import os
import queue
import re
import subprocess
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

CC = Path(os.environ.get("CC_HOME") or Path(__file__).resolve().parent.parent)   # ClaudeCoach/
MODEL = "opus"
MAX_SECS = 45 * 60            # a dev task can run long; a hung one is stopped here
CONTEXT_ITEMS = 8             # recent messages handed to a fresh session
RESTART_WAIT = 300            # how long a restart waits for athletes' replies in flight

_RESTART_RE = re.compile(r"\s*<restart>\s*(api|vm)\s*</restart>\s*", re.I)
VM_BUTTON = [[{"text": "Restart the VM now", "data": "dev:restart:vm"}]]
# A choice as buttons, not typing (Jamie, 7 Oct 2026: "give me buttons and an other
# selection rather than making me type"). Peak sends a tapped option back as his message
# (data "dev:say:<text>"); "Other" opens the keyboard.
_OPTIONS_RE = re.compile(r"\s*<options>(.*?)</options>\s*", re.I | re.S)
OTHER_BUTTON = {"text": "Other…", "data": "dev:other"}

DEV_PROMPT = """\
You are in the Dev tab of Peak, Jamie's coaching web app. This chat is for building and
fixing ClaudeCoach, Peak and the Diamond Peak site. It is separate from his athlete
coaching chat: do not coach him here. You are on the production VM, in the repo, and
CLAUDE.md applies (including how to write to Jamie). Replies render as Markdown in Peak.

Restarts: you cannot run systemctl, reboot or kill yourself. When Jamie asks you to
restart the API (claudecoach-api), or agrees to it, end your reply with
<restart>api</restart>. Peak restarts it a few seconds after your reply is delivered,
once athletes' replies in flight have finished (about a second of downtime). When he asks
to restart or reboot the VM, end your reply with <restart>vm</restart>: he gets a confirm
button. Never emit either unless he asked or agreed in this conversation.

Choices: whenever you ask Jamie to pick between options, end the reply with
<options>1. Short label|2. Short label|3. Short label</options> (2 to 5 options, each
under 40 characters, matching the numbered options in your reply). Peak shows them as
buttons plus an "Other" button, so he taps instead of typing; a tap arrives as that
label as his message. Only for a real choice, never on a reply that asks nothing."""

# Set by server.py: called (slug, reply_text) when a reply finishes, for a phone
# notification (the service worker hides it while Peak is on screen).
on_reply = None

_RUNNING: dict[str, "Sink"] = {}
_GUARD = threading.Lock()


class Busy(Exception):
    pass


class Sink:
    """One dev turn's events, (kind, text, extra), as api/chat.py's Sink sends them."""

    def __init__(self):
        self.q: queue.Queue = queue.Queue()
        self.status = "Thinking…"
        self.draft = ""

    def put(self, kind, text="", **extra):
        if kind == "status":
            self.status = text
        elif kind == "draft":
            self.draft = text
        self.q.put((kind, text, extra))


def _dir(slug: str) -> Path:
    return CC / "athletes" / slug / "dev-chat"


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _append(slug: str, who: str, text: str, **extra) -> dict:
    d = _dir(slug)
    d.mkdir(parents=True, exist_ok=True)
    e = {"id": uuid.uuid4().hex[:12], "ts": _now(), "who": who, "text": text, **extra}
    with open(d / "history.jsonl", "a") as fh:
        fh.write(json.dumps(e) + "\n")
    return e


def history(slug: str, limit: int = 200) -> list[dict]:
    """This chat's own messages, oldest first."""
    try:
        lines = (_dir(slug) / "history.jsonl").read_text().splitlines()
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except ValueError:
            pass
    out.sort(key=lambda e: e.get("ts") or "")       # stable
    return out[-limit:]


def state(slug: str, limit: int = 200) -> dict:
    """What the Dev tab shows: its own messages merged with the Dev messages from the
    shared chat (overnight bug fixes and the taps on them, and the dev talk from before
    this tab existed: api/chat.py is_dev), plus the turn still running, if any, so
    reopening Peak mid-task shows it working rather than an unanswered message."""
    items = history(slug, limit)
    try:
        import chat
        items = sorted(items + chat.timeline(slug, limit, tab="dev"), key=lambda e: e.get("ts") or "")
    except Exception:
        pass
    sink = _RUNNING.get(slug)
    return {"history": items[-limit:], "busy": sink is not None,
            "status": sink.status if sink else "", "draft": sink.draft if sink else ""}


def _load_session(slug: str) -> str | None:
    try:
        return json.loads((_dir(slug) / "session.json").read_text()).get("session_id")
    except (OSError, ValueError):
        return None


def _save_session(slug: str, sid: str | None) -> None:
    f = _dir(slug) / "session.json"
    if sid:
        f.write_text(json.dumps({"session_id": sid, "saved": _now()}))
    else:
        f.unlink(missing_ok=True)


def _fresh_prompt(slug: str, text: str) -> str:
    """A new session has no memory of this chat: give it the last few messages."""
    recent = [e for e in state(slug, CONTEXT_ITEMS + 1)["history"]
              if e.get("who") in ("me", "coach")][:-1]
    if not recent:
        return text
    lines = [("Jamie: " if e["who"] == "me" else "You: ") + (e.get("text") or "")[:1500]
             for e in recent[-CONTEXT_ITEMS:]]
    return ("Earlier in this Dev chat (for context):\n\n" + "\n\n".join(lines) +
            "\n\n---\n\nJamie's new message:\n\n" + text)


def split_options(text: str) -> tuple[str, list]:
    """(reply without the marker, button rows). The last marker wins; none -> []."""
    found = _OPTIONS_RE.findall(text or "")
    body = _OPTIONS_RE.sub("\n", text or "").strip()
    if not found:
        return body, []
    opts = [o.strip()[:80] for o in found[-1].split("|") if o.strip()][:6]
    if not opts:
        return body, []
    return body, [[{"text": o, "data": "dev:say:" + o}] for o in opts] + [[OTHER_BUTTON]]


def split_restart(text: str) -> tuple[str, str | None]:
    """(reply without the marker, "api" / "vm" / None). The last marker wins."""
    found = _RESTART_RE.findall(text or "")
    return _RESTART_RE.sub("\n", text or "").strip(), (found[-1].lower() if found else None)


def _run(engine, prompt: str, sid: str | None, sink: Sink, run) -> tuple:
    extra = ["--append-system-prompt", DEV_PROMPT] + (["--resume", sid] if sid else [])
    env = {**os.environ, "CC_TURN_ID": uuid.uuid4().hex}
    gen = engine._stream_once(prompt, MODEL, extra, str(CC.parent), env=env, run=run)
    while True:
        try:
            ev = next(gen)
        except StopIteration as stop:
            return stop.value
        if ev[0] == "chunk" and ev[1]:
            sink.put("draft", split_options(split_restart(ev[1])[0])[0])
        elif ev[0] == "status":
            sink.put("status", " · ".join(x for x in ev[1:] if x)[:140])


def start_turn(slug: str, text: str) -> Sink:
    import engine                          # lib/, on sys.path (server.py)
    sink = Sink()
    with _GUARD:
        if slug in _RUNNING:
            raise Busy(slug)
        _RUNNING[slug] = sink
    _append(slug, "me", text)

    def work():
        run = engine._register_run(engine.new_run_id(), owner=f"dev:{slug}")
        timer = threading.Timer(MAX_SECS, engine.cancel_run, args=(run.run_id,))
        timer.daemon = True
        timer.start()
        reply, restart = "", None
        try:
            sink.put("status", "Thinking…")
            sid = _load_session(slug)
            prompt = text if sid else _fresh_prompt(slug, text)
            final, streamed, new_sid, rc, _, _ = _run(engine, prompt, sid, sink, run)
            if sid and not (final or streamed) and not run.cancelled:
                _save_session(slug, None)       # the session is gone: start a new one
                final, streamed, new_sid, rc, _, _ = _run(
                    engine, _fresh_prompt(slug, text), None, sink, run)
            reply, restart = split_restart(final or streamed or "")
            reply, option_rows = split_options(reply)
            if run.cancelled:
                reply = (reply + "\n\n" if reply else "") + \
                    f"_Stopped after {MAX_SECS // 60} minutes._"
            if not reply:
                raise RuntimeError(f"no reply (exit {rc})")
            _save_session(slug, new_sid)
            rows = option_rows + (VM_BUTTON if restart == "vm" else [])
            extra = {"buttons": rows} if rows else {}
            _append(slug, "coach", reply, **extra)
            sink.put("message", reply, **extra)
            if restart == "api":
                ok, msg = restart_now(slug, "api")
                sink.put("message" if ok else "error", msg)
        except Exception as e:
            engine.log(f"[dev:{slug}] turn error: {e}")
            msg = "Sorry, that didn't finish. Try again in a moment."
            _append(slug, "coach", msg, error=True)
            sink.put("error", msg)
        finally:
            timer.cancel()
            engine._deregister_run(run)
            with _GUARD:
                _RUNNING.pop(slug, None)
            if reply and on_reply:
                try:
                    on_reply(slug, reply)
                except Exception:
                    pass
            sink.put("done")

    threading.Thread(target=work, name=f"dev-chat-{slug}", daemon=True).start()
    return sink


RESTART_SCRIPT = CC / "scripts" / "api-nightly-restart.py"
RESTART_LOG = Path("/root/Library/Logs/ClaudeCoach/api-restart.log")
SAID = {"api": "Restarting the API. Peak is back in a few seconds.",
        "vm": "Restarting the VM once athletes' replies in flight finish. "
              "Peak is back about a minute after."}


def restart_cmd(what: str) -> list[str]:
    """A transient systemd unit, so the restart outlives this process."""
    cmd = ["systemd-run", f"--unit=peak-dev-{what}-{int(time.time())}", "--on-active=3",
           "--timer-property=AccuracySec=1s", "--collect",
           f"--property=StandardOutput=append:{RESTART_LOG}",
           f"--property=StandardError=append:{RESTART_LOG}",
           "/usr/bin/python3", str(RESTART_SCRIPT), "--max-wait", str(RESTART_WAIT)]
    return cmd + (["--reboot"] if what == "vm" else [])


def restart_now(slug: str, what: str) -> tuple[bool, str]:
    """Schedule an API restart or VM reboot. (ok, what to tell Jamie)."""
    if what not in SAID:
        return False, "Restart what? Only the API or the VM."
    try:
        r = subprocess.run(restart_cmd(what), capture_output=True, text=True, timeout=20)
        ok, err = r.returncode == 0, (r.stderr or r.stdout).strip()
    except Exception as e:
        ok, err = False, str(e)
    msg = SAID[what] if ok else f"Couldn't start the restart: {err[:200]}"
    _append(slug, "coach", msg, **({} if ok else {"error": True}))
    return ok, msg
