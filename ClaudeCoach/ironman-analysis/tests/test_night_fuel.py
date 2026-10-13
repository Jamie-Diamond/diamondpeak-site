"""Night-before brief: the Nutrition line is worked out in code (shadow week, 13 Oct 2026)."""
import importlib.util
import sys
from pathlib import Path

CC = Path(__file__).resolve().parents[2]
sys.argv = ["x"]
_spec = importlib.util.spec_from_file_location("nbb", CC / "scripts" / "night-before-brief.py")
nb = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(nb)


def test_short_or_easy_needs_nothing():
    assert nb._fuel_line([{"type": "Run", "moving_time": 2280, "icu_training_load": 35}], {}, []) \
        == "0 g/hr carbs, water to thirst"
    assert nb._fuel_line([{"type": "Swim", "moving_time": 5400}], {}, []) == "0 g/hr carbs, water to thirst"
    assert nb._fuel_line([{"type": "Ride", "moving_time": 4500, "icu_training_load": 50}], {}, []) \
        == "0 g/hr carbs, water to thirst"


def test_hour_plus_with_work_gets_30():
    assert nb._fuel_line([{"type": "Ride", "moving_time": 4500, "icu_training_load": 74}], {}, []) \
        == "30 g/hr carbs + 500 ml/hr fluid"


def test_long_ride_uses_the_athletes_ramp():
    line = nb._fuel_line([{"type": "Ride", "moving_time": 12600, "icu_training_load": 180}],
                         {"nutrition_target_g_hr": 90}, [])
    assert line.endswith("g/hr carbs + 500-750 ml/hr fluid") and not line.startswith("0 ")
