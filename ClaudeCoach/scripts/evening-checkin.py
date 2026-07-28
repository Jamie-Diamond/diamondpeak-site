#!/usr/bin/env python3
"""Evening check-in — runs via VM crontab at 21:00 daily. Loops over all active athletes.

THE ONE EVENING QUESTION. Until 2026-07-28 three separate crons pushed to each athlete
inside fifty minutes (20:10 capture-reminder, 20:30 night-before-brief, 21:00 this) and a
per-activity debrief could add a fourth around 19:00, each ending in its own question.
Kathryn's RPE went unanswered on 25, 26 and 27 Jul; more asks cannot fix a cadence the
athlete has disengaged from.

This script is now the only evening surface that ASKS anything:
  * Case A0 — a debrief question the activity-watcher deferred into the evening slot
              (activity-watcher writes .evening-ask.json instead of pushing between
              18:00 and 21:00 — see _defer_evening_question there).
  * Case A  — a completed activity with no session-log entry (unchanged).
  * Case A2 — the retired capture-reminder's job: an unlogged KEY session up to 36h old,
              one ask per activity_id via .capture-reminded.json.
  * Case B  — a planned session with nothing completed (unchanged).
  * Case C/D— silence (unchanged).
Priority A0 > A > A2 > B > silence, and still exactly ONE message per athlete per evening.
The night-before brief stays a separate 20:30 push: it carries tomorrow's targets, never
asks a question, and has its own deterministic race-eve path.
"""
import json, os, re, subprocess, sys, time
from datetime import date, datetime, timedelta
from pathlib import Path

BASE            = Path(__file__).parent.parent  # ClaudeCoach/
PROJECT_DIR     = str(BASE.parent)
CLAUDE          = "/usr/bin/claude"
NOTIFY          = BASE / "telegram/notify.py"
ATHLETES_CONFIG = BASE / "config/athletes.json"
LOG_DIR         = Path.home() / "Library/Logs/ClaudeCoach"
LOG_DIR.mkdir(parents=True, exist_ok=True)
sys.path.insert(0, str(BASE / "lib"))
from coaching_levels import level_block as _level_block
import illness as illness_lib   # structured illness/compromised flag (surfacing gate)
import ops_log

TOOLS = "Read,Bash"

# Case-A messages acknowledge a COMPLETED activity ("... done.") and then ask a
# capture question. Per Step 2 of the prompt, an activity already present in
# session-log.json for today is ACCOUNTED FOR, so Case A must not fire for it.
# Haiku occasionally emits Case A anyway, re-asking for data already captured
# (e.g. injury pain re-asked on 2026-07-14 for a run already logged 0/10). This
# deterministic backstop suppresses that. Keyed on SPORT rather than activity_id
# because the emitted message never carries the id. Case B ("Did the ... happen
# today?") contains no "done" and is never matched, so a legitimate did-it-happen
# prompt is always preserved.
_CASE_A_SPORTS = ("run", "ride", "swim", "strength")


# Markers that a message is a Case-A completion acknowledgement + capture question,
# rather than free chat. Covers every Case-A question tail (injury / RPE / nutrition
# / strength) and is deliberately broad so a Haiku paraphrase of the template (which
# may drop the literal word "done") is still caught.
_CASE_A_MARKERS = ("done", "0-10", "0\u201310", "rpe", "nutrition", "carbs",
                   "how did it feel", "how did it go", "main focus", "pain")


def _completed_sport_ack(content):
    """Return the sport if `content` is a Case-A completion acknowledgement, else None."""
    c = content.lower()
    # Case B ("Did the ... happen today?") asks about a MISSING session and may name
    # a sport ("Did the run happen today?") — never treat it as a completion ack.
    if re.search(r"\bdid\b.*\bhappen\b", c):
        return None
    if not any(mk in c for mk in _CASE_A_MARKERS):
        return None
    for sport in _CASE_A_SPORTS:
        if sport in c:
            return sport
    return None


