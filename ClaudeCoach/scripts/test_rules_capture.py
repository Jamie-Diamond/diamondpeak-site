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


class RewordedFoldTests(unittest.TestCase):
    """The 27 Jul 2026 relaxation. The invariant used to require a removed rule's FULL
    significant-token set to be a subset of a survivor, so ANY rewording during a fold
    dropped a token and aborted the whole write — the intended in-place fold silently
    downgraded to a human review card, which is the churn fold-on-write existed to remove.
    Prose may now be reworded; every NUMBER must still survive and most content words must
    carry over.

    Fixtures here are synthetic on purpose. The real cases these were calibrated against
    are Jamie's live persistent-rules.md, and diamondpeak-site is a PUBLIC repo (athletes/
    is gitignored for exactly that reason), so the real text is replayed out-of-tree and
    only its token/digit profile is reproduced below."""

    def test_reworded_fold_keeping_every_number_is_permitted(self):
        """The case that used to abort: a fold that rewrites the sentence, keeps every
        figure, adds a new fact, and loses only a couple of content words."""
        before = _lines(
            "[perm] Takes 750mg magnesium and 500mg zinc before bed on hard training "
            "days, always with food, never on an empty stomach; started 12 Jun 2026 "
            "after cramping."
        )
        after = _lines(
            "[perm] Evening supplements: 750mg magnesium and 500mg zinc before bed on "
            "hard training days, taken with food and never on an empty stomach - plus "
            "200mg theanine on race eve. Started 12 Jun 2026 after cramping."
        )
        b = before.strip()
        a = after.strip()
        # It must be the RELAXED path doing the work, not the old strict subset.
        self.assertFalse(rc._sig_tokens(b) <= rc._sig_tokens(a))
        self.assertTrue(rc._absorbs(b, a))

        new_text, drops = rc.enforce_rule_guards(before, after, [])
        self.assertEqual(new_text, after)
        self.assertEqual(drops, [])
        # And it is a single-rule fold, so it auto-applies — no review card.
        verdict, _guarded, _d = rc.classify_merge_proposal(before, after, [])
        self.assertEqual(verdict, "auto_apply")

    def test_respacing_a_figure_is_not_a_figure_change(self):
        """"PF 30 CHEW" -> "PF30 CHEW" drops the bare token '30' while the figure is
        plainly still there. Digit RUNS survive that rewrite; whole tokens did not, and
        that alone was enough to abort an otherwise clean merge."""
        before = _lines("[perm] PF 30 CHEW is 30g of carbs and Jamie's regular product")
        after = _lines("[perm] PF30 CHEW is 30g of carbs, Jamie's regular product, never a gel")
        new_text, drops = rc.enforce_rule_guards(before, after, [])
        self.assertEqual(new_text, after)
        self.assertEqual(drops, [])

    def test_reword_that_drops_a_figure_still_aborts(self):
        """Rewording is not a licence to move numbers: the sentence may change, the
        figures may not. This is the half of the guard that is earning its keep."""
        before = _lines(
            "[perm] Takes 750mg magnesium and 500mg zinc before bed on hard training "
            "days, always with food, never on an empty stomach; started 12 Jun 2026."
        )
        after = _lines(
            "[perm] Evening supplements: 700mg magnesium and 500mg zinc before bed on "
            "hard training days, taken with food and never on an empty stomach. "
            "Started 12 Jun 2026."
        )
        new_text, drops = rc.enforce_rule_guards(before, after, [])
        self.assertEqual(new_text, before)
        self.assertTrue(drops[0][0].startswith("ABORT"))

    def test_numbers_kept_but_prose_gutted_still_aborts(self):
        """Preserving every figure is necessary, not sufficient. A rule stripped back to
        its numbers has lost its meaning, and the content-word floor must catch it —
        otherwise the relaxation would be a rubber stamp."""
        before = _lines(
            "[perm] Takes 750mg magnesium and 500mg zinc before bed on hard training "
            "days, always with food, never on an empty stomach; started 12 Jun 2026."
        )
        after = _lines("[perm] 750mg 500mg 12 2026")
        new_text, drops = rc.enforce_rule_guards(before, after, [])
        self.assertEqual(new_text, before)
        self.assertTrue(drops[0][0].startswith("ABORT"))

    def test_content_moved_off_the_perm_line_aborts(self):
        """A merge that replaces detailed rules with a bare heading and moves the
        substance onto non-[perm] continuation lines is a real loss of standing-rule
        surface, not a rewording. (This is the shape of the 2026-07-27-1 proposal, which
        the guard rejected and Jamie declined — the abort was correct.)"""
        before = _lines(
            "[perm] Report Form as final once the session has synced, with no provisional hedge",
            "[perm] Only flag Form below -25 for Jamie mid-build; -10 to -20 is background noise",
        )
        after = ("[perm] Fitness/Fatigue/Form reporting:\n"
                 "  - report as final once synced, no hedge\n"
                 "  - only flag below -25; -10 to -20 is noise\n")
        new_text, drops = rc.enforce_rule_guards(before, after, [])
        self.assertEqual(new_text, before)
        self.assertTrue(drops[0][0].startswith("ABORT"))

    def test_short_rule_losing_one_content_word_still_aborts(self):
        """The relaxation must not become a hole on SHORT rules. Dropping "Thursday"
        from a four-content-word rule is a lost training day, not a rewording, and the
        two-word slack that makes medium rules foldable is gated off below
        FOLD_PROSE_FLOOR_MIN_TOKENS so the ratio binds alone here."""
        before = _lines("[perm] Swim Tuesday and Thursday mornings")
        after = _lines("[perm] Swim Tuesday mornings")
        self.assertFalse(rc._absorbs(before.strip(), after.strip()))
        new_text, drops = rc.enforce_rule_guards(before, after, [])
        self.assertEqual(new_text, before)
        self.assertTrue(drops[0][0].startswith("ABORT"))

    def test_reworded_two_rule_merge_still_escalates(self):
        """The trap in relaxing the loss check: `is_multi_rule_merge` shares the same
        absorption predicate, so it must be relaxed in step. If only the fold invariant
        were relaxed, a two-rule semantic merge would stop aborting AND stop registering
        as a multi-rule merge, and would silently auto-apply — strictly worse than the
        churn being fixed."""
        before = _lines(
            "[perm] Eats 80g porridge before every long run, prepared the night before",
            "[perm] Drinks 500ml of water before every long run, sipped over 20 minutes",
        )
        after = _lines(
            "[perm] Long-run prep: 80g porridge prepared the night before, and 500ml "
            "water sipped across 20 minutes, before every long run",
        )
        _guarded, guard_drops = rc.enforce_rule_guards(before, after, [])
        self.assertEqual(guard_drops, [])          # relaxed guard accepts it as loss-free
        self.assertTrue(rc.is_multi_rule_merge(before, after))
        verdict, guarded, drops = rc.classify_merge_proposal(before, after, [])
        self.assertEqual(verdict, "escalate")      # ...but a human still sees it
        self.assertEqual(drops, [])


