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
_STRAVA_CLAIM_RE = re.compile(
    r"\b(updated|written|wrote|added|pushed|set|refreshed|renamed)\b[^.\n]{0,60}\b"
    r"strava\b|\bstrava\b[^.\n]{0,40}\b(description|name|title)\b[^.\n]{0,30}\b"
    r"(updated|written|refreshed|renamed|done)\b", re.IGNORECASE)
_ICU_CLAIM_RE = re.compile(
    r"\b(pushed|added|moved|created|updated|deleted|removed|rescheduled|swapped|shortened|extended)\b"
    r"[^.\n]{0,70}\b(calendar|intervals\.icu|icu|planned session|workout)\b"
    r"|\b(calendar|intervals\.icu)\b[^.\n]{0,40}\b(updated|pushed|done)\b", re.IGNORECASE)
# Language that means "I am about to" / "I could", not "I have". Suppresses the claim:
# a proposal is not an assertion, and verifying one wastes a read and risks a false
# "that didn't save" on a write nobody attempted.
_INTENT_RE = re.compile(
    r"\b(shall i|should i|want me to|i can|i could|do you want|would you like|"
    r"i'?ll |i will |about to|let me know)\b", re.IGNORECASE)


# A claim about the activity's NAME, not its description. Excluded on purpose: we never
# know what name was intended, so it is unverifiable — and treating it as a description
# claim would find an empty description, call the claim false, and retry by writing a
# description. On a sailing activity that would breach a hard rule (water sports are
# renamed only, never described). Unverifiable is a reason to stay silent.
_STRAVA_NAME_ONLY_RE = re.compile(r"\b(renam|name|title|called it)\w*\b", re.IGNORECASE)
_STRAVA_DESC_RE = re.compile(r"\b(description|write-?up|notes?|summary)\b", re.IGNORECASE)


def claim_kinds(reply: str) -> set:
    """Which external writes this reply asserts as DONE: subset of {"strava", "icu"}.
    "strava" means a DESCRIPTION claim specifically — see _STRAVA_NAME_ONLY_RE."""
    if not reply:
        return set()
    kinds = set()
    for sentence in re.split(r"(?<=[.!?\n])\s+", reply):
        if _INTENT_RE.search(sentence):
            continue
        if _STRAVA_CLAIM_RE.search(sentence) and not (
                _STRAVA_NAME_ONLY_RE.search(sentence) and not _STRAVA_DESC_RE.search(sentence)):
            kinds.add("strava")
        if _ICU_CLAIM_RE.search(sentence):
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
_RESULT_FAIL = {
    "strava": "Still not saving to Strava. I've logged it for a fix. Nothing was written, "
              "so don't treat that description as updated.",
    "icu": "Still not saving to your calendar. I've logged it for a fix. Treat your calendar "
           "as unchanged.",
}


def retry_line(kind: str, verdict: str = "absent") -> str:
    if verdict == "unchanged":
        return _RETRY_LINE.get(f"{kind}_unchanged", "That hasn't changed. Retrying now.")
    return _RETRY_LINE.get(kind, "That didn't actually save. Retrying now.")


def result_line(kind: str, ok: bool) -> str:
    return (_RESULT_OK if ok else _RESULT_FAIL).get(
        kind, "Saved this time." if ok else "Still not saving — logged for a fix.")
