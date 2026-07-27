#!/usr/bin/env python3
"""
Shared rule-capture guard — the TIER A "fold-on-write" invariant, extracted so both
session-sync.py (hourly, silent) and telegram/bot.py (live, athlete-facing) apply the
exact same deterministic check to a persistent-rules.md edit, instead of maintaining
two copies that could drift.

The rule surface used to be append-only: a refinement of an existing rule's topic (a
new nutrition item, a long-run progression nuance) could only land as a NEW near-
duplicate [perm] line, which then needed a nightly human-reviewed merge — one card per
refinement. The fix lets the model FOLD a refinement into the rule it extends (edit in
place, keeping every fact) instead of appending a near-dup. `enforce_rule_guards` is the
backstop that makes folding safe: an in-place edit that removes or rewrites an existing
[perm] line is permitted ONLY when every removed rule is ABSORBED by some [perm] line
still on file (see `_absorbs`: every number preserved, and most content words carried
over, so a silently-changed figure like 750mg->700mg fails). A rule the athlete has
CONFIRMED is held to a stricter bar again - it may be extended in place but never
reworded away. Any lossy edit fails
the invariant and the WHOLE write is refused — the caller gets `before_text` back,
untouched. A newly-appended [perm] line is still reverted if it contradicts a confirmed
preference, exactly duplicates an existing line, or would push the pile over the
standing-rule ceiling (unchanged append-guard behaviour).

Callers are responsible for snapshotting `before_text` themselves (before the model
runs) and re-reading `after_text` (after it has had a chance to edit the file); this
module only judges the diff and never touches disk.
"""
import importlib.util
import re
from collections import Counter
from pathlib import Path

_LIB_DIR    = Path(__file__).parent            # ClaudeCoach/lib/
_BASE       = _LIB_DIR.parent                  # ClaudeCoach/
_BUGFIXER   = _BASE / "scripts/bug-fixer.py"

# bug-fixer.py is hyphenated so it cannot be `import`ed by name; load it by path. It is
# the single source of truth for the ceiling, the confirmed-preference scan and the
# engine-injected rule constants — a failure here raises loudly rather than silently
# skipping the guard.
_bf_spec  = importlib.util.spec_from_file_location("cc_bug_fixer", str(_BUGFIXER))
bug_fixer = importlib.util.module_from_spec(_bf_spec)
_bf_spec.loader.exec_module(bug_fixer)

CEILING = bug_fixer.RULE_COUNT_CEILING

# A [perm] standing rule (the append-only lines that bloated); [expires:] lines carry a date.
_PERM_RE    = re.compile(r"^\s*\[perm\]", re.I)
_EXPIRES_RE = re.compile(r"^\s*\[expires:(\d{4}-\d{2}-\d{2})\]", re.I)
_TAG_RE     = re.compile(r"^\s*\[(?:perm|expires:[^\]]*)\]\s*", re.I)


def _norm(line: str) -> str:
    """Whitespace/case-insensitive normal form for exact-string comparison."""
    return re.sub(r"\s+", " ", line.strip()).lower()


def _content_key(line: str) -> str:
    """Normalised rule CONTENT (tag stripped; case/whitespace-insensitive; surrounding
    punctuation trimmed). Deliberately NOT token-set based: bug_fixer._tokens drops digits and
    short words, which would wrongly merge distinct enumerated rules ('rule 5' vs 'rule 6').
    Two lines with the same content key are the same rule bar case / spacing / trailing
    punctuation — a genuinely safe duplicate. Reordered or reworded variants differ here by
    design and fall through to the reviewed prune (the safe direction)."""
    s = re.sub(r"\s+", " ", _TAG_RE.sub("", line)).strip().lower()
    return s.strip(".,;:!?-–— ")


def _sig_tokens(line: str) -> set:
    """Significant tokens of a rule's CONTENT, with NUMBERS PRESERVED. Tag stripped, lower-cased,
    split on non-alphanumerics; single-letter noise dropped, pure digits kept. Used only by the
    fold invariant below: a removed rule is loss-free ONLY if this token set survives inside a
    rule still on file. Keeping digits means a silently-changed figure (e.g. 750mg -> 700mg) drops
    the '750' token and so FAILS the invariant — the guard refuses rather than let a number drift."""
    s = _TAG_RE.sub("", line).lower()
    return {t for t in re.findall(r"[a-z0-9]+", s) if len(t) >= 2 or t.isdigit()}


