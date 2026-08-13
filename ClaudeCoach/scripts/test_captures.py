#!/usr/bin/env python3
"""Offline tests for the capture pass-through fixes (13 Aug 2026).
Run: python3 ClaudeCoach/scripts/test_captures.py

WHAT BROKE. Four regex captures in telegram/bot.py ran before the model and CONSUMED the
message. On 13 Aug the day-rule capture asked Jamie to confirm a ride moving to another
day, over a button reading "Yes, that's the plan"; he tapped it twice; all that was written
was one line telling the plan audit not to flag the day. The ride never moved, because only
the model has the tools to move it and the model never saw the message. The same
confirmation was asked and recorded twice in three minutes, and none of it reached
history.json, so his next message was answered with no sight of the exchange.

WHAT IS TESTED HERE. The pure pieces of the fix, which are the pieces that can be wrong
silently: the register dedupe, the ask/don't-ask decision, the note handed to the model
turn, the transcript fold, and the honest copy. The routing itself (a capture returning a
note instead of True, so the message still reaches the model) needs a live Telegram loop
and is verified by reading _route_text — bot.py has no test harness and this file does not
try to build one.

Writes only to a tmpdir; never touches a real athlete directory.
"""
import json
import sys
import tempfile
import time
from datetime import date
from pathlib import Path

_here = Path(__file__).resolve().parent
sys.path.insert(0, str(_here.parent / "lib"))
sys.path.insert(0, str(_here.parent / "telegram"))
import day_overrides as D
import bot as B

FAILED = []


def check(name, cond):
    print(("PASS" if cond else "FAIL"), name)
    if not cond:
        FAILED.append(name)


# --- 1) day_overrides.has_override — the register side of the dedupe ---------------------
tmp = Path(tempfile.mkdtemp(prefix="cap-test-"))
SLUG = "testathlete"

check("has_override is False with no register at all",
      D.has_override(SLUG, tmp, "bike", "2026-08-13") is False)

D.record(SLUG, tmp, "bike", "2026-08-13", D.capture_note("Telegram", date(2026, 8, 13)))
check("has_override sees the entry it just recorded",
      D.has_override(SLUG, tmp, "bike", "2026-08-13") is True)
check("has_override is per DATE, not per sport",
      D.has_override(SLUG, tmp, "bike", "2026-08-20") is False)
check("has_override is per SPORT, not per date",
      D.has_override(SLUG, tmp, "swim", "2026-08-13") is False)
# The ICU activity types collapse onto one family, and the bot may hand either through.
check("has_override collapses Ride/VirtualRide onto bike",
      D.has_override(SLUG, tmp, "Ride", "2026-08-13")
      and D.has_override(SLUG, tmp, "GravelRide", "2026-08-13"))
check("has_override takes a date object as well as a string",
      D.has_override(SLUG, tmp, "bike", date(2026, 8, 13)) is True)

# FAIL-CLOSED. A register that cannot be read must NOT suppress the question: a duplicate
# question is cheap, a permission the athlete was never asked about is not.
D.register_path(SLUG, tmp).write_text("{not json at all", encoding="utf-8")
check("a corrupt register suppresses nothing (fails closed to asking)",
      D.has_override(SLUG, tmp, "bike", "2026-08-13") is False)
# An entry with a non-string / empty note is ignored by load(), so it must not count either.
D.register_path(SLUG, tmp).write_text(json.dumps({"bike:2026-08-13": ""}), encoding="utf-8")
check("an entry with an empty note does not count as recorded",
      D.has_override(SLUG, tmp, "bike", "2026-08-13") is False)


# --- 2) dayrule_repeat_reason — whether to ask at all -----------------------------------
NOW = 1_000_000.0
LIVE = {"family": "bike", "date": "2026-08-13", "expiry": NOW + 60}

check("nothing recorded and nothing pending -> ask",
      B.dayrule_repeat_reason("bike", "2026-08-13", False, None, NOW) == "")
check("already on the register -> do not ask again",
      B.dayrule_repeat_reason("bike", "2026-08-13", True, None, NOW)
      == "already on the register")
# The gate the register alone cannot provide: the 07:08 question is still unanswered at
# 07:11, so nothing is recorded yet and only the pending entry can stop the repeat.
check("same sport+date already asked and unanswered -> do not ask again",
      B.dayrule_repeat_reason("bike", "2026-08-13", False, LIVE, NOW)
      == "already asked, answer outstanding")
check("a pending for a DIFFERENT date does not suppress this one",
      B.dayrule_repeat_reason("bike", "2026-08-20", False, LIVE, NOW) == "")
check("a pending for a DIFFERENT sport does not suppress this one",
      B.dayrule_repeat_reason("swim", "2026-08-13", False, LIVE, NOW) == "")
