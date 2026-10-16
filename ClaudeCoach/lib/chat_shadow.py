#!/usr/bin/env python3
"""Chat shadow: which model each chat message needs, judged side by side, never sent.

Jamie, 16 Oct 2026, on chat cost: "1 yes" to a model picker - Haiku reads each message
and picks Haiku for simple ones, Sonnet by default, Opus only for plan changes or
pushback - judged on ~20 real messages side by side before anything goes live. And on
sending Opus's share of chat to Sonnet: "undecided, more shadowing required". This runs
both, and changes nothing an athlete sees:

after every real chat reply, launch() starts a DETACHED process that
  1. asks the picker (Haiku, PICKER_PROMPT) which model the message needs, and times it
  2. writes the reply again on the picker's model when that is not the model that
     answered, and on Sonnet whenever Opus answered
from the same fresh prompt (engine._assemble), read-only: Read/Grep and the two fetchers
only, with CC_READ_ONLY=1 so icu_fetch refuses every write. Saved next to the real reply
in SHADOW_DIR/<date>/chat-<slug>-<time>.json. On Max, like all dev work: a shadow run is
never given the API key.

When READY_AT pairs have been written, Jamie's Dev tab is told once, so they get reviewed.

    python3 ClaudeCoach/lib/chat_shadow.py --report [--days 7]
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from datetime import date, datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent             # ClaudeCoach/
SHADOW_DIR = Path.home() / "Library/Logs/ClaudeCoach/chat-shadow"
SHADOW_UNTIL = date(2026, 10, 26)       # ~20 differing pairs at ~25 athlete messages a week
DAILY_CAP = 30                          # launches a day, all athletes: bounds the cost
READY_AT = 20                           # pairs that differ, then Jamie's Dev tab is told
TOOLS = ("Read,Grep,Bash(python3 ClaudeCoach/lib/icu_fetch.py:*),"
         "Bash(python3 ClaudeCoach/lib/strava_fetch.py:*)")
MODELS = ("haiku", "sonnet", "opus")

PICKER_PROMPT = """\
You choose which Claude model answers an athlete's message to their AI endurance coach.
Reply with JSON only: {{"model": "haiku" | "sonnet" | "opus", "why": "<under 12 words>"}}

haiku - the message needs no coaching judgement: logging or confirming something (a
weight, how a session felt, "done"), thanks or an acknowledgement, a short factual
question answered by looking at their data or plan (what is tomorrow, what was my load),
or a plain answer to the coach's own question that needs no reasoning.
sonnet - the default: a coaching question that needs judgement, an explanation,
adjusting or swapping one session, fuelling or kit advice, how training is going, or
anything you are unsure about.
opus - only when the message asks to change the plan beyond one session (rebuild or
rebalance a week, move a race, add a block, a taper), reports illness or an injury that
changes training, disagrees with or corrects the coach, or asks for Opus by name.

Read short replies through the recent exchange: "yes do it" after the coach proposed a
new week is a plan change; "yes" to "did the session happen?" is logging.

Recent exchange:
{recent}

Athlete's message:
{message}
"""

# The reply must read as it would be sent, or the side-by-side compares a caveat with a
# reply (16 Oct check: "This is a read-only run, so here's what I'd change" in the reply).
TWIN_NOTE = ("SHADOW RE-RUN, READ-ONLY: this reply is for a side-by-side review and is "
             "never sent. You cannot change files, the plan or the calendar here. Write the "
             "reply exactly as you would send it, as if any change you would make had been "
             "made, never mentioning this note. Then, only if you would have changed "
             "something, add one last line outside the reply: WOULD CHANGE: <what>. "
             "Everything else applies as written.\n\n")


def model_key(model: str) -> str:
    m = str(model or "").lower()
    return next((k for k in MODELS if k in m), m)


def twins(real: str, picked: str | None) -> list[str]:
    """Models to write the reply on again: the picker's, if it differs from the one that
    answered, and Sonnet whenever Opus answered (the Opus -> Sonnet question)."""
    real, out = model_key(real), []
    if picked in MODELS and picked != real:
        out.append(picked)
    if real == "opus" and "sonnet" not in out:
        out.append("sonnet")
    return out


def parse_pick(text: str) -> tuple[str | None, str]:
    m = re.search(r"\{.*\}", text or "", re.S)
    try:
        d = json.loads(m.group()) if m else {}
    except ValueError:
        d = {}
    model = str(d.get("model") or "").lower().strip()
    return (model if model in MODELS else None), str(d.get("why") or "")[:120]


def _recent(history: list, n: int = 3) -> str:
    lines = []
    for h in (history or [])[-n:]:
        if h.get("user"):
            lines.append("athlete: " + str(h["user"])[:300].replace("\n", " "))
        if h.get("assistant"):
            lines.append("coach: " + str(h["assistant"])[:300].replace("\n", " "))
    return "\n".join(lines) or "(none)"


def active(today: date | None = None) -> bool:
    # Never under a test run: engine's chat tests drive fake replies, and a launch would
    # spawn a real `claude` in the background on whatever machine runs them.
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return False
    return (today or date.today()) <= SHADOW_UNTIL


def launch(user_message: str, history: list, sp_file, athlete_name: str, context: str,
           model: str, reply: str, cost: float | None = None) -> None:
    """Start the shadow of a chat turn that has just been answered. Never raises, never
    waits: the athlete's reply is already out."""
    try:
        if not active() or not (user_message or "").strip() or not (reply or "").strip():
            return
        now = datetime.now()
        d = SHADOW_DIR / now.date().isoformat()
        d.mkdir(parents=True, exist_ok=True)
        if len(list(d.glob("chat-*.json"))) >= DAILY_CAP:
            return
        slug = Path(sp_file).parent.name
        f = d / f"chat-{slug}-{now.strftime('%H%M%S')}.json"
        f.write_text(json.dumps({
            "slug": slug, "ts": now.isoformat(timespec="seconds"),
            "message": user_message, "history": history or [], "sp_file": str(sp_file),
            "athlete_name": athlete_name, "context": context or "",
            "real": {"model": model_key(model), "reply": reply,
                     "cost": round(cost, 4) if cost is not None else None},
            "pick": None, "twins": {}}, default=str))
        env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
        subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--run", str(f)],
                         env=env, cwd=str(BASE.parent), start_new_session=True,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
    except Exception:
        pass


