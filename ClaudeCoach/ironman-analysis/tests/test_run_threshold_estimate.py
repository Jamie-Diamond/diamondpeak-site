"""Passive GAP-at-HR run-threshold estimator (audit P1-8, Phase 4 leftovers)."""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "lib"))
from thresholds import estimate_run_threshold_from_gap  # noqa: E402


class _Client:
    def __init__(self, acts): self._a = acts
    def get_training_history(self, days=90): return self._a


def _run(hr, gap, mins=40, dec=2.0, lthr=180):
    return {"type": "Run", "moving_time": mins * 60, "gap": gap,
            "average_heartrate": hr, "decoupling": dec, "lthr": lthr}


def _linear_runs(n=8, slope=0.03, intercept=-1.0):
    # speed = intercept + slope*HR across HR 130..165
    return [_run(hr, intercept + slope * hr) for hr in range(130, 130 + 5 * n, 5)]


class TestEstimator:
    def test_recovers_linear_relationship(self):
        est = estimate_run_threshold_from_gap(_Client(_linear_runs()))
        # at LTHR 180: v = -1.0 + 0.03*180 = 4.4 m/s -> 227 s/km
        assert est is not None
        assert abs(est["pace_s_per_km"] - round(1000 / 4.4)) <= 1
        assert est["lthr_used"] == 180

    def test_too_few_runs_returns_none(self):
        assert estimate_run_threshold_from_gap(_Client(_linear_runs(n=4))) is None

    def test_narrow_hr_span_returns_none(self):
        acts = [_run(140 + i % 3, 3.2 + 0.01 * i) for i in range(8)]
        assert estimate_run_threshold_from_gap(_Client(acts)) is None

    def test_drifting_runs_excluded(self):
        acts = _linear_runs(n=5) + [_run(150, 1.0, dec=15.0)] * 4   # junk excluded
        assert estimate_run_threshold_from_gap(_Client(acts)) is None  # only 5 clean left

    def test_negative_slope_rejected(self):
        acts = [_run(hr, 5.0 - 0.02 * hr) for hr in range(130, 170, 5)]
        assert estimate_run_threshold_from_gap(_Client(acts)) is None

    def test_short_runs_ignored(self):
        acts = [_run(hr, -1.0 + 0.03 * hr, mins=20) for hr in range(130, 170, 5)]
        assert estimate_run_threshold_from_gap(_Client(acts)) is None
