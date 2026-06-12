"""Tests for telegram/bot.py fast_path quick-logs — per-athlete and per-location
state writes (heat/weight/ankle were hardwired to athletes/jamie/ until 2026-06-12)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]            # ClaudeCoach/
sys.path.insert(0, str(REPO / "telegram"))
sys.path.insert(0, str(REPO / "lib"))
import bot        # noqa: E402
import heat       # noqa: E402
import menstrual  # noqa: E402


@pytest.fixture
def sandbox(monkeypatch, tmp_path):
    """Two isolated athletes; git side effects stubbed out."""
    monkeypatch.setattr(bot, "_git_commit", lambda msg: None)
    monkeypatch.setattr(bot, "_athlete_dir", lambda slug: tmp_path / "athletes" / slug)
    monkeypatch.setattr(heat, "BASE", tmp_path)
    monkeypatch.setattr(menstrual, "BASE", tmp_path)
    for slug, prof in (("j", {"race_weight_kg": 79.0}), ("k", {})):
        adir = tmp_path / "athletes" / slug / "reference"
        adir.mkdir(parents=True)
        (adir.parent / "profile.json").write_text(json.dumps(prof))
    return tmp_path


def _state(tmp_path, slug):
    return json.loads((tmp_path / "athletes" / slug / "current-state.json").read_text())


class TestPerAthleteIsolation:
    def test_weight_lands_in_senders_state(self, sandbox):
        bot.fast_path("weight 62.8", slug="k")
        bot.fast_path("weight 82.5", slug="j")
        assert _state(sandbox, "k")["weight_readings"][0]["kg"] == 62.8
        assert _state(sandbox, "j")["weight_readings"][0]["kg"] == 82.5

    def test_weight_target_from_own_profile(self, sandbox):
        assert "79 kg" in bot.fast_path("weight 82.5", slug="j")
        assert "target" not in bot.fast_path("weight 62.8", slug="k")

    def test_heat_lands_in_senders_log(self, sandbox):
        bot.fast_path("heat 30", slug="k")
        assert (sandbox / "athletes/k/heat-log.json").exists()
        assert not (sandbox / "athletes/j/heat-log.json").exists()
        assert _state(sandbox, "k")["heat"]["sessions_cumulative"] == 1

    def test_no_slug_skips_handler(self, sandbox):
        assert bot.fast_path("weight 80", slug="") is None
        assert bot.fast_path("heat 30", slug="") is None
        assert bot.fast_path("ankle 3", slug="") is None


class TestPerLocationPain:
    def test_bare_ankle_form_matches(self, sandbox):
        reply = bot.fast_path("ankle 3", slug="k")
        assert "ankle pain 3/10" in reply
        assert _state(sandbox, "k")["ankle"]["pain_during"] == 3

    def test_ankle_keeps_legacy_block(self, sandbox):
        bot.fast_path("ankle left 2", slug="k")
        s = _state(sandbox, "k")
        assert s["ankle"]["pain_during"] == 2
        assert "pain" not in s                       # no generic block for ankle

    def test_other_locations_track_independently(self, sandbox):
        bot.fast_path("ankle 1", slug="k")
        bot.fast_path("pain knee 4", slug="k")
        s = _state(sandbox, "k")
        assert s["pain"]["knee"]["current"] == 4
        assert len(s["pain"]["knee"]["history"]) == 1
        assert s["ankle"]["pain_during"] == 1
        assert len(s["ankle"]["history"]) == 1       # knee reading did NOT pollute ankle

    def test_trend_compares_within_location_only(self, sandbox):
        bot.fast_path("pain knee 5", slug="k")
        bot.fast_path("ankle 1", slug="k")
        reply = bot.fast_path("pain knee 3", slug="k")
        assert "down from 5" in reply                # knee vs knee, not vs ankle's 1

    def test_pain_ankle_routes_to_ankle_block(self, sandbox):
        bot.fast_path("pain ankle 2", slug="k")
        s = _state(sandbox, "k")
        assert s["ankle"]["pain_during"] == 2
        assert "pain" not in s

    def test_bare_pain_logs_general(self, sandbox):
        bot.fast_path("pain 4", slug="k")
        assert _state(sandbox, "k")["pain"]["general"]["current"] == 4

    def test_rising_alert_within_location(self, sandbox):
        bot.fast_path("pain knee 2", slug="k")
        bot.fast_path("pain knee 3", slug="k")
        reply = bot.fast_path("pain knee 4", slug="k")
        assert "Three readings rising" in reply