# Fold relaxation, 27 Jul 2026. The invariant used to demand that a removed rule's FULL
# significant-token set (numbers included) be a subset of some surviving rule. Any
# rewording during a fold or merge drops a token, so the guard aborted and the intended
# in-place fold silently downgraded to a human review card - exactly the churn the July
# fold-on-write work (f841fb8, cd47f9a, 87fefaf) existed to remove. Relaxed deliberately,
# not deleted: NUMBERS must still all survive (that check is earning its keep - it stops
# figures drifting), while PROSE may be reworded so long as most of the removed rule's
# content words still appear in the rule that absorbed it.
# At most this PERCENTAGE of a removed rule's content words may be reworded away...
# Integer percent, not a 0.80 float: `int(15 * (1.0 - 0.80))` is 2, not 3, because
# 1.0 - 0.80 == 0.19999999999999996, so a float budget came out a word tighter than
# documented at every size where the product lands just under a whole number.
FOLD_PROSE_LOSS_PCT = 20
# ...but always allow this many, so SHORT rules are not held to an unreachable standard.
# A nine-content-word rule reworded from "Drinks 500ml of water before every long run"
# to "500ml water sipped..." loses two low-information words and lands at 0.78 - a
# faithful reword that a flat ratio would abort, which is the churn being fixed. On a
# long rule the ratio always binds first, so this floor loosens nothing where the words
# are plentiful.
FOLD_PROSE_MIN_ALLOWANCE = 2
# ...and only for rules long enough that the ratio alone is unreasonably strict. On a
# SHORT rule the floor would be a hole, not a kindness: "Swim Tuesday and Thursday
# mornings" -> "Swim Tuesday mornings" loses one content word out of four, which is a
# dropped training day, not a rewording. Below this many content words the ratio binds
# alone, so such a rule must be folded essentially word-complete.
FOLD_PROSE_FLOOR_MIN_TOKENS = 6

_DIGITS_RE = re.compile(r"\d+")


def _digit_runs(line: str) -> set:
    """Every run of digits in a rule's CONTENT: '750mg ... PF 30 CHEW' -> {'750', '30'}.

    Digit RUNS rather than _sig_tokens' whole tokens, because the token splitter makes
    figure-preservation depend on spelling: re-spacing "PF 30 CHEW" to "PF30 CHEW" drops
    the bare token '30' though the figure is plainly still there, and that alone was enough
    to abort a good merge. Runs survive that rewrite. Runs also avoid the substring trap a
    raw-text scan would fall into - '20' must NOT count as present merely because the line
    contains '2026'."""
    return set(_DIGITS_RE.findall(_TAG_RE.sub("", line)))


def _content_tokens(line: str) -> set:
    """A rule's content words (bug_fixer._tokens: 4+ chars, stop words dropped) - the
    tokens whose disappearance means a FACT went missing rather than a phrase being
    reworded. Reuses bug-fixer's stop list so the codebase has one such vocabulary."""
    return bug_fixer._tokens(_TAG_RE.sub("", line))


