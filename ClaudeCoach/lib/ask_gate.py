#!/usr/bin/env python3
"""Don't ask what you already know, and never ask two things at once.

WHY THIS EXISTS. The watchers cannot see the conversation — admitted in-chat on
10 Jun 2026 — so they re-ask what the athlete already answered. The 13 Aug 2026
full-log audit found three distinct shapes of it:

  * RE-ASKED AFTER ANSWERED. 7 Jul, Jamie: "log as a bug repeated asks"; 8-9 Jul,
    duplicate swim and ankle asks. The answer was sitting either in the
    session-log entry for that very activity or in the athlete's own chat turn.
  * NO BACKOFF ON SILENCE. Weight was asked four mornings running with nothing
    coming back. A question the athlete is visibly not answering is not a
    question, it is a nag, and asking it a fifth time makes the fifth answer
    less likely, not more.
  * TWO QUESTIONS IN ONE MESSAGE. `activity-watcher._send_followup_nudge` asked
    "RPE for the run? (1-10) — and how did the ankle feel this morning?" in one
    breath, and the run debrief format asked "Injury pain during and this
    morning?" — where the morning half is `morning-checkin`'s job, so the same
    question had two owners and fired twice.

WHAT IS HERE. Three pure pieces plus one small state file, deliberately split
that way so the decisions can be tested against fixtures with no athlete tree,
no network and no model: the ANSWERED CHECK (has this already been told to us),
the BACKOFF (has this gone unanswered twice), and the PICKER (which single
question survives). Every one of them is Python, for the reason
`lib/plan_builder.py:5-7` already learned twice: a model told to sometimes omit
a question reformats everything around it.

DESIGN RULES, each earned:

  1. ANSWERED IS DERIVED, NEVER RECORDED. `answer_date` reads the stores the
     answer actually lands in (`current-state.json` weight_readings and the
     ankle block, session-log fields) rather than expecting a caller to reset a
     counter. `telegram/bot.py` writes those stores from its fast paths and it
     does NOT call this module — if the reset depended on it doing so, a weight
     logged by the bot would leave the backoff counter running and the passive
     line would go out to an athlete who is weighing in daily.

  2. A MISS IS COUNTED ONCE PER MORNING, AND ONLY FOR AN ASK THAT SHIPPED.
     `morning-checkin` polls every 15 minutes and only claims its daily sentinel
     after a successful Claude call, so a failed call means the same morning is
     evaluated again — hence `last_miss_date`. And the card is generative: the
     model can still drop or reword the question, so the caller records the ask
     from the text it is about to send (`asked_in_text`), never from the fact
     that Python authorised one. Recording an ask that never shipped trips the
     backoff on silence we caused ourselves.

  3. THE VOLUNTEERED SCAN REQUIRES A TIMESTAMP. `telegram/bot._hist_entry`
     stamps every turn it writes with `ts`, so an unstamped entry is either
     historic or one of the watchers' own outbound appends (which carry an empty
     `user` and so can never register as athlete evidence anyway). Without the
     stamp there is no way to tell "he mentioned his ankle an hour ago" from
     "he mentioned it on Tuesday", and the second must not silence today's ask.

  4. THE PICKER USES MEASURED DURATION, NOT A SESSION CLASSIFIER.
     `lib/rpe_context.py`'s docstring documents why: `classify_session_type`
     reads the session NAME and names lie (Kathryn's 6x3min Z4 set is called
     "Wandsworth Running" and classifies as `run_easy`). Duration is measured.

State: `athletes/<slug>/standing-ask-state.json`, covered by the
`ClaudeCoach/athletes/` line in `.gitignore`, alongside the other per-athlete
state files. Schema, per question key:

    {"asks": {"weight": {"last_asked":      "YYYY-MM-DD" | null,
                         "misses":          int,
                         "last_miss_date":  "YYYY-MM-DD" | null,
                         "last_answer_seen":"YYYY-MM-DD" | null,
                         "last_passive":    "YYYY-MM-DD" | null}}}

Evaluation is pure (`evaluate`); only `save_state_in`, `note_asked_in` and
`note_passive_in` write — the same split `lib/acknowledgement.py` uses, and for
the same reason: it makes a read-only audit against the live tree possible.
"""

import json
import os
import re
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path

STATE_FILENAME = "standing-ask-state.json"

# -- standing morning asks --------------------------------------------------
WEIGHT = "weight"
ANKLE_SCORE = "ankle_score"
STANDING_QUESTIONS = (WEIGHT, ANKLE_SCORE)

