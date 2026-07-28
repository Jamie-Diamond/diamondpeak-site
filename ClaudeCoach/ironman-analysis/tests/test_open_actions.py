#!/usr/bin/env python3
"""Tests for lib/open_actions.py. Fixtures only — no athlete data, no network.

Collected by the ironman-analysis pytest suite (testpaths = ["tests"]); import
path follows the same sibling-lib pattern as test_ops_digest.py, since
open_actions.py lives in ClaudeCoach/lib, not under ironman-analysis/.
"""
import json
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]            # ClaudeCoach/
sys.path.insert(0, str(REPO / "lib"))
import open_actions as oa  # noqa: E402

TODAY = date(2026, 7, 28)


def _dir(actions, **rest):
    """An athlete dir laid out as BASE/athletes/<slug>, so the slug-based API works too."""
    d = Path(tempfile.mkdtemp()) / "athletes" / "fixture"
    d.mkdir(parents=True)
    (d / "current-state.json").write_text(json.dumps({**rest, "open_actions": actions}))
    return d


class Classify(unittest.TestCase):
    def test_overdue_counts_days(self):
        i = oa.evaluate_from_dir(_dir([{"action": "a", "due": "2026-05-31",
                                       "status": "pending"}]), TODAY)[0]
        self.assertEqual((i["bucket"], i["days_overdue"], i["escalation"]),
                         ("overdue", 58, "abandoned"))

    def test_due_soon_and_upcoming(self):
        items = oa.evaluate_from_dir(_dir([
            {"action": "soon", "due": "2026-07-31", "status": "pending"},
            {"action": "later", "due": "2026-09-01", "status": "pending"}]), TODAY)
        self.assertEqual([i["bucket"] for i in items], ["due_soon", "upcoming"])
        self.assertEqual([i["days_until"] for i in items], [3, 35])

    def test_due_today_is_due_soon_not_overdue(self):
        i = oa.evaluate_from_dir(_dir([{"action": "a", "due": "2026-07-28",
                                       "status": "pending"}]), TODAY)[0]
        self.assertEqual((i["bucket"], i["days_until"], i["escalation"]),
                         ("due_soon", 0, "none"))

    def test_null_due_is_open_ended_never_overdue(self):
        i = oa.evaluate_from_dir(_dir([{"action": "a", "due": None,
                                       "status": "pending"}]), TODAY)[0]
        self.assertEqual(i["bucket"], "open_ended")
        self.assertEqual(i["days_overdue"], 0)

    def test_closed_statuses_are_not_surfaced(self):
        items = oa.evaluate_from_dir(_dir([
            {"action": "d", "due": "2026-01-01", "status": "done"},
            {"action": "x", "due": "2026-01-01", "status": "dropped"},
            {"action": "n", "due": None, "status": "noted"}]), TODAY)
        self.assertEqual([i["open"] for i in items], [False, False, False])
        self.assertEqual(oa.render_lines(items), [])


class LegacyData(unittest.TestCase):
    def test_prose_status_splits_and_keeps_provenance(self):
        i = oa.evaluate_from_dir(_dir([{
            "action": "FTP retest", "due": "2026-05-31",
            "status": "done — replaced by eFTP 297W; per Jamie 2026-06-05"}]), TODAY)[0]
        self.assertEqual(i["status"], "done")
        self.assertEqual(i["status_note"], "replaced by eFTP 297W; per Jamie 2026-06-05")
        self.assertFalse(i["open"])

    def test_free_text_due_does_not_become_a_date(self):
        i = oa.evaluate_from_dir(_dir([{"action": "a", "due": "end May",
                                       "status": "pending"}]), TODAY)[0]
        self.assertIsNone(i["due"])
        self.assertEqual(i["bucket"], "open_ended")

    def test_unknown_status_stays_open(self):
        i = oa.evaluate_from_dir(_dir([{"action": "a", "due": "2026-01-01",
                                       "status": "wat"}]), TODAY)[0]
        self.assertEqual(i["status"], "pending")
        self.assertTrue(i["open"])

    def test_missing_key_and_junk_do_not_throw(self):
        d = Path(tempfile.mkdtemp())
        (d / "current-state.json").write_text('{"last_updated": "2026-07-28"}')
        self.assertEqual(oa.evaluate_from_dir(d, TODAY), [])
        (d / "current-state.json").write_text('{"open_actions": "nope"}')
        self.assertEqual(oa.evaluate_from_dir(d, TODAY), [])
        (d / "current-state.json").write_text("not json at all")
        self.assertEqual(oa.evaluate_from_dir(d, TODAY), [])
        self.assertEqual(oa.evaluate_from_dir(Path("/nonexistent"), TODAY), [])


