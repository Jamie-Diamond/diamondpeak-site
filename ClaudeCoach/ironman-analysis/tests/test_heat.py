"""Tests for lib/heat.py — heat-protocol state and ambient-exposure dosing."""
from __future__ import annotations

import json
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]            # ClaudeCoach/
sys.path.insert(0, str(REPO / "lib"))
import heat  # noqa: E402


def _act(temp=27.0, mins=90, type_="Ride", trainer=None, tss=None, avg_hr=None):
    return {"id": 12345, "average_temp": temp, "moving_time": mins * 60,
            "type": type_, "trainer": trainer, "icu_training_load": tss,
            "average_heartrate": avg_hr,
            "start_date_local": "2026-07-01T10:00:00"}


class TestExposureEntry:
    def test_long_hot_outdoor_ride_full_base_dose(self):
        e = heat.exposure_entry(_act(temp=28.5, mins=95))
        assert e is not None
        assert e["base_dose"] == heat.base_dose(95)
        assert e["temperature_c"] == 28.5
        assert e["date"] == "2026-07-01"

    def test_short_hot_session_half_base_dose(self):
        e = heat.exposure_entry(_act(mins=40))
        assert e["base_dose"] == heat.base_dose(40)

    def test_hotter_session_scores_higher(self):
        cool = heat.exposure_entry(_act(temp=26.0, mins=90))
        hot = heat.exposure_entry(_act(temp=37.0, mins=90))
        assert hot["dose"] > cool["dose"]

    def test_harder_session_scores_higher(self):
        easy = heat.exposure_entry(_act(temp=30.0, mins=90, tss=45))
        hard = heat.exposure_entry(_act(temp=30.0, mins=90, tss=130))
        assert hard["dose"] > easy["dose"]

    def test_reference_session_unweighted(self):
        # 30°C, 65% HRR (avg_hr 135.55 at rhr 53 / max 180), no dew-point → all 1.0
        e = heat.exposure_entry(_act(temp=30.0, mins=90, avg_hr=135.55))
        assert e["temp_mult"] == 1.0
        assert e["hr_strain_mult"] == 1.0
        assert e["humidity_mult"] == 1.0
        assert e["dose"] == heat.base_dose(90)

    def test_missing_hr_and_tss_neutral_strain(self):
        e = heat.exposure_entry(_act(temp=30.0, mins=90, tss=None, avg_hr=None))
        assert e["hr_strain_mult"] == 1.0

    def test_no_external_weather_neutral_humidity(self):
        # _act has no GPS, so no dew-point lookup → humidity multiplier stays 1.0
        e = heat.exposure_entry(_act(temp=33.0, mins=90))
        assert e["humidity_mult"] == 1.0
        assert "dew_point_c" not in e

    def test_below_ambient_threshold_no_credit(self):
        assert heat.exposure_entry(_act(temp=22.0)) is None

    def test_too_short_no_credit(self):
        assert heat.exposure_entry(_act(mins=25)) is None

    def test_no_temperature_no_credit(self):
        assert heat.exposure_entry(_act(temp=None)) is None

    def test_indoor_sessions_excluded(self):
        assert heat.exposure_entry(_act(trainer=True)) is None
        assert heat.exposure_entry(_act(type_="VirtualRide")) is None