# Two consecutive unanswered mornings, per the 13 Aug brief. Two rather than
# three because four in a row is what the athlete complained about, and the
# third ask is already the one that reads as nagging.
BACKOFF_MISSES = 2

# The passive replacement is a note about thinning data, not a question, so it
# carries no "?" and expects no reply. At most weekly: more often and it is the
# nag it replaced, wearing a different coat.
PASSIVE_EVERY_DAYS = 7

# No dashes in this copy on purpose. It is athlete-facing and gets flattened on
# deploy, and a sentence that survives the flattening intact is one less thing
# to check.
PASSIVE_LINES = {
    WEIGHT: "Weigh-ins have gone quiet. The weight trend gets thin without them.",
    ANKLE_SCORE: ("No ankle scores for a while, so I've stopped asking. "
                  "Drop one in whenever it's worth noting."),
}

# What the ask looks like once the card has been written, for `asked_in_text`.
# Matched case-insensitively against the message actually being sent — see
# design rule 2. Kept as substrings of the exact copy the prompt specifies, so a
# reworded question fails to match and correctly records no ask.
_ASK_SIGNATURES = {
    WEIGHT: ("weight this morning",),
    ANKLE_SCORE: ("ankle score", "injury pain score", "injury pain before",
                  "pain score before"),
}

# -- per-session questions --------------------------------------------------
RPE = "rpe"
ANKLE = "ankle"          # the DURING score: the debrief's own question
NUTRITION = "nutrition"

# Where the answer to each per-session question lives in a session-log entry.
# Any one field carrying a value counts as answered: "how did it feel" is
# answered by `feel` prose just as well as by an `rpe` figure.
_SESSION_FIELDS = {
    RPE: ("rpe", "feel"),
    ANKLE: ("injury_pain_during",),
    NUTRITION: ("nutrition_g_carb",),
}

# Keywords that mark an athlete turn as having volunteered this. Deliberately
# loose on the answer's SHAPE and strict on its window (design rule 3): "felt
# rough" answers "how did it feel?" as completely as "RPE 7" does, and a debrief
# that asks anyway is the logged bug.
_VOLUNTEERED = {
    RPE: ("rpe", "/10", "felt", "feeling", "felt like"),
    ANKLE: ("ankle", "pain", "niggle"),
    NUTRITION: ("carb", "g/hr", "gel", "bottle", "fuel", "sodium", "drink"),
}

# A session at or above this is a long one, and fuelling is the question that
# matters for it (design rule 4).
LONG_SESSION_MIN = 90

# Compound ankle ask, as generated from the run debrief format: "Injury pain
# score during and this morning? (0-10)". The morning half belongs to
# morning-checkin, which owns its own backoff counter for it, so it is cut here
# even when the during half is kept. Narrow on purpose: it matches the phrasing
# the prompt asks for and its near variants, and anything else is left alone
# rather than mangled.
_MORNING_HALF_RE = re.compile(
    r"\s*(?:,|and|/|&)\s*(?:how (?:was|did) it (?:feel )?)?"
    r"(?:this|yesterday|next)\s+morning'?s?\b[^?]*", re.I)


# ---------------------------------------------------------------------------
# dates
# ---------------------------------------------------------------------------

def _as_date(v):
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    if isinstance(v, str) and v.strip():
        try:
            return date.fromisoformat(v.strip()[:10])
        except ValueError:
            return None
    return None


def _as_dt(v):
    if isinstance(v, datetime):
        return v
    if isinstance(v, str) and v.strip():
        s = v.strip()
        try:
            return datetime.fromisoformat(s)
        except ValueError:
            pass
        d = _as_date(s)
        if d:
            return datetime(d.year, d.month, d.day)
    return None


def _max_date(values):
    dates = [d for d in (_as_date(v) for v in values) if d]
    return max(dates) if dates else None


# ---------------------------------------------------------------------------
# 1. asked and answered
# ---------------------------------------------------------------------------

def session_answered(entry, kind) -> bool:
    """Is this per-session question already answered in the session-log entry?

    The entry is the authoritative place: the activity-watcher prompt is told to
    fill these fields from the chat when the athlete has already given them, and
    the quick-log keyboard writes them directly.
    """
    fields = _SESSION_FIELDS.get(kind)
    if not fields or not isinstance(entry, dict):
        return False
    return any(entry.get(f) is not None and entry.get(f) != "" for f in fields)


