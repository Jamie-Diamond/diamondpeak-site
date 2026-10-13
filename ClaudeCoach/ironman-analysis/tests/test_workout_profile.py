"""lib/workout_profile: zone-coloured blocks for planned and done sessions (13 Oct 2026)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "lib"))
import workout_profile as wp  # noqa: E402

SS = {"ftp": 200, "power_zones": [55, 75, 90, 105, 120, 150, 999],
      "threshold_pace": 4.0, "pace_zones": [77.5, 87.7, 94.3, 100, 103.4, 111.5, 999]}


def test_planned_expands_repeats_and_colours_by_zone():
    doc = {"steps": [{"duration": 600, "power": {"start": 60, "end": 70, "units": "%ftp"}},
                     {"reps": 2, "steps": [{"duration": 480, "power": {"start": 95, "end": 102, "units": "%ftp"}},
                                           {"duration": 240, "power": {"start": 45, "end": 55, "units": "%ftp"}}]}]}
    assert wp.planned(doc, SS) == [[600, 65, 2], [480, 98, 4], [240, 50, 1], [480, 98, 4], [240, 50, 1]]


def test_completed_ride_by_power_run_by_pace():
    n = 600
    ride = {"watts": [200] * n, "time": list(range(n))}
    b = wp.completed(ride, SS, "Ride")
    assert b and all(x[1] == 100 and x[2] == 4 for x in b)
    run = {"watts": [400] * n, "velocity_smooth": [3.4] * n, "time": list(range(n))}
    b = wp.completed(run, dict(SS, ftp=452), "Run")
    assert b and b[0][1] == 85 and b[0][2] == 2          # by pace (Z2), not running power
