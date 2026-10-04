"""The sign-up baseline week gets a real load target, and does not demote the week after.

Live failure, 4 Oct 2026. James (CTL 69.6, two weeks after Ironman Cervia) was in his
baseline week. required_tss knew nothing of baseline weeks, read it as goal block 1 week 1,
demoted it to "easy" off his post-race week and returned weekly_tss_floor 0. The coach
told him "The floor for this baseline week is 0". Jamie: 0 is wrong, it needs a real
baseline.
"""
from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]            # ClaudeCoach/
sys.path.insert(0, str(REPO / "lib"))
import baseline  # noqa: E402
import goals  # noqa: E402
import plan_tools as pt  # noqa: E402

BASELINE_MON = date(2026, 10, 5)
BLOCK1_MON = date(2026, 10, 12)


@pytest.fixture
def james(tmp_path, monkeypatch):
    monkeypatch.setattr(baseline, "BASE", tmp_path)
    monkeypatch.setattr(goals, "BASE", tmp_path)
    d = tmp_path / "athletes" / "james"
    d.mkdir(parents=True)
    (d / "baseline.json").write_text(json.dumps(
        {"status": "active", "window": {"start": "2026-10-03", "end": "2026-10-11"}}))
    return {"goal": goals.make("fitter", today=date(2026, 10, 2), sports=["swim", "bike", "run"])}


def test_the_baseline_week_has_a_real_floor(james):
    r = pt.required_tss(james, 69.6, today=BASELINE_MON, last_week_tss=242, slug="james")
    assert r["week_type"] == "baseline"
    assert r["weekly_tss_floor"] == 244                      # half of 7 x CTL
    assert r["recommended_weekly_tss"] == 341
    assert "BASELINE WEEK" in r["note"]


def test_the_baseline_week_is_a_down_week():
    assert "baseline" in pt.DOWN_WEEK_TYPES


def test_a_light_baseline_week_does_not_demote_block_1(james):
    r = pt.required_tss(james, 69.6, today=BLOCK1_MON, last_week_tss=128, slug="james")
    assert r["goal_week_kind"] == "load"
    assert "return_step_cap" not in r
    assert r["weekly_tss_floor"] > 0


def test_no_fitness_yet_gives_no_number_rather_than_zero(james):
    r = pt.required_tss(james, None, today=BASELINE_MON, slug="james")
    assert r["weekly_tss_floor"] is None and "No Fitness" in r["note"]


def test_a_completed_baseline_changes_nothing(james, tmp_path):
    p = tmp_path / "athletes" / "james" / "baseline.json"
    p.write_text(json.dumps({**json.loads(p.read_text()), "status": "complete"}))
    assert pt.required_tss(james, 69.6, today=BASELINE_MON, slug="james")["week_type"] != "baseline"
