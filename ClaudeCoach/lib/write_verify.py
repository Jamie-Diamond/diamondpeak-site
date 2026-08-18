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
# This is the tool-derived half, and it is the SECOND line of defence, not the first: the
# prose fix above catches the incident on its own, and every caller that cannot supply a
# summary keeps that protection. Losing either half is a regression.
#
# It is DELIBERATELY three-valued in the same spirit as the verdicts above, and the third
# value is what makes it safe:
#   a set()      - every tool that ran is provably local (a file read, a file write, a
#                  maths helper, a read-only API call) or no tool ran at all, so no
#                  external write is possible. THE ONLY VALUE THAT SUPPRESSES.
#   a set of kinds - a tool that CAN write externally ran. Fail open to the prose.
#   None         - the summary cannot settle it. Fail open to the prose, which is exactly
#                  today's behaviour, so nothing regresses.
#
# The non-empty set and None behave IDENTICALLY at the call site. The distinction is kept
# because it is the honest one and because the kinds are worth logging. Do NOT be tempted
# to intersect the non-empty set with the prose kinds: _classify_tool is coarse string
# matching over a truncated command hint, and an intersection would let ONE misclassified
# fragment silence a TRUE claim, which is the failure this module exists to prevent.
#
# THE MEMBERSHIP RULE, which matters more than the lists themselves:
#   _TOOL_LOCAL membership is a CLAIM OF PROOF. Omission is the safe default, because
#   omission yields None and None fails open to exactly today's behaviour. If you cannot
#   prove from bot._classify_tool's branch that the fragment forecloses an external write,
#   leave it out. Adding a fragment wrongly is the only way to make this gate HARMFUL: it
#   would suppress verification of a genuine write.
#
# "LOCAL" means "provably no external WRITE". It does NOT mean "no network". The
# intervals.icu READ fragments below are in the list on purpose: they cost a round trip but
# cannot change anything at the far end. A reader who takes LOCAL to mean "offline" will
# delete them and quietly halve the gate's coverage.
#
# COMPOUND BASH COMMANDS: the ordering half is fixed, the truncation half is not
# (17 Aug 2026, logged as a limitation earlier the same day and closed the same day).
#
# What was wrong. One tool_use event yields one fragment, and bot._classify_tool was
# first-match-wins over a flat blob, testing "plan_tools" BEFORE the intervals.icu branch.
# A single chained command of the shape `plan_tools.py session-for-load ... && icu_fetch.py
# push_workout ...` therefore collapsed to the LOCAL fragment "built the session" while
# genuinely pushing to the calendar, and this gate would have suppressed a claim that was
# true. Suppressing a true claim is the one failure the module exists to prevent.
#
# What was done. _classify_tool now tests the five write markers against the WHOLE hint,
# ahead of every other branch, so a marker anywhere wins whatever else the command
# mentions. Deliberately a substring search and not a shell-segment parse: the classifier
# returns ONE triple, so segmentation still needs the same precedence rule, and parsing a
# hard-truncated command only adds ways to LOSE a marker. The reasoning is written out
# above the pre-pass in telegram/bot.py; the short version is that a false write costs
# nothing here (a non-empty set fails open to the prose) and a false local is the bug.
#
# WHAT IS STILL OPEN, so nobody reads this as closed. engine._tool_input_summary truncates
# the hint to 80 characters, and the realistic command above is 99 - `push_workout` is cut
# off upstream and never reaches _classify_tool at all.
#
# The surviving rule is CHARACTER OFFSET, not segment position, and the distinction matters
# because segment position is the intuitive reading and it is wrong. A marker is caught if
# and only if it falls inside the first 80 characters; segment order no longer affects
# anything. `python3 /Users/.../ClaudeCoach/lib/icu_fetch.py push_workout` puts the marker
# at index 99 of its FIRST segment and loses it just the same. So the ordering half is
# closed outright and the residual is one clean condition rather than a vague "long chains
# are risky". The remaining fix belongs in lib/engine.py (a summary that keeps the tail, or
# one fragment per segment) and is not in this module's gift. Pinned by a test below.
#
# The five markers are exactly the five keys of _TOOL_WRITES, and a test asserts that
# correspondence in both directions: a sixth entry added here without a marker in
# _classify_tool would silently reopen the hole, so it fails instead.
#
# These strings are the past-tense fragments from bot._classify_tool, so this module is
# coupled to another file by string literal. That is fragile and known to be; the
# alternative is a fourth element on all ~40 of _classify_tool's return sites, for a guard
# that has to stay easy to reason about. Tests assert the literals are still emitted, so a
# reworded status line fails a test rather than silently turning the gate into a no-op.
# When wording does drift the fragment stops matching, this returns None, and the gate
# quietly stops gating: degradation to the status quo rather than to silence, which is the
# only acceptable direction for a guard whose whole job is catching a lie.
_TOOL_WRITES = {
    # Values are SETS so one fragment can name more than one destination if a future tool
    # ever does both. Combined with |=, never .add().
    "updated intervals.icu": {"icu"},     # push_workout / edit_workout / delete_workout
    "updated it on strava": {"strava"},   # scripts/strava-update-activity.py
    # generate-blueprint.py:340 calls IcuClient.push_workout and stage1-plan.py:1339
    # builds an IcuClient: plan generation really does push the week to the calendar.
    "rebuilt your plan": {"icu"},
    # A REAL TRAP. The status line reads like a local note, but plan_tools log-strength
    # (lib/plan_tools.py:1266) calls IcuClient.create_manual_activity: a genuine ICU write.
    # This is exactly the fragment somebody would wave into _TOOL_LOCAL.
    "logged your strength work": {"icu"},
    # plan_tools render-workout is in fact a PURE renderer: lib/plan_tools.py:1276 returns
    # a description string plus a "how_to_push" note, and primitives/planned_tss.py:348
    # says the result is what push_workout sends separately. Listed as a write ANYWAY, so
    # the gate fails open: the athlete-facing status line says "Writing the workout to
    # intervals.icu" and the model is told to follow it immediately with push_workout.
    # Either reading forbids treating it as proof that nothing was written, which is the
    # only thing this list has to get right.
    "wrote the workout": {"icu"},
}
# Provably incapable of an external write. Each entry is a claim of proof; the evidence is
# the branch of bot._classify_tool that emits it.
_TOOL_LOCAL = frozenset({
    # Claude's own file tools. The strongest proof in the list: _classify_tool reaches
    # these branches only when the TOOL NAME is read/grep/glob or write/edit/multiedit,
    # and those tools cannot reach the network at all. "checked your data" and "saved your
    # data" are the two fragments from the 16 Aug incident turn.
    "checked your data", "saved your data", "checked your recent training",
    "checked your session log", "read your blueprint", "checked your heat log",
    "checked your notes", "read your plan",
    "updated your session log", "saved your preference", "updated your plan",
    "logged your heat dose",
    # intervals.icu READS. Network, but read-only endpoints.
    "checked your wellness", "read your fitness",
    "read the session detail", "checked your activities",
    # scripts/heat_accl.py: 64 lines, reads the heat log through lib/heat.py and prints an
    # ASCII trend. lib/heat.py's only network calls are open-meteo GETs. No write anywhere.
    "checked your heat dose",
    # plan_tools.py subcommands that are pure calculation or read-only, each checked
    # against lib/plan_tools.py rather than inferred from the status line.
    "built the session", "read the session load", "added up your week",
    "worked out your load target", "projected your fitness", "sense-checked the week",
    "predicted your race", "worked out your fuelling", "worked out your sweat rate",
    "checked the wetsuit call", "worked out the load",
})
# Deliberately in NEITHER list, so they fail open: "crunched the numbers" (the unmapped-tool
# catch-all, which could be anything including a bare Bash command), "checked intervals.icu"
# and "crunched your plan" (the fallbacks inside their own branches, reached by an endpoint
# or subcommand nobody enumerated), and "synced your log" / "built your race plan" (whole
# scripts not audited line by line for a push). None of those can rule a write OUT.