def _absorbs(removed_line: str, surviving_line: str) -> bool:
    """True if `surviving_line` carries `removed_line`'s content.

    The single "was this rule folded in, or lost?" predicate. The fold invariant, the
    fold-result exemption and the multi-rule-merge check all go through it, so all three
    agree on what absorption means - relaxing one without the others would let a
    two-rule semantic merge slip past `is_multi_rule_merge` into auto-apply.

    An edit that is loss-free by the strict original test (every significant token
    survives) is still accepted outright. Otherwise a REWORDED fold is accepted only when
    both hold:
      * every number survives. A dropped or drifted figure (750mg -> 700mg) is never a
        rewording, and this is the half of the guard that stops figures moving; and
      * enough of the removed rule's content words appear in the survivor - at most
        FOLD_PROSE_LOSS_PCT per cent of them may go, with a floor of
        FOLD_PROSE_MIN_ALLOWANCE words' slack once a rule has FOLD_PROSE_FLOOR_MIN_TOKENS
        content words - so deleting a genuine fact fails, and a short rule must be folded
        essentially word-complete.
    A rule with no content words (a bare figure) has nothing to reword, so it falls back
    to the strict test."""
    r_sig = _sig_tokens(removed_line)
    if not r_sig:
        return True
    if r_sig <= _sig_tokens(surviving_line):
        return True
    if not _digit_runs(removed_line) <= _digit_runs(surviving_line):
        return False
    r_content = _content_tokens(removed_line)
    if not r_content:
        return False
    missing = len(r_content - _content_tokens(surviving_line))
    allowed = len(r_content) * FOLD_PROSE_LOSS_PCT // 100
    if len(r_content) >= FOLD_PROSE_FLOOR_MIN_TOKENS:
        allowed = max(FOLD_PROSE_MIN_ALLOWANCE, allowed)
    return missing <= allowed


def _line_conflicts(line: str, prefs: list) -> str:
    """Return the confirmed preference a [perm] line contradicts, else ''. Mirrors
    bug-fixer._rule_conflict's deterministic backstop: a line that ASSERTS terms a confirmed
    preference explicitly FORBIDS, on the same topic, is a conflict. Conservative — only ever
    used to reject an append or route a contradiction to review, never to auto-delete a rule.
    This is the check that would have blocked the bare 'reply only Logged.' rule that fought
    Jamie's locked-in 'do not stop asking' preference."""
    ptoks = bug_fixer._tokens(line)
    if not ptoks:
        return ""
    for pref in prefs:
        ctoks = bug_fixer._tokens(pref)
        if not ctoks:
            continue
        forbidden = bug_fixer._forbidden_terms(pref) & ptoks
        overlap   = len(ptoks & ctoks) / max(1, len(ptoks | ctoks))
        if forbidden and overlap >= 0.25:
            return pref
    return ""


