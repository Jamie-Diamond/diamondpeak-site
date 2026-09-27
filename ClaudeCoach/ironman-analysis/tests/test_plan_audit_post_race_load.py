"""The plan audit must not fail a post-race recovery week on WEEKLY_LOAD.

Its target is a recovery fraction of maintenance, not a dose to hit, and a WEEKLY_LOAD
hard fail pages the coach with "N TSS vs target ~M (>X% off)" (Jamie, 27 Sep 2026: no
"X% off target" messages). Same exemption as stage1's _load_on_target. A normal week
is still checked.
"""
import contextlib
import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location(
    "post_race_audit_harness", _HERE / "test_plan_audit_post_race_recovery.py")
h = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(h)
pa = h.pa

TARGET = 300


def _audit(events, week_type):
    with contextlib.ExitStack() as st:
        p = st.enter_context
        p(mock.patch.object(pa, "_client", lambda cfg: h._Client(events)))
        p(mock.patch.object(pa, "fuel_target", lambda *a, **k: 90))
        p(mock.patch.object(pa, "current_phase",
                            lambda bp, ws: {"name": "Transition", "family": "transition"}))
        p(mock.patch.object(pa.pt, "_load_blueprint", lambda slug: {}))
        p(mock.patch.object(pa.pt, "last_week_actual_tss", lambda client: None))
        p(mock.patch.object(pa.pt, "required_tss", lambda *a, **k: {
            "recommended_weekly_tss": TARGET, "weekly_tss_floor": 0,
            "week_type": week_type}))
        p(mock.patch.object(pa.pt, "run_caps", lambda *a, **k: {"weekly_min_cap": None}))
        p(mock.patch.object(pa, "_weekly_tss_cap", lambda slug, phase, week_start=None: 900.0))
        p(mock.patch.object(pa.day_overrides, "load", lambda slug, base: {}))
        p(mock.patch.object(pa.weekly_availability, "effective_day_rules",
                            lambda *a, **k: ({}, None)))
        p(mock.patch.object(pa, "STREAKS", Path(tempfile.mkdtemp()) / "streaks.json"))
        return pa.audit_athlete("tester", h.CFG, weeks=2)


def _load_fails(report):
    return (report.get("fails") or {}).get("WEEKLY_LOAD", [])


class PostRaceWeekOffTargetIsNotAFail(unittest.TestCase):
    def test_over_target_recovery_week_passes(self):
        rep = _audit(h._sessions(h.THIS_WEEK, [140, 140, 140]), "post_race")   # +40%
        self.assertEqual(_load_fails(rep), [])

    def test_under_target_recovery_week_passes(self):
        rep = _audit(h._sessions(h.THIS_WEEK, [60]), "post_race")              # -80%
        self.assertEqual(_load_fails(rep), [])


class NormalWeekOffTargetStillFails(unittest.TestCase):
    def test_the_check_is_not_switched_off_for_everyone(self):
        rep = _audit(h._sessions(h.THIS_WEEK, [140, 140, 140]), "build")
        self.assertTrue(_load_fails(rep))


if __name__ == "__main__":
    unittest.main()
