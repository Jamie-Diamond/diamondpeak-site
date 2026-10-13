"""lib/workout_steps: a planned workout in plain words with the athlete's own numbers."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "lib"))
import workout_steps as ws  # noqa: E402


def P(lo, hi, s):
    return {"power": {"start": lo, "end": hi, "units": "%ftp"}, "duration": s}


def test_tempo_ride_in_watts_with_repeats_folded():
    doc = {"steps": [P(60, 70, 900), P(76, 84, 1500), P(50, 58, 300), P(76, 84, 1500), P(60, 70, 600)]}
    assert ws.lines(doc, {"ftp": 196}) == [
        "15 min · warm-up · 118-137 W (60-70% FTP)",
        "2 × 25 min · tempo · 149-165 W (76-84% FTP), with 5 min easy (98-114 W) between",
        "10 min · cool-down · 118-137 W (60-70% FTP)"]


def test_run_pace_from_threshold_and_no_structure():
    doc = {"steps": [{"pace": {"start": 78, "end": 88, "units": "%pace"}, "duration": 1920}]}
    assert ws.lines(doc, {"pace_s": 242, "pace_unit": "/km"}) == [
        "32 min · easy · 4:35-5:10/km (78-88% of threshold pace)"]
    assert ws.lines(None, {}) == [] and ws.lines({"steps": [P(60, 70, 600)]}, {}) == [
        "10 min · endurance · 60-70% FTP"]