def known_value_line(entry, kind) -> str:
    """The known answer as a debrief line, so it is FOLDED IN rather than asked
    for. Empty string when there is nothing to fold — the caller then simply
    drops the question line.

    No question mark and no numbers the athlete did not themselves supply, so
    this is safe at every coaching level: an RPE, a pain score and a g/hr figure
    are all the athlete's own data coming back to them, not a training metric.
    """
    if not isinstance(entry, dict):
        return ""
    if kind == RPE:
        rpe, feel = entry.get("rpe"), (entry.get("feel") or "").strip()
        if rpe is not None and feel:
            return f"RPE {rpe} logged, felt {feel.rstrip('.').lower()}."
        if rpe is not None:
            return f"RPE {rpe} logged."
        if feel:
            return f"Logged as {feel.rstrip('.').lower()}."
        return ""
    if kind == ANKLE:
        v = entry.get("injury_pain_during")
        return f"Ankle {v}/10 during, logged." if v is not None else ""
    if kind == NUTRITION:
        g = entry.get("nutrition_g_carb")
        return f"Fuelling logged at {g}g carbs/hr." if g is not None else ""
    return ""


def volunteered_since(history, kind, since=None) -> bool:
    """Did the athlete volunteer this in chat since `since`?

    `history` is the athlete's `telegram/history.json` list. Only the `user`
    side counts, and only turns carrying a `ts` at or after `since` — design
    rule 3. Scans from the newest end and stops at the first turn that is older,
    so this stays cheap on every watcher cycle.
    """
    kws = _VOLUNTEERED.get(kind)
    if not kws or not isinstance(history, list):
        return False
    since_dt = _as_dt(since)
    if since_dt is None:
        # No floor means no window, and an unwindowed keyword scan over a 30-turn
        # transcript would let "felt terrible" from Tuesday silence today's ask.
        # An unknown floor therefore yields no evidence: one extra ask is a much
        # smaller failure than a debrief that never asks again.
        return False
    for e in reversed(history):
        if not isinstance(e, dict):
            continue
        ts = _as_dt(e.get("ts"))
        if ts is None:
            continue              # unstamped: cannot be placed in the window
        if ts < since_dt:
            break                 # history is chronological; nothing older matters
        user = (e.get("user") or "").lower()
        if user and any(k in user for k in kws):
            return True
    return False


def already_answered(entry, kind, history=None, since=None) -> bool:
    """The full asked-and-answered check for one per-session question: the
    session-log entry first (cheap and authoritative), then the chat."""
    if session_answered(entry, kind):
        return True
    return volunteered_since(history, kind, since=since)


def entry_synced_at(entry):
    """When this activity landed, as the floor for the volunteered scan. Falls
    back to midnight on the activity's date; None when neither is readable, and
    the caller then scans nothing rather than scanning everything."""
    if not isinstance(entry, dict):
        return None
    return _as_dt(entry.get("logged_at")) or _as_dt(entry.get("date"))


# ---------------------------------------------------------------------------
# 2. the one-question picker
# ---------------------------------------------------------------------------

def pick_question(entry, wanted):
    """Which single question survives. None when none do.

    Deterministic, and comment-documented rather than delegated to a model:

      1. THE DURING-SCORE OUTRANKS EVERYTHING, including the duration rule. It is
         only ever in `wanted` when an injury is tracked and the score is still
         missing, and it is the one field that moves load and goes to a physio.
         Ranking it below duration meant a 2-hour injury run asked about gels and
         neither surface ever asked about the ankle, because the debrief filter
         and the follow-up nudge both consult this function and would agree.
      2. Otherwise, at or above LONG_SESSION_MIN the open question is FUELLING —
         that is what a long session teaches.
      3. Otherwise, how it went.

    Whatever loses here is not queued or retried: it rides along in the next
    natural exchange, which is the point of asking one thing.
    """
    wanted = [w for w in (wanted or []) if w]
    if not wanted:
        return None
    if ANKLE in wanted:
        return ANKLE
    try:
        dur = float((entry or {}).get("duration_min") or 0)
    except (TypeError, ValueError):
        dur = 0.0
    if dur >= LONG_SESSION_MIN and NUTRITION in wanted:
        return NUTRITION
    for kind in (RPE, NUTRITION):
        if kind in wanted:
            return kind
    return wanted[0]