class Escalation(unittest.TestCase):
    def test_bands(self):
        self.assertEqual(oa._escalation(0, False), "none")
        self.assertEqual(oa._escalation(1, False), "late")
        self.assertEqual(oa._escalation(13, False), "late")
        self.assertEqual(oa._escalation(14, False), "slipping")
        self.assertEqual(oa._escalation(41, False), "slipping")
        self.assertEqual(oa._escalation(42, False), "abandoned")

    def test_gating_bumps_one_band(self):
        self.assertEqual(oa._escalation(4, True), "slipping")
        self.assertEqual(oa._escalation(20, True), "abandoned")
        self.assertEqual(oa._escalation(0, True), "none")   # not yet late: no bump

    def test_in_flight_replaces_the_band(self):
        i = oa.evaluate_from_dir(_dir([{"action": "a", "due": "2026-05-31",
                                       "status": "booked"}]), TODAY)[0]
        self.assertEqual((i["days_overdue"], i["escalation"]), (58, "in_flight"))
        self.assertIn("IN FLIGHT", oa.render_line(i))
        self.assertNotIn("ABANDONED", oa.render_line(i))

    def test_open_ended_ages_only_with_raised(self):
        items = oa.evaluate_from_dir(_dir([
            {"action": "aged", "due": None, "raised": "2026-05-25", "status": "pending"},
            {"action": "undated", "due": None, "status": "pending"}]), TODAY)
        self.assertEqual((items[0]["days_open"], items[0]["escalation"]), (64, "abandoned"))
        self.assertEqual((items[1]["days_open"], items[1]["escalation"]), (None, "none"))
        self.assertIn("cannot age", oa.render_line(items[1]))


class Render(unittest.TestCase):
    ACTIONS = [{"action": "sweat test", "due": "2026-05-31", "status": "pending",
                "gates_race_decision": True},
               {"action": "helmet", "due": "2026-06-30", "status": "pending"},
               {"action": "ows", "due": "2026-07-31", "status": "pending"},
               {"action": "ice", "due": "2026-09-01", "status": "pending"},
               {"action": "icu event", "due": None, "status": "pending"},
               {"action": "old ftp", "due": "2026-05-31", "status": "done"}]

    def test_worst_first_ordering(self):
        lines = oa.render_lines(oa.evaluate_from_dir(_dir(self.ACTIONS), TODAY))
        self.assertEqual(len(lines), 5)                       # the done item is gone
        self.assertTrue(lines[0].startswith(oa.MARKERS["overdue"]))
        self.assertIn("sweat test", lines[0])                 # oldest overdue first
        self.assertTrue(lines[-1].startswith(oa.MARKERS["open_ended"]))

    def test_render_is_idempotent(self):
        d = _dir(self.ACTIONS)
        a = [oa.render_line(i) for i in oa.evaluate_from_dir(d, TODAY)]
        b = [oa.render_line(i) for i in oa.evaluate_from_dir(d, TODAY)]
        self.assertEqual(a, b)

    def test_no_model_arithmetic_left_to_do(self):
        line = oa.render_lines(oa.evaluate_from_dir(_dir(self.ACTIONS), TODAY))[0]
        self.assertIn("58 days OVERDUE", line)
        self.assertIn("due 2026-05-31", line)
        self.assertIn("gates a race decision", line)


