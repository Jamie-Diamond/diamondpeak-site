"""An UNBUILT future week must not hard-fail the TSS floor (17 Aug 2026).

plan_audit walks a 14-day window (current + next week) every morning, while
scripts/weekly-plan.sh builds only the coming week, on cron at Sunday 18:00. The next
week is therefore legitimately EMPTY from Monday until that run, and the armed floor
scored it "UNDER-TRAINING: planned 0 TSS ... this week DETRAINS the athlete" every
single day - the week of 2026-08-17 collected 16 such flags and is now fully planned.
Since eb5b2dbe a hard fail messages Jamie, so that artefact pages him once per athlete
per future week on the channel that carries the real hard fails.

These pin the whole rule, not just the suppression: a FUTURE + ZERO-session week gets a
note and no floor violation; the CURRENT week is still checked exactly as before; and a
future week that HAS sessions under the floor still hard-fails. The soft WEEKLY_LOAD
signal on the empty week is deliberately left alone (it does not set hard_fail) and is
asserted here so a later "tidy-up" cannot quietly take it with it.
"""
import contextlib
import importlib.util
import json
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

CC = Path(__file__).resolve().parents[2]


def _load():
    spec = importlib.util.spec_from_file_location("plan_audit", CC / "lib" / "plan_audit.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


pa = _load()

TODAY = date.today()
THIS_WEEK = TODAY - timedelta(days=TODAY.weekday())      # the window's win_start
NEXT_WEEK = THIS_WEEK + timedelta(days=7)

CTL = 90.0
FLOOR = 600           # ~7 x CTL, i.e. maintenance: the same shape required_tss returns
CFG = {"icu_athlete_id": "i1", "icu_api_key": "k", "nutrition_target_g_hr": 90,
       "race_target_splits": {"bike_min": 260}, "day_rules": {},
       "max_ctl_ramp_per_week": 5.0, "run_protocol": {}}


def _sessions(week_start, loads):
    """One Ride per entry, Mon/Tue/Wed..., structured and fuelled so only the weekly
    checks under test can speak."""
    return [{"category": "WORKOUT", "type": "Ride",
             "start_date_local": f"{(week_start + timedelta(days=i)).isoformat()}T07:00:00",
             "name": f"Endurance {i}", "description": "Steady. Fuel 90 g/hr.",
             "moving_time": 60 * 60, "icu_training_load": load,
             "workout_doc": {"steps": [{"power": {"value": 0.65}}]}}
            for i, load in enumerate(loads)]


class _Client:
    def __init__(self, events):
        self._events = events

    def get_events(self, start, end):
        return list(self._events)

    def get_wellness(self, days=3):
        return [{"ctl": CTL}]


def _audit(events):
    """audit_athlete with every remote/disk seam stubbed. The floor, the cap and the
    week loop itself are the real ones - the point is what the audit DOES with them."""
    with contextlib.ExitStack() as st:
        p = st.enter_context
        p(mock.patch.object(pa, "_client", lambda cfg: _Client(events)))
        p(mock.patch.object(pa, "fuel_target", lambda *a, **k: 90))
        p(mock.patch.object(pa, "current_phase", lambda bp, ws: {}))
        p(mock.patch.object(pa.pt, "_load_blueprint", lambda slug: {}))
        p(mock.patch.object(pa.pt, "last_week_actual_tss", lambda client: None))
        p(mock.patch.object(pa.pt, "required_tss", lambda *a, **k: {
            "recommended_weekly_tss": FLOOR, "weekly_tss_floor": FLOOR}))
        p(mock.patch.object(pa.pt, "run_caps", lambda *a, **k: {"weekly_min_cap": None}))
        p(mock.patch.object(pa, "_weekly_tss_cap", lambda slug, phase, week_start=None: 900.0))
        p(mock.patch.object(pa.day_overrides, "load", lambda slug, base: {}))
        p(mock.patch.object(pa.weekly_availability, "effective_day_rules",
                            lambda *a, **k: ({}, None)))
        # escalate_repeats persists the CURRENT week's soft streaks; keep that off the
        # committed config/plan-audit-streaks.json.
        p(mock.patch.object(pa, "STREAKS", Path(tempfile.mkdtemp()) / "streaks.json"))
        return pa.audit_athlete("tester", CFG, weeks=2)


def _floor_fails(report, week):
    return [f for f in (report.get("fails") or {}).get("RULES", [])
            if "weekly_tss_floor" in f and str(week) in f]


class UnbuiltFutureWeek(unittest.TestCase):
    """Next week empty because the Sunday generator has not run yet."""

    def setUp(self):
        # Mon/Tue/Wed at maintenance load: over the floor, inside the cap, four rest
        # days, and flat CTL so nothing else in the current week can fire.
        self.rep = _audit(_sessions(THIS_WEEK, [200, 200, 230]))

    def test_no_floor_hard_fail_on_the_unbuilt_week(self):
        self.assertEqual(_floor_fails(self.rep, NEXT_WEEK), [])

    def test_the_note_says_the_week_is_not_generated_yet(self):
        self.assertTrue(any("NOT GENERATED YET" in n and str(NEXT_WEEK) in n
                            for n in self.rep["notes"]), self.rep["notes"])

    def test_the_advisory_is_not_in_fails(self):
        # counts()/signature() fingerprint `fails`; an advisory in there would raise the
        # baseline and spend an alert.
        self.assertNotIn("NOT GENERATED YET", json.dumps(self.rep["fails"]))

    def test_it_does_not_page(self):
        self.assertFalse(self.rep["hard_fail"])
        self.assertEqual(pa.hard_lines(self.rep), [])
        self.assertEqual([i for i in self.rep["hard_ids"] if "weekly_tss_floor" in i], [])

    def test_the_soft_weekly_load_signal_is_untouched(self):
        # Out of scope of the fix and never a hard fail: the empty week is still
        # reported as off-target, it just does not block or alert.
        self.assertTrue(any(str(NEXT_WEEK) in f
                            for f in self.rep["fails"].get("WEEKLY_LOAD", [])),
                        self.rep["fails"])

    def test_the_suppression_does_not_land_in_skipped_either(self):
        # Passing None instead of 0 would have put "weekly_tss_floor check SKIPPED" into
        # SKIPPED, which IS fingerprinted - the same alerting problem, one category over.
        self.assertEqual([s for s in self.rep["fails"].get("SKIPPED", [])
                          if "weekly_tss_floor" in s], [])


class EmptyCurrentWeekStillFails(unittest.TestCase):
    """Monday 06:25 with nothing on this week is a real failure, twelve hours after the
    generator should have run. It must still block and still page."""

    def setUp(self):
        self.rep = _audit([])

    def test_the_current_week_still_hard_fails_the_floor(self):
        self.assertTrue(_floor_fails(self.rep, THIS_WEEK), self.rep["fails"])
        self.assertTrue(self.rep["hard_fail"])
        self.assertIn(f"RULES:{THIS_WEEK}:weekly_tss_floor", self.rep["hard_ids"])

    def test_no_not_generated_note_for_the_current_week(self):
        self.assertEqual([n for n in self.rep["notes"]
                          if "NOT GENERATED YET" in n and str(THIS_WEEK) in n], [])


class UnderTrainedFutureWeekStillFails(unittest.TestCase):
    """A future week that HAS been built, and built too light, is the original 5 Jul
    defect and must keep failing. Only the zero-session case is suppressed."""

    def setUp(self):
        self.rep = _audit(_sessions(THIS_WEEK, [200, 200, 230])
                          + _sessions(NEXT_WEEK, [100]))

    def test_the_built_but_light_future_week_hard_fails(self):
        self.assertTrue(_floor_fails(self.rep, NEXT_WEEK), self.rep["fails"])
        self.assertTrue(self.rep["hard_fail"])
        self.assertIn(f"RULES:{NEXT_WEEK}:weekly_tss_floor", self.rep["hard_ids"])

    def test_and_gets_no_not_generated_note(self):
        self.assertEqual([n for n in self.rep["notes"] if "NOT GENERATED YET" in n], [])


if __name__ == "__main__":
    unittest.main()