def _ids_in_session_log(adir):
    """Every activity_id present in session-log.json, regardless of date or stub state."""
    ids = set()
    sl_path = adir / "session-log.json"
    if sl_path.exists():
        try:
            for e in json.loads(sl_path.read_text()):
                if e.get("activity_id"):
                    ids.add(str(e.get("activity_id")))
        except Exception:
            pass
    return ids


# --- Case A0: debrief questions deferred out of the 18:00-21:00 window ----------------
# activity-watcher strips the trailing question from an evening debrief and appends it
# here rather than pushing it. Consuming it is this script's job, so the athlete gets the
# question at 21:00 inside the check-in push instead of at 19:00 in its own push.
QUEUE_FILE      = ".evening-ask.json"
_QUEUE_MAX_AGE_H = 24   # NOT "dated today": a 20:59 debrief can miss tonight's run (the
                        # 30s-per-athlete stagger), and the watcher has already suppressed
                        # the follow-up nudge for it, so a strict same-day filter would
                        # lose the ask forever.
_FEEDBACK_KWS   = ("rpe", "/10", "felt", "feeling", "pain", "ankle", "carbs", "g/hr")


def _queue_path(adir):
    return adir / QUEUE_FILE


def _read_queue(adir):
    try:
        entries = json.loads(_queue_path(adir).read_text())
        return entries if isinstance(entries, list) else []
    except Exception:
        return []


def _field_already_captured(adir, entry):
    """True if the data this queued question asks for has since been captured — the
    quick-log keyboard the watcher sends alongside the debrief is one tap, so by 21:00
    the answer is often already in. Same intent as activity-watcher's
    _chat_has_recent_feedback: never re-ask for data the athlete already gave."""
    aid = str(entry.get("activity_id", ""))
    sl_path = adir / "session-log.json"
    if not aid or not sl_path.exists():
        return False
    try:
        for e in json.loads(sl_path.read_text()):
            if str(e.get("activity_id", "")) != aid:
                continue
            for f in ("rpe", "injury_pain_during", "nutrition_g_carb", "feel"):
                if e.get(f) is not None:
                    return True
            return False
    except Exception:
        pass
    return False


def _chat_answered_since(adir, since_iso):
    """True if the athlete's own Telegram messages since `since_iso` carry session
    feedback. Mirrors activity-watcher._chat_has_recent_feedback rather than inventing a
    second de-duplication mechanism."""
    hist_f = adir / "telegram" / "history.json"
    if not hist_f.exists():
        return False
    try:
        entries = json.loads(hist_f.read_text())
    except Exception:
        return False
    for e in reversed(entries):
        ts = e.get("ts", "")
        if ts and since_iso and ts < since_iso:
            break
        u = (e.get("user") or "").lower()
        if u and any(k in u for k in _FEEDBACK_KWS):
            return True
    return False


_FUELLING_ASK = ("carb", "fuel", "nutrition", "g/hr", "bottle")


def _illness_active(adir):
    try:
        st = illness_lib.state_from_dir(adir)
        return bool(st and st["active"])
    except Exception:
        return False


def _live_queue(adir, now=None):
    """Queued asks still worth making: under 24h old, data not yet captured, not answered
    in chat since the debrief.

    A queued question is replayed verbatim, which normally preserves the illness gate
    because the watcher generated it under the same illness block. The exception is a flag
    raised AFTER the debrief — a fuelling ask queued at 18:40 must not resurface at 21:00
    once the athlete has said they are ill, since illness.SUPPRESSES covers fuelling and
    carb-intake flags."""
    now = now or datetime.now()
    ill = _illness_active(adir)
    live = []
    for e in _read_queue(adir):
        ts = e.get("ts", "")
        try:
            if datetime.fromisoformat(ts) < now - timedelta(hours=_QUEUE_MAX_AGE_H):
                continue
        except Exception:
            continue
        if _field_already_captured(adir, e):
            continue
        if _chat_answered_since(adir, ts):
            continue
        if ill and any(k in e.get("question", "").lower() for k in _FUELLING_ASK):
            continue
        live.append(e)
    return live


