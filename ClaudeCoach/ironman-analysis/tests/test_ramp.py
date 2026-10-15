"""lib/ramp.py: one weekly CTL ramp cap, scaled by Fitness (Jamie, 15 Oct 2026)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "lib"))
import ramp  # noqa: E402


def test_scaled():
    assert ramp.scaled(10) == 3.0 and ramp.scaled(20) == 3.0
    assert ramp.scaled(40) == 4.0
    assert ramp.scaled(60) == 5.0 and ramp.scaled(95) == 5.0
    assert ramp.scaled(None) == 5.0


def test_own_setting_wins():
    assert ramp.cap({"max_ctl_ramp_per_week": 6}, ctl=10) == 6.0
    assert ramp.cap({}, ctl=10) == 3.0
    assert ramp.cap({"max_ctl_ramp_per_week": None}, ctl=80) == 5.0
