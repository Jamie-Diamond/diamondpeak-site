"""Tests for primitives/planned_tss.py — deterministic planned-session TSS.

Pinned by the 11 Jun bug: a planned swim with load_target=60 was shown as
"~35 TSS" because the model was told to read icu_training_load (null) and then
estimate. The plan's own number must always win.
"""
from __future__ import annotations

from primitives.planned_tss import planned_session_tss, planned_sessions_block


def _ev(name="CSS swim ~46 min", type_="Swim", load_target=None,
        icu_load=None, moving_time=None, category="WORKOUT"):
    return {"name": name, "type": type_, "load_target": load_target,
            "icu_training_load": icu_load, "moving_time": moving_time,
            "category": category}


class TestPlannedSessionTss:
    def test_plan_load_target_always_wins(self):
        # The 11 Jun case: load_target 60, icu_training_load null
        r = planned_session_tss(_ev(load_target=60))
        assert r["tss"] == 60 and r["source"] == "plan"

    def test_icu_load_second(self):
        r = planned_session_tss(_ev(icu_load=48))
        assert r["tss"] == 48 and r["source"] == "icu"

    def test_css_swim_calculated_near_plan_value(self):
        # 46 min CSS session at IF 0.85 → ~55 TSS (vs the old guess of ~25)
        r = planned_session_tss(_ev())
        assert r["source"] == "calculated"
        assert 50 <= r["tss"] <= 60
        assert r["duration_min"] == 46          # parsed from the name

    def test_z2_ride_calculated(self):
        r = planned_session_tss(_ev(name="Long Z2 ride", type_="Ride", moving_time=4 * 3600))
        assert r["duration_min"] == 240
        assert 160 <= r["tss"] <= 180           # 4h × 0.65² × 100 = 169

    def test_strength_flat_rate(self):
        r = planned_session_tss(_ev(name="Strength 40 min", type_="WeightTraining"))
        assert r["tss"] == 20

    def test_hr_min_name_parse(self):
        r = planned_session_tss(_ev(name="4hr 30min Z2 ride", type_="Ride"))
        assert r["duration_min"] == 270


class TestPlannedSessionsBlock:
    def test_renders_workouts_only_with_source(self):
        evs = [_ev(load_target=60),
               _ev(name="note", type_="Run", category="NOTE")]
        block = planned_sessions_block(evs)
        # "Load", not "TSS" — athlete-facing wording since fc2c109
        assert "60 Load (from plan)" in block
        assert "note" not in block

    def test_empty_when_rest_day(self):
        assert planned_sessions_block([]) == ""


class TestHourlyRatesLine:
    def test_rates_derive_from_if_table(self):
        from primitives.planned_tss import hourly_rates_line
        line = hourly_rates_line()
        assert "Z2 ride 42/hr" in line          # 0.65² × 100
        assert "threshold ride 81/hr" in line   # 0.90² × 100
        assert "CSS swim 72/hr" in line         # 0.85² × 100