def enforce_rule_guards(before_text: str, after_text: str, prefs: list):
    """TIER A — capture-time guard. Runs AFTER the model has edited persistent-rules.md.

    Two edit shapes are allowed:
      * APPEND a new standing rule for a genuinely new topic.
      * FOLD a refinement into the existing rule it extends, by editing that rule in place. This
        is the fix for the near-duplicate churn: append-only capture used to force every
        refinement (a new nutrition item, a long-run progression nuance) to land as a separate
        near-duplicate line, which the nightly bug-fixer then had to merge — one review card per
        refinement, night after night. Folding on write means there is nothing left to merge.

    The guard is deterministic and conservative:
      * a NEWLY-APPENDED [perm] line is reverted if it contradicts a confirmed preference, exactly
        duplicates an existing line, or pushes the pile over the ceiling (unchanged behaviour);
      * an in-place edit that removes/rewrites an existing rule is PERMITTED only when every
        removed rule is absorbed by some [perm] line still on file (`_absorbs`). Rewording is
        allowed — it has to be, or every fold aborts on a dropped stop word — but every number
        must survive and most of the removed rule's content words must carry over. A CONFIRMED
        preference is held to the stricter original bar (see `_survives`): it may be EXTENDED in
        place, keeping every significant token, but rewording, weakening or deleting one still
        fails. Any edit that drops a fact, silently changes a figure, or rewrites a confirmed
        preference fails the invariant and the ENTIRE write is refused (file left untouched),
        routing that judgement to the human-reviewed merge.

    Returns (new_text, drops); a drops entry whose reason starts 'ABORT' means nothing written
    (new_text == before_text)."""
    before_perm = [l for l in before_text.splitlines(keepends=True) if _PERM_RE.match(l)]
    after_lines = after_text.splitlines(keepends=True)
    perm_idx    = [i for i, l in enumerate(after_lines) if _PERM_RE.match(l)]

    before_norm = Counter(_norm(l) for l in before_perm)
    after_norm  = Counter(_norm(after_lines[i]) for i in perm_idx)
    appended    = after_norm - before_norm            # NEW [perm] lines (incl. any fold result)
    removed     = set(before_norm - after_norm)       # pre-existing [perm] lines now gone

    if not appended and not removed:
        return after_text, []

    confirmed   = {_norm(p) for p in prefs}
    raw_by_norm = {}
    for l in before_perm:
        raw_by_norm.setdefault(_norm(l), l)
    removed_raw = [raw_by_norm.get(n, "") for n in removed]

    def _survives(n, perm_lines) -> bool:
        """Did the pre-existing rule `n` survive this edit, given the [perm] lines still on
        file? Two standards, by design:

        * A CONFIRMED preference may only be EXTENDED in place - some survivor must keep
          every one of its significant tokens, numbers included, and may add to them.
          Deliberately the STRICT subset test, not the reworded-prose budget `_absorbs`
          allows ordinary rules: for a preference the athlete has explicitly locked in,
          "most of the wording survived" is not good enough, so any rewording, weakening
          or deletion still aborts to a human review card.
        * Any other rule may also be REWORDED, within `_absorbs`' budget."""
        rline = raw_by_norm.get(n, "")
        if n in confirmed:
            rtoks = _sig_tokens(rline)
            return not rtoks or any(rtoks <= _sig_tokens(l) for l in perm_lines)
        return any(_absorbs(rline, l) for l in perm_lines)

    def _fold_ok(perm_lines) -> str:
        """'' if every removed rule survives in perm_lines; else an ABORT reason.
        perm_lines is the list of [perm] lines to check."""
        for n in removed:
            if _survives(n, perm_lines):
                continue
            if n in confirmed:
                return ("ABORT: edit would reword, weaken or remove a confirmed preference "
                        "(it may only be extended in place); reverted the model's edit")
            return ("ABORT: edit removed rule content not folded into any surviving rule; "
                    "reverted the model's edit")
        return ""

    # Validate folds against the model's full output BEFORE gating appends.
    if removed:
        reason = _fold_ok([after_lines[i] for i in perm_idx])
        if reason:
            _after_perm = [after_lines[i] for i in perm_idx]
            bad = next((raw_by_norm.get(n, "") for n in removed
                        if not _survives(n, _after_perm)), "")
            return before_text, [(reason, bad.strip())]

    # Attribute appended copies to the TAIL-most physical lines (never an identical earlier one).
    budget       = dict(appended)
    appended_idx = []
    for i in reversed(perm_idx):
        n = _norm(after_lines[i])
        if budget.get(n, 0) > 0:
            appended_idx.append(i)
            budget[n] -= 1
    appended_idx.sort()

    # A fold-result line (the survivor a removed rule folded into) must never be dropped, or the
    # fold loses data; it is count-neutral so it cannot breach the ceiling either.
    def _is_fold_result(i):
        return any(_sig_tokens(rl) and _absorbs(rl, after_lines[i]) for rl in removed_raw)

    dropped = {}                                      # idx -> reason
    for i in appended_idx:
        if _is_fold_result(i):
            continue
        line = after_lines[i]
        pref = _line_conflicts(line, prefs)
        if pref:
            dropped[i] = f"conflicts with confirmed preference: {pref.strip()[:120]}"
            continue
        if before_norm.get(_norm(line), 0) > 0:
            dropped[i] = "exact duplicate of an existing [perm] rule"

    def _standing_count():
        kept = [l for j, l in enumerate(after_lines) if j not in dropped]
        return bug_fixer._count_rules("".join(kept))

    for i in sorted((j for j in appended_idx
                     if j not in dropped and not _is_fold_result(j)), reverse=True):
        if _standing_count() <= CEILING:
            break
        dropped[i] = f"append would exceed the standing-rule ceiling ({CEILING})"

    if not dropped:
        return after_text, []

    new_text = "".join(l for j, l in enumerate(after_lines) if j not in dropped)

    # Re-verify the fold invariant after dropping bad appends (dropping a pure-new line cannot
    # orphan a folded rule, since fold results are exempt above — but assert it, cheaply).
    if removed:
        reason = _fold_ok([l for l in new_text.splitlines() if _PERM_RE.match(l)])
        if reason:
            return before_text, [(reason.replace("edit", "guard drop"), "")]

    drops = [(reason, after_lines[i].strip()) for i, reason in sorted(dropped.items())]
    return new_text, drops


