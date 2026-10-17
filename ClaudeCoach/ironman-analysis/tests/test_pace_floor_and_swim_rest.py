"""Pace floor and swim rest (Jamie, 17 Oct 2026).

FLOOR. For four months run warm-ups went to the watch at 56-64% of threshold pace
(6:18-7:12/km off a 4:02 threshold) and swim warm-ups at 58-66% of CSS. The name check
only looks one way (a hard session built easy). These lock the other way, at every layer:
render clamps, push refuses (any source, including hand-written text), the daily audit
flags what is already on the calendar.

REST. A swim recovery between reps is standing at the wall: "- Rest 30s", no target,
zero load, so Intervals.icu's planned swim Load matches logged swims.
"""
import contextlib
import importlib.util
import json
import sys
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

import pytest

from primitives.planned_tss import (PACE_FLOOR_PCT, PaceFloorError, _TID_ALIAS, _ZONE_BAND,
                                    assert_pace_floor, name_intensity_mismatch,
                                    render_workout, slow_pace_steps, top_step_pct)

CC = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(CC / "lib"))
import icu_api  # noqa: E402


# ── the parser ────────────────────────────────────────────────────────────────
class SlowPaceSteps(unittest.TestCase):
    def test_the_old_run_warmup_is_caught(self):
        bad = slow_pace_steps("Run", "- 15m 56-64% Pace\n- 30m 78-88% Pace")
        self.assertEqual([(b["pct"], b["floor"]) for b in bad], [(56, 68)])

    def test_run_recovery_band_is_exactly_the_floor_and_passes(self):
        self.assertEqual(slow_pace_steps("Run", "- 5m 68-76% Pace"), [])

    def test_old_swim_steps_are_caught(self):
        bad = slow_pace_steps("Swim", "- 7m 70-80% Pace\n- 20s 58-66% Pace\n- 4m 85-91% Pace")
        self.assertEqual([b["pct"] for b in bad], [70, 58])

    def test_swim_drill_band_is_exactly_the_floor_and_passes(self):
        self.assertEqual(slow_pace_steps("Swim", "- 5m 78-86% Pace"), [])

    def test_marked_kick_is_exempt_and_unmarked_slow_swim_is_not(self):
        self.assertEqual(slow_pace_steps("Swim", "- Kick 5m 64-74% Pace"), [])
        self.assertEqual(len(slow_pace_steps("Swim", "- 5m 64-74% Pace")), 1)

    def test_kick_is_not_an_exemption_for_run(self):
        self.assertEqual(len(slow_pace_steps("Run", "- Kick 5m 60-64% Pace")), 1)

    def test_a_single_value_target_is_read_too(self):
        self.assertEqual(slow_pace_steps("Run", "- 10m 60% Pace")[0]["pct"], 60)

    def test_rest_and_rpe_steps_have_no_target_and_pass(self):
        self.assertEqual(slow_pace_steps("Swim", "- Rest 30s\n- Easy swim, RPE 3 10m"), [])

    def test_bike_power_and_strength_are_not_checked(self):
        self.assertEqual(slow_pace_steps("Ride", "- 10m 50-55%"), [])
        self.assertEqual(slow_pace_steps("WeightTraining", "- 10m 50% Pace"), [])

    def test_run_variants_are_run(self):
        for t in ("TrailRun", "VirtualRun"):
            self.assertEqual(len(slow_pace_steps(t, "- 10m 56-64% Pace")), 1, t)

    def test_notes_prose_is_not_read_as_a_step(self):
        desc = "- 30m 78-88% Pace\n\nNotes\n• last time 60% pace felt too slow"
        self.assertEqual(slow_pace_steps("Run", desc), [])

    def test_assert_raises_with_a_message_that_says_how_to_fix_it(self):
        with self.assertRaises(PaceFloorError) as cm:
            assert_pace_floor("Swim", "- 20s 58-66% Pace")
        self.assertIn("78%", str(cm.exception))
        self.assertIn("- Rest 30s", str(cm.exception))
        self.assertTrue(issubclass(PaceFloorError, ValueError))