class Blocks(unittest.TestCase):
    def setUp(self):
        self.d = _dir([{"action": "sweat test", "due": "2026-05-31", "status": "pending"},
                       {"action": "ows", "due": "2026-07-31", "status": "pending"},
                       {"action": "ice", "due": "2026-09-01", "status": "pending"}])
        self._saved, oa.BASE = oa.BASE, self.d.parent.parent
        self.slug = self.d.name

    def tearDown(self):
        oa.BASE = self._saved

    def test_weekly_and_watchdog_share_every_line(self):
        weekly = oa.weekly_block(self.slug, TODAY)
        t9 = oa.watchdog_block(self.slug, TODAY)
        fired = [l.strip() for l in t9.splitlines()
                 if l.strip().startswith(tuple(oa.MARKERS.values()))]
        self.assertTrue(fired)
        for line in fired:
            self.assertIn(line, weekly)

    def test_t9_window_excludes_a_distant_item(self):
        t9 = oa.watchdog_block(self.slug, TODAY)
        self.assertIn("sweat test", t9)     # overdue
        self.assertIn("ows", t9)            # 3 days out, inside the 7-day window
        self.assertNotIn("ice", t9)         # 35 days out
        self.assertIn("ice", oa.weekly_block(self.slug, TODAY))   # weekly lists everything

    def test_weekly_block_is_explicit_when_empty(self):
        empty = _dir([])
        oa.BASE = empty.parent.parent
        block = oa.weekly_block(empty.name, TODAY)
        self.assertIn("NO open actions outstanding", block)
        self.assertIn("Omit the Open actions section", block)


class SetStatus(unittest.TestCase):
    def setUp(self):
        self.d = _dir([{"action": "Book sweat test", "due": "2026-05-31", "status": "pending"},
                       {"action": "FTP retest (May)", "due": "2026-05-31", "status": "pending"},
                       {"action": "FTP retest (Jul)", "due": "2026-07-15", "status": "pending"}],
                      last_updated="2026-07-28")

    def test_close_sets_status_note_and_closed_date(self):
        e = oa.set_status("", "sweat", "done", "tested, 1100 mg/L",
                          today=TODAY, athlete_dir=self.d)
        self.assertEqual((e["status"], e["closed"]), ("done", "2026-07-28"))
        self.assertEqual(e["status_note"], "tested, 1100 mg/L")
        self.assertEqual(oa.counts(oa.evaluate_from_dir(self.d, TODAY))["open"], 2)

    def test_reopening_clears_the_closed_date(self):
        oa.set_status("", "sweat", "done", today=TODAY, athlete_dir=self.d)
        e = oa.set_status("", "sweat", "pending", today=TODAY, athlete_dir=self.d)
        self.assertNotIn("closed", e)
        self.assertTrue(oa.evaluate_from_dir(self.d, TODAY)[0]["open"])

    def test_ambiguous_match_refuses(self):
        with self.assertRaises(ValueError):
            oa.set_status("", "FTP retest", "done", today=TODAY, athlete_dir=self.d)

    def test_no_match_refuses(self):
        with self.assertRaises(ValueError):
            oa.set_status("", "nothing like this", "done", today=TODAY, athlete_dir=self.d)

    def test_bad_status_refuses(self):
        with self.assertRaises(ValueError):
            oa.set_status("", "sweat", "finished-ish", today=TODAY, athlete_dir=self.d)

    def test_write_preserves_the_rest_of_the_state_file(self):
        oa.set_status("", "sweat", "done", today=TODAY, athlete_dir=self.d)
        state = json.loads((self.d / "current-state.json").read_text())
        self.assertEqual(state["last_updated"], "2026-07-28")
        self.assertEqual(len(state["open_actions"]), 3)

    def test_due_and_raised_can_be_set(self):
        e = oa.set_status("", "sweat", "booked", due="2026-08-04",
                          raised="2026-05-01", today=TODAY, athlete_dir=self.d)
        self.assertEqual((e["due"], e["raised"]), ("2026-08-04", "2026-05-01"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
