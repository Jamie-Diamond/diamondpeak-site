"""Tests for lib/menstrual.py — cycle phase state, anchoring, and forecasting."""
from __future__ import annotations

import json
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]            # ClaudeCoach/
sys.path.insert(0, str(REPO / "lib"))
import menstrual  # noqa: E402


@pytest.fixture
def athlete(monkeypatch, tmp_path):
    """Isolated athlete dir with tracking enabled and an anchor on 2026-06-10."""
    monkeypatch.setattr(menstrual, "BASE", tmp_path)
    adir = tmp_path / "athletes" / "x"
    adir.mkdir(parents=True)
    (adir / "profile.json").write_text(json.dumps({"menstrual_tracking": True}))
    (adir / "current-state.json").write_text(json.dumps(
        {"menstrual_cycle": {"last_period_start": "2026-06-10",
                             "cycle_length_days": 28}}))
    return adir


class TestPhaseFromDay:
    def test_phase_boundaries_28d(self):
        assert menstrual.phase_from_day(1) == "menstrual"
        assert menstrual.phase_from_day(5) == "menstrual"
        assert menstrual.phase_from_day(6) == "follicular"
        assert menstrual.phase_from_day(13) == "follicular"
        assert menstrual.phase_from_day(14) == "ovulation"
        assert menstrual.phase_from_day(15) == "luteal"
        assert menstrual.phase_from_day(28) == "luteal"

    def test_ovulation_scales_with_cycle_length(self):
        # luteal is ~fixed at 14 days → 32-day cycle ovulates day 18
        assert menstrual.phase_from_day(18, 32) == "ovulation"
        assert menstrual.phase_from_day(17, 32) == "follicular"

    def test_short_cycle_keeps_a_follicular_window(self):
        # ovulation never earlier than day 7 even for an implausibly short cycle
        assert menstrual.phase_from_day(6, 20) == "follicular"
        assert menstrual.phase_from_day(7, 20) == "ovulation"


class TestPhaseFor:
    def test_phase_on_dates_matches_coach_table(self, athlete):
        # Kathryn's coach table: 10-14 Jun menstrual, 15-22 Jun follicular,
        # ~23 Jun ovulation, 24 Jun-7 Jul luteal, next ~8 Jul
        assert menstrual.phase_for("x", date(2026, 6, 10))["phase"] == "menstrual"
        assert menstrual.phase_for("x", date(2026, 6, 14))["phase"] == "menstrual"
        assert menstrual.phase_for("x", date(2026, 6, 15))["phase"] == "follicular"
        assert menstrual.phase_for("x", date(2026, 6, 23))["phase"] == "ovulation"
        assert menstrual.phase_for("x", date(2026, 6, 24))["phase"] == "luteal"
        assert menstrual.phase_for("x", date(2026, 7, 7))["phase"] == "luteal"
        info = menstrual.phase_for("x", date(2026, 6, 12))
        assert info["day"] == 3 and info["next_period_expected"] == "2026-07-08"

    def test_overdue_clamps_to_luteal_then_goes_stale(self, athlete):
        d29 = menstrual.phase_for("x", date(2026, 7, 8))
        assert d29["phase"] == "luteal" and d29["overdue"]
        stale = menstrual.phase_for("x", date(2026, 7, 8) + timedelta(days=menstrual.STALE_AFTER_DAYS))
        assert stale["phase"] is None and stale["overdue"]

    def test_tracking_disabled_returns_none(self, athlete):
        (athlete / "profile.json").write_text(json.dumps({"menstrual_tracking": False}))
        assert menstrual.phase_for("x", date(2026, 6, 12)) is None

    def test_no_anchor_returns_none(self, athlete):
        (athlete / "current-state.json").write_text("{}")
        assert menstrual.phase_for("x", date(2026, 6, 12)) is None

    def test_future_anchor_returns_none(self, athlete):
        assert menstrual.phase_for("x", date(2026, 6, 9)) is None

    def test_icu_wellness_overrides_same_day_only(self, athlete):
        wellness = [{"id": "2026-06-15", "menstrualPhase": "PERIOD"}]
        on_day = menstrual.phase_for("x", date(2026, 6, 15), wellness=wellness)
        assert on_day["phase"] == "menstrual" and on_day["source"] == "icu"
        other_day = menstrual.phase_for("x", date(2026, 6, 16), wellness=wellness)
        assert other_day["phase"] == "follicular" and other_day["source"] == "computed"

    def test_unknown_icu_value_ignored(self, athlete):
        wellness = [{"id": "2026-06-15", "menstrualPhase": "SOMETHING_NEW"}]
        info = menstrual.phase_for("x", date(2026, 6, 15), wellness=wellness)
        assert info["phase"] == "follicular" and info["source"] == "computed"


class TestLogging:
    def test_log_period_start_moves_anchor_and_learns_length(self, athlete):
        menstrual.log_period_start("x", date(2026, 7, 9))
        mc = menstrual.cycle_state("x")
        assert mc["last_period_start"] == "2026-07-09"
        assert mc["cycle_length_days"] == 29          # observed 10 Jun → 9 Jul gap
        assert "2026-06-10" in mc["starts"]

    def test_implausible_gap_does_not_corrupt_length(self, athlete):
        menstrual.log_period_start("x", date(2026, 9, 1))   # 83-day gap → ignored
        assert menstrual.cycle_state("x")["cycle_length_days"] == 28

    def test_set_cycle_day_backdates_anchor(self, athlete):
        menstrual.set_cycle_day("x", 8, on=date(2026, 6, 20))
        mc = menstrual.cycle_state("x")
        assert mc["last_period_start"] == "2026-06-13"
        info = menstrual.phase_for("x", date(2026, 6, 20))
        assert info["day"] == 8 and info["phase"] == "follicular"


class TestForecastBlock:
    def test_window_covers_phase_transitions(self, athlete):
        block = menstrual.forecast_block("x", date(2026, 6, 15), 14)
        assert "FOLLICULAR" in block and "OVULATION" in block and "LUTEAL" in block
        assert "2026-06-23" in block                  # ovulation day present
        assert "MENSTRUAL" not in block               # window starts day 6

    def test_empty_when_tracking_off(self, athlete):
        (athlete / "profile.json").write_text("{}")
        assert menstrual.forecast_block("x", date(2026, 6, 15), 14) == ""
