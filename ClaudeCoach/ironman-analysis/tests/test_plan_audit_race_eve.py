"""Race eve (Jamie, 17 Oct 2026): the day before any race is rest or one short opener.

plan_audit.race_eve_findings flags a planned workout on the eve of a race that carries
more than 10 minutes at >= 90% of threshold, or is 90+ minutes long. Openers and short
easy spins pass; so does a race the day after another race (a race is not a workout).
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


def _workout(day, name, steps, moving_min=None):
    ev = {"category": "WORKOUT", "start_date_local": f"{day}T00:00:00", "name": name,
          "workout_doc": {"steps": steps}}
    if moving_min:
        ev["moving_time"] = moving_min * 60
    return ev


def _race(day, name="Club 10", cat="RACE_C"):
    return {"category": cat, "start_date_local": f"{day}T00:00:00", "name": name}


def _step(minutes, pct, key="power", units="%ftp"):
    return {"duration": minutes * 60, key: {"value": pct, "units": units}}


class RaceEve(unittest.TestCase):
    def test_threshold_session_the_day_before_a_c_race_is_flagged(self):
        w = _workout("2026-10-24", "Threshold 3x8",
                     [_step(15, 65), {"reps": 3, "steps": [_step(8, 98), _step(3, 55)]},
                      _step(10, 60)], moving_min=58)
        out = pa.race_eve_findings([w], [_race("2026-10-25")])
        self.assertEqual(len(out), 1)
        self.assertIn("24 min at race pace or harder", out[0]["detail"])
        self.assertEqual(out[0]["week"], "2026-10-19")

    def test_an_opener_passes(self):
        w = _workout("2026-10-24", "Openers",
                     [_step(15, 60), {"reps": 3, "steps": [_step(1, 110), _step(2, 55)]},
                      _step(5, 55)], moving_min=29)
        self.assertEqual(pa.race_eve_findings([w], [_race("2026-10-25")]), [])

    def test_a_long_easy_session_the_day_before_is_flagged(self):
        w = _workout("2026-10-24", "Long Z2 ride", [_step(120, 65)], moving_min=120)
        out = pa.race_eve_findings([w], [_race("2026-10-25", cat="RACE_A")])
        self.assertEqual(len(out), 1)
        self.assertIn("120 min long", out[0]["detail"])

    def test_run_pace_steps_count(self):
        w = _workout("2026-10-24", "Tempo run",
                     [_step(10, 80, "pace", "%pace"), _step(20, 95, "pace", "%pace")],
                     moving_min=30)
        self.assertEqual(len(pa.race_eve_findings([w], [_race("2026-10-25")])), 1)

    def test_a_hard_session_two_days_out_is_not_race_eve(self):
        w = _workout("2026-10-23", "VO2 5x4", [{"reps": 5, "steps": [_step(4, 115), _step(4, 50)]}],
                     moving_min=60)
        self.assertEqual(pa.race_eve_findings([w], [_race("2026-10-25")]), [])

    def test_no_races_no_findings(self):
        w = _workout("2026-10-24", "Threshold", [_step(30, 100)], moving_min=30)
        self.assertEqual(pa.race_eve_findings([w], []), [])


if __name__ == "__main__":
    unittest.main()
