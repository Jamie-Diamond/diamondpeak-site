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
[perm] line is permitted ONLY when every removed rule's significant tokens (numbers
included, so a silently-changed figure like 750mg->700mg fails) survive inside some
[perm] line still on file, and no confirmed preference is touched. Any lossy edit fails
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
      * an in-place edit that removes/rewrites an existing rule is PERMITTED only when it is a
        loss-free FOLD — every removed rule's significant tokens (numbers included) survive inside
        some [perm] line still on file. Any edit that drops a rule's content, silently changes a
        figure, or removes a confirmed preference fails the invariant and the ENTIRE write is
        refused (file left untouched), routing that judgement to the human-reviewed merge.

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
    removed_sig = [_sig_tokens(raw_by_norm.get(n, "")) for n in removed]

    def _fold_ok(perm_lines) -> str:
        """'' if every removed rule survives (folded) in perm_lines and no confirmed preference
        was removed; else an ABORT reason. perm_lines is the list of [perm] lines to check."""
        surviving = [_sig_tokens(l) for l in perm_lines]
        for n in removed:
            if n in confirmed:
                return ("ABORT: edit would remove/alter a confirmed preference; "
                        "reverted the model's edit")
            rtoks = _sig_tokens(raw_by_norm.get(n, ""))
            if rtoks and not any(rtoks <= s for s in surviving):
                return ("ABORT: edit removed rule content not folded into any surviving rule; "
                        "reverted the model's edit")
        return ""

    # Validate folds against the model's full output BEFORE gating appends.
    if removed:
        reason = _fold_ok([after_lines[i] for i in perm_idx])
        if reason:
            bad = next((raw_by_norm.get(n, "") for n in removed
                        if n in confirmed or not any(
                            _sig_tokens(raw_by_norm.get(n, "")) <= _sig_tokens(after_lines[i])
                            for i in perm_idx)), "")
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
        s = _sig_tokens(after_lines[i])
        return any(rt and rt <= s for rt in removed_sig)

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
