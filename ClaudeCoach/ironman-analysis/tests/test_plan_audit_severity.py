"""A violation the validator marks [soft] must not come back as hard_fail.

plan_audit computes `hard = any(fails[STRUCTURE, LONG_RIDE, RULES])`, and soft
intensity-distribution violations were being appended to RULES so they stayed visible.
Net effect: a distribution WARNING blocked, against the standing rule that warnings are
not hard rules (only safety ceilings block). It had never fired because the check kept
landing in SKIPPED - it only asserted once Kathryn's step bands were canonicalised on
4 Aug 2026, and then reported hard_fail on a 3pp distribution drift.
"""
import importlib.util
import unittest
from pathlib import Path

CC = Path(__file__).resolve().parents[2]


def _load():
    spec = importlib.util.spec_from_file_location("plan_audit", CC / "lib" / "plan_audit.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


pa = _load()


class Grading(unittest.TestCase):
    def test_soft_distribution_alone_is_not_a_hard_fail(self):
        self.assertFalse(pa.is_hard(
            {"DISTRIBUTION": ["week X: [soft] intensity_distribution_vo2_high: 13% vs 10%"]}))

    def test_a_hard_rule_still_blocks(self):
        self.assertTrue(pa.is_hard({"RULES": ["week X: [hard] no_rest_day: ..."]}))

    def test_structure_still_blocks(self):
        self.assertTrue(pa.is_hard({"STRUCTURE": ["VO2 6x3min — name claims vo2 ..."]}))

    def test_the_other_warn_categories_do_not_block(self):
        self.assertFalse(pa.is_hard({"FUELLING": ["x"], "WEEKLY_LOAD": ["y"],
                                     "SKIPPED": ["z"], "DIRECTED": ["w"]}))

    def test_empty_is_not_hard(self):
        self.assertFalse(pa.is_hard({k: [] for k in
                                     ("STRUCTURE", "LONG_RIDE", "RULES", "DISTRIBUTION")}))


class BaselineCompleteness(unittest.TestCase):
    """Every category plan_audit can populate needs a baseline entry, even at 0:
    within_baseline uses accepted.get(cat, -1), so a missing key rejects every run and
    alerts daily (the 28 Jul SKIPPED bug)."""

    def test_every_athlete_has_an_entry_for_every_category(self):
        import json
        accepted = json.loads((CC / "config" / "plan-audit-baseline.json").read_text())["athletes"]
        cats = {"STRUCTURE", "FUELLING", "LONG_RIDE", "WEEKLY_LOAD", "RULES",
                "DISTRIBUTION", "SKIPPED", "DIRECTED"}
        for slug, m in accepted.items():
            missing = cats - set(m) - {"STRUCTURE", "LONG_RIDE"}   # zero-default optional
            self.assertEqual(missing, set(), f"{slug} missing baseline categories: {missing}")

    def test_within_baseline_accepts_a_run_at_its_accepted_count(self):
        rep = {"fails": {"DISTRIBUTION": ["one"]}}
        self.assertTrue(pa.within_baseline(rep, {"DISTRIBUTION": 1}))

    def test_within_baseline_rejects_an_unlisted_category(self):
        rep = {"fails": {"DISTRIBUTION": ["one"]}}
        self.assertFalse(pa.within_baseline(rep, {"RULES": 4}))


if __name__ == "__main__":
    unittest.main()
