"""A generated, partially-planned post-race recovery/transition week must not
hard-fail the TSS floor either (genuinely open case left after 3c1e345f / 9f26e45c).

required_tss() only recognises "post-race" off cfg["race_date"]: the moment the
NEXT race is configured — routinely done before the blueprint sidecar is
regenerated to match — race_d stops being in the past and required_tss falls back
to the new block's standard phase target, even on days the LIVE blueprint still
has marked Transition. Without this fix that mismatch re-armed the standard-phase
floor against a week that is deliberately light, reproducing the same
"UNDER-TRAINING ... DETRAINS the athlete" false alarm the unbuilt-week and
fully-pinned-week fixes already killed for their own causes.

The fix does not just drop the floor to 0 — the week is meant to be LIGHT, not
unchecked — it replaces the stale standard-phase floor with the maintenance-ramp
floor the same convergence logic (4f9f1f5c) uses off-season, so a week that is
under even THAT reduced target still fails.
"""
import contextlib
import importlib.util
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

CTL = 60.0
MAINTENANCE_CTL = 40.0
# The stale standard-phase floor required_tss fell back to once the next race was
# configured — a real "base phase" style number, well above what a still-recovering
# week should be held to.
STALE_STANDARD_FLOOR = 400
RAMP_FLOOR = pa.pt.compute_required_tss(CTL, MAINTENANCE_CTL, pa.pt._MAINTENANCE_CONVERGE_WEEKS)

CFG = {"icu_athlete_id": "i1", "icu_api_key": "k", "nutrition_target_g_hr": 90,
       "race_target_splits": {"bike_min": 260}, "day_rules": {},
       "max_ctl_ramp_per_week": 5.0, "run_protocol": {},
       "ctl_targets": {"maintenance_ctl": MAINTENANCE_CTL}}


def _sessions(week_start, loads):
    return [{"category": "WORKOUT", "type": "Ride",
             "start_date_local": f"{(week_start + timedelta(days=i)).isoformat()}T07:00:00",
             "name": f"Recovery spin {i}", "description": "Easy. Fuel 90 g/hr.",
             "moving_time": 60 * 60, "icu_training_load": load,
             "workout_doc": {"steps": [{"power": {"value": 0.55}}]}}
            for i, load in enumerate(loads)]


class _Client:
    def __init__(self, events):
        self._events = events

    def get_events(self, start, end):
        return list(self._events)

    def get_wellness(self, days=3):
        return [{"ctl": CTL}]


def _audit(events):
    with contextlib.ExitStack() as st:
        p = st.enter_context
        p(mock.patch.object(pa, "_client", lambda cfg: _Client(events)))
        p(mock.patch.object(pa, "fuel_target", lambda *a, **k: 90))
        # The LIVE blueprint still has this week as post-race Transition...
        p(mock.patch.object(pa, "current_phase",
                            lambda bp, ws: {"name": "Transition", "family": "transition"}))
        p(mock.patch.object(pa.pt, "_load_blueprint", lambda slug: {}))
        p(mock.patch.object(pa.pt, "last_week_actual_tss", lambda client: None))
        # ...but required_tss, keyed off cfg["race_date"] alone, has already moved on to
        # the next block's standard phase target because the next race got configured
        # before the sidecar was regenerated.
        p(mock.patch.object(pa.pt, "required_tss", lambda *a, **k: {
            "recommended_weekly_tss": STALE_STANDARD_FLOOR,
            "weekly_tss_floor": STALE_STANDARD_FLOOR}))
        p(mock.patch.object(pa.pt, "run_caps", lambda *a, **k: {"weekly_min_cap": None}))
        p(mock.patch.object(pa, "_weekly_tss_cap", lambda slug, phase, week_start=None: 900.0))
        p(mock.patch.object(pa.day_overrides, "load", lambda slug, base: {}))
        p(mock.patch.object(pa.weekly_availability, "effective_day_rules",
                            lambda *a, **k: ({}, None)))
        p(mock.patch.object(pa, "STREAKS", Path(tempfile.mkdtemp()) / "streaks.json"))
        return pa.audit_athlete("tester", CFG, weeks=2)


def _floor_fails(report, week):
    return [f for f in (report.get("fails") or {}).get("RULES", [])
            if "weekly_tss_floor" in f and str(week) in f]


class RecoveryWeekBelowStandardFloorButOverRampFloor(unittest.TestCase):
    """Genuinely light, intentional recovery load — over the maintenance-ramp floor,
    under the stale standard-phase one. Must not hard-fail or page."""

    def setUp(self):
        assert RAMP_FLOOR < STALE_STANDARD_FLOOR, RAMP_FLOOR   # the fixture must be real
        over_ramp_floor = int(RAMP_FLOOR * 1.1)
        self.rep = _audit(_sessions(THIS_WEEK, [over_ramp_floor // 3] * 3))

    def test_no_floor_hard_fail(self):
        self.assertEqual(_floor_fails(self.rep, THIS_WEEK), [])

    def test_it_does_not_page(self):
        self.assertFalse(self.rep["hard_fail"])
        self.assertEqual([i for i in self.rep["hard_ids"] if "weekly_tss_floor" in i], [])

    def test_the_note_names_the_swap(self):
        self.assertTrue(any("POST-RACE" in n and "maintenance-ramp floor" in n
                            for n in self.rep["notes"]), self.rep["notes"])


class RecoveryWeekStillBelowTheRampFloorHardFails(unittest.TestCase):
    """The swap must not become a blanket suppression: a week under even the reduced
    maintenance-ramp target is still a real under-training failure."""

    def setUp(self):
        under_ramp_floor = max(0, int(RAMP_FLOOR * 0.5))
        self.rep = _audit(_sessions(THIS_WEEK, [under_ramp_floor]))

    def test_the_floor_still_fires_at_the_ramp_level(self):
        self.assertTrue(_floor_fails(self.rep, THIS_WEEK), self.rep["fails"])
        self.assertTrue(self.rep["hard_fail"])


if __name__ == "__main__":
    unittest.main()
