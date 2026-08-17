#!/usr/bin/env python3
"""Verify-after-write for the two EXTERNAL writes the logs caught the bot lying about.

WHY (13 Aug 2026 audit). Four times in May-June Jamie asked, in his own words, "did you
actually update Strava or just say you did?" and the honest answer was "No". The local
version of this problem is already guarded — bot._verify_logged_reply refuses to confirm
a capture unless a file's mtime moved — but that guard cannot see an external write, so a
claimed Strava description or a claimed calendar push is still taken on trust.

This module is the same philosophy pointed outward, with one deliberate asymmetry that
the caller must respect: PROVING a write happened is often impossible (we rarely know the
exact text that was meant to land), while proving it did NOT is often easy. So the verdict
is three-valued and the honest-but-quiet verdict is the default:

  "absent"    - proved not written (nothing is there). Tell the athlete, retry once.
  "unchanged" - proved nothing CHANGED, which is all that can be known when a baseline
                exists: a write of byte-identical text is indistinguishable from no write,
                and the athlete-facing wording must therefore be about the outcome
                ("unchanged"), not the mechanism, or it states more than we know.
  "unknown"   - cannot be proved either way. Say NOTHING extra; log it.
  "ok"        - proved written (exact-text read-back only).

Never accuse on "unknown". A false "that didn't save" is worse than the status quo,
because it teaches the athlete to distrust confirmations that were true.
"""
from __future__ import annotations

import re

# Reply text that CLAIMS an external write. Kept narrow and verb-led for the same reason
# bot._CAPTURE_CONFIRM_RE is: a loose pattern fires on discussion of a write ("I could
# push that to your calendar") and spends a network read on every planning chat.
#
# The gaps are NON-GREEDY (17 Aug 2026): the patterns are still used as booleans, so this
# cannot change whether a claim is found, but it keeps each match tight enough that the
# clause test below reads the right clause, and stops one claim's span swallowing the next
# one in the same sentence.
_STRAVA_CLAIM_RE = re.compile(
    r"\b(updated|written|wrote|added|pushed|set|refreshed|renamed)\b[^.\n]{0,60}?\b"
    r"strava\b|\bstrava\b[^.\n]{0,40}?\b(description|name|title)\b[^.\n]{0,30}?\b"
    r"(updated|written|refreshed|renamed|done)\b", re.IGNORECASE)
_ICU_CLAIM_RE = re.compile(
    r"\b(pushed|added|moved|created|updated|deleted|removed|rescheduled|swapped|shortened|extended)\b"
    r"[^.\n]{0,70}?\b(calendar|intervals\.icu|icu|planned session|workout)\b"
    r"|\b(calendar|intervals\.icu)\b[^.\n]{0,40}?\b(updated|pushed|done)\b", re.IGNORECASE)
# Language that means "I am about to" / "I could", not "I have". Suppresses the claim:
# a proposal is not an assertion, and verifying one wastes a read and risks a false
# "that didn't save" on a write nobody attempted.
_INTENT_RE = re.compile(
    r"\b(shall i|should i|want me to|i can|i could|do you want|would you like|"
    r"i'?ll |i will |about to|let me know)\b", re.IGNORECASE)