def _clear_queue(adir, consumed_ids):
    """Drop consumed entries (and anything aged out). Only ever called after a SEND —
    an unconsumed entry must survive a failed send, or the ask is lost silently."""
    keep, now = [], datetime.now()
    for e in _read_queue(adir):
        if str(e.get("activity_id", "")) in consumed_ids:
            continue
        try:
            if datetime.fromisoformat(e.get("ts", "")) < now - timedelta(hours=_QUEUE_MAX_AGE_H):
                continue
        except Exception:
            continue
        keep.append(e)
    try:
        if keep:
            _queue_path(adir).write_text(json.dumps(keep, indent=1))
        elif _queue_path(adir).exists():
            _queue_path(adir).unlink()
    except Exception:
        pass


def _queued_ask_text(live):
    """The deterministic Case-A0 message. One entry replays the watcher's own question
    verbatim — it was generated under this athlete's coaching-level and illness blocks, so
    replaying it preserves both registers with no second model call. Two or more collapse
    into ONE question rather than stacking question marks."""
    if not live:
        return ""
    if len(live) == 1:
        return live[0].get("question", "").strip()
    sports = []
    for e in live:
        s = (e.get("sport") or "session").lower()
        if s not in sports:
            sports.append(s)
    return f"RPE for the {' and the '.join(sports)}? (1-10 each)"


def _sports_logged_today(adir):
    """Set of lowercased sports with a session-log.json entry dated today."""
    logged = set()
    sl_path = adir / "session-log.json"
    if sl_path.exists():
        today = date.today().isoformat()
        try:
            for e in json.loads(sl_path.read_text()):
                if str(e.get("date")) == today and e.get("sport"):
                    logged.add(str(e.get("sport")).lower())
        except Exception:
            pass
    return logged


def _build_prompt(slug, first_name, injuries, pain_next_morning=0, coaching_level="mid",
                  queued_ask="", reminded_ids=None):
    today = date.today().isoformat()
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    reminded = ", ".join(reminded_ids or []) or "(none)"

    # Case A0 is pre-resolved in Python — the model is told the exact text to send so it
    # cannot re-word a question that was already generated under this athlete's coaching
    # level and illness gate.
    case_a0 = ""
    if queued_ask:
        case_a0 = (
            "Case A0 — HIGHEST PRIORITY. A debrief question was deferred into this "
            "check-in earlier this evening. Send EXACTLY this text and nothing else, "
            "then stop:\n"
            f"  {queued_ask}\n"
        )
    # Ask the injury question only if pain_next_morning > 0 — if last morning score
    # was 0, the ankle is fine and we don't ask every single run.
    if injuries and pain_next_morning > 0:
        injury_case = "  - Run: \"Good [X km] run done. Injury pain during today's run? (0-10)\""
    else:
        injury_case = "  - Run: \"Good [X km] run done. RPE and how did it feel?\""

    return f"""\
Evening training log check for {first_name}.

{_level_block(coaching_level)}
{illness_lib.prompt_block(slug, first_name=first_name)}


Apply the GLOBAL coaching rules in ClaudeCoach/athletes/_shared/persistent-rules.md (read them first).

Step 1 — Fetch data via Bash:
  python3 ClaudeCoach/lib/icu_fetch.py --athlete {slug} --endpoint history --days 1
  python3 ClaudeCoach/lib/icu_fetch.py --athlete {slug} --endpoint events --start {today} --end {today}

Step 2 — Read ClaudeCoach/athletes/{slug}/session-log.json (check which activity_ids are already stubbed).
An entry with a matching activity_id counts as ACCOUNTED FOR even if stub is true or rpe/pain
fields are null — missing field data is the capture-reminder's job, never yours. Case A applies
ONLY when there is NO entry at all for the activity.

Step 3 — Decide whether to send a message:

{case_a0}
Case A — A completed activity from TODAY exists NOT yet in session-log.json:
  Send one specific question (max 2 sentences, no preamble):
{injury_case}
  - Ride (>90 min): "Solid [X km] ride done. Nutrition — roughly g carbs/hr and bottles?"
  - Swim: "Swim done — [X m] at [pace]. RPE and how did it feel?"
  - Strength: "Strength session done. RPE and main focus?"

Case A2 — CAPTURE (this was the 20:10 capture-reminder's job; it is now yours, so that
the athlete gets one evening push instead of two). A completed activity from {yesterday}
that meets ALL of:
  1. Load (TSS) > 40 OR duration > 45 minutes
  2. Sport is Ride, VirtualRide, Run, VirtualRun, Brick or Swim (skip Strength)
  3. No entry in session-log.json with a matching activity_id
  4. activity_id is NOT in the already-asked list: {reminded}
     (one ask per session — repeat nagging is the defect being fixed)
  Send: "Log [session name] — say 'log session'"

Case B — A planned session has NO matching completed activity AND it's after 19:00:
  Before sending: read ClaudeCoach/athletes/{slug}/current-state.md — if there is any note from today indicating the session was swapped, substituted, or intentionally skipped, suppress the message entirely (treat as Case C).
  Otherwise send: "Did the [session name] happen today?"

Case C — All sessions accounted for and already stubbed: produce no output.

Case D — No planned sessions and no activities: produce no output.

Priority: Case A0 > Case A > Case A2 > Case B > silence. Only ever send ONE message, and
it must contain AT MOST ONE question — this is the only evening push that asks anything.

OUTPUT FORMAT — follow exactly:
- Cases A0, A, A2 or B: wrap your single message in <notify ids="...">...</notify> tags,
  where ids is a comma-separated list of the activity_ids the message is about (empty for
  Case B, which is about a session that did not happen). Nothing outside the tags.
- Cases C or D: output exactly <notify>SKIP</notify>. No other text."""