# ── render: never produces a slow step ──────────────────────────────────────────
class RenderNeverBelowFloor(unittest.TestCase):
    def test_every_zone_and_label_renders_at_or_above_the_floor(self):
        for sport in ("Run", "Swim"):
            sp = sport.lower()
            zones = set(_ZONE_BAND[sp]) | set(_TID_ALIAS[sp]) | {"mystery"}
            for z in zones:
                r = render_workout(sport, [{"minutes": 10, "zone": z}])
                self.assertEqual(slow_pace_steps(sport, r["description"]), [],
                                 f"{sport} {z}: {r['description']}")

    def test_an_explicit_low_if_is_clamped_and_reported(self):
        r = render_workout("Run", [{"minutes": 20, "zone": "Z2", "if": 0.60}])
        self.assertEqual(r["description"], "- 20m 68-76% Pace")
        self.assertEqual(r["pace_floor_clamped"][0]["was"], "56-64%")
        # Run's IF is its %pace, so the computed load is the load of what is pushed.
        self.assertEqual(r["avg_if"], 0.72)

    def test_unknown_swim_zone_default_is_clamped(self):
        r = render_workout("Swim", [{"minutes": 10, "zone": "mystery"}])
        self.assertEqual(r["description"], "- 10m 78-86% Pace")

    def test_a_clean_render_carries_no_clamp_key(self):
        self.assertNotIn("pace_floor_clamped",
                         render_workout("Run", [{"minutes": 30, "zone": "easy"}]))

    def test_kick_renders_marked_and_unclamped(self):
        r = render_workout("Swim", [{"minutes": 6, "zone": "kick"}])
        self.assertEqual(r["description"], "- Kick 6m 64-74% Pace")
        self.assertEqual(slow_pace_steps("Swim", r["description"]), [])
        # A kick step still counts as a step for the name check.
        self.assertEqual(top_step_pct(r["description"])[0], 74)

    def test_kick_only_swim_named_css_still_fails_the_name_check(self):
        r = render_workout("Swim", [{"minutes": 30, "zone": "kick"}], "CSS 5x400")
        self.assertIsNotNone(name_intensity_mismatch("Swim", "CSS 5x400", r["description"]))


# ── push: refuses from any source ───────────────────────────────────────────────
class _NoNetClient(icu_api.IcuClient):
    def __init__(self, event=None):
        super().__init__("i0", "k")
        self.sent = []
        self._event = event or {}

    def _post(self, path, payload):
        self.sent.append(("post", payload))
        return {"id": 1}

    def _put(self, path, payload):
        self.sent.append(("put", payload))
        return {"id": 1}

    def get_event(self, event_id):
        return dict(self._event)


class PushRefuses(unittest.TestCase):
    def setUp(self):
        self._indoor = mock.patch.dict(sys.modules, {"indoor": None})
        self._indoor.start()

    def tearDown(self):
        self._indoor.stop()

    def test_push_refuses_a_hand_written_slow_run_and_sends_nothing(self):
        c = _NoNetClient()
        with self.assertRaises(PaceFloorError):
            c.push_workout("Run", "2026-10-20", "Easy run",
                           description="- 15m 56-64% Pace\n- 30m 78-88% Pace")
        self.assertEqual(c.sent, [])

    def test_push_refuses_a_swim_rest_written_as_slow_swimming(self):
        c = _NoNetClient()
        with self.assertRaises(PaceFloorError):
            c.push_workout("Swim", "2026-10-20", "CSS", description="- 7m 97-103% Pace\n- 1m 58-66% Pace")
        self.assertEqual(c.sent, [])

    def test_push_of_a_rendered_session_goes_through(self):
        c = _NoNetClient()
        desc = render_workout("Swim", [{"minutes": 10, "zone": "warmup"},
                                       {"repeat": 3, "steps": [{"minutes": 5, "zone": "css"},
                                                               {"minutes": 0.5, "zone": "recovery"}]},
                                       {"minutes": 5, "zone": "kick"}])["description"]
        c.push_workout("Swim", "2026-10-20", "CSS 3x5", description=desc)
        self.assertEqual(len(c.sent), 1)

    def test_bike_push_is_untouched(self):
        c = _NoNetClient()
        c.push_workout("Ride", "2026-10-20", "Easy", description="- 60m 50-55%")
        self.assertEqual(len(c.sent), 1)

    def test_edit_with_a_slow_description_is_refused_using_the_events_own_type(self):
        c = _NoNetClient(event={"type": "Run"})
        with self.assertRaises(PaceFloorError):
            c.edit_workout(42, description="- 15m 56-64% Pace")
        self.assertEqual(c.sent, [])

    def test_edit_without_a_description_is_not_checked(self):
        c = _NoNetClient(event={"type": "Run", "description": "- 15m 56-64% Pace"})
        c.edit_workout(42, name="Easy run", type="VirtualRun")
        self.assertEqual(len(c.sent), 1)