# WHY (17 Aug 2026 incident). _INTENT_RE only ever looked FORWARD. On 16 Aug at 21:57
# Jamie asked for a bug to be logged and the reply reassured him about a plan pushed in an
# EARLIER turn: "The week I pushed earlier is still on the calendar and is correct; the
# failure was the overnight generator re-running against the old day rules." _ICU_CLAIM_RE
# read "pushed earlier is still on the calendar" as a fresh write, found no new events
# (correctly - nothing was being written), and told him "That didn't actually save to your
# calendar" and then "Treat your calendar as unchanged". Both false, and the retry path
# re-asks the model to perform a calendar write nobody wanted, so an unsuppressed
# retrospective claim can MUTATE a calendar that was already right. That is the worst
# outcome this module can produce, and it came from prose about the past.
#
# So: a claim is about THIS turn. Two constructions say otherwise and are suppressed here.
#   persistence  - existing state asserted to survive ("is still on", "remains", "unchanged
#                  from", "already there"). Nothing is being done; something is being
#                  described.
#   retrospective- the write verb is attributed to an earlier turn or an earlier day
#                  ("pushed earlier", "wrote yesterday", "in my last message").
#
# Every marker is anchored to its CONSTRUCTION, never left as a bare word, because this bot
# reports duration and load in the same breath as a write: "Pushed Thursday's swim to your
# calendar - still a 40 minute session" and "Moved Friday's ride on your calendar, load
# unchanged" are genuine writes that a bare `still` or `unchanged` would silence. Likewise
# the possessive lookaheads: "yesterday" is a time of action, "yesterday's" is the name of a
# session, and "Moved yesterday's ride onto Thursday" is a real write.
#
# The balance is deliberately tilted towards suppressing. Over-suppression costs a silent
# skip - back to the pre-13-Aug status quo for one reply, with nothing said to the athlete.
# Under-suppression costs a false accusation, a retraction the athlete cannot check, and a
# model-driven write to a calendar that was already correct.
_RETRO_RE = re.compile(
    r"\b(?:is|are|was|were|it'?s|they'?re|that'?s)\s+still\b"
    r"|\bstill\s+(?:on|in|there|sitting|scheduled|set|live|correct|right|stands|shows)\b"
    r"|\b(?:is|are|was|were|remains?|stays?|stayed|left)\s+unchanged\b"
    r"|\bunchanged\s+(?:from|since)\b"
    r"|\b(?:remains?|remained)\s+(?:on|in|as|at|the|your|there|correct|intact|in place)\b"
    r"|\b(?:stays?|stayed)\s+(?:on|in|as|where|there|put)\b"
    r"|\b(?:untouched|intact|as it was|as before|no change)\b"
    r"|\b(?:is|are|was|were|had|have|has|i'?d|i'?ve)\s+already\b"
    r"|\balready\s+(?:on|in|there|live|sitting|scheduled|pushed|added|written|wrote|"
    r"updated|created|moved|done|been|has|have|had)\b"
    r"|\bearlier\b(?!\s+(?:in|than|to|at|start|finish))"
    r"|\bpreviously\b"
    r"|\byesterday\b(?!['’]s)"
    r"|\blast week\b(?!['’]s)"
    r"|\b(?:last night|this morning|over the weekend|at the weekend)\b(?!['’]s)"
    r"|\bfrom (?:before|earlier|the earlier|my earlier|our earlier)\b"
    r"|\bin (?:my|an|the) (?:earlier|last|previous) (?:message|reply|note|answer)\b"
    r"|\bwhen i (?:pushed|wrote|added|updated|built|made|created)\b",
    re.IGNORECASE)

# _RETRO_RE is tested against a WINDOW around each claim, not against the whole sentence,
# because the two live one semicolon apart in real replies: "Pushed Thursday's swim to your
# calendar; Tuesday's ride is unchanged" is a genuine write followed by an aside, and a
# sentence-wide test would throw the write away.
#
# The window is bounded by the marks that separate independent statements - semicolon and
# dashes - and NOT by the comma, which routinely sits inside a single claim ("Pushed
# Thursday's swim, the 2km one, to your calendar"). It is bounded by the NEIGHBOURING CLAIMS
# too, which is what makes these two behave differently on the same "and":
#   "The week I pushed earlier is still on the calendar and I've now added Thursday's swim
#    to your calendar."   -> the second claim gets its own window and survives.
#   "I pushed the week to your calendar and it's still there."
#                          -> one claim, one window, the whole sentence, suppressed.
# Splitting on a bare "and" would have got the first right and the second wrong.
_CLAUSE_SPLIT_RE = re.compile(r";|—|–|(?<= )-(?= )")