# The zero-tool collapse line, built by bot.call_claude_streaming when no tool ran at all.
# The purest case the gate has: nothing ran, so nothing external happened, so any claim of
# a write in the prose is retrospective. Exactly the incident shape.
_THOUGHT_ONLY_RE = re.compile(r"^thought for \d+\s*s$", re.IGNORECASE)


def tool_summary_kinds(tool_summary) -> set | None:
    """Which external writes the tools that ACTUALLY ran this turn could have performed.

    `tool_summary` is bot.call_claude_streaming's collapse line, ", ".join(past-tense
    fragments) with character zero upper-cased, e.g. "Checked your data, saved your data".
    Returns set() (provably none), a non-empty subset of {"strava", "icu"}, or None for
    "cannot settle". Only set() suppresses; see the note above.

    It splits on the comma rather than substring-searching the whole line, because a
    substring search cannot tell "every fragment is local" from "one fragment is local and
    another is unrecognised", and that difference is the entire three-valued design."""
    if tool_summary is None:
        return None
    text = str(tool_summary).strip().rstrip(".")
    if not text:
        return None                   # no summary at all: settles nothing
    if _THOUGHT_ONLY_RE.match(text):
        return set()                  # no tool ran, so nothing external was touched
    kinds = set()
    unrecognised = False
    for fragment in text.split(","):
        f = fragment.strip().rstrip(".").lower()
        if not f:
            continue
        if f in _TOOL_WRITES:
            kinds |= _TOOL_WRITES[f]
        elif f not in _TOOL_LOCAL:
            unrecognised = True       # keep scanning: a later fragment may name a writer
    if kinds:
        return kinds                  # a writer ran: fail open to the prose
    return None if unrecognised else set()