class IcuFetchCli(unittest.TestCase):
    def test_a_refusal_exits_2_and_is_logged_for_the_dev_inbox(self):
        spec = importlib.util.spec_from_file_location("icu_fetch_t", CC / "lib" / "icu_fetch.py")
        f = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(f)
        args = mock.Mock(endpoint="push_workout", caller="chat", athlete="tester")

        def _write():
            raise PaceFloorError("Run step '- 15m 56-64% Pace' ...")

        import ops_log
        with mock.patch.object(ops_log, "alert") as al, pytest.raises(SystemExit) as ex:
            f._floor_checked(args, _write)
        self.assertEqual(ex.value.code, 2)
        self.assertIn("pace floor refused", al.call_args[0][1])

    def test_other_value_errors_are_not_swallowed(self):
        spec = importlib.util.spec_from_file_location("icu_fetch_t2", CC / "lib" / "icu_fetch.py")
        f = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(f)

        def _write():
            raise ValueError("something else")

        with self.assertRaises(ValueError):
            f._floor_checked(mock.Mock(), _write)


class PlanBuilderPreflight(unittest.TestCase):
    def test_a_slow_session_stops_the_week_before_the_first_push(self):
        import plan_builder
        built = {"week_start": "2026-10-19", "sessions": [
            {"sport": "Ride", "date": "2026-10-19", "name": "Easy", "description": "- 60m 60-70%",
             "description_raw": "", "load_target": 40},
            {"sport": "Run", "date": "2026-10-20", "name": "Easy run",
             "description": "- 15m 56-64% Pace", "description_raw": "", "load_target": 40}]}
        with mock.patch.object(plan_builder, "_cfg") as cfg, \
                mock.patch("icu_api.IcuClient") as client:
            with self.assertRaises(PaceFloorError):
                plan_builder.push("tester", built, replace=True)
        cfg.assert_not_called()
        client.assert_not_called()


# ── audit: flags what is already on the calendar ────────────────────────────────
_h_spec = importlib.util.spec_from_file_location(
    "pace_floor_audit_harness", Path(__file__).resolve().parent / "test_plan_audit_post_race_recovery.py")
h = importlib.util.module_from_spec(_h_spec)
_h_spec.loader.exec_module(h)
pa = h.pa


def _audit(events):
    with contextlib.ExitStack() as st:
        p = st.enter_context
        p(mock.patch.object(pa, "_client", lambda cfg: h._Client(events)))
        p(mock.patch.object(pa, "fuel_target", lambda *a, **k: 90))
        p(mock.patch.object(pa, "current_phase", lambda bp, ws: {"name": "Build", "family": "build"}))
        p(mock.patch.object(pa.pt, "_load_blueprint", lambda slug: {}))
        p(mock.patch.object(pa.pt, "last_week_actual_tss", lambda client: None))
        p(mock.patch.object(pa.pt, "required_tss", lambda *a, **k: {}))
        p(mock.patch.object(pa.pt, "run_caps", lambda *a, **k: {"weekly_min_cap": None}))
        p(mock.patch.object(pa, "_weekly_tss_cap", lambda slug, phase, week_start=None: 900.0))
        p(mock.patch.object(pa.day_overrides, "load", lambda slug, base: {}))
        p(mock.patch.object(pa.weekly_availability, "effective_day_rules", lambda *a, **k: ({}, None)))
        p(mock.patch.object(pa, "STREAKS", Path(tempfile.mkdtemp()) / "streaks.json"))
        return pa.audit_athlete("tester", h.CFG, weeks=2)


def _run(day, desc, name="Easy run"):
    return {"category": "WORKOUT", "type": "Run", "name": name, "description": desc,
            "start_date_local": f"{day.isoformat()}T07:00:00", "moving_time": 2700,
            "icu_training_load": 40, "workout_doc": {"steps": [{"duration": 900}]}}


class AuditFlagsSlowSteps(unittest.TestCase):
    def test_a_future_slow_run_is_a_hard_structure_finding(self):
        rep = _audit([_run(h.TODAY + timedelta(days=1), "- 15m 56-64% Pace\n- 30m 78-88% Pace")])
        self.assertTrue(rep["hard_fail"])
        self.assertTrue(any("68% floor" in x for x in rep["fails"]["STRUCTURE"]))
        self.assertTrue(any(":slow_pace_step:" in i for i in rep["hard_ids"]))

    def test_a_session_already_past_is_not_a_finding(self):
        past = h.TODAY - timedelta(days=1)
        if past < h.THIS_WEEK:
            self.skipTest("Monday: no past day inside the audit window")
        rep = _audit([_run(past, "- 15m 56-64% Pace")])
        self.assertFalse(any(":slow_pace_step:" in i for i in rep.get("hard_ids", [])))

    def test_a_clean_run_is_not_flagged(self):
        rep = _audit([_run(h.TODAY + timedelta(days=1), "- 15m 70-78% Pace\n- 30m 78-88% Pace")])
        self.assertFalse(any(":slow_pace_step:" in i for i in rep.get("hard_ids", [])))