def confirmed_preferences(slug: str) -> list:
    """Thin pass-through to bug_fixer._confirmed_preferences, so callers don't need
    their own importlib load of bug-fixer.py just to get this one list."""
    return bug_fixer._confirmed_preferences(slug)


def is_multi_rule_merge(before_text: str, after_text: str) -> bool:
    """True if some line in `after_text` newly absorbs the content of TWO OR MORE
    distinct pre-existing [perm] lines from `before_text` — a semantic merge of
    independently-worded rules into one new sentence. This is the over-merge risk
    class the fold-on-write guard deliberately does NOT auto-approve:
    `enforce_rule_guards` only proves no rule's content is LOST, not that combining
    several differently-worded existing rules into new prose was itself a safe
    judgement call — that is exactly what session-sync's fold-on-write fix left to
    human review rather than trying to auto-approve.

    Distinguishing this from a safe FOLD (one existing rule extended with a new
    fact) or a safe DEDUP (several near-identical lines collapsed to the fullest
    existing wording, i.e. the surviving line already existed verbatim) needs one
    more check beyond the loss invariant: per NEW line (one that did not exist
    verbatim in `before_text`), count how many DISTINCT removed rules' significant
    tokens it absorbs. A fold or dedup absorbs at most one; a merge of
    independently-worded rules absorbs two or more."""
    before_perm = [l for l in before_text.splitlines(keepends=True) if _PERM_RE.match(l)]
    after_lines = after_text.splitlines(keepends=True)
    perm_idx    = [i for i, l in enumerate(after_lines) if _PERM_RE.match(l)]

    before_norm = Counter(_norm(l) for l in before_perm)
    after_norm  = Counter(_norm(after_lines[i]) for i in perm_idx)
    appended    = after_norm - before_norm            # NEW [perm] lines (not verbatim before)
    removed     = set(before_norm - after_norm)        # pre-existing [perm] lines now gone
    if not removed or not appended:
        return False

    raw_by_norm = {}
    for l in before_perm:
        raw_by_norm.setdefault(_norm(l), l)
    removed_raw = {n: raw_by_norm.get(n, "") for n in removed}

    # Attribute appended copies to the TAIL-most physical lines, mirroring
    # enforce_rule_guards' own attribution so the two funcs agree on which lines
    # are "new".
    budget       = dict(appended)
    appended_idx = []
    for i in reversed(perm_idx):
        n = _norm(after_lines[i])
        if budget.get(n, 0) > 0:
            appended_idx.append(i)
            budget[n] -= 1

    for i in appended_idx:
        absorbed = sum(1 for rl in removed_raw.values()
                       if _sig_tokens(rl) and _absorbs(rl, after_lines[i]))
        if absorbed >= 2:
            return True
    return False


def classify_merge_proposal(before_text: str, after_text: str, prefs: list):
    """Classify a prune/merge PROPOSAL (bug-fixer's nightly rule-consolidation drafts)
    for auto-apply eligibility. Runs the SAME fold-on-write guard used by
    session-sync.py and telegram/bot.py — this module is the single arbiter across
    all three writers — plus one extra check specific to a *proposed* consolidation:
    whether it merges two or more independently-worded PRE-EXISTING rules into one
    new sentence (see `is_multi_rule_merge`). That is a semantic judgement call the
    loss-free guard alone does not make, and it is exactly the risk class
    session-sync's fold-on-write fix deliberately left to human review.

    Returns (verdict, guarded_text, drops):
      'auto_apply' — loss-free AND at most one pre-existing rule folded per new
                     line; safe to write straight to the live file, no review card.
      'escalate'   — either the guard rejected the edit (drops non-empty;
                     guarded_text is `before_text`, unchanged) or it merges two or
                     more independently-worded pre-existing rules into one line
                     (guard accepted the edit as loss-free, but it still routes to
                     a human review card)."""
    guarded, drops = enforce_rule_guards(before_text, after_text, prefs)
    if drops:
        return "escalate", guarded, drops
    if is_multi_rule_merge(before_text, after_text):
        return "escalate", guarded, []
    return "auto_apply", guarded, []