def claim_kinds(reply: str, tool_summary=None) -> set:
    """Which external writes this reply asserts as DONE: subset of {"strava", "icu"}.
    "strava" means a DESCRIPTION claim specifically — see _STRAVA_NAME_ONLY_RE.

    `tool_summary` is bot's one-line tool-collapse summary for the SAME model call that
    produced `reply`, when the caller has it. It can only ever SUBTRACT, and only on proof
    that no tool capable of an external write ran. Omit it and behaviour is unchanged,
    which is why every caller that cannot supply one is free to leave it None."""
    if not reply:
        return set()
    # The gate runs FIRST and returns early: if no tool this turn could have written
    # anything, no amount of prose makes a write have happened, and the two network reads
    # the prose path would provoke are pure waste. Note what is NOT here: no intersection
    # of tool kinds with prose kinds. A non-empty gate and an unsettled gate both fall
    # straight through to the prose.
    if tool_summary is not None:
        gate = tool_summary_kinds(tool_summary)
        if gate is not None and not gate:
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
    return kinds


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
# that line with no evidence behind it. bot._verify_external_writes used to compute the
# retry's outcome as `ok = _verify_icu_calendar_claim(slug) == "ok"`, which folds "unknown"
# (the read-back failed, we know nothing) in with "absent" (proved nothing is there). So a
# post-retry network hiccup, on a retry that may well have worked, still asserted the
# calendar was unchanged. That is the exact accusation-on-unknown this module's own
# docstring forbids. bot now keeps the post-retry verdict and passes it as `verdict=`.
#
# WHICH WAY THE DEFAULT LEANS, and why it is not the other way. The absolute wording needs
# a PROVED verdict ("absent"/"unchanged") behind it; everything else, INCLUDING verdict=None,
# gets the hedge. It is tempting to argue the opposite - that a caller who passes no verdict
# has already proved the failure in its bool - and it is wrong, because `ok=False` is what a
# caller returns when it could not establish anything either way. A bare bool cannot tell
# "proved nothing is there" from "the read-back fell over", and the assertive copy states
# the first about both. Hedging costs an under-claim on a proved failure; the alternative
# costs a false accusation, which is the thing this module exists to prevent.
#
# THE STRAVA CALLER, which used to be the worked example of that (fixed 17 Aug 2026, later
# the same day). bot's Strava branch really did reduce its retry to a bool, and
# _retry_strava_description really did return False on two paths with no proof at all: when
# the read-back itself failed (desc came back None, which strava_desc_verdict scores
# "absent" purely because the text it never received is empty), and on the early return
# after the 120s subprocess timeout, which may have written before it died. Left on the
# hedge, that was honest. What it was not was complete: the SAME False also carried a
# genuine proved absence - a description read back and found empty - so every Strava ops
# alert said "could not be confirmed" even when the failure was established, and a real
# signal read as noise. _retry_strava_description now returns (ok, verdict): the two
# unproved paths return "unknown" instead of a fabricated "absent", the proved ones return
# "absent"/"unchanged", and bot passes it as verdict= exactly as the calendar branch does.
# The rule above is unchanged; it simply no longer has a caller forced to under-claim.
_RESULT_FAIL_UNPROVED = {
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
    retry. Pass it whenever you have it: only "absent"/"unchanged" unlock the absolute
    wording. "unknown", and an omitted verdict, both hedge - see above."""
    if ok:
        return _RESULT_OK.get(kind, "Saved this time.")
    table = _RESULT_FAIL_PROVED if verdict in ACTIONABLE else _RESULT_FAIL_UNPROVED
    return table.get(kind, "Still not saving - logged for a fix.")


# ---------------------------------------------------------------------------
# Naming what changed, for a turn that was STOPPED part-way (bug #30, 17 Aug 2026)
# ---------------------------------------------------------------------------
#
# Everything above answers "did the write the bot CLAIMED actually land?". A cancelled
# turn asks the harder-sounding but simpler question: the bot never got to claim anything,
# so what, if anything, did it manage to do before it was killed?
#
# Jamie's instruction when asked how strong the guarantee had to be: "It doesn't have to
# guarantee it, but it should be aware of what it hasn't changed so it knows how to rectify
# it." A kill mid-turn cannot be atomic - the model may already have pushed one session of
# a three-session replan - so the deliverable is not "nothing happened", it is an HONEST
# account plus, where the change is cleanly reversible, the means to reverse it.
#
# icu_events_verdict is the wrong tool for that job and deliberately stays as it is: it
# collapses the whole window to one frozenset and answers yes/no. Naming a day and a
# session needs the events themselves, so these functions take event LISTS. They are pure -
# no network, no transport - because the athlete-facing wording is the part most worth
# testing and the part most easily got wrong.


def _event_key(e) -> str:
    return str((e or {}).get("id") or "")


def _event_shape(e) -> tuple:
    """The comparable part of one event. Same fields as events_fingerprint, minus the id,
    so two versions of the SAME event can be compared for "did it move/rename/rescale"."""
    e = e or {}
    return (
        (e.get("start_date_local") or "")[:10],
        e.get("type") or "",
        (e.get("name") or "").strip(),
        e.get("icu_training_load") or e.get("load_target") or 0,
        round((e.get("moving_time") or 0) / 60),
    )


def events_diff(before_events, after_events) -> dict:
    """What moved between two reads of the planned-event window.

    Returns {"created": [event], "deleted": [event], "edited": [(before, after)]}.

    Classified on ID SET MEMBERSHIP, not on tuple equality of the fingerprint. The
    fingerprint carries the id, so an edited event shows up there as one tuple leaving and
    another arriving - i.e. indistinguishable from a delete plus an unrelated create, which
    is precisely the misreading that would have an undo delete a session the athlete
    wanted. Same id on both sides means it was EDITED, and an edit is the case we refuse to
    reverse (see bot._undo_cancelled_change)."""
    before = {_event_key(e): e for e in (before_events or []) if _event_key(e)}
    after = {_event_key(e): e for e in (after_events or []) if _event_key(e)}
    created = [after[k] for k in after if k not in before]
    deleted = [before[k] for k in before if k not in after]
    edited = [(before[k], after[k]) for k in before
              if k in after and _event_shape(before[k]) != _event_shape(after[k])]
    for bucket in (created, deleted):
        bucket.sort(key=lambda e: ((e.get("start_date_local") or ""), _event_key(e)))
    edited.sort(key=lambda p: ((p[0].get("start_date_local") or ""), _event_key(p[0])))
    return {"created": created, "deleted": deleted, "edited": edited}


def diff_is_empty(diff) -> bool:
    return not any((diff or {}).get(k) for k in ("created", "deleted", "edited"))


def describe_event(e) -> str:
    """One event in the athlete's terms: the DAY first, because the day is what they
    recognise and the day is what they need to go and look at. No event ids - the athlete
    has never seen one and quoting it reads as the bot talking to itself."""
    e = e or {}
    day = (e.get("start_date_local") or "")[:10]
    try:
        from datetime import date as _date
        when = _date.fromisoformat(day).strftime("%a %-d %b")
    except Exception:
        when = day or "an unknown day"
    name = (e.get("name") or "").strip() or (e.get("type") or "session")
    load = e.get("icu_training_load") or e.get("load_target") or 0
    tail = f" ({int(load)} TSS)" if load else ""
    return f"{when}: {name}{tail}"


def describe_diff(diff) -> list:
    """The change, as the lines the athlete is shown. One bullet per event, grouped by what
    happened to it, so a three-session replan that got one session in reads as one line and
    not as a paragraph of hedging."""
    lines = []
    for e in (diff or {}).get("created", []):
        lines.append(f"• added {describe_event(e)}")
    for e in (diff or {}).get("deleted", []):
        lines.append(f"• removed {describe_event(e)}")
    for b, a in (diff or {}).get("edited", []):
        lines.append(f"• changed {describe_event(b)} → {describe_event(a)}")
    return lines