def classify_question(segment):
    """Which question a rendered debrief segment is asking, or None.

    Used to filter the model's own trailing question lines. Ordered most
    specific first: a fuelling ask mentions carbs, an ankle ask mentions the
    ankle, and only what is left is the generic "how did it feel".
    """
    if not segment or "?" not in segment:
        return None
    s = segment.lower()
    if any(k in s for k in ("carb", "nutrition", "fuel", "sodium", "bottle")):
        return NUTRITION
    if any(k in s for k in ("ankle", "injury pain", "pain score", "pain during")):
        return ANKLE
    if any(k in s for k in ("rpe", "how did it feel", "how it felt",
                            "how did that feel", "main focus")):
        return RPE
    return None


def strip_morning_half(segment):
    """Cut the morning half out of a compound ankle ask, leaving the during
    half. `morning-checkin` owns the morning score and has its own backoff for
    it; two owners is how it came to be asked twice."""
    if not segment:
        return segment
    return _MORNING_HALF_RE.sub("", segment, count=1)


# ---------------------------------------------------------------------------
# 3. backoff state
# ---------------------------------------------------------------------------

def _blank():
    return {"last_asked": None, "misses": 0, "last_miss_date": None,
            "last_answer_seen": None, "last_passive": None}


def _state_path(athlete_dir) -> Path:
    return Path(athlete_dir) / STATE_FILENAME


def load_state_from_dir(athlete_dir) -> dict:
    """Read the backoff record. Missing or corrupt reads as empty, which fails
    OPEN here: an unknown history has no misses, so the question is asked. That
    is the right direction for this gate — the failure it exists to prevent is
    over-asking, and silence caused by a corrupt state file would be a worse
    bug than one extra ask."""
    p = _state_path(athlete_dir)
    try:
        raw = json.loads(p.read_text()) if p.exists() else {}
    except Exception:
        raw = {}
    asks = raw.get("asks") if isinstance(raw.get("asks"), dict) else {}
    out = {}
    for q, rec in asks.items():
        if not isinstance(rec, dict):
            continue
        base = _blank()
        base.update({k: rec.get(k, base[k]) for k in base})
        try:
            base["misses"] = int(base["misses"] or 0)
        except (TypeError, ValueError):
            base["misses"] = 0
        out[q] = base
    return {"asks": out}


def _atomic_write(path: Path, payload: dict) -> None:
    """A half-written record reads as 'never asked' and restarts the nagging,
    so this follows acknowledgement._atomic_write exactly."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".ask-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def save_state_in(athlete_dir, state) -> None:
    _atomic_write(_state_path(athlete_dir), {"asks": (state or {}).get("asks", {})})


def evaluate(record, today=None, answered_on=None, question=None) -> tuple[dict, dict]:
    """Pure backoff decision for ONE standing question.

    Returns `(updated_record, decision)` where decision is
    `{"ask": bool, "passive": str|None, "misses": int}`.

    `answered_on` is the date of the most recent answer anywhere (see
    `answer_date`), NOT a flag: an answer dated on or after the last ask clears
    the counter, an older one does not. Callers persist the returned record
    whether or not they end up asking, because the reset and the miss are facts
    about what already happened.
    """
    today = today or date.today()
    rec = _blank()
    rec.update(record or {})
    try:
        rec["misses"] = int(rec.get("misses") or 0)
    except (TypeError, ValueError):
        rec["misses"] = 0

    last_asked = _as_date(rec.get("last_asked"))
    answered = _as_date(answered_on)

    if answered and (last_asked is None or answered >= last_asked):
        # Answered — including answered without being asked, which is the athlete
        # weighing in unprompted and is exactly as good as a reply.
        rec["misses"] = 0
        rec["last_miss_date"] = None
        rec["last_answer_seen"] = answered.isoformat()
    elif (last_asked and last_asked < today
            and rec["misses"] < BACKOFF_MISSES
            and _as_date(rec.get("last_miss_date")) != today):
        # Asked on an earlier day, nothing came back. Once per morning (the
        # 15-minute poll re-evaluates a morning whose Claude call failed), and
        # not once we have already stopped asking.
        rec["misses"] += 1
        rec["last_miss_date"] = today.isoformat()

    if rec["misses"] < BACKOFF_MISSES:
        return rec, {"ask": True, "passive": None, "misses": rec["misses"]}

    last_passive = _as_date(rec.get("last_passive"))
    due = last_passive is None or (today - last_passive).days >= PASSIVE_EVERY_DAYS
    passive = PASSIVE_LINES.get(question or (record or {}).get("question")) if due else None
    return rec, {"ask": False, "passive": passive, "misses": rec["misses"]}


def decide(state, question, today=None, answered_on=None) -> tuple[dict, dict]:
    """`evaluate` for a question inside a loaded state, returning the state with
    that question's record updated. The caller saves once, after all questions."""
    state = {"asks": dict((state or {}).get("asks", {}))}
    rec = dict(state["asks"].get(question) or _blank())
    new_rec, decision = evaluate(rec, today=today, answered_on=answered_on,
                                 question=question)
    state["asks"][question] = new_rec
    return state, decision