def _ask(prompt: str, model: str, slug: str, tools: str, timeout: int, env: dict) -> dict:
    import claude_call
    t0 = time.time()
    r = claude_call.run_claude(prompt, model=model, fallback=[], allowed_tools=tools,
                               cwd=str(BASE.parent), timeout=timeout,
                               no_session_persistence=True, label=f"chat-shadow:{slug}",
                               extra_args=["--output-format", "json"], env=env)
    out = {"secs": round(time.time() - t0, 1), "rc": r.returncode}
    try:
        d = json.loads(r.stdout or "")
        out.update(text=(d.get("result") or "").strip(), cost=d.get("total_cost_usd"))
    except ValueError:
        out.update(text=(r.stdout or "").strip(), cost=None)
    return out


def _run(path: str) -> None:
    sys.path.insert(0, str(BASE / "lib"))
    f = Path(path)
    rec = json.loads(f.read_text())
    slug = rec["slug"]
    env = {**os.environ, "CC_ATHLETE_SCOPE": slug, "CC_READ_ONLY": "1"}
    env.pop("ANTHROPIC_API_KEY", None)
    try:
        p = _ask(PICKER_PROMPT.format(recent=_recent(rec["history"]), message=rec["message"]),
                 "haiku", slug, "", 60, env)
        model, why = parse_pick(p.get("text", ""))
        rec["pick"] = {"model": model, "why": why, "secs": p["secs"], "cost": p.get("cost")}
        f.write_text(json.dumps(rec, default=str))
        import engine
        prompt = TWIN_NOTE + engine._assemble(rec["message"], rec["history"], rec["sp_file"],
                                              rec["athlete_name"], rec["context"])
        for m in twins(rec["real"]["model"], model):
            rec["twins"][m] = _ask(prompt, m, slug, TOOLS, 400, env)
            f.write_text(json.dumps(rec, default=str))
    except Exception as e:
        rec["error"] = f"{type(e).__name__}: {e}"
        f.write_text(json.dumps(rec, default=str))
    _maybe_tell()


def _records(days: int | None = None) -> list[dict]:
    dirs = sorted(SHADOW_DIR.glob("*"))
    out = []
    for d in (dirs[-days:] if days else dirs):
        for f in sorted(d.glob("chat-*.json")):
            try:
                out.append(json.loads(f.read_text()))
            except (OSError, ValueError):
                pass
    return out


def _maybe_tell() -> None:
    """Tell Jamie's Dev tab once, when READY_AT pairs are in. Never raises."""
    try:
        flag = SHADOW_DIR / ".told"
        pairs = [r for r in _records() if r.get("twins")]
        if flag.exists() or len(pairs) < READY_AT:
            return
        flag.write_text(datetime.now().isoformat(timespec="seconds"))
        import outbox
        cid = json.loads((BASE / "config" / "athletes.json").read_text())["jamie"]["chat_id"]
        outbox.record(cid, f"*Chat shadow ready:* {len(pairs)} real messages answered side by "
                           "side - the picker's model against the one that answered, and "
                           "Sonnet against Opus.\n\nAsk me here to *review the chat shadow*.",
                      source="dev-session", tab="dev")
    except Exception:
        pass


def _tg(t: str) -> str:
    m = re.search(r"<telegram>(.*?)</telegram>", t or "", re.S)
    body = (m.group(1) if m else (t or "")).strip()
    w = re.search(r"^WOULD CHANGE:.*$", t or "", re.M)
    if w and w.group() not in body:
        body += "\n[" + w.group().strip() + "]"
    return body


def report(days: int = 7) -> str:
    recs = _records(days)
    if not recs:
        return "no chat shadow runs yet"
    picks, agree = {}, 0
    out = []
    for r in recs:
        real = r["real"]["model"]
        pick = (r.get("pick") or {}).get("model")
        picks[pick] = picks.get(pick, 0) + 1
        agree += pick == real
        out.append(f"===== {r['ts']} {r['slug']} | answered on {real} "
                   f"(${r['real'].get('cost') or 0:.3f}) | picker: {pick} "
                   f"({(r.get('pick') or {}).get('why', '')}; "
                   f"{(r.get('pick') or {}).get('secs', '?')}s)\n"
                   f"--- athlete:\n{r['message']}\n--- {real} (sent):\n{_tg(r['real']['reply'])}\n"
                   + "".join(f"--- {m} (shadow, ${t.get('cost') or 0:.3f}):\n{_tg(t.get('text'))}\n"
                             for m, t in (r.get("twins") or {}).items())
                   + (f"!! {r['error']}\n" if r.get("error") else ""))
    head = (f"{len(recs)} messages; picker chose {picks}; agreed with today's model on "
            f"{agree}/{len(recs)}")
    return head + "\n\n" + "\n".join(out)


if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1] == "--run":
        _run(sys.argv[2])
    elif "--report" in sys.argv:
        n = int(sys.argv[sys.argv.index("--days") + 1]) if "--days" in sys.argv else 7
        print(report(n))
