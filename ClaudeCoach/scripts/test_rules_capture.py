#!/usr/bin/env python3
"""
Unit tests for lib/rules_capture.enforce_rule_guards — the TIER A "fold-on-write"
capture guard shared by session-sync.py (hourly) and telegram/bot.py (live chat).

These reconstruct the invariant the commit that introduced fold-on-write (f841fb8)
asserted "11/11 guard unit tests pass" for, but never committed as a file: a loss-free
fold (every removed rule's content, numbers included, survives in a rule still on file)
is permitted; a lossy edit — a dropped fact, a silently changed figure, a removed
confirmed preference, or a deletion not folded anywhere — must ABORT and revert the
whole write to `before_text`. Also covers the pre-existing append-guard behaviour
(conflict / exact-duplicate / ceiling) unchanged by the fold work.

Run: python3 ClaudeCoach/scripts/test_rules_capture.py
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "lib"))
import rules_capture as rc


def _lines(*rules):
    return "".join(f"{r}\n" for r in rules)


class FoldInvariantTests(unittest.TestCase):
    """The core no-information-loss invariant for in-place edits (folds)."""

    def test_loss_free_fold_is_permitted(self):
        """Folding a refinement into an existing rule, keeping every original fact
        and adding a new one, must be accepted as-is."""
        before = _lines("[perm] Takes 750mg magnesium before bed")
        after = _lines("[perm] Takes 750mg magnesium before bed, plus 500mg zinc on rest days")
        prefs = []
        new_text, drops = rc.enforce_rule_guards(before, after, prefs)
        self.assertEqual(new_text, after)
        self.assertEqual(drops, [])

    def test_fact_drop_aborts_and_reverts(self):
        """Rewriting a rule so it drops an existing fact (not folded anywhere) must
        abort the whole write and return before_text untouched."""
        before = _lines("[perm] Long run progression: +10% weekly, cap at 22 miles")
        after = _lines("[perm] Long run progression: +10% weekly")   # cap fact dropped
        new_text, drops = rc.enforce_rule_guards(before, after, [])
        self.assertEqual(new_text, before)
        self.assertTrue(drops)
        self.assertTrue(drops[0][0].startswith("ABORT"))

    def test_number_change_aborts_and_reverts(self):
        """A silently changed figure (750mg -> 700mg) must fail the invariant even
        though the rest of the sentence is unchanged — digits are significant tokens."""
        before = _lines("[perm] Takes 750mg magnesium before bed")
        after = _lines("[perm] Takes 700mg magnesium before bed")
        new_text, drops = rc.enforce_rule_guards(before, after, [])
        self.assertEqual(new_text, before)
        self.assertTrue(drops)
        self.assertTrue(drops[0][0].startswith("ABORT"))

    def test_confirmed_preference_removal_aborts_and_reverts(self):
        """Removing/rewriting a line that is itself a confirmed (locked-in) preference
        must abort, even if the new wording looks like an innocuous refinement."""
        before = _lines("[perm] Never suggest suppressing appetite as a race strategy")
        after = _lines("[perm] Suggest appetite suppression only if the athlete asks")
        prefs = ["[perm] Never suggest suppressing appetite as a race strategy"]
        new_text, drops = rc.enforce_rule_guards(before, after, prefs)
        self.assertEqual(new_text, before)
        self.assertTrue(drops)
        self.assertTrue(drops[0][0].startswith("ABORT"))

    def test_fold_alongside_unrelated_rules_and_new_topic_append(self):
        """A realistic turn: one existing rule is folded loss-free, an unrelated
        existing rule is untouched, and a genuinely new topic is appended in the
        same edit. All three should be accepted together."""
        before = _lines(
            "[perm] Takes 750mg magnesium before bed",
            "[perm] Wears compression socks on long travel days",
        )
        after = _lines(
            "[perm] Takes 750mg magnesium before bed, plus 500mg zinc on rest days",
            "[perm] Wears compression socks on long travel days",
            "[perm] Prefers metric splits over pace-per-mile",
        )
        new_text, drops = rc.enforce_rule_guards(before, after, [])
        self.assertEqual(new_text, after)
        self.assertEqual(drops, [])

    def test_non_folded_deletion_aborts_and_reverts(self):
        """A rule simply deleted, with nothing surviving that carries its content,
        must abort — deletions are judgement calls for the reviewed prune, not capture."""
        before = _lines(
            "[perm] Takes 750mg magnesium before bed",
            "[perm] Wears compression socks on long travel days",
        )
        after = _lines("[perm] Wears compression socks on long travel days")
        new_text, drops = rc.enforce_rule_guards(before, after, [])
        self.assertEqual(new_text, before)
        self.assertTrue(drops)
        self.assertTrue(drops[0][0].startswith("ABORT"))


class AppendGuardTests(unittest.TestCase):
    """Pre-existing (unchanged) append-only guard behaviour: conflict / dup / ceiling."""

    def test_no_change_is_a_noop(self):
        text = _lines("[perm] Runs Tuesday/Thursday/Saturday")
        new_text, drops = rc.enforce_rule_guards(text, text, [])
        self.assertEqual(new_text, text)
        self.assertEqual(drops, [])

    def test_genuinely_new_topic_append_is_kept(self):
        before = _lines("[perm] Runs Tuesday/Thursday/Saturday")
        after = before + "[perm] Prefers metric splits over pace-per-mile\n"
        new_text, drops = rc.enforce_rule_guards(before, after, [])
        self.assertEqual(new_text, after)
        self.assertEqual(drops, [])

    def test_appended_line_conflicting_with_preference_is_reverted(self):
        before = _lines("[perm] Never suppress your appetite as a race strategy")
        after = before + "[perm] Suppress appetite when racing hard\n"
        prefs = ["[perm] Never suppress your appetite as a race strategy"]
        new_text, drops = rc.enforce_rule_guards(before, after, prefs)
        self.assertEqual(new_text, before)
        self.assertTrue(drops)
        self.assertIn("conflicts with confirmed preference", drops[0][0])

    def test_exact_duplicate_append_is_reverted(self):
        before = _lines("[perm] Runs Tuesday/Thursday/Saturday")
        after = before + "[perm] runs tuesday/thursday/saturday\n"   # case/space variant
        new_text, drops = rc.enforce_rule_guards(before, after, [])
        self.assertEqual(new_text, before)
        self.assertTrue(drops)
        self.assertIn("exact duplicate", drops[0][0])

    def test_append_over_ceiling_is_reverted(self):
        before = _lines(*[f"[perm] Standing rule number {i}" for i in range(rc.CEILING)])
        after = before + "[perm] One rule too many\n"
        new_text, drops = rc.enforce_rule_guards(before, after, [])
        self.assertEqual(new_text, before)
        self.assertTrue(drops)
        self.assertIn("ceiling", drops[0][0])


class MergeClassificationTests(unittest.TestCase):
    """classify_merge_proposal — the auto-apply/escalate decision bug-fixer.py's
    nightly prune/merge cards now use. Same shared guard as the fold tests above,
    plus the extra over-merge check that keeps a semantic merge of two
    independently-worded rules routed to a human, even when it is loss-free."""

    def test_trivial_dedup_is_auto_applied(self):
        """Two case/whitespace-variant copies of the SAME rule collapsing to the
        fuller existing wording is a pure duplicate removal — safe to auto-apply."""
        before = _lines(
            "[perm] Runs Tuesday/Thursday/Saturday",
            "[perm] runs tuesday/thursday/saturday",
        )
        after = _lines("[perm] Runs Tuesday/Thursday/Saturday")
        verdict, guarded, drops = rc.classify_merge_proposal(before, after, [])
        self.assertEqual(verdict, "auto_apply")
        self.assertEqual(drops, [])
        self.assertEqual(guarded, after)

    def test_loss_free_single_rule_fold_is_auto_applied(self):
        """A refinement folded into the one rule it extends (as in the fold tests
        above) is exactly the trivial case bug-fixer should no longer bother Jamie
        with — it should auto-apply, not just be guard-accepted."""
        before = _lines("[perm] Takes 750mg magnesium before bed")
        after = _lines("[perm] Takes 750mg magnesium before bed, plus 500mg zinc on rest days")
        verdict, guarded, drops = rc.classify_merge_proposal(before, after, [])
        self.assertEqual(verdict, "auto_apply")
        self.assertEqual(drops, [])

    def test_lossy_merge_escalates_via_guard_rejection(self):
        """A merge that drops a fact must escalate — the guard itself refuses it,
        same as the plain fold-invariant tests, and classify must surface that."""
        before = _lines(
            "[perm] Long run progression: +10% weekly, cap at 22 miles",
            "[perm] Wears compression socks on long travel days",
        )
        after = _lines(
            "[perm] Long run progression: +10% weekly",   # cap fact dropped
            "[perm] Wears compression socks on long travel days",
        )
        verdict, guarded, drops = rc.classify_merge_proposal(before, after, [])
        self.assertEqual(verdict, "escalate")
        self.assertEqual(guarded, before)   # reverted
        self.assertTrue(drops)
        self.assertTrue(drops[0][0].startswith("ABORT"))

    def test_merge_of_two_independently_worded_rules_escalates(self):
        """Combining TWO distinct, independently-worded pre-existing rules into one
        new sentence is loss-free (every fact from both survives) but is exactly the
        over-merge judgement call that must still go to a human review card."""
        before = _lines(
            "[perm] Eats porridge before every long run",
            "[perm] Drinks 500ml water before every long run",
        )
        after = _lines(
            "[perm] Eats porridge and drinks 500ml water before every long run",
        )
        # Sanity: the plain guard alone considers this loss-free (nothing dropped).
        _guarded, guard_drops = rc.enforce_rule_guards(before, after, [])
        self.assertEqual(guard_drops, [])
        self.assertTrue(rc.is_multi_rule_merge(before, after))

        verdict, guarded, drops = rc.classify_merge_proposal(before, after, [])
        self.assertEqual(verdict, "escalate")
        self.assertEqual(drops, [])   # guard itself didn't object — the merge check did


if __name__ == "__main__":
    unittest.main(verbosity=2)