def _asserts_now(sentence: str, pattern) -> bool:
    """True if `sentence` claims `pattern`'s write happened in THIS turn.

    Every match is weighed, not just the first: a sentence that recalls an old write and
    then makes a new one is still making the new one."""
    spans = [m.span() for m in pattern.finditer(sentence)]
    if not spans:
        return False
    seps = [m.span() for m in _CLAUSE_SPLIT_RE.finditer(sentence)]
    for i, (start, end) in enumerate(spans):
        lo = max([se for ss, se in seps if se <= start] + [0])
        hi = min([ss for ss, se in seps if ss >= end] + [len(sentence)])
        if i:
            lo = max(lo, spans[i - 1][1])
        if i + 1 < len(spans):
            hi = min(hi, spans[i + 1][0])
        # Never read a window that excludes part of the claim itself: a marker sitting
        # inside the matched span belongs to that claim whatever the punctuation says.
        if not _RETRO_RE.search(sentence[lo:max(hi, end)]):
            return True
    return False


# A claim about the activity's NAME, not its description. Excluded on purpose: we never
# know what name was intended, so it is unverifiable — and treating it as a description
# claim would find an empty description, call the claim false, and retry by writing a
# description. On a sailing activity that would breach a hard rule (water sports are
# renamed only, never described). Unverifiable is a reason to stay silent.
_STRAVA_NAME_ONLY_RE = re.compile(r"\b(renam|name|title|called it)\w*\b", re.IGNORECASE)
_STRAVA_DESC_RE = re.compile(r"\b(description|write-?up|notes?|summary)\b", re.IGNORECASE)


# WHY (17 Aug 2026, same incident). Reading the prose was always the weak way to answer
# "did this turn write to intervals.icu?", because the bot already KNOWS: it classifies
# every tool_use event as it streams and collapses them into a one-line summary. On the
# turn that misfired, that summary was "Checked your data, saved your data" - a Read and a
# local file Write, neither of which can reach intervals.icu. Kathryn's genuine calendar
# write three seconds earlier logged "... updated intervals.icu ...". The evidence was
# sitting in the same function and the verifier never asked for it.
#
# This is the tool-derived half. It is DELIBERATELY three-valued in the same spirit as the
# verdicts above, and the third value is what makes it safe:
#   a set()      - every tool that ran is provably local (a file read, a file write, a
#                  maths helper) or no tool ran at all, so no external write is possible.
#   a set of kinds - a tool that CAN write externally ran.
#   None         - the summary cannot settle it. Fail open to the prose, which is exactly
#                  today's behaviour, so nothing regresses.
#
# None is also what an UNRECOGNISED fragment returns, and that matters more than the lists
# themselves: these strings are the past-tense fragments from bot._classify_tool, so this
# module is coupled to another file by string literal. When that file's wording drifts, the
# fragment stops matching, this returns None, and the gate quietly stops gating. Drift
# degrades to the status quo rather than to silence, which is the only acceptable direction
# for a guard whose whole job is catching a lie.
_TOOL_WRITES = {
    "updated intervals.icu": {"icu"},     # push_workout / edit_workout / delete_workout
    "wrote the workout": {"icu"},         # plan_tools render-workout writes to intervals.icu
    "rebuilt your plan": {"icu"},         # plan generation pushes the week to the calendar
    "updated it on strava": {"strava"},
}
# Provably incapable of an external write: file reads, local file writes, pure maths.
_TOOL_LOCAL = (
    "checked your data", "saved your data", "checked your recent training",
    "checked your session log", "read your blueprint", "checked your heat log",
    "checked your notes", "read your plan",
    "updated your session log", "saved your preference", "updated your plan",
    "logged your heat dose", "checked your wellness", "read your fitness",
    "read the session detail", "checked your activities", "checked your heat dose",
    "built the session", "read the session load", "added up your week",
    "worked out your load target", "projected your fitness", "sense-checked the week",
    "predicted your race", "worked out your fuelling", "worked out your sweat rate",
    "checked the wetsuit call", "worked out the load",
)
# Everything else is UNKNOWN on purpose. "crunched the numbers" is the catch-all for any
# unmapped tool including a bare Bash command, "checked intervals.icu" is the catch-all for
# an unrecognised icu_fetch subcommand, and "built your race plan" / "synced your log" /
# "logged your strength work" run scripts that may or may not push. None of those can be
# used to rule a write OUT.
_TOOL_NO_TOOLS_RE = re.compile(r"^thought for \d+s$", re.IGNORECASE)


