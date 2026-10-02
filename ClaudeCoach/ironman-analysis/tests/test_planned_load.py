"""The Load a planned session shows on Peak's calendar and Form chart (refresh-site-data
_planned_load; Fred, 2 Oct 2026): a by-feel ride falls back to its planned Load, a
baseline ramp uses its planned Load rather than Intervals.icu's price for the whole
ramp, and a planned strength session shows the athlete's own typical Load."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]            # ClaudeCoach/
sys.path.insert(0, str(REPO / "lib"))
sys.path.insert(0, str(REPO / "ironman-analysis"))
_spec = importlib.util.spec_from_file_location("refresh_site_data",
                                               REPO / "scripts" / "refresh-site-data.py")
R = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(R)


def test_by_feel_ride_uses_its_planned_load():
    assert R._planned_load({"type": "Ride", "name": "Long ride (easy)", "load_target": 32}) == 32


def test_intervals_figure_wins_when_there_is_one():
    assert R._planned_load({"type": "Ride", "name": "Sweetspot", "icu_training_load": 60,
                            "load_target": 55}) == 60


def test_baseline_ramp_uses_the_planned_load():
    assert R._planned_load({"type": "Ride", "name": "\U0001f52c Baseline: ramp test",
                            "icu_training_load": 201, "load_target": 50}) == 50


def test_strength_uses_the_athletes_own_typical_load():
    hist = [{"type": "WeightTraining", "icu_training_load": x} for x in (17, 18, 13, 17)]
    hist.append({"type": "Ride", "icu_training_load": 90})
    assert R._strength_load(hist) == 17
    assert R._planned_load({"type": "WeightTraining", "name": "Strength"}, 17) == 17
    assert R._planned_load({"type": "WeightTraining", "name": "Strength", "moving_time": 2400}) == 16
    assert R._planned_load({"type": "WeightTraining", "name": "Strength"}) == 15