def notify(msg, chat_id, slug=""):
    """Send via notify.py, retry once; alert the ops log if delivery fails."""
    for _attempt in (1, 2):
        try:
            r = subprocess.run(
                ["python3", str(NOTIFY), "--chat-id", str(chat_id), msg],
                cwd=PROJECT_DIR, timeout=15,
            )
            if r.returncode == 0:
                return True
        except Exception:
            pass
    ops_log.alert("evening-checkin", "Telegram send failed after retry", athlete=slug)
    return False


def _record(slug, ok=True, detail=""):
    """Heartbeat for this run — and for the retired capture-reminder, whose work Case A2
    now does. coach_alert.DELIVERABLES still lists capture-reminder as a per-athlete daily
    deliverable; the job still happens, just inside this process, so it records here rather
    than showing up as a permanent gap in every ops digest. Removing that registry entry is
    the right long-term fix but coach_alert.py belongs to another change."""
    ops_log.record_run("evening-checkin", athlete=slug, ok=ok, detail=detail)
    ops_log.record_run("capture-reminder", athlete=slug, ok=ok,
                       detail=f"merged into evening-checkin ({detail})")


def run_athlete(slug, athlete_cfg):
    adir = BASE / f"athletes/{slug}"
    chat_id = athlete_cfg.get("chat_id", "")
    log_file = LOG_DIR / "evening-checkin.log"
    if not chat_id:
        print(f"[{slug}] SKIP: no chat_id in athletes.json", file=sys.stderr)
        return

    profile = {}
    if (adir / "profile.json").exists():
        try:
            profile = json.loads((adir / "profile.json").read_text())
        except Exception:
            pass

    first_name = profile.get("name", slug).split()[0]
    injuries = profile.get("injuries", [])

    pain_next_morning = 0
    state_f = adir / "current-state.json"
    if state_f.exists():
        try:
            ankle = json.loads(state_f.read_text()).get("ankle", {})
            pain_next_morning = ankle.get("pain_next_morning", 0) or 0
        except Exception:
            pass

    coaching_level = profile.get("coaching_level", "mid")

    live_queue = _live_queue(adir)
    queued_ask = _queued_ask_text(live_queue)
    queue_ids = {str(e.get("activity_id", "")) for e in live_queue if e.get("activity_id")}

    reminded_file = adir / ".capture-reminded.json"
    try:
        reminded_ids = json.loads(reminded_file.read_text()) if reminded_file.exists() else []
    except Exception:
        reminded_ids = []

    prompt = _build_prompt(slug, first_name, injuries, pain_next_morning,
                           coaching_level=coaching_level, queued_ask=queued_ask,
                           reminded_ids=reminded_ids)

    with open(log_file, "a") as lf:
        result = subprocess.run(
            [CLAUDE, "-p", "--allowedTools", TOOLS, "--model", "claude-haiku-4-5-20251001"],
            input=prompt,  # prompt on stdin, not argv (MAX_ARG_STRLEN)
            stdout=subprocess.PIPE, stderr=lf, text=True,
            cwd=PROJECT_DIR, timeout=180,
        )

    output = (result.stdout or "").strip()
    import re
    m = re.search(r'<notify(?:\s+ids="([^"]*)")?>(.*?)</notify>', output,
                  re.DOTALL | re.IGNORECASE)
    content = m.group(2).strip() if m else ""
    msg_ids = [i.strip() for i in ((m.group(1) if m else "") or "").split(",") if i.strip()]

    def _send(text, ids, detail):
        """One send path, so the ledger and the queue are only ever written after the
        message actually left."""
        if not notify(text, chat_id, slug=slug):
            return
        if ids:
            try:
                reminded_file.write_text(json.dumps((reminded_ids + list(ids))[-50:]))
            except Exception:
                pass
        consumed = queue_ids & set(ids) if ids else queue_ids
        if consumed:
            _clear_queue(adir, consumed)
        _record(slug, ok=True, detail=detail)

    if not m:
        # No notify tag at all — treat as silent
        _record(slug, ok=True, detail="silent")
    elif content.upper() == "SKIP":
        if queued_ask:
            # Deterministic backstop, same shape as the Case-A duplicate backstop below:
            # the model chose silence but a debrief question was deferred into this slot
            # and nothing else will ask it, so send it verbatim.
            _send(queued_ask, sorted(queue_ids), "sent (deferred debrief ask, backstop)")
        else:
            # Cases C/D — model confirmed nothing to send
            _record(slug, ok=True, detail="silent")
    elif (msg_ids and not (set(msg_ids) & queue_ids)
            and set(msg_ids) <= _ids_in_session_log(adir)):
        # Deterministic backstop, now keyed on activity_id: every activity this message is
        # about is already in session-log.json, so it is accounted for and the ask is a
        # duplicate. Queued Case-A0 ids are excluded — those ARE stubbed by design (the
        # watcher logged them, then deferred its question), so an id match there is
        # expected, not a duplicate.
        _record(slug, ok=True, detail=f"suppressed-dup:{','.join(msg_ids)}")
    elif not msg_ids and (ack_sport := _completed_sport_ack(content)) and not queued_ask \
            and ack_sport in _sports_logged_today(adir):
        # Fallback for a model that dropped the ids attribute: the original sport-keyed
        # backstop. Only trusted when there is no queued ask (a Case-A0 replay is about an
        # activity that is legitimately already logged).
        _record(slug, ok=True, detail=f"suppressed-dup:{ack_sport}")
    else:
        detail = "sent (deferred debrief ask)" if queued_ask else "sent"
        _send(content, msg_ids, detail)


def main():
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] evening-checkin starting", file=sys.stderr)
    try:
        athletes = json.loads(ATHLETES_CONFIG.read_text())
    except Exception as e:
        print(f"[{ts}] Failed to load athletes config: {e}", file=sys.stderr)
        sys.exit(1)

    stagger = int(os.environ.get("ATHLETE_STAGGER_S", "30"))
    processed = False
    for slug, cfg in athletes.items():
        if not cfg.get("active", True):
            continue
        if processed:
            time.sleep(stagger)   # space Claude calls — rate-limit contention
        processed = True
        try:
            run_athlete(slug, cfg)
        except Exception as exc:
            print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}][{slug}] evening-checkin error: {exc}", file=sys.stderr)
            ops_log.alert("evening-checkin", f"exception: {exc}", athlete=slug)


if __name__ == "__main__":
    main()