class ConfirmedMarkerTests(unittest.TestCase):
    """Which standing rules count as explicitly locked in.

    This was silently broken: _CONFIRMED_MARKERS looked for "reconfirm", which appears
    ZERO times across the real rules, so _confirmed_preferences returned an empty list and
    BOTH protections built on it - the capture guard's "never rewrite a confirmed
    preference" branch and the append-time contradiction check - were vacuous in
    production. The real form is a dated confirmation clause.

    Fixtures are synthetic (this repo is public and athletes/ is gitignored), but each
    mirrors a shape that occurs in the live files."""

    def _confirmed(self, line):
        return rc.bug_fixer._is_confirmed_rule(line)

    def test_dated_confirmation_clauses_are_locks(self):
        for line in [
            "[perm] Report Form as final once synced. Confirmed 21 Jul 2026.",
            "[perm] Descents are ridden however is safe. Confirmed by Jamie 12 Jul 2026.",
            "[perm] Ride to HR, not %FTP (confirmed 19 Jul 2026).",
            "[perm] Never blend the two figures (Kathryn confirmed 11 Jul 2026 after confusion).",
        ]:
            self.assertTrue(self._confirmed(line), line)

    def test_incidental_uses_of_confirm_are_not_locks(self):
        """The reason the marker is anchored to a date. Each of these occurs in the live
        rules and must NOT lock the rule."""
        for line in [
            # an instruction to seek confirmation, not a record of one
            "[perm] Confirm with Jamie before deleting anything, since deletes are irreversible.",
            "[perm] Confirm actual intake directly with Kathryn before including it.",
            # ordinary prose about data being confirmed
            "[perm] Once the day's only activity is confirmed synced, state the figures directly.",
            "[perm] Never guess kit the athlete has not confirmed - leave it blank or ask.",
            "[perm] Include any athlete-confirmed nutrition data (carbs g + g/hr).",
            # the dangerous one: a substring match would lock a rule that says the OPPOSITE
            "[perm] Run threshold pace is an UNCONFIRMED working estimate of 5:00/km, "
            "given by Kathryn 22 Jul 2026, NOT field-tested.",
        ]:
            self.assertFalse(self._confirmed(line), line)

    def test_literal_markers_still_match(self):
        """The pre-existing literals stay - dropping them would silently unprotect the one
        live rule that matches on 'do not suppress'."""
        self.assertTrue(self._confirmed("[perm] Do not suppress the appetite prompt"))
        self.assertTrue(self._confirmed("[perm] RECONFIRMED: keep asking about sleep"))

    def test_a_dated_confirmed_rule_is_now_protected_end_to_end(self):
        """The point of the fix: with the marker matching, rewriting a confirmed
        preference aborts the whole write instead of sailing through."""
        rule = "[perm] Only flag Form when it is outside the normal range. Confirmed 21 Jul 2026."
        before = _lines(rule)
        after = _lines("[perm] Flag Form whenever it is negative. Confirmed 21 Jul 2026.")
        new_text, drops = rc.enforce_rule_guards(before, after, [rule])
        self.assertEqual(new_text, before)
        self.assertTrue(drops[0][0].startswith("ABORT"))
        self.assertIn("confirmed preference", drops[0][0])


