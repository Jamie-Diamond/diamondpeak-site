"""A goal's distance is not its time (Jamie, 16 Oct 2026: "10k in 50 min" was read as 10 min,
so Kathryn's band came out at the sub-40 level)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "lib"))
import race_fitness as rf  # noqa: E402


def test_distance_in_the_goal_is_skipped():
    assert rf.parse_goal_s("10k in 50 min", "10k") == 3000
    assert rf.parse_goal_s("5k in 19:30", "5k") == 1170
    assert rf.parse_goal_s("sub 40", "10k") == 2400
    assert rf.parse_goal_s("Sub 3:15", "marathon") == 3 * 3600 + 15 * 60


def test_kathryns_goal_gets_the_47_55_level():
    assert rf.level_for_time("10k", rf.parse_goal_s("10k in 50 min", "10k"))["level"] == 3