def tool_summary_kinds(tool_summary) -> set | None:
    """Which external writes the tools that ACTUALLY ran this turn could have performed.

    `tool_summary` is bot.call_claude_streaming's collapse line, e.g. "Checked your data,
    saved your data". Returns None when it cannot be settled - see the note above; None is
    the safe answer and the caller must fall back to the prose."""
    text = (tool_summary or "").strip().rstrip(".")
    if not text:
        return None
    if _TOOL_NO_TOOLS_RE.match(text):
        return set()                  # no tool ran, so nothing external was touched
    kinds = set()
    for fragment in text.split(","):
        f = fragment.strip().lower()
        if not f:
            continue
        if f in _TOOL_WRITES:
            kinds |= _TOOL_WRITES[f]
        elif f not in _TOOL_LOCAL:
            return None               # unrecognised: cannot rule a write out
    return kinds


def claim_kinds(reply: str, tool_summary=None) -> set:
    """Which external writes this reply asserts as DONE: subset of {"strava", "icu"}.
    "strava" means a DESCRIPTION claim specifically — see _STRAVA_NAME_ONLY_RE.

    `tool_summary`, when the caller can supply it, is the stronger evidence and narrows the
    result to writes the tools that ran could actually have performed. Omitted, the prose
    stands alone (see tool_summary_kinds)."""
    if not reply:
        return set()
    kinds = set()
    for sentence in re.split(r"(?<=[.!?\n])\s+", reply):
        if _INTENT_RE.search(sentence):
            continue
        if _asserts_now(sentence, _STRAVA_CLAIM_RE) and not (
                _STRAVA_NAME_ONLY_RE.search(sentence) and not _STRAVA_DESC_RE.search(sentence)):
            kinds.add("strava")
        if _asserts_now(sentence, _ICU_CLAIM_RE):
            kinds.add("icu")
    possible = tool_summary_kinds(tool_summary)
    return kinds if possible is None else (kinds & possible)


def _norm(text) -> str:
    """Whitespace-insensitive comparison form. Strava returns descriptions with its own
    newline handling, so an exact == on raw text gives false 'absent' verdicts."""
    return re.sub(r"\s+", " ", (text or "")).strip()


def strava_desc_verdict(after: str, expected: str | None = None,
                        before: str | None = None) -> str:
    """Verdict on a claimed Strava description write, read back from the API.

    `expected` is the exact text WE wrote (the bot's own writes in activity-watcher know
    this, so those get an absolute verdict). `before` is the description seen at the last
    verification of the same activity: if the model claims an update and the text has not
    moved since we last looked, the claim is false. With neither, only an empty
    description proves anything."""
    a = _norm(after)
    if expected is not None:
        e = _norm(expected)
        if not e:
            return "unknown"          # nothing meaningful was meant to land
        return "ok" if a == e else "absent"
    if not a:
        return "absent"               # a claimed update cannot leave it blank
    if before is not None and a == _norm(before):
        return "unchanged"            # byte-identical to the baseline — nothing changed
    return "unknown"


def icu_events_verdict(before_fp, after_fp, snapshot_age_s: float,
                       max_age_s: float = 300.0, touched_in_turn: bool = False) -> str:
    """Verdict on a claimed calendar write.

    `*_fp` are fingerprints of the planned-event window (see events_fingerprint).
    `touched_in_turn` is for the absolute case where an event's own created/updated
    timestamp lands inside the turn — trusted when the caller can establish it.

    Fails to "unknown" whenever the before-snapshot is missing or older than max_age_s:
    an unrelated earlier write would show as a difference, so a stale snapshot can only
    ever produce a WRONG accusation, never a right one."""
    if touched_in_turn:
        return "ok"
    if before_fp is None or snapshot_age_s is None or snapshot_age_s > max_age_s:
        return "unknown"
    if after_fp is None:
        return "unknown"              # read failed; silence beats a guess
    if after_fp != before_fp:
        return "ok"
    return "absent"


