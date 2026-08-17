"""A violation the validator marks [soft] must not come back as hard_fail.

plan_audit computes `hard = any(fails[STRUCTURE, LONG_RIDE, RULES])`, and soft
intensity-distribution violations were being appended to RULES so they stayed visible.
Net effect: a distribution WARNING blocked, against the standing rule that warnings are
not hard rules (only safety ceilings block). It had never fired because the check kept
landing in SKIPPED - it only asserted once Kathryn's step bands were canonicalised on
4 Aug 2026, and then reported hard_fail on a 3pp distribution drift.
"""
import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest import mock

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


class SoftFindingsNeverAlert(unittest.TestCase):
    """The alert path must not widen what counts as hard.

    Calum is the live soft-only athlete, but he is hard_fail=False today so a live run
    proves nothing about the discrimination itself. These assert it directly.
    """

    SOFT_ONLY = {"fails": {
        "DISTRIBUTION": ["week X: [soft] intensity_distribution_vo2_high: 13% vs 10%"],
        "FUELLING": ["Long ride — no fuelling stated (expect 90 g/hr)"],
        "WEEKLY_LOAD": ["week X: 400 TSS vs target ~600 (>15% off)"],
        "SKIPPED": ["week X: ctl_ramp check SKIPPED"],
        "DIRECTED": ["week X: [hard] swim_directed_day: ..."],
    }, "athlete": "kathryn"}

    def test_no_hard_lines_from_a_soft_only_report(self):
        self.assertEqual(pa.hard_lines(self.SOFT_ONLY), [])

    def test_a_soft_only_report_never_reaches_the_alert_path(self):
        self.assertFalse(pa.is_hard(self.SOFT_ONLY["fails"]))

    def test_the_message_names_the_rules_and_the_weeks(self):
        rep = {"athlete": "kathryn", "hard_ids": ["RULES:2026-08-17:no_rest_day"],
               "fails": {"RULES": ["week 2026-08-17: [hard] no_rest_day: load on 7 of 7 days"],
                         "FUELLING": ["Long ride — no fuelling stated"]}}
        text = pa.alert_text(rep)
        self.assertIn("Kathryn", text)
        self.assertIn("2026-08-17", text)
        self.assertIn("no_rest_day", text)
        self.assertNotIn("fuelling", text)      # a warning must not travel with it
        self.assertNotIn("—", text)


class AlertDedup(unittest.TestCase):
    """Same finding tomorrow = silence; a new or changed one = an alert."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()) / "alerted.json"
        self._real = pa.ALERTED
        pa.ALERTED = self.tmp
        self.sent = []

    def tearDown(self):
        pa.ALERTED = self._real

    def _stub(self, action="dry-run"):
        def send(reason, text, key="", cooldown_h=None):
            self.sent.append((reason, key, text))
            return action
        return send

    @staticmethod
    def _report(ids, athlete="kathryn"):
        return {"athlete": athlete, "hard_ids": list(ids),
                "fails": {"RULES": [f"week 2026-08-17: [hard] {i.split(':')[-1]}: detail"
                                    for i in ids]}}

    def test_an_unchanged_finding_does_not_re_alert(self):
        rep = self._report(["RULES:2026-08-17:no_rest_day"])
        with mock.patch.object(pa.coach_alert, "send", self._stub()):
            self.assertEqual(pa.notify_hard_fail(rep), "dry-run")
            self.assertEqual(pa.notify_hard_fail(rep), "known")
        self.assertEqual(len(self.sent), 1)

    def test_a_new_finding_alerts(self):
        with mock.patch.object(pa.coach_alert, "send", self._stub()):
            pa.notify_hard_fail(self._report(["RULES:2026-08-17:no_rest_day"]))
            pa.notify_hard_fail(self._report(["RULES:2026-08-17:no_rest_day",
                                              "RULES:2026-08-24:weekly_tss_floor"]))
        self.assertEqual(len(self.sent), 2)

    def test_a_partial_fix_does_not_alert(self):
        both = ["RULES:2026-08-17:no_rest_day", "RULES:2026-08-24:weekly_tss_floor"]
        with mock.patch.object(pa.coach_alert, "send", self._stub()):
            pa.notify_hard_fail(self._report(both))
            pa.notify_hard_fail(self._report(both[:1]))
        self.assertEqual(len(self.sent), 1)

    def test_a_fixed_then_re_broken_finding_alerts_again(self):
        gone = ["RULES:2026-08-17:no_rest_day"]
        with mock.patch.object(pa.coach_alert, "send", self._stub()):
            pa.notify_hard_fail(self._report(gone))
            pa.notify_hard_fail(self._report(["RULES:2026-08-24:weekly_tss_floor"]))
            pa.notify_hard_fail(self._report(gone))
        self.assertEqual(len(self.sent), 3)

    def test_a_failed_send_is_retried_not_banked(self):
        rep = self._report(["RULES:2026-08-17:no_rest_day"])
        with mock.patch.object(pa.coach_alert, "send", self._stub("send-failed")):
            pa.notify_hard_fail(rep)
            pa.notify_hard_fail(rep)
        self.assertEqual(len(self.sent), 2)

    def test_the_dedup_key_is_the_finding_identity_not_the_time(self):
        with mock.patch.object(pa.coach_alert, "send", self._stub()):
            pa.notify_hard_fail(self._report(["RULES:2026-08-17:no_rest_day"]))
        reason, key, _ = self.sent[0]
        self.assertEqual(reason, pa.coach_alert.PLAN_HARD_FAIL)
        self.assertTrue(key.startswith("kathryn|"))
        self.assertEqual(key, f"kathryn|{pa._fingerprint(['RULES:2026-08-17:no_rest_day'])}")

    def test_an_audit_that_could_not_run_does_not_alert(self):
        """main() sets hard_fail on its own exception so rc stays honest. That is not
        a breached rule, and an intervals.icu blip must not message the coach."""
        rep = {"athlete": "kathryn", "signature": "error:ConnectionError", "hard_fail": True,
               "error": "ConnectionError: intervals.icu unreachable"}
        with mock.patch.object(pa.coach_alert, "send", self._stub()):
            self.assertEqual(pa.notify_hard_fail(rep), "error")
        self.assertEqual(self.sent, [])

    def test_a_hard_fail_with_no_identity_still_alerts(self):
        rep = {"athlete": "kathryn", "signature": "LONG_RIDE=1",
               "fails": {"LONG_RIDE": ["330min ride exceeds the 300min ceiling"]}}
        with mock.patch.object(pa.coach_alert, "send", self._stub()):
            self.assertEqual(pa.notify_hard_fail(rep), "dry-run")
        self.assertIn("long_ride:", self.sent[0][2])

    def test_a_structure_finding_reaches_the_message(self):
        rep = {"athlete": "jamie",
               "hard_ids": ["STRUCTURE:2026-08-19:no_structured_steps:Swim - CSS 5x400"],
               "fails": {"STRUCTURE": ["Swim - CSS 5x400 (Swim) — no structured steps"]}}
        with mock.patch.object(pa.coach_alert, "send", self._stub()):
            pa.notify_hard_fail(rep)
        self.assertIn("structure: Swim - CSS 5x400", self.sent[0][2])


if __name__ == "__main__":
    unittest.main()
