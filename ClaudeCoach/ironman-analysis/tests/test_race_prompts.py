"""Fuelling and heat suggestions (Jamie, 30 Sep 2026).

"For any event over 90 mins it suggests the nutrition tracking, and reminds during peak
phase if they said no. Heat: if the goal event might be hot, suggest; again in peak if
they say no." Hot = 21°C+. Nutrition = carbs / salt / water after long sessions, NOT the
Food tab (nutrition_tracker keeps its own switch).
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]            # ClaudeCoach/
sys.path.insert(0, str(REPO / "lib"))
import plan_tools as pt  # noqa: E402
import race_prompts as rp  # noqa: E402

MARATHON = {"race_name": "Brighton Marathon", "race_date": "2027-04-04", "race_goal": "sub 3"}
TEN_K = {"race_name": "Town 10k", "race_date": "2027-04-04", "race_goal": "sub 40"}


class TestNutrition:
    OFF = {"fuelling_coaching": False}

    def test_asked_only_when_fuelling_is_off_and_the_race_is_over_90_minutes(self):
        assert rp.due("nutrition", MARATHON, self.OFF, "base")["stage"] == "first"
        assert rp.due("nutrition", MARATHON, {}, "base") is None        # on by default
        assert rp.due("nutrition", TEN_K, self.OFF, "base") is None

    def test_no_is_silent_until_peak_then_asked_once(self):
        prof = rp.record(self.OFF, "nutrition", "no")
        assert prof["fuelling_coaching"] is False
        assert rp.due("nutrition", MARATHON, prof, "build") is None
        assert rp.due("nutrition", MARATHON, prof, "peak")["stage"] == "peak_reminder"
        prof = rp.mark_peak_reminded(prof, "nutrition")
        assert rp.due("nutrition", MARATHON, prof, "peak") is None

    def test_yes_uses_the_settings_switch_never_the_food_tab(self):
        prof = rp.record(self.OFF, "nutrition", "yes")
        assert prof["fuelling_coaching"] is True and "nutrition_tracker" not in prof
        assert rp.due("nutrition", MARATHON, prof, "peak") is None

    def test_the_message_says_carbs_salt_water(self):
        line = rp.message_line(rp.due("nutrition", MARATHON, self.OFF, "base"))
        assert "carbs, salt and water" in line and "fuelling yes" in line


class TestHeat:
    def test_21_and_over_is_hot(self):
        assert rp.due("heat", MARATHON, {}, "base", high_c=21.0)["stage"] == "first"
        assert rp.due("heat", MARATHON, {}, "base", high_c=20.9) is None

    def test_unknown_weather_asks_the_athlete(self):
        q = rp.due("heat", MARATHON, {}, "base", high_c=None)
        assert "21°C or warmer" in rp.message_line(q)

    def test_yes_arms_the_heat_protocol(self):
        prof = rp.record({"heat_silent": True, "heat_protocol": False}, "heat", "yes")
        assert prof["race_conditions"] == "hot" and prof["heat_protocol"] is True
        assert "heat_silent" not in prof
        assert rp.due("heat", MARATHON, prof, "peak", high_c=28) is None

    def test_no_reminds_once_in_peak(self):
        prof = rp.record({}, "heat", "no")
        assert prof["heat_protocol"] is False
        assert rp.due("heat", MARATHON, prof, "build", high_c=28) is None
        assert rp.due("heat", MARATHON, prof, "peak", high_c=28)["stage"] == "peak_reminder"


class TestWhen:
    def test_never_in_offseason_recovery_or_taper(self, monkeypatch):
        monkeypatch.setattr(rp, "venue_high_cached", lambda slug, cfg: 25.0)
        for wt in ("offseason", "post_race", "taper", "race"):
            assert rp.due_for_week("x", MARATHON, {}, "base", wt) == []
        got = rp.due_for_week("x", MARATHON, {"fuelling_coaching": False}, "base", "base")
        assert [q["topic"] for q in got] == ["nutrition", "heat"]

    def test_place_from_race_name(self):
        assert rp._place({"race_name": "Brighton Marathon"}) == "Brighton"
        assert rp._place({"race_name": "X", "race_location": "Cervia"}) == "Cervia"


def test_cli_records_the_answer(tmp_path):
    (tmp_path / "athletes" / "x").mkdir(parents=True)
    (tmp_path / "athletes" / "x" / "profile.json").write_text(json.dumps({"name": "X"}))
    out = pt.set_race_prompt("x", "nutrition", "no", base=tmp_path)
    assert out["fuelling_coaching"] is False and "peak" in out["next"]
    out = pt.set_race_prompt("x", "heat", "yes", base=tmp_path)
    assert "generate-blueprint" in out["next"]


def test_cleared_injuries_are_not_current():
    for f in ("scripts/activity-watcher.py", "scripts/morning-checkin.py",
              "scripts/evening-checkin.py", "scripts/night-before-brief.py",
              "lib/session_library.py"):
        assert '("cleared", "resolved")' in (REPO / f).read_text(), f
