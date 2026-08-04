"""No ride over the ceiling — close the gap with intensity, not more duration.

Jamie, 4 Aug 2026: "I don't think there is much benefit going over 5hrs, I would
rather add intensity below it to get the same TSS." His 6 Aug ride was 330min against
a 300min ceiling. plan_audit had caught it, but only once it was already on the
calendar; nothing stopped the proposer writing it, because the brief's
long_ride_target_min (255 for him) is advisory prose in a prompt. Hard at plan time
means the attempt is blocking and the planner re-proposes.
"""
import unittest
from datetime import date

from primitives.validate_plan import validate_week

WS = date(2026, 8, 3)


def _ride(mins, name="Long ride", sport="Ride"):
    return [{"start_date_local": "2026-08-06T00:00:00", "type": sport,
             "category": "WORKOUT", "load_target": 200, "name": name,
             "moving_time": int(mins * 60)}]


def _codes(rep, sev=None):
    return [v.code for v in rep.violations if sev is None or v.severity == sev]


class Ceiling(unittest.TestCase):
    def test_a_ride_over_the_ceiling_is_hard(self):
        rep = validate_week(_ride(330), WS, long_ride_max_min=300)
        self.assertIn("long_ride_over_ceiling", _codes(rep, "hard"))

    def test_the_message_says_to_add_intensity_not_duration(self):
        rep = validate_week(_ride(330), WS, long_ride_max_min=300)
        detail = next(v.detail for v in rep.violations if v.code == "long_ride_over_ceiling")
        self.assertIn("INTENSITY", detail)
        self.assertIn("330min", detail)

    def test_a_ride_at_the_ceiling_passes(self):
        rep = validate_week(_ride(300), WS, long_ride_max_min=300)
        self.assertNotIn("long_ride_over_ceiling", _codes(rep))

    def test_virtual_and_gravel_rides_count(self):
        for sport in ("VirtualRide", "GravelRide"):
            rep = validate_week(_ride(330, sport=sport), WS, long_ride_max_min=300)
            self.assertIn("long_ride_over_ceiling", _codes(rep, "hard"), sport)

    def test_a_long_run_is_not_a_ride(self):
        rep = validate_week(_ride(330, sport="Run"), WS, long_ride_max_min=300)
        self.assertNotIn("long_ride_over_ceiling", _codes(rep))

    def test_an_athlete_specific_ceiling_is_honoured(self):
        rep = validate_week(_ride(330), WS, long_ride_max_min=360)
        self.assertNotIn("long_ride_over_ceiling", _codes(rep))

    def test_an_unsupplied_ceiling_is_a_skip_not_a_silent_pass(self):
        rep = validate_week(_ride(330), WS)
        self.assertNotIn("long_ride_over_ceiling", _codes(rep))
        self.assertTrue(any("long_ride" in s for s in rep.skipped))

    def test_an_event_with_no_duration_cannot_breach(self):
        e = _ride(330)
        e[0]["moving_time"] = None
        rep = validate_week(e, WS, long_ride_max_min=300)
        self.assertNotIn("long_ride_over_ceiling", _codes(rep))


if __name__ == "__main__":
    unittest.main()
