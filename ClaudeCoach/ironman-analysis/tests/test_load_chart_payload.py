"""Regression tests for the morning load chart payload (scripts/morning-checkin.py).

History: "today's load missing from the chart" recurred three times (May–Jun 2026)
because the builder was buried in the send function where no test could reach it,
and each earlier fix repaired a neighbouring fault (sport normalisation, send
timeout, card-text TSS) without touching the today-exclusion. These tests pin the
actual complaint: TODAY'S BAR MUST SHOW THE PLANNED SESSION AT MORNING SEND TIME.
"""
from __future__ import annotations

import importlib.util
from datetime import date, timedelta
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]            # ClaudeCoach/
MC = REPO / "scripts" / "morning-checkin.py"

TODAY = date(2026, 6, 10)


@pytest.fixture(scope="module")
def mc():
    spec = importlib.util.spec_from_file_location("morning_checkin", MC)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _planned(d, sport, tss=45):
    return {"start_date_local": f"{d}T00:00:00", "type": sport,
            "category": "WORKOUT", "load_target": tss, "moving_time": 3600}


def _completed(d, sport, tss=50):
    return {"start_date_local": f"{d}T08:00:00", "type": sport,
            "icu_training_load": tss, "moving_time": 3600}


def _day(payload, d):
    return next(x for x in payload["days"] if x["date"] == d.isoformat())


class TestTodayBar:
    def test_morning_send_shows_todays_planned_session(self, mc):
        """THE bug: at 06:30 nothing is completed — today must show the plan."""
        payload = mc._build_load_chart_payload(
            TODAY, wellness_rows=[], history_acts=[],
            events=[_planned(TODAY, "Run", 45)])
        acts = _day(payload, TODAY)["activities"]
        assert acts == [{"sport": "Run", "tss": 45, "dur": 60, "status": "planned"}]

    def test_completed_sport_drops_its_planned_twin(self, mc):
        """Re-send after training: the run is done — no double bar."""
        payload = mc._build_load_chart_payload(
            TODAY, wellness_rows=[],
            history_acts=[_completed(TODAY, "Run", 52)],
            events=[_planned(TODAY, "Run", 45), _planned(TODAY, "WeightTraining", 20)])
        acts = _day(payload, TODAY)["activities"]
        statuses = {(a["sport"], a["status"]) for a in acts}
        assert ("Run", "completed") in statuses
        assert ("Run", "planned") not in statuses          # twin dropped
        assert ("Strength", "planned") in statuses         # unrelated plan kept

    def test_future_days_show_planned_past_days_show_completed(self, mc):
        yesterday, tomorrow = TODAY - timedelta(days=1), TODAY + timedelta(days=1)
        payload = mc._build_load_chart_payload(
            TODAY, wellness_rows=[],
            history_acts=[_completed(yesterday, "Ride", 120)],
            events=[_planned(tomorrow, "Swim", 35)])
        assert _day(payload, yesterday)["activities"][0]["status"] == "completed"
        assert _day(payload, tomorrow)["activities"][0]["status"] == "planned"

    def test_standard_sport_names_survive_normalisation(self, mc):
        """Regression for the 20 May bug: Run/Ride/Swim planned events were
        renamed 'Other' and discarded by the renderer."""
        payload = mc._build_load_chart_payload(
            TODAY, wellness_rows=[], history_acts=[],
            events=[_planned(TODAY, "Ride"), _planned(TODAY + timedelta(days=2), "Swim")])
        assert _day(payload, TODAY)["activities"][0]["sport"] == "Ride"
        assert _day(payload, TODAY + timedelta(days=2))["activities"][0]["sport"] == "Swim"

    def test_window_is_sixteen_days_with_today_marked(self, mc):
        payload = mc._build_load_chart_payload(TODAY, [], [], [])
        assert len(payload["days"]) == 16
        assert payload["today"] == "06-10"
        assert payload["days"][8]["date"] == TODAY.isoformat()