check("an EXPIRED pending does not suppress the question",
      B.dayrule_repeat_reason("bike", "2026-08-13", False,
                              {"family": "bike", "date": "2026-08-13",
                               "expiry": NOW - 1}, NOW) == "")
check("a pending with no expiry is treated as expired, not as a suppression",
      B.dayrule_repeat_reason("bike", "2026-08-13", False,
                              {"family": "bike", "date": "2026-08-13"}, NOW) == "")
check("recorded wins even when a pending is live (either alone suppresses)",
      B.dayrule_repeat_reason("bike", "2026-08-13", True, LIVE, NOW)
      == "already on the register")
check("dayrule_repeat_reason defaults `now` to the clock",
      B.dayrule_repeat_reason("bike", "2026-08-13", False,
                              {"family": "bike", "date": "2026-08-13",
                               "expiry": time.time() + 60}) != "")


# --- 3) capture_context_note — what the model is told -----------------------------------
note = B.capture_context_note(
    "dayrule", "bike on 2026-08-13",
    "That's not one of your usual *bike* days — is a *bike* on *Thu 13 Aug* deliberate?")
check("the note names what was stored", "bike on 2026-08-13" in note)
check("the note spells out that nothing else changed",
      "BOOKKEEPING" in note and "still outstanding" in note)
check("the note hands the instruction back to the model",
      "live instruction" in note)
check("the note quotes the read-back the athlete already has",
      "> That's not one of your usual" in note and "do not repeat it" in note)
check("an unknown kind degrades to the kind itself rather than raising",
      "somethingnew" in B.capture_context_note("somethingnew", "x"))
check("no question means no quoted read-back",
      "do not repeat" not in B.capture_context_note("race", "x"))
for kind in ("race", "hours", "dayshape", "dayrule", "action"):
    check(f"kind {kind!r} renders a human phrase",
          B._CAPTURE_KINDS[kind] in B.capture_context_note(kind, "x"))


# --- 4) _capture — the router's handle on a fired capture --------------------------------
cap = B._capture("dayrule", "bike on 2026-08-13", "Just to be sure?")
check("a fired capture is truthy (so it still claims the message)", bool(cap))
check("a fired capture carries the note and the sent read-back",
      cap["note"].startswith("## Just recorded") and cap["msg"] == "Just to be sure?")


# --- 5) _capture_history_assistant — one transcript entry per turn -----------------------
# The athlete's message must appear ONCE in history even though the bot sent two messages
# for it. Two entries with the same `user` text read as the instruction being repeated,
# which is the misreading the 13 Aug "Now" reply was built on.
check("read-back and reply fold into one entry, read-back first",
      B._capture_history_assistant("Just to be sure?", "Moving it now.")
      == "Just to be sure?\n\nMoving it now.")
check("a capture with no model reply keeps the read-back",
      B._capture_history_assistant("Just to be sure?", "") == "Just to be sure?")
check("a turn with no capture is the reply, unchanged",
      B._capture_history_assistant("", "Moving it now.") == "Moving it now.")
check("both empty is an empty string, never None",
      B._capture_history_assistant("", "") == "")
check("whitespace-only read-back does not add a blank prefix",
      B._capture_history_assistant("  \n ", "Moving it now.") == "Moving it now.")


# --- 6) honest copy ---------------------------------------------------------------------
q = B.dayrule_question("bike", "Thu 13 Aug")
btns = [b["text"] for b in B.DAYRULE_BUTTONS["inline_keyboard"][0]]
check("the question no longer claims to be about the plan",
      "that's the plan" not in q.lower())
check("the question says what the write actually does (not flagging it)",
      "flag" in q.lower())
check("the question echoes the absolute date, so a misread weekday is visible",
      "Thu 13 Aug" in q and "bike" in q)
check("the yes button promises only not-flagging",
      btns[0] == "Yes — don't flag it" and "plan" not in btns[0].lower())
check("there is still a plain No", btns[1] == "No")
check("the callback data is unchanged (the confirm handler keys off it)",
      [b["callback_data"] for b in B.DAYRULE_BUTTONS["inline_keyboard"][0]]
      == ["__DAYRULE_YES__", "__DAYRULE_NO__"])

done = B.dayrule_recorded_message("bike", "Thu 13 Aug")
check("the post-tap message does not imply the calendar moved",
      "doesn't by itself move anything in your calendar" in done)
check("the post-tap message still says it applies to that day only",
      "usual bike days are unchanged" in done)
check("the post-tap message names the day it applies to", "Thu 13 Aug" in done)


if FAILED:
    print(f"{len(FAILED)} FAILED")
    sys.exit(1)
print("all checks passed")
