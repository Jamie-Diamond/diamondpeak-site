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
    # one steady effort, drawn as 3 laps (Jamie, 15 Oct 2026: never one long line)
    assert len(b) == 3 and all(x[1] == 100 and x[2] == 4 for x in b)
    run = {"watts": [400] * n, "velocity_smooth": [3.4] * n, "time": list(range(n))}
    b = wp.completed(run, dict(SS, ftp=452), "Run")
    assert b and b[0][1] == 85 and b[0][2] == 2          # by pace (Z2), not running power


def test_intervals_come_out_as_blocks_not_minutes():
    """Jamie, 13 Oct 2026: obvious intervals should be one block each."""
    import random
    random.seed(1)
    w = ([120] * 600 + ([190] * 600 + [120] * 300) * 3 + [120] * 600)
    w = [x + random.randint(-15, 15) for x in w]           # road-style noise
    b = wp.completed({"watts": w, "time": list(range(len(w)))}, SS, "Ride")
    hard = [x for x in b if x[1] > 85]
    assert len(hard) == 3 and all(540 <= x[0] <= 660 for x in hard)


def test_plan_is_retimed_onto_the_efforts_and_changes_flagged():
    """Jamie, 13 Oct 2026: a 5-min-longer warm-up must not push every target out of sync."""
    plan = [[600, 65, 2], [480, 98, 4], [240, 50, 1], [480, 98, 4], [600, 65, 2]]
    done = [[900, 66, 2], [480, 99, 4], [240, 55, 1], [480, 97, 4], [600, 62, 2]]
    fit = wp.fit_plan(plan, done)
    assert [b[0] for b in fit] == [600, 300, 480, 240, 480, 600]     # efforts pinned
    assert [b[2] for b in fit] == [0, 2, 0, 0, 0, 0]                 # only the extra 5 min flagged
    cut = wp.fit_plan(plan, [[300, 66, 2], [480, 99, 4], [240, 55, 1], [480, 97, 4], [600, 62, 2]])
    assert cut[0] == [300, 65, -1] and cut[1][2] == 0               # warm-up cut short
    assert wp.fit_plan(plan, [[900, 66, 2], [480, 99, 4], [600, 62, 2]]) is None  # a skipped effort


def test_executed_counts_efforts_from_the_stream():
    plan = [[600, 65, 2], [600, 91, 3], [300, 50, 1], [600, 91, 3], [300, 50, 1], [600, 91, 3], [900, 65, 2]]
    done = [[780, 66, 2], [720, 88, 3], [300, 55, 1], [630, 95, 4], [300, 52, 1], [600, 102, 4], [1100, 62, 2]]
    line = wp.executed(plan, done)
    assert "efforts in the power stream: 12 min at 88%, 10.5 min at 95%, 10 min at 102%" in line
    assert "planned structure EXECUTED" in line
    short = wp.executed(plan, done[:4] + [[1500, 60, 2]])
    assert "NOT fully executed (2 of 3 efforts found)" in short
    assert wp.executed([[3600, 65, 2]], done) == ""                  # no efforts planned


def test_done_sessions_get_laps():
    """Jamie, 15 Oct 2026: at least 3 laps, none over 5 km on a run, split evenly."""
    n = 4800                                   # 80 min at 3.4 m/s = 16.3 km
    run = {"velocity_smooth": [3.4] * n, "time": list(range(n))}
    b = wp.completed(run, SS, "Run")
    km = [x[0] * 3.4 / 1000 for x in b]
    assert len(b) >= 5 and max(km) <= 5.5               # warm-up, 3+ main laps, warm-down
    main = km[1:-1]
    assert max(main) - min(main) < 0.6                  # even, not 5 + 5 + 1
    short = {"velocity_smooth": [3.4] * 1500, "time": list(range(1500))}
    assert len(wp.completed(short, SS, "Run")) == 3      # 25 min easy: warm-up / main / down