def events_fingerprint(events) -> frozenset:
    """Identity of a planned-event window: enough per event that a move, rename, load
    change or deletion all show up, and nothing that ICU churns on its own."""
    fp = set()
    for e in events or []:
        fp.add((
            str(e.get("id") or ""),
            (e.get("start_date_local") or "")[:10],
            e.get("type") or "",
            (e.get("name") or "").strip(),
            e.get("icu_training_load") or e.get("load_target") or 0,
            round((e.get("moving_time") or 0) / 60),
        ))
    return frozenset(fp)


ACTIONABLE = ("absent", "unchanged")   # verdicts that speak to the athlete and retry


# Athlete-facing copy. One line, no jargon, and it promises only what happens next. The
# "unchanged" wording is deliberately weaker than the "absent" wording: it is the strongest
# claim the evidence supports.
_RETRY_LINE = {
    "strava": "That didn't actually save to Strava. Retrying now.",
    "icu": "That didn't actually save to your calendar. Retrying now.",
    "strava_unchanged": "Your Strava description is unchanged from before. Retrying now.",
    "icu_unchanged": "Your calendar is unchanged from before. Retrying now.",
}
_RESULT_OK = {
    "strava": "Saved to Strava this time.",
    "icu": "Saved to your calendar this time.",
}
# WHY the failure copy was softened (17 Aug 2026, same incident). "Treat your calendar as
# unchanged" is an instruction, and Jamie acts on instructions. It was shown to him on a
# turn where nothing was being written and his calendar was perfectly fine.
#
# The hallucinated claim is fixed above, but there is a second, independent way to reach
# that line with no evidence behind it. bot._verify_external_writes computes the retry's
# outcome as `ok = _verify_icu_calendar_claim(slug) == "ok"`, which folds "unknown" (the
# read-back failed, we know nothing) in with "absent" (proved nothing is there). So a
# post-retry network hiccup, on a retry that may well have worked, still asserts the
# calendar is unchanged. That is the exact accusation-on-unknown this module's own
# docstring forbids.
#
# So the DEFAULT wording now states only what holds under both readings: the save was not
# confirmed, go and look. `verdict` restores the absolute wording for a caller that can
# prove the negative. Nothing passes it yet - the fix belongs in bot.py, out of scope here.
_RESULT_FAIL = {
    "strava": "Still not saving to Strava, and I've logged it for a fix. I couldn't confirm "
              "the description landed, so check the activity before relying on it.",
    "icu": "Still not saving to your calendar, and I've logged it for a fix. I couldn't "
           "confirm the change landed, so check your calendar before relying on it.",
}
_RESULT_FAIL_PROVED = {
    "strava": "Still not saving to Strava. I've logged it for a fix. Nothing was written, "
              "so don't treat that description as updated.",
    "icu": "Still not saving to your calendar. I've logged it for a fix. Treat your calendar "
           "as unchanged.",
}


def retry_line(kind: str, verdict: str = "absent") -> str:
    if verdict == "unchanged":
        return _RETRY_LINE.get(f"{kind}_unchanged", "That hasn't changed. Retrying now.")
    return _RETRY_LINE.get(kind, "That didn't actually save. Retrying now.")


def result_line(kind: str, ok: bool, verdict: str | None = None) -> str:
    """The line sent after the retry. `verdict` is the verdict of the read-back AFTER the
    retry: pass it only when the failure is PROVED ("absent"/"unchanged"), never on
    "unknown" - see above. Omitted, the wording hedges, which is always safe."""
    if ok:
        return _RESULT_OK.get(kind, "Saved this time.")
    table = _RESULT_FAIL_PROVED if verdict in ACTIONABLE else _RESULT_FAIL
    return table.get(kind, "Still not saving - logged for a fix.")