def asked_in_text(question, text) -> bool:
    """Did the message actually being sent carry this ask? Design rule 2 — the
    card is generative, so the record follows the text, not the intent.

    The signature must land on a line that is itself a question. A watchdog flag
    reading "ankle scores rising, drop run volume" mentions the words without
    asking anything, and counting it as an ask would credit a question that was
    never put and then charge the athlete a miss for not answering it.
    """
    if not text:
        return False
    sigs = _ASK_SIGNATURES.get(question, ())
    for line in str(text).split("\n"):
        if "?" not in line:
            continue
        low = line.lower()
        if any(sig in low for sig in sigs):
            return True
    return False


def note_asked_in(athlete_dir, question, today=None) -> None:
    """Record that the ask shipped, so tomorrow can count a miss against it."""
    today = today or date.today()
    state = load_state_from_dir(athlete_dir)
    rec = dict(state["asks"].get(question) or _blank())
    rec["last_asked"] = today.isoformat()
    state["asks"][question] = rec
    save_state_in(athlete_dir, state)


def note_passive_in(athlete_dir, question, today=None) -> None:
    """Record that the passive line shipped, so it stays at most weekly."""
    today = today or date.today()
    state = load_state_from_dir(athlete_dir)
    rec = dict(state["asks"].get(question) or _blank())
    rec["last_passive"] = today.isoformat()
    state["asks"][question] = rec
    save_state_in(athlete_dir, state)


# ---------------------------------------------------------------------------
# answer sources for the standing asks (design rule 1)
# ---------------------------------------------------------------------------

def answer_date(question, current_state=None, session_log=None):
    """The most recent date this standing question was answered ANYWHERE.

    Reads the stores the answers land in, because the writer (`telegram/bot.py`
    fast paths, the quick-log keyboard, `telegram-feedback.py`) does not and
    should not have to call this module.
    """
    cs = current_state if isinstance(current_state, dict) else {}
    log = session_log if isinstance(session_log, list) else []

    if question == WEIGHT:
        return _max_date(r.get("date") for r in (cs.get("weight_readings") or [])
                         if isinstance(r, dict))

    if question == ANKLE_SCORE:
        ankle = cs.get("ankle") if isinstance(cs.get("ankle"), dict) else {}
        cands = [h.get("date") for h in (ankle.get("history") or [])
                 if isinstance(h, dict)]
        cands.append(ankle.get("pain_today_resting_date"))
        # A next-morning score written onto yesterday's run answers the morning
        # ask, and it is the one the morning card asks for by name — so the
        # session log is a first-class source here, not a fallback.
        for e in log:
            if isinstance(e, dict) and e.get("injury_pain_next_morning") is not None:
                cands.append(e.get("date"))
        return _max_date(cands)

    return None


def weight_reading_due(current_state, today=None, stale_days=3) -> bool:
    """No weight reading in the last `stale_days` days.

    Moved out of the morning prompt (where the model checked it by reading
    current-state.json) so the backoff below has something deterministic to
    gate: a counter cannot be trusted if the condition it counts is decided by
    a model that may or may not have looked.
    """
    today = today or date.today()
    last = answer_date(WEIGHT, current_state=current_state)
    if last is None:
        return True
    return (today - last).days > stale_days


__all__ = [
    "WEIGHT", "ANKLE_SCORE", "STANDING_QUESTIONS", "RPE", "ANKLE", "NUTRITION",
    "BACKOFF_MISSES", "PASSIVE_EVERY_DAYS", "PASSIVE_LINES", "LONG_SESSION_MIN",
    "session_answered", "known_value_line", "volunteered_since",
    "already_answered", "entry_synced_at", "pick_question", "classify_question",
    "strip_morning_half", "load_state_from_dir", "save_state_in", "evaluate",
    "decide", "asked_in_text", "note_asked_in", "note_passive_in",
    "answer_date", "weight_reading_due",
]
