"""SMART goals (13 Oct 2026): a target and date for every goal but Keep my fitness, with
realistic ranges worked out from the athlete's start values."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "lib"))
import goals  # noqa: E402

SV = {"ctl": 63.5, "ftp": 270, "run_threshold": "4:30", "css": "1:35"}


def test_realistic_ranges_from_the_start_values():
    assert goals.realistic("ftp", SV, 12)["range"] == [278, 292]
    assert goals.realistic("run", SV, 12)["range"] == ["4:19", "4:25"]
    returning = goals.realistic("ftp", {"ctl": 9.3, "ftp": 212}, 12)["range"]
    assert returning[1] > 212 * 1.08                    # bigger gains coming back from a layoff


def test_targets_are_judged():
    assert goals.assess("run", "4:22", SV, 12) == "realistic"
    assert goals.assess("run", "4:00", SV, 12) == "unrealistic"
    assert goals.assess("run", "4:35", SV, 12) == "too easy"
    assert goals.assess("ftp", 296, SV, 12) == "stretch"
    assert goals.assess("ftp", 310, SV, 12) == "unrealistic"


def test_only_open_ended_goals_may_skip_a_target():
    assert goals.smart_missing({"type": "maintain"}) == []
    assert goals.smart_missing({"type": "run"}) == ["target", "date"]
    assert goals.smart_missing({"type": "run", "target": "5k in 21:30", "by": "2027-01-10"}) == []