class TestDoseMultipliers:
    def test_reference_is_neutral(self):
        # 30°C, 65% HRR, 16°C dew-point → all three multipliers == 1.0
        assert heat.dose_multipliers(
            30.0, avg_hr=135.55, dew_point_c=16.0) == (1.0, 1.0, 1.0)

    def test_temp_above_reference_boosts(self):
        assert heat.dose_multipliers(37.0)[0] > 1.0

    def test_temp_below_reference_discounts(self):
        assert heat.dose_multipliers(25.0)[0] < 1.0

    def test_temp_clamped(self):
        assert heat.dose_multipliers(60.0)[0] == heat.DOSE_TEMP_MAX
        assert heat.dose_multipliers(0.0)[0] == heat.DOSE_TEMP_MIN

    def test_hr_strain_above_reference_boosts(self):
        assert heat.dose_multipliers(30.0, avg_hr=160)[1] > 1.0

    def test_hr_strain_below_reference_discounts(self):
        assert heat.dose_multipliers(30.0, avg_hr=110)[1] < 1.0

    def test_hr_strain_clamped(self):
        assert heat.dose_multipliers(30.0, avg_hr=300)[1] == heat.DOSE_HR_MAX
        assert heat.dose_multipliers(30.0, avg_hr=53)[1] == heat.DOSE_HR_MIN

    def test_tss_fallback_when_no_hr(self):
        # 600 TSS over 60 min = 600 TSS/hr → clamps to TSS-proxy ceiling
        assert heat.dose_multipliers(30.0, tss=600, mins=60)[1] == heat.DOSE_INT_MAX

    def test_missing_strain_inputs_neutral(self):
        assert heat.dose_multipliers(30.0)[1] == 1.0
        assert heat.dose_multipliers(30.0, tss=90, mins=0)[1] == 1.0

    def test_humidity_above_reference_boosts(self):
        assert heat.dose_multipliers(30.0, dew_point_c=22.0)[2] > 1.0

    def test_humidity_below_reference_discounts(self):
        assert heat.dose_multipliers(30.0, dew_point_c=8.0)[2] < 1.0

    def test_humidity_clamped(self):
        assert heat.dose_multipliers(30.0, dew_point_c=40.0)[2] == heat.DOSE_DP_MAX
        assert heat.dose_multipliers(30.0, dew_point_c=-30.0)[2] == heat.DOSE_DP_MIN

    def test_missing_dewpoint_neutral_humidity(self):
        assert heat.dose_multipliers(30.0, avg_hr=140)[2] == 1.0


class TestState:
    @pytest.fixture
    def athlete(self, monkeypatch, tmp_path):
        monkeypatch.setattr(heat, "BASE", tmp_path)
        ref = tmp_path / "athletes" / "x" / "reference"
        ref.mkdir(parents=True)
        return ref / "training-blueprint.json"

    def _write(self, sidecar, active, starts):
        sidecar.write_text(json.dumps(
            {"env_protocols": {"heat": {"active": active, "starts": starts}}}))

    def test_inactive_when_race_not_hot(self, athlete):
        self._write(athlete, False, None)
        s = heat.state("x")
        assert s == {"active": False, "starts": None,
                     "in_protocol_window": False, "maintenance": False}

    def test_active_but_paused_before_starts(self, athlete):
        future = (date.today() + timedelta(days=30)).isoformat()
        self._write(athlete, True, future)
        s = heat.state("x")
        assert s["active"] is True
        assert s["in_protocol_window"] is False

    def test_in_window_from_starts(self, athlete):
        self._write(athlete, True, date.today().isoformat())
        assert heat.state("x")["in_protocol_window"] is True

    def test_profile_kill_switch_wins(self, athlete):
        self._write(athlete, True, date.today().isoformat())
        s = heat.state("x", {"heat_protocol": False})
        assert s["active"] is False

    def test_missing_sidecar_inactive(self, monkeypatch, tmp_path):
        monkeypatch.setattr(heat, "BASE", tmp_path)
        assert heat.state("nobody")["active"] is False

    def test_maintenance_is_optin(self, athlete):
        future = (date.today() + timedelta(days=30)).isoformat()
        self._write(athlete, True, future)
        assert heat.state("x")["maintenance"] is False                     # default: silent pre-window
        assert heat.state("x", {"heat_maintenance": True})["maintenance"] is True
        # opt-in means nothing if heat itself is inactive
        self._write(athlete, False, None)
        assert heat.state("x", {"heat_maintenance": True})["maintenance"] is False


class TestBaseDose:
    # Calibration anchors: the saturating base curve is tuned to hit these
    # two points exactly (base_dose(30)=0.5, base_dose(60)=1.0).
    def test_anchor_30min_is_half_dose(self):
        assert heat.base_dose(30) == 0.5

    def test_anchor_60min_is_full_dose(self):
        assert heat.base_dose(60) == 1.0