# ── swim rest between reps ──────────────────────────────────────────────────────
CSS_5x7 = [{"minutes": 10, "zone": "warmup"},
           {"repeat": 5, "steps": [{"minutes": 7, "zone": "css"}, {"minutes": 1, "zone": "recovery"}]},
           {"minutes": 5, "zone": "cooldown"}]


class SwimRest(unittest.TestCase):
    def test_recovery_inside_a_repeat_is_rest_at_the_wall(self):
        r = render_workout("Swim", CSS_5x7, "CSS 5x7")
        lines = r["description"].splitlines()
        self.assertEqual(lines[0], "- 10m 85-91% Pace")          # warm-up still swimming
        self.assertEqual(lines[1:3], ["- 7m 97-103% Pace", "- Rest 1m"])
        self.assertEqual(lines.count("- Rest 1m"), 5)
        self.assertEqual(lines[-1], "- 5m 85-91% Pace")         # cool-down still swimming
        self.assertEqual(r["duration_min"], 55)                  # rest is still time

    def test_rest_carries_no_load(self):
        with_rest = render_workout("Swim", CSS_5x7)["tss"]
        no_rest = render_workout("Swim", [{"minutes": 10, "zone": "warmup"},
                                          {"repeat": 5, "steps": [{"minutes": 7, "zone": "css"}]},
                                          {"minutes": 5, "zone": "cooldown"}])["tss"]
        self.assertEqual(with_rest, no_rest)

    def test_sub_minute_rest_renders_in_seconds(self):
        r = render_workout("Swim", [{"repeat": 2, "steps": [{"minutes": 2, "zone": "css"},
                                                            {"minutes": 0.25, "zone": "Z1"}]}])
        self.assertEqual(r["description"].splitlines()[1], "- Rest 15s")

    def test_a_recovery_only_swim_stays_swimming(self):
        r = render_workout("Swim", [{"repeat": 4, "steps": [{"minutes": 5, "zone": "recovery"}]}])
        self.assertNotIn("Rest", r["description"])
        r = render_workout("Swim", [{"minutes": 30, "zone": "recovery"}])
        self.assertEqual(r["description"], "- 30m 85-91% Pace")

    def test_an_explicit_rest_zone_is_rest_anywhere(self):
        r = render_workout("Swim", [{"minutes": 10, "zone": "easy"}, {"minutes": 2, "zone": "rest"}])
        self.assertEqual(r["description"].splitlines()[1], "- Rest 2m")

    def test_run_and_bike_recoveries_are_unchanged(self):
        for sport in ("Run", "Ride"):
            r = render_workout(sport, [{"repeat": 2, "steps": [{"minutes": 3, "zone": "threshold"},
                                                               {"minutes": 2, "zone": "recovery"}]}])
            self.assertNotIn("Rest", r["description"])

    def test_rest_keeps_the_name_check_and_the_floor_happy(self):
        r = render_workout("Swim", CSS_5x7, "CSS 5x7")
        self.assertIsNone(name_intensity_mismatch("Swim", "CSS 5x7", r["description"]))
        self.assertEqual(slow_pace_steps("Swim", r["description"]), [])

    def test_rpe_only_renders_the_same_rest(self):
        r = render_workout("Swim", CSS_5x7, rpe_only=True)
        self.assertIn("- Rest 1m", r["description"].splitlines())


def test_floors_are_the_documented_numbers():
    assert PACE_FLOOR_PCT == {"run": 68, "swim": 78}
    assert _ZONE_BAND["run"]["recovery"][0] == 68 and _ZONE_BAND["swim"]["drill"][0] == 78


def test_run_walk_and_rest_recoveries_have_no_pace_target():
    """Jamie, 17 Oct 2026: the pace floor must not ban walking recoveries in a run."""
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "lib"))
    import icu_api
    from primitives.planned_tss import render_workout
    r = render_workout("Run", [{"repeat": 2, "steps": [{"minutes": 3, "zone": "threshold"},
                                                       {"minutes": 1, "zone": "walk"}]},
                               {"minutes": 1, "zone": "rest"}])
    assert "- Walk 1m" in r["description"] and "- Rest 1m" in r["description"]
    icu_api.pace_floor_guard("Run", r["description"])      # passes: no % target on them
