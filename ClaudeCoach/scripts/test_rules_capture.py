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


if __name__ == "__main__":
    unittest.main(verbosity=2)
