"""Tests for primitives/run_durability.py — within-run form-fade metrics."""
from __future__ import annotations

import pytest

from primitives.run_durability import (
    compute_run_durability, fade_line,
    MIN_DURATION_S, WARMUP_SKIP_S,
)


def _streams(n_s=3600, watts=300.0, hr_start=140.0, hr_end=140.0,
             cad_start=85.0, cad_end=85.0, v_start=3.2, v_end=3.2):
    """Synthetic per-second streams with linear drift between start and end."""
    time_s, watts_l, hr_l, cad_l, v_l = [], [], [], [], []
    for i in range(n_s):
        f = i / max(n_s - 1, 1)
        time_s.append(i)
        watts_l.append(watts)
        hr_l.append(hr_start + (hr_end - hr_start) * f)
        cad_l.append(cad_start + (cad_end - cad_start) * f)
        v_l.append(v_start + (v_end - v_start) * f)
    return time_s, watts_l, hr_l, cad_l, v_l


class TestComputeRunDurability:
    def test_steady_run_shows_no_fade(self):
        m = compute_run_durability(*_streams())
        assert m is not None
        assert abs(m["decoupling_pct"]) < 0.5
        assert abs(m["cadence_fade_pct"]) < 0.5
        assert abs(m["cost_fade_pct"]) < 0.5
        assert m["flags"] == []

    def test_fading_run_flags_all_three(self):
        # HR drifts up 10%, cadence drops 6%, speed drops 10% at constant watts
        m = compute_run_durability(*_streams(hr_start=140, hr_end=154,
                                             cad_start=86, cad_end=80,
                                             v_start=3.3, v_end=2.95))
        assert m["decoupling_pct"] > 3.0          # power:HR efficiency lost
        assert m["cadence_fade_pct"] < -3.0
        assert m["cost_fade_pct"] > 5.0           # each m/s costs more watts
        assert len(m["flags"]) >= 2

    def test_short_run_not_judged(self):
        assert compute_run_durability(*_streams(n_s=WARMUP_SKIP_S + MIN_DURATION_S - 60)) is None

    def test_no_power_returns_none(self):
        t, w, hr, cad, v = _streams()
        assert compute_run_durability(t, None, hr, cad, v) is None
        assert compute_run_durability(t, [0.0] * len(t), hr, cad, v) is None

    def test_walk_break_samples_excluded(self):
        # 10-min standstill mid-run must not poison the windows
        t, w, hr, cad, v = _streams()
        for i in range(1800, 2400):
            v[i] = 0.0
        m = compute_run_durability(t, w, hr, cad, v)
        assert m is not None and abs(m["cost_fade_pct"]) < 1.0

    def test_missing_cadence_still_computes(self):
        t, w, hr, cad, v = _streams()
        m = compute_run_durability(t, w, hr, None, v)
        assert m is not None and m["cadence_fade_pct"] is None


class TestFadeLine:
    def test_renders_all_metrics_and_warns_on_flags(self):
        m = {"decoupling_pct": 6.1, "cadence_fade_pct": -3.4, "cost_fade_pct": 5.2,
             "flags": ["decoupling 6.1% > 5.0%"]}
        line = fade_line(m)
        assert "6.1%" in line and "-3.4%" in line and "+5.2%" in line and "⚠" in line

    def test_clean_run_no_warning(self):
        m = {"decoupling_pct": 1.2, "cadence_fade_pct": 0.3, "cost_fade_pct": -0.5, "flags": []}
        assert "⚠" not in fade_line(m)
