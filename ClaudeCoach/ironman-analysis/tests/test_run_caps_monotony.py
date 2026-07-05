"""Run-volume caps in validate_week + Foster monotony guard (audit P1-9, P1-3)."""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

from primitives.validate_plan import validate_week

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "lib"))
import plan_tools as pt  # noqa: E402

WS = date(2026, 7, 6)


def _run(day, mins, load=50, name="run"):
    return {"start_date_local": f"2026-07-{day:02d}T00:00:00", "type": "Run",
            "category": "WORKOUT", "load_target": load,
            "moving_time": mins * 60, "name": name}


def _ride(day, load):
    return {"start_date_local": f"2026-07-{day:02d}T00:00:00", "type": "Ride",
            "category": "WORKOUT", "load_target": load, "name": "ride"}


class TestRunVolumeChecks:
    def test_weekly_cap_breach_is_hard(self):
        rep = validate_week([_run(6, 90), _run(8, 90), _run(10, 90)], WS,
                            run_week_min_cap=200, run_long_min_cap=120)
        assert any(v.code == "run_weekly_volume" and v.severity == "hard"
                   for v in rep.violations)

    def test_long_run_cap_breach_is_hard(self):
        rep = validate_week([_run(6, 150, name="mega long run")], WS,
                            run_week_min_cap=300, run_long_min_cap=120)
        assert any(v.code == "run_long_volume" for v in rep.violations)

    def test_compliant_runs_pass(self):
        rep = validate_week([_run(6, 60), _run(8, 80)], WS,
                            run_week_min_cap=200, run_long_min_cap=120)
        assert not [v for v in rep.violations if v.code.startswith("run_")]

    def test_missing_cap_is_skipped_not_silent(self):
        rep = validate_week([_run(6, 60)], WS)
        assert any("run_weekly_volume" in s and "SKIPPED" in s for s in rep.skipped)

    def test_no_runs_no_noise(self):
        rep = validate_week([_ride(6, 100)], WS)
        assert not any("run" in s for s in rep.skipped)

    def test_missing_duration_is_flagged(self):
        ev = _run(6, 0)
        rep = validate_week([ev], WS, run_week_min_cap=200, run_long_min_cap=120)
        assert any("no" in s and "duration" in s for s in rep.skipped)


class TestMonotony:
    def test_flat_week_fires_soft(self):
        evs = [_ride(d, 80) for d in range(6, 13)]        # 7 identical days, no rest
        rep = validate_week(evs, WS)
        v = [v for v in rep.violations if v.code == "monotony"]
        assert v and v[0].severity == "soft"

    def test_varied_week_with_rest_days_is_quiet(self):
        evs = [_ride(6, 60), _ride(8, 120), _ride(10, 40), _ride(11, 200)]
        rep = validate_week(evs, WS)
        assert not [v for v in rep.violations if v.code == "monotony"]


class TestRunCapsHelper:
    class _Client:
        def __init__(self, acts): self._a = acts
        def get_training_history(self, days=30): return self._a

    def _hist(self, km_per_wk):
        acts = []
        for w, km in enumerate(km_per_wk):
            acts.append({"type": "Run", "start_date_local": f"2026-06-{2 + 7*w:02d}T07:00:00",
                         "distance": km * 1000, "moving_time": km * 6 * 60})
        return acts

    def test_caps_use_best_of_last4_x110_with_floor(self):
        caps = pt.run_caps(self._Client(self._hist([20, 30, 25, 28])), today=date(2026, 7, 6))
        assert caps["weekly_km_cap"] == 33.0            # 30 x 1.10
        assert caps["weekly_min_cap"] == round(30 * 6 * 1.10)
        caps2 = pt.run_caps(self._Client(self._hist([10, 12])), today=date(2026, 7, 6))
        assert caps2["weekly_km_cap"] == 25.0           # floor: normal band top

    def test_failure_returns_none_not_silence(self):
        class Boom:
            def get_training_history(self, days=30): raise RuntimeError("icu down")
        caps = pt.run_caps(Boom(), today=date(2026, 7, 6))
        assert caps["weekly_km_cap"] is None and caps["long_run_min_cap"] is None