class ConfirmedExtensionTests(unittest.TestCase):
    """A confirmed preference may be EXTENDED in place, but not reworded away.

    The blanket "any alteration of a confirmed rule aborts" rule made the live ankle
    rule (rule 12) permanently review-card-only the moment it gained a "Confirmed 27 Jul
    2026" clause — its own successful fold locked it against every future refinement.
    Jamie's call: allow extension, keep everything else strict. Note the bar here is the
    STRICT token-subset test, deliberately NOT the reworded-prose budget `_absorbs`
    grants ordinary rules — see test_reworded_confirmed_rule_aborts_even_though_absorbs
    below, which is the test that stops this becoming a general relaxation.

    Fixtures are synthetic; the real rule-12 cases are replayed out-of-tree because
    athletes/ is gitignored and this repo is public."""

    RULE = ("[perm] Include at least one dedicated mobility and ankle strength session "
            "every week, and do not let it drop out even in travel weeks. "
            "Confirmed 27 Jul 2026.")

    def test_extending_a_confirmed_rule_in_place_is_permitted(self):
        """Every original token kept, a new fact appended — the rule-12 shape."""
        before = _lines(self.RULE)
        after = _lines(self.RULE.rstrip() + " Format is 3x15 min, placeable on any day.")
        new_text, drops = rc.enforce_rule_guards(before, after, [self.RULE])
        self.assertEqual(new_text, after)
        self.assertEqual(drops, [])

    def test_reworded_confirmed_rule_aborts_even_though_absorbs_allows_it(self):
        """The guard rail on the relaxation. This rewording is inside `_absorbs`' prose
        budget, so an ordinary rule could be edited this way — a confirmed one may not."""
        reworded = (self.RULE.replace("Include at least one dedicated", "Do at least one")
                             .replace("do not let it drop out", "keep it in"))
        self.assertNotEqual(reworded, self.RULE)
        # Prove the ordinary-rule test would have accepted it...
        self.assertTrue(rc._absorbs(self.RULE, reworded))
        # ...and that being confirmed is what blocks it.
        new_text, drops = rc.enforce_rule_guards(
            _lines(self.RULE), _lines(reworded), [self.RULE])
        self.assertEqual(new_text, _lines(self.RULE))
        self.assertTrue(drops[0][0].startswith("ABORT"))
        self.assertIn("confirmed preference", drops[0][0])

    def test_confirmed_rule_losing_a_number_aborts(self):
        rule = self.RULE.rstrip() + " Format is 3x15 min."
        after = rule.replace("3x15", "3x10")
        new_text, drops = rc.enforce_rule_guards(_lines(rule), _lines(after), [rule])
        self.assertEqual(new_text, _lines(rule))
        self.assertTrue(drops[0][0].startswith("ABORT"))

    def test_deleting_a_confirmed_rule_aborts(self):
        before = _lines(self.RULE, "[perm] Wears compression socks on long travel days")
        after = _lines("[perm] Wears compression socks on long travel days")
        new_text, drops = rc.enforce_rule_guards(before, after, [self.RULE])
        self.assertEqual(new_text, before)
        self.assertTrue(drops[0][0].startswith("ABORT"))
        self.assertIn("confirmed preference", drops[0][0])

    def test_an_ordinary_rule_alongside_a_confirmed_one_may_still_be_reworded(self):
        """The two standards coexist: the confirmed rule is extended, the ordinary one
        beside it is reworded, and both land in the same write."""
        other = ("[perm] Takes 750mg magnesium and 500mg zinc before bed on hard training "
                 "days, always with food, never on an empty stomach.")
        before = _lines(self.RULE, other)
        after = _lines(
            self.RULE.rstrip() + " Format is 3x15 min.",
            "[perm] Evening supplements: 750mg magnesium and 500mg zinc before bed on hard "
            "training days, taken with food and never on an empty stomach.",
        )
        new_text, drops = rc.enforce_rule_guards(before, after, [self.RULE])
        self.assertEqual(new_text, after)
        self.assertEqual(drops, [])


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
