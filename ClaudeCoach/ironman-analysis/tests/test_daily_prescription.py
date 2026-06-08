"""Tests for the prescription backstop in scripts/daily-prescription.py (WS E #14).

Hermetic: the icu_fetch network call and the per-athlete file reads are stubbed.
Covers the assembly (event → planned, nested ankle parse) and the shadow logging.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]            # ClaudeCoach/
DP = REPO / "scripts" / "daily-prescription.py"


@pytest.fixture(scope="module")
def dp():
    spec = importlib.util.spec_from_file_location("daily_prescription", DP)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _events(*rows):
    return [{"category": "WORKOUT", "type": t, "name": n,
             "load_target": load, "moving_time": load * 36} for (t, n, load) in rows]


class TestTodaysPlanned:
    def test_picks_highest_load_and_classifies(self, dp, monkeypatch):
        evs = _events(("WeightTraining", "Strength 35 min", 20),
                      ("Ride", "Build ride (3x20 sweet spot)", 180))
        monkeypatch.setattr(dp, "_icu", lambda *a, **k: evs)
        p = dp._todays_planned("x", "2026-06-19")
        assert p["session_type"] == "bike_threshold"   # the ride, not the strength
        assert p["total_duration_min"] > 0

    def test_none_when_no_workout(self, dp, monkeypatch):
        monkeypatch.setattr(dp, "_icu", lambda *a, **k: [])
        assert dp._todays_planned("x", "2026-06-19") is None


class TestAnkleParse:
    def test_nested_ankle_block(self, dp, monkeypatch, tmp_path):
        adir = tmp_path / "athletes" / "x"
        adir.mkdir(parents=True)
        (adir / "current-state.json").write_text(
            '{"ankle": {"pain_during": 3, "pain_next_morning": 1, '
            '"four_pain_free_weeks_reached": false}}')
        monkeypatch.setattr(dp, "BASE", tmp_path)
        pain, cleared = dp._ankle_state("x")
        assert pain == 3 and cleared is False

    def test_missing_file_is_unrestricted(self, dp, monkeypatch, tmp_path):
        monkeypatch.setattr(dp, "BASE", tmp_path)
        assert dp._ankle_state("nobody") == (0, True)


class TestPrescriptionShadow:
    def test_logs_engine_prescription(self, dp, monkeypatch, tmp_path):
        evs = _events(("Run", "Tue — 5x800m intervals", 60))   # run_quality
        monkeypatch.setattr(dp, "_icu",
                            lambda slug, ep, *a: evs if ep == "events" else [])
        monkeypatch.setattr(dp, "_latest_fitness", lambda s: (140.0, 100.0))  # big ATL gap
        monkeypatch.setattr(dp, "_hrv_trend_and_sleep", lambda s: (-9.0, 6.0))
        monkeypatch.setattr(dp, "_last_rpe", lambda s: 8)
        monkeypatch.setattr(dp, "_ankle_state", lambda s: (0, True))
        log = tmp_path / "p.log"
        monkeypatch.setattr(dp, "LOG_FILE", log)
        monkeypatch.setenv("PRESCRIPTION_BACKSTOP", "shadow")
        dp._prescription_shadow("x", {})
        text = log.read_text()
        assert "BACKSTOP (shadow)" in text
        assert "run_quality" in text
        assert "shadow mode" in text          # states it's not authoritative

    def test_off_switch_skips(self, dp, monkeypatch, tmp_path):
        log = tmp_path / "p.log"
        monkeypatch.setattr(dp, "LOG_FILE", log)
        monkeypatch.setenv("PRESCRIPTION_BACKSTOP", "off")
        dp._prescription_shadow("x", {})
        assert not log.exists()

    def test_soft_fails(self, dp, monkeypatch, tmp_path):
        def boom(*a, **k):
            raise RuntimeError("icu down")
        monkeypatch.setattr(dp, "_icu", boom)
        monkeypatch.setattr(dp, "LOG_FILE", tmp_path / "p.log")
        monkeypatch.setenv("PRESCRIPTION_BACKSTOP", "shadow")
        dp._prescription_shadow("x", {})     # must not raise
