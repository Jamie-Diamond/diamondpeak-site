"""Tests for lib/recovery_score.py — taper-aware TSB scoring.

The normal scorer treats TSB ≥ +10 as 'too fresh' (detraining risk). During
taper that freshness is the GOAL (+5…+15 by race eve), so correct taper days
must not be marked AMBER.
"""
from __future__ import annotations

import json
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]            # ClaudeCoach/
sys.path.insert(0, str(REPO / "lib"))
import recovery_score as rs  # noqa: E402


class TestTaperTsb:
    def test_race_week_freshness_scores_green_in_taper(self):
        # TSB +12, good HRV/sleep, no pain — race-week perfection
        normal = rs.compute(hrv_today=45, hrv_baseline=44, tsb=12, sleep_hrs=8.0, pain=0)
        taper = rs.compute(hrv_today=45, hrv_baseline=44, tsb=12, sleep_hrs=8.0, pain=0,
                           in_taper=True)
        assert normal["signals"]["tsb"]["score"] == 65       # 'too fresh' normally
        assert taper["signals"]["tsb"]["score"] == 95        # on target in taper
        assert taper["label"] == "GREEN"

    def test_heavy_fatigue_in_taper_still_flags(self):
        taper = rs.compute(hrv_today=40, hrv_baseline=44, tsb=-20, sleep_hrs=7.0, pain=0,
                           in_taper=True)
        assert taper["signals"]["tsb"]["score"] == 40        # fatigued in taper = wrong

    def test_default_behaviour_unchanged(self):
        a = rs.compute(hrv_today=45, hrv_baseline=44, tsb=-8, sleep_hrs=7.5, pain=0)
        b = rs.compute(hrv_today=45, hrv_baseline=44, tsb=-8, sleep_hrs=7.5, pain=0,
                       in_taper=False)
        assert a == b


class TestInTaper:
    @pytest.fixture
    def sidecar(self, monkeypatch, tmp_path):
        monkeypatch.setattr(rs, "ROOT", tmp_path)
        ref = tmp_path / "athletes" / "x" / "reference"
        ref.mkdir(parents=True)
        return ref / "training-blueprint.json"

    def _write(self, sidecar, phases):
        sidecar.write_text(json.dumps({"phases": phases}))

    def test_true_inside_taper_window(self, sidecar):
        today = date.today()
        self._write(sidecar, [{"name": "Taper",
                               "start": (today - timedelta(days=2)).isoformat(),
                               "end": (today + timedelta(days=12)).isoformat()}])
        assert rs.in_taper("x") is True

    def test_false_in_build(self, sidecar):
        today = date.today()
        self._write(sidecar, [
            {"name": "Build", "start": (today - timedelta(days=10)).isoformat(),
             "end": (today + timedelta(days=10)).isoformat()},
            {"name": "Taper", "start": (today + timedelta(days=11)).isoformat(),
             "end": (today + timedelta(days=30)).isoformat()},
        ])
        assert rs.in_taper("x") is False

    def test_missing_sidecar_false(self, monkeypatch, tmp_path):
        monkeypatch.setattr(rs, "ROOT", tmp_path)
        assert rs.in_taper("nobody") is False
